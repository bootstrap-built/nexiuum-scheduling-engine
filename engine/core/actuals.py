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
from datetime import timedelta
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
                # Phase 2: inherit instance from the slot so apply_plan
                # routes the update to the correct Schedule board.
                instance=slot.instance,
                # Re-stamp the N# from the existing Slot so it round-trips
                # (Snapshot → SlotWrite → board → next Snapshot).
                n_number=slot.n_number,
            )
        )
        notes.append(f"slot {slot.id}: actual_start + Status=Running")

    return Plan(slot_writes=tuple(writes), notes=tuple(notes))


def plan_for_actual_end(
    snapshot: Snapshot,
    event: ActualEndReported,
    *,
    handoff_buffer_minutes: int = 30,
) -> Plan:
    """Build a Plan that stamps actual_end + Status=Done on matching slots,
    AND adjusts dependent-stage slots' planned_start so they respect the
    actual finish time (Phase 2C baton-pass).

    Behavior:
    1. The finishing slot(s) get `actual_end` + `Status = Done`.
    2. For each finishing slot, look up its recipe → find stages that
       depend on this stage → find matching slots → push their
       `planned_start` forward to `actual_at + handoff_buffer` if they
       were planned earlier. Never pulls planned_start earlier (would
       yank work into the past). Only touches dependent slots that
       aren't already Running/Done/manually-placed — once a slot is in
       flight or finished, the engine doesn't shove it around.

    **IMMEDIATE-DEPENDENTS ONLY (intentional, NOT a bug):**
    The baton-pass pushes only stages whose `depends_on` directly
    contains `event.stage_id`. It does NOT transitively cascade.
    Example: in recipe `press → blister → clamshell`, when press
    finishes, the baton-pass pushes blister but NOT clamshell — even if
    pushing blister now means it overlaps clamshell's planned_start.
    Clamshell will be pushed on the NEXT baton-pass, when blister
    actually finishes and emits its own ActualEndReported.
    Rationale: a finished stage's actual_end is the ground truth for
    its dependents. We don't speculate about when blister WILL finish
    — we wait for that signal and react then. This keeps the engine
    deterministic and avoids cascading reflows on every press event.
    Test `test_baton_pass_does_not_cascade_to_transitive_dependents`
    pins this boundary.

    Empty Plan if no finishing slot matches or all are already finished.
    Idempotent: dependent slots already planned at/after the handoff
    cutoff are skipped.

    `handoff_buffer_minutes` is the minimum separation between a stage
    end and a dependent start. The caller (worker) reads this from
    settings.cross_stage_handoff_buffer_minutes.
    """
    candidates = _matching_slots(snapshot.slots, event.job_reference_id, event.stage_id)
    if not candidates:
        return Plan(
            notes=(f"no Schedule slot for job={event.job_reference_id} stage={event.stage_id}",),
        )

    writes: list[SlotWrite] = []
    notes: list[str] = []
    handoff_at = event.actual_at + timedelta(minutes=handoff_buffer_minutes)

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
                # Phase 2: inherit instance from the slot so apply_plan
                # routes the update to the correct Schedule board.
                instance=slot.instance,
                # Re-stamp the N# from the existing Slot so it round-trips.
                n_number=slot.n_number,
            )
        )
        notes.append(f"slot {slot.id}: actual_end + Status=Done")

    # ── Baton-pass: find dependent slots and push their planned_start ───
    # Look up the recipe pinned on the finishing slot. From there, find
    # stage ids that list `event.stage_id` in their `depends_on`. Then
    # find slots of those stages for the same job_reference_id and push
    # their planned_start to `handoff_at` if currently earlier.
    job_slots = [
        s for s in snapshot.slots if s.job_reference_id == event.job_reference_id
    ]
    pinned_recipe_key: str | None = None
    pinned_recipe_version: int | None = None
    for slot in candidates:
        if slot.recipe_key and slot.recipe_version is not None:
            pinned_recipe_key = slot.recipe_key
            pinned_recipe_version = slot.recipe_version
            break

    if pinned_recipe_key is not None and pinned_recipe_version is not None:
        recipe = snapshot.recipe_by_composite_key(
            pinned_recipe_key, pinned_recipe_version
        )
        if recipe is None:
            notes.append(
                f"recipe {pinned_recipe_key} v{pinned_recipe_version} not found in "
                f"snapshot — baton-pass skipped (dangling recipe)"
            )
        else:
            dependent_stage_ids = {
                stage.id for stage in recipe.stages
                if event.stage_id in stage.depends_on
            }
            # Phase 1.5 — synthetic packaging stages (from a spec sheet's
            # packaging_breakdown) live on slots as `pkg_*` stage_ids but
            # aren't in the recipe. By construction they depend on the
            # recipe's terminal stages. If `event.stage_id` is one of those
            # terminals (a recipe stage that nothing in the recipe depends
            # on), every `pkg_*` slot for this job is a dependent and gets
            # pushed.
            #
            # CRITICAL: gate on event.stage_id being a recipe stage. A
            # `pkg_*` slot finishing also looks "terminal" by the
            # depends-on check (nothing in the recipe depends on it
            # either), but its siblings are NOT actually downstream of it
            # — both pkg_0_Clamshell and pkg_1_Sachet depend on press,
            # not on each other. Without this gate, finishing one
            # packaging slice would falsely push every other packaging
            # slice forward.
            event_stage_is_in_recipe = any(
                s.id == event.stage_id for s in recipe.stages
            )
            event_is_terminal = event_stage_is_in_recipe and not any(
                event.stage_id in s.depends_on for s in recipe.stages
            )
            if event_is_terminal:
                for s in job_slots:
                    if s.stage_id and s.stage_id.startswith("pkg_"):
                        dependent_stage_ids.add(s.stage_id)
            for dep_slot in job_slots:
                if dep_slot.stage_id not in dependent_stage_ids:
                    continue
                if dep_slot.is_immovable:
                    notes.append(
                        f"dependent slot {dep_slot.id} "
                        f"(stage={dep_slot.stage_id}) is immovable "
                        f"(running/done/manually placed) — skipping"
                    )
                    continue
                current_start = dep_slot.planned_start
                if current_start is not None and current_start >= handoff_at:
                    notes.append(
                        f"dependent slot {dep_slot.id} "
                        f"(stage={dep_slot.stage_id}) already planned at/after "
                        f"handoff ({current_start.isoformat()}) — no push needed"
                    )
                    continue
                duration = (
                    dep_slot.planned_end - dep_slot.planned_start
                    if dep_slot.planned_start and dep_slot.planned_end
                    else None
                )
                new_end = handoff_at + duration if duration else dep_slot.planned_end
                writes.append(
                    SlotWrite(
                        slot_id=dep_slot.id,
                        planned_start=handoff_at,
                        planned_end=new_end,
                        # Phase 2: inherit instance from the dependent slot.
                        # Critical: a Gray Space press finishing can push a
                        # Nexiuum packaging slot — that write must route to
                        # the Nexiuum Schedule board, not Gray Space.
                        instance=dep_slot.instance,
                        # Baton-pass writes originate from an existing Slot —
                        # copy its N# so it survives the write/re-read cycle.
                        n_number=dep_slot.n_number,
                    )
                )
                notes.append(
                    f"baton-pass: slot {dep_slot.id} "
                    f"(stage={dep_slot.stage_id}) planned_start pushed to "
                    f"{handoff_at.isoformat()}"
                )
    elif candidates:
        notes.append(
            "finishing slots have no recipe pinning — baton-pass skipped"
        )

    return Plan(slot_writes=tuple(writes), notes=tuple(notes))
