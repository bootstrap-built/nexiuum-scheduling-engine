"""Integration tests that hit live Monday boards.

Skipped unless GRAY_SPACE_MONDAY_TOKEN is set. Run with:
    source ~/.monday_tokens
    pytest tests/test_snapshot_integration.py -v
"""

from __future__ import annotations

import pytest

from engine.io.snapshot import read_snapshot
from engine.models import MachineStatus, RecipeStatus
from tests.conftest import has_real_monday_token

pytestmark = pytest.mark.skipif(
    not has_real_monday_token(),
    reason="requires real MONDAY_GRAYSPACE_TOKEN env var (source ~/.monday_tokens first)",
)


@pytest.mark.asyncio
async def test_snapshot_read_returns_seven_machines():
    """Capacity Engine should have 7 machines: 6 presses + Elphaba."""
    snapshot = await read_snapshot()
    machine_names = {m.name for m in snapshot.machines}
    expected = {
        "Gandalf the Gray",
        "Lancelot",
        "Houdini",
        "Merlin",
        "Penn & Teller",
        "Copperfield",
        "Elphaba",
    }
    assert expected == machine_names, f"Expected {expected}, got {machine_names}"


@pytest.mark.asyncio
async def test_snapshot_read_routing_flags_present():
    snapshot = await read_snapshot()
    by_name = {m.name: m for m in snapshot.machines}

    # Penn & Teller is the only dual-sided machine
    assert by_name["Penn & Teller"].dual_sided_only is True
    assert by_name["Gandalf the Gray"].dual_sided_only is False

    # Copperfield has the max_job_size cap
    assert by_name["Copperfield"].max_job_size == 10000

    # Lancelot has the force-route condition for high active
    assert by_name["Lancelot"].force_route_condition == "active_mg > 80"


@pytest.mark.asyncio
async def test_snapshot_read_merlin_is_down():
    """Merlin was set Down for repair during 1.5B1 setup."""
    snapshot = await read_snapshot()
    merlin = next(m for m in snapshot.machines if m.name == "Merlin")
    assert merlin.status == MachineStatus.DOWN


@pytest.mark.asyncio
async def test_snapshot_read_three_recipes():
    snapshot = await read_snapshot()
    by_key = {r.recipe_key: r for r in snapshot.recipes}
    assert "tablet-press-standard" in by_key
    assert "capsule-fill-standard" in by_key
    assert "clamshell-tablet" in by_key

    # The clamshell recipe should parse a 4-stage DAG
    clamshell = by_key["clamshell-tablet"]
    assert len(clamshell.stages) == 4
    stage_ids = {s.id for s in clamshell.stages}
    assert stage_ids == {"press", "blister", "lotcode", "clamshell"}

    # Status check
    assert by_key["tablet-press-standard"].status == RecipeStatus.ACTIVE
    assert by_key["clamshell-tablet"].status == RecipeStatus.DRAFT


# Removed: test_snapshot_read_test_slot_resolves_relations — the N3236 test
# slot was a 1.5B1 verification artifact and has been deleted. Connect-Boards
# parsing is now exercised by the apply_plan live smoke test instead.
