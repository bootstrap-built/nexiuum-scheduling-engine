"""Snapshot reader — load all engine-relevant Monday board state.

Reads Capacity Engine, Process Recipe, and Schedule boards. Parses each row
into a domain model. Returns an immutable `Snapshot`.

The pure-core placement function depends on Snapshot, never on Monday client
or raw JSON. Read errors are surfaced as exceptions; partial snapshots are
never returned.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime
from typing import Any

from engine.config import Settings, get_settings
from engine.core.timezone import monday_to_local, now_local
from engine.io.monday import MondayClient
from engine.models import (
    Machine,
    MachineStatus,
    Priority,
    Recipe,
    RecipeStage,
    RecipeStatus,
    Slot,
    SlotStatus,
    Snapshot,
)

log = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────────
# Column value parsing helpers
# ─────────────────────────────────────────────────────────────────────────


def _cv_by_id(item: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """Index an item's column_values list by column id."""
    return {cv["id"]: cv for cv in item.get("column_values", []) or []}


def _text(cv: dict[str, Any] | None) -> str | None:
    if not cv:
        return None
    t = cv.get("text")
    return t if t not in (None, "") else None


def _number(cv: dict[str, Any] | None) -> float | None:
    t = _text(cv)
    if t is None:
        return None
    try:
        return float(t)
    except ValueError:
        return None


def _int(cv: dict[str, Any] | None) -> int | None:
    n = _number(cv)
    return int(n) if n is not None else None


def _status_label(cv: dict[str, Any] | None) -> str | None:
    return _text(cv)


def _checkbox(cv: dict[str, Any] | None) -> bool:
    if not cv:
        return False
    # Monday checkbox value is JSON like '{"checked":"true"}'
    raw = cv.get("value")
    if not raw:
        return False
    try:
        parsed = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return False
    return parsed.get("checked") in (True, "true")


def _date_payload(cv: dict[str, Any] | None) -> dict[str, str] | None:
    """Return the Monday date column payload from the raw `value` JSON."""
    if not cv:
        return None
    raw = cv.get("value")
    if not raw:
        return None
    try:
        return json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return None


def _board_relation_ids(cv: dict[str, Any] | None) -> list[str]:
    """Extract linked item IDs from a board_relation column.

    Prefers the typed union's `linked_item_ids` field (present when the query
    uses the BoardRelationValue fragment). Falls back to parsing the raw
    `value` JSON for older query shapes.
    """
    if not cv:
        return []
    # Typed union path — works when GraphQL query uses BoardRelationValue fragment
    typed = cv.get("linked_item_ids")
    if typed is not None:
        return [str(x) for x in typed]
    # Fallback — parse raw value JSON
    raw = cv.get("value")
    if not raw:
        return []
    try:
        parsed = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return []
    ids = parsed.get("linkedPulseIds") or parsed.get("item_ids") or []
    out: list[str] = []
    for entry in ids:
        if isinstance(entry, dict):
            v = entry.get("linkedPulseId") or entry.get("item_id")
            if v is not None:
                out.append(str(v))
        else:
            out.append(str(entry))
    return out


def _dependency_ids(cv: dict[str, Any] | None) -> list[str]:
    """Extract linked item IDs from a dependency column (same shape as board_relation)."""
    return _board_relation_ids(cv)


def _mirror_text(cv: dict[str, Any] | None) -> str | None:
    return _text(cv)


# ─────────────────────────────────────────────────────────────────────────
# Row parsers
# ─────────────────────────────────────────────────────────────────────────


def _parse_machine(item: dict[str, Any], s: Settings) -> Machine:
    cv = _cv_by_id(item)
    status_text = _status_label(cv.get(s.col_cap_status))
    try:
        # Empty / missing status — assume Online by convention (operator hasn't set it).
        # Unrecognized label — default to DOWN as a safety measure so the engine
        # doesn't schedule jobs on a machine whose state we can't parse.
        if status_text is None or status_text == "":
            status = MachineStatus.ONLINE
        else:
            status = MachineStatus(status_text)
    except ValueError:
        log.warning(
            "Machine %r has unrecognized status label %r; defaulting to DOWN",
            item.get("name"), status_text,
        )
        status = MachineStatus.DOWN
    process_group = _status_label(cv.get(s.col_cap_process_group))
    return Machine(
        id=str(item["id"]),
        name=item["name"],
        process_group=process_group,  # type: ignore[arg-type]
        status=status,
        capacity_per_hour=_number(cv.get(s.col_cap_capacity)) or 0.0,
        hours_per_day=_number(cv.get(s.col_cap_hours_per_day)) or 0.0,
        working_window_start=_int(cv.get(s.col_cap_window_start)) or 0,
        working_window_end=_int(cv.get(s.col_cap_window_end)) or 24,
        changeover_minutes=_int(cv.get(s.col_cap_changeover)) or 30,
        dual_sided_only=_checkbox(cv.get(s.col_cap_dual_sided)),
        max_job_size=_int(cv.get(s.col_cap_max_job_size)),
        force_route_condition=_text(cv.get(s.col_cap_force_route)),
        last_job_ended_at=monday_to_local(
            _date_payload(cv.get(s.col_cap_last_job_ended_at)),
            s.factory_tz,
        ),
    )


def _parse_recipe(item: dict[str, Any], s: Settings) -> Recipe:
    cv = _cv_by_id(item)
    recipe_key = _text(cv.get(s.col_recipe_key)) or ""
    version = _int(cv.get(s.col_recipe_version)) or 1
    status_text = _status_label(cv.get(s.col_recipe_status)) or "Draft"
    try:
        status = RecipeStatus(status_text)
    except ValueError:
        status = RecipeStatus.DRAFT

    stages_raw = _text(cv.get(s.col_recipe_stages)) or "[]"
    try:
        stages_parsed = json.loads(stages_raw)
    except (json.JSONDecodeError, TypeError):
        log.warning("Recipe %s has invalid stages JSON; treating as empty", item.get("id"))
        stages_parsed = []

    # Validate machine_class against the canonical ProcessGroup labels. A typo
    # in the recipe JSON would otherwise create an unroutable stage with a
    # confusing error later — fail loud here instead.
    valid_classes = {
        "Pressing", "Capsule", "Sachet", "Blister",
        "Clamshell", "Bottle", "Lot Coder", "Hand-pack",
    }
    stages_list = []
    for s_entry in stages_parsed:
        machine_class = s_entry.get("machine_class")
        if machine_class not in valid_classes:
            log.warning(
                "Recipe %s stage %r has unknown machine_class %r; valid: %s",
                item.get("id"), s_entry.get("id"), machine_class, sorted(valid_classes),
            )
        stages_list.append(
            RecipeStage(
                id=str(s_entry.get("id", "")),
                machine_class=machine_class,
                depends_on=tuple(str(d) for d in (s_entry.get("depends_on") or [])),
            )
        )
    stages = tuple(stages_list)

    return Recipe(
        id=str(item["id"]),
        name=item["name"],
        recipe_key=recipe_key,
        version=version,
        status=status,
        stages=stages,
    )


def _parse_slot(item: dict[str, Any], s: Settings) -> Slot:
    cv = _cv_by_id(item)

    job_ref_ids = _board_relation_ids(cv.get(s.col_schedule_job_reference))
    machine_ids = _board_relation_ids(cv.get(s.col_schedule_machine))
    dep_ids = _dependency_ids(cv.get(s.col_schedule_dependent_on))

    status_text = _status_label(cv.get(s.col_schedule_status)) or "Queued"
    try:
        status = SlotStatus(status_text)
    except ValueError:
        status = SlotStatus.QUEUED

    priority_text = _status_label(cv.get(s.col_schedule_priority)) or "Normal"
    try:
        priority = Priority(priority_text)
    except ValueError:
        priority = Priority.NORMAL

    return Slot(
        id=str(item["id"]),
        name=item["name"],
        job_reference_id=job_ref_ids[0] if job_ref_ids else None,
        machine_id=machine_ids[0] if machine_ids else None,
        stage_id=_text(cv.get(s.col_schedule_stage_id)),
        recipe_key=_text(cv.get(s.col_schedule_recipe_key)),
        recipe_version=_int(cv.get(s.col_schedule_recipe_version)),
        quantity=_int(cv.get(s.col_schedule_quantity)) or 0,
        planned_start=monday_to_local(_date_payload(cv.get(s.col_schedule_planned_start)), s.factory_tz),
        planned_end=monday_to_local(_date_payload(cv.get(s.col_schedule_planned_end)), s.factory_tz),
        actual_start=monday_to_local(_date_payload(cv.get(s.col_schedule_actual_start)), s.factory_tz),
        actual_end=monday_to_local(_date_payload(cv.get(s.col_schedule_actual_end)), s.factory_tz),
        dependent_on_ids=tuple(dep_ids),
        status=status,
        manually_placed=_checkbox(cv.get(s.col_schedule_manually_placed)),
        priority=priority,
        last_reflow_hash=_text(cv.get(s.col_schedule_last_reflow_hash)),
        drift_last_detected_at=monday_to_local(
            _date_payload(cv.get(s.col_schedule_drift_last_detected_at)),
            s.factory_tz,
        ),
    )


# ─────────────────────────────────────────────────────────────────────────
# Public entry point
# ─────────────────────────────────────────────────────────────────────────


async def read_snapshot(client: MondayClient | None = None) -> Snapshot:
    """Read all three boards and assemble a Snapshot.

    Pass `client` if you already have an open MondayClient; otherwise this
    opens a short-lived Gray Space client for the duration of the call.
    """
    from engine.io.monday import MondayClient as _MC, gray_space_client  # noqa: PLC0415

    s = get_settings()

    async def _read_all(c: MondayClient) -> Snapshot:
        machines_raw = await c.fetch_board_items(s.gray_space_capacity_engine_board)
        recipes_raw = await c.fetch_board_items(s.gray_space_process_recipe_board)
        slots_raw = await c.fetch_board_items(s.gray_space_schedule_board)

        machines = tuple(_parse_machine(i, s) for i in machines_raw)
        recipes = tuple(_parse_recipe(i, s) for i in recipes_raw)
        slots = tuple(_parse_slot(i, s) for i in slots_raw)

        return Snapshot(
            read_at=now_local(s.factory_tz),
            machines=machines,
            recipes=recipes,
            slots=slots,
        )

    if client is not None:
        return await _read_all(client)
    async with gray_space_client() as c:
        return await _read_all(c)
