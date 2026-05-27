"""Phase 1.5 live verify — container-rate capacity + cross-machine split +
order-driven packaging breakdown.

What this exercises end-to-end against live Monday boards:

1. Live dual-instance snapshot read (Gray Space + Nexiuum).
2. A synthetic press-only recipe + an order with packaging_breakdown
   covering two machine classes (Clamshell + Sachet). Engine should:
   - Place a single press slot on Gray Space.
   - Fan the Clamshell slice across MULTIPLE Nexiuum Clamshell machines
     (if more than one is available + qty above split_min_quantity).
   - Place the Sachet slice on Nexiuum Sachet machine(s) — possibly
     also split.
   - Apply the items_per_container multiplier when computing durations.
3. apply_plan writes the resulting slots to both Schedule boards.
4. Reads back to confirm.
5. Cleanup.

Safety: never writes to board 8196668916 (Production Schedule, read-only).
Strips job_reference_id from Nexiuum writes (same Phase 2B workaround —
Nexiuum Job Reference connects only to Production Schedule).
"""
from __future__ import annotations

import asyncio
import os
import sys
from dataclasses import replace

# Add the engine repo root to sys.path so `from engine.*` works
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from engine.config import get_settings, reset_settings_for_tests
from engine.core.scheduler import plan_for_new_order
from engine.core.timezone import now_local
from engine.io.apply import apply_plan
from engine.io.monday import gray_space_client, nexiuum_client
from engine.io.snapshot import read_snapshot
from engine.models import (
    PackagingSlice,
    Plan,
    Recipe,
    RecipeStage,
    RecipeStatus,
    ScheduleNewOrder,
    Snapshot,
)

# Real Blend Records item — used as job_reference on the Gray Space slot
# (Gray Space Schedule's Job Reference connects to Blend Records). The
# Nexiuum slot has job_reference_id stripped because Nexiuum Schedule's
# Job Reference connects to Production Schedule, not Blend Records.
TEST_JOB_REF = "11801201557"  # N3236 - ROAR LLC


async def main() -> int:
    reset_settings_for_tests()
    s = get_settings()
    assert s.nexiuum_enabled, "Nexiuum config must be set for this verify"

    print("=" * 72)
    print("Phase 1.5 live verify — breakdown + container rate + split")
    print("=" * 72)

    # ── Step 1: live snapshot ───────────────────────────────────────────
    print("\n[1/5] Reading live dual-instance snapshot...")
    snap = await read_snapshot()
    clam_machines = [
        m for m in snap.machines
        if m.process_group == "Clamshell" and m.is_available
    ]
    sach_machines = [
        m for m in snap.machines
        if m.process_group == "Sachet" and m.is_available
    ]
    print(f"  ✓ {len(clam_machines)} eligible Clamshell, "
          f"{len(sach_machines)} eligible Sachet machines")
    if not clam_machines or not sach_machines:
        print("  ✗ Need at least one Clamshell + one Sachet machine "
              "with non-zero capacity. Have Capacity columns been filled "
              "in by Makayla yet?")
        return 1

    # ── Step 2: synthetic press-only recipe ─────────────────────────────
    print("\n[2/5] Constructing synthetic press-only recipe...")
    synthetic_recipe = Recipe(
        id="synthetic-phase15",
        name="press only (Phase 1.5 verify)",
        recipe_key="press-phase15-verify",
        version=1,
        status=RecipeStatus.ACTIVE,
        stages=(
            RecipeStage(id="press", machine_class="Pressing", depends_on=()),
        ),
        instance="gray_space",
    )
    snap_with_recipe = Snapshot(
        read_at=snap.read_at,
        machines=snap.machines,
        recipes=snap.recipes + (synthetic_recipe,),
        slots=snap.slots,
    )
    print(f"  ✓ Recipe pinned: {synthetic_recipe.recipe_key} "
          f"v{synthetic_recipe.version}")

    # ── Step 3: order with packaging_breakdown ──────────────────────────
    print("\n[3/5] Computing plan for 1M-tab order, 50/50 clamshell/sachet...")
    order = ScheduleNewOrder(
        job_reference_id=TEST_JOB_REF,
        recipe_key="press-phase15-verify",
        recipe_version=1,
        quantity=1_000_000,
        packaging_breakdown=(
            PackagingSlice(
                machine_class="Clamshell",
                quantity=500_000,
                items_per_container=3,
                config_notes="3ct",
            ),
            PackagingSlice(
                machine_class="Sachet",
                quantity=500_000,
                items_per_container=5,
                config_notes="5ct",
            ),
        ),
    )
    now = now_local(s.factory_tz)
    plan = plan_for_new_order(snap_with_recipe, order, now=now)
    print(f"  ✓ Plan has {len(plan.slot_writes)} slot writes:")
    for w in plan.slot_writes:
        m = snap.machine_by_id(w.machine_id) if w.machine_id else None
        m_name = m.name if m else "?"
        duration_h = (
            (w.planned_end - w.planned_start).total_seconds() / 3600.0
            if w.planned_start and w.planned_end else 0
        )
        print(
            f"     stage={w.stage_id} qty={w.quantity:,} → {m_name} "
            f"(instance={w.instance}) duration={duration_h:.2f}h"
        )

    # Assertions on plan shape.
    press_writes = [w for w in plan.slot_writes if w.stage_id == "press"]
    clam_writes = [
        w for w in plan.slot_writes
        if w.stage_id and w.stage_id.startswith("pkg_") and "Clamshell" in w.stage_id
    ]
    sach_writes = [
        w for w in plan.slot_writes
        if w.stage_id and w.stage_id.startswith("pkg_") and "Sachet" in w.stage_id
    ]
    assert len(press_writes) == 1, "Press should produce exactly one slot"
    assert press_writes[0].quantity == 1_000_000
    assert sum(w.quantity for w in clam_writes) == 500_000, (
        f"Clamshell slice quantities should sum to 500_000, got "
        f"{sum(w.quantity for w in clam_writes)}"
    )
    assert sum(w.quantity for w in sach_writes) == 500_000, (
        f"Sachet slice quantities should sum to 500_000, got "
        f"{sum(w.quantity for w in sach_writes)}"
    )
    if len(clam_machines) >= 2:
        assert len(clam_writes) >= 2, (
            f"Expected Clamshell to split across {len(clam_machines)}+ "
            f"machines (qty=500k >= split_min_quantity={s.split_min_quantity}), "
            f"got {len(clam_writes)}"
        )
    print(
        f"  ✓ press=1 slot, clamshell={len(clam_writes)} slot(s), "
        f"sachet={len(sach_writes)} slot(s)"
    )

    # ── Step 4: apply (with Nexiuum job_reference workaround) ───────────
    print("\n[4/5] Applying plan to live Schedule boards...")
    adjusted_writes = tuple(
        replace(w, job_reference_id=None) if w.instance == "nexiuum" else w
        for w in plan.slot_writes
    )
    plan = Plan(
        slot_writes=adjusted_writes,
        machine_writes=plan.machine_writes,
        notes=plan.notes,
    )

    result = await apply_plan(plan)
    if result.errors:
        print(f"  ✗ Errors: {result.errors}")
        return 1
    print(f"  ✓ Created {len(result.created_slot_ids)} slots")
    print(f"  ✓ reflow_hash: {result.reflow_hash}")

    print("\n[4b/5] Reading back from both Schedule boards...")
    async with gray_space_client() as c:
        gs_items = await c.fetch_board_items(s.gray_space_schedule_board)
    async with nexiuum_client() as c:
        nx_items = await c.fetch_board_items(s.nexiuum_schedule_board)
    gs_new = [i for i in gs_items if i["id"] in result.created_slot_ids]
    nx_new = [i for i in nx_items if i["id"] in result.created_slot_ids]
    print(f"  ✓ Gray Space: {len(gs_new)} new slot(s)")
    print(f"  ✓ Nexiuum:    {len(nx_new)} new slot(s)")
    expected_nx = len(clam_writes) + len(sach_writes)
    assert len(gs_new) == 1, f"Expected 1 GS slot, got {len(gs_new)}"
    assert len(nx_new) == expected_nx, (
        f"Expected {expected_nx} NX slots, got {len(nx_new)}"
    )

    # ── Step 5: cleanup ─────────────────────────────────────────────────
    print("\n[5/5] Cleaning up test slots...")
    async with gray_space_client() as c:
        for slot in gs_new:
            await c.query(
                f'mutation {{ delete_item(item_id: {slot["id"]}) {{ id }} }}'
            )
            print(f"  ✓ Deleted Gray Space slot {slot['id']}")
    async with nexiuum_client() as c:
        for slot in nx_new:
            await c.query(
                f'mutation {{ delete_item(item_id: {slot["id"]}) {{ id }} }}'
            )
            print(f"  ✓ Deleted Nexiuum slot {slot['id']}")

    print("\n" + "=" * 72)
    print("✓✓✓ Phase 1.5 live verify PASSED")
    print("=" * 72)
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
