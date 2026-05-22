"""Pure-core: turn source-board actual_start/end events into Schedule writes.

E5 covers the start side: Blend Records "Blend Status" flips to "Pressing"
→ the engine writes `actual_start = <event timestamp>` + `Status = Running`
on the matching Schedule slot(s) atomically.

A "matching slot" is one where:
  - `job_reference_id` == event.job_reference_id
  - `stage_id` == event.stage_id (the engine-side stage label, e.g. "press")
  - Status is Queued (we never overwrite Running/Done — idempotent)
  - `actual_start` is None (defence-in-depth idempotency)

If no slot matches, returns an empty Plan: this is the legitimate
"operator advanced a blend the engine never scheduled" case — log + ack,
don't error.
"""

from __future__ import annotations

import logging
from typing import Iterable

from engine.models import (
    ActualEndReported,
    ActualStartReported,
    Plan,
    Slot,
    SlotStatus,
    SlotWrite,
    Snapshot,
)

log = logging.getLogger(__name__)


def _matching_slots(
    slots: Iterable[Slot],
    job_reference_id: str,
    stage_id: str,
) -> list[Slot]:
    return [
        s for s in slots
        if s.job_reference_id == job_reference_id and s.stage_id == stage_id
    ]


def plan_for_actual_start(
    snapshot: Snapshot,
    event: ActualStartReported,
) -> Plan:
    """Build a Plan that stamps actual_start + Status=Running on matching slots.

    Empty Plan when no slot matches, when the slot already has
    actual_start, or when the slot is already Running/Done. The IO shell
    is responsible for treating an empty Plan as a no-op.
    """
    candidates = _matching_slots(snapshot.slots, event.job_reference_id, event.stage_id)
    if not candidates:
        log.info(
            "plan_for_actual_start: no slot for job=%s stage=%s — no-op",
            event.job_reference_id, event.stage_id,
        )
        return Plan(
            notes=(f"no Schedule slot for job={event.job_reference_id} stage={event.stage_id}",),
        )

    writes: list[SlotWrite] = []
    notes: list[str] = []
    for slot in candidates:
        if slot.actual_start is not None:
            notes.append(f"slot {slot.id} already has actual_start; skipping")
            continue
        if slot.status in {SlotStatus.RUNNING, SlotStatus.DONE}:
            notes.append(f"slot {slot.id} status={slot.status.value}; skipping")
            continue
        writes.append(
            SlotWrite(
                slot_id=slot.id,
                actual_start=event.actual_at,
                status=SlotStatus.RUNNING,
            )
        )
        notes.append(f"slot {slot.id}: actual_start + Status=Running")

    return Plan(slot_writes=tuple(writes), notes=tuple(notes))


def plan_for_actual_end(
    snapshot: Snapshot,
    event: ActualEndReported,
) -> Plan:
    """Build a Plan that stamps actual_end + Status=Done on matching slots.

    Symmetric to plan_for_actual_start. Empty Plan if no slot matches or
    the slot is already Done. Idempotent on `actual_end` being None.

    Phase 1 note: Blend Records has no clean "Pressed" signal yet — the
    actual_end webhook source is TBD pending Jason/Zane input. This
    function is wired for symmetry but not invoked from the webhook
    layer until that's decided.
    """
    candidates = _matching_slots(snapshot.slots, event.job_reference_id, event.stage_id)
    if not candidates:
        return Plan(
            notes=(f"no Schedule slot for job={event.job_reference_id} stage={event.stage_id}",),
        )

    writes: list[SlotWrite] = []
    notes: list[str] = []
    for slot in candidates:
        if slot.actual_end is not None:
            notes.append(f"slot {slot.id} already has actual_end; skipping")
            continue
        if slot.status == SlotStatus.DONE:
            notes.append(f"slot {slot.id} already Done; skipping")
            continue
        writes.append(
            SlotWrite(
                slot_id=slot.id,
                actual_end=event.actual_at,
                status=SlotStatus.DONE,
            )
        )
        notes.append(f"slot {slot.id}: actual_end + Status=Done")

    return Plan(slot_writes=tuple(writes), notes=tuple(notes))
