"""Pure-core: drift detection for the polling safety net (E6).

The webhook path is the primary source of `actual_start` / `actual_end`.
This module covers the case where a webhook is missed (engine restarting,
network blip, source-board automation lag): a periodic sweep reads the
Snapshot, finds slots that "should have started/ended by now" but haven't,
and emits one `DriftDetected` event per slot.

Phase 1 contract — detect only:
  - Sweep stamps `drift_last_detected_at` on each candidate slot so the
    next sweep won't re-emit within the suppression window.
  - No reflow on drift in Phase 1 (single press stage; nothing to ripple).
    Reflow on drift is a future ticket — see plan v3 §"Polling safety net".

`find_drift_candidates` is pure (takes Snapshot + now + thresholds, returns
candidate list). `plan_for_drift` is pure (takes Snapshot + DriftDetected,
returns a Plan with one SlotWrite stamping `drift_last_detected_at`).
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Literal

from engine.models import (
    DriftDetected,
    Plan,
    Slot,
    SlotStatus,
    SlotWrite,
    Snapshot,
)

log = logging.getLogger(__name__)


DriftKind = Literal["late_start", "late_end"]


def _is_late_start(slot: Slot, now: datetime, threshold: timedelta) -> bool:
    """Queued slot whose planned_start is more than `threshold` in the past
    and has no actual_start yet."""
    if slot.status != SlotStatus.QUEUED:
        return False
    if slot.actual_start is not None:
        return False
    if slot.planned_start is None:
        return False
    return slot.planned_start < (now - threshold)


def _is_late_end(slot: Slot, now: datetime, threshold: timedelta) -> bool:
    """Running slot whose planned_end is more than `threshold` in the past
    and has no actual_end yet."""
    if slot.status != SlotStatus.RUNNING:
        return False
    if slot.actual_end is not None:
        return False
    if slot.planned_end is None:
        return False
    return slot.planned_end < (now - threshold)


def _is_suppressed(slot: Slot, now: datetime, suppression: timedelta) -> bool:
    """True if drift was already detected on this slot inside the suppression
    window — sweep should leave it alone until the window expires."""
    if slot.drift_last_detected_at is None:
        return False
    return (now - slot.drift_last_detected_at) < suppression


def find_drift_candidates(
    snapshot: Snapshot,
    *,
    now: datetime,
    threshold_minutes: int,
    suppression_minutes: int,
) -> list[tuple[Slot, DriftKind]]:
    """Scan the snapshot for slots exceeding their drift threshold.

    Skips:
      - Blocked, Done, manually_placed slots (engine doesn't manage them)
      - Slots inside the suppression window (already flagged recently)

    Returns the (slot, kind) pairs the sweep should emit as DriftDetected
    events. Order is deterministic (snapshot order) for testability.
    """
    threshold = timedelta(minutes=threshold_minutes)
    suppression = timedelta(minutes=suppression_minutes)
    out: list[tuple[Slot, DriftKind]] = []
    for slot in snapshot.slots:
        if slot.manually_placed:
            continue
        if slot.status in {SlotStatus.BLOCKED, SlotStatus.DONE}:
            continue
        if _is_suppressed(slot, now, suppression):
            continue
        if _is_late_start(slot, now, threshold):
            out.append((slot, "late_start"))
            continue
        if _is_late_end(slot, now, threshold):
            out.append((slot, "late_end"))
    return out


def plan_for_drift(
    snapshot: Snapshot,
    event: DriftDetected,
    *,
    now: datetime,
    threshold_minutes: int,
    suppression_minutes: int,
) -> Plan:
    """Build a Plan that stamps `drift_last_detected_at = now` on the slot.

    Re-validates the drift predicate against the FRESH snapshot the worker
    reads at dispatch time. Sweep enqueues `DriftDetected` based on the
    snapshot it saw, but the worker reads a new snapshot before applying.
    Between sweep and worker, any of these can happen:
      - An `actual_start`/`actual_end` webhook lands → slot is no longer late
      - The slot moves to Done/Blocked/manually_placed
      - The planned dates get edited
      - Another DriftDetected for the same slot already stamped recently
        (suppression window now active)

    Without re-validation, a stale `late_start` event could stamp a slot
    that has since become Running, and the stamp would then suppress a
    legitimate `late_end` detection for `suppression_minutes`. Per-slot
    suppression is correct (one stamp = one Monday write) but it must
    only fire when the slot is *currently* drifted for `event.kind`.

    Phase 1: stamps `drift_last_detected_at` only. No reflow.
    """
    slot = next((s for s in snapshot.slots if s.id == event.slot_id), None)
    if slot is None:
        return Plan(notes=(f"drift slot {event.slot_id} no longer in snapshot",))

    if slot.manually_placed:
        return Plan(notes=(f"drift slot {slot.id} now manually_placed; skipping",))

    if slot.status in {SlotStatus.BLOCKED, SlotStatus.DONE}:
        return Plan(notes=(f"drift slot {slot.id} status={slot.status.value}; skipping",))

    suppression = timedelta(minutes=suppression_minutes)
    if _is_suppressed(slot, now, suppression):
        return Plan(notes=(f"drift slot {slot.id} already inside suppression window; skipping",))

    threshold = timedelta(minutes=threshold_minutes)
    if event.kind == "late_start":
        if not _is_late_start(slot, now, threshold):
            return Plan(notes=(f"drift slot {slot.id} no longer late_start; skipping",))
    elif event.kind == "late_end":
        if not _is_late_end(slot, now, threshold):
            return Plan(notes=(f"drift slot {slot.id} no longer late_end; skipping",))
    else:
        # Unknown kind — refuse rather than write a meaningless stamp.
        return Plan(notes=(f"drift slot {slot.id}: unknown kind {event.kind!r}; skipping",))

    return Plan(
        slot_writes=(
            # Phase 2: inherit instance from the slot so the drift stamp
            # lands on the correct Schedule board.
            SlotWrite(
                slot_id=slot.id,
                drift_last_detected_at=now,
                instance=slot.instance,
            ),
        ),
        notes=(f"drift {event.kind} on slot {slot.id}: stamped drift_last_detected_at",),
    )
