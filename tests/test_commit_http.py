"""HTTP tests for /commit using FastAPI TestClient + mocked Monday writes."""

from __future__ import annotations

import asyncio
from datetime import datetime
from unittest.mock import AsyncMock, patch
from zoneinfo import ZoneInfo

import pytest
from fastapi.testclient import TestClient

from engine.io.apply import ApplyResult
from engine.main import app
from engine.models import (
    Machine,
    MachineStatus,
    Recipe,
    RecipeStage,
    RecipeStatus,
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


@pytest.fixture
def client():
    """TestClient with worker patched + apply_plan mocked.

    TestClient handles the FastAPI lifespan (start/stop worker) automatically.
    """
    with (
        patch("engine.io.worker.read_snapshot", new_callable=AsyncMock) as mock_snap,
        patch("engine.io.worker.apply_plan", new_callable=AsyncMock) as mock_apply,
        patch("engine.io.worker.now_local", return_value=NOW),
    ):
        mock_snap.return_value = _fake_snapshot()
        mock_apply.return_value = ApplyResult(
            created_slot_ids=["new-slot-id"], reflow_hash="h-new",
        )
        with TestClient(app) as c:
            yield c


def test_commit_creates_slot_and_returns_ids(client):
    resp = client.post(
        "/commit",
        json={
            "job_reference_id": "11801201557",
            "recipe_key": "tablet-press-standard",
            "recipe_version": 1,
            "quantity": 100000,
        },
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["feasible"] is True
    assert body["job_reference_id"] == "11801201557"
    assert body["created_slot_ids"] == ["new-slot-id"]
    assert body["reflow_hash"] == "h-new"


def test_commit_rejects_simulate_sentinel(client):
    """The simulate sentinel must be rejected. After the pydantic numeric-
    string pattern landed, this rejection happens at validation (422) rather
    than the in-route defensive check (400). The defensive check stays as
    belt-and-suspenders in case the schema is ever relaxed.
    """
    resp = client.post(
        "/commit",
        json={
            "job_reference_id": "__simulate__",
            "recipe_key": "tablet-press-standard",
            "recipe_version": 1,
            "quantity": 100000,
        },
    )
    assert resp.status_code == 422


def test_commit_requires_non_empty_job_reference():
    """Pydantic validation: job_reference_id must be at least 1 char."""
    # No need for the patched client — Pydantic rejects before any worker is involved.
    with TestClient(app) as c:
        resp = c.post(
            "/commit",
            json={
                "job_reference_id": "",
                "recipe_key": "x",
                "recipe_version": 1,
                "quantity": 100,
            },
        )
    assert resp.status_code == 422


def test_commit_rejects_zero_quantity():
    with TestClient(app) as c:
        resp = c.post(
            "/commit",
            json={
                "job_reference_id": "11801201557",
                "recipe_key": "x",
                "recipe_version": 1,
                "quantity": 0,
            },
        )
    assert resp.status_code == 422


def test_commit_rejects_non_numeric_job_reference():
    """job_reference_id must be a Monday item id (digits only)."""
    with TestClient(app) as c:
        resp = c.post(
            "/commit",
            json={
                "job_reference_id": "abc123",
                "recipe_key": "x",
                "recipe_version": 1,
                "quantity": 100,
            },
        )
    assert resp.status_code == 422


def test_commit_rejects_recipe_key_with_invalid_chars():
    """recipe_key is kebab-case only — uppercase, spaces, dots are out."""
    with TestClient(app) as c:
        for bad in ("Tablet-Press", "tablet press", "tablet.press", " tablet"):
            resp = c.post(
                "/commit",
                json={
                    "job_reference_id": "11801201557",
                    "recipe_key": bad,
                    "recipe_version": 1,
                    "quantity": 100,
                },
            )
            assert resp.status_code == 422, f"expected 422 for recipe_key={bad!r}"


def test_commit_rejects_recipe_key_too_long():
    """recipe_key max_length=64 — Monday text column would happily accept more."""
    with TestClient(app) as c:
        resp = c.post(
            "/commit",
            json={
                "job_reference_id": "11801201557",
                "recipe_key": "a" * 65,
                "recipe_version": 1,
                "quantity": 100,
            },
        )
    assert resp.status_code == 422


def test_commit_returns_504_on_worker_timeout(monkeypatch):
    """If the worker doesn't respond within commit_timeout_seconds, return 504.

    The submitted event stays on the queue (no rollback); the HTTP caller
    can retry without producing duplicate work because the engine is
    idempotent on the upstream Blend Records id.
    """
    from engine.io.worker import start_worker, stop_worker

    async def _hang(*_a, **_kw):
        await asyncio.sleep(10)

    monkeypatch.setenv("COMMIT_TIMEOUT_SECONDS", "0.1")

    with (
        patch("engine.io.worker.read_snapshot", new_callable=AsyncMock) as mock_snap,
        patch("engine.io.worker.apply_plan", side_effect=_hang),
        patch("engine.io.worker.now_local", return_value=NOW),
    ):
        mock_snap.return_value = _fake_snapshot()
        with TestClient(app) as c:
            resp = c.post(
                "/commit",
                json={
                    "job_reference_id": "11801201557",
                    "recipe_key": "tablet-press-standard",
                    "recipe_version": 1,
                    "quantity": 100000,
                },
            )
    assert resp.status_code == 504
    assert "did not respond" in resp.json()["detail"]
