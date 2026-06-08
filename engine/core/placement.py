"""Placement — given a machine and a duration, find the earliest valid start.

Pure function. Forward-schedule from a candidate start time, respecting:
- Machine's existing immovable slots (Running + Manually placed)
- Machine's changeover buffer between consecutive jobs
- Machine's working window (start_hour, end_hour)
- Earliest-allowed-start from dependency chain (for multi-stage jobs)

Jobs do NOT split across days. If a job's duration exceeds the working
window length, it's placed at the next day's window start regardless (so a
9-hour job in a 16-hour window starts at window_start). If duration exceeds
24 hours, the job will overrun the next-day window — caller's responsibility
to surface this (treat as a configuration error).
"""

from __future__ import annotations

from datetime import datetime, timedelta

from engine.models import Machine, Slot


def find_earliest_start(
    machine: Machine,
    duration_hours: float,
    *,
    earliest_allowed_start: datetime,
    queue: list[Slot],
    now: datetime,
) -> tuple[datetime, datetime]:
    """Find the earliest (start, end) where this duration fits on this machine.

    Arguments:
        machine: target machine
        duration_hours: job duration (Quantity / Capacity)
        earliest_allowed_start: lower bound from dependency chain (e.g., end
            of a predecessor stage). For stages with no deps, pass `now`.
        queue: existing slots on this machine that the engine must respect.
            Should include all active slots — engine packs the new job after
            the latest immovable slot. Callers should pass the result of
            `snapshot.slots_on_machine(machine.id)` here.
        now: current time (for "no earlier than now" floor).

    Returns: (start, end). Both timezone-aware in machine-local time.
    """
    # 1. Determine the tail of the queue — latest planned_end of any slot
    # the engine must work around (immovable OR active and ahead of us).
    queue_tail = _queue_tail(queue)

    # 2. Earliest the new slot could start, ignoring working window.
    floor_candidates = [now, earliest_allowed_start]
    if queue_tail is not None:
        floor_candidates.append(queue_tail + timedelta(minutes=machine.changeover_minutes))

    candidate = max(floor_candidates)

    # 3. 24-hour machines have no window constraint — place immediately.
    duration = timedelta(hours=duration_hours)
    if machine.working_window_start == 0 and machine.working_window_end >= 24:
        return candidate, candidate + duration

    # 4. If the job's duration exceeds one window's length, it can't fit any
    # single day's window. Place at the next valid window start and let end
    # overrun the clock — production runs through the night for these jobs.
    window_hours = machine.working_window_end - machine.working_window_start
    candidate = _advance_to_working_window(candidate, machine)
    if duration_hours >= window_hours:
        return candidate, candidate + duration

    # 5. Job fits in one window — try the current day, advance if it would
    # overflow the window's end. This loops at most a few times in practice.
    for _ in range(366):  # hard cap = one year of attempts; should never loop more than 1-2 times in real use
        window_end = _window_end_for_day(candidate, machine)
        if candidate + duration <= window_end:
            return candidate, candidate + duration
        candidate = (candidate + timedelta(days=1)).replace(
            hour=machine.working_window_start, minute=0, second=0, microsecond=0
        )
    raise RuntimeError(
        f"placement loop exhausted for machine={machine.name} duration={duration_hours}h"
    )


# ─────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────


def _queue_tail(queue: list[Slot]) -> datetime | None:
    """Latest planned_end among queue slots that the engine must respect.

    For v1 simplicity: respect ALL active slots (not just immovable). Engine
    is local-only and conservative — it never displaces a queued job, just
    appends behind the last one. If multiple expedites are happening
    concurrently, the worker's serialization (single async worker) ensures
    they're processed in order and the queue reflects each placement.
    """
    ends = [s.planned_end for s in queue if s.is_active and s.planned_end is not None]
    return max(ends) if ends else None


def _advance_to_working_window(t: datetime, machine: Machine) -> datetime:
    """Push `t` forward to the next moment inside the machine's working window.

    If `t` is already inside the window, returns `t` unchanged.
    """
    start_hour = machine.working_window_start
    end_hour = machine.working_window_end

    # 24-hour machine (window spans the full day) — never advance.
    if start_hour == 0 and end_hour >= 24:
        return t

    # If before today's window start, jump to today's window start.
    today_start = t.replace(hour=start_hour, minute=0, second=0, microsecond=0)
    today_end = t.replace(hour=end_hour, minute=0, second=0, microsecond=0)

    if t < today_start:
        return today_start
    if t < today_end:
        return t
    # After today's window — jump to tomorrow's window start.
    return (t + timedelta(days=1)).replace(
        hour=start_hour, minute=0, second=0, microsecond=0
    )


def _window_end_for_day(t: datetime, machine: Machine) -> datetime:
    """End of the working window for the day containing `t`."""
    end_hour = machine.working_window_end
    if end_hour >= 24:
        # Window ends at midnight next day.
        return (t + timedelta(days=1)).replace(
            hour=0, minute=0, second=0, microsecond=0
        )
    return t.replace(hour=end_hour, minute=0, second=0, microsecond=0)


def add_working_hours(t: datetime, hours: float, machine: Machine) -> datetime:
    """Advance `t` by `hours` of the machine's *working* time.

    Closed (out-of-window) hours don't count. If the machine works 08:00-16:00
    and `t` is 15:00 with hours=4, the result is 11:00 the next working day
    (1h consumed today + 3h tomorrow). A 24-hour machine has no closed time, so
    this is simply `t + hours`. If `t` falls outside the window, counting
    begins at the next window open.

    Used for the planned press→packaging buffer (#28) — the pressed product
    rests for a fixed span of working hours before packaging may begin.
    """
    if hours <= 0:
        return t
    # 24-hour machine: no closed time to skip — plain wall-clock advance.
    if machine.working_window_start == 0 and machine.working_window_end >= 24:
        return t + timedelta(hours=hours)

    remaining = timedelta(hours=hours)
    cursor = _advance_to_working_window(t, machine)
    for _ in range(366):  # hard cap = one year; real buffers span 1-2 windows
        window_end = _window_end_for_day(cursor, machine)
        available = window_end - cursor
        if remaining <= available:
            return cursor + remaining
        remaining -= available
        cursor = (cursor + timedelta(days=1)).replace(
            hour=machine.working_window_start, minute=0, second=0, microsecond=0
        )
    raise RuntimeError(
        f"add_working_hours loop exhausted for machine={machine.name} hours={hours}"
    )
