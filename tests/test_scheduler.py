"""Unit tests for the scheduler entry point — recipe lookup, topo order, placement."""

from __future__ import annotations

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from engine.core.scheduler import (
    DanglingRecipeError,
    InactiveRecipeError,
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


def test_draft_recipe_rejected_for_new_orders():
    """A Draft recipe must not be used for new-order placement."""
    draft = Recipe(
        id="R1", name="tablet-press-standard v1",
        recipe_key="tablet-press-standard", version=1,
        status=RecipeStatus.DRAFT,
        stages=(RecipeStage(id="press", machine_class="Pressing", depends_on=()),),
    )
    snap = _snap([_machine("Gandalf")], [draft])
    with pytest.raises(InactiveRecipeError) as exc_info:
        plan_for_new_order(snap, _order(), now=NOW)
    assert exc_info.value.status == "Draft"


def test_retired_recipe_rejected_for_new_orders():
    """A Retired recipe must not be used for new-order placement."""
    retired = Recipe(
        id="R1", name="tablet-press-standard v1",
        recipe_key="tablet-press-standard", version=1,
        status=RecipeStatus.RETIRED,
        stages=(RecipeStage(id="press", machine_class="Pressing", depends_on=()),),
    )
    snap = _snap([_machine("Gandalf")], [retired])
    with pytest.raises(InactiveRecipeError) as exc_info:
        plan_for_new_order(snap, _order(), now=NOW)
    assert exc_info.value.status == "Retired"


# ─── Topological sort edge cases ─────────────────────────────────────────


def test_topo_sort_self_loop_raises():
    """A stage that depends on itself must be detected as a cycle."""
    self_loop = Recipe(
        id="R1", name="bad", recipe_key="bad", version=1,
        status=RecipeStatus.ACTIVE,
        stages=(RecipeStage(id="a", machine_class="Pressing", depends_on=("a",)),),
    )
    snap = _snap([_machine("Gandalf")], [self_loop])
    order = ScheduleNewOrder(
        job_reference_id="N1", recipe_key="bad", recipe_version=1, quantity=1000,
    )
    with pytest.raises(ValueError, match="cycle"):
        plan_for_new_order(snap, order, now=NOW)


def test_topo_sort_two_node_cycle_raises():
    """A→B and B→A — both stages permanently at indegree 1."""
    cycle = Recipe(
        id="R1", name="bad", recipe_key="bad", version=1,
        status=RecipeStatus.ACTIVE,
        stages=(
            RecipeStage(id="a", machine_class="Pressing", depends_on=("b",)),
            RecipeStage(id="b", machine_class="Pressing", depends_on=("a",)),
        ),
    )
    snap = _snap([_machine("Gandalf")], [cycle])
    order = ScheduleNewOrder(
        job_reference_id="N1", recipe_key="bad", recipe_version=1, quantity=1000,
    )
    with pytest.raises(ValueError, match="cycle"):
        plan_for_new_order(snap, order, now=NOW)


# ─── UnroutableStageError reason discrimination ──────────────────────────


def test_unroutable_no_machines_in_class():
    """No machines of the required class — reason = no_machines_in_class."""
    snap = _snap(
        [_machine("Elphaba", process_group="Capsule")],
        [_recipe_press_only()],
    )
    with pytest.raises(UnroutableStageError) as exc_info:
        plan_for_new_order(snap, _order(), now=NOW)
    assert exc_info.value.reason == "no_machines_in_class"


def test_unroutable_all_machines_down():
    """Machines exist but all Down — reason = all_machines_down."""
    from engine.models import MachineStatus
    gandalf = _machine("Gandalf")
    # Mutate to Down by reconstructing (frozen dataclass)
    down = Machine(
        id=gandalf.id, name=gandalf.name, process_group=gandalf.process_group,
        status=MachineStatus.DOWN, capacity_per_hour=gandalf.capacity_per_hour,
        hours_per_day=gandalf.hours_per_day,
        working_window_start=gandalf.working_window_start,
        working_window_end=gandalf.working_window_end,
        changeover_minutes=gandalf.changeover_minutes,
        dual_sided_only=gandalf.dual_sided_only,
        max_job_size=gandalf.max_job_size,
        force_route_condition=gandalf.force_route_condition,
        last_job_ended_at=gandalf.last_job_ended_at,
    )
    snap = _snap([down], [_recipe_press_only()])
    with pytest.raises(UnroutableStageError) as exc_info:
        plan_for_new_order(snap, _order(), now=NOW)
    assert exc_info.value.reason == "all_machines_down"


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


# ─── Flavor propagation + naming (#5) ────────────────────────────────────
# Flavor rides from the Order onto every emitted SlotWrite and into the
# composed slot name, mirroring N#. The labels module owns the format.


def test_new_order_stamps_flavor_on_every_slot_write():
    """Every SlotWrite the scheduler emits carries the Order's flavor."""
    snap = _snap([_machine("Gandalf")], [_recipe_press_only()])
    order = _order(n_number="N12345", flavor="Strawberry Banana")
    plan = plan_for_new_order(snap, order, now=NOW)
    assert plan.slot_writes
    assert all(w.flavor == "Strawberry Banana" for w in plan.slot_writes)


def test_new_order_slot_name_includes_n_number_and_flavor():
    """Phase 2D slot name renders 'N12345 · Strawberry Banana → press'."""
    snap = _snap([_machine("Gandalf")], [_recipe_press_only()])
    order = _order(n_number="N12345", flavor="Strawberry Banana")
    plan = plan_for_new_order(snap, order, now=NOW)
    press = next(w for w in plan.slot_writes if w.stage_id == "press")
    assert press.name == "N12345 · Strawberry Banana → press"


def test_new_order_flavor_defaults_to_none_legacy():
    """No flavor supplied (legacy GS) → SlotWrites carry flavor=None and the
    name falls back to the N#-only / #<last-6> identity (unchanged from #4)."""
    snap = _snap([_machine("Gandalf")], [_recipe_press_only()])
    order = _order(n_number="N12345")  # no flavor
    plan = plan_for_new_order(snap, order, now=NOW)
    press = next(w for w in plan.slot_writes if w.stage_id == "press")
    assert press.flavor is None
    assert press.name == "N12345 → press"
