"""Scheduler — the pure-core entry point.

`plan_for_new_order(snapshot, order, now)` produces a Plan that:
1. Looks up the order's pinned recipe (composite key).
2. Topologically sorts the recipe's stages (DAG support for Phase 2).
3. For each stage, picks an eligible machine and finds the earliest start
   that respects the stage's predecessor ends.
4. Returns a Plan of SlotWrites — one per stage.

Used in two contexts:
- Live scheduling (IO shell writes the Plan to Monday)
- CTP /simulate (returns projected dates, no writeback)

The function is pure: same Snapshot + order → same Plan. No IO, no clock
calls inside this function — `now` is passed by the caller.
"""

from __future__ import annotations

from datetime import datetime
from typing import NamedTuple

from engine.core.placement import find_earliest_start
from engine.core.routing import eligible_machines
from engine.models import (
    Plan,
    Recipe,
    RecipeStage,
    ScheduleNewOrder,
    SlotStatus,
    SlotWrite,
    Snapshot,
)


class DanglingRecipeError(RuntimeError):
    """Raised when the order's recipe_key + version can't be found.

    Per v3 plan invariant: engine never falls back to a different version.
    Caller should mark affected items Status=Blocked and surface to ops.
    """

    def __init__(self, recipe_key: str, recipe_version: int):
        self.recipe_key = recipe_key
        self.recipe_version = recipe_version
        super().__init__(f"Recipe not found: {recipe_key} v{recipe_version}")


class UnroutableStageError(RuntimeError):
    """Raised when a stage has no eligible machine (none online or none match)."""

    def __init__(self, stage_id: str, machine_class: str):
        self.stage_id = stage_id
        self.machine_class = machine_class
        super().__init__(
            f"No eligible machine for stage '{stage_id}' (class {machine_class})"
        )


class _StagePlacement(NamedTuple):
    """Internal — per-stage placement result before assembling the Plan."""

    stage: RecipeStage
    machine_id: str
    start: datetime
    end: datetime


def plan_for_new_order(
    snapshot: Snapshot,
    order: ScheduleNewOrder,
    *,
    now: datetime,
) -> Plan:
    """Build a Plan placing a new order's stages across eligible machines.

    Caller passes `now` so the function stays pure.
    """
    recipe = _resolve_recipe(snapshot, order)
    stages_in_topo_order = _topological_order(recipe)
    placements = _place_stages(snapshot, order, recipe, stages_in_topo_order, now=now)
    slot_writes = _build_slot_writes(order, recipe, placements)

    notes = (
        f"order={order.job_reference_id} recipe={recipe.recipe_key} v{recipe.version} "
        f"placed {len(slot_writes)} slot(s)",
    )
    return Plan(slot_writes=tuple(slot_writes), notes=notes)


# ─────────────────────────────────────────────────────────────────────────
# Recipe resolution (invariant: composite-key pinning)
# ─────────────────────────────────────────────────────────────────────────


def _resolve_recipe(snapshot: Snapshot, order: ScheduleNewOrder) -> Recipe:
    recipe = snapshot.recipe_by_composite_key(order.recipe_key, order.recipe_version)
    if recipe is None:
        raise DanglingRecipeError(order.recipe_key, order.recipe_version)
    return recipe


# ─────────────────────────────────────────────────────────────────────────
# Topological order (DAG flattening — Phase 1: single stage; Phase 2: multi)
# ─────────────────────────────────────────────────────────────────────────


def _topological_order(recipe: Recipe) -> list[RecipeStage]:
    """Kahn's algorithm. Stable order: stages with no deps first.

    Raises ValueError on cycles (recipe schema bug; should be caught at edit time).
    """
    by_id = {s.id: s for s in recipe.stages}
    indegree: dict[str, int] = {s.id: len(s.depends_on) for s in recipe.stages}
    ready: list[str] = [s.id for s in recipe.stages if not s.depends_on]
    out: list[RecipeStage] = []

    while ready:
        ready.sort()  # stable order
        nxt = ready.pop(0)
        out.append(by_id[nxt])
        # Reduce indegree of stages that depended on nxt
        for s in recipe.stages:
            if nxt in s.depends_on:
                indegree[s.id] -= 1
                if indegree[s.id] == 0:
                    ready.append(s.id)

    if len(out) != len(recipe.stages):
        raise ValueError(
            f"Recipe {recipe.recipe_key} v{recipe.version} has a cycle in stages"
        )
    return out


# ─────────────────────────────────────────────────────────────────────────
# Per-stage placement
# ─────────────────────────────────────────────────────────────────────────


def _place_stages(
    snapshot: Snapshot,
    order: ScheduleNewOrder,
    recipe: Recipe,
    stages: list[RecipeStage],
    *,
    now: datetime,
) -> list[_StagePlacement]:
    """For each stage in topo order, choose machine + start/end times."""
    placements: dict[str, _StagePlacement] = {}

    for stage in stages:
        # Earliest this stage could start = max of (predecessor ends, now).
        predecessor_ends = [
            placements[dep_id].end
            for dep_id in stage.depends_on
            if dep_id in placements
        ]
        earliest_allowed = max(predecessor_ends + [now])

        # Eligible machines for this stage's machine_class.
        candidates = eligible_machines(
            snapshot, machine_class=stage.machine_class, order=order,
        )
        if not candidates:
            raise UnroutableStageError(stage.id, stage.machine_class)

        # For each candidate, find its earliest start. Pick the machine with
        # the soonest start. Ties broken by routing order (candidates[0] wins).
        best: tuple[str, datetime, datetime] | None = None
        for machine in candidates:
            duration_hours = order.quantity / machine.capacity_per_hour
            queue = list(snapshot.slots_on_machine(machine.id))
            start, end = find_earliest_start(
                machine,
                duration_hours,
                earliest_allowed_start=earliest_allowed,
                queue=queue,
                now=now,
            )
            if best is None or start < best[1]:
                best = (machine.id, start, end)

        assert best is not None
        placements[stage.id] = _StagePlacement(
            stage=stage, machine_id=best[0], start=best[1], end=best[2]
        )

    return list(placements.values())


# ─────────────────────────────────────────────────────────────────────────
# SlotWrite assembly
# ─────────────────────────────────────────────────────────────────────────


def _build_slot_writes(
    order: ScheduleNewOrder,
    recipe: Recipe,
    placements: list[_StagePlacement],
) -> list[SlotWrite]:
    machine_name_for: dict[str, str] = {}  # filled in by caller when applying

    writes: list[SlotWrite] = []
    for p in placements:
        # Name is `{job_ref} → {Machine}`; engine doesn't know machine name here,
        # so use `{job_ref} → {stage_id}` as a placeholder. IO shell rewrites
        # to the real machine name after resolving via the snapshot.
        placeholder_name = f"{order.job_reference_id} → {p.stage.id}"
        writes.append(
            SlotWrite(
                slot_id=None,  # create
                name=placeholder_name,
                machine_id=p.machine_id,
                job_reference_id=order.job_reference_id,
                stage_id=p.stage.id,
                recipe_key=recipe.recipe_key,
                recipe_version=recipe.version,
                quantity=order.quantity,
                planned_start=p.start,
                planned_end=p.end,
                status=SlotStatus.QUEUED,
                manually_placed=False,
            )
        )

    return writes
