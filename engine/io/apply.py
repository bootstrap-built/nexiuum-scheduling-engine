"""apply_plan — convert a Plan into Monday GraphQL mutations and execute.

Pure-core produces Plans. This module is the IO half: it knows how to
serialize a SlotWrite into Monday's column_values shape, batch creates
and updates into one mutation, stamp every write with `last_reflow_hash`
for the echo guard, and surface failures back to the worker.

Phase 1 scope: single-batch writes. No dependency wiring (multi-stage
recipes are Phase 2 — at that point apply_plan grows a Phase B where
created slot IDs back-fill `dependent_on_ids` on later stages).

Field-clearing semantics: `SlotWrite.fields_to_clear` is a frozenset of
SlotWrite attribute names. Those columns get explicitly nulled in the
mutation. Other None-valued fields are skipped (don't-touch).
"""

from __future__ import annotations

import json
import logging
import uuid
from dataclasses import dataclass, field
from typing import Any

from engine.config import Settings, get_settings
from engine.core.timezone import local_to_monday
from engine.io.monday import MondayClient, gray_space_client
from engine.models import Plan, SlotWrite

log = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────────
# Result types
# ─────────────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class ApplyResult:
    """Outcome of an apply_plan call.

    `created_slot_ids` and `updated_slot_ids` are positional matches against
    the input Plan's `slot_writes` order — index N of the input list maps
    to the same index in the relevant output list.

    `reflow_hash` is the UUID stamped onto every SlotWrite. Caller saves
    this for the echo guard's recent-hashes set.

    `errors` is a list of human-readable failure messages. Empty on success.
    """

    created_slot_ids: list[str] = field(default_factory=list)
    updated_slot_ids: list[str] = field(default_factory=list)
    reflow_hash: str = ""
    errors: list[str] = field(default_factory=list)

    @property
    def success(self) -> bool:
        return not self.errors


# ─────────────────────────────────────────────────────────────────────────
# Column value serializer
# ─────────────────────────────────────────────────────────────────────────


def _build_column_values(
    write: SlotWrite,
    settings: Settings,
    reflow_hash: str,
) -> dict[str, Any]:
    """Convert a SlotWrite's non-None fields to a Monday column_values dict.

    `reflow_hash` overrides any caller-provided value — the apply_plan
    stamps every write with the same hash so the echo guard recognizes
    engine-originated changes.

    `fields_to_clear` produces explicit nulls for those columns.
    """
    cv: dict[str, Any] = {}

    if write.machine_id is not None:
        cv[settings.col_schedule_machine] = {"item_ids": [int(write.machine_id)]}

    if write.job_reference_id is not None and write.job_reference_id != "__simulate__":
        cv[settings.col_schedule_job_reference] = {"item_ids": [int(write.job_reference_id)]}

    if write.stage_id is not None:
        cv[settings.col_schedule_stage_id] = write.stage_id

    if write.recipe_key is not None:
        cv[settings.col_schedule_recipe_key] = write.recipe_key

    if write.recipe_version is not None:
        cv[settings.col_schedule_recipe_version] = str(write.recipe_version)

    if write.quantity is not None:
        cv[settings.col_schedule_quantity] = str(write.quantity)

    if write.planned_start is not None:
        cv[settings.col_schedule_planned_start] = local_to_monday(
            write.planned_start, settings.factory_tz
        )

    if write.planned_end is not None:
        cv[settings.col_schedule_planned_end] = local_to_monday(
            write.planned_end, settings.factory_tz
        )

    if write.actual_start is not None:
        cv[settings.col_schedule_actual_start] = local_to_monday(
            write.actual_start, settings.factory_tz
        )

    if write.actual_end is not None:
        cv[settings.col_schedule_actual_end] = local_to_monday(
            write.actual_end, settings.factory_tz
        )

    if write.dependent_on_ids is not None:
        cv[settings.col_schedule_dependent_on] = {
            "item_ids": [int(x) for x in write.dependent_on_ids]
        }

    if write.status is not None:
        cv[settings.col_schedule_status] = {"label": write.status.value}

    if write.manually_placed is not None:
        cv[settings.col_schedule_manually_placed] = {
            "checked": "true" if write.manually_placed else "false"
        }

    if write.priority is not None:
        cv[settings.col_schedule_priority] = {"label": write.priority.value}

    if write.drift_last_detected_at is not None:
        cv[settings.col_schedule_drift_last_detected_at] = local_to_monday(
            write.drift_last_detected_at, settings.factory_tz
        )

    # Echo-guard hash always present on engine writes.
    cv[settings.col_schedule_last_reflow_hash] = reflow_hash

    # Explicit field clearing — overrides any value set above.
    for field_name in write.fields_to_clear:
        col_id = _slot_field_to_column_id(field_name, settings)
        if col_id is not None:
            cv[col_id] = None

    return cv


def _slot_field_to_column_id(field_name: str, settings: Settings) -> str | None:
    """Map a SlotWrite attribute name to its Monday column ID. None if unmapped."""
    mapping = {
        "machine_id": settings.col_schedule_machine,
        "job_reference_id": settings.col_schedule_job_reference,
        "stage_id": settings.col_schedule_stage_id,
        "recipe_key": settings.col_schedule_recipe_key,
        "recipe_version": settings.col_schedule_recipe_version,
        "quantity": settings.col_schedule_quantity,
        "planned_start": settings.col_schedule_planned_start,
        "planned_end": settings.col_schedule_planned_end,
        "actual_start": settings.col_schedule_actual_start,
        "actual_end": settings.col_schedule_actual_end,
        "dependent_on_ids": settings.col_schedule_dependent_on,
        "status": settings.col_schedule_status,
        "manually_placed": settings.col_schedule_manually_placed,
        "priority": settings.col_schedule_priority,
        "last_reflow_hash": settings.col_schedule_last_reflow_hash,
        "drift_last_detected_at": settings.col_schedule_drift_last_detected_at,
    }
    return mapping.get(field_name)


# ─────────────────────────────────────────────────────────────────────────
# Mutation builders
# ─────────────────────────────────────────────────────────────────────────


def _build_batched_mutation(
    plan: Plan,
    settings: Settings,
    reflow_hash: str,
) -> tuple[str, dict[str, Any], list[tuple[int, str]]]:
    """Build one GraphQL mutation that creates and/or updates all slots.

    Returns (mutation_string, variables, aliases) where `aliases` is a list
    of (plan_index, alias_name) pairs so the caller can correlate response
    fields back to the input Plan order.
    """
    pieces: list[str] = []
    variables: dict[str, Any] = {}
    aliases: list[tuple[int, str]] = []
    board_id_str = str(settings.gray_space_schedule_board)

    for idx, write in enumerate(plan.slot_writes):
        alias = f"w{idx}"
        cv = _build_column_values(write, settings, reflow_hash)
        cv_var = f"cv_{idx}"
        variables[cv_var] = json.dumps(cv)

        if write.slot_id is None:
            # Create
            name_var = f"name_{idx}"
            variables[name_var] = write.name or f"slot-{idx}"
            pieces.append(
                f"{alias}: create_item("
                f"board_id: {board_id_str}, "
                f"item_name: ${name_var}, "
                f"column_values: ${cv_var}"
                f") {{ id }}"
            )
            variables[f"_name_{idx}_type"] = None  # placeholder; types below
            aliases.append((idx, alias))
        else:
            # Update
            item_var = f"item_{idx}"
            variables[item_var] = str(write.slot_id)
            pieces.append(
                f"{alias}: change_multiple_column_values("
                f"board_id: {board_id_str}, "
                f"item_id: ${item_var}, "
                f"column_values: ${cv_var}"
                f") {{ id }}"
            )
            aliases.append((idx, alias))

    if not pieces:
        return "", {}, []

    # Strip the placeholder _name_<idx>_type entries (unused; left in dict
    # to keep the loop tidy above).
    variables = {k: v for k, v in variables.items() if not k.startswith("_")}

    # Build the GraphQL variable declarations.
    var_decls: list[str] = []
    for k in variables:
        if k.startswith("cv_") or k.startswith("name_"):
            var_decls.append(f"${k}: JSON!" if k.startswith("cv_") else f"${k}: String!")
        elif k.startswith("item_"):
            var_decls.append(f"${k}: ID!")

    mutation = f"mutation({', '.join(var_decls)}) {{\n  " + "\n  ".join(pieces) + "\n}"
    return mutation, variables, aliases


# ─────────────────────────────────────────────────────────────────────────
# Public entry point
# ─────────────────────────────────────────────────────────────────────────


async def apply_plan(
    plan: Plan,
    *,
    client: MondayClient | None = None,
    settings: Settings | None = None,
    reflow_hash: str | None = None,
) -> ApplyResult:
    """Execute a Plan against Monday.

    All slot_writes are sent in one batched GraphQL mutation. Each write is
    stamped with `last_reflow_hash` so the echo guard can recognize
    engine-originated changes when their `Schedule item modified` webhook
    bounces back.

    If `client` is None, a short-lived Gray Space client is opened for the
    duration of the call.
    """
    s = settings or get_settings()
    rh = reflow_hash or uuid.uuid4().hex

    # Codex E4 review #7: machine_writes was silently dropped. Fail loudly
    # until the IO shell actually applies them (needed for "Last job ended
    # at" updates when actual_end work lands).
    if plan.machine_writes:
        raise NotImplementedError(
            f"apply_plan: Plan.machine_writes not yet implemented "
            f"(got {len(plan.machine_writes)} entries). "
            f"Implement Capacity Engine writes before producing them."
        )

    if not plan.slot_writes:
        return ApplyResult(reflow_hash=rh)

    mutation, variables, aliases = _build_batched_mutation(plan, s, rh)

    async def _run(c: MondayClient) -> ApplyResult:
        try:
            data, gql_errors = await c.query_collecting_errors(mutation, variables=variables)
        except Exception as e:
            log.exception("apply_plan transport failure")
            return ApplyResult(reflow_hash=rh, errors=[f"GraphQL transport error: {e}"])

        # Index GraphQL errors by the alias they apply to. Monday returns a
        # `path` array; the first element is the alias for aliased mutations.
        errors_by_alias: dict[str, list[str]] = {}
        unrouted_errors: list[str] = []  # errors with no parseable alias path
        for err in gql_errors:
            msg = err.get("message", "Monday returned an unspecified error")
            path = err.get("path") or []
            if path and isinstance(path[0], str):
                errors_by_alias.setdefault(path[0], []).append(msg)
            else:
                unrouted_errors.append(msg)

        created: list[str] = []
        updated: list[str] = []
        errors: list[str] = []
        for idx, alias in aliases:
            payload = data.get(alias)
            alias_errors = errors_by_alias.get(alias) or []
            write = plan.slot_writes[idx]
            if payload and "id" in payload:
                slot_id = str(payload["id"])
                if write.slot_id is None:
                    created.append(slot_id)
                else:
                    updated.append(slot_id)
                # Defensive: Monday could theoretically return both an id and
                # an error for the same alias. Surface that so it's visible.
                if alias_errors:
                    errors.append(
                        f"slot index {idx} (alias {alias}) wrote id={slot_id} "
                        f"but Monday also returned errors: {'; '.join(alias_errors)}"
                    )
            else:
                # Missing or null payload — this write did not land in Monday.
                # Per Monday's batched-mutation semantics, earlier aliases in
                # the same query may have already succeeded; their reflow_hash
                # is already stamped. No rollback — operator reconciles.
                target = (
                    f"create (job={write.job_reference_id})"
                    if write.slot_id is None
                    else f"update slot_id={write.slot_id}"
                )
                detail = "; ".join(alias_errors) if alias_errors else (
                    "no id returned and no per-alias error from Monday"
                )
                errors.append(
                    f"slot index {idx} (alias {alias}) {target} failed: {detail}"
                )

        # Top-level errors with no alias path apply to the whole batch.
        for msg in unrouted_errors:
            errors.append(f"batch-level error: {msg}")

        if errors:
            log.error(
                "apply_plan partial/full failure: "
                "created=%d updated=%d failed=%d reflow_hash=%s",
                len(created), len(updated),
                len(aliases) - len(created) - len(updated),
                rh,
            )

        return ApplyResult(
            created_slot_ids=created,
            updated_slot_ids=updated,
            reflow_hash=rh,
            errors=errors,
        )

    if client is not None:
        return await _run(client)
    async with gray_space_client() as c:
        return await _run(c)
