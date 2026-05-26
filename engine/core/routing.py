"""Routing — given an order + a stage, return ordered eligible machines.

Pure function. No IO. Reads from a Snapshot, produces a routing decision.

Routing has two layers:
1. Hard rules (forced routes) — applied first, in order. If a hard rule
   matches, the candidate set is restricted to the machines named by the
   rule. Order does not matter within the matched set.
2. Soft routing — applied when no hard rule fires. Round-robin against
   eligible machines, ordered by `last_job_ended_at` (earliest first =
   least-recently-used). Down machines are excluded.

Hard rules are intentionally hardcoded — they encode physical machine
properties that don't change. The Capacity Engine board's columns
(`dual_sided_only`, `max_job_size`, `force_route_condition`) carry the
machine-side flags the rules read.
"""

from __future__ import annotations

from datetime import datetime

from engine.models import Machine, ProcessGroup, ScheduleNewOrder, Snapshot


def eligible_machines(
    snapshot: Snapshot,
    *,
    machine_class: ProcessGroup,
    order: ScheduleNewOrder | None = None,
) -> list[Machine]:
    """Return the ordered list of machines eligible for an order's stage.

    Best candidate first. Empty list if no machine can run this stage.

    `machine_class` is the Process Group this stage requires (from the
    recipe's RecipeStage.machine_class). `order` carries the routing-relevant
    properties of the upstream order (dual_sided, active_mg, quantity); pass
    None to ignore order-specific hard rules (useful for diagnostic queries).

    Machine eligibility uses `Machine.is_available` (Online + hours_per_day
    > 0 + capacity_per_hour > 0) so machines that lack capacity numbers
    (e.g., Nexiuum's VERIFY WITH MAKAYLA machines at provisioning time)
    are excluded — protects downstream `quantity / capacity` math from
    divide-by-zero. They re-enter routing the moment ops fills in Capacity.
    """
    in_group = [m for m in snapshot.machines if m.process_group == machine_class]
    online = [m for m in in_group if m.is_available]

    # ── Hard rule 1: dual-sided tablets → ONLY dual-sided machines ──
    if order is not None and order.dual_sided:
        dual_sided = [m for m in online if m.dual_sided_only]
        return dual_sided  # may be empty — caller must handle

    # ── Skip dual-sided machines for non-dual-sided orders ──
    # (Penn & Teller is reserved for dual-sided work; round-robin would
    # otherwise route normal jobs onto it.)
    if order is None or not order.dual_sided:
        online = [m for m in online if not m.dual_sided_only]

    # ── Hard rule 2: force-route by condition (e.g., active_mg > 80 → Lancelot) ──
    # Force-routed machines still respect max_job_size — a forced route to an
    # over-capacity machine isn't a route at all. If the force-route candidate
    # is capped out by quantity, fall through to soft routing on other machines.
    if order is not None and order.active_mg is not None:
        forced = [m for m in online if _matches_force_route(m, order)]
        forced = [
            m for m in forced
            if m.max_job_size is None or order.quantity <= m.max_job_size
        ]
        if forced:
            return forced

    # ── Hard rule 3: max_job_size restricts which machines can run a large job ──
    # Copperfield has max_job_size=10000. A 50,000-tab job cannot run there.
    if order is not None:
        eligible_by_size = [
            m for m in online
            if m.max_job_size is None or order.quantity <= m.max_job_size
        ]
    else:
        eligible_by_size = online

    # ── Soft rule: prefer max_job_size machines for jobs that fit ──
    # Copperfield (R&D line) gets priority for jobs <10k tabs that fit its cap.
    if order is not None:
        priority_small = [
            m for m in eligible_by_size
            if m.max_job_size is not None and order.quantity <= m.max_job_size
        ]
        normal = [m for m in eligible_by_size if m not in priority_small]
        return _round_robin_order(priority_small) + _round_robin_order(normal)

    return _round_robin_order(eligible_by_size)


def _matches_force_route(machine: Machine, order: ScheduleNewOrder) -> bool:
    """Evaluate a machine's `force_route_condition` against an order.

    Supports a tiny DSL: `<field> <op> <number>` where field is `active_mg`,
    op is one of `>`, `>=`, `<`, `<=`, `==`. Anything else returns False and
    logs (caller's responsibility).

    Examples:
        "active_mg > 80"   → match if order.active_mg > 80
        "active_mg >= 100" → match if order.active_mg >= 100
    """
    condition = machine.force_route_condition
    if not condition:
        return False

    parts = condition.strip().split()
    if len(parts) != 3:
        return False
    field, op, value_str = parts
    try:
        value = float(value_str)
    except ValueError:
        return False

    actual: float | None
    if field == "active_mg":
        actual = order.active_mg
    elif field == "quantity":
        actual = float(order.quantity)
    else:
        return False

    if actual is None:
        return False

    if op == ">":
        return actual > value
    if op == ">=":
        return actual >= value
    if op == "<":
        return actual < value
    if op == "<=":
        return actual <= value
    if op == "==":
        return actual == value
    return False


def _round_robin_order(machines: list[Machine]) -> list[Machine]:
    """Sort machines for round-robin selection.

    Least-recently-used first: machines with the earliest `last_job_ended_at`
    take precedence. Machines that have never run a job (last_job_ended_at
    is None) come before any that have, on the theory that they need to be
    warmed in.
    """

    def sort_key(m: Machine) -> tuple[int, datetime]:
        # Tuple: (has_run_flag, last_ended). None last_ended sorts first.
        if m.last_job_ended_at is None:
            return (0, datetime.min.replace(tzinfo=None))
        return (1, m.last_job_ended_at.replace(tzinfo=None))

    return sorted(machines, key=sort_key)
