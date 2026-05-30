"""Finish populating the Nexiuum Schedule board (id 18414776220).

Created by create_nexiuum_phase2_boards.py but only got Name column before
the script blew up on board_relation. This script adds every column we can
via API, and prints the ones that must be added manually.
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

PROD_SCHED = 8196668916  # READ-ONLY
SCHEDULE_BOARD_ID = 18414776220
CAPACITY_ENGINE_ID = 18414776125

SCHEDULE_STATUS_LABELS = ["Queued", "Running", "Done", "Blocked"]
PRIORITY_LABELS = ["Normal", "Expedite"]


def gql(query: str, variables: dict[str, Any] | None = None) -> dict[str, Any]:
    r = requests.post(
        MONDAY_URL, headers=HEADERS,
        json={"query": query, "variables": variables or {}},
        timeout=30,
    )
    r.raise_for_status()
    out = r.json()
    if "errors" in out:
        raise RuntimeError(json.dumps(out, indent=2))
    return out["data"]


unsupported: list[dict[str, str]] = []


def safety(board_id: int) -> None:
    if board_id == PROD_SCHED:
        raise RuntimeError("REFUSED: would write to read-only Production Schedule")


def col(board_id: int, title: str, ctype: str,
        defaults: dict[str, Any] | None = None,
        optional: bool = False) -> str | None:
    safety(board_id)
    q = """
    mutation ($bid: ID!, $title: String!, $type: ColumnType!, $defaults: JSON) {
      create_column(board_id: $bid, title: $title, column_type: $type, defaults: $defaults) {
        id title type
      }
    }
    """
    vars_: dict[str, Any] = {"bid": str(board_id), "title": title, "type": ctype}
    if defaults is not None:
        vars_["defaults"] = json.dumps(defaults)
    try:
        data = gql(q, vars_)
        cid = data["create_column"]["id"]
        print(f"  + {title:32s} ({ctype:14s}) -> {cid}")
        return cid
    except RuntimeError as e:
        msg = str(e)
        if optional and "not supported" in msg.lower():
            print(f"  ! {title:32s} ({ctype}) NOT SUPPORTED VIA API")
            unsupported.append({"title": title, "type": ctype})
            return None
        raise


def main() -> None:
    # First check what's already there to avoid duplicates
    existing = gql(
        """{ boards(ids: [18414776220]) { columns { id title type } } }"""
    )["boards"][0]["columns"]
    existing_titles = {c["title"] for c in existing}
    print(f"Existing columns on schedule board: {existing_titles}")

    bid = SCHEDULE_BOARD_ID
    cols: dict[str, str | None] = {}

    plan = [
        ("Job Reference", "board_relation", None, True),
        ("Machine", "board_relation", None, True),
        ("Stage ID", "text", None, False),
        ("Recipe Key", "text", None, False),
        ("Recipe Version", "numbers", None, False),
        ("Quantity", "numbers", None, False),
        ("Capacity (mirror)", "mirror", None, True),
        ("Duration (hrs)", "formula", None, True),
        ("planned_start", "date", None, False),
        ("planned_end", "date", None, False),
        ("actual_start", "date", None, False),
        ("actual_end", "date", None, False),
        ("Dependent On", "dependency", None, True),
        ("Status", "status",
         {"labels": {str(i): l for i, l in enumerate(SCHEDULE_STATUS_LABELS)}}, False),
        ("Manually placed", "checkbox", None, False),
        ("Priority", "status",
         {"labels": {str(i): l for i, l in enumerate(PRIORITY_LABELS)}}, False),
        ("last_reflow_hash", "text", None, False),
        ("drift_last_detected_at", "date", None, False),
    ]

    for title, ctype, defaults, optional in plan:
        if title in existing_titles:
            print(f"  ~ {title} already exists; skipping")
            continue
        cols[title] = col(bid, title, ctype, defaults, optional=optional)
        time.sleep(0.3)

    if unsupported:
        print("\n=== MANUAL FOLLOW-UP REQUIRED ===")
        for u in unsupported:
            print(f"  - Add column '{u['title']}' (type: {u['type']}) via UI")

    # Verify
    b = gql(
        """{ boards(ids: [18414776220]) {
            id name columns { id title type }
        } }"""
    )["boards"][0]
    print(f"\nFinal Schedule board {b['id']} columns:")
    for c in b["columns"]:
        print(f"  {c['id']:40s} {c['title']:30s} {c['type']}")

    out_path = os.path.join(os.path.dirname(__file__), "schedule_manual_followups.json")
    with open(out_path, "w") as f:
        json.dump({"board_id": bid, "unsupported_columns": unsupported}, f, indent=2)
    print(f"\nWrote follow-ups to {out_path}")


if __name__ == "__main__":
    main()
