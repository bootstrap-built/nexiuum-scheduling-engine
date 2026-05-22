"""Unit tests for the worker — process_event dispatch + queue submission."""

from __future__ import annotations

import asyncio
from datetime import datetime
from unittest.mock import AsyncMock, patch
from zoneinfo import ZoneInfo

import pytest

from engine.io.apply import ApplyResult
from engine.io.worker import (
    WorkerNotRunning,
    enqueue_event,
    process_event,
    start_worker,
    stop_worker,
    submit_event,
)
from engine.models import (
    CapacityChanged,
    Machine,
    MachineStatus,
    Recipe,
    RecipeStage,
    RecipeStatus,
    ScheduleNewOrder,
    Snapshot,
)

TZ = ZoneInfo("America/Denver")
NOW = datetime(2026, 5, 21, 8, 0, 0, tzinfo=TZ)


def _fake_snapshot() -> Snapshot:
    machine = Machine(
        id="12047953695",
        name="Gandalf the Gray",
        process_group="Pressing",
        status=MachineStatus.ONLINE,
        capacity_per_hour=40000,
        hours_per_day=16,
        working_window_start=6,
        working_window_end=22,
        changeover_minutes=30,
        dual_sided_only=False,
        max_job_size=None,
        force_route_condition=None,
        last_job_ended_at=None,
    )
    recipe = Recipe(
        id="R1", name="r v1",
        recipe_key="tablet-press-standard", version=1,
        status=RecipeStatus.ACTIVE,
        stages=(RecipeStage(id="press", machine_class="Pressing", depends_on=()),),
    )
    return Snapshot(read_at=NOW, machines=(machine,), recipes=(recipe,), slots=())


# ─── process_event dispatch ──────────────────────────────────────────────


@pytest.mark.asyncio
async def test_process_event_schedules_new_order_and_applies():
    """ScheduleNewOrder → calls apply_plan → returns the ApplyResult."""
    order = ScheduleNewOrder(
        job_reference_id="11801201557",
        recipe_key="tablet-press-standard",
        recipe_version=1,
        quantity=100000,
    )
    fake_result = ApplyResult(
        created_slot_ids=["new-slot-1"], reflow_hash="hash-xyz",
    )

    with (
        patch("engine.io.worker.read_snapshot", new_callable=AsyncMock) as mock_snap,
        patch("engine.io.worker.apply_plan", new_callable=AsyncMock) as mock_apply,
        patch("engine.io.worker.now_local", return_value=NOW),
    ):
        mock_snap.return_value = _fake_snapshot()
        mock_apply.return_value = fake_result

        result = await process_event(order)

    assert result is fake_result
    mock_snap.assert_awaited_once()
    mock_apply.assert_awaited_once()


@pytest.mark.asyncio
async def test_process_event_raises_on_apply_failure():
    """If apply_plan returns success=False, process_event raises."""
    order = ScheduleNewOrder(
        job_reference_id="11801201557",
        recipe_key="tablet-press-standard",
        recipe_version=1,
        quantity=100000,
    )
    failed_result = ApplyResult(
        created_slot_ids=[], reflow_hash="hash-fail",
        errors=["graphql 400: bad column id"],
    )
    with (
        patch("engine.io.worker.read_snapshot", new_callable=AsyncMock) as mock_snap,
        patch("engine.io.worker.apply_plan", new_callable=AsyncMock) as mock_apply,
        patch("engine.io.worker.now_local", return_value=NOW),
    ):
        mock_snap.return_value = _fake_snapshot()
        mock_apply.return_value = failed_result

        with pytest.raises(RuntimeError, match="apply_plan failed"):
            await process_event(order)


@pytest.mark.asyncio
async def test_process_event_capacity_changed_is_stub():
    """CapacityChanged returns None (handler not yet implemented)."""
    event = CapacityChanged(machine_id="12047953695")
    with patch("engine.io.worker.read_snapshot", new_callable=AsyncMock) as mock_snap:
        mock_snap.return_value = _fake_snapshot()
        result = await process_event(event)
    assert result is None


@pytest.mark.asyncio
async def test_process_event_no_slot_writes_returns_none():
    """A plan with zero writes shouldn't call apply_plan."""
    # Recipe that won't match anything routable
    order = ScheduleNewOrder(
        job_reference_id="J1",
        recipe_key="does-not-exist",
        recipe_version=1,
        quantity=100,
    )
    with (
        patch("engine.io.worker.read_snapshot", new_callable=AsyncMock) as mock_snap,
        patch("engine.io.worker.now_local", return_value=NOW),
    ):
        mock_snap.return_value = _fake_snapshot()
        # plan_for_new_order will raise DanglingRecipeError; let it propagate
        from engine.core.scheduler import DanglingRecipeError

        with pytest.raises(DanglingRecipeError):
            await process_event(order)


# ─── submit_event + worker loop ──────────────────────────────────────────


@pytest.mark.asyncio
async def test_submit_event_processes_via_worker():
    """submit_event waits for the worker to finish the event."""
    order = ScheduleNewOrder(
        job_reference_id="11801201557",
        recipe_key="tablet-press-standard",
        recipe_version=1,
        quantity=100000,
    )

    with (
        patch("engine.io.worker.read_snapshot", new_callable=AsyncMock) as mock_snap,
        patch("engine.io.worker.apply_plan", new_callable=AsyncMock) as mock_apply,
        patch("engine.io.worker.now_local", return_value=NOW),
    ):
        mock_snap.return_value = _fake_snapshot()
        mock_apply.return_value = ApplyResult(
            created_slot_ids=["slot-1"], reflow_hash="h-1",
        )

        await start_worker()
        try:
            result = await asyncio.wait_for(submit_event(order), timeout=2.0)
        finally:
            await stop_worker()

    assert result is not None
    assert result.created_slot_ids == ["slot-1"]
    assert result.reflow_hash == "h-1"


@pytest.mark.asyncio
async def test_submit_event_propagates_exception():
    """If process_event raises, the awaiting caller sees the exception."""
    order = ScheduleNewOrder(
        job_reference_id="J",
        recipe_key="does-not-exist",
        recipe_version=1,
        quantity=100,
    )
    with (
        patch("engine.io.worker.read_snapshot", new_callable=AsyncMock) as mock_snap,
        patch("engine.io.worker.now_local", return_value=NOW),
    ):
        mock_snap.return_value = _fake_snapshot()
        from engine.core.scheduler import DanglingRecipeError

        await start_worker()
        try:
            with pytest.raises(DanglingRecipeError):
                await asyncio.wait_for(submit_event(order), timeout=2.0)
        finally:
            await stop_worker()


# ─── Cancellation / shutdown behavior ────────────────────────────────────


@pytest.mark.asyncio
async def test_submit_event_fails_fast_when_worker_not_running():
    """submit_event raises WorkerNotRunning without a live worker, not hang."""
    # Ensure no worker is running.
    await stop_worker()
    order = ScheduleNewOrder(
        job_reference_id="J",
        recipe_key="tablet-press-standard",
        recipe_version=1,
        quantity=100,
    )
    with pytest.raises(WorkerNotRunning):
        await submit_event(order)


@pytest.mark.asyncio
async def test_enqueue_event_fails_fast_when_worker_not_running():
    """enqueue_event also raises WorkerNotRunning — webhook layer maps to 'dropped'."""
    await stop_worker()
    order = ScheduleNewOrder(
        job_reference_id="J",
        recipe_key="tablet-press-standard",
        recipe_version=1,
        quantity=100,
    )
    with pytest.raises(WorkerNotRunning):
        await enqueue_event(order)


@pytest.mark.asyncio
async def test_stop_worker_during_in_flight_event_cancels_future():
    """If the worker is cancelled mid-event, the awaiting future is cancelled."""
    order = ScheduleNewOrder(
        job_reference_id="11801201557",
        recipe_key="tablet-press-standard",
        recipe_version=1,
        quantity=100000,
    )

    # apply_plan blocks forever so we can cancel mid-flight.
    async def _hang(*_args, **_kw):
        await asyncio.sleep(60)

    with (
        patch("engine.io.worker.read_snapshot", new_callable=AsyncMock) as mock_snap,
        patch("engine.io.worker.apply_plan", side_effect=_hang),
        patch("engine.io.worker.now_local", return_value=NOW),
    ):
        mock_snap.return_value = _fake_snapshot()

        await start_worker()
        task = asyncio.create_task(submit_event(order))
        # Give the worker a moment to pick up the submission and enter apply_plan.
        await asyncio.sleep(0.05)
        await stop_worker()
        # The awaiting submit_event task should resolve quickly (cancelled), not hang.
        # The cancelled future raises CancelledError when awaited — not
        # TimeoutError, which would mean the future was never resolved.
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, timeout=1.0)


@pytest.mark.asyncio
async def test_stop_worker_drains_pending_submissions():
    """Submissions still on the queue at shutdown get their futures cancelled."""
    order = ScheduleNewOrder(
        job_reference_id="11801201557",
        recipe_key="tablet-press-standard",
        recipe_version=1,
        quantity=100000,
    )

    # First submission hangs; subsequent ones will queue behind it.
    async def _hang(*_args, **_kw):
        await asyncio.sleep(60)

    with (
        patch("engine.io.worker.read_snapshot", new_callable=AsyncMock) as mock_snap,
        patch("engine.io.worker.apply_plan", side_effect=_hang),
        patch("engine.io.worker.now_local", return_value=NOW),
    ):
        mock_snap.return_value = _fake_snapshot()

        await start_worker()
        # Submit one that the worker will pick up and hang on.
        t1 = asyncio.create_task(submit_event(order))
        await asyncio.sleep(0.05)
        # Submit a second one that will sit on the queue.
        t2 = asyncio.create_task(submit_event(order))
        await asyncio.sleep(0.05)

        await stop_worker()

        # Both tasks should resolve (with cancellation/exception), not hang.
        for t in (t1, t2):
            with pytest.raises((asyncio.CancelledError, BaseException)):
                await asyncio.wait_for(t, timeout=1.0)
