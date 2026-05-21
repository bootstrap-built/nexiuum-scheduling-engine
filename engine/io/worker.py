"""Async worker — serializes engine writes.

One worker coroutine, one queue. All write paths (commit endpoint,
webhook intake, polling sweep) enqueue events here. The worker drains
the queue serially, runs the appropriate handler against a fresh
snapshot, applies the resulting Plan, and stamps the echo guard.

Read paths (`/simulate`, `/health`) bypass the worker — they read fresh
state on every request without contending with writes.

Per v3 plan: this is the concurrency model decision. Single async worker
keeps the engine simple and deterministic at Phase 1 scale. If queue
depth ever exceeds ~10 the worker should be sharded by machine.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass

from engine.config import get_settings
from engine.core.scheduler import plan_for_new_order
from engine.core.timezone import now_local
from engine.io.apply import ApplyResult, apply_plan
from engine.io.echo_guard import get_echo_guard
from engine.io.snapshot import read_snapshot
from engine.models import (
    ActualEndReported,
    ActualStartReported,
    CapacityChanged,
    DriftDetected,
    Event,
    ExpediteRequested,
    ManualReschedule,
    ScheduleNewOrder,
)

log = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────────
# Submission wrapper
# ─────────────────────────────────────────────────────────────────────────


@dataclass
class _Submission:
    """One event enqueued for processing, with a future the caller can await."""

    event: Event
    future: asyncio.Future[ApplyResult | None]


_queue: asyncio.Queue[_Submission] | None = None
_worker_task: asyncio.Task[None] | None = None


def get_queue() -> asyncio.Queue[_Submission]:
    """Module-level singleton queue. Lazy because asyncio.Queue() needs a loop."""
    global _queue
    if _queue is None:
        _queue = asyncio.Queue()
    return _queue


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
        if not plan.slot_writes:
            return None
        result = await apply_plan(plan)
        get_echo_guard().remember(result.reflow_hash)
        log.info(
            "ScheduleNewOrder applied: created=%s reflow_hash=%s",
            result.created_slot_ids, result.reflow_hash,
        )
        return result

    # ── Stubs for handlers not yet implemented (E5/E6 territory) ──
    # The infrastructure is in place; specific reflow algorithms come next.
    if isinstance(event, CapacityChanged):
        log.info("CapacityChanged event for machine %s — reflow not yet implemented", event.machine_id)
        return None
    if isinstance(event, (ActualStartReported, ActualEndReported)):
        log.info("Actual event %s — handler not yet implemented", type(event).__name__)
        return None
    if isinstance(event, (ManualReschedule, ExpediteRequested, DriftDetected)):
        log.info("Event %s — handler not yet implemented", type(event).__name__)
        return None

    log.warning("Unhandled event type: %s", type(event).__name__)
    return None


# ─────────────────────────────────────────────────────────────────────────
# Worker loop
# ─────────────────────────────────────────────────────────────────────────


async def worker_loop() -> None:
    """Drain the event queue forever. One event at a time, serialized."""
    queue = get_queue()
    log.info("worker_loop started")
    while True:
        submission = await queue.get()
        try:
            result = await process_event(submission.event)
            if not submission.future.done():
                submission.future.set_result(result)
        except Exception as exc:
            log.exception("worker failed processing event %r", submission.event)
            if not submission.future.done():
                submission.future.set_exception(exc)
        finally:
            queue.task_done()


# ─────────────────────────────────────────────────────────────────────────
# Public API
# ─────────────────────────────────────────────────────────────────────────


async def submit_event(event: Event) -> ApplyResult | None:
    """Submit an event to the worker; await processing; return the result."""
    submission = _Submission(event=event, future=asyncio.get_running_loop().create_future())
    await get_queue().put(submission)
    return await submission.future


async def start_worker() -> asyncio.Task[None]:
    """Start the worker task. Call once at app startup."""
    global _worker_task
    if _worker_task is not None and not _worker_task.done():
        return _worker_task
    _worker_task = asyncio.create_task(worker_loop(), name="engine-worker")
    return _worker_task


async def stop_worker() -> None:
    """Cancel the worker task and wait for shutdown. Call at app shutdown."""
    global _worker_task
    if _worker_task is None or _worker_task.done():
        return
    _worker_task.cancel()
    try:
        await _worker_task
    except asyncio.CancelledError:
        pass
    _worker_task = None
