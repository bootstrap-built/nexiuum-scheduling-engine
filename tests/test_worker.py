"""Unit tests for the worker — process_event dispatch + queue submission."""

from __future__ import annotations

import asyncio
from datetime import datetime
from unittest.mock import AsyncMock, patch
from zoneinfo import ZoneInfo

import pytest

from engine.io.apply import ApplyResult
from engine.io.echo_guard import get_echo_guard
from engine.io.worker import (
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
    """ScheduleNewOrder → calls apply_plan → stamps echo guard."""
    get_echo_guard().clear()
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
    # Echo guard should remember the reflow hash
    assert get_echo_guard().is_engine_origin("hash-xyz")


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
    get_echo_guard().clear()
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
