"""Unit tests for the polling sweep — sweep_once + lifecycle (E6)."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta
from unittest.mock import AsyncMock, patch
from zoneinfo import ZoneInfo

import pytest

from engine.io.sweep import (
    is_sweep_alive,
    start_sweep,
    stop_sweep,
    sweep_once,
)
from engine.io.worker import start_worker, stop_worker
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


def _late_slot(id_: str = "S1") -> Slot:
    return Slot(
        id=id_, name=f"slot {id_}",
        job_reference_id="J1", machine_id="M1",
        stage_id="press", recipe_key="tablet-press-standard", recipe_version=1,
        quantity=100000,
        planned_start=NOW - timedelta(minutes=30), planned_end=NOW,
        actual_start=None, actual_end=None,
        dependent_on_ids=(), status=SlotStatus.QUEUED,
        manually_placed=False, priority=Priority.NORMAL,
        last_reflow_hash=None, drift_last_detected_at=None,
    )


def _snapshot(slots: tuple[Slot, ...]) -> Snapshot:
    return Snapshot(read_at=NOW, machines=(_machine(),), recipes=(_recipe(),), slots=slots)


# ─── sweep_once ──────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_sweep_once_no_candidates_returns_zero():
    """Empty snapshot → no drift, no enqueue."""
    with (
        patch("engine.io.sweep.read_snapshot", new_callable=AsyncMock) as mock_snap,
        patch("engine.io.sweep.enqueue_event", new_callable=AsyncMock) as mock_enq,
        patch("engine.io.sweep.now_local", return_value=NOW),
    ):
        mock_snap.return_value = _snapshot(())
        n = await sweep_once()
    assert n == 0
    mock_enq.assert_not_awaited()


@pytest.mark.asyncio
async def test_sweep_once_enqueues_one_drift_per_candidate():
    """Two stale slots → two DriftDetected events enqueued."""
    snap = _snapshot((_late_slot("S1"), _late_slot("S2")))
    with (
        patch("engine.io.sweep.read_snapshot", new_callable=AsyncMock) as mock_snap,
        patch("engine.io.sweep.enqueue_event", new_callable=AsyncMock) as mock_enq,
        patch("engine.io.sweep.now_local", return_value=NOW),
    ):
        mock_snap.return_value = snap
        n = await sweep_once()
    assert n == 2
    assert mock_enq.await_count == 2
    enqueued_events = [call.args[0] for call in mock_enq.await_args_list]
    assert all(isinstance(e, DriftDetected) for e in enqueued_events)
    assert {e.slot_id for e in enqueued_events} == {"S1", "S2"}
    assert all(e.kind == "late_start" for e in enqueued_events)


@pytest.mark.asyncio
async def test_sweep_once_handles_worker_not_running():
    """If the worker is down, sweep_once logs and continues, doesn't crash."""
    from engine.io.worker import WorkerNotRunning

    snap = _snapshot((_late_slot("S1"),))
    with (
        patch("engine.io.sweep.read_snapshot", new_callable=AsyncMock) as mock_snap,
        patch(
            "engine.io.sweep.enqueue_event",
            new_callable=AsyncMock,
            side_effect=WorkerNotRunning("test"),
        ),
        patch("engine.io.sweep.now_local", return_value=NOW),
    ):
        mock_snap.return_value = snap
        n = await sweep_once()
    # Enqueue failed, so count of successfully-enqueued is 0.
    assert n == 0


# ─── lifecycle: start_sweep / stop_sweep ─────────────────────────────────


@pytest.mark.asyncio
async def test_start_sweep_returns_running_task():
    """start_sweep launches a task that is alive."""
    # Patch sweep_once so the loop doesn't actually hit Monday.
    with patch("engine.io.sweep.sweep_once", new_callable=AsyncMock) as mock_once:
        mock_once.return_value = 0
        task = await start_sweep()
        try:
            assert is_sweep_alive()
            assert task is not None
            assert not task.done()
        finally:
            await stop_sweep()
    assert not is_sweep_alive()


@pytest.mark.asyncio
async def test_start_sweep_is_idempotent():
    """Calling start_sweep twice returns the same task, doesn't spawn a second."""
    with patch("engine.io.sweep.sweep_once", new_callable=AsyncMock) as mock_once:
        mock_once.return_value = 0
        t1 = await start_sweep()
        t2 = await start_sweep()
        try:
            assert t1 is t2
        finally:
            await stop_sweep()


@pytest.mark.asyncio
async def test_stop_sweep_when_not_running_is_noop():
    """Stopping an un-started sweep doesn't raise."""
    await stop_sweep()
    assert not is_sweep_alive()


@pytest.mark.asyncio
async def test_sweep_loop_survives_sweep_once_exception():
    """If sweep_once raises, the loop catches and keeps running (doesn't die).

    Verified by: after a raising sweep_once, the task remains alive and is
    waiting in asyncio.sleep for the next tick.
    """
    sweep_event = asyncio.Event()

    async def _raise_then_signal():
        sweep_event.set()
        raise RuntimeError("monday read failed")

    with patch(
        "engine.io.sweep.sweep_once",
        new_callable=AsyncMock,
        side_effect=_raise_then_signal,
    ):
        await start_sweep()
        # Wait for sweep_once to have been called and raised.
        await asyncio.wait_for(sweep_event.wait(), timeout=2.0)
        # Give the loop one tick to catch the exception and enter sleep.
        await asyncio.sleep(0.01)
        try:
            # The sweep task is still alive — the loop swallowed the exception.
            assert is_sweep_alive()
        finally:
            await stop_sweep()
    assert not is_sweep_alive()


# ─── End-to-end via the real worker ──────────────────────────────────────


@pytest.mark.asyncio
async def test_sweep_once_drives_worker_to_stamp_drift_last_detected_at():
    """Sweep + worker integration: drift is enqueued, worker dispatches plan_for_drift,
    apply_plan is called with a SlotWrite that sets drift_last_detected_at."""
    snap = _snapshot((_late_slot("S1"),))
    from engine.io.apply import ApplyResult

    with (
        patch("engine.io.sweep.read_snapshot", new_callable=AsyncMock) as mock_sweep_snap,
        patch("engine.io.sweep.now_local", return_value=NOW),
        patch("engine.io.worker.read_snapshot", new_callable=AsyncMock) as mock_worker_snap,
        patch("engine.io.worker.now_local", return_value=NOW),
        patch("engine.io.worker.apply_plan", new_callable=AsyncMock) as mock_apply,
    ):
        mock_sweep_snap.return_value = snap
        mock_worker_snap.return_value = snap
        mock_apply.return_value = ApplyResult(
            updated_slot_ids=["S1"], reflow_hash="h-drift",
        )

        await start_worker()
        try:
            n = await sweep_once()
            # Let the worker pick up + process the queued DriftDetected.
            for _ in range(50):
                await asyncio.sleep(0)
                if mock_apply.await_count >= 1:
                    break
        finally:
            await stop_worker()

    assert n == 1
    mock_apply.assert_awaited_once()
    plan = mock_apply.await_args.args[0]
    assert len(plan.slot_writes) == 1
    w = plan.slot_writes[0]
    assert w.slot_id == "S1"
    assert w.drift_last_detected_at == NOW
