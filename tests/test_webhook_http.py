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


def test_webhook_blend_records_ignored_non_status_column(client):
    """Blend Records edits on any column other than Blend Status are no-ops."""
    payload = {
        "event": {
            "boardId": 18404836849,
            "pulseId": 11801201557,
            "type": "update_column_value",
            "columnId": "text_some_other_column",
            "userId": "different-user-456",
        }
    }
    resp = client.post(WEBHOOK_PATH, json=payload)
    assert resp.status_code == 200
    assert resp.json()["kind"] == "blend_records_ignored_column"


def test_webhook_blend_records_status_not_pressing_acknowledged(client):
    """Blend Status changing to anything other than Pressing → ack but no enqueue."""
    payload = {
        "event": {
            "boardId": 18404836849,
            "pulseId": 11801201557,
            "type": "update_column_value",
            "columnId": "color_mm1mb9cm",
            "userId": "different-user-456",
            "value": {"label": {"text": "Blending", "index": 2}},
        }
    }
    resp = client.post(WEBHOOK_PATH, json=payload)
    assert resp.status_code == 200
    assert resp.json()["kind"] == "blend_records_status_not_actionable"


def test_webhook_blend_records_pressing_enqueues_actual_start(client):
    """Blend Status → Pressing fires an ActualStartReported event."""
    payload = {
        "event": {
            "boardId": 18404836849,
            "pulseId": 11801201557,
            "type": "update_column_value",
            "columnId": "color_mm1mb9cm",
            "userId": "different-user-456",
            "value": {"label": {"text": "Pressing", "index": 5}},
            "changedAt": 1779879000,  # 2026-05-22 ~16:10 UTC
        }
    }
    with patch("engine.routes.webhook.enqueue_event", new_callable=AsyncMock) as mock_enq:
        resp = client.post(WEBHOOK_PATH, json=payload)
    assert resp.status_code == 200
    assert resp.json()["kind"] == "actual_start_reported"
    mock_enq.assert_awaited_once()
    # Inspect the enqueued event.
    enqueued = mock_enq.await_args.args[0]
    from engine.models import ActualStartReported
    assert isinstance(enqueued, ActualStartReported)
    assert enqueued.job_reference_id == "11801201557"
    assert enqueued.stage_id == "press"


def test_webhook_blend_records_done_enqueues_actual_end(client):
    """Phase 2C: Blend Status → Done fires an ActualEndReported event.
    This drives both the press slot's actual_end stamp AND the baton-pass
    to dependent packaging slots."""
    payload = {
        "event": {
            "boardId": 18404836849,
            "pulseId": 11801201557,
            "type": "update_column_value",
            "columnId": "color_mm1mb9cm",
            "userId": "different-user-456",
            "value": {"label": {"text": "Done", "index": 1}},
            "changedAt": 1779879000,
        }
    }
    with patch("engine.routes.webhook.enqueue_event", new_callable=AsyncMock) as mock_enq:
        resp = client.post(WEBHOOK_PATH, json=payload)
    assert resp.status_code == 200
    assert resp.json()["kind"] == "actual_end_reported"
    mock_enq.assert_awaited_once()
    enqueued = mock_enq.await_args.args[0]
    from engine.models import ActualEndReported
    assert isinstance(enqueued, ActualEndReported)
    assert enqueued.job_reference_id == "11801201557"
    assert enqueued.stage_id == "press"


def test_webhook_blend_records_pressing_falls_back_to_now_without_changed_at(client):
    """Without changedAt, the engine uses now() — still enqueues a valid event."""
    payload = {
        "event": {
            "boardId": 18404836849,
            "pulseId": 11801201557,
            "type": "update_column_value",
            "columnId": "color_mm1mb9cm",
            "userId": "different-user-456",
            "value": {"label": {"text": "Pressing", "index": 5}},
            # no changedAt
        }
    }
    with patch("engine.routes.webhook.enqueue_event", new_callable=AsyncMock) as mock_enq:
        resp = client.post(WEBHOOK_PATH, json=payload)
    assert resp.status_code == 200
    assert resp.json()["kind"] == "actual_start_reported"
    mock_enq.assert_awaited_once()


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


# ─── Phase 2D — spec-sheet trigger ───────────────────────────────────────


SPEC_SHEET_PATH = f"/webhook/monday/spec-sheet/{TEST_WEBHOOK_SECRET}"
PRODUCTION_SCHEDULE_BOARD = 8196668916


def test_spec_sheet_webhook_rejects_wrong_secret(client):
    resp = client.post(
        "/webhook/monday/spec-sheet/nope",
        json={"event": {"boardId": PRODUCTION_SCHEDULE_BOARD, "pulseId": 1}},
    )
    assert resp.status_code == 401


def test_spec_sheet_webhook_challenge_handshake(client):
    resp = client.post(SPEC_SHEET_PATH, json={"challenge": "abc"})
    assert resp.status_code == 200
    assert resp.json() == {"challenge": "abc"}


def test_spec_sheet_webhook_enqueues_spec_sheet_item_ready(client):
    """Right board + pulseId → SpecSheetItemReady event enqueued."""
    from engine.models import SpecSheetItemReady

    payload = {
        "event": {
            "boardId": PRODUCTION_SCHEDULE_BOARD,
            "pulseId": 12117039441,
            "userId": "operator-789",
            "type": "update_column_value",
        }
    }
    with patch("engine.routes.webhook.enqueue_event", new_callable=AsyncMock) as mock_enq:
        resp = client.post(SPEC_SHEET_PATH, json=payload)
    assert resp.status_code == 200
    assert resp.json() == {"status": "enqueued", "kind": "spec_sheet_item_ready"}
    mock_enq.assert_called_once()
    (event,) = mock_enq.call_args.args
    assert isinstance(event, SpecSheetItemReady)
    assert event.item_id == "12117039441"


def test_spec_sheet_webhook_ignores_wrong_board(client):
    """Triggers from boards other than Production Schedule are dropped —
    defense against stray Monday automations."""
    payload = {
        "event": {
            "boardId": 99999999,
            "pulseId": 12117039441,
            "userId": "operator-789",
        }
    }
    with patch("engine.routes.webhook.enqueue_event", new_callable=AsyncMock) as mock_enq:
        resp = client.post(SPEC_SHEET_PATH, json=payload)
    assert resp.status_code == 200
    assert resp.json()["kind"] == "wrong_board"
    mock_enq.assert_not_called()


def test_spec_sheet_webhook_no_pulse_id_ignored(client):
    payload = {"event": {"boardId": PRODUCTION_SCHEDULE_BOARD}}
    with patch("engine.routes.webhook.enqueue_event", new_callable=AsyncMock) as mock_enq:
        resp = client.post(SPEC_SHEET_PATH, json=payload)
    assert resp.status_code == 200
    assert resp.json()["kind"] == "no_pulse_id"
    mock_enq.assert_not_called()


def test_spec_sheet_webhook_drops_engine_echo(client):
    """If our own engine user triggered the change, suppress."""
    payload = {
        "event": {
            "boardId": PRODUCTION_SCHEDULE_BOARD,
            "pulseId": 12117039441,
            "userId": TEST_ENGINE_USER_ID,
        }
    }
    with patch("engine.routes.webhook.enqueue_event", new_callable=AsyncMock) as mock_enq:
        resp = client.post(SPEC_SHEET_PATH, json=payload)
    assert resp.status_code == 200
    assert resp.json()["kind"] == "echo"
    mock_enq.assert_not_called()
