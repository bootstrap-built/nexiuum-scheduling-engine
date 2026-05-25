"""Polling sweep — the safety net for missed actual_start/actual_end webhooks.

Reads a fresh Snapshot every `polling_interval_minutes`, runs the pure-core
drift detector, and enqueues one `DriftDetected` event per stale slot. The
worker dispatches each event to `plan_for_drift`, which stamps
`drift_last_detected_at` on the slot so the next sweep suppresses it.

Lifecycle mirrors the worker (`engine.io.worker`): a single asyncio task
started in the FastAPI lifespan, cancel-safe shutdown, fail-fast helpers
for tests.

Why not APScheduler: one timer, no cron expressions, no jitter requirements.
A bare `while True: sleep(N)` loop covers Phase 1 with zero new deps.
"""

from __future__ import annotations

import asyncio
import logging

from engine.config import get_settings
from engine.core.drift import find_drift_candidates
from engine.core.timezone import now_local
from engine.io.snapshot import read_snapshot
from engine.io.worker import WorkerNotRunning, enqueue_event
from engine.models import DriftDetected

log = logging.getLogger(__name__)


_sweep_task: asyncio.Task[None] | None = None
_last_error: str | None = None  # most recent sweep_once exception, for /health


def is_sweep_alive() -> bool:
    """True if the sweep task is running and hasn't crashed."""
    return _sweep_task is not None and not _sweep_task.done()


def last_error() -> str | None:
    """Most recent exception text from sweep_once. Cleared after a success."""
    return _last_error


async def sweep_once() -> int:
    """One pass: read snapshot, find drift, enqueue events. Returns count enqueued.

    Reads a fresh snapshot every call — same contract as the worker. Catches
    `WorkerNotRunning` on enqueue so a transient outage in the worker doesn't
    crash the sweep loop (the sweep keeps observing; the worker will catch
    up when it comes back).
    """
    s = get_settings()
    now = now_local(s.factory_tz)
    snapshot = await read_snapshot()
    candidates = find_drift_candidates(
        snapshot,
        now=now,
        threshold_minutes=s.drift_threshold_minutes,
        suppression_minutes=s.drift_suppression_minutes,
    )
    if not candidates:
        log.info("sweep: no drift candidates (slots=%d)", len(snapshot.slots))
        return 0

    enqueued = 0
    for slot, kind in candidates:
        try:
            await enqueue_event(DriftDetected(slot_id=slot.id, kind=kind))
        except WorkerNotRunning:
            log.error(
                "sweep: worker not running; dropping DriftDetected slot=%s kind=%s",
                slot.id, kind,
            )
            continue
        enqueued += 1
        log.info("sweep: enqueued DriftDetected slot=%s kind=%s", slot.id, kind)
    return enqueued


async def sweep_loop() -> None:
    """Run sweep_once forever on the configured cadence.

    Per-iteration exceptions are caught and logged so a transient Monday
    read failure doesn't kill the loop. CancelledError propagates so
    `stop_sweep()` works.
    """
    global _last_error
    s = get_settings()
    interval_seconds = max(60, s.polling_interval_minutes * 60)
    log.info("sweep_loop started (interval=%ds)", interval_seconds)
    while True:
        try:
            await sweep_once()
            _last_error = None
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.exception("sweep_once raised; continuing on next interval")
            _last_error = f"{type(exc).__name__}: {exc}"
        try:
            await asyncio.sleep(interval_seconds)
        except asyncio.CancelledError:
            raise


async def start_sweep() -> asyncio.Task[None]:
    """Start the sweep task. Call once at app startup, after start_worker()."""
    global _sweep_task
    if _sweep_task is not None and not _sweep_task.done():
        return _sweep_task
    _sweep_task = asyncio.create_task(sweep_loop(), name="engine-sweep")
    return _sweep_task


async def stop_sweep() -> None:
    """Cancel the sweep task. Idempotent."""
    global _sweep_task
    if _sweep_task is None or _sweep_task.done():
        _sweep_task = None
        return
    _sweep_task.cancel()
    try:
        await _sweep_task
    except asyncio.CancelledError:
        pass
    _sweep_task = None


def reset_state_for_tests() -> None:
    """Test helper — wipe module-level sweep task handle.

    Mirrors `engine.io.worker.reset_state_for_tests`. Tests that span
    event loops need to discard the task handle between runs.
    """
    global _sweep_task, _last_error
    _sweep_task = None
    _last_error = None
