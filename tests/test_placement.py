"""Unit tests for placement — forward-schedule respecting windows + queue."""

from __future__ import annotations

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from engine.core.placement import find_earliest_start
from engine.models import Machine, MachineStatus, Priority, Slot, SlotStatus

TZ = ZoneInfo("America/Denver")


def _machine(
    *,
    window_start: int = 6,
    window_end: int = 22,
    changeover: int = 30,
) -> Machine:
    return Machine(
        id="M1",
        name="M1",
        process_group="Pressing",
        status=MachineStatus.ONLINE,
        capacity_per_hour=40000,
        hours_per_day=16,
        working_window_start=window_start,
        working_window_end=window_end,
        changeover_minutes=changeover,
        dual_sided_only=False,
        max_job_size=None,
        force_route_condition=None,
        last_job_ended_at=None,
    )


def _slot(
    *,
    machine_id: str = "M1",
    start: datetime | None = None,
    end: datetime | None = None,
    status: SlotStatus = SlotStatus.QUEUED,
    manually_placed: bool = False,
) -> Slot:
    return Slot(
        id="S",
        name="S",
        job_reference_id="J",
        machine_id=machine_id,
        stage_id="press",
        recipe_key="tablet-press-standard",
        recipe_version=1,
        quantity=100000,
        planned_start=start,
        planned_end=end,
        actual_start=None,
        actual_end=None,
        dependent_on_ids=(),
        status=status,
        manually_placed=manually_placed,
        priority=Priority.NORMAL,
        last_reflow_hash=None,
        drift_last_detected_at=None,
    )


def test_empty_queue_starts_at_floor():
    """No existing slots; start at the earliest_allowed_start (clamped to window)."""
    m = _machine()
    now = datetime(2026, 5, 21, 8, 0, 0, tzinfo=TZ)
    start, end = find_earliest_start(
        m, duration_hours=2, earliest_allowed_start=now, queue=[], now=now,
    )
    assert start == datetime(2026, 5, 21, 8, 0, 0, tzinfo=TZ)
    assert end == datetime(2026, 5, 21, 10, 0, 0, tzinfo=TZ)


def test_start_before_window_advances_to_window_start():
    """If 'now' is 4am but window opens at 6am, start at 6am."""
    m = _machine()
    early = datetime(2026, 5, 21, 4, 0, 0, tzinfo=TZ)
    start, _ = find_earliest_start(
        m, duration_hours=2, earliest_allowed_start=early, queue=[], now=early,
    )
    assert start == datetime(2026, 5, 21, 6, 0, 0, tzinfo=TZ)


def test_queue_tail_pushes_start_with_changeover():
    """Existing slot ends at 10am; next start is 10:30am (30-min changeover)."""
    m = _machine(changeover=30)
    existing = _slot(
        start=datetime(2026, 5, 21, 8, 0, 0, tzinfo=TZ),
        end=datetime(2026, 5, 21, 10, 0, 0, tzinfo=TZ),
    )
    now = datetime(2026, 5, 21, 8, 30, 0, tzinfo=TZ)
    start, end = find_earliest_start(
        m, duration_hours=1, earliest_allowed_start=now,
        queue=[existing], now=now,
    )
    assert start == datetime(2026, 5, 21, 10, 30, 0, tzinfo=TZ)
    assert end == datetime(2026, 5, 21, 11, 30, 0, tzinfo=TZ)


def test_dependency_floor_overrides_queue_tail():
    """If earliest_allowed_start (from a dependency) is later than queue tail+changeover, use it."""
    m = _machine(changeover=30)
    existing = _slot(
        start=datetime(2026, 5, 21, 8, 0, 0, tzinfo=TZ),
        end=datetime(2026, 5, 21, 9, 0, 0, tzinfo=TZ),
    )
    # Queue tail + changeover = 9:30am. Dependency says no earlier than noon.
    dep = datetime(2026, 5, 21, 12, 0, 0, tzinfo=TZ)
    now = datetime(2026, 5, 21, 8, 0, 0, tzinfo=TZ)
    start, _ = find_earliest_start(
        m, duration_hours=1, earliest_allowed_start=dep,
        queue=[existing], now=now,
    )
    assert start == datetime(2026, 5, 21, 12, 0, 0, tzinfo=TZ)


def test_job_that_overruns_window_starts_next_day():
    """6-hour job starting at 5pm (window ends 10pm) → would end 11pm → bump to next day 6am."""
    m = _machine(window_start=6, window_end=22)  # 6am - 10pm
    late = datetime(2026, 5, 21, 17, 0, 0, tzinfo=TZ)
    start, end = find_earliest_start(
        m, duration_hours=6, earliest_allowed_start=late, queue=[], now=late,
    )
    assert start == datetime(2026, 5, 22, 6, 0, 0, tzinfo=TZ)
    assert end == datetime(2026, 5, 22, 12, 0, 0, tzinfo=TZ)


def test_24h_window_never_advances_for_window():
    """If window is 0-24, jobs can start at any time."""
    m = _machine(window_start=0, window_end=24)
    overnight = datetime(2026, 5, 21, 23, 0, 0, tzinfo=TZ)
    start, end = find_earliest_start(
        m, duration_hours=2, earliest_allowed_start=overnight,
        queue=[], now=overnight,
    )
    assert start == datetime(2026, 5, 21, 23, 0, 0, tzinfo=TZ)
    assert end == datetime(2026, 5, 22, 1, 0, 0, tzinfo=TZ)


def test_done_slots_dont_block():
    """A slot marked Done shouldn't push the new job's start time."""
    m = _machine(changeover=30)
    done = _slot(
        start=datetime(2026, 5, 21, 8, 0, 0, tzinfo=TZ),
        end=datetime(2026, 5, 21, 20, 0, 0, tzinfo=TZ),  # would push way back if respected
        status=SlotStatus.DONE,
    )
    now = datetime(2026, 5, 21, 9, 0, 0, tzinfo=TZ)
    start, _ = find_earliest_start(
        m, duration_hours=1, earliest_allowed_start=now,
        queue=[done], now=now,
    )
    # Done slot ignored — start should be at 'now'
    assert start == datetime(2026, 5, 21, 9, 0, 0, tzinfo=TZ)


def test_multiple_queued_slots_use_latest_end():
    """Queue tail is the latest end among active slots."""
    m = _machine(changeover=30)
    earlier = _slot(
        start=datetime(2026, 5, 21, 8, 0, 0, tzinfo=TZ),
        end=datetime(2026, 5, 21, 9, 0, 0, tzinfo=TZ),
    )
    later = _slot(
        start=datetime(2026, 5, 21, 9, 30, 0, tzinfo=TZ),
        end=datetime(2026, 5, 21, 11, 0, 0, tzinfo=TZ),
    )
    now = datetime(2026, 5, 21, 8, 0, 0, tzinfo=TZ)
    start, _ = find_earliest_start(
        m, duration_hours=1, earliest_allowed_start=now,
        queue=[earlier, later], now=now,
    )
    # Latest end is 11:00 + 30 min changeover = 11:30
    assert start == datetime(2026, 5, 21, 11, 30, 0, tzinfo=TZ)


def test_long_job_overruns_window_and_doesnt_loop_forever():
    """A 20-hour job on a 16-hour-window machine should place at next window
    start and run past window_end (not infinite-loop trying to fit)."""
    m = _machine(window_start=6, window_end=22)  # 16-hour window
    now = datetime(2026, 5, 21, 8, 0, 0, tzinfo=TZ)
    start, end = find_earliest_start(
        m, duration_hours=20, earliest_allowed_start=now, queue=[], now=now,
    )
    # 20h > 16h window — placed at 8am, runs to 4am next day, ignores window_end
    assert start == datetime(2026, 5, 21, 8, 0, 0, tzinfo=TZ)
    assert end == datetime(2026, 5, 22, 4, 0, 0, tzinfo=TZ)


# ─── DST boundary regression ─────────────────────────────────────────────


def test_dst_spring_forward_advances_correctly():
    """March 8 2026 is the spring-forward day in America/Denver (MST → MDT,
    UTC-7 → UTC-6). A job that overflows March 7's window should land at
    March 8's window start (6am MDT), not at some shifted hour."""
    m = _machine(window_start=6, window_end=22)
    # Friday March 6, 6pm (MST) — 5-hour job runs to 11pm — past window end.
    # Should bump to March 7 6am.
    friday_late = datetime(2026, 3, 6, 18, 0, 0, tzinfo=TZ)
    start, _ = find_earliest_start(
        m, duration_hours=5, earliest_allowed_start=friday_late,
        queue=[], now=friday_late,
    )
    # Next window start is March 7 (still MST that day; DST shifts March 8).
    assert start == datetime(2026, 3, 7, 6, 0, 0, tzinfo=TZ)

    # Saturday March 7, 6pm — same scenario across the spring-forward.
    saturday_late = datetime(2026, 3, 7, 18, 0, 0, tzinfo=TZ)
    start, end = find_earliest_start(
        m, duration_hours=5, earliest_allowed_start=saturday_late,
        queue=[], now=saturday_late,
    )
    # Should land at March 8 6am MDT (not 7am or 5am — .replace() sets the
    # local hour directly regardless of DST shift).
    assert start.year == 2026 and start.month == 3 and start.day == 8
    assert start.hour == 6
    # End: 6am + 5 hours = 11am MDT, same day.
    assert end.day == 8 and end.hour == 11
