---
status: accepted
---

# Press scheduling triggers on Blend Status → Blending, not on order approval

## Context

Phase 2D wired a Monday `create_item` webhook (#8) so a new Production Schedule
item ingests and **schedules immediately** — press onto the Gray Space Schedule
board, packaging onto the Nexiuum Schedule board, all at PS-item creation.

The 2026-06-03 Development Meeting rejected that timing for manufacturing
orders: scheduling at approval places work on machines before anyone starts it
and before active-ingredient availability is known, producing a schedule that
looks authoritative but isn't. The real "this is happening now" signal is Gray
Space setting the Blend Record's Blend Status to `Blending`.

The Blend Record is moved upstream: a Monday workflow creates it (with the
spec-sheet data) when a PS item that requires blending/pressing is created, and
it lands with a Blend Status that is **not yet `Blending`** (the live board's
status set is `Pending / Blending / Pressing / Dedusting/Packaging / Done /
Stuck`; new records start at `Pending`). So the Blend Record becomes an **input
signal**, not a side-effect of engine scheduling.

The engine already classifies orders by `manufacturing_route` via `ROUTE_RULES`
(`should_schedule`, `include_press`, `priority`), but `build_schedule_order`
currently honors only `should_schedule` — `include_press` and `priority` are
computed and discarded.

## Decision

The scheduling trigger differs by manufacturing route:

- **Manufacturing** (`include_press=True`): `create_item` is a **no-op** for
  machine placement. The engine builds the order's **entire** plan — press
  **and** all downstream packaging — when it receives the new **`Blending`**
  event (Blend Status set to `Blending` on the Blend Records board).
- **Kitting / Packaging-only** (`include_press=False`): schedule on
  `create_item`, **skipping the press stage** (this finally wires `include_press`
  rather than relying on the Process DAG to omit press).
- **Samples / no payload**: skip (unchanged).

An approved manufacturing order that hasn't reached `Blending` yet is **not**
placed on any machine; it surfaces in a derived **backlog lane** (#21) —
"PS item exists, no Slots yet." The engine persists nothing at `create_item`;
backlog is derived from Monday (the engine stays stateless). The `/view`
renderer draws each backlog entry as an **estimated-duration bar** in a Backlog
lane at the bottom of the chart, so ops can see "there's a 2M-tab order coming"
and roughly how long it will take — *without* implying a machine or a committed
time. The estimate uses the **backlog rate** (below); the bar is a read-layer
projection, not a written Slot.

## Considered alternatives

- **Schedule on approval (current #8 behavior).** Rejected — places work on
  machines before it's real, churns the schedule as un-started orders drift, and
  ignores active availability. Explicitly rejected in the meeting.
- **Packaging on `create_item`, press on `Blending`.** Rejected — splits a
  single order's chain across two trigger moments; the meeting wanted the whole
  press→packaging chain to appear together when Blending starts.

## Consequences

- **Blend Record ↔ PS Order correlation key (#23) — built.** The `Blending`
  event arrives keyed by Blend Record id; the Order/Slots are keyed by PS item
  id. The engine resolves the link by reading the Blend Record's `text_mm1mjk8n`
  (`engine/io/blend_records_io.py`). The same read also closes a latent Phase 2D
  actuals correlation gap.
- **Upstream + Monday wiring already live (verified 2026-06-08).** No cross-repo
  work was a prerequisite: the Production Schedule board carries live
  `create_item` webhooks (form → PS item, blend_intake → Blend Record with the
  correlation key), and the Blend Records board carries a live status webhook
  filtered to **Blending** (index 0 on the live board). That webhook was already
  firing into the engine and hitting the "not actionable" branch — this ADR's
  handler is the only piece that was missing. End-to-end goes live on the next
  engine deploy.
- `build_schedule_order` must start honoring `include_press` (and, separately,
  `priority` — currently dropped; relevant to the parked priority-bump feature).
- A new `Blending` event joins the reactive scope; the `SpecSheetItemReady`
  entry's "plan and write Slots" behavior becomes route-gated (no machine
  placement for manufacturing orders). Update CONTEXT.md's Reactive scope table
  when the trigger is built.
- `#8`'s create_item auto-schedule is superseded for manufacturing orders — its
  payload-absent skip stays; its scheduling becomes route-gated.
- Plan-computation mechanics (inter-stage buffer, working-window, batch-blend
  duration, backlog lane) are independent of this trigger and not blocked.
- **Backlog estimate is derived, not materialized (reconciled 2026-06-08).** We
  considered materializing backlog as real Slots on a synthetic "Backlog"
  machine so the bars would be first-class timeline rows. Rejected — it would
  contradict this ADR's "engine persists nothing at `create_item`" and #21's
  "without implying a machine/time placement," and it would add engine surface
  (a synthetic machine excluded from every capacity/placement/drift enumeration,
  a `SlotStatus="Backlog"`, and replace-on-Blending). Instead the backlog set
  and its duration estimates are computed in the read/view layer; the engine
  writes nothing until Blending.
- **Backlog rate** (the per-entry duration estimate): a backlogged order's real
  duration depends on which press it eventually lands on, which isn't known
  until Blending. The estimate therefore uses one representative rate — the
  **slowest general-purpose press**: the minimum `capacity_per_hour` among
  *online* Machines in the order's pressing ProcessGroup, **excluding** any
  Machine flagged `dual_sided_only` or carrying a `max_job_size` cap (the
  purpose-built small / dual-batch presses, slow by design — including them
  would make every estimate pessimistic). If every eligible Machine is
  special-purpose, fall back to the slowest of those. The estimate is visibility
  only — not a capacity reservation and not a customer promise.
