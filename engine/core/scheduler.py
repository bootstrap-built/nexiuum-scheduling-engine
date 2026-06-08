"""Scheduler — the pure-core entry point.

`plan_for_new_order(snapshot, order, now)` produces a Plan that:
1. Looks up the order's pinned recipe (composite key).
2. Topologically sorts the recipe's stages (DAG support for Phase 2).
3. Appends synthetic packaging stages from order.packaging_breakdown, each
   depending on the recipe DAG's terminal stage(s).
4. For each stage:
   - Single-machine stages (press, capsule, etc.) place on the eligible
     machine with the earliest valid start.
   - Packaging stages with multiple eligible machines and qty >=
     split_min_quantity fan across up to split_max_machines, with quantity
     divided proportional to capacity_per_hour.
   - Duration uses (quantity / capacity_per_hour) for item-rate machines
     and (quantity / (capacity_per_hour * items_per_container)) for
     container-rate machines in CONTAINER_CAPACITY_GROUPS.
5. Returns a Plan of SlotWrites — one per machine-chunk per stage.

Used in two contexts:
- Live scheduling (IO shell writes the Plan to Monday)
- CTP /simulate (returns projected dates, no writeback)

The function is pure: same Snapshot + order + settings → same Plan. No IO,
no clock calls inside this function — `now` is passed by the caller. Tests
can pass explicit `settings` to keep the function fully deterministic; the
production callers fall back to get_settings() when omitted.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING, Literal

from engine.core.labels import compose_slot_name
from engine.core.placement import find_earliest_start
from engine.core.routing import eligible_machines
from engine.models import (
    CONTAINER_CAPACITY_GROUPS,
    SPLITTABLE_PROCESS_GROUPS,
    Machine,
    Plan,
    ProcessGroup,
    Recipe,
    RecipeStage,
    ScheduleNewOrder,
    Slot,
    SlotStatus,
    SlotWrite,
    Snapshot,
)

if TYPE_CHECKING:
    from engine.config import Settings


class DanglingRecipeError(RuntimeError):
    """Raised when the order's recipe_key + version can't be found.

    Per v3 plan invariant: engine never falls back to a different version.
    Caller should mark affected items Status=Blocked and surface to ops.
    """

    def __init__(self, recipe_key: str, recipe_version: int):
        self.recipe_key = recipe_key
        self.recipe_version = recipe_version
        super().__init__(f"Recipe not found: {recipe_key} v{recipe_version}")


class InactiveRecipeError(RuntimeError):
    """Raised when a new order references a recipe that exists but isn't Active.

    Draft and Retired recipes are NOT usable for new orders. In-flight slots
    pinned to a retired version continue to resolve correctly (via composite
    key) — only new-order placement is gated by this check.
    """

    def __init__(self, recipe_key: str, recipe_version: int, status: str):
        self.recipe_key = recipe_key
        self.recipe_version = recipe_version
        self.status = status
        super().__init__(
            f"Recipe {recipe_key} v{recipe_version} is {status}; only Active recipes "
            "can be used for new orders"
        )


class UnroutableStageError(RuntimeError):
    """Raised when a stage has no eligible machine.

    `reason` distinguishes the failure mode for operator visibility:
    - 'no_machines_in_class' — Capacity Engine has zero machines of this class
    - 'all_machines_down' — machines exist but all are Down or zero-capacity
    - 'no_eligible_after_rules' — hard rules (dual-sided, force-route, max
       job size) eliminated all candidates
    """

    def __init__(self, stage_id: str, machine_class: str, reason: str = "no_eligible_after_rules"):
        self.stage_id = stage_id
        self.machine_class = machine_class
        self.reason = reason
        super().__init__(
            f"No eligible machine for stage '{stage_id}' (class {machine_class}): {reason}"
        )


@dataclass(frozen=True)
class _StageSpec:
    """Internal — a stage to place. May come from the recipe or from the
    order's packaging breakdown.

    `source="recipe"`: stage_id matches RecipeStage.id; depends_on inherited
    from the recipe; quantity = order.quantity; items_per_container = 1.
    `source="packaging"`: synthetic. stage_id is `pkg_<idx>_<machine_class>`;
    depends_on = recipe terminal stage ids; quantity from the slice;
    items_per_container honors the slice (for container-rate machines).
    """

    stage_id: str
    machine_class: ProcessGroup
    depends_on: tuple[str, ...]
    quantity: int
    items_per_container: int = 1
    config_notes: str = ""
    source: Literal["recipe", "packaging"] = "recipe"


@dataclass(frozen=True)
class _StagePlacement:
    """Internal — one machine-chunk placement for a stage.

    A stage that splits across N machines emits N _StagePlacement entries,
    each with chunk_index/chunk_total set and chunk_quantity carrying the
    chunk's share. A non-split stage emits a single placement with
    chunk_total=1 and chunk_quantity=spec.quantity.
    """

    spec: _StageSpec
    machine_id: str
    start: datetime
    end: datetime
    chunk_index: int
    chunk_total: int
    chunk_quantity: int


def plan_for_new_order(
    snapshot: Snapshot,
    order: ScheduleNewOrder,
    *,
    now: datetime,
    settings: "Settings | None" = None,
) -> Plan:
    """Build a Plan placing a new order's stages across eligible machines.

    Caller passes `now` so the function stays pure. `settings` may be
    omitted in production (falls back to get_settings()); pass explicit
    settings in tests to pin split thresholds + cap.
    """
    if settings is None:
        from engine.config import get_settings  # noqa: PLC0415 — avoid import cycle at module load
        settings = get_settings()

    recipe = _resolve_recipe(snapshot, order)
    stage_specs = _build_stage_specs(order, recipe)
    placements_by_stage = _place_stages(
        snapshot, order, stage_specs, now=now, settings=settings,
    )
    slot_writes = _build_slot_writes(order, recipe, placements_by_stage, snapshot)

    total_chunks = sum(len(c) for c in placements_by_stage.values())
    notes = (
        f"order={order.job_reference_id} recipe={recipe.recipe_key} v{recipe.version} "
        f"placed {total_chunks} slot(s) across {len(placements_by_stage)} stage(s)",
    )
    return Plan(slot_writes=tuple(slot_writes), notes=notes)


# ─────────────────────────────────────────────────────────────────────────
# Recipe resolution (invariant: composite-key pinning)
# ─────────────────────────────────────────────────────────────────────────


def _resolve_recipe(snapshot: Snapshot, order: ScheduleNewOrder) -> Recipe:
    """Look up the order's pinned recipe and gate on Active status.

    Composite-key pinning is the v3 invariant — engine never falls back to a
    different version. Status gate prevents new orders from using Draft
    (work-in-progress) or Retired (removed-from-service) recipes. In-flight
    slots that were pinned to a now-retired version still resolve correctly
    here, but new-order placement (the caller, plan_for_new_order) refuses.
    """
    from engine.models import RecipeStatus  # noqa: PLC0415 — avoid import cycle

    recipe = snapshot.recipe_by_composite_key(order.recipe_key, order.recipe_version)
    if recipe is None:
        raise DanglingRecipeError(order.recipe_key, order.recipe_version)
    if recipe.status != RecipeStatus.ACTIVE:
        raise InactiveRecipeError(order.recipe_key, order.recipe_version, recipe.status.value)
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


def _build_stage_specs(
    order: ScheduleNewOrder, recipe: Recipe,
) -> list[_StageSpec]:
    """Convert recipe stages + order packaging breakdown into a unified
    topologically-ordered list of _StageSpec.

    Recipe stages come first (in recipe topological order, full order
    quantity). If the order carries packaging_breakdown, synthetic packaging
    stages are appended, each depending on the recipe DAG's terminal
    stages (recipe stages that nothing else in the recipe depends on) — so
    every packaging slice runs strictly after the recipe is done.

    Convention: synthetic stage_ids are `pkg_<idx>_<MachineClass>` so the
    Marey chart, snapshot parser, and baton-pass logic can identify them
    by prefix without needing a parallel registry.
    """
    recipe_stages_topo = _topological_order(recipe)

    # ADR-0004 — Kitting-Only orders (include_press=False) are already-pressed
    # inventory: drop the Pressing-class recipe stages and schedule only the
    # packaging breakdown. Any surviving recipe stage that depended on a dropped
    # press stage has that dependency stripped (Phase 1 recipes are press-only,
    # so this normally leaves no recipe stages at all).
    dropped_ids: set[str] = set()
    if not order.include_press:
        dropped_ids = {s.id for s in recipe_stages_topo if s.machine_class == "Pressing"}
        recipe_stages_topo = [s for s in recipe_stages_topo if s.id not in dropped_ids]

    specs: list[_StageSpec] = [
        _StageSpec(
            stage_id=s.id,
            machine_class=s.machine_class,
            depends_on=tuple(d for d in s.depends_on if d not in dropped_ids),
            quantity=order.quantity,
            items_per_container=1,
            config_notes="",
            source="recipe",
        )
        for s in recipe_stages_topo
    ]

    if not order.packaging_breakdown:
        return specs

    # Recipe terminal stages = stages no other (surviving) recipe stage depends
    # on. Computed over the post-drop set so packaging hangs off what remains
    # (or off nothing, when include_press dropped every recipe stage).
    surviving_ids = {s.id for s in recipe_stages_topo}
    has_dependents: set[str] = set()
    for stage in recipe_stages_topo:
        for dep in stage.depends_on:
            if dep in surviving_ids:
                has_dependents.add(dep)
    terminal_stage_ids = tuple(
        s.id for s in recipe_stages_topo if s.id not in has_dependents
    )

    for idx, slice_ in enumerate(order.packaging_breakdown):
        specs.append(
            _StageSpec(
                stage_id=f"pkg_{idx}_{slice_.machine_class}",
                machine_class=slice_.machine_class,
                depends_on=terminal_stage_ids,
                quantity=slice_.quantity,
                items_per_container=max(1, slice_.items_per_container),
                config_notes=slice_.config_notes,
                source="packaging",
            )
        )

    return specs


def _place_stages(
    snapshot: Snapshot,
    order: ScheduleNewOrder,
    stage_specs: list[_StageSpec],
    *,
    now: datetime,
    settings: "Settings",
) -> dict[str, list[_StagePlacement]]:
    """For each stage spec (in topo order), choose machine(s) + times.

    Returns: stage_id → list of _StagePlacement. A single-machine stage has
    a one-element list; a split stage has up to settings.split_max_machines
    elements.

    Within a single planning pass, this function maintains
    `pending_by_machine` — synthetic Slot stand-ins for each chunk it has
    already placed. Subsequent stages whose machine class overlaps with an
    earlier stage's (e.g., two `Clamshell` slices in one breakdown, or a
    single split stage placing multiple chunks) see those pending slots
    when computing earliest-start. Without this, two slices targeting the
    same machine class would both schedule into the same physical-machine
    window because the Monday snapshot has no record of either yet.
    """
    placements: dict[str, list[_StagePlacement]] = {}
    pending_by_machine: dict[str, list[Slot]] = {}

    for spec in stage_specs:
        # Earliest this stage could start = max-end across ALL chunks of
        # each predecessor stage. If press splits across 2 machines and
        # they finish at T1 and T2, packaging waits for max(T1, T2).
        predecessor_ends = []
        for dep_id in spec.depends_on:
            if dep_id in placements:
                predecessor_ends.append(
                    max(p.end for p in placements[dep_id])
                )
        earliest_allowed = max(predecessor_ends + [now])

        candidates = eligible_machines(
            snapshot, machine_class=spec.machine_class, order=order,
        )
        if not candidates:
            raise UnroutableStageError(
                spec.stage_id, spec.machine_class,
                reason=_diagnose_no_candidates(snapshot, spec.machine_class),
            )

        should_split = (
            spec.machine_class in SPLITTABLE_PROCESS_GROUPS
            and len(candidates) >= 2
            and spec.quantity >= settings.split_min_quantity
        )

        if not should_split:
            new_placements = [
                _place_on_single_machine(
                    spec, candidates, snapshot, pending_by_machine,
                    earliest_allowed=earliest_allowed, now=now,
                )
            ]
        else:
            new_placements = _split_stage_across_machines(
                spec, candidates, snapshot, pending_by_machine,
                earliest_allowed=earliest_allowed, now=now, settings=settings,
            )

        placements[spec.stage_id] = new_placements
        # Augment the in-plan machine queues so later stages see these
        # placements (P0 fix: prevents two same-class slices from colliding).
        for p in new_placements:
            pending_by_machine.setdefault(p.machine_id, []).append(
                _placement_as_pending_slot(p, order)
            )

    return placements


def _queue_for_machine(
    snapshot: Snapshot,
    machine_id: str,
    pending_by_machine: dict[str, list[Slot]],
) -> list[Slot]:
    """Merge Monday-side slots + in-plan pending placements for a machine."""
    return list(snapshot.slots_on_machine(machine_id)) + pending_by_machine.get(
        machine_id, []
    )


def _placement_as_pending_slot(
    placement: "_StagePlacement", order: ScheduleNewOrder,
) -> Slot:
    """Wrap an in-plan placement as a synthetic Slot for use as a queue
    obstacle in subsequent same-machine placements. Status=QUEUED so
    `_queue_tail` (in placement.py) sees it. id="" so it can't be
    confused with a real Monday item.
    """
    from engine.models import Priority, SlotStatus  # noqa: PLC0415
    return Slot(
        id="",
        name=f"<pending:{placement.spec.stage_id}>",
        job_reference_id=order.job_reference_id,
        machine_id=placement.machine_id,
        stage_id=placement.spec.stage_id,
        recipe_key=None,
        recipe_version=None,
        quantity=placement.chunk_quantity,
        planned_start=placement.start,
        planned_end=placement.end,
        actual_start=None,
        actual_end=None,
        dependent_on_ids=(),
        status=SlotStatus.QUEUED,
        manually_placed=False,
        priority=Priority.NORMAL,
        last_reflow_hash=None,
        drift_last_detected_at=None,
    )


def _place_on_single_machine(
    spec: _StageSpec,
    candidates: list[Machine],
    snapshot: Snapshot,
    pending_by_machine: dict[str, list[Slot]],
    *,
    earliest_allowed: datetime,
    now: datetime,
) -> _StagePlacement:
    """Pick the machine with the earliest valid start. Ties → routing order."""
    best: _StagePlacement | None = None
    for machine in candidates:
        duration_hours = _duration_hours(spec, machine, spec.quantity)
        queue = _queue_for_machine(snapshot, machine.id, pending_by_machine)
        start, end = find_earliest_start(
            machine, duration_hours,
            earliest_allowed_start=earliest_allowed, queue=queue, now=now,
        )
        if best is None or start < best.start:
            best = _StagePlacement(
                spec=spec, machine_id=machine.id, start=start, end=end,
                chunk_index=0, chunk_total=1, chunk_quantity=spec.quantity,
            )
    assert best is not None  # candidates non-empty by precondition
    return best


def _split_stage_across_machines(
    spec: _StageSpec,
    candidates: list[Machine],
    snapshot: Snapshot,
    pending_by_machine: dict[str, list[Slot]],
    *,
    earliest_allowed: datetime,
    now: datetime,
    settings: "Settings",
) -> list[_StagePlacement]:
    """Split spec.quantity across the top-K earliest-available eligible
    machines, proportional to capacity_per_hour.

    K = min(len(candidates), settings.split_max_machines).
    """
    # Order candidates by earliest available start (estimated against full
    # quantity, including any in-plan pending slots — exact ranking doesn't
    # matter, just a tie-break for "which K machines do we choose"). The
    # actual placement re-runs find_earliest_start with the chunk-sized
    # duration.
    candidates_with_starts: list[tuple[Machine, datetime]] = []
    for machine in candidates:
        full_duration = _duration_hours(spec, machine, spec.quantity)
        queue = _queue_for_machine(snapshot, machine.id, pending_by_machine)
        start, _end = find_earliest_start(
            machine, full_duration,
            earliest_allowed_start=earliest_allowed, queue=queue, now=now,
        )
        candidates_with_starts.append((machine, start))
    candidates_with_starts.sort(key=lambda t: (t[1], t[0].name))

    if settings.split_max_machines < 1:
        # Defensive — Settings validates ge=1, but a bad direct construction
        # could still hit here. Fall back to single-machine to avoid silently
        # dropping the stage.
        chosen = [candidates_with_starts[0][0]]
    else:
        k = min(len(candidates_with_starts), settings.split_max_machines)
        chosen = [m for m, _ in candidates_with_starts[:k]]

    chunk_qtys = _proportional_chunks(
        spec.quantity,
        [m.capacity_per_hour for m in chosen],
        round_to=max(1, settings.split_chunk_round_to),
    )

    placements: list[_StagePlacement] = []
    chunk_total = sum(1 for q in chunk_qtys if q > 0)
    write_idx = 0
    # Local pending augmentation for THIS split — each chunk we place is
    # visible to subsequent chunks in the same split. (E.g., a 2-chunk
    # split that happens to land both chunks on the same machine through
    # a misconfigured candidate list shouldn't double-book it.)
    local_pending: dict[str, list[Slot]] = {
        mid: list(slots) for mid, slots in pending_by_machine.items()
    }
    for machine, qty in zip(chosen, chunk_qtys, strict=True):
        if qty <= 0:
            continue  # rounding edge case eliminated this machine
        duration_hours = _duration_hours(spec, machine, qty)
        queue = _queue_for_machine(snapshot, machine.id, local_pending)
        start, end = find_earliest_start(
            machine, duration_hours,
            earliest_allowed_start=earliest_allowed, queue=queue, now=now,
        )
        p = _StagePlacement(
            spec=spec, machine_id=machine.id, start=start, end=end,
            chunk_index=write_idx, chunk_total=chunk_total,
            chunk_quantity=qty,
        )
        placements.append(p)
        # No order in scope here — synthesize a placeholder for queue use.
        # job_reference_id doesn't matter for queue obstacle semantics.
        from engine.models import Priority, SlotStatus  # noqa: PLC0415
        local_pending.setdefault(machine.id, []).append(
            Slot(
                id="", name="<pending:split-chunk>",
                job_reference_id="", machine_id=machine.id,
                stage_id=spec.stage_id, recipe_key=None, recipe_version=None,
                quantity=qty, planned_start=start, planned_end=end,
                actual_start=None, actual_end=None, dependent_on_ids=(),
                status=SlotStatus.QUEUED, manually_placed=False,
                priority=Priority.NORMAL, last_reflow_hash=None,
                drift_last_detected_at=None,
            )
        )
        write_idx += 1
    return placements


def _proportional_chunks(
    total: int, weights: list[float], *, round_to: int,
) -> list[int]:
    """Split `total` into len(weights) chunks proportional to weights.

    Algorithm — largest-remainder method:
    1. Compute the ideal share for each weight (`weight/sum * total`).
    2. Floor each share to the nearest `round_to` multiple.
    3. Compute the leftover (total - sum of floors). It's always >= 0 and
       always a multiple of `round_to` because flooring can only reduce.
    4. Distribute the leftover one `round_to` bump at a time to the
       chunks with the largest *fractional remainder* (the part that was
       dropped by flooring), ties broken by weight then by index.

    Guarantees: sum(out) == total exactly, every chunk >= 0, no negative
    clamp logic needed. Caller guarantees weights are all > 0.

    Edge cases:
    - total < round_to: floors all = 0, leftover = total which isn't a
      multiple of round_to. We hand the entire `total` to the
      largest-weight chunk in that case — better than rounding down to
      zero and silently dropping work.
    - All weights equal: even split with leftover applied to lowest-index
      chunk (deterministic for tests).
    """
    if not weights or total <= 0:
        return []

    weight_sum = sum(weights)
    if weight_sum <= 0:
        # Defensive — `is_available` requires capacity > 0, but stay safe.
        weight_sum = float(len(weights))
        weights = [1.0] * len(weights)

    ideal = [w / weight_sum * total for w in weights]
    floored = [int(r // round_to) * round_to for r in ideal]

    leftover = total - sum(floored)
    if leftover <= 0:
        return floored

    # If leftover < round_to, we can't bump anyone — the rounding granularity
    # is coarser than what's left. Dump it on the largest-weight chunk so
    # we still preserve `sum == total`.
    if leftover < round_to:
        largest_idx = max(range(len(weights)), key=lambda i: (weights[i], -i))
        floored[largest_idx] += leftover
        return floored

    # Distribute the leftover in `round_to`-sized bumps, prioritizing the
    # chunks that lost the most to flooring (largest fractional remainder).
    fractional_loss = [ideal[i] - floored[i] for i in range(len(weights))]
    # Indices sorted by (loss desc, weight desc, index asc) — deterministic.
    order = sorted(
        range(len(weights)),
        key=lambda i: (-fractional_loss[i], -weights[i], i),
    )
    bumps_remaining = leftover // round_to
    residual = leftover - bumps_remaining * round_to
    for i in order:
        if bumps_remaining <= 0:
            break
        floored[i] += round_to
        bumps_remaining -= 1
    # Any sub-round_to residual (e.g., total=151, round_to=100 leaves 51)
    # lands on the largest-weight chunk so sum(out) == total exactly.
    if residual > 0:
        largest_idx = max(range(len(weights)), key=lambda i: (weights[i], -i))
        floored[largest_idx] += residual
    return floored


def _duration_hours(spec: _StageSpec, machine: Machine, quantity: int) -> float:
    """Quantity / effective throughput.

    For machines in CONTAINER_CAPACITY_GROUPS, capacity_per_hour is in
    *containers* per hour. Multiply by items_per_container to get the
    effective tab/capsule throughput.

    Press/Capsule and other item-rate groups: throughput = capacity_per_hour.
    """
    effective_capacity = machine.capacity_per_hour
    if machine.process_group in CONTAINER_CAPACITY_GROUPS:
        effective_capacity *= max(1, spec.items_per_container)
    return quantity / effective_capacity


# ─────────────────────────────────────────────────────────────────────────
# SlotWrite assembly
# ─────────────────────────────────────────────────────────────────────────


def _diagnose_no_candidates(snapshot: Snapshot, machine_class: str) -> str:
    """Categorize WHY no eligible machines were found, for operator visibility."""
    in_class = [m for m in snapshot.machines if m.process_group == machine_class]
    if not in_class:
        return "no_machines_in_class"
    online = [m for m in in_class if m.is_available]
    if not online:
        return "all_machines_down"
    return "no_eligible_after_rules"


def _build_slot_writes(
    order: ScheduleNewOrder,
    recipe: Recipe,
    placements_by_stage: dict[str, list[_StagePlacement]],
    snapshot: Snapshot,
) -> list[SlotWrite]:
    """Flatten per-stage chunk placements into individual SlotWrites.

    Naming convention (identity prefix owned by `engine.core.labels`):
    - With an N#: `N12345 → {stage_id}` — operators see the PO at a glance.
    - Without one (legacy / unlinked): `#<last-6-of-job-ref> → {stage_id}`,
      matching the engine's prior Marey fallback.
    - Split stage: appends ` (1/2)` — chunk index out of chunk_total.
    - Packaging slice with config notes: appends `· 3ct diamond` so the
      Marey chart and operator-facing slot list explain why two clamshell
      slots have different qtys.
    """
    writes: list[SlotWrite] = []
    for stage_id, chunks in placements_by_stage.items():
        for chunk in chunks:
            machine = snapshot.machine_by_id(chunk.machine_id)
            instance = machine.instance if machine else "gray_space"

            suffix_parts: list[str] = []
            if chunk.chunk_total > 1:
                suffix_parts.append(f"{chunk.chunk_index + 1}/{chunk.chunk_total}")
            if chunk.spec.config_notes:
                suffix_parts.append(chunk.spec.config_notes)
            suffix = f" ({' · '.join(suffix_parts)})" if suffix_parts else ""
            # The Slot has no Monday id at placement time, so the labels
            # module's `#<last-6>` fallback uses the job_reference_id (the
            # stable seed id) — same identifier the Marey view falls back to.
            # When n_number is present it wins and job_reference_id is unused.
            base_name = compose_slot_name(
                n_number=order.n_number,
                flavor=order.flavor,
                stage_id=stage_id,
                slot_id=order.job_reference_id,
            )
            name = f"{base_name}{suffix}"

            writes.append(
                SlotWrite(
                    slot_id=None,  # create
                    name=name,
                    machine_id=chunk.machine_id,
                    job_reference_id=order.job_reference_id,
                    stage_id=stage_id,
                    recipe_key=recipe.recipe_key,
                    recipe_version=recipe.version,
                    quantity=chunk.chunk_quantity,
                    planned_start=chunk.start,
                    planned_end=chunk.end,
                    status=SlotStatus.QUEUED,
                    manually_placed=False,
                    instance=instance,
                    origin_instance=order.origin_instance,
                    n_number=order.n_number,
                    flavor=order.flavor,
                )
            )

    return writes
