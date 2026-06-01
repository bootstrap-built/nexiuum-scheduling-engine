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
          drift: datetime | None = None, n_number: str | None = None,
          flavor: str | None = None,
          job_reference_id: str = "11801201557") -> Slot:
    return Slot(
        id=id, name=f"slot {id}", job_reference_id=job_reference_id,
        machine_id=machine_id, stage_id="press",
        recipe_key="tablet-press-standard", recipe_version=1, quantity=50000,
        planned_start=NOW, planned_end=NOW,
        actual_start=None, actual_end=None,
        dependent_on_ids=(), status=status, manually_placed=False,
        priority=Priority.NORMAL, last_reflow_hash=None,
        drift_last_detected_at=drift, n_number=n_number, flavor=flavor,
    )


def _snap(machines, slots) -> Snapshot:
    return Snapshot(read_at=NOW, machines=tuple(machines), recipes=(), slots=tuple(slots))


def test_schedule_json_serializes_empty_snapshot():
    """No slots, machine list still surfaces."""
    snap = _snap([_machine("M1", "Gandalf")], [])
    with patch("engine.routes.view.read_snapshot", new_callable=AsyncMock) as m, \
         patch("engine.routes.view._fetch_blend_enrichment", new_callable=AsyncMock) as fe:
        m.return_value = snap
        fe.return_value = {}
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
    with patch("engine.routes.view.read_snapshot", new_callable=AsyncMock) as m, \
         patch("engine.routes.view._fetch_blend_enrichment", new_callable=AsyncMock) as fe:
        m.return_value = snap
        fe.return_value = {}
        with TestClient(app) as c:
            r = c.get("/schedule.json")
    data = r.json()
    by_id = {s["id"]: s for s in data["slots"]}
    assert by_id["S1"]["status"] == "Running"
    assert by_id["S1"]["drift_last_detected_at"] is None
    assert by_id["S2"]["status"] == "Queued"
    assert by_id["S2"]["drift_last_detected_at"].startswith("2026-05-24T23:00:00")


def test_schedule_json_merges_blend_enrichment():
    """job_label / job_name / job_client / job_active land on each slot,
    keyed by job_reference_id. Slots without matching enrichment get nulls
    rather than being dropped."""
    snap = _snap(
        [_machine("M1", "Gandalf")],
        [
            _slot("S1", "M1"),          # job_reference_id = "11801201557"
            Slot(
                id="S2", name="orphan", job_reference_id="99999",
                machine_id="M1", stage_id="press",
                recipe_key="x", recipe_version=1, quantity=10,
                planned_start=NOW, planned_end=NOW,
                actual_start=None, actual_end=None,
                dependent_on_ids=(), status=SlotStatus.QUEUED,
                manually_placed=False, priority=Priority.NORMAL,
                last_reflow_hash=None, drift_last_detected_at=None,
            ),
        ],
    )
    enrich = {
        "11801201557": {
            "job_label": "N3236",
            "job_name": "N3236 - ROAR LLC",
            "job_client": "ROAR LLC",
            "job_active": "7OH 85mg",
        },
    }
    with patch("engine.routes.view.read_snapshot", new_callable=AsyncMock) as m, \
         patch("engine.routes.view._fetch_blend_enrichment", new_callable=AsyncMock) as fe:
        m.return_value = snap
        fe.return_value = enrich
        with TestClient(app) as c:
            r = c.get("/schedule.json")
    by_id = {s["id"]: s for s in r.json()["slots"]}
    assert by_id["S1"]["job_label"] == "N3236"
    assert by_id["S1"]["job_client"] == "ROAR LLC"
    assert by_id["S1"]["job_active"] == "7OH 85mg"
    # Orphan slot: enrichment fields present but null
    assert by_id["S2"]["job_label"] is None
    assert by_id["S2"]["job_client"] is None


def test_schedule_json_includes_n_number_and_lane_label():
    """Every slot object carries n_number, plus a composed lane_label:
    - Slot's own N# wins (Phase 2D flow).
    - Legacy GS (n_number=None) falls back to the Blend Records job_label.
    - Neither → '#<last-6-of-slot-id>'.
    """
    snap = _snap(
        [_machine("M1", "Gandalf")],
        [
            _slot("S1", "M1", n_number="N3629"),            # Phase 2D
            _slot("S2", "M1", job_reference_id="55501"),    # legacy, enriched
            _slot("990000777", "M1", job_reference_id="00"),  # no n#, no enrich
        ],
    )
    enrich = {"55501": {"job_label": "N1234", "job_name": None,
                        "job_client": None, "job_active": None}}
    with patch("engine.routes.view.read_snapshot", new_callable=AsyncMock) as m, \
         patch("engine.routes.view._fetch_blend_enrichment", new_callable=AsyncMock) as fe:
        m.return_value = snap
        fe.return_value = enrich
        with TestClient(app) as c:
            r = c.get("/schedule.json")
    by_id = {s["id"]: s for s in r.json()["slots"]}
    # n_number surfaced verbatim (None when the slot has none).
    assert by_id["S1"]["n_number"] == "N3629"
    assert by_id["S2"]["n_number"] is None
    # lane_label composed via the labels module.
    assert by_id["S1"]["lane_label"] == "N3629"          # own N# wins
    assert by_id["S2"]["lane_label"] == "N1234"          # falls back to job_label
    assert by_id["990000777"]["lane_label"] == "#000777"  # last-6 of slot id


def test_schedule_json_includes_flavor_and_composes_lane_label():
    """Every slot carries `flavor`, and the lane_label folds it in after the
    N# (the Phase 2D '<N#> · <Flavor>' identity). Legacy slots without a
    flavor are unchanged from the #4 N#-only behaviour.
    """
    snap = _snap(
        [_machine("M1", "Gandalf")],
        [
            _slot("S1", "M1", n_number="N3629", flavor="Strawberry Banana"),
            _slot("S2", "M1", n_number="N777"),  # no flavor → N#-only
        ],
    )
    with patch("engine.routes.view.read_snapshot", new_callable=AsyncMock) as m, \
         patch("engine.routes.view._fetch_blend_enrichment", new_callable=AsyncMock) as fe:
        m.return_value = snap
        fe.return_value = {}
        with TestClient(app) as c:
            r = c.get("/schedule.json")
    by_id = {s["id"]: s for s in r.json()["slots"]}
    # flavor surfaced verbatim (None when absent).
    assert by_id["S1"]["flavor"] == "Strawberry Banana"
    assert by_id["S2"]["flavor"] is None
    # lane_label composes the full identity, untruncated.
    assert by_id["S1"]["lane_label"] == "N3629 · Strawberry Banana"
    assert by_id["S2"]["lane_label"] == "N777"  # N#-only, unchanged


def test_view_serves_html():
    """/view is reachable and returns HTML containing the renderer."""
    with TestClient(app) as c:
        r = c.get("/view")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/html")
    # Sanity: it embeds the expected JS hook and the JSON endpoint path
    assert "/schedule.json" in r.text
    assert "Production Schedule" in r.text


def test_view_escapes_user_data_in_popover():
    """The popover renders flavor (free text from the spec-sheet form) and the
    composed title into innerHTML — they must be HTML-escaped, or a flavor like
    `<img onerror=...>` would be stored XSS on this internal dashboard. Guards
    against the esc() wrapping being removed.
    """
    with TestClient(app) as c:
        r = c.get("/view")
    # The escape helper ships, and the untrusted fields are wrapped in it.
    assert "function esc(" in r.text
    assert "esc(s.flavor)" in r.text
    assert "esc(title)" in r.text
    # Machine name also reaches innerHTML (the lane label row) — escaped too.
    assert "esc(m.name)" in r.text
