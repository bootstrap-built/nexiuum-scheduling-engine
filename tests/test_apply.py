"""Unit tests for apply_plan (no Monday interaction).

Covers the column-value serializer and the batched mutation builder.
Live writes are tested separately (test_apply_live.py — requires token).
"""

from __future__ import annotations

import json
import os
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

os.environ.setdefault("MONDAY_GRAYSPACE_TOKEN", "test-token-not-real")

from engine.config import get_settings  # noqa: E402
from engine.io.apply import (  # noqa: E402
    _build_batched_mutation,
    _build_column_values,
)
from engine.models import Plan, Priority, SlotStatus, SlotWrite  # noqa: E402

TZ = ZoneInfo("America/Denver")
SETTINGS = get_settings()


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
    cv = _build_column_values(w, SETTINGS, "abc123")

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


def test_column_values_skips_simulate_job_id():
    """SIMULATE_JOB_ID sentinel must never get sent as a real Job Reference link."""
    w = SlotWrite(
        slot_id=None,
        job_reference_id="__simulate__",
        machine_id="12047953695",
        quantity=100,
    )
    cv = _build_column_values(w, SETTINGS, "h1")
    assert SETTINGS.col_schedule_job_reference not in cv


def test_column_values_skips_none_fields():
    """None-valued fields are omitted from the column_values dict."""
    w = SlotWrite(slot_id="X", machine_id="12047953695")
    cv = _build_column_values(w, SETTINGS, "h1")
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
    cv = _build_column_values(w, SETTINGS, "h1")
    assert cv[SETTINGS.col_schedule_actual_start] is None
    assert cv[SETTINGS.col_schedule_actual_end] is None


def test_column_values_fields_to_clear_overrides_set_value():
    """If a field is both set and in fields_to_clear, clear wins (defensive)."""
    w = SlotWrite(
        slot_id="X",
        actual_start=datetime(2026, 5, 21, 8, 0, 0, tzinfo=TZ),
        fields_to_clear=frozenset({"actual_start"}),
    )
    cv = _build_column_values(w, SETTINGS, "h1")
    assert cv[SETTINGS.col_schedule_actual_start] is None


# ─── Batched mutation builder ────────────────────────────────────────────


def test_mutation_builder_creates_aliased_create_for_new_slot():
    plan = Plan(slot_writes=(SlotWrite(slot_id=None, machine_id="12047953695", name="X"),))
    mutation, vars_, aliases = _build_batched_mutation(plan, SETTINGS, "h1")
    assert "create_item" in mutation
    assert "w0:" in mutation
    assert aliases == [(0, "w0")]
    assert "name_0" in vars_
    assert "cv_0" in vars_


def test_mutation_builder_creates_aliased_update_for_existing_slot():
    plan = Plan(slot_writes=(SlotWrite(slot_id="12000", machine_id="12047953695"),))
    mutation, vars_, aliases = _build_batched_mutation(plan, SETTINGS, "h1")
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
    mutation, vars_, aliases = _build_batched_mutation(plan, SETTINGS, "h1")
    assert aliases == [(0, "w0"), (1, "w1"), (2, "w2")]
    assert "w0:" in mutation and "w1:" in mutation and "w2:" in mutation


def test_mutation_builder_empty_plan_returns_empty():
    plan = Plan(slot_writes=())
    mutation, vars_, aliases = _build_batched_mutation(plan, SETTINGS, "h1")
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
    _, vars_, _ = _build_batched_mutation(plan, SETTINGS, "h1")
    parsed = json.loads(vars_["cv_0"])
    assert parsed[SETTINGS.col_schedule_machine] == {"item_ids": [12047953695]}
    assert parsed[SETTINGS.col_schedule_status] == {"label": "Queued"}
