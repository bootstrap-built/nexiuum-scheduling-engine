"""Unit tests for the /simulate CTP handler.

All tests use mocked Snapshots — no Monday interaction. Integration coverage
(real /simulate request against live boards) is a separate concern requiring
a running engine + token; covered in a future session.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from engine.routes.simulate import (
    SIMULATE_JOB_ID,
    SimulateRequest,
    simulate_handler,
)
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


def _press_recipe() -> Recipe:
    return Recipe(
        id="R1",
        name="tablet-press-standard v1",
        recipe_key="tablet-press-standard",
        version=1,
        status=RecipeStatus.ACTIVE,
        stages=(RecipeStage(id="press", machine_class="Pressing", depends_on=()),),
    )


def _snap(machines: list[Machine], recipes: list[Recipe]) -> Snapshot:
    return Snapshot(
        read_at=NOW, machines=tuple(machines), recipes=tuple(recipes), slots=(),
    )


def _req(**overrides) -> SimulateRequest:
    defaults = {
        "recipe_key": "tablet-press-standard",
        "recipe_version": 1,
        "quantity": 100000,
    }
    defaults.update(overrides)
    return SimulateRequest(**defaults)


# ─── Happy path ──────────────────────────────────────────────────────────


def test_simulate_returns_projected_dates_and_binding_machine():
    snap = _snap([_machine("Gandalf the Gray")], [_press_recipe()])
    out = simulate_handler(_req(), snap, now=NOW)
    assert out.feasible is True
    # 100k tabs / 40k per hour = 2.5 hours
    assert out.projected_start == NOW
    assert out.projected_end == NOW + timedelta(hours=2.5)
    assert out.binding_machine_id == "Gandalf the Gray"
    assert out.binding_machine_name == "Gandalf the Gray"
    assert len(out.stages) == 1


def test_simulate_applies_20_percent_pad_by_default():
    snap = _snap([_machine("Gandalf the Gray")], [_press_recipe()])
    out = simulate_handler(_req(), snap, now=NOW)
    duration = out.projected_end - out.projected_start
    expected_pad = out.projected_end + duration * 0.20
    assert out.padded_end == expected_pad
    # 2.5hr * 0.20 = 0.5hr pad. projected_end + 30min.
    assert out.padded_end == NOW + timedelta(hours=3)


def test_simulate_custom_pad_factor():
    snap = _snap([_machine("Gandalf the Gray")], [_press_recipe()])
    out = simulate_handler(_req(pad_factor=0.0), snap, now=NOW)
    assert out.padded_end == out.projected_end


def test_simulate_does_not_write_real_job_id():
    """Sentinel ID confirms the simulation never mistakes itself for a real order."""
    snap = _snap([_machine("Gandalf the Gray")], [_press_recipe()])
    out = simulate_handler(_req(), snap, now=NOW)
    # The handler internally builds an order with SIMULATE_JOB_ID. The notes
    # field surfaces it (via plan_for_new_order's note string).
    assert any(SIMULATE_JOB_ID in n for n in out.notes)


# ─── Error paths surface as raised exceptions ────────────────────────────


def test_simulate_dangling_recipe_raises():
    """Recipe missing entirely → DanglingRecipeError propagates."""
    from engine.core.scheduler import DanglingRecipeError

    snap = _snap([_machine("Gandalf")], [])  # no recipes
    with pytest.raises(DanglingRecipeError):
        simulate_handler(_req(), snap, now=NOW)


def test_simulate_inactive_recipe_raises():
    """Recipe present but not Active → InactiveRecipeError."""
    from engine.core.scheduler import InactiveRecipeError

    draft = Recipe(
        id="R1", name="x", recipe_key="tablet-press-standard", version=1,
        status=RecipeStatus.DRAFT,
        stages=(RecipeStage(id="press", machine_class="Pressing", depends_on=()),),
    )
    snap = _snap([_machine("Gandalf")], [draft])
    with pytest.raises(InactiveRecipeError):
        simulate_handler(_req(), snap, now=NOW)


def test_simulate_unroutable_raises():
    """Stage can't be routed → UnroutableStageError with reason."""
    from engine.core.scheduler import UnroutableStageError

    snap = _snap(
        [_machine("Elphaba", process_group="Capsule")],
        [_press_recipe()],  # needs Pressing
    )
    with pytest.raises(UnroutableStageError) as exc_info:
        simulate_handler(_req(), snap, now=NOW)
    assert exc_info.value.reason == "no_machines_in_class"


# ─── Binding-machine selection across multi-stage DAGs ───────────────────


def test_simulate_picks_last_finishing_stage_as_binding():
    """For a multi-stage recipe, binding machine = the one finishing last."""
    clamshell = Recipe(
        id="R3", name="clamshell-tablet v1",
        recipe_key="clamshell-tablet", version=1,
        status=RecipeStatus.ACTIVE,
        stages=(
            RecipeStage(id="press", machine_class="Pressing", depends_on=()),
            RecipeStage(id="blister", machine_class="Blister", depends_on=("press",)),
            RecipeStage(id="lotcode", machine_class="Lot Coder", depends_on=("press",)),
            RecipeStage(id="clamshell", machine_class="Clamshell", depends_on=("blister", "lotcode")),
        ),
    )
    machines = [
        _machine("Gandalf the Gray", capacity=40000, process_group="Pressing"),
        _machine("Blister1", capacity=10000, process_group="Blister"),
        _machine("LotCoder1", capacity=20000, process_group="Lot Coder"),
        _machine("ClamshellMC", capacity=4000, process_group="Clamshell"),
    ]
    snap = _snap(machines, [clamshell])
    req = _req(recipe_key="clamshell-tablet", quantity=40000)
    out = simulate_handler(req, snap, now=NOW)
    # Final stage (clamshell) is always last in topo order; it should bind.
    assert out.binding_machine_name == "ClamshellMC"
    assert any(s.stage_id == "clamshell" for s in out.stages)


# ─── Pydantic validation ────────────────────────────────────────────────


def test_simulate_request_rejects_zero_quantity():
    with pytest.raises(ValueError):
        SimulateRequest(recipe_key="x", recipe_version=1, quantity=0)


def test_simulate_request_rejects_negative_quantity():
    with pytest.raises(ValueError):
        SimulateRequest(recipe_key="x", recipe_version=1, quantity=-10)


def test_simulate_request_rejects_out_of_range_pad():
    with pytest.raises(ValueError):
        SimulateRequest(recipe_key="x", recipe_version=1, quantity=100, pad_factor=1.5)
