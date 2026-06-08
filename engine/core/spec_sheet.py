"""Spec Sheet → ScheduleNewOrder translation (Phase 2D).

Pure module. No IO. Tests pass JSON strings and snapshots in; this module
returns events ready for the worker queue.

Inputs:
- The JSON string from a Production Schedule item's `Spec Sheet Payload`
  long-text column (written by the spec sheet form at submission time).

Outputs:
- A `ScheduleNewOrder` event the worker can hand to `plan_for_new_order`.

Translation responsibilities:
1. Parse the JSON into a `SpecSheetPayload` (local Pydantic model — we
   don't import the form repo's schema because cross-repo dependency
   couples release cycles. The two schemas must be kept in sync; tests
   pin the field contract.)
2. Derive a recipe_key from `product_type` (+ size descriptors for future
   product-specific recipes). MVP supports Tablets only; other product
   types raise `UnsupportedProductTypeError`.
3. Apply the manufacturing-route filter — some routes shouldn't trigger
   scheduling at all (Samples), some skip the press stage (Kitting Only).
4. Build the `PackagingSlice` tuple from the flavor's
   `packaging_breakdown`. Maps form's freeform `packaging_type` string
   to engine's `ProcessGroup` literal.
"""
from __future__ import annotations

import json
import logging
from typing import Annotated

from pydantic import BaseModel, Field, ValidationError

from engine.models import (
    PackagingSlice,
    Priority,
    ProcessGroup,
    ScheduleNewOrder,
)

log = logging.getLogger(__name__)


# ─── Local mirror of the form's payload (only fields engine needs) ─────────


PositiveInt = Annotated[int, Field(gt=0)]


class _ActiveRow(BaseModel):
    """Subset of the form's ActiveRow — engine only needs mg for the
    `active_mg > 80` force-route rule."""

    name: str = ""
    mg: int = 0


class _PackagingAllocation(BaseModel):
    """Subset of the form's PackagingAllocation."""

    packaging_type: str
    qty: PositiveInt
    items_per_container: int = 1
    config_notes: str = ""


class _FlavorRow(BaseModel):
    """Subset of the form's FlavorRow."""

    flavor: str
    qty: PositiveInt
    packaging_breakdown: list[_PackagingAllocation] = []


class SpecSheetPayload(BaseModel):
    """Engine's view of the Spec Sheet Payload JSON.

    Mirrors the spec-sheet-form's SubmissionPayload schema for the fields
    the engine cares about — drops Customer/Bill-To/Ship-To/Deal IDs and
    other AM-bookkeeping fields. If the form adds a field the engine
    needs, add it here too (and add a contract test).
    """

    product_type: str  # Tablets / Capsules / Pouches / Liquids / Stick Packs / Loose Powder
    tablet_size: str = ""  # composite "12mm Bisect" — used in future product-specific recipe keys
    is_dual: bool = False
    manufacturing_route: str = ""
    actives: list[_ActiveRow] = []
    packaging_type: str = ""  # header default; ignored when breakdown is non-empty
    flavors: list[_FlavorRow]
    flavor_index: int  # which entry in flavors[] this Production Schedule item represents

    model_config = {
        # Don't reject fields the form added that engine doesn't read yet.
        "extra": "ignore",
    }


# ─── Errors ────────────────────────────────────────────────────────────────


class SpecSheetParseError(ValueError):
    """Raised when the JSON in the long-text column can't be parsed or
    fails schema validation."""


class UnsupportedProductTypeError(RuntimeError):
    """Raised when the order's product_type doesn't yet have a recipe.

    MVP supports Tablets only. Capsules/Pouches/Liquids/Stick Packs/
    Loose Powder will raise this until corresponding recipes are
    authored on the Process Recipe board AND added to
    `RECIPE_KEY_BY_PRODUCT_TYPE` below.
    """

    def __init__(self, product_type: str):
        self.product_type = product_type
        super().__init__(
            f"product_type={product_type!r} has no engine recipe mapping yet. "
            f"Author a recipe on Process Recipe + add to "
            f"RECIPE_KEY_BY_PRODUCT_TYPE in spec_sheet.py."
        )


class UnsupportedManufacturingRouteError(RuntimeError):
    """Raised when the manufacturing_route shouldn't schedule (e.g.,
    Samples — too small to be worth a slot). The worker catches this
    and acknowledges the webhook without enqueueing work."""

    def __init__(self, route: str):
        self.route = route
        super().__init__(f"manufacturing_route={route!r} skipped (not scheduled)")


class UnknownPackagingTypeError(RuntimeError):
    """Raised when a packaging_type string in the breakdown can't be
    mapped to a known ProcessGroup. Surfaces as a clear ops-facing
    error rather than silently routing to the wrong machine class."""

    def __init__(self, packaging_type: str):
        self.packaging_type = packaging_type
        super().__init__(
            f"unknown packaging_type={packaging_type!r}; expected one of "
            f"Clamshell / Sachet / Blister / Bottle (case-insensitive, "
            f"plural-tolerant)."
        )


# ─── Static configuration ──────────────────────────────────────────────────


# Recipe key per product type. MVP ships with Tablets only — the other
# product types need their own recipes authored on the Process Recipe
# board before they can route. Add entries here as recipes land.
RECIPE_KEY_BY_PRODUCT_TYPE: dict[str, str] = {
    "Tablets": "tablet-press-standard",
}

# Manufacturing route → (should_schedule, include_press, priority).
# KEYS MUST MATCH the form's route list verbatim — nexiuum-spec-sheet-form's
# app/route_metadata.py MANUFACTURING_ROUTE_OPTIONS is the source of truth. The
# 2026-06-03 meeting relabeled "Packaging" → "Kitting" form-side (PR #25); the
# engine drifted and silently skipped every "* Kitting" order as an unknown
# route. Mirror of route_metadata's presses/packages axes:
#  - Manufacturing / Manufacturing + Kitting / Keep for Kitting / Ship Bulk
#    all press (include_press=True).
#  - Kitting Only is already-pressed inventory → skips press.
#  - Samples is small enough that ops handles it outside the scheduler.
ROUTE_RULES: dict[str, tuple[bool, bool, Priority]] = {
    "Manufacturing":           (True,  True,  Priority.NORMAL),
    "Manufacturing + Kitting": (True,  True,  Priority.NORMAL),
    "Kitting Only":            (True,  False, Priority.NORMAL),
    "Keep for Kitting":        (True,  True,  Priority.NORMAL),
    "Ship Bulk":               (True,  True,  Priority.NORMAL),
    "Samples":                 (False, False, Priority.NORMAL),
}
# Empty/missing route is treated as "Manufacturing + Kitting" — the most common
# case. Better default than silently rejecting.
DEFAULT_ROUTE = "Manufacturing + Kitting"


# Maps form-side packaging type string → engine ProcessGroup. Case-
# insensitive comparison + plural-tolerant lookup happens in
# `_normalize_packaging_type`.
_PACKAGING_GROUP_BY_NAME: dict[str, ProcessGroup] = {
    "clamshell": "Clamshell",
    "sachet":    "Sachet",
    "blister":   "Blister",
    "bottle":    "Bottle",
}


# ─── Public API ────────────────────────────────────────────────────────────


def parse_spec_sheet_payload(json_str: str) -> SpecSheetPayload:
    """Parse the long-text column JSON into a SpecSheetPayload.

    Raises `SpecSheetParseError` on JSON errors or schema validation
    failures. The caller (worker) maps this to a 400-class log entry —
    a malformed payload is operator data error, not engine fault.
    """
    if not json_str or not json_str.strip():
        raise SpecSheetParseError("spec sheet payload column is empty")
    try:
        obj = json.loads(json_str)
    except json.JSONDecodeError as e:
        raise SpecSheetParseError(f"payload is not valid JSON: {e}") from e
    try:
        return SpecSheetPayload.model_validate(obj)
    except ValidationError as e:
        raise SpecSheetParseError(f"payload schema mismatch: {e}") from e


def derive_recipe_key(payload: SpecSheetPayload) -> str:
    """Map product_type → recipe_key.

    MVP: only Tablets has a recipe. Future products extend
    RECIPE_KEY_BY_PRODUCT_TYPE. tablet_size / capsule_size etc. could
    further differentiate (e.g., "tablet-press-12mm") once we have
    per-size recipes; for now Tablets is one global recipe.
    """
    if payload.product_type in RECIPE_KEY_BY_PRODUCT_TYPE:
        return RECIPE_KEY_BY_PRODUCT_TYPE[payload.product_type]
    raise UnsupportedProductTypeError(payload.product_type)


def resolve_route(payload: SpecSheetPayload) -> tuple[bool, bool, Priority]:
    """Returns (should_schedule, include_press, priority).

    Defaults to "Manufacturing + Kitting" when route is missing
    (rather than skipping work — too easy to lose orders to a typo).
    Unknown non-empty route strings raise — better to surface an explicit
    error than to default-route unfamiliar workflow tags.
    """
    route = payload.manufacturing_route or DEFAULT_ROUTE
    if route in ROUTE_RULES:
        return ROUTE_RULES[route]
    raise UnsupportedManufacturingRouteError(route)


def build_packaging_breakdown(
    payload: SpecSheetPayload,
) -> tuple[PackagingSlice, ...]:
    """Pull the breakdown from the indexed flavor and map to engine PackagingSlice tuple.

    Returns an empty tuple when the indexed flavor has no breakdown
    (single-packaging order using the header packaging_type — handled
    upstream by the engine treating the header as the implicit single
    slice).
    """
    if not 0 <= payload.flavor_index < len(payload.flavors):
        raise SpecSheetParseError(
            f"flavor_index={payload.flavor_index} out of range "
            f"(flavors has {len(payload.flavors)} entries)"
        )
    flavor = payload.flavors[payload.flavor_index]
    if not flavor.packaging_breakdown:
        return ()
    slices: list[PackagingSlice] = []
    for alloc in flavor.packaging_breakdown:
        slices.append(
            PackagingSlice(
                machine_class=_normalize_packaging_type(alloc.packaging_type),
                quantity=alloc.qty,
                items_per_container=max(1, alloc.items_per_container),
                config_notes=alloc.config_notes,
            )
        )
    return tuple(slices)


def primary_active_mg(payload: SpecSheetPayload) -> float | None:
    """Return the highest-mg active ingredient's mg, or None if no actives.

    Engine's `active_mg > 80` force-route rule (Hard Rule 2) reads this.
    """
    if not payload.actives:
        return None
    return float(max(a.mg for a in payload.actives))


def build_schedule_order(
    payload: SpecSheetPayload,
    job_reference_id: str,
    n_number: str | None = None,
) -> ScheduleNewOrder:
    """High-level builder: parse a payload + apply all the translation
    rules to produce a `ScheduleNewOrder` event.

    `n_number` is the originating PO's traceability number, read by the IO
    shell from the Production Schedule item's "Nexiuum #" board_relation.
    Threaded through as a pass-through label; None when the item isn't linked
    to a PO yet (or for the legacy Gray Space flow that doesn't carry one).

    Raises `UnsupportedProductTypeError` or `UnsupportedManufacturingRouteError`
    when the payload shouldn't schedule.
    """
    should_schedule, _include_press, _priority = resolve_route(payload)
    if not should_schedule:
        raise UnsupportedManufacturingRouteError(payload.manufacturing_route or "")

    recipe_key = derive_recipe_key(payload)
    breakdown = build_packaging_breakdown(payload)
    flavor = payload.flavors[payload.flavor_index]

    # Quantity: for the order overall (the press stage), use the flavor's
    # full qty. The breakdown's slice qtys are independent (and may already
    # be set to portions of flavor.qty by the operator).
    return ScheduleNewOrder(
        job_reference_id=job_reference_id,
        recipe_key=recipe_key,
        recipe_version=1,  # MVP — only one version per recipe key today
        quantity=flavor.qty,
        dual_sided=payload.is_dual,
        active_mg=primary_active_mg(payload),
        packaging_breakdown=breakdown,
        n_number=n_number,
        flavor=flavor.flavor,
        # Phase 2D orders originate on the Nexiuum Production Schedule board.
        # The apply layer uses this to drop the Job Reference board_relation
        # link on cross-instance (e.g. Gray Space press) slots (#9).
        origin_instance="nexiuum",
    )


# ─── Internal helpers ──────────────────────────────────────────────────────


def _normalize_packaging_type(raw: str) -> ProcessGroup:
    """Case-insensitive + plural-tolerant lookup against the four engine
    container groups. Tests pin the supported variants.

    "Clamshell" / "Clamshells" / "clamshell" / "CLAMSHELL" all → "Clamshell".
    """
    key = (raw or "").strip().lower()
    if key.endswith("s") and key[:-1] in _PACKAGING_GROUP_BY_NAME:
        key = key[:-1]
    if key in _PACKAGING_GROUP_BY_NAME:
        return _PACKAGING_GROUP_BY_NAME[key]
    raise UnknownPackagingTypeError(raw)
