"""Async worker — serializes engine writes.

One worker coroutine, one queue. All write paths (commit endpoint,
webhook intake, polling sweep) enqueue events here. The worker drains
the queue serially, runs the appropriate handler against a fresh
snapshot, and applies the resulting Plan.

Read paths (`/simulate`, `/health`) bypass the worker — they read fresh
state on every request without contending with writes.

Per v3 plan: this is the concurrency model decision. Single async worker
keeps the engine simple and deterministic at Phase 1 scale. If queue
depth ever exceeds ~10 the worker should be sharded by machine.

Two submission paths:
- `submit_event(event)` — awaits processing, returns result (used by /commit).
- `enqueue_event(event)` — fire-and-forget, returns immediately (used by
  webhooks, where slow processing would cause Monday to retry the webhook).
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass

from engine.config import get_settings
from engine.core.actuals import plan_for_actual_end, plan_for_actual_start
from engine.core.drift import plan_for_drift
from engine.core.scheduler import plan_for_new_order
from engine.core.timezone import now_local
from engine.io.apply import ApplyResult, apply_plan
from engine.io.snapshot import read_snapshot
from engine.core.spec_sheet import (
    SpecSheetParseError,
    UnsupportedManufacturingRouteError,
    UnsupportedProductTypeError,
    build_schedule_order,
    parse_spec_sheet_payload,
)
from engine.io.spec_sheet_io import (
    ProductionScheduleReadError,
    read_ps_item_for_ingest,
)
from engine.models import (
    ActualEndReported,
    ActualStartReported,
    CapacityChanged,
    DriftDetected,
    Event,
    ExpediteRequested,
    ManualReschedule,
    ScheduleNewOrder,
    SpecSheetItemReady,
)

log = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────────
# Submission wrapper
# ─────────────────────────────────────────────────────────────────────────


@dataclass
class _Submission:
    """One event enqueued for processing.

    `future` resolves with the ApplyResult (or exception) when processing
    completes. Fire-and-forget callers (webhooks) simply don't await it.
    """

    event: Event
    future: asyncio.Future[ApplyResult | None]


_queue: asyncio.Queue[_Submission] | None = None
_worker_task: asyncio.Task[None] | None = None
_last_error: str | None = None  # most recent process_event exception, for /health


def get_queue() -> asyncio.Queue[_Submission]:
    """Module-level singleton queue. Lazy because asyncio.Queue() needs a loop."""
    global _queue
    if _queue is None:
        _queue = asyncio.Queue()
    return _queue


def is_worker_alive() -> bool:
    """True if a worker task is running and hasn't crashed."""
    return _worker_task is not None and not _worker_task.done()


def queue_depth() -> int:
    """Current queue depth — number of pending submissions waiting on the worker.

    Returns 0 if the queue hasn't been created yet (no events ever submitted)."""
    if _queue is None:
        return 0
    return _queue.qsize()


def last_error() -> str | None:
    """Most recent exception text from process_event. Cleared after a success."""
    return _last_error


# ─────────────────────────────────────────────────────────────────────────
# Event dispatch
# ─────────────────────────────────────────────────────────────────────────


async def process_event(event: Event) -> ApplyResult | None:
    """Dispatch one event to its handler, run pure-core, apply Plan.

    Reads a fresh snapshot at the start of every event — Monday is system
    of record and the engine never caches scheduling-relevant state
    across events.
    """
    s = get_settings()
    snapshot = await read_snapshot()
    now = now_local(s.factory_tz)

    if isinstance(event, ScheduleNewOrder):
        plan = plan_for_new_order(snapshot, event, now=now)
        return await _apply_or_noop(plan, label="ScheduleNewOrder")

    if isinstance(event, SpecSheetItemReady):
        # Phase 2D — translate a Production Schedule item into a
        # ScheduleNewOrder + reuse the existing planning path. Reading
        # Monday for the payload happens here (IO shell), translation
        # is pure-core in engine.core.spec_sheet.
        try:
            ingest = await read_ps_item_for_ingest(event.item_id)
            payload = parse_spec_sheet_payload(ingest.payload_text)
            order = build_schedule_order(
                payload,
                job_reference_id=event.item_id,
                n_number=ingest.n_number,
            )
        except UnsupportedManufacturingRouteError as e:
            log.info(
                "SpecSheetItemReady item=%s skipped: %s",
                event.item_id, e,
            )
            return None
        except UnsupportedProductTypeError as e:
            # Surface as warning (not error) so /health stays green —
            # operator data-entry / missing-recipe is expected during
            # the MVP rollout.
            log.warning(
                "SpecSheetItemReady item=%s rejected: %s",
                event.item_id, e,
            )
            return None
        except (SpecSheetParseError, ProductionScheduleReadError) as e:
            log.error(
                "SpecSheetItemReady item=%s ingest failed: %s",
                event.item_id, e,
            )
            return None
        plan = plan_for_new_order(snapshot, order, now=now)
        return await _apply_or_noop(plan, label=f"SpecSheetItemReady[{event.item_id}]")

    if isinstance(event, ActualStartReported):
        plan = plan_for_actual_start(snapshot, event)
        return await _apply_or_noop(plan, label="ActualStartReported")

    if isinstance(event, ActualEndReported):
        plan = plan_for_actual_end(
            snapshot, event,
            handoff_buffer_minutes=s.cross_stage_handoff_buffer_minutes,
        )
        return await _apply_or_noop(plan, label="ActualEndReported")

    if isinstance(event, DriftDetected):
        plan = plan_for_drift(
            snapshot, event, now=now,
            threshold_minutes=s.drift_threshold_minutes,
            suppression_minutes=s.drift_suppression_minutes,
        )
        return await _apply_or_noop(plan, label=f"DriftDetected[{event.kind}]")

    # ── Stubs for handlers not yet implemented ──
    if isinstance(event, CapacityChanged):
        log.info("CapacityChanged event for machine %s — reflow not yet implemented", event.machine_id)
        return None
    if isinstance(event, (ManualReschedule, ExpediteRequested)):
        log.info("Event %s — handler not yet implemented", type(event).__name__)
        return None

    log.warning("Unhandled event type: %s", type(event).__name__)
    return None


async def _apply_or_noop(plan, *, label: str) -> ApplyResult | None:
    """Shared apply tail: empty plan → no-op; apply → check success → log."""
    if not plan.slot_writes:
        log.info("%s: no-op (notes=%s)", label, list(plan.notes))
        return None
    result = await apply_plan(plan)
    if not result.success:
        log.error(
            "%s apply_plan failed: errors=%s reflow_hash=%s",
            label, result.errors, result.reflow_hash,
        )
        raise RuntimeError(f"apply_plan failed: {result.errors}")
    log.info(
        "%s applied: created=%s updated=%s reflow_hash=%s",
        label, result.created_slot_ids, result.updated_slot_ids, result.reflow_hash,
    )
    return result


# ─────────────────────────────────────────────────────────────────────────
# Worker loop
# ─────────────────────────────────────────────────────────────────────────


async def worker_loop() -> None:
    """Drain the event queue forever. One event at a time, serialized.

    Cancellation contract: if the worker is cancelled mid-event, the
    active submission's future is cancelled (so awaiting callers don't
    hang) before re-raising CancelledError. `stop_worker()` then drains
    any remaining queued submissions.
    """
    global _last_error
    queue = get_queue()
    log.info("worker_loop started")
    while True:
        submission = await queue.get()
        try:
            try:
                result = await process_event(submission.event)
            except asyncio.CancelledError:
                if not submission.future.done():
                    submission.future.cancel()
                raise
            except Exception as exc:
                log.exception("worker failed processing event %r", submission.event)
                _last_error = f"{type(exc).__name__}: {exc}"
                if not submission.future.done():
                    submission.future.set_exception(exc)
            else:
                _last_error = None
                if not submission.future.done():
                    submission.future.set_result(result)
        finally:
            queue.task_done()


# ─────────────────────────────────────────────────────────────────────────
# Public API
# ─────────────────────────────────────────────────────────────────────────


class WorkerNotRunning(RuntimeError):
    """Raised when submit_event / enqueue_event is called with no live worker."""


async def submit_event(event: Event) -> ApplyResult | None:
    """Submit an event to the worker; await processing; return the result.

    Used by /commit, which needs the created slot ids in its response.
    Raises WorkerNotRunning if the worker isn't alive — fail fast rather
    than awaiting a future that no one will resolve.
    """
    if not is_worker_alive():
        raise WorkerNotRunning("worker task is not running")
    submission = _Submission(event=event, future=asyncio.get_running_loop().create_future())
    await get_queue().put(submission)
    return await submission.future


async def enqueue_event(event: Event) -> None:
    """Drop an event on the queue without waiting for processing.

    Used by webhooks: Monday retries slow webhooks, so we must return
    200 immediately. The worker processes async; results are not
    surfaced to the webhook caller. Raises WorkerNotRunning if no live
    worker.
    """
    if not is_worker_alive():
        raise WorkerNotRunning("worker task is not running")
    loop = asyncio.get_running_loop()
    submission = _Submission(event=event, future=loop.create_future())
    await get_queue().put(submission)
    # Intentionally do not await submission.future.


async def start_worker() -> asyncio.Task[None]:
    """Start the worker task. Call once at app startup."""
    global _worker_task
    if _worker_task is not None and not _worker_task.done():
        return _worker_task
    _worker_task = asyncio.create_task(worker_loop(), name="engine-worker")
    return _worker_task


async def stop_worker() -> None:
    """Cancel the worker task and drain pending submissions.

    Any submissions still in the queue when shutdown begins have their
    futures cancelled so callers don't hang forever.
    """
    global _worker_task
    if _worker_task is None or _worker_task.done():
        _worker_task = None
        return
    _worker_task.cancel()
    try:
        await _worker_task
    except asyncio.CancelledError:
        pass
    _worker_task = None
    # Drain any submissions still on the queue.
    queue = get_queue()
    drained = 0
    while not queue.empty():
        try:
            sub = queue.get_nowait()
        except asyncio.QueueEmpty:
            break
        if not sub.future.done():
            sub.future.cancel()
        queue.task_done()
        drained += 1
    if drained:
        log.info("stop_worker drained %d pending submissions", drained)


def reset_state_for_tests() -> None:
    """Test helper — wipe module-level singletons.

    asyncio.Queue is loop-bound; tests that span multiple event loops
    need to discard the queue between runs. Call this from a fixture.
    """
    global _queue, _worker_task, _last_error
    _queue = None
    _worker_task = None
    _last_error = None
