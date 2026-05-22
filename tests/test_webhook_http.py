"""HTTP tests for /webhook/monday/{secret} — auth + dispatch + echo filter."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient

from engine.io import engine_identity
from engine.main import app
from tests.conftest import TEST_ENGINE_USER_ID, TEST_WEBHOOK_SECRET

WEBHOOK_PATH = f"/webhook/monday/{TEST_WEBHOOK_SECRET}"


@pytest.fixture
def client():
    engine_identity.reset_engine_user_id()
    with TestClient(app) as c:
        yield c


# ─── Path-secret auth ────────────────────────────────────────────────────


def test_webhook_rejects_missing_secret(client):
    """No secret in path → 404 (route doesn't match)."""
    resp = client.post("/webhook/monday", json={"challenge": "x"})
    assert resp.status_code == 404


def test_webhook_rejects_wrong_secret(client):
    """Wrong secret → 401."""
    resp = client.post("/webhook/monday/wrong-secret", json={"challenge": "x"})
    assert resp.status_code == 401


# ─── Challenge handshake ─────────────────────────────────────────────────


def test_webhook_returns_challenge_unchanged(client):
    """Monday webhook setup: POST {"challenge": "..."} → echo it back."""
    resp = client.post(WEBHOOK_PATH, json={"challenge": "ABC12345"})
    assert resp.status_code == 200
    assert resp.json() == {"challenge": "ABC12345"}


# ─── Echo filter via userId ──────────────────────────────────────────────


def test_webhook_drops_engine_echo_by_user_id(client):
    """Event whose userId matches the engine's Monday user → suppressed."""
    payload = {
        "event": {
            "boardId": 18413802995,  # Schedule
            "pulseId": 99999,
            "type": "update_column_value",
            "userId": TEST_ENGINE_USER_ID,  # same user as engine
        }
    }
    resp = client.post(WEBHOOK_PATH, json=payload)
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ignored"
    assert body["kind"] == "echo"


def test_webhook_processes_operator_change_with_different_user_id(client):
    """Event with non-engine userId is processed as a real operator change."""
    payload = {
        "event": {
            "boardId": 18413802995,
            "pulseId": 99999,
            "type": "update_column_value",
            "userId": "different-user-456",
        }
    }
    resp = client.post(WEBHOOK_PATH, json=payload)
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "received"
    assert body["kind"] == "schedule_change_unhandled"


def test_webhook_processes_event_without_user_id(client):
    """Missing userId → fail open (process as operator change)."""
    payload = {
        "event": {
            "boardId": 18413802995,
            "pulseId": 99999,
            "type": "update_column_value",
            # no userId
        }
    }
    resp = client.post(WEBHOOK_PATH, json=payload)
    assert resp.status_code == 200
    assert resp.json()["status"] == "received"


# ─── Event dispatch ──────────────────────────────────────────────────────


def test_webhook_capacity_engine_event_enqueues(client):
    """A column change on the Capacity Engine board enqueues a CapacityChanged event."""
    payload = {
        "event": {
            "boardId": 18413803163,  # Capacity Engine
            "pulseId": 12047953695,  # Gandalf
            "type": "update_column_value",
            "columnId": "color_mm3gcye0",
            "userId": "different-user-456",
        }
    }
    # Patch in the route's namespace — enqueue_event is imported `from ... import`.
    with patch("engine.routes.webhook.enqueue_event", new_callable=AsyncMock) as mock_enq:
        resp = client.post(WEBHOOK_PATH, json=payload)
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "enqueued"
    assert body["kind"] == "capacity_changed"
    mock_enq.assert_awaited_once()


def test_webhook_blend_records_event_acknowledged(client):
    """Blend Records events are acknowledged but not yet handled (E5)."""
    payload = {
        "event": {
            "boardId": 18404836849,  # Blend Records
            "pulseId": 11801201557,
            "type": "change_status_column_value",
            "userId": "different-user-456",
        }
    }
    resp = client.post(WEBHOOK_PATH, json=payload)
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "received"
    assert body["kind"] == "blend_records_unhandled"


def test_webhook_unrecognized_source_acknowledged(client):
    """Events from unknown boards are acknowledged so Monday doesn't retry."""
    payload = {
        "event": {
            "boardId": 12345,
            "pulseId": 67,
            "type": "update_column_value",
            "userId": "different-user-456",
        }
    }
    resp = client.post(WEBHOOK_PATH, json=payload)
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "received"
    assert body["kind"] == "unrecognized_source"


# ─── Enqueue-only semantics ──────────────────────────────────────────────


def test_webhook_returns_200_even_if_worker_drops_event(client):
    """If worker is unavailable, webhook still returns 200 (so Monday doesn't retry)."""
    from engine.io.worker import WorkerNotRunning

    payload = {
        "event": {
            "boardId": 18413803163,
            "pulseId": 12047953695,
            "type": "update_column_value",
            "userId": "different-user-456",
        }
    }
    with patch("engine.routes.webhook.enqueue_event", new_callable=AsyncMock) as mock_enq:
        mock_enq.side_effect = WorkerNotRunning("worker down")
        resp = client.post(WEBHOOK_PATH, json=payload)
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "dropped"
    assert body["kind"] == "worker_unavailable"
