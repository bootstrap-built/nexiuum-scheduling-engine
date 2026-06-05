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
Space flipping the Blend Record `Received → Blending`.

The Blend Record is being moved upstream: a Monday workflow will create it (with
the spec-sheet data) when a PS item that requires blending/pressing is created,
landing it in `Received`. So the Blend Record becomes an **input signal**, not a
side-effect of engine scheduling.

The engine already classifies orders by `manufacturing_route` via `ROUTE_RULES`
(`should_schedule`, `include_press`, `priority`), but `build_schedule_order`
currently honors only `should_schedule` — `include_press` and `priority` are
computed and discarded.

## Decision

The scheduling trigger differs by manufacturing route:

- **Manufacturing** (`include_press=True`): `create_item` is a **no-op** for
  machine placement. The engine builds the order's **entire** plan — press
  **and** all downstream packaging — when it receives the new **`Blending`**
  event (Blend Status `Received → Blending` on the Blend Records board).
- **Kitting / Packaging-only** (`include_press=False`): schedule on
  `create_item`, **skipping the press stage** (this finally wires `include_press`
  rather than relying on the Process DAG to omit press).
- **Samples / no payload**: skip (unchanged).

An approved manufacturing order that hasn't reached `Blending` yet is **not**
placed on any machine; it surfaces in a derived **backlog lane** (#21) —
"PS item exists, no Slots yet." The engine persists nothing at `create_item`;
backlog is derived from Monday (the engine stays stateless).

## Considered alternatives

- **Schedule on approval (current #8 behavior).** Rejected — places work on
  machines before it's real, churns the schedule as un-started orders drift, and
  ignores active availability. Explicitly rejected in the meeting.
- **Packaging on `create_item`, press on `Blending`.** Rejected — splits a
  single order's chain across two trigger moments; the meeting wanted the whole
  press→packaging chain to appear together when Blending starts.

## Consequences

- **Blocked on a Blend Record ↔ PS Order correlation key (#23).** The `Blending`
  event arrives keyed by Blend Record id; the Order/Slots are keyed by PS item
  id. The engine needs a reliable link (resolving #23 also fixes a latent
  Phase 2D actuals correlation gap).
- **Blocked on upstream changes (other repos):** the form must emit the payload
  the backlog/derivation needs, and the Blend Record must be created on
  PS-item creation carrying the correlation key. Engine trigger work resumes
  once those land.
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
