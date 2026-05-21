"""HTTP tests for /webhook/monday — challenge handshake + event dispatch."""

from __future__ import annotations

import os
from unittest.mock import AsyncMock, patch

import pytest

from fastapi.testclient import TestClient

from engine.io.echo_guard import get_echo_guard
from engine.main import app


@pytest.fixture
def client():
    get_echo_guard().clear()
    with TestClient(app) as c:
        yield c


# ─── Challenge handshake ─────────────────────────────────────────────────


def test_webhook_returns_challenge_unchanged(client):
    """Monday webhook setup: POST {"challenge": "..."} → echo it back."""
    resp = client.post("/webhook/monday", json={"challenge": "ABC12345"})
    assert resp.status_code == 200
    assert resp.json() == {"challenge": "ABC12345"}


# ─── Event dispatch ──────────────────────────────────────────────────────


def test_webhook_capacity_engine_event_enqueues(client):
    """A column change on the Capacity Engine board enqueues a CapacityChanged event."""
    payload = {
        "event": {
            "boardId": 18413803163,  # Capacity Engine
            "pulseId": 12047953695,  # Gandalf
            "type": "update_column_value",
            "columnId": "color_mm3gcye0",
        }
    }
    # Patch in the route's namespace — submit_event is imported `from ... import submit_event`.
    with patch("engine.routes.webhook.submit_event", new_callable=AsyncMock) as mock_submit:
        mock_submit.return_value = None
        resp = client.post("/webhook/monday", json=payload)
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "enqueued"
    assert body["kind"] == "capacity_changed"


def test_webhook_schedule_event_dropped_if_engine_echo(client):
    """If the Schedule item's last_reflow_hash is in the echo guard, ignore."""
    # Pre-populate the guard with the hash the mock returns.
    get_echo_guard().remember("h-engine-origin")

    payload = {
        "event": {
            "boardId": 18413802995,  # Schedule
            "pulseId": 99999,
            "type": "update_column_value",
        }
    }

    fake_monday_response = {
        "items": [{"column_values": [{"id": "text_mm3hf0h5", "text": "h-engine-origin"}]}]
    }
    with patch("engine.routes.webhook.gray_space_client") as mock_factory:
        mock_client = AsyncMock()
        mock_client.query = AsyncMock(return_value=fake_monday_response)
        mock_factory.return_value.__aenter__.return_value = mock_client
        mock_factory.return_value.__aexit__.return_value = None

        resp = client.post("/webhook/monday", json=payload)

    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ignored"
    assert body["kind"] == "echo"


def test_webhook_schedule_event_processed_if_not_echo(client):
    """If the Schedule item's hash is NOT in the guard, it's an operator change."""
    payload = {
        "event": {
            "boardId": 18413802995,
            "pulseId": 99999,
            "type": "update_column_value",
        }
    }
    fake_monday_response = {
        "items": [{"column_values": [{"id": "text_mm3hf0h5", "text": "some-unknown-hash"}]}]
    }
    with patch("engine.routes.webhook.gray_space_client") as mock_factory:
        mock_client = AsyncMock()
        mock_client.query = AsyncMock(return_value=fake_monday_response)
        mock_factory.return_value.__aenter__.return_value = mock_client
        mock_factory.return_value.__aexit__.return_value = None

        resp = client.post("/webhook/monday", json=payload)

    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "received"
    assert body["kind"] == "schedule_change_unhandled"


def test_webhook_blend_records_event_acknowledged(client):
    """Blend Records events are acknowledged but not yet handled (E5)."""
    payload = {
        "event": {
            "boardId": 18404836849,  # Blend Records
            "pulseId": 11801201557,
            "type": "change_status_column_value",
        }
    }
    resp = client.post("/webhook/monday", json=payload)
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "received"
    assert body["kind"] == "blend_records_unhandled"


def test_webhook_unrecognized_source_acknowledged(client):
    """Events from unknown boards are acknowledged so Monday doesn't retry."""
    payload = {"event": {"boardId": 12345, "pulseId": 67, "type": "update_column_value"}}
    resp = client.post("/webhook/monday", json=payload)
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "received"
    assert body["kind"] == "unrecognized_source"
