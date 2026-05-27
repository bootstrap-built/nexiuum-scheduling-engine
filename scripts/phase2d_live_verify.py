"""Phase 2D live verify — spec-sheet → engine end-to-end.

Synthetic Spec Sheet Payload JSON is injected (mock the Production
Schedule read since no PS item has a payload yet — the form is live but
the first real submission hasn't landed). Everything else runs against
live Monday:
1. Real dual-instance snapshot read.
2. Pure-core spec_sheet translation → ScheduleNewOrder.
3. plan_for_new_order against the live snapshot.
4. apply_plan writes slots to both Schedule boards.
5. Read back + assert + cleanup.

Use this as a regression smoke after engine changes that touch the
spec-sheet ingest path.
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
from unittest.mock import patch

# Add the engine repo root to sys.path so `from engine.*` works
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from engine.config import get_settings, reset_settings_for_tests
from engine.core.spec_sheet import build_schedule_order, parse_spec_sheet_payload
from engine.core.timezone import now_local
from engine.io.apply import apply_plan
from engine.io.monday import gray_space_client, nexiuum_client
from engine.io.snapshot import read_snapshot
from engine.core.scheduler import plan_for_new_order

TEST_PS_ITEM_ID = "12117999999"  # synthetic — not a real item, used only as job_reference label


# Synthetic Spec Sheet Payload — what the form would have written to
# `Spec Sheet Payload` on a real Production Schedule item. Hand-crafted
# to exercise: container-rate capacity (Clamshell), cross-machine split
# (>=2 machines + qty above split_min_quantity), packaging breakdown
# (50/50 clamshell + sachet), items_per_container multiplier.
SYNTHETIC_PAYLOAD = {
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
            "packaging_breakdown": [
                {
                    "packaging_type": "Clamshell",
                    "qty": 500_000,
                    "items_per_container": 3,
                    "config_notes": "3ct diamond",
                },
                {
                    "packaging_type": "Sachet",
                    "qty": 500_000,
                    "items_per_container": 5,
                    "config_notes": "5ct",
                },
            ],
        }
    ],
    "flavor_index": 0,
}


async def main() -> int:
    reset_settings_for_tests()
    s = get_settings()
    assert s.nexiuum_enabled, "Nexiuum config must be set for this verify"

    print("=" * 72)
    print("Phase 2D live verify — spec-sheet → engine end-to-end")
    print("=" * 72)

    # ── Step 1: live snapshot ───────────────────────────────────────────
    print("\n[1/6] Reading live dual-instance snapshot...")
    snap = await read_snapshot()
    clam_count = sum(
        1 for m in snap.machines
        if m.process_group == "Clamshell" and m.is_available
    )
    sach_count = sum(
        1 for m in snap.machines
        if m.process_group == "Sachet" and m.is_available
    )
    print(f"  ✓ {clam_count} eligible Clamshell, {sach_count} eligible Sachet machines")
    if clam_count == 0 or sach_count == 0:
        print("  ✗ Need at least one each. Aborting.")
        return 1

    # ── Step 2: parse synthetic payload ─────────────────────────────────
    print("\n[2/6] Parsing synthetic Spec Sheet Payload JSON...")
    payload_text = json.dumps(SYNTHETIC_PAYLOAD)
    payload = parse_spec_sheet_payload(payload_text)
    print(f"  ✓ product_type={payload.product_type} "
          f"route={payload.manufacturing_route} "
          f"flavor_qty={payload.flavors[0].qty:,}")

    # ── Step 3: build ScheduleNewOrder ──────────────────────────────────
    print("\n[3/6] Building ScheduleNewOrder from payload...")
    order = build_schedule_order(payload, job_reference_id=TEST_PS_ITEM_ID)
    print(f"  ✓ recipe_key={order.recipe_key} v{order.recipe_version} "
          f"quantity={order.quantity:,}")
    print(f"  ✓ packaging_breakdown:")
    for slice_ in order.packaging_breakdown:
        print(f"     {slice_.machine_class}: qty={slice_.quantity:,} "
              f"items_per_container={slice_.items_per_container} "
              f"notes={slice_.config_notes!r}")

    # ── Step 4: plan against live snapshot ──────────────────────────────
    print("\n[4/6] Computing plan against live snapshot...")
    now = now_local(s.factory_tz)
    # Need to splice in the recipe — tablet-press-standard is the live
    # Gray Space recipe used in Phase 1, present in the snapshot.
    plan = plan_for_new_order(snap, order, now=now)
    print(f"  ✓ Plan has {len(plan.slot_writes)} slot writes")

    # ── Step 5: apply ──────────────────────────────────────────────────
    # Strip job_reference_id from ALL writes — the synthetic id doesn't
    # exist on either connected board (Blend Records on GS, Production
    # Schedule on NX), so Monday rejects the connect-board write.
    # For real Phase 2D submissions: the PS item id IS valid on NX, and
    # Gray Space Schedule's Job Reference column needs to be reconfigured
    # to also accept Production Schedule items (follow-up task).
    from dataclasses import replace as _replace
    from engine.models import Plan
    stripped = tuple(_replace(w, job_reference_id=None) for w in plan.slot_writes)
    plan = Plan(slot_writes=stripped, machine_writes=plan.machine_writes, notes=plan.notes)

    print("\n[5/6] Applying plan to live Schedule boards (job_reference stripped)...")
    result = await apply_plan(plan)
    if result.errors:
        print(f"  ✗ Errors: {result.errors}")
        return 1
    print(f"  ✓ Created {len(result.created_slot_ids)} slots")
    print(f"  ✓ reflow_hash: {result.reflow_hash}")

    # Find which slots landed on which boards (for cleanup).
    async with gray_space_client() as c:
        gs_items = await c.fetch_board_items(s.gray_space_schedule_board)
    async with nexiuum_client() as c:
        nx_items = await c.fetch_board_items(s.nexiuum_schedule_board)
    gs_new = [i for i in gs_items if i["id"] in result.created_slot_ids]
    nx_new = [i for i in nx_items if i["id"] in result.created_slot_ids]
    print(f"  ✓ Gray Space: {len(gs_new)} new slot(s)")
    print(f"  ✓ Nexiuum:    {len(nx_new)} new slot(s)")
    assert len(gs_new) == 1, f"Expected 1 GS slot, got {len(gs_new)}"
    assert len(nx_new) >= 2, f"Expected >=2 NX slots, got {len(nx_new)}"

    # ── Step 6: cleanup ────────────────────────────────────────────────
    print("\n[6/6] Cleaning up test slots...")
    async with gray_space_client() as c:
        for slot in gs_new:
            await c.query(f'mutation {{ delete_item(item_id: {slot["id"]}) {{ id }} }}')
            print(f"  ✓ Deleted GS slot {slot['id']}")
    async with nexiuum_client() as c:
        for slot in nx_new:
            await c.query(f'mutation {{ delete_item(item_id: {slot["id"]}) {{ id }} }}')
            print(f"  ✓ Deleted NX slot {slot['id']}")

    print("\n" + "=" * 72)
    print("✓✓✓ Phase 2D live verify PASSED")
    print("=" * 72)
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
