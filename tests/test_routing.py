"""Unit tests for routing — hard rules + round-robin."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from engine.core.routing import eligible_machines
from engine.models import Machine, MachineStatus, ScheduleNewOrder, Snapshot


def _machine(
    name: str,
    process_group: str = "Pressing",
    status: MachineStatus = MachineStatus.ONLINE,
    capacity_per_hour: float = 40000,
    dual_sided_only: bool = False,
    max_job_size: int | None = None,
    force_route_condition: str | None = None,
    last_job_ended_at: datetime | None = None,
) -> Machine:
    return Machine(
        id=name,
        name=name,
        process_group=process_group,  # type: ignore[arg-type]
        status=status,
        capacity_per_hour=capacity_per_hour,
        hours_per_day=16,
        working_window_start=6,
        working_window_end=22,
        changeover_minutes=30,
        dual_sided_only=dual_sided_only,
        max_job_size=max_job_size,
        force_route_condition=force_route_condition,
        last_job_ended_at=last_job_ended_at,
    )


def _snap(*machines: Machine) -> Snapshot:
    return Snapshot(
        read_at=datetime(2026, 5, 21, 8, 0, 0, tzinfo=timezone.utc),
        machines=machines,
        recipes=(),
        slots=(),
    )


def _order(
    quantity: int = 100000,
    dual_sided: bool = False,
    active_mg: float | None = None,
) -> ScheduleNewOrder:
    return ScheduleNewOrder(
        job_reference_id="N0001",
        recipe_key="tablet-press-standard",
        recipe_version=1,
        quantity=quantity,
        dual_sided=dual_sided,
        active_mg=active_mg,
    )


# ─── Process group filtering ─────────────────────────────────────────────


def test_only_machines_in_target_class_are_eligible():
    gandalf = _machine("Gandalf", process_group="Pressing")
    elphaba = _machine("Elphaba", process_group="Capsule")
    snap = _snap(gandalf, elphaba)
    out = eligible_machines(snap, machine_class="Pressing", order=_order())
    assert [m.name for m in out] == ["Gandalf"]


def test_down_machines_excluded():
    gandalf = _machine("Gandalf")
    merlin = _machine("Merlin", status=MachineStatus.DOWN)
    snap = _snap(gandalf, merlin)
    out = eligible_machines(snap, machine_class="Pressing", order=_order())
    assert [m.name for m in out] == ["Gandalf"]


# ─── Dual-sided hard rule ────────────────────────────────────────────────


def test_dual_sided_order_routes_only_to_dual_sided_machines():
    gandalf = _machine("Gandalf", dual_sided_only=False)
    pnt = _machine("Penn & Teller", dual_sided_only=True)
    snap = _snap(gandalf, pnt)
    out = eligible_machines(snap, machine_class="Pressing", order=_order(dual_sided=True))
    assert [m.name for m in out] == ["Penn & Teller"]


def test_normal_order_never_routes_to_dual_sided_machine():
    """Penn & Teller is reserved — round-robin should not pick it for normal jobs."""
    gandalf = _machine("Gandalf")
    pnt = _machine("Penn & Teller", dual_sided_only=True)
    snap = _snap(gandalf, pnt)
    out = eligible_machines(snap, machine_class="Pressing", order=_order())
    assert "Penn & Teller" not in [m.name for m in out]
    assert [m.name for m in out] == ["Gandalf"]


def test_dual_sided_with_no_eligible_returns_empty():
    """If no dual-sided machine is online, return empty list."""
    gandalf = _machine("Gandalf", dual_sided_only=False)
    pnt = _machine("Penn & Teller", dual_sided_only=True, status=MachineStatus.DOWN)
    snap = _snap(gandalf, pnt)
    out = eligible_machines(snap, machine_class="Pressing", order=_order(dual_sided=True))
    assert out == []


# ─── Force-route condition (Lancelot for high-active) ────────────────────


def test_high_active_routes_to_force_route_machine():
    gandalf = _machine("Gandalf")
    lancelot = _machine("Lancelot", force_route_condition="active_mg > 80")
    snap = _snap(gandalf, lancelot)
    out = eligible_machines(snap, machine_class="Pressing", order=_order(active_mg=100))
    assert [m.name for m in out] == ["Lancelot"]


def test_low_active_ignores_force_route():
    gandalf = _machine("Gandalf")
    lancelot = _machine("Lancelot", force_route_condition="active_mg > 80")
    snap = _snap(gandalf, lancelot)
    out = eligible_machines(snap, machine_class="Pressing", order=_order(active_mg=50))
    # Lancelot is still eligible but not forced — both appear
    names = [m.name for m in out]
    assert "Gandalf" in names
    assert "Lancelot" in names


def test_invalid_force_route_condition_silently_ignored():
    """Malformed condition shouldn't crash routing."""
    lancelot = _machine("Lancelot", force_route_condition="this is garbage")
    snap = _snap(lancelot)
    # Should not raise
    out = eligible_machines(snap, machine_class="Pressing", order=_order(active_mg=100))
    assert [m.name for m in out] == ["Lancelot"]


# ─── Copperfield max_job_size ────────────────────────────────────────────


def test_small_job_prefers_max_job_size_machine():
    """Copperfield (max 10k) gets priority for jobs <10k tabs."""
    gandalf = _machine("Gandalf", max_job_size=None)
    copperfield = _machine("Copperfield", max_job_size=10000)
    snap = _snap(gandalf, copperfield)
    out = eligible_machines(snap, machine_class="Pressing", order=_order(quantity=5000))
    assert out[0].name == "Copperfield"


def test_large_job_excludes_max_job_size_machine():
    """Copperfield (max 10k) cannot run a 50k-tab job."""
    gandalf = _machine("Gandalf", max_job_size=None)
    copperfield = _machine("Copperfield", max_job_size=10000)
    snap = _snap(gandalf, copperfield)
    out = eligible_machines(snap, machine_class="Pressing", order=_order(quantity=50000))
    assert [m.name for m in out] == ["Gandalf"]


def test_small_job_with_no_max_size_machine_uses_general_pool():
    """If no max-size-capped machine exists, small jobs use the regular pool."""
    gandalf = _machine("Gandalf", max_job_size=None)
    lance = _machine("Lancelot", max_job_size=None)
    snap = _snap(gandalf, lance)
    out = eligible_machines(snap, machine_class="Pressing", order=_order(quantity=5000))
    assert set(m.name for m in out) == {"Gandalf", "Lancelot"}


# ─── Round-robin (least-recently-used) ───────────────────────────────────


def test_round_robin_prefers_never_used_machines_first():
    """A machine that's never run a job comes before one that has."""
    used = _machine(
        "Used",
        last_job_ended_at=datetime(2026, 5, 20, 12, 0, 0, tzinfo=timezone.utc),
    )
    fresh = _machine("Fresh", last_job_ended_at=None)
    snap = _snap(used, fresh)
    out = eligible_machines(snap, machine_class="Pressing", order=_order())
    assert [m.name for m in out] == ["Fresh", "Used"]


def test_round_robin_oldest_finished_first():
    older = _machine(
        "Older",
        last_job_ended_at=datetime(2026, 5, 19, 12, 0, 0, tzinfo=timezone.utc),
    )
    newer = _machine(
        "Newer",
        last_job_ended_at=datetime(2026, 5, 20, 12, 0, 0, tzinfo=timezone.utc),
    )
    snap = _snap(older, newer)
    out = eligible_machines(snap, machine_class="Pressing", order=_order())
    assert [m.name for m in out] == ["Older", "Newer"]
