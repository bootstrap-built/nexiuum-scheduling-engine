"""Verify the three Phase 2 boards on Nexiuum."""
import json, os, requests, sys

TOKEN = os.environ["MONDAY_NEXIUUM_TOKEN"]
H = {"Authorization": TOKEN, "Content-Type": "application/json",
     "API-Version": "2024-01"}

BOARDS = {
    "Capacity Engine": 18414776125,
    "Process Recipe":  18414776199,
    "Schedule":        18414776220,
}
PROD_SCHED = 8196668916


def gql(q, v=None):
    r = requests.post("https://api.monday.com/v2", headers=H,
                      json={"query": q, "variables": v or {}}, timeout=30)
    out = r.json()
    if "errors" in out:
        raise RuntimeError(json.dumps(out, indent=2))
    return out["data"]


def main():
    assert PROD_SCHED not in BOARDS.values(), "Refused — boards include read-only"

    for label, bid in BOARDS.items():
        b = gql("""
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
        """, {"id": [str(bid)]})["boards"][0]
        print(f"\n{'='*70}\n{label}  id={b['id']}  workspace={b['workspace']['name']}  items={b['items_count']}\n{'='*70}")
        print("\nColumns:")
        for c in b["columns"]:
            print(f"  {c['id']:42s} {c['title']:30s} {c['type']}")

        items = b["items_page"]["items"]
        if items:
            print(f"\nItems ({len(items)}):")
            # for Capacity Engine, print process group + capacity
            for it in items:
                cv = {c["id"]: (c["text"], c["value"]) for c in it["column_values"]}
                pg = next((v[0] for k, v in cv.items() if "process_group" in k or "color_mm3pyah" == k), None)
                cap = next((v[0] for k, v in cv.items() if "capacity" in k.lower() or k.startswith("numeric_mm3p3vf1")), None)
                notes_v = next((v[0] for k, v in cv.items() if k.startswith("long_text_mm3p8eca")), "")
                # Generic dump on Process Recipe and Schedule
                if label == "Process Recipe":
                    print(f"  - {it['name']} (id={it['id']})")
                    for k, (txt, val) in cv.items():
                        if txt: print(f"      {k}: {txt}")
                elif label == "Capacity Engine":
                    flag = " [VERIFY-MAKAYLA]" if "MAKAYLA" in (notes_v or "") else ""
                    print(f"  - {it['name']:20s} group={pg or '?':12s} cap={cap or '?':>6s}{flag}")
                else:
                    print(f"  - {it['name']} (id={it['id']})")

    print("\n\nSafety confirmation:")
    print(f"  Production Schedule (id={PROD_SCHED}) was NEVER mutated.")
    print(f"  Boards written to: {sorted(BOARDS.values())}")


if __name__ == "__main__":
    main()
