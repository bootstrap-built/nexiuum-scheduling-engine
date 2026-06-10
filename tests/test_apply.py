"""Unit tests for apply_plan (no Monday interaction).

Covers the column-value serializer and the batched mutation builder.
Live writes are tested separately (test_apply_live.py — requires token).
"""

from __future__ import annotations

import json
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from engine.config import get_settings
from engine.io.apply import (
    _build_batched_mutation_for_instance,
    _build_column_values,
)
from engine.models import Plan, Priority, SlotStatus, SlotWrite

TZ = ZoneInfo("America/Denver")
SETTINGS = get_settings()
GS_COLS = SETTINGS.schedule_cols("gray_space")


def _build_cv(write, settings, reflow_hash):
    """Test helper — apply.py's _build_column_values for Gray Space."""
    return _build_column_values(
        write, settings.schedule_cols("gray_space"), settings, reflow_hash
    )


def _build_mut(plan, settings, reflow_hash):
    """Test helper — apply.py's batched mutation builder for Gray Space."""
    return _build_batched_mutation_for_instance(
        list(enumerate(plan.slot_writes)),
        settings.gray_space_schedule_board,
        settings.schedule_cols("gray_space"),
        settings,
        reflow_hash,
    )


# ─── Column value serializer ─────────────────────────────────────────────


def test_column_values_create_slot_full_payload():
    """A typical create-slot SlotWrite serializes the right column types."""
    w = SlotWrite(
        slot_id=None,
        name="N0001 → Gandalf",
        machine_id="12047953695",
        job_reference_id="11801201557",
        stage_id="press",
        recipe_key="tablet-press-standard",
        recipe_version=1,
        quantity=100000,
        planned_start=datetime(2026, 5, 21, 8, 0, 0, tzinfo=TZ),
        planned_end=datetime(2026, 5, 21, 10, 30, 0, tzinfo=TZ),
        status=SlotStatus.QUEUED,
        priority=Priority.NORMAL,
        manually_placed=False,
    )
    cv = _build_cv(w, SETTINGS, "abc123")

    # Machine + job ref are board_relation columns: {"item_ids": [N]}
    assert cv[SETTINGS.col_schedule_machine] == {"item_ids": [12047953695]}
    assert cv[SETTINGS.col_schedule_job_reference] == {"item_ids": [11801201557]}

    # Text/number columns: stringified
    assert cv[SETTINGS.col_schedule_stage_id] == "press"
    assert cv[SETTINGS.col_schedule_recipe_key] == "tablet-press-standard"
    assert cv[SETTINGS.col_schedule_recipe_version] == "1"
    assert cv[SETTINGS.col_schedule_quantity] == "100000"

    # Date+hour: local → UTC. 8 AM MDT = 14:00 UTC.
    assert cv[SETTINGS.col_schedule_planned_start] == {
        "date": "2026-05-21", "time": "14:00:00"
    }
    assert cv[SETTINGS.col_schedule_planned_end] == {
        "date": "2026-05-21", "time": "16:30:00"
    }

    # Status + Priority: labels
    assert cv[SETTINGS.col_schedule_status] == {"label": "Queued"}
    assert cv[SETTINGS.col_schedule_priority] == {"label": "Normal"}

    # Checkbox: {"checked": "true"|"false"}
    assert cv[SETTINGS.col_schedule_manually_placed] == {"checked": "false"}

    # Echo-guard hash stamped on every write
    assert cv[SETTINGS.col_schedule_last_reflow_hash] == "abc123"


def test_column_values_writes_n_number_when_present():
    """A SlotWrite carrying an N# serializes into the N# text column."""
    w = SlotWrite(slot_id=None, machine_id="12047953695", quantity=100, n_number="N3629")
    cv = _build_cv(w, SETTINGS, "h1")
    assert cv[SETTINGS.col_schedule_n_number] == "N3629"


def test_column_values_omits_n_number_when_none():
    """n_number=None means don't-touch — no N# column write."""
    w = SlotWrite(slot_id="X", machine_id="12047953695")
    cv = _build_cv(w, SETTINGS, "h1")
    assert SETTINGS.col_schedule_n_number not in cv


def test_n_number_round_trips_write_then_read():
    """N# survives the write → re-read cycle: the apply serializer writes
    the same column id the snapshot parser reads, on both instances.

    This is the round-trip guarantee baton-pass / future reflow rely on —
    N# present on Slot reads without re-fetching from upstream.
    """
    from engine.io.snapshot import _parse_slot

    for instance in ("gray_space", "nexiuum"):
        cols = SETTINGS.schedule_cols(instance)
        # Write side: SlotWrite → column_values.
        w = SlotWrite(
            slot_id=None, machine_id="1", quantity=10,
            n_number="N42", instance=instance,
        )
        cv = _build_column_values(w, cols, SETTINGS, "rh")
        assert cv[cols.n_number] == "N42"

        # Read side: a Monday item carrying that column → Slot.n_number.
        item = {
            "id": "slot-1",
            "name": "whatever",
            "column_values": [{"id": cols.n_number, "text": "N42"}],
        }
        slot = _parse_slot(item, SETTINGS, instance=instance)
        assert slot.n_number == "N42"


def test_parse_slot_n_number_none_when_column_blank():
    """A blank/absent N# column reads back as None, not ''."""
    from engine.io.snapshot import _parse_slot

    cols = SETTINGS.schedule_cols("gray_space")
    item = {
        "id": "slot-2", "name": "x",
        "column_values": [{"id": cols.n_number, "text": ""}],
    }
    assert _parse_slot(item, SETTINGS, instance="gray_space").n_number is None
    # Column entirely absent.
    item2 = {"id": "slot-3", "name": "x", "column_values": []}
    assert _parse_slot(item2, SETTINGS, instance="gray_space").n_number is None


def test_column_values_writes_flavor_when_present():
    """A SlotWrite carrying a flavor serializes into the Flavor text column."""
    w = SlotWrite(
        slot_id=None, machine_id="12047953695", quantity=100,
        flavor="Strawberry Banana",
    )
    cv = _build_cv(w, SETTINGS, "h1")
    assert cv[SETTINGS.col_schedule_flavor] == "Strawberry Banana"


def test_column_values_omits_flavor_when_none():
    """flavor=None means don't-touch — no Flavor column write."""
    w = SlotWrite(slot_id="X", machine_id="12047953695")
    cv = _build_cv(w, SETTINGS, "h1")
    assert SETTINGS.col_schedule_flavor not in cv


def test_flavor_round_trips_write_then_read():
    """Flavor survives the write → re-read cycle: the apply serializer writes
    the same column id the snapshot parser reads, on both instances —
    mirroring N#'s round-trip guarantee that baton-pass relies on.
    """
    from engine.io.snapshot import _parse_slot

    for instance in ("gray_space", "nexiuum"):
        cols = SETTINGS.schedule_cols(instance)
        w = SlotWrite(
            slot_id=None, machine_id="1", quantity=10,
            flavor="Blueberry", instance=instance,
        )
        cv = _build_column_values(w, cols, SETTINGS, "rh")
        assert cv[cols.flavor] == "Blueberry"

        item = {
            "id": "slot-1",
            "name": "whatever",
            "column_values": [{"id": cols.flavor, "text": "Blueberry"}],
        }
        slot = _parse_slot(item, SETTINGS, instance=instance)
        assert slot.flavor == "Blueberry"


def test_parse_slot_flavor_none_when_column_blank():
    """A blank/absent Flavor column reads back as None, not ''."""
    from engine.io.snapshot import _parse_slot

    cols = SETTINGS.schedule_cols("gray_space")
    item = {
        "id": "slot-2", "name": "x",
        "column_values": [{"id": cols.flavor, "text": ""}],
    }
    assert _parse_slot(item, SETTINGS, instance="gray_space").flavor is None
    item2 = {"id": "slot-3", "name": "x", "column_values": []}
    assert _parse_slot(item2, SETTINGS, instance="gray_space").flavor is None


def test_column_values_skips_simulate_job_id():
    """SIMULATE_JOB_ID sentinel must never get sent as a real Job Reference link."""
    w = SlotWrite(
        slot_id=None,
        job_reference_id="__simulate__",
        machine_id="12047953695",
        quantity=100,
    )
    cv = _build_cv(w, SETTINGS, "h1")
    assert SETTINGS.col_schedule_job_reference not in cv


def test_column_values_skips_job_reference_when_target_instance_differs_from_origin():
    """Cross-instance press slot: a Nexiuum-origin order's Gray Space press slot
    must NOT set the Job Reference board_relation, because the Gray Space Schedule
    board's Job Reference column is connected to Blend Records, not the Nexiuum
    Production Schedule item the job_reference_id points at (issue #9)."""
    w = SlotWrite(
        slot_id=None,
        job_reference_id="12152485009",  # a Nexiuum Production Schedule item
        machine_id="12047953695",
        quantity=100,
        instance="gray_space",  # press lands on Gray Space
        origin_instance="nexiuum",  # but the order originates on Nexiuum
    )
    cv = _build_cv(w, SETTINGS, "h1")
    assert SETTINGS.col_schedule_job_reference not in cv


def test_column_values_sets_job_reference_when_instance_matches_origin():
    """Same-instance write keeps the Job Reference link. A Nexiuum packaging slot
    on a Nexiuum-origin order, and a Gray Space slot on a Gray Space-origin order,
    both stay connected (Phase 1 behavior preserved)."""
    # Gray Space-origin (Phase 1): origin defaults to gray_space.
    gs = SlotWrite(
        slot_id=None, job_reference_id="11801201557",
        machine_id="12047953695", quantity=100,
    )
    cv_gs = _build_cv(gs, SETTINGS, "h1")
    assert cv_gs[SETTINGS.col_schedule_job_reference] == {"item_ids": [11801201557]}

    # Nexiuum-origin packaging slot landing on the Nexiuum Schedule board.
    nx = SlotWrite(
        slot_id=None, job_reference_id="12152485009",
        machine_id="12047953695", quantity=100,
        instance="nexiuum", origin_instance="nexiuum",
    )
    cv_nx = _build_column_values(
        nx, SETTINGS.schedule_cols("nexiuum"), SETTINGS, "h1"
    )
    assert cv_nx[SETTINGS.col_nx_schedule_job_reference] == {"item_ids": [12152485009]}


def test_column_values_skips_none_fields():
    """None-valued fields are omitted from the column_values dict."""
    w = SlotWrite(slot_id="X", machine_id="12047953695")
    cv = _build_cv(w, SETTINGS, "h1")
    # Only machine + reflow hash; everything else omitted.
    assert set(cv.keys()) == {
        SETTINGS.col_schedule_machine,
        SETTINGS.col_schedule_last_reflow_hash,
    }


def test_column_values_fields_to_clear_emits_null():
    """fields_to_clear sets the column value to null (explicit clear)."""
    w = SlotWrite(
        slot_id="X",
        fields_to_clear=frozenset({"actual_start", "actual_end"}),
    )
    cv = _build_cv(w, SETTINGS, "h1")
    assert cv[SETTINGS.col_schedule_actual_start] is None
    assert cv[SETTINGS.col_schedule_actual_end] is None


def test_column_values_fields_to_clear_overrides_set_value():
    """If a field is both set and in fields_to_clear, clear wins (defensive)."""
    w = SlotWrite(
        slot_id="X",
        actual_start=datetime(2026, 5, 21, 8, 0, 0, tzinfo=TZ),
        fields_to_clear=frozenset({"actual_start"}),
    )
    cv = _build_cv(w, SETTINGS, "h1")
    assert cv[SETTINGS.col_schedule_actual_start] is None


# ─── Batched mutation builder ────────────────────────────────────────────


def test_mutation_builder_creates_aliased_create_for_new_slot():
    plan = Plan(slot_writes=(SlotWrite(slot_id=None, machine_id="12047953695", name="X"),))
    mutation, vars_, aliases = _build_mut(plan, SETTINGS, "h1")
    assert "create_item" in mutation
    assert "w0:" in mutation
    assert aliases == [(0, "w0")]
    assert "name_0" in vars_
    assert "cv_0" in vars_


def test_mutation_builder_creates_aliased_update_for_existing_slot():
    plan = Plan(slot_writes=(SlotWrite(slot_id="12000", machine_id="12047953695"),))
    mutation, vars_, aliases = _build_mut(plan, SETTINGS, "h1")
    assert "change_multiple_column_values" in mutation
    assert aliases == [(0, "w0")]
    assert vars_["item_0"] == "12000"


def test_mutation_builder_mixed_creates_and_updates():
    plan = Plan(
        slot_writes=(
            SlotWrite(slot_id=None, machine_id="12047953695", name="new"),
            SlotWrite(slot_id="12000", machine_id="12047953695"),
            SlotWrite(slot_id=None, machine_id="12047930644", name="new2"),
        )
    )
    mutation, vars_, aliases = _build_mut(plan, SETTINGS, "h1")
    assert aliases == [(0, "w0"), (1, "w1"), (2, "w2")]
    assert "w0:" in mutation and "w1:" in mutation and "w2:" in mutation


def test_mutation_builder_empty_plan_returns_empty():
    plan = Plan(slot_writes=())
    mutation, vars_, aliases = _build_mut(plan, SETTINGS, "h1")
    assert mutation == ""
    assert vars_ == {}
    assert aliases == []


def test_mutation_cv_var_is_valid_json():
    """The cv_<idx> variable is sent as a JSON string."""
    plan = Plan(
        slot_writes=(
            SlotWrite(
                slot_id=None,
                name="X",
                machine_id="12047953695",
                quantity=100,
                status=SlotStatus.QUEUED,
            ),
        )
    )
    _, vars_, _ = _build_mut(plan, SETTINGS, "h1")
    parsed = json.loads(vars_["cv_0"])
    assert parsed[SETTINGS.col_schedule_machine] == {"item_ids": [12047953695]}
    assert parsed[SETTINGS.col_schedule_status] == {"label": "Queued"}


# ─── machine_writes guardrail ─────────────────────────────────────────────


def test_apply_plan_raises_on_machine_writes():
    """Codex E4 review #7: machine_writes must not silently drop."""
    import asyncio
    from engine.io.apply import apply_plan
    from engine.models import MachineWrite

    plan = Plan(
        slot_writes=(),
        machine_writes=(MachineWrite(machine_id="12047953695", last_job_ended_at=None),),
    )
    with pytest.raises(NotImplementedError, match="machine_writes not yet implemented"):
        asyncio.run(apply_plan(plan))


# ─── Partial-failure handling (Codex E4 review #5) ───────────────────────
#
# Monday's batched aliased mutations are NOT transactional — aliases execute
# sequentially and some may succeed while others fail. apply_plan must
# report both: keep the successful slot ids, but populate `errors` so the
# worker raises and the operator can reconcile in Monday directly.


@pytest.mark.asyncio
async def test_apply_plan_partial_success_returns_both_successes_and_errors():
    """First alias succeeds; second fails — result includes both."""
    from engine.io.apply import apply_plan

    plan = Plan(slot_writes=(
        SlotWrite(slot_id=None, machine_id="12047953695", name="w0", quantity=100),
        SlotWrite(slot_id=None, machine_id="12047930644", name="w1", quantity=200),
    ))

    fake_data = {"w0": {"id": "9001"}, "w1": None}
    fake_errors = [
        {"message": "BoardRelationValue not found", "path": ["w1"]},
    ]

    class _FakeClient:
        async def query_collecting_errors(self, *a, **kw):
            return fake_data, fake_errors

        async def delete_items(self, item_ids):  # #12 rollback collaborator
            return list(item_ids), []

    result = await apply_plan(plan, client=_FakeClient())
    # created_slot_ids stays the historical "what we created this run" record,
    # even though #12 then rolls the slot back.
    assert result.created_slot_ids == ["9001"]
    assert result.updated_slot_ids == []
    assert not result.success
    assert any("alias w1" in e and "BoardRelationValue" in e for e in result.errors)


# NOTE: #9's `test_apply_plan_partial_failure_flags_created_slots_as_orphans`
# (created slots persist as orphans on partial failure) was superseded by #12 —
# those slots are now rolled back. See `test_apply_plan_rolls_back_created_slots_
# on_partial_failure` (clean rollback) and `test_rollback_failure_keeps_orphan_
# surfaced` (rollback itself fails) below.


@pytest.mark.asyncio
async def test_apply_plan_full_success_has_no_orphans():
    """A fully successful apply flags no orphans."""
    from engine.io.apply import apply_plan

    plan = Plan(slot_writes=(
        SlotWrite(slot_id=None, machine_id="12047953695", name="w0", quantity=100),
    ))
    fake_data = {"w0": {"id": "9001"}}

    class _FakeClient:
        async def query_collecting_errors(self, *a, **kw):
            return fake_data, []

    result = await apply_plan(plan, client=_FakeClient())
    assert result.success
    assert result.orphaned_slot_ids == []


@pytest.mark.asyncio
async def test_apply_plan_full_failure_returns_errors_no_ids():
    """All aliases fail — no created/updated, full error list."""
    from engine.io.apply import apply_plan

    plan = Plan(slot_writes=(
        SlotWrite(slot_id="100", machine_id="12047953695"),
        SlotWrite(slot_id="200", machine_id="12047930644"),
    ))
    fake_data = {"w0": None, "w1": None}
    fake_errors = [
        {"message": "complexity budget exceeded", "path": ["w0"]},
        {"message": "complexity budget exceeded", "path": ["w1"]},
    ]

    class _FakeClient:
        async def query_collecting_errors(self, *a, **kw):
            return fake_data, fake_errors

    result = await apply_plan(plan, client=_FakeClient())
    assert result.created_slot_ids == []
    assert result.updated_slot_ids == []
    assert not result.success
    assert len(result.errors) == 2
    assert all("complexity budget" in e for e in result.errors)


@pytest.mark.asyncio
async def test_apply_plan_full_success_returns_clean_result():
    """Every alias succeeds → no errors, both ids populated."""
    from engine.io.apply import apply_plan

    plan = Plan(slot_writes=(
        SlotWrite(slot_id=None, machine_id="12047953695", name="w0"),
        SlotWrite(slot_id="200", machine_id="12047930644"),
    ))
    fake_data = {"w0": {"id": "9001"}, "w1": {"id": "200"}}

    class _FakeClient:
        async def query_collecting_errors(self, *a, **kw):
            return fake_data, []

    result = await apply_plan(plan, client=_FakeClient())
    assert result.created_slot_ids == ["9001"]
    assert result.updated_slot_ids == ["200"]
    assert result.success
    assert result.errors == []


@pytest.mark.asyncio
async def test_apply_plan_top_level_error_without_path_surfaces_as_batch():
    """A GraphQL error with no `path` (e.g. query parse error) is batch-level."""
    from engine.io.apply import apply_plan

    plan = Plan(slot_writes=(SlotWrite(slot_id="100", machine_id="12047953695"),))
    fake_data: dict = {}
    fake_errors = [{"message": "Parse error: unexpected EOF"}]

    class _FakeClient:
        async def query_collecting_errors(self, *a, **kw):
            return fake_data, fake_errors

    result = await apply_plan(plan, client=_FakeClient())
    assert not result.success
    assert any("batch-level error" in e for e in result.errors)
    assert any("Parse error" in e for e in result.errors)


@pytest.mark.asyncio
async def test_apply_plan_transport_error_returned_as_error():
    """httpx-style exceptions become a single GraphQL transport error in the result."""
    from engine.io.apply import apply_plan

    plan = Plan(slot_writes=(SlotWrite(slot_id="100", machine_id="12047953695"),))

    class _BrokenClient:
        async def query_collecting_errors(self, *a, **kw):
            raise RuntimeError("connection refused")

    result = await apply_plan(plan, client=_BrokenClient())
    assert not result.success
    assert len(result.errors) == 1
    assert "transport error" in result.errors[0]
    assert "connection refused" in result.errors[0]


# ─── Rollback on partial failure (#12 — apply_plan atomicity) ─────────────
#
# A mid-plan failure must leave NO orphan slots: any slot created in the
# failing run is rolled back (deleted). Rollback only ever touches slots
# created in THIS run — never pre-existing/updated slots.


@pytest.mark.asyncio
async def test_apply_plan_rolls_back_created_slots_on_partial_failure():
    """A mid-plan failure deletes the slots created in that run → no orphans."""
    from engine.io.apply import apply_plan

    plan = Plan(slot_writes=(
        SlotWrite(slot_id=None, machine_id="12047953695", name="w0", quantity=100),
        SlotWrite(slot_id=None, machine_id="12047930644", name="w1", quantity=200),
    ))
    fake_data = {"w0": {"id": "9001"}, "w1": None}
    fake_errors = [{"message": "BoardRelationValue not found", "path": ["w1"]}]

    deleted_calls: list[list[str]] = []

    class _FakeClient:
        async def query_collecting_errors(self, *a, **kw):
            return fake_data, fake_errors

        async def delete_items(self, item_ids):
            deleted_calls.append(list(item_ids))
            return list(item_ids), []

    result = await apply_plan(plan, client=_FakeClient())
    assert not result.success                    # failure still surfaced
    assert result.created_slot_ids == ["9001"]   # historical "what we created"
    assert result.rolled_back_slot_ids == ["9001"]
    assert result.orphaned_slot_ids == []        # cleaned up — nothing left behind
    assert deleted_calls == [["9001"]]           # exactly the created slot, nothing else


@pytest.mark.asyncio
async def test_delete_items_batches_and_routes_per_id_errors():
    """MondayClient.delete_items builds an aliased delete_item batch and routes
    per-alias errors back to their slot id, returning (deleted, errors)."""
    import httpx
    from unittest.mock import patch
    from engine.io.monday import MondayClient

    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = json.loads(request.content)
        return httpx.Response(200, json={
            "data": {"d0": {"id": "111"}, "d1": None},
            "errors": [{"message": "item not found", "path": ["d1"]}],
        })

    real = httpx.AsyncClient

    def patched(*a, **kw):
        kw["transport"] = httpx.MockTransport(handler)
        return real(*a, **kw)

    with patch("engine.io.monday.httpx.AsyncClient", side_effect=patched):
        async with MondayClient(token="t") as c:
            deleted, errs = await c.delete_items(["111", "222"])

    assert deleted == ["111"]
    assert len(errs) == 1
    assert "222" in errs[0] and "item not found" in errs[0]
    # A real aliased delete_item mutation went out, with the ids as variables.
    assert "delete_item" in captured["body"]["query"]
    assert captured["body"]["variables"]["id_0"] == "111"
    assert captured["body"]["variables"]["id_1"] == "222"


@pytest.mark.asyncio
async def test_delete_items_treats_id_with_warning_as_deleted():
    """If a delete alias returns an id alongside a warning, the item IS gone —
    count it as deleted, not as a residual orphan. Mirrors the create path's
    'an id came back means it exists' signal, so rollback doesn't falsely report
    an already-removed slot as an orphan."""
    import httpx
    from unittest.mock import patch
    from engine.io.monday import MondayClient

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={
            "data": {"d0": {"id": "111"}},
            "errors": [{"message": "soft warning", "path": ["d0"]}],
        })

    real = httpx.AsyncClient

    def patched(*a, **kw):
        kw["transport"] = httpx.MockTransport(handler)
        return real(*a, **kw)

    with patch("engine.io.monday.httpx.AsyncClient", side_effect=patched):
        async with MondayClient(token="t") as c:
            deleted, errs = await c.delete_items(["111"])

    assert deleted == ["111"]
    assert errs == []


@pytest.mark.asyncio
async def test_delete_items_empty_is_noop():
    """delete_items([]) makes no network call and returns empty lists."""
    from engine.io.monday import MondayClient

    async with MondayClient(token="t") as c:  # no transport patched — must not call out
        deleted, errs = await c.delete_items([])
    assert deleted == []
    assert errs == []


@pytest.mark.asyncio
async def test_rollback_never_deletes_updated_or_preexisting_slots():
    """Rollback only removes slots CREATED in the failing run. An updated
    (pre-existing) slot must never be passed to delete_items, even when the
    run fails."""
    from engine.io.apply import apply_plan

    plan = Plan(slot_writes=(
        SlotWrite(slot_id=None, machine_id="12047953695", name="w0", quantity=100),  # create OK
        SlotWrite(slot_id="200", machine_id="12047930644"),                          # update (pre-existing)
        SlotWrite(slot_id=None, machine_id="12047930644", name="w2", quantity=50),   # create FAILS
    ))
    fake_data = {"w0": {"id": "9001"}, "w1": {"id": "200"}, "w2": None}
    fake_errors = [{"message": "boom", "path": ["w2"]}]

    deleted_calls: list[list[str]] = []

    class _FakeClient:
        async def query_collecting_errors(self, *a, **kw):
            return fake_data, fake_errors

        async def delete_items(self, item_ids):
            deleted_calls.append(list(item_ids))
            return list(item_ids), []

    result = await apply_plan(plan, client=_FakeClient())
    assert not result.success
    assert result.created_slot_ids == ["9001"]
    assert result.updated_slot_ids == ["200"]
    assert result.rolled_back_slot_ids == ["9001"]
    assert result.orphaned_slot_ids == []

    flat_deleted = [i for call in deleted_calls for i in call]
    assert "200" not in flat_deleted   # the pre-existing/updated slot is never deleted
    assert flat_deleted == ["9001"]


@pytest.mark.asyncio
async def test_rollback_failure_keeps_orphan_surfaced():
    """If the rollback delete itself fails, the created slot stays surfaced as
    an orphan and a rollback error is appended — the failure stays loud rather
    than reporting a clean rollback that didn't happen."""
    from engine.io.apply import apply_plan

    plan = Plan(slot_writes=(
        SlotWrite(slot_id=None, machine_id="12047953695", name="w0", quantity=100),
        SlotWrite(slot_id=None, machine_id="12047930644", name="w1", quantity=200),
    ))
    fake_data = {"w0": {"id": "9001"}, "w1": None}
    fake_errors = [{"message": "boom", "path": ["w1"]}]

    class _FakeClient:
        async def query_collecting_errors(self, *a, **kw):
            return fake_data, fake_errors

        async def delete_items(self, item_ids):
            return [], [f"item {item_ids[0]}: delete refused"]   # rollback fails

    result = await apply_plan(plan, client=_FakeClient())
    assert not result.success
    assert result.created_slot_ids == ["9001"]
    assert result.rolled_back_slot_ids == []
    assert result.orphaned_slot_ids == ["9001"]   # rollback couldn't remove it → still an orphan
    assert any("rollback delete failed" in e for e in result.errors)


# ─── Echo registry recording (Codex E4 B1) ──────────────────────────────


@pytest.mark.asyncio
async def test_apply_plan_records_writes_in_echo_registry():
    """apply_plan must feed the write-origin echo registry: updates are
    recorded (column-scoped) on the target board, creates are recorded
    pulse-scoped once Monday returns the new id. The webhook route relies
    on these records to tell engine echoes from operator changes."""
    from engine.config import get_settings
    from engine.io import recent_writes
    from engine.io.apply import apply_plan

    plan = Plan(slot_writes=(
        SlotWrite(slot_id=None, machine_id="12047953695", name="w0"),
        SlotWrite(slot_id="200", machine_id="12047930644"),
    ))
    fake_data = {"w0": {"id": "9001"}, "w1": {"id": "200"}}

    class _FakeClient:
        async def query_collecting_errors(self, *a, **kw):
            return fake_data, []

    result = await apply_plan(plan, client=_FakeClient())
    assert result.success

    board = get_settings().schedule_board("gray_space")
    # Create: pulse-scoped — any event on the new slot is an echo.
    assert recent_writes.is_engine_echo(board, "9001", None)
    assert recent_writes.is_engine_echo(board, "9001", "anything")
    # Update: column-scoped — the machine column was written...
    cols = get_settings().schedule_cols("gray_space")
    assert recent_writes.is_engine_echo(board, "200", cols.machine)
    # ...but an untouched column on the same slot is NOT an echo.
    assert not recent_writes.is_engine_echo(board, "200", "definitely_not_written")
    # And other boards/items are never suppressed.
    assert not recent_writes.is_engine_echo(18404836849, "200", cols.machine)


@pytest.mark.asyncio
async def test_apply_plan_records_update_even_when_mutation_fails():
    """Updates are recorded BEFORE the mutation fires (webhook delivery can
    race our response parsing). A record for a failed write is harmless —
    TTL-bounded suppression of an event that never arrives."""
    from engine.config import get_settings
    from engine.io import recent_writes
    from engine.io.apply import apply_plan

    plan = Plan(slot_writes=(SlotWrite(slot_id="300", machine_id="12047930644"),))

    class _FakeClient:
        async def query_collecting_errors(self, *a, **kw):
            raise RuntimeError("transport down")

    result = await apply_plan(plan, client=_FakeClient())
    assert not result.success
    board = get_settings().schedule_board("gray_space")
    cols = get_settings().schedule_cols("gray_space")
    assert recent_writes.is_engine_echo(board, "300", cols.machine)
