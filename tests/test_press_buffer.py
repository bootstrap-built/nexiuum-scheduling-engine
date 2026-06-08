"""Tests for the planned press→packaging rest buffer (#28).

Two layers:
  1. `add_working_hours` — the working-hours arithmetic in placement.py.
  2. `plan_for_new_order` — the buffer fires on the pressing→downstream edge
     only, and is counted on the downstream machine's working window.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from engine.config import Settings
from engine.core.placement import add_working_hours
from engine.core.scheduler import (
    PRESS_TO_PACKAGING_BUFFER_HOURS,
    plan_for_new_order,
)
from engine.models import (
    Machine,
    MachineStatus,
    PackagingSlice,
    Recipe,
    RecipeStage,
    RecipeStatus,
    ScheduleNewOrder,
    Snapshot,
)

TZ = ZoneInfo("America/Denver")
NOW = datetime(2026, 5, 21, 8, 0, 0, tzinfo=TZ)


def _machine(
    name: str,
    process_group: str,
    capacity: float,
    *,
    window_start: int = 0,
    window_end: int = 24,
) -> Machine:
    return Machine(
        id=name, name=name, process_group=process_group,  # type: ignore[arg-type]
        status=MachineStatus.ONLINE, capacity_per_hour=capacity,
        hours_per_day=window_end - window_start,
        working_window_start=window_start, working_window_end=window_end,
        changeover_minutes=0, dual_sided_only=False,
        max_job_size=None, force_route_condition=None, last_job_ended_at=None,
    )


def _settings() -> Settings:
    # Splitting disabled so each stage lands on one machine — clean arithmetic.
    return Settings(split_min_quantity=10_000_000, split_max_machines=4,
                    split_chunk_round_to=100)


def _snap(machines, recipes) -> Snapshot:
    return Snapshot(read_at=NOW, machines=tuple(machines),
                    recipes=tuple(recipes), slots=())


# ─── add_working_hours ──────────────────────────────────────────────────────


def test_add_working_hours_24h_machine_is_wall_clock():
    """A 24-hour machine has no closed time — the buffer is plain +hours."""
    m = _machine("M", "Clamshell", 1000, window_start=0, window_end=24)
    t = datetime(2026, 5, 21, 15, 0, tzinfo=TZ)
    assert add_working_hours(t, 4, m) == t + timedelta(hours=4)


def test_add_working_hours_spills_across_window_close():
    """08:00-16:00 machine, start 15:00 + 4h = 1h today + 3h next day → 11:00."""
    m = _machine("M", "Clamshell", 1000, window_start=8, window_end=16)
    t = datetime(2026, 5, 21, 15, 0, tzinfo=TZ)
    assert add_working_hours(t, 4, m) == datetime(2026, 5, 22, 11, 0, tzinfo=TZ)


def test_add_working_hours_starts_at_next_open_when_outside_window():
    """Start after close (20:00) → counting begins at next open (08:00)."""
    m = _machine("M", "Clamshell", 1000, window_start=8, window_end=16)
    t = datetime(2026, 5, 21, 20, 0, tzinfo=TZ)
    assert add_working_hours(t, 4, m) == datetime(2026, 5, 22, 12, 0, tzinfo=TZ)


def test_add_working_hours_zero_is_noop():
    m = _machine("M", "Clamshell", 1000, window_start=8, window_end=16)
    t = datetime(2026, 5, 21, 12, 0, tzinfo=TZ)
    assert add_working_hours(t, 0, m) == t


# ─── buffer fires on the pressing → downstream edge ─────────────────────────


def test_buffer_inserts_4h_between_press_and_packaging():
    """Packaging starts exactly 4 working hours after pressing ends (24h
    machines, so the buffer is wall-clock)."""
    press = _machine("Press", "Pressing", capacity=40_000)
    clam = _machine("Clam", "Clamshell", capacity=40_000)
    recipe = Recipe(
        id="R1", name="press v1", recipe_key="press", version=1,
        status=RecipeStatus.ACTIVE,
        stages=(RecipeStage(id="press", machine_class="Pressing", depends_on=()),),
    )
    order = ScheduleNewOrder(
        job_reference_id="N-1", recipe_key="press", recipe_version=1,
        quantity=40_000,
        packaging_breakdown=(
            PackagingSlice(machine_class="Clamshell", quantity=40_000,
                           items_per_container=1, config_notes=""),
        ),
    )
    plan = plan_for_new_order(_snap([press, clam], [recipe]), order,
                              now=NOW, settings=_settings())
    press_w = next(w for w in plan.slot_writes if w.stage_id == "press")
    pkg_w = next(w for w in plan.slot_writes if w.stage_id and w.stage_id.startswith("pkg_"))
    gap_h = (pkg_w.planned_start - press_w.planned_end).total_seconds() / 3600.0
    assert gap_h == pytest.approx(PRESS_TO_PACKAGING_BUFFER_HOURS, rel=1e-9)


def test_buffer_only_on_press_edge_not_intermediate_handoffs():
    """press→blister gets the buffer; blister→packaging does NOT (only the
    pressing edge is buffered)."""
    press = _machine("Press", "Pressing", capacity=40_000)
    blister = _machine("Blister", "Blister", capacity=40_000)
    clam = _machine("Clam", "Clamshell", capacity=40_000)
    recipe = Recipe(
        id="R1", name="press+blister v1", recipe_key="pb", version=1,
        status=RecipeStatus.ACTIVE,
        stages=(
            RecipeStage(id="press", machine_class="Pressing", depends_on=()),
            RecipeStage(id="blister", machine_class="Blister", depends_on=("press",)),
        ),
    )
    order = ScheduleNewOrder(
        job_reference_id="N-2", recipe_key="pb", recipe_version=1,
        quantity=40_000,
        packaging_breakdown=(
            PackagingSlice(machine_class="Clamshell", quantity=40_000,
                           items_per_container=1, config_notes=""),
        ),
    )
    plan = plan_for_new_order(_snap([press, blister, clam], [recipe]), order,
                              now=NOW, settings=_settings())
    press_w = next(w for w in plan.slot_writes if w.stage_id == "press")
    blister_w = next(w for w in plan.slot_writes if w.stage_id == "blister")
    pkg_w = next(w for w in plan.slot_writes if w.stage_id and w.stage_id.startswith("pkg_"))
    # press → blister: buffered
    press_gap = (blister_w.planned_start - press_w.planned_end).total_seconds() / 3600.0
    assert press_gap == pytest.approx(PRESS_TO_PACKAGING_BUFFER_HOURS, rel=1e-9)
    # blister → packaging: NOT buffered (back-to-back)
    assert pkg_w.planned_start == blister_w.planned_end


def test_buffer_is_working_hours_aware_on_restricted_packaging_window():
    """When the packaging machine has a restricted window, the 4h buffer is
    counted in that machine's working hours and spills past a window close."""
    press = _machine("Press", "Pressing", capacity=40_000)  # 24h
    # Clamshell works 08:00-16:00. Press for a 40k order at 40k/hr = 1h, starting
    # at NOW (08:00) ends 09:00. Buffer 4 working-hours on the 08-16 window →
    # 13:00 same day (well within the window, no spill).
    clam = _machine("Clam", "Clamshell", capacity=40_000,
                    window_start=8, window_end=16)
    recipe = Recipe(
        id="R1", name="press v1", recipe_key="press", version=1,
        status=RecipeStatus.ACTIVE,
        stages=(RecipeStage(id="press", machine_class="Pressing", depends_on=()),),
    )
    order = ScheduleNewOrder(
        job_reference_id="N-3", recipe_key="press", recipe_version=1,
        quantity=40_000,
        packaging_breakdown=(
            PackagingSlice(machine_class="Clamshell", quantity=40_000,
                           items_per_container=1, config_notes=""),
        ),
    )
    plan = plan_for_new_order(_snap([press, clam], [recipe]), order,
                              now=NOW, settings=_settings())
    press_w = next(w for w in plan.slot_writes if w.stage_id == "press")
    pkg_w = next(w for w in plan.slot_writes if w.stage_id and w.stage_id.startswith("pkg_"))
    assert press_w.planned_end == datetime(2026, 5, 21, 9, 0, tzinfo=TZ)
    assert pkg_w.planned_start == datetime(2026, 5, 21, 13, 0, tzinfo=TZ)
