"""Tests for /schedule.json + /view."""

from __future__ import annotations

from datetime import datetime
from unittest.mock import AsyncMock, patch
from zoneinfo import ZoneInfo

import pytest
from fastapi.testclient import TestClient

from engine.main import app
from engine.models import (
    Machine,
    MachineStatus,
    Priority,
    Slot,
    SlotStatus,
    Snapshot,
)

MDT = ZoneInfo("America/Denver")
NOW = datetime(2026, 5, 24, 23, 0, tzinfo=MDT)


def _machine(id: str, name: str, group: str = "Pressing", cap: float = 40000.0,
             status: MachineStatus = MachineStatus.ONLINE) -> Machine:
    return Machine(
        id=id, name=name, process_group=group, status=status,
        capacity_per_hour=cap, hours_per_day=24,
        working_window_start=0, working_window_end=24,
        changeover_minutes=30, dual_sided_only=False,
        max_job_size=None, force_route_condition=None,
        last_job_ended_at=None,
    )


def _slot(id: str, machine_id: str, status: SlotStatus = SlotStatus.QUEUED,
          drift: datetime | None = None) -> Slot:
    return Slot(
        id=id, name=f"slot {id}", job_reference_id="11801201557",
        machine_id=machine_id, stage_id="press",
        recipe_key="tablet-press-standard", recipe_version=1, quantity=50000,
        planned_start=NOW, planned_end=NOW,
        actual_start=None, actual_end=None,
        dependent_on_ids=(), status=status, manually_placed=False,
        priority=Priority.NORMAL, last_reflow_hash=None,
        drift_last_detected_at=drift,
    )


def _snap(machines, slots) -> Snapshot:
    return Snapshot(read_at=NOW, machines=tuple(machines), recipes=(), slots=tuple(slots))


def test_schedule_json_serializes_empty_snapshot():
    """No slots, machine list still surfaces."""
    snap = _snap([_machine("M1", "Gandalf")], [])
    with patch("engine.routes.view.read_snapshot", new_callable=AsyncMock) as m:
        m.return_value = snap
        with TestClient(app) as c:
            r = c.get("/schedule.json")
    assert r.status_code == 200
    data = r.json()
    assert data["machines"][0]["name"] == "Gandalf"
    assert data["machines"][0]["process_group"] == "Pressing"
    assert data["slots"] == []
    assert data["read_at"].startswith("2026-05-24T23:00:00")


def test_schedule_json_includes_drift_and_status():
    """Slot status + drift_last_detected_at round-trip into JSON."""
    snap = _snap(
        [_machine("M1", "Gandalf")],
        [
            _slot("S1", "M1", status=SlotStatus.RUNNING),
            _slot("S2", "M1", status=SlotStatus.QUEUED, drift=NOW),
        ],
    )
    with patch("engine.routes.view.read_snapshot", new_callable=AsyncMock) as m:
        m.return_value = snap
        with TestClient(app) as c:
            r = c.get("/schedule.json")
    data = r.json()
    by_id = {s["id"]: s for s in data["slots"]}
    assert by_id["S1"]["status"] == "Running"
    assert by_id["S1"]["drift_last_detected_at"] is None
    assert by_id["S2"]["status"] == "Queued"
    assert by_id["S2"]["drift_last_detected_at"].startswith("2026-05-24T23:00:00")


def test_view_serves_html():
    """/view is reachable and returns HTML containing the renderer."""
    with TestClient(app) as c:
        r = c.get("/view")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/html")
    # Sanity: it embeds the expected JS hook and the JSON endpoint path
    assert "/schedule.json" in r.text
    assert "Production Schedule" in r.text
