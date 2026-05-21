"""HTTP-layer tests for /simulate using FastAPI TestClient.

Validates Pydantic serialization, error-to-HTTP-status mapping, and the
dependency-injection plumbing. No Monday interaction — both `snapshot` and
`now` are overridden via `app.dependency_overrides`.
"""

from __future__ import annotations

import os
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

# Set a dummy token before importing the app so pydantic-settings doesn't
# choke on the missing env var. Real Monday calls are mocked out via
# dependency overrides so the dummy token is never used.
os.environ.setdefault("MONDAY_GRAYSPACE_TOKEN", "test-token-not-real")

from fastapi.testclient import TestClient  # noqa: E402

from engine.main import app  # noqa: E402
from engine.models import (  # noqa: E402
    Machine,
    MachineStatus,
    Recipe,
    RecipeStage,
    RecipeStatus,
    Snapshot,
)
from engine.routes.simulate import (  # noqa: E402
    get_current_snapshot,
    get_current_time,
)

TZ = ZoneInfo("America/Denver")
NOW = datetime(2026, 5, 21, 8, 0, 0, tzinfo=TZ)


def _machine(name: str, capacity: float = 40000, process_group: str = "Pressing") -> Machine:
    return Machine(
        id=name,
        name=name,
        process_group=process_group,  # type: ignore[arg-type]
        status=MachineStatus.ONLINE,
        capacity_per_hour=capacity,
        hours_per_day=16,
        working_window_start=6,
        working_window_end=22,
        changeover_minutes=30,
        dual_sided_only=False,
        max_job_size=None,
        force_route_condition=None,
        last_job_ended_at=None,
    )


def _press_recipe(status: RecipeStatus = RecipeStatus.ACTIVE) -> Recipe:
    return Recipe(
        id="R1",
        name="tablet-press-standard v1",
        recipe_key="tablet-press-standard",
        version=1,
        status=status,
        stages=(RecipeStage(id="press", machine_class="Pressing", depends_on=()),),
    )


def _snap(machines: list[Machine], recipes: list[Recipe]) -> Snapshot:
    return Snapshot(
        read_at=NOW, machines=tuple(machines), recipes=tuple(recipes), slots=(),
    )


@pytest.fixture
def client():
    """Yield a TestClient with snapshot+now mocked. Reset overrides after each test."""

    def fake_snapshot():
        return _snap([_machine("Gandalf the Gray")], [_press_recipe()])

    def fake_now():
        return NOW

    app.dependency_overrides[get_current_snapshot] = fake_snapshot
    app.dependency_overrides[get_current_time] = fake_now
    try:
        yield TestClient(app)
    finally:
        app.dependency_overrides.clear()


# ─── Happy path ──────────────────────────────────────────────────────────


def test_simulate_post_returns_200_with_projected_dates(client):
    resp = client.post(
        "/simulate",
        json={
            "recipe_key": "tablet-press-standard",
            "recipe_version": 1,
            "quantity": 100000,
        },
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["feasible"] is True
    assert body["binding_machine_name"] == "Gandalf the Gray"
    assert body["projected_start"].startswith("2026-05-21T08:00")
    # 100k / 40k = 2.5hr → end = 10:30
    assert body["projected_end"].startswith("2026-05-21T10:30")
    # padded_end with 20% pad → 11:00
    assert body["padded_end"].startswith("2026-05-21T11:00")
    assert len(body["stages"]) == 1
    assert body["stages"][0]["stage_id"] == "press"


def test_simulate_post_health_route_still_works(client):
    """Sanity — the new router didn't break /health."""
    resp = client.get("/health")
    assert resp.status_code == 200
    assert resp.json()["status"] == "ok"


# ─── Validation errors return 422 (Pydantic) ─────────────────────────────


def test_simulate_post_rejects_zero_quantity(client):
    resp = client.post(
        "/simulate",
        json={"recipe_key": "x", "recipe_version": 1, "quantity": 0},
    )
    assert resp.status_code == 422


def test_simulate_post_rejects_missing_recipe_key(client):
    resp = client.post(
        "/simulate",
        json={"recipe_version": 1, "quantity": 100},
    )
    assert resp.status_code == 422


# ─── Domain errors map to HTTP 400 with structured payload ───────────────


def test_simulate_post_dangling_recipe_returns_400(client):
    """Override snapshot to one with no recipes."""

    def empty_recipes():
        return _snap([_machine("Gandalf the Gray")], [])

    app.dependency_overrides[get_current_snapshot] = empty_recipes
    resp = client.post(
        "/simulate",
        json={"recipe_key": "tablet-press-standard", "recipe_version": 1, "quantity": 100000},
    )
    assert resp.status_code == 400
    body = resp.json()["detail"]
    assert body["feasible"] is False
    assert body["error_kind"] == "DanglingRecipe"
    assert body["recipe_key"] == "tablet-press-standard"


def test_simulate_post_inactive_recipe_returns_400(client):
    def draft_recipe():
        return _snap([_machine("Gandalf the Gray")], [_press_recipe(RecipeStatus.DRAFT)])

    app.dependency_overrides[get_current_snapshot] = draft_recipe
    resp = client.post(
        "/simulate",
        json={"recipe_key": "tablet-press-standard", "recipe_version": 1, "quantity": 100000},
    )
    assert resp.status_code == 400
    body = resp.json()["detail"]
    assert body["error_kind"] == "InactiveRecipe"
    assert body["recipe_status"] == "Draft"


def test_simulate_post_unroutable_stage_returns_400(client):
    def no_pressing_machines():
        return _snap(
            [_machine("Elphaba", process_group="Capsule")],
            [_press_recipe()],
        )

    app.dependency_overrides[get_current_snapshot] = no_pressing_machines
    resp = client.post(
        "/simulate",
        json={"recipe_key": "tablet-press-standard", "recipe_version": 1, "quantity": 100000},
    )
    assert resp.status_code == 400
    body = resp.json()["detail"]
    assert body["error_kind"] == "UnroutableStage"
    assert body["unroutable_reason"] == "no_machines_in_class"


# ─── Custom pad_factor propagates through HTTP ───────────────────────────


def test_simulate_post_zero_pad(client):
    resp = client.post(
        "/simulate",
        json={
            "recipe_key": "tablet-press-standard",
            "recipe_version": 1,
            "quantity": 100000,
            "pad_factor": 0.0,
        },
    )
    assert resp.status_code == 200
    body = resp.json()
    # No pad: padded_end == projected_end
    assert body["padded_end"] == body["projected_end"]
