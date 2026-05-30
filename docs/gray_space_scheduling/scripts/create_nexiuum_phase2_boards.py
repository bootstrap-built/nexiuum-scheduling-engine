"""Create the three Nexiuum Phase 2 scheduling boards.

Per gray-space-scheduling-plan.md v3 schemas:
  1. Nexiuum Capacity Engine — packaging machines
  2. Nexiuum Process Recipe — multi-stage packaging + press recipes
  3. Nexiuum Schedule — flat slot items (empty for now)

HARD CONSTRAINTS:
  - Writes only to NEW boards created here.
  - Production Schedule (8196668916) is read-only — never mutate.
  - Uses MONDAY_NEXIUUM_TOKEN only.
"""

from __future__ import annotations

import json
import os
import sys
import time
from typing import Any

import requests

MONDAY_URL = "https://api.monday.com/v2"
TOKEN = os.environ["MONDAY_NEXIUUM_TOKEN"]
HEADERS = {
    "Authorization": TOKEN,
    "Content-Type": "application/json",
    "API-Version": "2024-01",
}
WORKSPACE_ID = 10266420  # "Production" — same workspace as Production Schedule
PRODUCTION_SCHEDULE_ID = 8196668916  # READ-ONLY. NEVER WRITE.

DRY_RUN = "--dry-run" in sys.argv


def gql(query: str, variables: dict[str, Any] | None = None) -> dict[str, Any]:
    payload: dict[str, Any] = {"query": query}
    if variables:
        payload["variables"] = variables
    r = requests.post(MONDAY_URL, headers=HEADERS, json=payload, timeout=30)
    r.raise_for_status()
    out = r.json()
    if "errors" in out:
        raise RuntimeError(f"GraphQL error: {json.dumps(out, indent=2)}")
    if out.get("data") is None:
        raise RuntimeError(f"No data in response: {json.dumps(out, indent=2)}")
    return out["data"]


def safety_check(board_id: int) -> None:
    if board_id == PRODUCTION_SCHEDULE_ID:
        raise RuntimeError(
            f"REFUSED: attempted write to read-only board {PRODUCTION_SCHEDULE_ID}"
        )


def create_board(name: str) -> int:
    print(f"\n=== Creating board: {name} ===")
    q = """
    mutation ($name: String!, $ws: ID!) {
      create_board(
        board_name: $name,
        board_kind: public,
        workspace_id: $ws
      ) { id name }
    }
    """
    data = gql(q, {"name": name, "ws": str(WORKSPACE_ID)})
    bid = int(data["create_board"]["id"])
    safety_check(bid)  # cannot equal PROD_SCHED, but be paranoid
    print(f"  -> board id: {bid}")
    return bid


def get_default_column_ids(board_id: int) -> list[str]:
    """Get the default columns Monday auto-creates so we can delete the
    placeholder 'Status' / 'Date' the API gives us and replace with our schema.
    Actually — simpler: leave defaults alone, just add our columns by title.
    We'll also delete the auto-Status if our schema has a different Status."""
    q = """
    query ($id: [ID!]) {
      boards(ids: $id) { columns { id title type } }
    }
    """
    data = gql(q, {"id": [str(board_id)]})
    return data["boards"][0]["columns"]


UNSUPPORTED_VIA_API: list[dict[str, str]] = []


def create_column(
    board_id: int,
    title: str,
    column_type: str,
    defaults: dict[str, Any] | None = None,
    *,
    optional: bool = False,
) -> str | None:
    safety_check(board_id)
    q = """
    mutation ($bid: ID!, $title: String!, $type: ColumnType!, $defaults: JSON) {
      create_column(
        board_id: $bid,
        title: $title,
        column_type: $type,
        defaults: $defaults
      ) { id title type }
    }
    """
    vars_: dict[str, Any] = {
        "bid": str(board_id),
        "title": title,
        "type": column_type,
    }
    if defaults is not None:
        vars_["defaults"] = json.dumps(defaults)
    try:
        data = gql(q, vars_)
    except RuntimeError as e:
        msg = str(e)
        if optional and ("not supported" in msg or "InvalidColumnTypeException" in msg):
            print(f"    ! column '{title}' ({column_type}) NOT SUPPORTED VIA API — create manually")
            UNSUPPORTED_VIA_API.append({
                "board_id": str(board_id),
                "title": title,
                "type": column_type,
            })
            return None
        raise
    cid = data["create_column"]["id"]
    print(f"    + column '{title}' ({column_type}) -> {cid}")
    return cid


def delete_column(board_id: int, column_id: str) -> None:
    safety_check(board_id)
    q = """
    mutation ($bid: ID!, $cid: String!) {
      delete_column(board_id: $bid, column_id: $cid) { id }
    }
    """
    gql(q, {"bid": str(board_id), "cid": column_id})
    print(f"    - deleted default column {column_id}")


def cleanup_defaults(board_id: int, keep_titles: set[str]) -> None:
    """Delete any non-Name default columns that aren't in our target schema."""
    cols = get_default_column_ids(board_id)
    for c in cols:
        if c["type"] == "name":
            continue
        if c["title"] in keep_titles:
            continue
        try:
            delete_column(board_id, c["id"])
            time.sleep(0.3)
        except Exception as e:
            print(f"    (skip delete {c['title']}: {e})")


def create_item(board_id: int, name: str, column_values: dict[str, Any]) -> int:
    safety_check(board_id)
    q = """
    mutation ($bid: ID!, $name: String!, $cv: JSON!) {
      create_item(board_id: $bid, item_name: $name, column_values: $cv) {
        id name
      }
    }
    """
    data = gql(q, {
        "bid": str(board_id),
        "name": name,
        "cv": json.dumps(column_values),
    })
    iid = int(data["create_item"]["id"])
    print(f"    item '{name}' -> {iid}")
    return iid


# ---------------------------------------------------------------------------
# Board 1: Capacity Engine
# ---------------------------------------------------------------------------

PROCESS_GROUP_LABELS = [
    "Pressing", "Capsule", "Sachet", "Blister", "Clamshell",
    "Bottle", "Lot Coder", "Hand-pack",
]
MACHINE_STATUS_LABELS = ["Online", "Down", "Scheduled Maintenance"]


def build_capacity_engine() -> tuple[int, dict[str, str]]:
    board_id = create_board("Nexiuum Capacity Engine")
    cleanup_defaults(board_id, keep_titles=set())

    cols: dict[str, str] = {}

    cols["process_group"] = create_column(
        board_id, "Process Group", "status",
        defaults={"labels": {str(i): lbl for i, lbl in enumerate(PROCESS_GROUP_LABELS)}},
    )
    cols["machine_status"] = create_column(
        board_id, "Status", "status",
        defaults={"labels": {str(i): lbl for i, lbl in enumerate(MACHINE_STATUS_LABELS)}},
    )
    cols["capacity"] = create_column(board_id, "Capacity (units/hr)", "numbers")
    cols["hours_per_day"] = create_column(board_id, "Hours per day", "numbers")
    cols["window_start"] = create_column(board_id, "Working window start", "numbers")
    cols["window_end"] = create_column(board_id, "Working window end", "numbers")
    cols["changeover"] = create_column(board_id, "Changeover buffer (min)", "numbers")
    cols["dual_sided"] = create_column(board_id, "Dual-sided only", "checkbox")
    cols["max_job_size"] = create_column(board_id, "Max job size", "numbers")
    cols["force_route"] = create_column(board_id, "Force-route condition", "text")
    cols["last_job_ended"] = create_column(board_id, "Last job ended at", "date")
    cols["notes"] = create_column(board_id, "Notes", "long_text")

    return board_id, cols


# Machines to seed
MACHINES: list[dict[str, Any]] = (
    [{"name": f"Sachet-{i}", "group": "Sachet", "cap": 1750} for i in range(1, 5)]
    + [{"name": "Sachet-5", "group": "Sachet", "cap": 1500, "notes": "slower line per Jason"}]
    + [{"name": f"Clamshell-{i}", "group": "Clamshell", "cap": 0,
        "notes": "VERIFY WITH MAKAYLA — capacity TBD"} for i in range(1, 8)]
    + [{"name": f"Blister-Fast-{i}", "group": "Blister", "cap": 4000} for i in range(1, 3)]
    + [{"name": f"Blister-Std-{i}", "group": "Blister", "cap": 0,
        "notes": "VERIFY WITH MAKAYLA — capacity TBD"} for i in range(1, 5)]
    + [{"name": "Bottling-1", "group": "Bottle", "cap": 1200}]
    + [{"name": "Bottling-2", "group": "Bottle", "cap": 2400}]
    + [{"name": "ManualBottle-1", "group": "Hand-pack", "cap": 0,
        "notes": "VERIFY WITH MAKAYLA — hand-fill rate TBD"}]
    + [{"name": "LotCoder-1", "group": "Lot Coder", "cap": 0,
        "notes": "VERIFY WITH MAKAYLA — count + speed TBD"}]
)


def seed_capacity_engine(board_id: int, cols: dict[str, str]) -> None:
    print(f"\n  Seeding {len(MACHINES)} machines...")
    for m in MACHINES:
        cv: dict[str, Any] = {
            cols["process_group"]: {"label": m["group"]},
            cols["machine_status"]: {"label": "Online"},
            cols["capacity"]: m["cap"],
            cols["hours_per_day"]: 16,
            cols["window_start"]: 6,
            cols["window_end"]: 22,
            cols["changeover"]: 30,
            cols["dual_sided"]: {"checked": "false"},
        }
        if "notes" in m:
            cv[cols["notes"]] = m["notes"]
        create_item(board_id, m["name"], cv)
        time.sleep(0.25)


# ---------------------------------------------------------------------------
# Board 2: Process Recipe
# ---------------------------------------------------------------------------

RECIPE_STATUS_LABELS = ["Draft", "Active", "Retired"]


def build_process_recipe() -> tuple[int, dict[str, str]]:
    board_id = create_board("Nexiuum Process Recipe")
    cleanup_defaults(board_id, keep_titles=set())

    cols: dict[str, str] = {}
    cols["recipe_key"] = create_column(board_id, "Recipe Key", "text")
    cols["version"] = create_column(board_id, "Version", "numbers")
    cols["status"] = create_column(
        board_id, "Status", "status",
        defaults={"labels": {str(i): lbl for i, lbl in enumerate(RECIPE_STATUS_LABELS)}},
    )
    cols["stages"] = create_column(board_id, "Stages", "long_text")
    cols["notes"] = create_column(board_id, "Notes", "long_text")
    return board_id, cols


STUB_STAGES = [
    {"id": "press", "machine_class": "Pressing", "depends_on": []},
    {"id": "blister", "machine_class": "Blister", "depends_on": ["press"]},
    {"id": "lotcode", "machine_class": "Lot Coder", "depends_on": ["press"]},
    {"id": "clamshell", "machine_class": "Clamshell",
     "depends_on": ["blister", "lotcode"]},
]


def seed_process_recipe(board_id: int, cols: dict[str, str]) -> int:
    cv: dict[str, Any] = {
        cols["recipe_key"]: "tablet-blister-clamshell",
        cols["version"]: 1,
        cols["status"]: {"label": "Active"},
        cols["stages"]: json.dumps(STUB_STAGES),
    }
    return create_item(board_id, "tablet-blister-clamshell", cv)


# ---------------------------------------------------------------------------
# Board 3: Schedule
# ---------------------------------------------------------------------------

SCHEDULE_STATUS_LABELS = ["Queued", "Running", "Done", "Blocked"]
PRIORITY_LABELS = ["Normal", "Expedite"]


def build_schedule(capacity_board_id: int) -> tuple[int, dict[str, str]]:
    board_id = create_board("Nexiuum Schedule")
    cleanup_defaults(board_id, keep_titles=set())

    cols: dict[str, str] = {}

    # Connect-Boards columns: Job Reference (no specific target board — operator
    # picks). Machine -> Capacity Engine.
    cols["job_reference"] = create_column(
        board_id, "Job Reference", "board_relation",
    )
    cols["machine"] = create_column(
        board_id, "Machine", "board_relation",
        defaults={"boardIds": [capacity_board_id]},
    )
    cols["stage_id"] = create_column(board_id, "Stage ID", "text")
    cols["recipe_key"] = create_column(board_id, "Recipe Key", "text")
    cols["recipe_version"] = create_column(board_id, "Recipe Version", "numbers")
    cols["quantity"] = create_column(board_id, "Quantity", "numbers")
    # Mirror — depends on the Machine connect_boards column being created first.
    # NOTE: Mirror columns require a linked_column reference; if `defaults`
    # syntax fails we'll log and ask the user to wire it manually.
    try:
        cols["capacity_mirror"] = create_column(
            board_id, "Capacity (mirror)", "mirror",
            defaults={
                "relation_column": {cols["machine"]: True},
                "displayed_column": {"name": "Capacity (units/hr)"},
            },
        )
    except Exception as e:
        print(f"    (mirror auto-config failed: {e}; create manually in UI)")
    # Formula — Monday API can't always set formula text; create empty and
    # log a follow-up.
    try:
        cols["duration_hrs"] = create_column(
            board_id, "Duration (hrs)", "formula",
            defaults={"formula": f"{{Quantity}} / {{Capacity (mirror)}}"},
        )
    except Exception as e:
        print(f"    (formula auto-config failed: {e}; set formula manually)")
    cols["planned_start"] = create_column(board_id, "planned_start", "date")
    cols["planned_end"] = create_column(board_id, "planned_end", "date")
    cols["actual_start"] = create_column(board_id, "actual_start", "date")
    cols["actual_end"] = create_column(board_id, "actual_end", "date")
    cols["dependent_on"] = create_column(board_id, "Dependent On", "dependency")
    cols["status"] = create_column(
        board_id, "Status", "status",
        defaults={"labels": {str(i): lbl for i, lbl in enumerate(SCHEDULE_STATUS_LABELS)}},
    )
    cols["manually_placed"] = create_column(board_id, "Manually placed", "checkbox")
    cols["priority"] = create_column(
        board_id, "Priority", "status",
        defaults={"labels": {str(i): lbl for i, lbl in enumerate(PRIORITY_LABELS)}},
    )
    cols["last_reflow_hash"] = create_column(board_id, "last_reflow_hash", "text")
    cols["drift_last_detected_at"] = create_column(
        board_id, "drift_last_detected_at", "date"
    )

    return board_id, cols


# ---------------------------------------------------------------------------
# Verification
# ---------------------------------------------------------------------------

def verify_board(board_id: int) -> dict[str, Any]:
    q = """
    query ($id: [ID!]) {
      boards(ids: $id) {
        id name items_count
        workspace { id name }
        columns { id title type }
        items_page(limit: 100) {
          items {
            id name
            column_values { id text value }
          }
        }
      }
    }
    """
    data = gql(q, {"id": [str(board_id)]})
    return data["boards"][0]


def main() -> None:
    if DRY_RUN:
        print("DRY RUN — no mutations")
        return

    print(f"\n>>> Creating three Phase 2 boards in workspace {WORKSPACE_ID} (Production)\n")

    # 1. Capacity Engine
    cap_id, cap_cols = build_capacity_engine()
    seed_capacity_engine(cap_id, cap_cols)

    # 2. Process Recipe
    rec_id, rec_cols = build_process_recipe()
    recipe_item_id = seed_process_recipe(rec_id, rec_cols)

    # 3. Schedule
    sched_id, sched_cols = build_schedule(cap_id)

    print("\n\n=== Verification ===")
    for label, bid in [
        ("Capacity Engine", cap_id),
        ("Process Recipe", rec_id),
        ("Schedule", sched_id),
    ]:
        b = verify_board(bid)
        print(f"\n--- {label}: {b['id']} ({b['name']}) — items={b['items_count']} ---")
        for c in b["columns"]:
            print(f"  {c['id']:40s} {c['title']:30s} {c['type']}")

    # Persist IDs for downstream
    out = {
        "capacity_engine": {"board_id": cap_id, "columns": cap_cols},
        "process_recipe": {
            "board_id": rec_id,
            "columns": rec_cols,
            "stub_recipe_item_id": recipe_item_id,
        },
        "schedule": {"board_id": sched_id, "columns": sched_cols},
    }
    out_path = os.path.join(
        os.path.dirname(__file__), "nexiuum_phase2_board_ids.json"
    )
    with open(out_path, "w") as f:
        json.dump(out, f, indent=2)
    print(f"\nWrote board IDs and column map to {out_path}")


if __name__ == "__main__":
    main()
