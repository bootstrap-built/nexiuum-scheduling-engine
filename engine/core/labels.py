"""Label composition — the single source of truth for how an operator sees
a Slot's identity.

Two pure functions render the operator-facing strings the engine produces:

- ``compose_slot_name`` — the Schedule item's row name (written on every
  SlotWrite the engine creates, and the name surfaced on snapshot reads).
- ``compose_lane_label`` — the Marey chart's lane label.

Both encode the same ``N12345 · Strawberry Banana`` identity format and the
same None-fallback rules, so the rule for "how does an operator recognise a
Slot?" lives in exactly one place (parent PRD #2, decision 12).

Fallback rules (exhaustively, the permutations operators trust visually):

==================  ==================  =================================
N#                  Flavor              identity rendered
==================  ==================  =================================
``N12345``          ``Strawberry``      ``N12345 · Strawberry``
``N12345``          None / ``""``       ``N12345``
None / ``""``       (any)               ``#<last-6-of-slot-id>``
None / ``""``       (any), no slot id   ``#unknown``
==================  ==================  =================================

The N# is the customer-facing key, so when it is present Flavor only ever
*appends* to it — Flavor never stands alone. When N# is absent (the legacy
Gray Space Blend Records flow, which doesn't carry an N# yet) the label
falls back to the last 6 characters of the Slot id, matching the engine's
prior Marey behaviour.

Flavor support arrives in a later slice; today every caller passes
``flavor=None``. The functions handle the full permutation set now so that
slice is a pure caller change.

Truncation is the renderer's job — these functions always return the full
string (a long flavor comes back un-truncated; CSS ellipsis handles overflow
on the chart, and the click popover shows the full name).
"""

from __future__ import annotations

import re

# An N# is the Nexiuum-side PO traceability number: the letter "N" followed by
# digits (e.g. "N3629"). Confirmed against 200 live Production Schedule items —
# 100% match this shape, none multi-linked. Used to guard the *ingest*
# boundaries (the PS board_relation display_value and the legacy Blend Records
# PO-Number enrichment) against malformed or multi-link values: a Monday
# board_relation with two linked POs renders as "N1, N2", which must NOT be
# stamped onto every Slot as the N#. A non-matching value degrades to the
# `#<last-6>` fallback rather than persisting garbage to live board data.
_N_NUMBER_RE = re.compile(r"N\d+")


def is_n_number(value: str | None) -> bool:
    """True iff `value` is a well-formed N# ("N" + digits, after stripping).

    Callers use this at ingest to decide whether an upstream value is a real
    N# (stamp it) or noise (fall back). The `compose_*` functions deliberately
    do NOT call this — they faithfully render whatever identity they're given;
    validation is the caller's job at the data boundary.
    """
    if not value:
        return False
    return _N_NUMBER_RE.fullmatch(value.strip()) is not None


def _compose_identity(
    n_number: str | None,
    flavor: str | None,
    slot_id: str | None,
) -> str:
    """The shared ``N12345 · Flavor`` / ``#last6`` identity core.

    Empty strings are treated the same as None so a blank Monday column
    (which reads back as ``""``) falls through to the next rule rather than
    rendering an empty identity.
    """
    n = (n_number or "").strip()
    if n:
        f = (flavor or "").strip()
        return f"{n} · {f}" if f else n
    sid = (slot_id or "").strip()
    if sid:
        return f"#{sid[-6:]}"
    # No N#, no slot id — only reachable for a not-yet-created Slot with no
    # N# (e.g. a direct /commit of a legacy order). The snapshot re-read will
    # recompose a ``#<last-6>`` identity once Monday assigns the id.
    return "#unknown"


def compose_slot_name(
    n_number: str | None,
    flavor: str | None,
    stage_id: str | None,
    slot_id: str | None,
) -> str:
    """Compose a Schedule item's row name: ``<identity> → <stage_id>``.

    Used by the scheduler when it places a new Slot (SlotWrite.name) and by
    the view layer when it renders a Slot read from a snapshot. The caller is
    responsible for any trailing chunk/config suffix (e.g.
    ``(1/2 · 5ct diamond)``) — this function owns the identity prefix and the
    stage, which is the part that must be consistent everywhere.

    ``stage_id`` is appended after a ``→`` when present; a Slot with no stage
    yet renders just the identity.
    """
    identity = _compose_identity(n_number, flavor, slot_id)
    stage = (stage_id or "").strip()
    return f"{identity} → {stage}" if stage else identity


def compose_lane_label(
    n_number: str | None,
    flavor: str | None,
    slot_id: str | None,
) -> str:
    """Compose the Marey lane label: just the identity (``N12345 · Flavor``
    or the ``#<last-6>`` fallback). No stage — the lane label answers "whose
    order is this?", not "what step?".
    """
    return _compose_identity(n_number, flavor, slot_id)
