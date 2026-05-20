"""Unit tests for the scheduler entry point — recipe lookup, topo order, placement."""

from __future__ import annotations

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from engine.core.scheduler import (
    DanglingRecipeError,
    UnroutableStageError,
    plan_for_new_order,
)
from engine.models import (
    Machine,
    MachineStatus,
    Recipe,
    RecipeStage,
    RecipeStatus,
    ScheduleNewOrder,
    SlotStatus,
    Snapshot,
)

TZ = ZoneInfo("America/Denver")
NOW = datetime(2026, 5, 21, 8, 0, 0, tzinfo=TZ)


def _machine(
    name: str,
    process_group: str = "Pressing",
    capacity: float = 40000,
    dual_sided_only: bool = False,
    max_job_size: int | None = None,
    force_route_condition: str | None = None,
) -> Machine:
    return Machine(
        id=name,
        name=name,
        process_group=process_group,  # type: ignore[arg-type]
        status=MachineStatus.ONLINE,
        capacity_per_hour=capacity,
        hours_per_day=16,
        working_window_start=6,
        working_window_end=22,
        changeover_minutes=30,
        dual_sided_only=dual_sided_only,
        max_job_size=max_job_size,
        force_route_condition=force_route_condition,
        last_job_ended_at=None,
    )


def _recipe_press_only() -> Recipe:
    return Recipe(
        id="R1",
        name="tablet-press-standard v1",
        recipe_key="tablet-press-standard",
        version=1,
        status=RecipeStatus.ACTIVE,
        stages=(
            RecipeStage(id="press", machine_class="Pressing", depends_on=()),
        ),
    )


def _recipe_clamshell() -> Recipe:
    """4-stage DAG: press → (blister ∥ lotcode) → clamshell."""
    return Recipe(
        id="R3",
        name="clamshell-tablet v1",
        recipe_key="clamshell-tablet",
        version=1,
        status=RecipeStatus.ACTIVE,
        stages=(
            RecipeStage(id="press",     machine_class="Pressing",  depends_on=()),
            RecipeStage(id="blister",   machine_class="Blister",   depends_on=("press",)),
            RecipeStage(id="lotcode",   machine_class="Lot Coder", depends_on=("press",)),
            RecipeStage(id="clamshell", machine_class="Clamshell", depends_on=("blister", "lotcode")),
        ),
    )


def _snap(machines: list[Machine], recipes: list[Recipe], slots: tuple = ()) -> Snapshot:
    return Snapshot(read_at=NOW, machines=tuple(machines), recipes=tuple(recipes), slots=slots)


def _order(quantity: int = 100000, **kwargs) -> ScheduleNewOrder:
    return ScheduleNewOrder(
        job_reference_id="N0001",
        recipe_key="tablet-press-standard",
        recipe_version=1,
        quantity=quantity,
        **kwargs,
    )


# ─── Recipe resolution ───────────────────────────────────────────────────


def test_dangling_recipe_raises():
    snap = _snap([_machine("Gandalf")], [])  # no recipes
    with pytest.raises(DanglingRecipeError):
        plan_for_new_order(snap, _order(), now=NOW)


def test_wrong_version_raises():
    """Even if the same recipe_key exists at a different version, refuse."""
    recipe_v2 = Recipe(
        id="R1",
        name="tablet-press-standard v2",
        recipe_key="tablet-press-standard",
        version=2,
        status=RecipeStatus.ACTIVE,
        stages=(RecipeStage(id="press", machine_class="Pressing", depends_on=()),),
    )
    snap = _snap([_machine("Gandalf")], [recipe_v2])
    with pytest.raises(DanglingRecipeError):
        plan_for_new_order(snap, _order(), now=NOW)  # asks for v1


# ─── Single-stage placement (Phase 1) ────────────────────────────────────


def test_single_stage_press_basic():
    snap = _snap([_machine("Gandalf", capacity=40000)], [_recipe_press_only()])
    plan = plan_for_new_order(snap, _order(quantity=200000), now=NOW)
    assert len(plan.slot_writes) == 1
    w = plan.slot_writes[0]
    assert w.machine_id == "Gandalf"
    assert w.stage_id == "press"
    assert w.recipe_key == "tablet-press-standard"
    assert w.recipe_version == 1
    assert w.status == SlotStatus.QUEUED
    assert w.planned_start == NOW
    # 200,000 tabs / 40,000 tabs/hr = 5hr
    assert w.planned_end == NOW + timedelta(hours=5)


def test_single_stage_picks_fastest_finishing_machine():
    """Between two empty machines, picks the one that finishes first."""
    gandalf = _machine("Gandalf", capacity=40000)  # 200k → 5hr
    houdini = _machine("Houdini", capacity=30000)  # 200k → 6.67hr
    snap = _snap([gandalf, houdini], [_recipe_press_only()])
    plan = plan_for_new_order(snap, _order(quantity=200000), now=NOW)
    assert plan.slot_writes[0].machine_id == "Gandalf"


def test_dual_sided_forces_penn_and_teller():
    order = _order(dual_sided=True)
    gandalf = _machine("Gandalf", dual_sided_only=False)
    pnt = _machine("Penn & Teller", dual_sided_only=True, capacity=5000)
    snap = _snap([gandalf, pnt], [_recipe_press_only()])
    plan = plan_for_new_order(snap, order, now=NOW)
    assert plan.slot_writes[0].machine_id == "Penn & Teller"


def test_high_active_forces_lancelot():
    order = _order(active_mg=100)
    gandalf = _machine("Gandalf")
    lance = _machine("Lancelot", force_route_condition="active_mg > 80")
    snap = _snap([gandalf, lance], [_recipe_press_only()])
    plan = plan_for_new_order(snap, order, now=NOW)
    assert plan.slot_writes[0].machine_id == "Lancelot"


def test_unroutable_stage_raises():
    """No machines in the required class → UnroutableStageError."""
    snap = _snap(
        [_machine("Elphaba", process_group="Capsule")],  # only capsule
        [_recipe_press_only()],  # needs Pressing
    )
    with pytest.raises(UnroutableStageError):
        plan_for_new_order(snap, _order(), now=NOW)


# ─── Multi-stage DAG placement (Phase 2 preview) ─────────────────────────


def test_clamshell_recipe_produces_four_slots():
    machines = [
        _machine("Gandalf", process_group="Pressing", capacity=40000),
        _machine("Blister1", process_group="Blister", capacity=4000),
        _machine("LotCoder1", process_group="Lot Coder", capacity=7000),
        _machine("Clamshell1", process_group="Clamshell", capacity=3200),
    ]
    recipe = _recipe_clamshell()
    order = ScheduleNewOrder(
        job_reference_id="N0001",
        recipe_key="clamshell-tablet",
        recipe_version=1,
        quantity=100000,
    )
    snap = _snap(machines, [recipe])
    plan = plan_for_new_order(snap, order, now=NOW)
    assert len(plan.slot_writes) == 4
    stage_ids = [w.stage_id for w in plan.slot_writes]
    # Topo order: press first (no deps), then blister + lotcode (deps on press),
    # then clamshell (deps on both).
    assert stage_ids[0] == "press"
    assert "clamshell" == stage_ids[-1]
    assert set(stage_ids[1:3]) == {"blister", "lotcode"}


def test_dag_respects_predecessor_end_times():
    """Clamshell stage cannot start before max(blister.end, lotcode.end)."""
    machines = [
        _machine("Gandalf", process_group="Pressing", capacity=40000),  # 100k/4hr = 25hr — slow
        _machine("Blister1", process_group="Blister", capacity=10000),  # 100k/4hr
        _machine("LotCoder1", process_group="Lot Coder", capacity=20000),  # 100k/4hr — fast
        _machine("Clamshell1", process_group="Clamshell", capacity=4000),
    ]
    recipe = _recipe_clamshell()
    order = ScheduleNewOrder(
        job_reference_id="N0001",
        recipe_key="clamshell-tablet",
        recipe_version=1,
        quantity=40000,
    )
    snap = _snap(machines, [recipe])
    plan = plan_for_new_order(snap, order, now=NOW)
    by_stage = {w.stage_id: w for w in plan.slot_writes}
    press_end = by_stage["press"].planned_end
    blister_end = by_stage["blister"].planned_end
    lotcode_end = by_stage["lotcode"].planned_end
    clamshell_start = by_stage["clamshell"].planned_start

    # blister + lotcode both start at or after press_end
    assert by_stage["blister"].planned_start >= press_end
    assert by_stage["lotcode"].planned_start >= press_end
    # clamshell starts at or after the LATER of blister/lotcode (the merge)
    assert clamshell_start >= max(blister_end, lotcode_end)
