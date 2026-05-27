"""Phase 1.5 — packaging container-rate capacity, cross-machine split, and
order-driven packaging breakdown.

Three concerns live in one file so the fixtures stay shared:

1. CONTAINER_CAPACITY_GROUPS multiply capacity_per_hour by
   items_per_container when computing duration. Press/Capsule stay
   item-rate.

2. Packaging stages above settings.split_min_quantity with >=2 eligible
   machines fan across up to settings.split_max_machines, with quantity
   distributed proportional to capacity. Below threshold or only one
   eligible machine → single placement (matches Phase 1 behavior).

3. order.packaging_breakdown appends one synthetic packaging stage per
   slice, each depending on the recipe's terminal stage(s). Slices may
   share a machine_class (rare) or differ (common — clamshell + sachet).
"""

from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from engine.config import Settings
from engine.core.scheduler import plan_for_new_order
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


# ─── Fixtures ──────────────────────────────────────────────────────────────


def _machine(
    name: str,
    process_group: str,
    capacity: float,
    *,
    instance: str = "gray_space",
) -> Machine:
    return Machine(
        id=name,
        name=name,
        process_group=process_group,  # type: ignore[arg-type]
        status=MachineStatus.ONLINE,
        capacity_per_hour=capacity,
        hours_per_day=24,  # 24h window so window math doesn't muddy duration assertions
        working_window_start=0,
        working_window_end=24,
        changeover_minutes=0,  # zero changeover for clean arithmetic
        dual_sided_only=False,
        max_job_size=None,
        force_route_condition=None,
        last_job_ended_at=None,
        instance=instance,  # type: ignore[arg-type]
    )


def _press_recipe() -> Recipe:
    """Recipe with a single pressing stage — packaging is order-driven."""
    return Recipe(
        id="R1",
        name="tablet-press-standard v1",
        recipe_key="tablet-press-standard",
        version=1,
        status=RecipeStatus.ACTIVE,
        stages=(RecipeStage(id="press", machine_class="Pressing", depends_on=()),),
    )


def _settings(
    *, split_min: int = 50_000, split_max_machines: int = 4, round_to: int = 100,
) -> Settings:
    return Settings(
        split_min_quantity=split_min,
        split_max_machines=split_max_machines,
        split_chunk_round_to=round_to,
    )


def _snap(machines: list[Machine], recipes: list[Recipe]) -> Snapshot:
    return Snapshot(
        read_at=NOW, machines=tuple(machines), recipes=tuple(recipes), slots=(),
    )


# ─── 1. Container-rate capacity multiplier ─────────────────────────────────


def test_clamshell_capacity_treated_as_containers_per_hour():
    """Clamshell-1 = 3,200 containers/hr packs 16,000 tabs/hr at 5 tabs/clamshell.

    A 16,000-tab slice should take exactly 1 hour, not 5 hours.
    """
    press = _machine("Mainline", "Pressing", capacity=40000)
    clam = _machine("Clamshell-1", "Clamshell", capacity=3200)
    recipe = _press_recipe()

    order = ScheduleNewOrder(
        job_reference_id="N-1",
        recipe_key=recipe.recipe_key,
        recipe_version=1,
        quantity=16_000,
        packaging_breakdown=(
            PackagingSlice(
                machine_class="Clamshell", quantity=16_000,
                items_per_container=5, config_notes="5ct",
            ),
        ),
    )

    plan = plan_for_new_order(
        _snap([press, clam], [recipe]), order, now=NOW,
        settings=_settings(split_min=1_000_000),  # disable splitting for this test
    )

    pkg = next(w for w in plan.slot_writes if w.stage_id and w.stage_id.startswith("pkg_"))
    duration_h = (pkg.planned_end - pkg.planned_start).total_seconds() / 3600.0
    assert duration_h == pytest.approx(1.0, rel=1e-9)


def test_pressing_capacity_still_item_rate():
    """Pressing machines stay item-rate even if items_per_container set elsewhere.

    Multiplier should only fire for CONTAINER_CAPACITY_GROUPS.
    """
    press = _machine("Mainline", "Pressing", capacity=40000)
    recipe = _press_recipe()
    order = ScheduleNewOrder(
        job_reference_id="N-1",
        recipe_key=recipe.recipe_key,
        recipe_version=1,
        quantity=80_000,  # 2h on a 40k/hr machine
    )

    plan = plan_for_new_order(
        _snap([press], [recipe]), order, now=NOW, settings=_settings(),
    )
    press_w = next(w for w in plan.slot_writes if w.stage_id == "press")
    duration_h = (press_w.planned_end - press_w.planned_start).total_seconds() / 3600.0
    assert duration_h == pytest.approx(2.0, rel=1e-9)


def test_sachet_blister_bottle_all_get_multiplier():
    """All four container-rate groups apply the multiplier."""
    press = _machine("Mainline", "Pressing", capacity=1_000_000)  # press is instant
    machines_under_test = [
        ("Sachet", _machine("Sach-1", "Sachet", capacity=1_000)),
        ("Blister", _machine("Blist-1", "Blister", capacity=1_000)),
        ("Bottle", _machine("Bott-1", "Bottle", capacity=1_000)),
    ]
    recipe = _press_recipe()
    for mc, m in machines_under_test:
        order = ScheduleNewOrder(
            job_reference_id=f"N-{mc}",
            recipe_key=recipe.recipe_key,
            recipe_version=1,
            quantity=10_000,
            packaging_breakdown=(
                PackagingSlice(machine_class=mc, quantity=10_000, items_per_container=10),  # type: ignore[arg-type]
            ),
        )
        plan = plan_for_new_order(
            _snap([press, m], [recipe]), order, now=NOW,
            settings=_settings(split_min=1_000_000),
        )
        pkg = next(w for w in plan.slot_writes if w.stage_id and w.stage_id.startswith("pkg_"))
        duration_h = (pkg.planned_end - pkg.planned_start).total_seconds() / 3600.0
        assert duration_h == pytest.approx(1.0, rel=1e-9), (
            f"{mc}: expected 1h (10k tabs / (1k containers/hr * 10/container)), got {duration_h}h"
        )


# ─── 2. Cross-machine split ────────────────────────────────────────────────


def test_split_two_equal_machines_halves_time():
    """500k tabs across two equal-capacity clamshell machines → 250k each.

    Demonstrates Makayla's "cut the time in half" expectation literally.
    """
    press = _machine("Mainline", "Pressing", capacity=1_000_000)  # press is instant
    clam_a = _machine("Clam-A", "Clamshell", capacity=4_000)  # 4k containers/hr * 5 = 20k tabs/hr
    clam_b = _machine("Clam-B", "Clamshell", capacity=4_000)
    recipe = _press_recipe()

    order = ScheduleNewOrder(
        job_reference_id="N-split",
        recipe_key=recipe.recipe_key,
        recipe_version=1,
        quantity=500_000,
        packaging_breakdown=(
            PackagingSlice(machine_class="Clamshell", quantity=500_000, items_per_container=5),
        ),
    )

    plan = plan_for_new_order(
        _snap([press, clam_a, clam_b], [recipe]), order, now=NOW,
        settings=_settings(split_min=50_000, split_max_machines=4),
    )

    pkg_writes = [w for w in plan.slot_writes if w.stage_id and w.stage_id.startswith("pkg_")]
    assert len(pkg_writes) == 2
    assert sum(w.quantity for w in pkg_writes) == 500_000
    # Equal capacities → equal split (within round_to)
    assert pkg_writes[0].quantity == pkg_writes[1].quantity == 250_000


def test_split_proportional_to_capacity():
    """A 3:1 capacity ratio gets a 3:1 quantity split, not 1:1."""
    press = _machine("Mainline", "Pressing", capacity=1_000_000)
    fast = _machine("Clam-Fast", "Clamshell", capacity=6_000)  # 3x
    slow = _machine("Clam-Slow", "Clamshell", capacity=2_000)
    recipe = _press_recipe()

    order = ScheduleNewOrder(
        job_reference_id="N-prop",
        recipe_key=recipe.recipe_key,
        recipe_version=1,
        quantity=400_000,
        packaging_breakdown=(
            PackagingSlice(machine_class="Clamshell", quantity=400_000, items_per_container=5),
        ),
    )

    plan = plan_for_new_order(
        _snap([press, fast, slow], [recipe]), order, now=NOW,
        settings=_settings(split_min=50_000, split_max_machines=4, round_to=1_000),
    )
    pkg = {w.machine_id: w.quantity for w in plan.slot_writes if w.stage_id and w.stage_id.startswith("pkg_")}
    assert sum(pkg.values()) == 400_000
    # 6000:2000 = 3:1 → 300k:100k
    assert pkg["Clam-Fast"] == 300_000
    assert pkg["Clam-Slow"] == 100_000


def test_split_below_threshold_uses_single_machine():
    """A small order doesn't fragment even when multiple machines exist."""
    press = _machine("Mainline", "Pressing", capacity=1_000_000)
    a = _machine("Clam-A", "Clamshell", capacity=3200)
    b = _machine("Clam-B", "Clamshell", capacity=3200)
    recipe = _press_recipe()

    order = ScheduleNewOrder(
        job_reference_id="N-small",
        recipe_key=recipe.recipe_key,
        recipe_version=1,
        quantity=10_000,  # well below 50k threshold
        packaging_breakdown=(
            PackagingSlice(machine_class="Clamshell", quantity=10_000, items_per_container=5),
        ),
    )

    plan = plan_for_new_order(
        _snap([press, a, b], [recipe]), order, now=NOW, settings=_settings(),
    )
    pkg_writes = [w for w in plan.slot_writes if w.stage_id and w.stage_id.startswith("pkg_")]
    assert len(pkg_writes) == 1


def test_split_caps_at_max_machines():
    """8 eligible machines + max=4 → at most 4 slots."""
    press = _machine("Mainline", "Pressing", capacity=1_000_000)
    clams = [_machine(f"Clam-{i}", "Clamshell", capacity=3200) for i in range(8)]
    recipe = _press_recipe()

    order = ScheduleNewOrder(
        job_reference_id="N-cap",
        recipe_key=recipe.recipe_key,
        recipe_version=1,
        quantity=400_000,
        packaging_breakdown=(
            PackagingSlice(machine_class="Clamshell", quantity=400_000, items_per_container=5),
        ),
    )

    plan = plan_for_new_order(
        _snap([press, *clams], [recipe]), order, now=NOW,
        settings=_settings(split_max_machines=4),
    )
    pkg_writes = [w for w in plan.slot_writes if w.stage_id and w.stage_id.startswith("pkg_")]
    assert len(pkg_writes) == 4


def test_press_stage_never_splits():
    """Press stays single-machine even when above threshold + multiple machines.

    Per Makayla: splitting is a packaging-side concept only.
    """
    press_a = _machine("Press-A", "Pressing", capacity=40_000)
    press_b = _machine("Press-B", "Pressing", capacity=40_000)
    recipe = _press_recipe()

    order = ScheduleNewOrder(
        job_reference_id="N-press",
        recipe_key=recipe.recipe_key,
        recipe_version=1,
        quantity=1_000_000,
    )

    plan = plan_for_new_order(
        _snap([press_a, press_b], [recipe]), order, now=NOW,
        settings=_settings(),
    )
    press_writes = [w for w in plan.slot_writes if w.stage_id == "press"]
    assert len(press_writes) == 1


# ─── 3. Packaging breakdown ────────────────────────────────────────────────


def test_breakdown_50_50_clamshell_sachet():
    """1M-tab order, 500k 3ct clamshell + 500k 5ct sachet.

    Expect: 1 press slot (full 1M qty), N clamshell slots (sum to 500k),
    N sachet slots (sum to 500k). Packaging starts after press ends.
    """
    press = _machine("Mainline", "Pressing", capacity=200_000)  # 5h for 1M
    clam_a = _machine("Clam-A", "Clamshell", capacity=3_200)
    clam_b = _machine("Clam-B", "Clamshell", capacity=3_200)
    sach = _machine("Sach-1", "Sachet", capacity=5_000)
    recipe = _press_recipe()

    order = ScheduleNewOrder(
        job_reference_id="N-mix",
        recipe_key=recipe.recipe_key,
        recipe_version=1,
        quantity=1_000_000,
        packaging_breakdown=(
            PackagingSlice(machine_class="Clamshell", quantity=500_000, items_per_container=3, config_notes="3ct"),
            PackagingSlice(machine_class="Sachet",    quantity=500_000, items_per_container=5, config_notes="5ct"),
        ),
    )

    plan = plan_for_new_order(
        _snap([press, clam_a, clam_b, sach], [recipe]), order, now=NOW,
        settings=_settings(),
    )

    press_writes = [w for w in plan.slot_writes if w.stage_id == "press"]
    clam_writes = [w for w in plan.slot_writes if w.stage_id and w.stage_id.startswith("pkg_") and "Clamshell" in w.stage_id]
    sach_writes = [w for w in plan.slot_writes if w.stage_id and w.stage_id.startswith("pkg_") and "Sachet" in w.stage_id]

    # Press: one slot, full quantity (press doesn't split).
    assert len(press_writes) == 1
    assert press_writes[0].quantity == 1_000_000

    # Clamshell: 2 eligible + 500k slice → splits.
    assert len(clam_writes) == 2
    assert sum(w.quantity for w in clam_writes) == 500_000

    # Sachet: only 1 eligible → single slot.
    assert len(sach_writes) == 1
    assert sach_writes[0].quantity == 500_000

    # Both packaging stages start no earlier than press end.
    press_end = press_writes[0].planned_end
    for w in [*clam_writes, *sach_writes]:
        assert w.planned_start >= press_end


def test_breakdown_synthetic_stage_ids_are_distinct():
    """Each slice gets its own pkg_<idx>_<MachineClass> so deps + chart labels work."""
    press = _machine("Mainline", "Pressing", capacity=1_000_000)
    clam = _machine("Clam-1", "Clamshell", capacity=3_200)
    sach = _machine("Sach-1", "Sachet", capacity=5_000)
    recipe = _press_recipe()

    order = ScheduleNewOrder(
        job_reference_id="N-ids",
        recipe_key=recipe.recipe_key,
        recipe_version=1,
        quantity=200_000,
        packaging_breakdown=(
            PackagingSlice(machine_class="Clamshell", quantity=100_000, items_per_container=5),
            PackagingSlice(machine_class="Sachet",    quantity=100_000, items_per_container=5),
        ),
    )

    plan = plan_for_new_order(
        _snap([press, clam, sach], [recipe]), order, now=NOW,
        settings=_settings(split_min=1_000_000),  # force single slots so we can count
    )
    pkg_stage_ids = {w.stage_id for w in plan.slot_writes if w.stage_id and w.stage_id.startswith("pkg_")}
    assert pkg_stage_ids == {"pkg_0_Clamshell", "pkg_1_Sachet"}


def test_breakdown_with_no_eligible_machines_for_a_slice_raises():
    """If the order asks for Sachet packaging but no Sachet machines exist,
    UnroutableStageError fires for that synthetic stage.
    """
    from engine.core.scheduler import UnroutableStageError

    press = _machine("Mainline", "Pressing", capacity=1_000_000)
    # No Sachet machine in snapshot.
    recipe = _press_recipe()

    order = ScheduleNewOrder(
        job_reference_id="N-unroutable",
        recipe_key=recipe.recipe_key,
        recipe_version=1,
        quantity=100_000,
        packaging_breakdown=(
            PackagingSlice(machine_class="Sachet", quantity=100_000, items_per_container=5),
        ),
    )

    with pytest.raises(UnroutableStageError) as exc_info:
        plan_for_new_order(
            _snap([press], [recipe]), order, now=NOW, settings=_settings(),
        )
    assert "pkg_0_Sachet" in str(exc_info.value)


# ─── Codex review regressions ──────────────────────────────────────────────


def test_two_same_class_slices_do_not_overlap_on_one_machine():
    """P0 — two Clamshell slices in the same order must not double-book a
    machine. The second slice's queue lookup has to include the first
    slice's just-placed chunks.

    Repro: 1 Clamshell machine + 2 Clamshell slices below split threshold
    (each forced single-machine). Without the pending-queue augmentation,
    both slices would land at the same start time on the only machine.
    """
    press = _machine("Mainline", "Pressing", capacity=1_000_000)
    clam = _machine("Clam-1", "Clamshell", capacity=2_000)
    recipe = _press_recipe()

    order = ScheduleNewOrder(
        job_reference_id="N-overlap",
        recipe_key=recipe.recipe_key,
        recipe_version=1,
        quantity=20_000,
        packaging_breakdown=(
            PackagingSlice(machine_class="Clamshell", quantity=10_000, items_per_container=5, config_notes="5ct"),
            PackagingSlice(machine_class="Clamshell", quantity=10_000, items_per_container=3, config_notes="3ct"),
        ),
    )
    plan = plan_for_new_order(
        _snap([press, clam], [recipe]), order, now=NOW,
        settings=_settings(split_min=1_000_000),  # force single-machine per slice
    )
    pkg_writes = sorted(
        [w for w in plan.slot_writes if w.stage_id and w.stage_id.startswith("pkg_")],
        key=lambda w: w.planned_start,
    )
    assert len(pkg_writes) == 2
    # Second slice must start no earlier than first slice's end.
    assert pkg_writes[1].planned_start >= pkg_writes[0].planned_end


def test_pkg_sibling_actual_end_does_not_push_other_pkg_siblings():
    """P2 — finishing pkg_0_Clamshell must not push pkg_1_Sachet.

    Both depend on press, not on each other. The previous baton-pass logic
    classified ANY non-recipe-stage event as "terminal" and would sweep
    every pkg_* slot for the job — including unrelated siblings.
    """
    from engine.core.actuals import plan_for_actual_end
    from engine.models import (
        ActualEndReported, Priority, Slot, SlotStatus,
    )

    press_recipe = _press_recipe()
    press_slot = Slot(
        id="press-1", name="press", job_reference_id="J1", machine_id="M1",
        stage_id="press", recipe_key=press_recipe.recipe_key, recipe_version=1,
        quantity=1_000_000,
        planned_start=NOW, planned_end=NOW,
        actual_start=None, actual_end=None,
        dependent_on_ids=(), status=SlotStatus.DONE,
        manually_placed=False, priority=Priority.NORMAL,
        last_reflow_hash=None, drift_last_detected_at=None,
    )
    clam_slot = Slot(
        id="pkg-clam-1", name="pkg_0_Clamshell",
        job_reference_id="J1", machine_id="M2", stage_id="pkg_0_Clamshell",
        recipe_key=press_recipe.recipe_key, recipe_version=1,
        quantity=500_000,
        planned_start=NOW, planned_end=NOW.replace(hour=12),
        actual_start=NOW, actual_end=None,
        dependent_on_ids=(), status=SlotStatus.RUNNING,
        manually_placed=False, priority=Priority.NORMAL,
        last_reflow_hash=None, drift_last_detected_at=None,
    )
    sach_slot = Slot(
        id="pkg-sach-1", name="pkg_1_Sachet",
        job_reference_id="J1", machine_id="M3", stage_id="pkg_1_Sachet",
        recipe_key=press_recipe.recipe_key, recipe_version=1,
        quantity=500_000,
        planned_start=NOW.replace(hour=10), planned_end=NOW.replace(hour=15),
        actual_start=None, actual_end=None,
        dependent_on_ids=(), status=SlotStatus.QUEUED,
        manually_placed=False, priority=Priority.NORMAL,
        last_reflow_hash=None, drift_last_detected_at=None,
    )
    snap = Snapshot(
        read_at=NOW, machines=(_machine("M1", "Pressing", 40000),),
        recipes=(press_recipe,), slots=(press_slot, clam_slot, sach_slot),
    )
    event = ActualEndReported(
        job_reference_id="J1", stage_id="pkg_0_Clamshell",
        actual_at=NOW.replace(hour=12),
    )

    plan = plan_for_actual_end(snap, event, handoff_buffer_minutes=30)
    pushed_slots = {w.slot_id for w in plan.slot_writes}
    # The finishing pkg_0_Clamshell slot itself gets actual_end stamped.
    assert "pkg-clam-1" in pushed_slots
    # But the sibling pkg-sach-1 must NOT be pushed.
    assert "pkg-sach-1" not in pushed_slots


def test_proportional_chunks_preserves_total_at_boundary():
    """P2 — small totals where rounding would otherwise inflate the sum.

    Codex's minimal repro: total=151, 3 equal weights, round_to=100.
    Old algorithm produced [0, 100, 100] = 200 (over by 49 units).
    """
    from engine.core.scheduler import _proportional_chunks
    out = _proportional_chunks(151, [1.0, 1.0, 1.0], round_to=100)
    assert sum(out) == 151
    assert all(v >= 0 for v in out)


def test_proportional_chunks_normal_case_unchanged():
    """Sanity — the bread-and-butter case still works."""
    from engine.core.scheduler import _proportional_chunks
    out = _proportional_chunks(500_000, [3_200.0, 3_200.0], round_to=100)
    assert out == [250_000, 250_000]


def test_proportional_chunks_skewed_capacities():
    """3:1 capacity ratio yields a 3:1 split."""
    from engine.core.scheduler import _proportional_chunks
    out = _proportional_chunks(400_000, [6_000.0, 2_000.0], round_to=1_000)
    assert out == [300_000, 100_000]


def test_settings_rejects_zero_split_max_machines():
    """Pydantic validator catches the silent stage-drop misconfiguration."""
    from pydantic import ValidationError
    from engine.config import Settings
    with pytest.raises(ValidationError):
        Settings(split_max_machines=0)


def test_split_slot_name_includes_chunk_label_and_config_notes():
    """Split slots get `(1/2)` and config notes in their names."""
    press = _machine("Mainline", "Pressing", capacity=1_000_000)
    clam_a = _machine("Clam-A", "Clamshell", capacity=3_200)
    clam_b = _machine("Clam-B", "Clamshell", capacity=3_200)
    recipe = _press_recipe()

    order = ScheduleNewOrder(
        job_reference_id="N-name",
        recipe_key=recipe.recipe_key,
        recipe_version=1,
        quantity=500_000,
        packaging_breakdown=(
            PackagingSlice(
                machine_class="Clamshell", quantity=500_000,
                items_per_container=5, config_notes="5ct diamond",
            ),
        ),
    )

    plan = plan_for_new_order(
        _snap([press, clam_a, clam_b], [recipe]), order, now=NOW, settings=_settings(),
    )
    pkg_names = [w.name for w in plan.slot_writes if w.stage_id and w.stage_id.startswith("pkg_")]
    assert any("(1/2 · 5ct diamond)" in (n or "") for n in pkg_names)
    assert any("(2/2 · 5ct diamond)" in (n or "") for n in pkg_names)
