"""Pure-core tests for plan_for_actual_start / plan_for_actual_end."""

from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

from engine.core.actuals import plan_for_actual_end, plan_for_actual_start
from engine.models import (
    ActualEndReported,
    ActualStartReported,
    Machine,
    MachineStatus,
    Priority,
    Recipe,
    RecipeStage,
    RecipeStatus,
    Slot,
    SlotStatus,
    Snapshot,
)

TZ = ZoneInfo("America/Denver")
NOW = datetime(2026, 5, 22, 10, 0, 0, tzinfo=TZ)
ACTUAL_AT = datetime(2026, 5, 22, 10, 5, 0, tzinfo=TZ)


def _machine(id_: str = "M1") -> Machine:
    return Machine(
        id=id_, name="Gandalf", process_group="Pressing",
        status=MachineStatus.ONLINE,
        capacity_per_hour=40000, hours_per_day=16,
        working_window_start=6, working_window_end=22,
        changeover_minutes=30, dual_sided_only=False,
        max_job_size=None, force_route_condition=None,
        last_job_ended_at=None,
    )


def _recipe() -> Recipe:
    return Recipe(
        id="R1", name="r v1",
        recipe_key="tablet-press-standard", version=1,
        status=RecipeStatus.ACTIVE,
        stages=(RecipeStage(id="press", machine_class="Pressing", depends_on=()),),
    )


def _slot(
    *,
    id_: str = "S1",
    job_reference_id: str = "J1",
    stage_id: str = "press",
    status: SlotStatus = SlotStatus.QUEUED,
    actual_start: datetime | None = None,
    actual_end: datetime | None = None,
) -> Slot:
    return Slot(
        id=id_, name=f"slot {id_}",
        job_reference_id=job_reference_id, machine_id="M1",
        stage_id=stage_id, recipe_key="tablet-press-standard", recipe_version=1,
        quantity=100000,
        planned_start=NOW, planned_end=NOW,
        actual_start=actual_start, actual_end=actual_end,
        dependent_on_ids=(), status=status,
        manually_placed=False, priority=Priority.NORMAL,
        last_reflow_hash=None, drift_last_detected_at=None,
    )


def _snapshot(slots: tuple[Slot, ...]) -> Snapshot:
    return Snapshot(read_at=NOW, machines=(_machine(),), recipes=(_recipe(),), slots=slots)


# ─── plan_for_actual_start ───────────────────────────────────────────────


def test_actual_start_writes_when_slot_queued_and_unstarted():
    snap = _snapshot((_slot(),))
    event = ActualStartReported(job_reference_id="J1", stage_id="press", actual_at=ACTUAL_AT)
    plan = plan_for_actual_start(snap, event)
    assert len(plan.slot_writes) == 1
    w = plan.slot_writes[0]
    assert w.slot_id == "S1"
    assert w.actual_start == ACTUAL_AT
    assert w.status == SlotStatus.RUNNING


def test_actual_start_noop_when_no_slot_for_job():
    snap = _snapshot((_slot(job_reference_id="J-other"),))
    event = ActualStartReported(job_reference_id="J1", stage_id="press", actual_at=ACTUAL_AT)
    plan = plan_for_actual_start(snap, event)
    assert plan.slot_writes == ()
    assert any("no Schedule slot" in n for n in plan.notes)


def test_actual_start_noop_when_wrong_stage():
    snap = _snapshot((_slot(stage_id="package"),))
    event = ActualStartReported(job_reference_id="J1", stage_id="press", actual_at=ACTUAL_AT)
    plan = plan_for_actual_start(snap, event)
    assert plan.slot_writes == ()


def test_actual_start_idempotent_when_already_started():
    snap = _snapshot((_slot(actual_start=NOW, status=SlotStatus.RUNNING),))
    event = ActualStartReported(job_reference_id="J1", stage_id="press", actual_at=ACTUAL_AT)
    plan = plan_for_actual_start(snap, event)
    assert plan.slot_writes == ()
    assert any("already has actual_start" in n for n in plan.notes)


def test_actual_start_skips_done_slot():
    snap = _snapshot((_slot(status=SlotStatus.DONE, actual_start=NOW, actual_end=NOW),))
    event = ActualStartReported(job_reference_id="J1", stage_id="press", actual_at=ACTUAL_AT)
    plan = plan_for_actual_start(snap, event)
    assert plan.slot_writes == ()


def test_actual_start_writes_all_matching_queued_slots():
    """Multi-machine parallel pressing: every Queued press slot for the job updates."""
    snap = _snapshot((
        _slot(id_="S1", job_reference_id="J1"),
        _slot(id_="S2", job_reference_id="J1"),
        _slot(id_="S3", job_reference_id="J1", status=SlotStatus.RUNNING, actual_start=NOW),
        _slot(id_="S4", job_reference_id="J-other"),
    ))
    event = ActualStartReported(job_reference_id="J1", stage_id="press", actual_at=ACTUAL_AT)
    plan = plan_for_actual_start(snap, event)
    written_ids = {w.slot_id for w in plan.slot_writes}
    assert written_ids == {"S1", "S2"}  # S3 already Running, S4 different job


# ─── plan_for_actual_end ─────────────────────────────────────────────────


def test_actual_end_writes_when_slot_running_and_unended():
    snap = _snapshot((_slot(status=SlotStatus.RUNNING, actual_start=NOW),))
    event = ActualEndReported(job_reference_id="J1", stage_id="press", actual_at=ACTUAL_AT)
    plan = plan_for_actual_end(snap, event)
    assert len(plan.slot_writes) == 1
    w = plan.slot_writes[0]
    assert w.actual_end == ACTUAL_AT
    assert w.status == SlotStatus.DONE


def test_actual_end_idempotent_when_already_done():
    snap = _snapshot((_slot(status=SlotStatus.DONE, actual_start=NOW, actual_end=NOW),))
    event = ActualEndReported(job_reference_id="J1", stage_id="press", actual_at=ACTUAL_AT)
    plan = plan_for_actual_end(snap, event)
    assert plan.slot_writes == ()
