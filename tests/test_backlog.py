"""Tests for engine.core.backlog — backlog rate + derivation (#21, ADR-0004)."""

from __future__ import annotations

from engine.core.backlog import BacklogEntry, backlog_press_rate, derive_backlog
from engine.models import Machine, MachineStatus, ScheduleNewOrder


def _machine(
    name: str,
    *,
    process_group: str = "Pressing",
    capacity: float = 40000,
    status: MachineStatus = MachineStatus.ONLINE,
    dual_sided_only: bool = False,
    max_job_size: int | None = None,
) -> Machine:
    return Machine(
        id=name, name=name, process_group=process_group,  # type: ignore[arg-type]
        status=status, capacity_per_hour=capacity, hours_per_day=16,
        working_window_start=6, working_window_end=22, changeover_minutes=30,
        dual_sided_only=dual_sided_only, max_job_size=max_job_size,
        force_route_condition=None, last_job_ended_at=None,
    )


def _order(job_id: str, *, qty: int = 100000, include_press: bool = True,
           n: str | None = None, flavor: str | None = None) -> ScheduleNewOrder:
    return ScheduleNewOrder(
        job_reference_id=job_id, recipe_key="tablet-press-standard",
        recipe_version=1, quantity=qty, include_press=include_press,
        n_number=n, flavor=flavor,
    )


# ─── backlog_press_rate ────────────────────────────────────────────────────


def test_rate_picks_slowest_general_purpose():
    """Min capacity among general-purpose online presses."""
    machines = [
        _machine("fast", capacity=50000),
        _machine("slow", capacity=30000),
        _machine("mid", capacity=40000),
    ]
    assert backlog_press_rate(machines) == 30000


def test_rate_excludes_dual_sided_and_capped_units():
    """Purpose-built small/dual-batch presses (slow by design) are excluded so
    they don't drag the estimate down."""
    machines = [
        _machine("general", capacity=40000),
        _machine("dual", capacity=8000, dual_sided_only=True),     # excluded
        _machine("small", capacity=5000, max_job_size=10000),      # excluded
    ]
    assert backlog_press_rate(machines) == 40000


def test_rate_falls_back_to_slowest_special_when_all_special():
    machines = [
        _machine("dual", capacity=8000, dual_sided_only=True),
        _machine("small", capacity=5000, max_job_size=10000),
    ]
    assert backlog_press_rate(machines) == 5000


def test_rate_ignores_offline_and_zero_capacity():
    machines = [
        _machine("down", capacity=20000, status=MachineStatus.DOWN),
        _machine("zero", capacity=0),
        _machine("online", capacity=35000),
    ]
    assert backlog_press_rate(machines) == 35000


def test_rate_none_when_no_eligible_press():
    assert backlog_press_rate([_machine("clam", process_group="Clamshell")]) is None
    assert backlog_press_rate([]) is None


def test_rate_only_considers_named_process_group():
    machines = [
        _machine("press", capacity=40000),
        _machine("clam", process_group="Clamshell", capacity=1000),
    ]
    assert backlog_press_rate(machines, process_group="Clamshell") == 1000


# ─── derive_backlog ────────────────────────────────────────────────────────


def test_derive_includes_unplaced_pressing_order_with_estimate():
    machines = [_machine("press", capacity=40000)]
    orders = [_order("ps-1", qty=100000, n="N1", flavor="Strawberry")]
    backlog = derive_backlog(orders, placed_job_ids=[], machines=machines)
    assert backlog == [
        BacklogEntry(
            job_reference_id="ps-1", n_number="N1", flavor="Strawberry",
            quantity=100000, estimated_hours=3,  # ceil(100000/40000) = 3
        )
    ]


def test_derive_excludes_already_placed_orders():
    machines = [_machine("press", capacity=40000)]
    orders = [_order("ps-1"), _order("ps-2")]
    backlog = derive_backlog(orders, placed_job_ids=["ps-1"], machines=machines)
    assert [e.job_reference_id for e in backlog] == ["ps-2"]


def test_derive_excludes_non_pressing_orders():
    """Kitting-Only (include_press=False) schedules at create_item, never
    backlogged."""
    machines = [_machine("press", capacity=40000)]
    orders = [_order("ps-1", include_press=False)]
    assert derive_backlog(orders, placed_job_ids=[], machines=machines) == []


def test_derive_estimate_none_when_no_press_rate():
    """No online press → entry still shows, but with no duration bar."""
    orders = [_order("ps-1", qty=100000)]
    backlog = derive_backlog(orders, placed_job_ids=[], machines=[])
    assert len(backlog) == 1
    assert backlog[0].estimated_hours is None


def test_derive_preserves_candidate_order():
    machines = [_machine("press", capacity=40000)]
    orders = [_order("ps-3"), _order("ps-1"), _order("ps-2")]
    backlog = derive_backlog(orders, placed_job_ids=[], machines=machines)
    assert [e.job_reference_id for e in backlog] == ["ps-3", "ps-1", "ps-2"]
