"""Phase 2 — dual-instance snapshot + settings validation.

Covers the read path that merges Capacity Engine + Process Recipe items
from BOTH Monday accounts (Gray Space + Nexiuum) with an `instance` tag,
plus the all-or-nothing settings validator and per-instance auth header.

Schedule reads stay on Gray Space this phase — verified explicitly so the
later schedule-migration phase doesn't accidentally regress.
"""

from __future__ import annotations

import json
import os
from unittest.mock import AsyncMock, patch

import httpx
import pytest

from engine.config import Settings, get_settings, reset_settings_for_tests
from engine.io.monday import MondayClient
from engine.io.snapshot import _parse_machine, _parse_recipe, _parse_slot, read_snapshot
from engine.models import MachineStatus, Snapshot


# ─────────────────────────────────────────────────────────────────────────
# Settings validator — partial Nexiuum config must fail
# ─────────────────────────────────────────────────────────────────────────


def test_settings_token_alone_is_phase_1_mode(monkeypatch: pytest.MonkeyPatch) -> None:
    """Token in env without board IDs = valid Phase 1 config.

    The token comes from `~/.monday_tokens` on Josh's shell — its presence
    in env is NOT a Phase 2 opt-in signal. Board IDs (non-zero) are.
    """
    monkeypatch.setenv("MONDAY_NEXIUUM_TOKEN", "fake-token-from-shell")
    monkeypatch.delenv("NEXIUUM_CAPACITY_ENGINE_BOARD", raising=False)
    monkeypatch.delenv("NEXIUUM_PROCESS_RECIPE_BOARD", raising=False)
    reset_settings_for_tests()
    s = Settings()  # type: ignore[call-arg]
    assert s.nexiuum_enabled is False  # token alone doesn't enable


def test_settings_rejects_partial_board_config(monkeypatch: pytest.MonkeyPatch) -> None:
    """Some Nexiuum board IDs set but not all three = partial config, must raise."""
    monkeypatch.setenv("MONDAY_NEXIUUM_TOKEN", "fake-token")
    monkeypatch.setenv("NEXIUUM_CAPACITY_ENGINE_BOARD", "12345")
    monkeypatch.setenv("NEXIUUM_PROCESS_RECIPE_BOARD", "67890")
    monkeypatch.delenv("NEXIUUM_SCHEDULE_BOARD", raising=False)
    reset_settings_for_tests()
    with pytest.raises(Exception) as excinfo:
        Settings()  # type: ignore[call-arg]
    assert "Partial Nexiuum board config" in str(excinfo.value)


def test_settings_rejects_boards_set_without_token(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """All three Nexiuum boards set without a token = error.

    You can't actually read those boards without auth, so fail loud.
    """
    monkeypatch.setenv("MONDAY_NEXIUUM_TOKEN", "")
    monkeypatch.setenv("NEXIUUM_CAPACITY_ENGINE_BOARD", "12345")
    monkeypatch.setenv("NEXIUUM_PROCESS_RECIPE_BOARD", "67890")
    monkeypatch.setenv("NEXIUUM_SCHEDULE_BOARD", "11111")
    reset_settings_for_tests()
    with pytest.raises(Exception) as excinfo:
        Settings()  # type: ignore[call-arg]
    assert "MONDAY_NEXIUUM_TOKEN is empty" in str(excinfo.value)


def test_settings_accepts_full_nexiuum_config(monkeypatch: pytest.MonkeyPatch) -> None:
    """All four Nexiuum fields populated → valid, nexiuum_enabled True."""
    monkeypatch.setenv("MONDAY_NEXIUUM_TOKEN", "fake-nexiuum-token")
    monkeypatch.setenv("NEXIUUM_CAPACITY_ENGINE_BOARD", "111")
    monkeypatch.setenv("NEXIUUM_PROCESS_RECIPE_BOARD", "222")
    monkeypatch.setenv("NEXIUUM_SCHEDULE_BOARD", "333")
    reset_settings_for_tests()
    s = Settings()  # type: ignore[call-arg]
    assert s.nexiuum_enabled is True
    assert s.nexiuum_capacity_engine_board == 111
    assert s.nexiuum_process_recipe_board == 222
    assert s.nexiuum_schedule_board == 333


def test_settings_accepts_no_nexiuum_config(monkeypatch: pytest.MonkeyPatch) -> None:
    """All Nexiuum fields blank → valid Phase 1 config, nexiuum_enabled False."""
    monkeypatch.setenv("MONDAY_NEXIUUM_TOKEN", "")
    monkeypatch.delenv("NEXIUUM_CAPACITY_ENGINE_BOARD", raising=False)
    monkeypatch.delenv("NEXIUUM_PROCESS_RECIPE_BOARD", raising=False)
    monkeypatch.delenv("NEXIUUM_SCHEDULE_BOARD", raising=False)
    reset_settings_for_tests()
    s = Settings()  # type: ignore[call-arg]
    assert s.nexiuum_enabled is False


# ─────────────────────────────────────────────────────────────────────────
# Parser — uses per-instance column IDs (regression test, 2026-05-25)
# ─────────────────────────────────────────────────────────────────────────
#
# The Track B refactor silently parsed Nexiuum items with Gray Space column
# IDs, producing machines with capacity=0 and process_group=None. The new
# tests didn't catch it because they used items with empty column_values.
# These tests pin the per-instance column-id wiring against real column IDs.


def test_parse_machine_uses_nexiuum_columns_for_nexiuum_instance(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A Nexiuum machine item must be parsed with Nexiuum column IDs.

    Regression for the Track B silent parser bug. We construct a Monday item
    payload using Nexiuum column IDs and assert the parser reads the
    Capacity field. If the parser used Gray Space column IDs (the bug),
    capacity would default to 0.0.
    """
    monkeypatch.setenv("MONDAY_NEXIUUM_TOKEN", "x")
    monkeypatch.setenv("NEXIUUM_CAPACITY_ENGINE_BOARD", "1")
    monkeypatch.setenv("NEXIUUM_PROCESS_RECIPE_BOARD", "2")
    monkeypatch.setenv("NEXIUUM_SCHEDULE_BOARD", "3")
    reset_settings_for_tests()
    s = get_settings()

    cols = s.cap_cols("nexiuum")
    item = {
        "id": "nx-machine-1",
        "name": "Sachet-1",
        "column_values": [
            {"id": cols.capacity, "text": "1750"},
            {"id": cols.process_group, "text": "Sachet"},
            {"id": cols.status, "text": "Online"},
        ],
    }
    machine = _parse_machine(item, s, instance="nexiuum")
    assert machine.name == "Sachet-1"
    assert machine.capacity_per_hour == 1750.0  # would be 0.0 with the bug
    assert machine.process_group == "Sachet"  # would be None with the bug
    assert machine.status == MachineStatus.ONLINE
    assert machine.instance == "nexiuum"


def test_parse_machine_uses_gray_space_columns_for_gray_space_instance(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Gray Space items continue to parse with Gray Space column IDs.

    The two instances have different column IDs — parser must not cross
    the streams in either direction.
    """
    monkeypatch.setenv("MONDAY_NEXIUUM_TOKEN", "")
    monkeypatch.delenv("NEXIUUM_CAPACITY_ENGINE_BOARD", raising=False)
    monkeypatch.delenv("NEXIUUM_PROCESS_RECIPE_BOARD", raising=False)
    monkeypatch.delenv("NEXIUUM_SCHEDULE_BOARD", raising=False)
    reset_settings_for_tests()
    s = get_settings()

    cols = s.cap_cols("gray_space")
    item = {
        "id": "gs-machine-1",
        "name": "Gandalf",
        "column_values": [
            {"id": cols.capacity, "text": "40000"},
            {"id": cols.process_group, "text": "Pressing"},
            {"id": cols.status, "text": "Online"},
        ],
    }
    machine = _parse_machine(item, s, instance="gray_space")
    assert machine.capacity_per_hour == 40000.0
    assert machine.process_group == "Pressing"
    assert machine.instance == "gray_space"


def test_parse_machine_gray_space_columns_do_not_apply_to_nexiuum_items(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If a Nexiuum item carries only Gray Space column IDs (the bug
    scenario), the parser must NOT extract values — confirms separation."""
    monkeypatch.setenv("MONDAY_NEXIUUM_TOKEN", "x")
    monkeypatch.setenv("NEXIUUM_CAPACITY_ENGINE_BOARD", "1")
    monkeypatch.setenv("NEXIUUM_PROCESS_RECIPE_BOARD", "2")
    monkeypatch.setenv("NEXIUUM_SCHEDULE_BOARD", "3")
    reset_settings_for_tests()
    s = get_settings()

    # Build an item with Gray Space column IDs but pass it through the
    # Nexiuum parser. Result should be empty/default fields.
    gs_cols = s.cap_cols("gray_space")
    item_with_gs_cols = {
        "id": "wrong",
        "name": "Wrong",
        "column_values": [
            {"id": gs_cols.capacity, "text": "9999"},
            {"id": gs_cols.process_group, "text": "Pressing"},
        ],
    }
    machine = _parse_machine(item_with_gs_cols, s, instance="nexiuum")
    # Nexiuum parser uses Nexiuum column IDs, so the Gray Space values are
    # invisible. Capacity defaults to 0.0, process_group to None.
    assert machine.capacity_per_hour == 0.0
    assert machine.process_group is None


def test_parse_slot_uses_nexiuum_columns_for_nexiuum_instance(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A Nexiuum slot must be parsed with Nexiuum Schedule column IDs."""
    monkeypatch.setenv("MONDAY_NEXIUUM_TOKEN", "x")
    monkeypatch.setenv("NEXIUUM_CAPACITY_ENGINE_BOARD", "1")
    monkeypatch.setenv("NEXIUUM_PROCESS_RECIPE_BOARD", "2")
    monkeypatch.setenv("NEXIUUM_SCHEDULE_BOARD", "3")
    reset_settings_for_tests()
    s = get_settings()

    cols = s.schedule_cols("nexiuum")
    item = {
        "id": "nx-slot-1",
        "name": "Order-1234 → Sachet-1",
        "column_values": [
            {"id": cols.quantity, "text": "5000"},
            {"id": cols.stage_id, "text": "sachet"},
            {"id": cols.recipe_key, "text": "tablet-pouch"},
        ],
    }
    slot = _parse_slot(item, s, instance="nexiuum")
    assert slot.quantity == 5000
    assert slot.stage_id == "sachet"
    assert slot.recipe_key == "tablet-pouch"
    assert slot.instance == "nexiuum"


# ─────────────────────────────────────────────────────────────────────────
# Snapshot — single-instance behavior unchanged
# ─────────────────────────────────────────────────────────────────────────


async def test_snapshot_single_instance_unchanged(monkeypatch: pytest.MonkeyPatch) -> None:
    """With only Gray Space configured, read_snapshot opens exactly one
    client and only reads Gray Space boards. The Nexiuum-side fetch must
    never run.

    Verified by patching gray_space_client + nexiuum_client and asserting
    nexiuum_client is NOT entered.
    """
    monkeypatch.setenv("MONDAY_NEXIUUM_TOKEN", "")
    monkeypatch.delenv("NEXIUUM_CAPACITY_ENGINE_BOARD", raising=False)
    monkeypatch.delenv("NEXIUUM_PROCESS_RECIPE_BOARD", raising=False)
    monkeypatch.delenv("NEXIUUM_SCHEDULE_BOARD", raising=False)
    reset_settings_for_tests()

    fake_client = AsyncMock(spec=MondayClient)
    fake_client.fetch_board_items = AsyncMock(return_value=[])
    fake_client.__aenter__ = AsyncMock(return_value=fake_client)
    fake_client.__aexit__ = AsyncMock(return_value=None)

    def gs_factory() -> MondayClient:
        return fake_client

    nexiuum_factory_called = False

    def nx_factory() -> MondayClient:
        nonlocal nexiuum_factory_called
        nexiuum_factory_called = True
        raise AssertionError("nexiuum_client() must not be called when disabled")

    with (
        patch("engine.io.monday.gray_space_client", side_effect=gs_factory),
        patch("engine.io.monday.nexiuum_client", side_effect=nx_factory),
    ):
        snap = await read_snapshot()

    assert nexiuum_factory_called is False
    assert isinstance(snap, Snapshot)
    # Exactly 3 board reads (cap engine, recipes, schedule) — Gray Space only.
    assert fake_client.fetch_board_items.call_count == 3


# ─────────────────────────────────────────────────────────────────────────
# Snapshot — dual-instance merges Capacity Engine + recipes, schedule stays
# on Gray Space only
# ─────────────────────────────────────────────────────────────────────────


def _machine_item(item_id: str, name: str) -> dict:
    """Minimal raw Monday item shape for a Capacity Engine row.

    All column values absent → defaults kick in (Online machine, 0 capacity,
    etc.) which is fine — we only care about which board the row came from.
    """
    return {"id": item_id, "name": name, "column_values": []}


def _recipe_item(item_id: str, name: str) -> dict:
    """Minimal raw Monday item shape for a Process Recipe row."""
    return {"id": item_id, "name": name, "column_values": []}


async def test_snapshot_dual_instance_merges_capacity_engines(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With both instances configured, read_snapshot returns a unified list
    of machines + recipes, each tagged with its source instance. Schedule
    stays Gray-Space-only (intentional this phase)."""
    monkeypatch.setenv("MONDAY_NEXIUUM_TOKEN", "fake-nexiuum-token")
    monkeypatch.setenv("NEXIUUM_CAPACITY_ENGINE_BOARD", "999111")
    monkeypatch.setenv("NEXIUUM_PROCESS_RECIPE_BOARD", "999222")
    monkeypatch.setenv("NEXIUUM_SCHEDULE_BOARD", "999333")
    reset_settings_for_tests()
    s = get_settings()
    assert s.nexiuum_enabled is True

    # Gray Space returns one machine, one recipe, no slots.
    gs_client = AsyncMock(spec=MondayClient)
    gs_client.__aenter__ = AsyncMock(return_value=gs_client)
    gs_client.__aexit__ = AsyncMock(return_value=None)

    async def gs_fetch(board_id: int, *, page_size: int = 500) -> list[dict]:
        if board_id == s.gray_space_capacity_engine_board:
            return [_machine_item("gs-m1", "Gandalf")]
        if board_id == s.gray_space_process_recipe_board:
            return [_recipe_item("gs-r1", "tablet-press")]
        if board_id == s.gray_space_schedule_board:
            return []
        raise AssertionError(f"unexpected Gray Space board fetch: {board_id}")

    gs_client.fetch_board_items = AsyncMock(side_effect=gs_fetch)

    # Nexiuum returns one machine, one recipe.
    nx_client = AsyncMock(spec=MondayClient)
    nx_client.__aenter__ = AsyncMock(return_value=nx_client)
    nx_client.__aexit__ = AsyncMock(return_value=None)

    async def nx_fetch(board_id: int, *, page_size: int = 500) -> list[dict]:
        if board_id == s.nexiuum_capacity_engine_board:
            return [_machine_item("nx-m1", "NexiPress-1")]
        if board_id == s.nexiuum_process_recipe_board:
            return [_recipe_item("nx-r1", "capsule-fill-nexi")]
        if board_id == s.nexiuum_schedule_board:
            return []  # Phase 2B: Nexiuum Schedule is read but starts empty
        raise AssertionError(f"unexpected Nexiuum board fetch: {board_id}")

    nx_client.fetch_board_items = AsyncMock(side_effect=nx_fetch)

    with (
        patch("engine.io.monday.gray_space_client", return_value=gs_client),
        patch("engine.io.monday.nexiuum_client", return_value=nx_client),
    ):
        snap = await read_snapshot()

    # Merged: machines + recipes + slots from both instances.
    machine_ids = {(m.id, m.instance) for m in snap.machines}
    recipe_ids = {(r.id, r.instance) for r in snap.recipes}

    assert machine_ids == {("gs-m1", "gray_space"), ("nx-m1", "nexiuum")}
    assert recipe_ids == {("gs-r1", "gray_space"), ("nx-r1", "nexiuum")}

    # 3 board reads each instance now (cap engine, recipes, schedule).
    assert gs_client.fetch_board_items.call_count == 3
    assert nx_client.fetch_board_items.call_count == 3


# ─────────────────────────────────────────────────────────────────────────
# MondayClient — correct token per instance
# ─────────────────────────────────────────────────────────────────────────


# ─────────────────────────────────────────────────────────────────────────
# apply_plan — cross-instance write routing (Phase 2B Stage 3)
# ─────────────────────────────────────────────────────────────────────────


async def test_apply_plan_routes_nexiuum_writes_to_nexiuum_board(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A SlotWrite with instance='nexiuum' must be sent to the Nexiuum
    Schedule board using Nexiuum column IDs via the Nexiuum client."""
    from engine.io.apply import apply_plan
    from engine.models import Plan, SlotStatus, SlotWrite

    monkeypatch.setenv("MONDAY_GRAYSPACE_TOKEN", "gs-tok")
    monkeypatch.setenv("MONDAY_NEXIUUM_TOKEN", "nx-tok")
    monkeypatch.setenv("NEXIUUM_CAPACITY_ENGINE_BOARD", "111")
    monkeypatch.setenv("NEXIUUM_PROCESS_RECIPE_BOARD", "222")
    monkeypatch.setenv("NEXIUUM_SCHEDULE_BOARD", "12345")
    reset_settings_for_tests()
    s = get_settings()

    captured: dict[str, list] = {"gs": [], "nx": []}

    gs_client = AsyncMock(spec=MondayClient)
    gs_client.__aenter__ = AsyncMock(return_value=gs_client)
    gs_client.__aexit__ = AsyncMock(return_value=None)
    async def gs_query(mutation, variables=None):
        captured["gs"].append((mutation, variables))
        return ({}, [])
    gs_client.query_collecting_errors = AsyncMock(side_effect=gs_query)

    nx_client = AsyncMock(spec=MondayClient)
    nx_client.__aenter__ = AsyncMock(return_value=nx_client)
    nx_client.__aexit__ = AsyncMock(return_value=None)
    async def nx_query(mutation, variables=None):
        captured["nx"].append((mutation, variables))
        return ({"w0": {"id": "9999"}}, [])
    nx_client.query_collecting_errors = AsyncMock(side_effect=nx_query)

    plan = Plan(slot_writes=(
        SlotWrite(
            slot_id=None,
            name="N1234 → Sachet-1",
            machine_id="50001",
            stage_id="sachet",
            quantity=1000,
            status=SlotStatus.QUEUED,
            instance="nexiuum",
        ),
    ))

    with (
        patch("engine.io.apply.gray_space_client", return_value=gs_client),
        patch("engine.io.apply.nexiuum_client", return_value=nx_client),
    ):
        result = await apply_plan(plan)

    # Gray Space client never touched (no Gray Space writes in the plan)
    assert captured["gs"] == []
    # Nexiuum client got the mutation
    assert len(captured["nx"]) == 1
    mutation, variables = captured["nx"][0]
    # Mutation targets the Nexiuum board id, not the Gray Space one
    assert str(s.nexiuum_schedule_board) in mutation
    assert str(s.gray_space_schedule_board) not in mutation
    # column_values use Nexiuum column IDs (e.g., col_nx_schedule_quantity)
    cv_json = json.loads(variables["cv_0"])
    nx_cols = s.schedule_cols("nexiuum")
    assert nx_cols.quantity in cv_json
    assert nx_cols.status in cv_json
    # And NOT Gray Space column IDs
    gs_cols = s.schedule_cols("gray_space")
    assert gs_cols.quantity != nx_cols.quantity  # sanity — they really differ
    assert gs_cols.quantity not in cv_json
    # Result reflects the Nexiuum-side create
    assert result.created_slot_ids == ["9999"]
    assert not result.errors


async def test_apply_plan_splits_mixed_plan_gs_first(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A plan with writes for both instances fires two mutations, Gray
    Space first (so press-stage IDs exist before packaging-stage dependency
    backfills land in Phase 2C)."""
    from engine.io.apply import apply_plan
    from engine.models import Plan, SlotStatus, SlotWrite

    monkeypatch.setenv("MONDAY_GRAYSPACE_TOKEN", "gs-tok")
    monkeypatch.setenv("MONDAY_NEXIUUM_TOKEN", "nx-tok")
    monkeypatch.setenv("NEXIUUM_CAPACITY_ENGINE_BOARD", "111")
    monkeypatch.setenv("NEXIUUM_PROCESS_RECIPE_BOARD", "222")
    monkeypatch.setenv("NEXIUUM_SCHEDULE_BOARD", "12345")
    reset_settings_for_tests()

    call_order: list[str] = []

    gs_client = AsyncMock(spec=MondayClient)
    gs_client.__aenter__ = AsyncMock(return_value=gs_client)
    gs_client.__aexit__ = AsyncMock(return_value=None)
    async def gs_query(mutation, variables=None):
        call_order.append("gs")
        return ({"w0": {"id": "press-slot-1"}}, [])
    gs_client.query_collecting_errors = AsyncMock(side_effect=gs_query)

    nx_client = AsyncMock(spec=MondayClient)
    nx_client.__aenter__ = AsyncMock(return_value=nx_client)
    nx_client.__aexit__ = AsyncMock(return_value=None)
    async def nx_query(mutation, variables=None):
        call_order.append("nx")
        return ({"w1": {"id": "pack-slot-1"}}, [])
    nx_client.query_collecting_errors = AsyncMock(side_effect=nx_query)

    plan = Plan(slot_writes=(
        SlotWrite(slot_id=None, name="GS", machine_id="1", quantity=100,
                  status=SlotStatus.QUEUED, instance="gray_space"),
        SlotWrite(slot_id=None, name="NX", machine_id="2", quantity=100,
                  status=SlotStatus.QUEUED, instance="nexiuum"),
    ))

    with (
        patch("engine.io.apply.gray_space_client", return_value=gs_client),
        patch("engine.io.apply.nexiuum_client", return_value=nx_client),
    ):
        result = await apply_plan(plan)

    # Gray Space fired before Nexiuum
    assert call_order == ["gs", "nx"]
    assert "press-slot-1" in result.created_slot_ids
    assert "pack-slot-1" in result.created_slot_ids
    assert not result.errors


async def test_apply_plan_errors_when_nexiuum_writes_but_not_enabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If a plan contains Nexiuum writes but Nexiuum config is off, fail
    loudly with a clear error rather than silently dropping the writes."""
    from engine.io.apply import apply_plan
    from engine.models import Plan, SlotStatus, SlotWrite

    monkeypatch.setenv("MONDAY_NEXIUUM_TOKEN", "")
    monkeypatch.delenv("NEXIUUM_CAPACITY_ENGINE_BOARD", raising=False)
    monkeypatch.delenv("NEXIUUM_PROCESS_RECIPE_BOARD", raising=False)
    monkeypatch.delenv("NEXIUUM_SCHEDULE_BOARD", raising=False)
    reset_settings_for_tests()

    plan = Plan(slot_writes=(
        SlotWrite(slot_id=None, name="NX", machine_id="1", quantity=100,
                  status=SlotStatus.QUEUED, instance="nexiuum"),
    ))

    result = await apply_plan(plan)
    assert result.errors
    assert "Nexiuum-instance writes but Nexiuum config is not enabled" in result.errors[0]
    assert result.created_slot_ids == []


# ─────────────────────────────────────────────────────────────────────────
# Multi-stage scheduler — tags writes with the right instance
# ─────────────────────────────────────────────────────────────────────────


def test_scheduler_tags_slot_writes_with_machine_instance() -> None:
    """plan_for_new_order must propagate each placed machine's instance
    onto the corresponding SlotWrite so apply_plan can route correctly."""
    from datetime import datetime
    from engine.core.scheduler import plan_for_new_order
    from engine.models import (
        Machine, MachineStatus, Recipe, RecipeStage, RecipeStatus,
        ScheduleNewOrder, Snapshot,
    )

    tz_now = datetime(2026, 5, 25, 8, 0, 0)

    gs_press = Machine(
        id="gs1", name="Gandalf", process_group="Pressing",  # type: ignore[arg-type]
        status=MachineStatus.ONLINE, capacity_per_hour=40000.0,
        hours_per_day=16, working_window_start=6, working_window_end=22,
        changeover_minutes=30, dual_sided_only=False, max_job_size=None,
        force_route_condition=None, last_job_ended_at=None,
        instance="gray_space",
    )
    nx_blister = Machine(
        id="nx1", name="Blister-Fast-1", process_group="Blister",  # type: ignore[arg-type]
        status=MachineStatus.ONLINE, capacity_per_hour=4000.0,
        hours_per_day=16, working_window_start=6, working_window_end=22,
        changeover_minutes=30, dual_sided_only=False, max_job_size=None,
        force_route_condition=None, last_job_ended_at=None,
        instance="nexiuum",
    )
    recipe = Recipe(
        id="r1", name="press-then-blister",
        recipe_key="press-then-blister", version=1, status=RecipeStatus.ACTIVE,
        stages=(
            RecipeStage(id="press", machine_class="Pressing", depends_on=()),  # type: ignore[arg-type]
            RecipeStage(id="blister", machine_class="Blister", depends_on=("press",)),  # type: ignore[arg-type]
        ),
        instance="nexiuum",  # recipe lives on Nexiuum per Phase 2 addendum #8
    )

    snapshot = Snapshot(
        read_at=tz_now, machines=(gs_press, nx_blister),
        recipes=(recipe,), slots=(),
    )
    order = ScheduleNewOrder(
        job_reference_id="job-1", recipe_key="press-then-blister",
        recipe_version=1, quantity=10000,
    )

    plan = plan_for_new_order(snapshot, order, now=tz_now)

    # Two stage writes, one per stage. Each tagged with the placed machine's instance.
    assert len(plan.slot_writes) == 2
    by_stage = {w.stage_id: w for w in plan.slot_writes}
    assert by_stage["press"].instance == "gray_space"
    assert by_stage["press"].machine_id == "gs1"
    assert by_stage["blister"].instance == "nexiuum"
    assert by_stage["blister"].machine_id == "nx1"


async def test_monday_client_uses_correct_token_per_instance(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Each MondayClient instance must send its OWN token in the
    Authorization header. Verified by intercepting the HTTPX request and
    inspecting the header."""
    monkeypatch.setenv("MONDAY_GRAYSPACE_TOKEN", "gs-token-AAA")
    monkeypatch.setenv("MONDAY_NEXIUUM_TOKEN", "nx-token-BBB")
    monkeypatch.setenv("NEXIUUM_CAPACITY_ENGINE_BOARD", "111")
    monkeypatch.setenv("NEXIUUM_PROCESS_RECIPE_BOARD", "222")
    monkeypatch.setenv("NEXIUUM_SCHEDULE_BOARD", "333")
    reset_settings_for_tests()

    seen_tokens: list[str] = []

    def transport_handler(request: httpx.Request) -> httpx.Response:
        seen_tokens.append(request.headers.get("Authorization", ""))
        return httpx.Response(200, json={"data": {"me": {"id": "1"}}})

    # Patch httpx.AsyncClient to use our mock transport so we can read the
    # outgoing Authorization header per client.
    real_async_client = httpx.AsyncClient

    def patched_async_client(*args: object, **kwargs: object) -> httpx.AsyncClient:
        kwargs["transport"] = httpx.MockTransport(transport_handler)
        return real_async_client(*args, **kwargs)  # type: ignore[arg-type]

    with patch("engine.io.monday.httpx.AsyncClient", side_effect=patched_async_client):
        from engine.io.monday import gray_space_client, nexiuum_client

        async with gray_space_client() as gs:
            await gs.query("query { me { id } }")
        async with nexiuum_client() as nx:
            await nx.query("query { me { id } }")

    assert seen_tokens == ["gs-token-AAA", "nx-token-BBB"]
