"""Backlog derivation — the "approved but not yet scheduled" lane (#21, ADR-0004).

Pure-core. The engine persists nothing for backlog: a pressing order is
deferred at create_item and surfaces here until its Blend Record flips to
"Blending" and it gets placed on machines. The `/view` renderer derives the
backlog set from Monday on each poll and draws each entry as an
**estimated-duration bar** — visibility only, never a machine/time commitment.

Two pieces:
- `backlog_press_rate` — the representative rate used to size the estimate bar.
- `derive_backlog` — the set of un-placed pressing orders + their estimates.
"""
from __future__ import annotations

import math
from collections.abc import Iterable
from dataclasses import dataclass

from engine.models import Machine, MachineStatus, ProcessGroup, ScheduleNewOrder


def backlog_press_rate(
    machines: Iterable[Machine],
    process_group: ProcessGroup = "Pressing",
) -> float | None:
    """The slowest *general-purpose* machine rate in a ProcessGroup (ADR-0004).

    A backlogged order's real duration depends on which press it eventually
    lands on, which isn't known until Blending. The estimate therefore uses one
    representative rate: the minimum `capacity_per_hour` among *online* machines
    in the group, **excluding** purpose-built small / dual-batch units (those
    flagged `dual_sided_only` or carrying a `max_job_size` cap) — they are slow
    by design and would make every estimate pessimistic. If every eligible
    machine is special-purpose, fall back to the slowest of those.

    Returns None when the group has no online machine with positive capacity
    (the caller then shows the order with no duration estimate).
    """
    online = [
        m for m in machines
        if m.process_group == process_group
        and m.status == MachineStatus.ONLINE
        and m.capacity_per_hour > 0
    ]
    if not online:
        return None
    general = [m for m in online if not m.dual_sided_only and m.max_job_size is None]
    pool = general or online
    return min(m.capacity_per_hour for m in pool)


@dataclass(frozen=True)
class BacklogEntry:
    """One row in the derived backlog lane.

    `estimated_hours` is the bar length the renderer draws — `quantity` over
    the backlog rate, rounded up to whole hours. None when no press rate is
    available (no online press machines), in which case the entry still shows
    (identity + qty) but without a duration bar.
    """

    job_reference_id: str
    n_number: str | None
    flavor: str | None
    quantity: int
    estimated_hours: float | None


def derive_backlog(
    candidates: Iterable[ScheduleNewOrder],
    placed_job_ids: Iterable[str],
    machines: Iterable[Machine],
    *,
    process_group: ProcessGroup = "Pressing",
) -> list[BacklogEntry]:
    """Derive the backlog lane from ingested orders and what's already placed.

    `candidates` are the pressing orders the engine knows about (built from PS
    items carrying a Spec Sheet Payload). An order is **backlogged** when it
    presses (`include_press`) and has **no Slots yet** — i.e. its
    `job_reference_id` is not in `placed_job_ids` (the set of job ids that
    already appear on the Schedule boards). Once an order reaches Blending it
    gets placed and drops out of this set on the next poll.

    Pure: takes the already-resolved rate inputs (machines) and computes each
    entry's estimate. Ordering follows `candidates` order (callers pass them in
    a stable order — e.g. PS board order).
    """
    placed = set(placed_job_ids)
    rate = backlog_press_rate(machines, process_group)
    entries: list[BacklogEntry] = []
    for order in candidates:
        if not order.include_press:
            continue
        if order.job_reference_id in placed:
            continue
        estimated_hours = (
            math.ceil(order.quantity / rate) if rate and order.quantity > 0 else None
        )
        entries.append(
            BacklogEntry(
                job_reference_id=order.job_reference_id,
                n_number=order.n_number,
                flavor=order.flavor,
                quantity=order.quantity,
                estimated_hours=estimated_hours,
            )
        )
    return entries
