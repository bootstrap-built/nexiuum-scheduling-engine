"""Phase 2D — Spec Sheet → ScheduleNewOrder translation tests.

Pure-core tests (no IO). Covers:
- Payload JSON parsing + schema mismatch errors
- Recipe derivation per product type (Tablets supported, others raise)
- Manufacturing route filter (Manufacturing+Packaging schedules, Samples
  skips, Hot Shot → Expedite, missing → default)
- Packaging breakdown mapping (form's freeform packaging_type → engine
  ProcessGroup; case-insensitive + plural-tolerant)
- items_per_container passthrough (the explicit numeric replacing the
  old config_notes regex)
- build_schedule_order end-to-end
"""
from __future__ import annotations

import json

import pytest

from engine.core.spec_sheet import (
    DEFAULT_ROUTE,
    SpecSheetParseError,
    UnknownPackagingTypeError,
    UnsupportedManufacturingRouteError,
    UnsupportedProductTypeError,
    build_packaging_breakdown,
    build_schedule_order,
    derive_recipe_key,
    parse_spec_sheet_payload,
    primary_active_mg,
    resolve_route,
)
from engine.models import PackagingSlice, Priority


# ─── Helpers ───────────────────────────────────────────────────────────────


def _payload_dict(**overrides) -> dict:
    """Form-shape payload with sensible defaults for a tablet order.

    Mirrors the spec-sheet-form's SubmissionPayload model_dump shape.
    Override any field via kwargs.
    """
    base = {
        "product_type": "Tablets",
        "tablet_size": "12mm Bisect",
        "is_dual": False,
        "manufacturing_route": "Manufacturing + Packaging",
        "actives": [{"name": "Caffeine", "mg": 200}],
        "packaging_type": "Blister",
        "flavors": [
            {
                "flavor": "Strawberry",
                "qty": 1_000_000,
                "packaging_breakdown": [],
            }
        ],
        "flavor_index": 0,
    }
    base.update(overrides)
    return base


def _payload_json(**overrides) -> str:
    return json.dumps(_payload_dict(**overrides))


# ─── Parsing ───────────────────────────────────────────────────────────────


def test_parse_minimal_payload():
    p = parse_spec_sheet_payload(_payload_json())
    assert p.product_type == "Tablets"
    assert p.flavor_index == 0
    assert len(p.flavors) == 1
    assert p.flavors[0].qty == 1_000_000


def test_parse_rejects_empty_string():
    with pytest.raises(SpecSheetParseError, match="empty"):
        parse_spec_sheet_payload("")


def test_parse_rejects_invalid_json():
    with pytest.raises(SpecSheetParseError, match="valid JSON"):
        parse_spec_sheet_payload("{not-json")


def test_parse_rejects_schema_mismatch():
    # flavor_index is required
    bad = _payload_dict()
    del bad["flavor_index"]
    with pytest.raises(SpecSheetParseError, match="schema"):
        parse_spec_sheet_payload(json.dumps(bad))


def test_parse_ignores_unknown_fields():
    """Form may add fields the engine doesn't read — model_config extra=ignore
    keeps the engine robust to forward-compat schema drift."""
    payload = _payload_dict(future_field="some new thing")
    p = parse_spec_sheet_payload(json.dumps(payload))
    assert p.product_type == "Tablets"


# ─── Recipe derivation ─────────────────────────────────────────────────────


def test_recipe_key_tablets_supported():
    p = parse_spec_sheet_payload(_payload_json())
    assert derive_recipe_key(p) == "tablet-press-standard"


@pytest.mark.parametrize("product", ["Capsules", "Pouches", "Liquids", "Stick Packs", "Loose Powder"])
def test_recipe_key_other_products_raise(product):
    """MVP only ships Tablets. Other products surface a clear error so ops
    knows the corresponding recipe still needs to be authored."""
    # Different product types have different required fields, but we only
    # need a parsable payload for derive_recipe_key — give it minimal
    # extras to satisfy basic schema requirements.
    extras = {"product_type": product}
    if product == "Capsules":
        extras["capsule_size"] = "0"
    if product == "Pouches":
        extras["pouch_subtype"] = "Honey"
        extras["size_value"] = 60
        extras["size_unit"] = "ml"
    if product in ("Liquids", "Stick Packs", "Loose Powder"):
        extras["size_value"] = 10
        extras["size_unit"] = "ml" if product != "Loose Powder" else "g"
    p = parse_spec_sheet_payload(_payload_json(**extras))
    with pytest.raises(UnsupportedProductTypeError) as exc:
        derive_recipe_key(p)
    assert exc.value.product_type == product


# ─── Manufacturing route ───────────────────────────────────────────────────


def test_route_manufacturing_plus_packaging_default():
    p = parse_spec_sheet_payload(_payload_json(manufacturing_route="Manufacturing + Packaging"))
    should, press, prio = resolve_route(p)
    assert should is True
    assert press is True
    assert prio == Priority.NORMAL


def test_route_packaging_only_skips_press():
    p = parse_spec_sheet_payload(_payload_json(manufacturing_route="Packaging"))
    _should, press, _prio = resolve_route(p)
    assert press is False


def test_route_samples_does_not_schedule():
    p = parse_spec_sheet_payload(_payload_json(manufacturing_route="Samples"))
    should, _press, _prio = resolve_route(p)
    assert should is False


def test_route_hot_shot_expedite():
    p = parse_spec_sheet_payload(_payload_json(manufacturing_route="Hot Shot"))
    should, press, prio = resolve_route(p)
    assert should is True
    assert press is True
    assert prio == Priority.EXPEDITE


def test_route_missing_defaults_to_manufacturing_plus_packaging():
    p = parse_spec_sheet_payload(_payload_json(manufacturing_route=""))
    should, press, _prio = resolve_route(p)
    # Default route is the most common — schedule with press.
    assert should is True
    assert press is True
    # The default constant is documented; sanity check it.
    assert DEFAULT_ROUTE == "Manufacturing + Packaging"


def test_route_unknown_raises():
    p = parse_spec_sheet_payload(_payload_json(manufacturing_route="Bogus Route"))
    with pytest.raises(UnsupportedManufacturingRouteError):
        resolve_route(p)


# ─── Packaging breakdown mapping ───────────────────────────────────────────


def test_breakdown_empty_returns_empty_tuple():
    p = parse_spec_sheet_payload(_payload_json())
    assert build_packaging_breakdown(p) == ()


def test_breakdown_50_50_clamshell_sachet():
    payload = _payload_dict()
    payload["flavors"][0]["packaging_breakdown"] = [
        {
            "packaging_type": "Clamshell", "qty": 500_000,
            "items_per_container": 3, "config_notes": "3ct diamond",
        },
        {
            "packaging_type": "Sachet", "qty": 500_000,
            "items_per_container": 5, "config_notes": "5ct",
        },
    ]
    p = parse_spec_sheet_payload(json.dumps(payload))
    breakdown = build_packaging_breakdown(p)
    assert breakdown == (
        PackagingSlice("Clamshell", 500_000, 3, "3ct diamond"),
        PackagingSlice("Sachet", 500_000, 5, "5ct"),
    )


@pytest.mark.parametrize("form_value,expected", [
    ("Clamshell", "Clamshell"),
    ("clamshell", "Clamshell"),
    ("CLAMSHELL", "Clamshell"),
    ("Clamshells", "Clamshell"),     # form's existing dropdown uses plural
    ("Sachet", "Sachet"),
    ("Sachets", "Sachet"),
    ("Blister", "Blister"),
    ("Blisters", "Blister"),
    ("Bottle", "Bottle"),
    ("Bottles", "Bottle"),
])
def test_breakdown_packaging_type_normalization(form_value, expected):
    payload = _payload_dict()
    payload["flavors"][0]["packaging_breakdown"] = [
        {"packaging_type": form_value, "qty": 100, "items_per_container": 5},
    ]
    p = parse_spec_sheet_payload(json.dumps(payload))
    slices = build_packaging_breakdown(p)
    assert slices[0].machine_class == expected


def test_breakdown_unknown_packaging_type_raises():
    payload = _payload_dict()
    payload["flavors"][0]["packaging_breakdown"] = [
        {"packaging_type": "Crate", "qty": 100, "items_per_container": 1},
    ]
    p = parse_spec_sheet_payload(json.dumps(payload))
    with pytest.raises(UnknownPackagingTypeError) as exc:
        build_packaging_breakdown(p)
    assert exc.value.packaging_type == "Crate"


def test_breakdown_items_per_container_default_one():
    """If the form (or an older submission) omits items_per_container, the
    engine assumes 1 — matches the form's default and prevents a
    divide-by-zero / scheduling-everything-instantly bug."""
    payload = _payload_dict()
    payload["flavors"][0]["packaging_breakdown"] = [
        {"packaging_type": "Sachet", "qty": 1000},  # no items_per_container key
    ]
    p = parse_spec_sheet_payload(json.dumps(payload))
    slices = build_packaging_breakdown(p)
    assert slices[0].items_per_container == 1


def test_breakdown_uses_indexed_flavor():
    """flavor_index picks which flavor's breakdown to use."""
    payload = _payload_dict(
        flavor_index=1,
        flavors=[
            {"flavor": "Strawberry", "qty": 100, "packaging_breakdown": [
                {"packaging_type": "Clamshell", "qty": 100, "items_per_container": 5},
            ]},
            {"flavor": "Blueberry", "qty": 200, "packaging_breakdown": [
                {"packaging_type": "Sachet", "qty": 200, "items_per_container": 1},
            ]},
        ],
    )
    p = parse_spec_sheet_payload(json.dumps(payload))
    slices = build_packaging_breakdown(p)
    assert len(slices) == 1
    assert slices[0].machine_class == "Sachet"


def test_breakdown_out_of_range_flavor_index_raises():
    payload = _payload_dict(flavor_index=5)  # only 1 flavor
    p = parse_spec_sheet_payload(json.dumps(payload))
    with pytest.raises(SpecSheetParseError, match="flavor_index"):
        build_packaging_breakdown(p)


# ─── Active mg ─────────────────────────────────────────────────────────────


def test_primary_active_picks_highest_mg():
    payload = _payload_dict(actives=[
        {"name": "Caffeine", "mg": 100},
        {"name": "L-Theanine", "mg": 200},
        {"name": "B12", "mg": 50},
    ])
    p = parse_spec_sheet_payload(json.dumps(payload))
    assert primary_active_mg(p) == 200.0


def test_primary_active_none_when_actives_empty():
    payload = _payload_dict(actives=[])
    p = parse_spec_sheet_payload(json.dumps(payload))
    assert primary_active_mg(p) is None


# ─── End-to-end builder ────────────────────────────────────────────────────


def test_build_schedule_order_happy_path():
    payload = _payload_dict()
    payload["flavors"][0]["packaging_breakdown"] = [
        {"packaging_type": "Clamshell", "qty": 500_000, "items_per_container": 3},
        {"packaging_type": "Sachet",    "qty": 500_000, "items_per_container": 5},
    ]
    p = parse_spec_sheet_payload(json.dumps(payload))
    order = build_schedule_order(p, job_reference_id="ps-12345")

    assert order.job_reference_id == "ps-12345"
    assert order.recipe_key == "tablet-press-standard"
    assert order.recipe_version == 1
    assert order.quantity == 1_000_000  # full flavor qty
    assert order.dual_sided is False
    assert order.active_mg == 200.0
    assert len(order.packaging_breakdown) == 2


def test_build_schedule_order_samples_route_raises():
    p = parse_spec_sheet_payload(_payload_json(manufacturing_route="Samples"))
    with pytest.raises(UnsupportedManufacturingRouteError):
        build_schedule_order(p, job_reference_id="ps-9999")


def test_build_schedule_order_unsupported_product_raises():
    payload = _payload_dict(product_type="Capsules", capsule_size="0")
    p = parse_spec_sheet_payload(json.dumps(payload))
    with pytest.raises(UnsupportedProductTypeError):
        build_schedule_order(p, job_reference_id="ps-9998")


def test_build_schedule_order_propagates_dual_sided():
    p = parse_spec_sheet_payload(_payload_json(is_dual=True))
    order = build_schedule_order(p, job_reference_id="ps-1")
    assert order.dual_sided is True


def test_build_schedule_order_sets_origin_instance_nexiuum():
    """Phase 2D orders originate on the Nexiuum Production Schedule board, so the
    order's origin_instance is 'nexiuum'. This is what lets the apply layer skip
    the Job Reference link on the Gray Space press slot (#9)."""
    p = parse_spec_sheet_payload(_payload_json())
    order = build_schedule_order(p, job_reference_id="ps-1")
    assert order.origin_instance == "nexiuum"


def test_build_schedule_order_threads_n_number():
    """The N# the IO shell read off the PS item lands on the order."""
    p = parse_spec_sheet_payload(_payload_json())
    order = build_schedule_order(p, job_reference_id="ps-1", n_number="N3629")
    assert order.n_number == "N3629"


def test_build_schedule_order_n_number_defaults_to_none():
    """Legacy / unlinked items: no N# supplied → order.n_number is None,
    never raises (it's a label, not a key)."""
    p = parse_spec_sheet_payload(_payload_json())
    order = build_schedule_order(p, job_reference_id="ps-1")
    assert order.n_number is None


def test_build_schedule_order_extracts_flavor():
    """The order carries the indexed flavor's name from the payload."""
    p = parse_spec_sheet_payload(_payload_json())  # flavors[0].flavor == "Strawberry"
    order = build_schedule_order(p, job_reference_id="ps-1")
    assert order.flavor == "Strawberry"


def test_build_schedule_order_flavor_uses_indexed_entry():
    """flavor_index selects which flavor's name lands on the order —
    the same entry whose qty/breakdown the order already uses."""
    payload = _payload_dict(
        flavor_index=1,
        flavors=[
            {"flavor": "Strawberry", "qty": 100},
            {"flavor": "Blueberry Banana", "qty": 200},
        ],
    )
    p = parse_spec_sheet_payload(json.dumps(payload))
    order = build_schedule_order(p, job_reference_id="ps-1")
    assert order.flavor == "Blueberry Banana"
    assert order.quantity == 200  # same indexed entry the qty comes from


def test_build_schedule_order_out_of_range_flavor_index_raises():
    """flavor_index past the end still raises the existing parse error,
    unchanged by flavor extraction (the breakdown guard fires first)."""
    payload = _payload_dict(flavor_index=5)  # only 1 flavor
    p = parse_spec_sheet_payload(json.dumps(payload))
    with pytest.raises(SpecSheetParseError, match="flavor_index"):
        build_schedule_order(p, job_reference_id="ps-1")
