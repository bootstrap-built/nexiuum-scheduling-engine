"""Pure-core tests for engine.core.labels — slot-name + lane-label composition.

Exhaustively covers the None/blank permutations operators trust visually
(N# present/absent × Flavor present/absent), per parent PRD #2's testing
decisions. Flavor support arrives in a later slice, but the labels module
handles the full permutation set today so that slice is a pure caller change.

Synthetic inputs only — these are pure functions with no IO.
"""

from __future__ import annotations

import pytest

from engine.core.labels import compose_lane_label, compose_slot_name, is_n_number


# ─── is_n_number — the ingest-boundary guard ──────────────────────────────


@pytest.mark.parametrize(
    "value,expected",
    [
        ("N3629", True),
        ("N1", True),
        ("  N42 ", True),       # surrounding whitespace tolerated
        ("N0", True),
        ("n3629", False),       # lowercase — POs issue uppercase
        ("3629", False),        # missing N
        ("N", False),           # N but no digits
        ("N3629-A", False),     # suffix
        ("N1, N2", False),      # multi-linked board_relation render
        ("ROAR LLC", False),    # arbitrary PO-Number text
        ("", False),
        ("   ", False),
        (None, False),
    ],
)
def test_is_n_number(value, expected):
    assert is_n_number(value) is expected


# ─── compose_lane_label — the identity an operator sees on the chart ───────


def test_lane_label_full_n_and_flavor():
    assert compose_lane_label("N12345", "Strawberry Banana", "9001") == (
        "N12345 · Strawberry Banana"
    )


def test_lane_label_n_only_no_separator():
    """N# present, flavor absent → bare N#, no ' · ' separator."""
    assert compose_lane_label("N12345", None, "9001") == "N12345"


def test_lane_label_n_only_blank_flavor_treated_as_absent():
    assert compose_lane_label("N12345", "", "9001") == "N12345"
    assert compose_lane_label("N12345", "   ", "9001") == "N12345"


def test_lane_label_missing_n_falls_back_to_last6_of_slot_id():
    """No N# → '#<last-6-of-slot-id>', regardless of flavor."""
    assert compose_lane_label(None, None, "1234567890") == "#567890"
    # Flavor is ignored when there's no N# to anchor it.
    assert compose_lane_label(None, "Strawberry", "1234567890") == "#567890"


def test_lane_label_blank_n_treated_as_absent():
    assert compose_lane_label("", "Strawberry", "1234567890") == "#567890"
    assert compose_lane_label("   ", None, "1234567890") == "#567890"


def test_lane_label_short_slot_id_uses_whole_id():
    """Slot id shorter than 6 chars → whole id (slicing is safe)."""
    assert compose_lane_label(None, None, "42") == "#42"


def test_lane_label_no_n_no_slot_id_returns_unknown_sentinel():
    """Both N# and slot id absent → defensive '#unknown' (shouldn't happen
    for a real persisted Slot, but never raise)."""
    assert compose_lane_label(None, None, None) == "#unknown"
    assert compose_lane_label(None, None, "") == "#unknown"


# ─── compose_slot_name — the Schedule row name (identity + stage) ──────────


def test_slot_name_full_n_and_flavor_with_stage():
    assert compose_slot_name("N12345", "Strawberry", "press", "9001") == (
        "N12345 · Strawberry → press"
    )


def test_slot_name_n_only_with_stage():
    assert compose_slot_name("N12345", None, "press", "9001") == "N12345 → press"


def test_slot_name_missing_n_falls_back_to_last6_with_stage():
    assert compose_slot_name(None, None, "press", "1234567890") == "#567890 → press"


def test_slot_name_missing_n_and_slot_id_uses_unknown():
    """New placement with no N# and no Slot id yet → '#unknown → stage'."""
    assert compose_slot_name(None, None, "press", None) == "#unknown → press"


def test_slot_name_without_stage_is_just_identity():
    """No stage → identity only, no trailing ' → '."""
    assert compose_slot_name("N12345", "Strawberry", None, "9001") == (
        "N12345 · Strawberry"
    )
    assert compose_slot_name("N12345", None, "", "9001") == "N12345"


def test_slot_name_blank_stage_treated_as_absent():
    assert compose_slot_name("N12345", None, "   ", "9001") == "N12345"


@pytest.mark.parametrize(
    "n_number,flavor,slot_id,expected",
    [
        ("N1", "Cherry", "abcdef", "N1 · Cherry"),
        ("N1", None, "abcdef", "N1"),
        (None, "Cherry", "abcdef", "#abcdef"),
        (None, None, "abcdef", "#abcdef"),
        (None, None, None, "#unknown"),
    ],
)
def test_lane_label_permutation_matrix(n_number, flavor, slot_id, expected):
    """The full N# × Flavor permutation grid the labels module guarantees."""
    assert compose_lane_label(n_number, flavor, slot_id) == expected


def test_slot_name_and_lane_label_share_identity():
    """compose_slot_name's prefix is exactly compose_lane_label's output —
    one rule for 'how does an operator recognise a Slot?'."""
    n, f, sid = "N999", "Lime", "777777"
    lane = compose_lane_label(n, f, sid)
    name = compose_slot_name(n, f, "blister", sid)
    assert name == f"{lane} → blister"
