"""Pure-core tests for drift detection + plan_for_drift (E6)."""

from __future__ import annotations

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from engine.core.drift import find_drift_candidates, plan_for_drift
from engine.models import (
    DriftDetected,
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
NOW = datetime(2026, 5, 24, 14, 0, 0, tzinfo=TZ)

THRESHOLD = 15  # minutes
SUPPRESSION = 60  # minutes


def _machine() -> Machine:
    return Machine(
        id="M1", name="Gandalf", process_group="Pressing",
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
    status: SlotStatus = SlotStatus.QUEUED,
    planned_start: datetime | None = None,
    planned_end: datetime | None = None,
    actual_start: datetime | None = None,
    actual_end: datetime | None = None,
    drift_last_detected_at: datetime | None = None,
    manually_placed: bool = False,
) -> Slot:
    return Slot(
        id=id_, name=f"slot {id_}",
        job_reference_id="J1", machine_id="M1",
        stage_id="press", recipe_key="tablet-press-standard", recipe_version=1,
        quantity=100000,
        planned_start=planned_start, planned_end=planned_end,
        actual_start=actual_start, actual_end=actual_end,
        dependent_on_ids=(), status=status,
        manually_placed=manually_placed, priority=Priority.NORMAL,
        last_reflow_hash=None, drift_last_detected_at=drift_last_detected_at,
    )


def _snapshot(slots: tuple[Slot, ...]) -> Snapshot:
    return Snapshot(read_at=NOW, machines=(_machine(),), recipes=(_recipe(),), slots=slots)


def _candidates(slots: tuple[Slot, ...]):
    return find_drift_candidates(
        _snapshot(slots),
        now=NOW,
        threshold_minutes=THRESHOLD,
        suppression_minutes=SUPPRESSION,
    )


# ─── find_drift_candidates — late_start ──────────────────────────────────


def test_late_start_detected_when_planned_start_past_threshold():
    """Queued slot, planned_start 30 min ago, no actual_start → late_start."""
    slot = _slot(planned_start=NOW - timedelta(minutes=30))
    out = _candidates((slot,))
    assert len(out) == 1
    assert out[0][0].id == "S1"
    assert out[0][1] == "late_start"


def test_late_start_not_detected_inside_threshold():
    """Queued slot, planned_start 5 min ago → within threshold, no drift."""
    slot = _slot(planned_start=NOW - timedelta(minutes=5))
    out = _candidates((slot,))
    assert out == []


def test_late_start_not_detected_when_actual_start_present():
    """Already started — webhook landed; no drift event."""
    slot = _slot(
        status=SlotStatus.RUNNING,
        planned_start=NOW - timedelta(minutes=30),
        actual_start=NOW - timedelta(minutes=20),
    )
    out = _candidates((slot,))
    assert out == []


def test_late_start_not_detected_when_planned_start_missing():
    """No planned_start → engine never scheduled this; nothing to drift."""
    slot = _slot(planned_start=None)
    out = _candidates((slot,))
    assert out == []


# ─── find_drift_candidates — late_end ────────────────────────────────────


def test_late_end_detected_when_planned_end_past_threshold():
    """Running slot, planned_end 30 min ago, no actual_end → late_end."""
    slot = _slot(
        status=SlotStatus.RUNNING,
        planned_start=NOW - timedelta(hours=2),
        actual_start=NOW - timedelta(hours=2),
        planned_end=NOW - timedelta(minutes=30),
    )
    out = _candidates((slot,))
    assert len(out) == 1
    assert out[0][1] == "late_end"


def test_late_end_not_detected_inside_threshold():
    slot = _slot(
        status=SlotStatus.RUNNING,
        actual_start=NOW - timedelta(hours=1),
        planned_end=NOW - timedelta(minutes=5),
    )
    out = _candidates((slot,))
    assert out == []


def test_late_end_not_detected_when_actual_end_present():
    slot = _slot(
        status=SlotStatus.RUNNING,
        actual_start=NOW - timedelta(hours=2),
        planned_end=NOW - timedelta(minutes=30),
        actual_end=NOW - timedelta(minutes=10),
    )
    out = _candidates((slot,))
    assert out == []


# ─── suppression window ──────────────────────────────────────────────────


def test_suppression_blocks_recently_detected_slot():
    """Sweep already flagged this slot 30 min ago — within 60 min window, skip."""
    slot = _slot(
        planned_start=NOW - timedelta(minutes=30),
        drift_last_detected_at=NOW - timedelta(minutes=30),
    )
    out = _candidates((slot,))
    assert out == []


def test_suppression_expires_after_window():
    """drift_last_detected_at older than suppression → re-emit drift."""
    slot = _slot(
        planned_start=NOW - timedelta(minutes=120),
        drift_last_detected_at=NOW - timedelta(minutes=90),
    )
    out = _candidates((slot,))
    assert len(out) == 1


# ─── filter rules: status, manually_placed ───────────────────────────────


def test_blocked_slot_ignored():
    slot = _slot(
        status=SlotStatus.BLOCKED,
        planned_start=NOW - timedelta(minutes=30),
    )
    out = _candidates((slot,))
    assert out == []


def test_done_slot_ignored():
    slot = _slot(
        status=SlotStatus.DONE,
        planned_start=NOW - timedelta(hours=3),
        actual_start=NOW - timedelta(hours=3),
        actual_end=NOW - timedelta(hours=1),
    )
    out = _candidates((slot,))
    assert out == []


def test_manually_placed_slot_ignored():
    slot = _slot(
        planned_start=NOW - timedelta(minutes=30),
        manually_placed=True,
    )
    out = _candidates((slot,))
    assert out == []


# ─── multiple candidates, deterministic order ────────────────────────────


def test_multiple_candidates_returned_in_snapshot_order():
    s1 = _slot(id_="S1", planned_start=NOW - timedelta(minutes=30))
    s2 = _slot(id_="S2", planned_start=NOW - timedelta(minutes=45))
    s3 = _slot(
        id_="S3",
        status=SlotStatus.RUNNING,
        actual_start=NOW - timedelta(hours=2),
        planned_end=NOW - timedelta(minutes=20),
    )
    out = _candidates((s1, s2, s3))
    assert [(s.id, k) for s, k in out] == [
        ("S1", "late_start"),
        ("S2", "late_start"),
        ("S3", "late_end"),
    ]


# ─── plan_for_drift ──────────────────────────────────────────────────────


def test_plan_for_drift_stamps_drift_last_detected_at():
    slot = _slot(planned_start=NOW - timedelta(minutes=30))
    snap = _snapshot((slot,))
    event = DriftDetected(slot_id="S1", kind="late_start")
    plan = plan_for_drift(
        snap, event, now=NOW,
        threshold_minutes=THRESHOLD, suppression_minutes=SUPPRESSION,
    )
    assert len(plan.slot_writes) == 1
    w = plan.slot_writes[0]
    assert w.slot_id == "S1"
    assert w.drift_last_detected_at == NOW
    # No other field touched — drift is detect-only in Phase 1.
    assert w.status is None
    assert w.planned_start is None
    assert w.actual_start is None
    # P0 regression (2026-05-26): instance must inherit from the slot so
    # apply_plan routes the drift stamp to the correct Schedule board.
    assert w.instance == slot.instance


def test_plan_for_drift_nexiuum_slot_inherits_instance():
    """REGRESSION (P0, 2026-05-26): drift stamp on a Nexiuum slot must
    write to the Nexiuum Schedule board, not Gray Space."""
    from engine.models import Priority, Slot, SlotStatus

    nx_slot = Slot(
        id="NX-S1", name="nx-drift-test",
        job_reference_id="J1", machine_id="NX-M1",
        stage_id="blister", recipe_key="recipe", recipe_version=1,
        quantity=1000,
        planned_start=NOW - timedelta(minutes=30),
        planned_end=NOW,
        actual_start=None, actual_end=None,
        dependent_on_ids=(), status=SlotStatus.QUEUED,
        manually_placed=False, priority=Priority.NORMAL,
        last_reflow_hash=None, drift_last_detected_at=None,
        instance="nexiuum",
    )
    snap = _snapshot((nx_slot,))
    event = DriftDetected(slot_id="NX-S1", kind="late_start")
    plan = plan_for_drift(
        snap, event, now=NOW,
        threshold_minutes=THRESHOLD, suppression_minutes=SUPPRESSION,
    )
    assert len(plan.slot_writes) == 1
    assert plan.slot_writes[0].instance == "nexiuum"


def test_plan_for_drift_noop_when_slot_missing():
    snap = _snapshot(())
    event = DriftDetected(slot_id="missing", kind="late_start")
    plan = plan_for_drift(
        snap, event, now=NOW,
        threshold_minutes=THRESHOLD, suppression_minutes=SUPPRESSION,
    )
    assert plan.slot_writes == ()
    assert any("no longer in snapshot" in n for n in plan.notes)


def test_plan_for_drift_noop_when_slot_now_done():
    """Race: slot moved to Done between sweep and worker dispatch."""
    slot = _slot(
        status=SlotStatus.DONE,
        planned_start=NOW - timedelta(hours=3),
        actual_start=NOW - timedelta(hours=3),
        actual_end=NOW - timedelta(minutes=10),
    )
    snap = _snapshot((slot,))
    event = DriftDetected(slot_id="S1", kind="late_start")
    plan = plan_for_drift(
        snap, event, now=NOW,
        threshold_minutes=THRESHOLD, suppression_minutes=SUPPRESSION,
    )
    assert plan.slot_writes == ()
    assert any("status=Done" in n for n in plan.notes)


# ─── plan_for_drift — re-validation against fresh snapshot ───────────────


def test_plan_for_drift_noop_when_late_start_event_but_slot_now_running():
    """Race: late_start enqueued → webhook landed actual_start → worker dispatch.

    Without re-validation, the worker would stamp drift_last_detected_at
    even though the slot is no longer late, which would then suppress
    later legitimate late_end detection.
    """
    slot = _slot(
        status=SlotStatus.RUNNING,
        planned_start=NOW - timedelta(minutes=30),
        actual_start=NOW - timedelta(minutes=5),
    )
    snap = _snapshot((slot,))
    event = DriftDetected(slot_id="S1", kind="late_start")
    plan = plan_for_drift(
        snap, event, now=NOW,
        threshold_minutes=THRESHOLD, suppression_minutes=SUPPRESSION,
    )
    assert plan.slot_writes == ()
    assert any("no longer late_start" in n for n in plan.notes)


def test_plan_for_drift_noop_when_slot_now_manually_placed():
    """Race: operator pinned the slot between sweep and worker."""
    slot = _slot(
        planned_start=NOW - timedelta(minutes=30),
        manually_placed=True,
    )
    snap = _snapshot((slot,))
    event = DriftDetected(slot_id="S1", kind="late_start")
    plan = plan_for_drift(
        snap, event, now=NOW,
        threshold_minutes=THRESHOLD, suppression_minutes=SUPPRESSION,
    )
    assert plan.slot_writes == ()
    assert any("manually_placed" in n for n in plan.notes)


def test_plan_for_drift_noop_when_planned_start_moved_inside_threshold():
    """Race: operator dragged the slot to start in 10 min between sweep and worker."""
    slot = _slot(planned_start=NOW + timedelta(minutes=10))
    snap = _snapshot((slot,))
    event = DriftDetected(slot_id="S1", kind="late_start")
    plan = plan_for_drift(
        snap, event, now=NOW,
        threshold_minutes=THRESHOLD, suppression_minutes=SUPPRESSION,
    )
    assert plan.slot_writes == ()
    assert any("no longer late_start" in n for n in plan.notes)


def test_plan_for_drift_noop_when_already_inside_suppression_window():
    """Duplicate DriftDetected queued: first stamped, second sees fresh suppression.

    Defense against two sweep passes (or sweep + repeat) enqueuing the same
    slot before the first stamp is applied. Once one Plan lands, the
    second sees `drift_last_detected_at` inside the window and no-ops —
    avoiding redundant Monday writes + window extension.
    """
    slot = _slot(
        planned_start=NOW - timedelta(minutes=30),
        drift_last_detected_at=NOW - timedelta(seconds=5),
    )
    snap = _snapshot((slot,))
    event = DriftDetected(slot_id="S1", kind="late_start")
    plan = plan_for_drift(
        snap, event, now=NOW,
        threshold_minutes=THRESHOLD, suppression_minutes=SUPPRESSION,
    )
    assert plan.slot_writes == ()
    assert any("suppression window" in n for n in plan.notes)


def test_plan_for_drift_late_end_after_stale_late_start_still_works():
    """The cross-kind suppression bug Codex flagged.

    Scenario: a slot was Queued and late, sweep enqueued late_start. Before
    the worker processed it, the slot started for real (Running, actual_start
    set). The stale late_start should NOT stamp drift_last_detected_at,
    so a later legitimate late_end is free to be detected and stamped.
    """
    # The slot is now Running with planned_end in the past and no actual_end.
    slot = _slot(
        status=SlotStatus.RUNNING,
        planned_start=NOW - timedelta(hours=1),
        actual_start=NOW - timedelta(minutes=50),
        planned_end=NOW - timedelta(minutes=20),
    )
    snap = _snapshot((slot,))

    # First, the stale late_start event arrives. It should no-op.
    stale_event = DriftDetected(slot_id="S1", kind="late_start")
    stale_plan = plan_for_drift(
        snap, stale_event, now=NOW,
        threshold_minutes=THRESHOLD, suppression_minutes=SUPPRESSION,
    )
    assert stale_plan.slot_writes == ()  # The fix prevents the stamp

    # Then, a legitimate late_end event arrives — must succeed because the
    # stale late_start did NOT poison drift_last_detected_at.
    real_event = DriftDetected(slot_id="S1", kind="late_end")
    real_plan = plan_for_drift(
        snap, real_event, now=NOW,
        threshold_minutes=THRESHOLD, suppression_minutes=SUPPRESSION,
    )
    assert len(real_plan.slot_writes) == 1
    assert real_plan.slot_writes[0].drift_last_detected_at == NOW


def test_plan_for_drift_write_preserves_n_number():
    """The drift stamp originates from an existing Slot — it copies that
    Slot's N# so the value survives the write/re-read cycle."""
    from dataclasses import replace

    slot = replace(_slot(planned_start=NOW - timedelta(minutes=30)), n_number="N3629")
    snap = _snapshot((slot,))
    event = DriftDetected(slot_id="S1", kind="late_start")
    plan = plan_for_drift(
        snap, event, now=NOW,
        threshold_minutes=THRESHOLD, suppression_minutes=SUPPRESSION,
    )
    assert len(plan.slot_writes) == 1
    assert plan.slot_writes[0].n_number == "N3629"


def test_plan_for_drift_write_preserves_flavor():
    """The drift stamp originates from an existing Slot — it copies that
    Slot's flavor so the value survives the write/re-read cycle (like N#)."""
    from dataclasses import replace

    slot = replace(_slot(planned_start=NOW - timedelta(minutes=30)), flavor="Strawberry Banana")
    snap = _snapshot((slot,))
    event = DriftDetected(slot_id="S1", kind="late_start")
    plan = plan_for_drift(
        snap, event, now=NOW,
        threshold_minutes=THRESHOLD, suppression_minutes=SUPPRESSION,
    )
    assert len(plan.slot_writes) == 1
    assert plan.slot_writes[0].flavor == "Strawberry Banana"
