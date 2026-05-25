"""Tests for the /health endpoint structure (worker + sweep liveness)."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient

from engine.io.worker import start_worker, stop_worker
from engine.main import app


def test_health_returns_degraded_when_no_lifespan():
    """Without `with TestClient(app)`, lifespan never runs → worker + sweep
    are not started → /health reports degraded."""
    client = TestClient(app)  # no `with` — no lifespan
    resp = client.get("/health")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "degraded"
    assert body["worker"]["alive"] is False
    assert body["sweep"]["alive"] is False
    assert body["worker"]["queue_depth"] == 0
    # last_error should be None on a fresh process
    assert body["worker"]["last_error"] is None
    assert body["sweep"]["last_error"] is None


def test_health_returns_ok_when_lifespan_started_both_tasks():
    """`with TestClient(app)` runs the lifespan, which starts worker + sweep."""
    # Patch read_snapshot so sweep_once doesn't actually hit Monday.
    fake_snap_data: dict = {}
    with (
        patch("engine.io.sweep.read_snapshot", new_callable=AsyncMock) as mock_snap,
    ):
        # Return an "empty" snapshot to keep sweep happy.
        from engine.models import Snapshot
        from datetime import datetime
        from zoneinfo import ZoneInfo
        TZ = ZoneInfo("America/Denver")
        mock_snap.return_value = Snapshot(
            read_at=datetime(2026, 5, 24, 12, 0, 0, tzinfo=TZ),
            machines=(), recipes=(), slots=(),
        )
        with TestClient(app) as c:
            resp = c.get("/health")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ok"
    assert body["worker"]["alive"] is True
    assert body["sweep"]["alive"] is True


def test_health_shape_has_all_expected_keys():
    """Lock the contract for monitoring integration."""
    client = TestClient(app)
    body = client.get("/health").json()
    assert set(body.keys()) >= {"status", "version", "worker", "sweep"}
    assert set(body["worker"].keys()) >= {"alive", "queue_depth", "last_error"}
    assert set(body["sweep"].keys()) >= {"alive", "last_error"}


@pytest.mark.asyncio
async def test_worker_last_error_populated_after_failure():
    """A failing process_event leaves the exception text in last_error."""
    from engine.io import worker as worker_module
    from engine.models import ScheduleNewOrder

    order = ScheduleNewOrder(
        job_reference_id="11801201557",
        recipe_key="does-not-exist",  # will raise DanglingRecipeError
        recipe_version=1,
        quantity=100,
    )
    from datetime import datetime
    from zoneinfo import ZoneInfo
    from engine.models import Snapshot
    TZ = ZoneInfo("America/Denver")
    snap = Snapshot(
        read_at=datetime(2026, 5, 24, 12, 0, 0, tzinfo=TZ),
        machines=(), recipes=(), slots=(),
    )
    with (
        patch("engine.io.worker.read_snapshot", new_callable=AsyncMock) as mock_snap,
        patch("engine.io.worker.now_local",
              return_value=datetime(2026, 5, 24, 12, 0, 0, tzinfo=TZ)),
    ):
        mock_snap.return_value = snap
        await start_worker()
        try:
            from engine.io.worker import submit_event
            with pytest.raises(Exception):
                await submit_event(order)
        finally:
            await stop_worker()
    assert worker_module.last_error() is not None
    assert "DanglingRecipe" in worker_module.last_error()
