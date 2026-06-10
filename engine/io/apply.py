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
from dataclasses import dataclass, field, replace
from typing import Any

from engine.config import ScheduleCols, Settings, get_settings
from engine.core.timezone import local_to_monday
from engine.io import recent_writes
from engine.io.monday import MondayClient, gray_space_client, nexiuum_client
from engine.models import MondayInstance, Plan, SlotWrite

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
    rolled_back_slot_ids: list[str] = field(default_factory=list)

    @property
    def success(self) -> bool:
        return not self.errors

    @property
    def orphaned_slot_ids(self) -> list[str]:
        """Slots created on Monday that are still orphaned after a failed apply.

        A non-atomic mid-plan failure can leave just-created slots behind (#9).
        #12 rolls those back: on any apply error, the slots created in the run
        are deleted and recorded in `rolled_back_slot_ids`. This property is the
        residue — created ids that were NOT successfully rolled back (e.g. the
        delete itself failed). Empty on full success AND after a clean rollback;
        non-empty only when real orphans remain, keeping them loud and
        recoverable.
        """
        if not self.errors:
            return []
        rolled_back = set(self.rolled_back_slot_ids)
        return [sid for sid in self.created_slot_ids if sid not in rolled_back]


# ─────────────────────────────────────────────────────────────────────────
# Column value serializer
# ─────────────────────────────────────────────────────────────────────────


def _build_column_values(
    write: SlotWrite,
    cols: ScheduleCols,
    settings: Settings,
    reflow_hash: str,
) -> dict[str, Any]:
    """Convert a SlotWrite's non-None fields to a Monday column_values dict.

    `cols` is the per-instance Schedule column map (so the same SlotWrite
    can be serialized for either Gray Space or Nexiuum Schedule boards).

    `reflow_hash` overrides any caller-provided value — the apply_plan
    stamps every write with the same hash so the echo guard recognizes
    engine-originated changes.

    `fields_to_clear` produces explicit nulls for those columns.
    """
    cv: dict[str, Any] = {}

    if write.machine_id is not None:
        cv[cols.machine] = {"item_ids": [int(write.machine_id)]}

    # Job Reference board_relation: only valid when this slot's target Schedule
    # board is connected to the order's origin board. Each Schedule board's Job
    # Reference column connects to its OWN instance's source board, so the link
    # holds only when the write lands on the origin instance. For a cross-instance
    # slot (e.g. a Nexiuum-origin order's Gray Space press stage) Monday would
    # reject the link with "items that are not in the connected boards" (#9), so
    # we skip it — N#/flavor labels carry the human-facing identity instead.
    if (
        write.job_reference_id is not None
        and write.job_reference_id != "__simulate__"
        and write.instance == write.origin_instance
    ):
        cv[cols.job_reference] = {"item_ids": [int(write.job_reference_id)]}

    if write.stage_id is not None:
        cv[cols.stage_id] = write.stage_id

    if write.recipe_key is not None:
        cv[cols.recipe_key] = write.recipe_key

    if write.recipe_version is not None:
        cv[cols.recipe_version] = str(write.recipe_version)

    if write.quantity is not None:
        cv[cols.quantity] = str(write.quantity)

    if write.planned_start is not None:
        cv[cols.planned_start] = local_to_monday(write.planned_start, settings.factory_tz)

    if write.planned_end is not None:
        cv[cols.planned_end] = local_to_monday(write.planned_end, settings.factory_tz)

    if write.actual_start is not None:
        cv[cols.actual_start] = local_to_monday(write.actual_start, settings.factory_tz)

    if write.actual_end is not None:
        cv[cols.actual_end] = local_to_monday(write.actual_end, settings.factory_tz)

    if write.dependent_on_ids is not None:
        cv[cols.dependent_on] = {"item_ids": [int(x) for x in write.dependent_on_ids]}

    if write.status is not None:
        cv[cols.status] = {"label": write.status.value}

    if write.manually_placed is not None:
        cv[cols.manually_placed] = {
            "checked": "true" if write.manually_placed else "false"
        }

    if write.priority is not None:
        cv[cols.priority] = {"label": write.priority.value}

    if write.drift_last_detected_at is not None:
        cv[cols.drift_last_detected_at] = local_to_monday(
            write.drift_last_detected_at, settings.factory_tz
        )

    if write.n_number is not None:
        cv[cols.n_number] = write.n_number

    if write.flavor is not None:
        cv[cols.flavor] = write.flavor

    # Echo-guard hash always present on engine writes.
    cv[cols.last_reflow_hash] = reflow_hash

    # Explicit field clearing — overrides any value set above.
    for field_name in write.fields_to_clear:
        col_id = _slot_field_to_column_id(field_name, cols)
        if col_id is not None:
            cv[col_id] = None

    return cv


def _slot_field_to_column_id(field_name: str, cols: ScheduleCols) -> str | None:
    """Map a SlotWrite attribute name to its Monday column ID. None if unmapped."""
    mapping = {
        "machine_id": cols.machine,
        "job_reference_id": cols.job_reference,
        "stage_id": cols.stage_id,
        "recipe_key": cols.recipe_key,
        "recipe_version": cols.recipe_version,
        "quantity": cols.quantity,
        "planned_start": cols.planned_start,
        "planned_end": cols.planned_end,
        "actual_start": cols.actual_start,
        "actual_end": cols.actual_end,
        "dependent_on_ids": cols.dependent_on,
        "status": cols.status,
        "manually_placed": cols.manually_placed,
        "priority": cols.priority,
        "last_reflow_hash": cols.last_reflow_hash,
        "drift_last_detected_at": cols.drift_last_detected_at,
        "n_number": cols.n_number,
        "flavor": cols.flavor,
    }
    return mapping.get(field_name)


# ─────────────────────────────────────────────────────────────────────────
# Mutation builders
# ─────────────────────────────────────────────────────────────────────────


def _build_batched_mutation_for_instance(
    indexed_writes: list[tuple[int, SlotWrite]],
    board_id: int,
    cols: ScheduleCols,
    settings: Settings,
    reflow_hash: str,
) -> tuple[str, dict[str, Any], list[tuple[int, str]]]:
    """Build one GraphQL mutation for one instance's Schedule board.

    `indexed_writes` is a list of (original_plan_index, write) pairs — the
    plan index is preserved across instance splits so the caller can map
    aliases back to the input Plan in stable order.

    Returns (mutation_string, variables, aliases) where `aliases` is a list
    of (plan_index, alias_name) pairs.
    """
    pieces: list[str] = []
    variables: dict[str, Any] = {}
    aliases: list[tuple[int, str]] = []
    board_id_str = str(board_id)

    for plan_idx, write in indexed_writes:
        alias = f"w{plan_idx}"
        cv = _build_column_values(write, cols, settings, reflow_hash)
        cv_var = f"cv_{plan_idx}"
        variables[cv_var] = json.dumps(cv)

        if write.slot_id is None:
            # Create
            name_var = f"name_{plan_idx}"
            variables[name_var] = write.name or f"slot-{plan_idx}"
            pieces.append(
                f"{alias}: create_item("
                f"board_id: {board_id_str}, "
                f"item_name: ${name_var}, "
                f"column_values: ${cv_var}"
                f") {{ id }}"
            )
            aliases.append((plan_idx, alias))
        else:
            # Update
            item_var = f"item_{plan_idx}"
            variables[item_var] = str(write.slot_id)
            pieces.append(
                f"{alias}: change_multiple_column_values("
                f"board_id: {board_id_str}, "
                f"item_id: ${item_var}, "
                f"column_values: ${cv_var}"
                f") {{ id }}"
            )
            aliases.append((plan_idx, alias))

    if not pieces:
        return "", {}, []

    # Build the GraphQL variable declarations.
    var_decls: list[str] = []
    for k in variables:
        if k.startswith("cv_"):
            var_decls.append(f"${k}: JSON!")
        elif k.startswith("name_"):
            var_decls.append(f"${k}: String!")
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

    Phase 2: writes are grouped by `SlotWrite.instance` and dispatched as
    one batched mutation per instance against that instance's Schedule
    board using that instance's Monday client. Both instances use the same
    `reflow_hash` so the echo guard recognizes engine-originated changes
    on either side.

    If `client` is passed, it's used for Gray Space writes (legacy callers
    that already have an open Gray Space client). Nexiuum writes always
    open their own short-lived client. If `client` is None, both instances
    open short-lived clients.

    Per-instance mutations execute SEQUENTIALLY (Gray Space first, then
    Nexiuum) so press-stage slot IDs exist by the time packaging-stage
    writes need them in `dependent_on_ids`. Phase 2B doesn't yet do that
    backfill — that's task #10 (baton-pass wiring) — but the ordering
    invariant is in place now so the wiring stays trivial.
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

    # ── Group writes by instance, preserving original plan order ────────
    by_instance: dict[MondayInstance, list[tuple[int, SlotWrite]]] = {
        "gray_space": [],
        "nexiuum": [],
    }
    for idx, write in enumerate(plan.slot_writes):
        by_instance[write.instance].append((idx, write))

    # Pre-flight: if any writes target Nexiuum, dual-instance must be configured.
    if by_instance["nexiuum"] and not s.nexiuum_enabled:
        return ApplyResult(
            reflow_hash=rh,
            errors=[
                f"Plan has {len(by_instance['nexiuum'])} Nexiuum-instance "
                f"writes but Nexiuum config is not enabled (set "
                f"MONDAY_NEXIUUM_TOKEN + 3 board IDs)."
            ],
        )

    aggregate = ApplyResult(reflow_hash=rh)

    # Per-instance results retained so rollback can delete each instance's
    # created slots with that instance's own client (created ids alone don't
    # carry which board/token they belong to).
    per_instance: list[tuple[MondayInstance, ApplyResult]] = []

    # ── Gray Space writes ───────────────────────────────────────────────
    if by_instance["gray_space"]:
        result = await _apply_for_instance(
            indexed_writes=by_instance["gray_space"],
            instance="gray_space",
            plan=plan,
            settings=s,
            reflow_hash=rh,
            client_override=client,
        )
        per_instance.append(("gray_space", result))
        aggregate = _merge_results(aggregate, result)

    # ── Nexiuum writes (sequential after Gray Space) ────────────────────
    # Short-circuit (#12): if Gray Space already errored, do NOT start Nexiuum.
    # A plan is one order; Nexiuum packaging stages depend on the Gray Space
    # press stages that just failed, so any Nexiuum slots would only have to be
    # rolled back. Not creating them is the cleaner half of atomicity ("never
    # created") and shrinks the surface where a fallible rollback delete is
    # needed. Gray Space's own creates are still rolled back below.
    if by_instance["nexiuum"] and not aggregate.errors:
        result = await _apply_for_instance(
            indexed_writes=by_instance["nexiuum"],
            instance="nexiuum",
            plan=plan,
            settings=s,
            reflow_hash=rh,
            client_override=None,  # Nexiuum always opens its own client
        )
        per_instance.append(("nexiuum", result))
        aggregate = _merge_results(aggregate, result)

    if aggregate.errors:
        log.error(
            "apply_plan partial/full failure across instances: "
            "created=%d updated=%d errors=%d reflow_hash=%s",
            len(aggregate.created_slot_ids),
            len(aggregate.updated_slot_ids),
            len(aggregate.errors), rh,
        )
        # #12 atomicity: a non-atomic mid-plan failure leaves the slots already
        # created on Monday as orphans. Roll them back — delete each instance's
        # this-run creates with that instance's client — so a failed apply leaves
        # the boards clean. Only `created_slot_ids` are deleted; updated slots
        # pre-existed and are never touched.
        aggregate = await _rollback(
            aggregate, per_instance, settings=s, client_override=client
        )

        orphans = aggregate.orphaned_slot_ids
        if orphans:
            # Rollback could not remove these (e.g. the delete itself failed) —
            # keep them loud and recoverable rather than silently "clean".
            log.error(
                "apply_plan left %d orphan slot(s) on Monday after rollback "
                "(reflow_hash=%s): %s — created but neither the plan nor the "
                "rollback fully succeeded; manual cleanup required.",
                len(orphans), rh, orphans,
            )

    return aggregate


async def _apply_for_instance(
    *,
    indexed_writes: list[tuple[int, SlotWrite]],
    instance: MondayInstance,
    plan: Plan,
    settings: Settings,
    reflow_hash: str,
    client_override: MondayClient | None,
) -> ApplyResult:
    """Build and execute one batched mutation against the given instance."""
    cols = settings.schedule_cols(instance)
    board_id = settings.schedule_board(instance)
    if board_id <= 0:
        return ApplyResult(
            reflow_hash=reflow_hash,
            errors=[
                f"{instance} Schedule board id is 0 — board not configured."
            ],
        )

    mutation, variables, aliases = _build_batched_mutation_for_instance(
        indexed_writes, board_id, cols, settings, reflow_hash,
    )

    # Echo registry (Codex E4 B1): record updates BEFORE the mutation fires —
    # Monday can dispatch the webhook before our HTTP response is parsed, so
    # recording after would race. A record for a write that then fails is
    # harmless (TTL-bounded suppression of an event that never comes).
    # Creates can't be pre-recorded (no id yet); they're recorded in the
    # alias loop below as ids come back.
    ttl = settings.echo_write_ttl_seconds
    for _, write in indexed_writes:
        if write.slot_id is not None:
            recent_writes.record_write(
                board_id,
                write.slot_id,
                set(_build_column_values(write, cols, settings, reflow_hash)),
                ttl_seconds=ttl,
            )

    async def _run(c: MondayClient) -> ApplyResult:
        try:
            data, gql_errors = await c.query_collecting_errors(mutation, variables=variables)
        except Exception as e:
            log.exception("apply_plan transport failure (%s)", instance)
            return ApplyResult(
                reflow_hash=reflow_hash,
                errors=[f"{instance} GraphQL transport error: {e}"],
            )

        errors_by_alias: dict[str, list[str]] = {}
        unrouted_errors: list[str] = []
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
                    # Engine-created item: any webhook on it inside the TTL
                    # is an echo (creation entries are pulse-scoped).
                    recent_writes.record_write(
                        board_id, slot_id, None, ttl_seconds=ttl,
                    )
                else:
                    updated.append(slot_id)
                if alias_errors:
                    errors.append(
                        f"[{instance}] slot index {idx} (alias {alias}) "
                        f"wrote id={slot_id} but Monday also returned errors: "
                        f"{'; '.join(alias_errors)}"
                    )
            else:
                target = (
                    f"create (job={write.job_reference_id})"
                    if write.slot_id is None
                    else f"update slot_id={write.slot_id}"
                )
                detail = "; ".join(alias_errors) if alias_errors else (
                    "no id returned and no per-alias error from Monday"
                )
                errors.append(
                    f"[{instance}] slot index {idx} (alias {alias}) "
                    f"{target} failed: {detail}"
                )

        for msg in unrouted_errors:
            errors.append(f"[{instance}] batch-level error: {msg}")

        return ApplyResult(
            created_slot_ids=created,
            updated_slot_ids=updated,
            reflow_hash=reflow_hash,
            errors=errors,
        )

    # Use override if given AND the instance matches Gray Space (the legacy
    # caller path). Otherwise open a fresh client of the right flavor.
    if client_override is not None and instance == "gray_space":
        return await _run(client_override)

    client_factory = nexiuum_client if instance == "nexiuum" else gray_space_client
    async with client_factory() as c:
        return await _run(c)


async def _rollback(
    aggregate: ApplyResult,
    per_instance: list[tuple[MondayInstance, ApplyResult]],
    *,
    settings: Settings,
    client_override: MondayClient | None,
) -> ApplyResult:
    """Delete every slot created during a failed apply, per instance.

    Rollback is best-effort and idempotent at the run level: it only deletes
    the ids in each instance's `created_slot_ids` (slots this run created), so
    pre-existing/updated slots are never touched. Slots that delete cleanly are
    recorded in `rolled_back_slot_ids`; anything rollback could not remove stays
    surfaced via `ApplyResult.orphaned_slot_ids` plus an appended error.
    """
    rolled_back: list[str] = []
    rollback_errors: list[str] = []

    for instance, result in per_instance:
        ids = result.created_slot_ids
        if not ids:
            continue
        try:
            # Reuse the legacy override for Gray Space; otherwise open a fresh
            # client of the right flavor — mirrors _apply_for_instance's choice.
            if client_override is not None and instance == "gray_space":
                deleted, errs = await client_override.delete_items(ids)
            else:
                factory = nexiuum_client if instance == "nexiuum" else gray_space_client
                async with factory() as c:
                    deleted, errs = await c.delete_items(ids)
            rolled_back.extend(deleted)
            rollback_errors.extend(
                f"[{instance}] rollback delete failed: {e}" for e in errs
            )
        except Exception as e:  # transport / client-open failure — leave orphans loud
            log.exception("apply_plan rollback transport failure (%s)", instance)
            rollback_errors.append(f"[{instance}] rollback transport error: {e}")

    return replace(
        aggregate,
        errors=aggregate.errors + rollback_errors,
        rolled_back_slot_ids=aggregate.rolled_back_slot_ids + rolled_back,
    )


def _merge_results(a: ApplyResult, b: ApplyResult) -> ApplyResult:
    """Concatenate two per-instance ApplyResults. reflow_hash assumed equal."""
    return ApplyResult(
        created_slot_ids=a.created_slot_ids + b.created_slot_ids,
        updated_slot_ids=a.updated_slot_ids + b.updated_slot_ids,
        reflow_hash=a.reflow_hash,
        errors=a.errors + b.errors,
        rolled_back_slot_ids=a.rolled_back_slot_ids + b.rolled_back_slot_ids,
    )
