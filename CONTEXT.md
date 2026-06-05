# Nexiuum Scheduling Engine

The engine that places production work onto machines across the Gray Space and Nexiuum Monday accounts. It reads upstream order signals, derives a stage DAG from recipes, and writes one slot per machine-stage assignment back to per-account Schedule boards.

## Language

### Upstream entities

**Order**:
A per-flavor scheduling unit. One row on the Production Schedule board (Phase 2D+) or, in the legacy Gray Space flow, one row on Blend Records (Phase 1). Has exactly one Recipe, one quantity, and zero or more PackagingSlices. This is what the engine schedules. Identified inside the engine by `order_id` (currently `job_reference_id` — rename pending).
_Avoid_: Job, Item, Flavor

**PO**:
A row on the Quotes/Deals/POs board, identified by an N#. One PO spawns one or more Orders (one per flavor). The engine does not model PO as an entity — it never reads or writes the Quotes/Deals/POs board — but it does carry the PO's **N#** as a traceability attribute on every Order and Slot (see N# below). Upstream lifecycle is Quote → Deal → PO; the engine only cares about a row once it has become a PO and the per-flavor Orders have spawned.
_Avoid_: Deal, Quote, Customer Order

**N#**:
The Nexiuum-side traceability number for a PO. Issued by Nexiuum upstream and carried through every system end-to-end. The engine treats N# as a pass-through attribute on every Order (read from the Production Schedule item's mirror column, which reflects the linked PO's N#) and stamps it on every Slot it writes (text column on both Schedule boards, plus the slot name). Operators use it to trace any Slot, log line, or Marey chart entry back to the originating customer order. The engine does not aggregate by N# or perform PO-level operations — it's a label, not a key into engine logic. Nullable for the legacy Gray-Space-Blend-Records-triggered flow (N# lives only on the printed spec sheet PDF there; adding a Blend Records column is out of scope for this build).

**Flavor**:
The customer-facing differentiator between Orders sharing the same N#. One PS item = one Flavor (e.g., "Strawberry", "Strawberry Banana", "Cherry Lime"). The engine reads the Flavor as a label off the PS item, carries it on Order, and uses it alongside N# on Slot names and Marey lane labels so operators can tell which of the (potentially many) Orders under a single N# they're looking at. Like N#, it's a label not a key.

**Blend Record**:
A row on the Gray Space Blend Records board representing the blending/pressing work for one Order. Created by an upstream Monday workflow when a Production Schedule item that requires blending/pressing is created — carrying the spec-sheet data — and landing in `Received`. Its **Blend Status** is the engine's press-scheduling trigger: when Gray Space flips it `Received → Blending`, the engine releases that Order's press + downstream stages onto the schedule (see Reactive scope). Later transitions drive actuals: `Pressing → actual_start`, `Done → actual_end`. The engine reads this board (status webhooks) but never creates or writes it.
_Avoid_: Batch, Press Record

### Monday boards

The engine touches the following Monday boards; relationship in parentheses.

**Quotes/Deals/POs** (Nexiuum) — upstream customer-order board. Source of truth for POs and N#. *(Not touched by the engine.)*

**Production Schedule** (Nexiuum, board `8196668916`) — one row per Flavor. Engine reads the Spec Sheet Payload JSON column on PS item change events to ingest Orders. *(Read-only — currently as a build-time safety guard to avoid impacting live orders; not a permanent boundary. Per-touch human approval required for any write.)*

**Process Recipe** (per-instance — both Gray Space and Nexiuum have one) — versioned Processes (stage DAGs). *(Read-only.)*

**Capacity Engine** (per-instance — Gray Space board `18413803163` + Nexiuum equivalent) — Machine metadata. *(Read + limited write — engine writes only `last_job_ended_at`.)*

**Schedule** (per-instance — Gray Space board `18413802995` + Nexiuum equivalent) — Slots. *(Read + write — the engine's primary write target. N# and Flavor are also text columns here, stamped on every SlotWrite.)*

**Blend Records** (Gray Space, board `18404836849`) — production-floor blending/pressing records. *(Receives status webhooks only — Pressing fires `ActualStartReported`, Done fires `ActualEndReported`. Engine does not read or write the board itself; it writes back to the linked Schedule Slot.)*

### Engine internals

**Process** (or **Production Process**):
The ordered sequence of Stages an Order goes through (blending, pressing, blistering, clamshelling, boxing, etc.). Versioned by `process_key + process_version`. Lives on the Process Recipe board (Monday board name — internal term is just Process). Upstream supplies only the `process_key`; the engine resolves to the Active version at scheduling time and pins it on the Order (ADR-0003). Once pinned, an Order never silently swaps Process versions.
_Avoid_: Recipe (ops uses "recipe" for blending ratios — a different thing)

**Process Status**:
The lifecycle status of a Process version on the Process Recipe board. Three values: `Draft` (work-in-progress; not selectable for new Orders), `Active` (the publishable, schedulable version for its `process_key`), `Retired` (no longer used by new Orders, but in-flight Slots pinned to it still resolve correctly). Invariant: at most one Active version per `process_key` at any time.

**Process version pinning**:
The engine's commitment that an Order's Process version is locked at scheduling time and never silently changed. New Orders resolve their version via Active-lookup (engine-side); the resulting `(process_key, process_version)` is stored on the Order and on every Slot the engine writes. In-flight Slot resolution uses the exact pinned version (never falls back to a different one), so mid-flight edits to the Process Recipe board don't retroactively change running plans.

**Stage**:
A node in a Process — the unit of work performed on one ProcessGroup of Machines (e.g. blending, pressing, blistering). Has zero or more upstream dependencies inside the Process DAG. Two sub-kinds exist; "Stage" alone means the default (declared) kind.
_Avoid_: Step

**Packaging Stage**:
A Stage fabricated by the engine per Order from a PackagingSlice. Not declared on the Process Recipe board — synthesised at plan time and hung off the Process's terminal Stages. Identified by `stage_id = pkg_<idx>_<machine_class>`. Exists only for the duration of that Order's plan.

**PackagingSlice**:
An entry in `Order.packaging_breakdown`. Becomes exactly one Packaging Stage. Carries `machine_class`, `quantity`, `items_per_container` (drives container-rate capacity math), and a free-text `config_notes` (operator-facing — e.g., "3 count diamond" blister).

**Slot**:
The engine's output. One row on a Schedule board representing one `(Order, Stage, Machine)` assignment. A Stage may produce multiple Slots when it splits across machines.

**ProcessGroup**:
The class of a Machine — `Pressing | Capsule | Sachet | Blister | Clamshell | Bottle | Lot Coder | Hand-pack`. A Stage is routed to one or more Machines of matching ProcessGroup. Carries two derived properties: **container-rate capacity** (capacity is containers/hr, not items/hr — set today: `{Sachet, Blister, Clamshell, Bottle}`) and **splittability** (same set today, but tracked independently so the two rules can drift later).
_Avoid_: Machine class (the code uses this synonym; prefer ProcessGroup in conversation)

**Instance**:
Which of the two Monday accounts a row originated from or is destined for. Two values: `gray_space` (Gray Space subsidiary) and `nexiuum` (parent company). Every Machine, Slot, and SlotWrite carries an instance. On every event the engine builds a Snapshot that merges Machines, Processes, and Slots from both accounts; the placer treats them as one pool; each SlotWrite is then routed back to its placed Machine's instance's Schedule board. Cross-account Connect-Boards are deliberately avoided (Phase 2 design lock).
_Avoid_: Account, Tenant, Workspace (operators may say "Monday account" — accepted in conversation, but `Instance` wins in the glossary and code)
**Invariant:** a Slot's instance always equals its placed Machine's instance. Divergence is a bug, not a supported case.

**Drift**:
The gap between the engine's planned schedule and reality. Two kinds the engine recognises: **late start** (`planned_start` has passed but `actual_start` is still None) and **late end** (`planned_end` has passed but `actual_end` is still None). When the Sweep observes drift on a Slot, the engine stamps `drift_last_detected_at` for telemetry — **no other action today** (no reflow). Reflow-on-drift is future work; see Reflow.

**Sweep**:
The polling loop that runs every `drift_sweep_interval_seconds` (default 900 = 15 min). One pass: build a fresh Snapshot, call `find_drift_candidates`, enqueue a `DriftDetected` event per stale Slot. Each Slot then enters a `drift_suppression_minutes` (default 60) window — won't re-fire until that elapses. Acts as the safety net for missed actual-start / actual-end webhooks. Liveness + last error surface via `/health`.

**Echo guard**:
The mechanism by which the engine ignores webhooks fired by its own Monday writes. When the engine writes to a Monday board, those writes generate webhooks that would otherwise re-enter the engine and cause loops or false events. The engine's Monday user id (detected at startup via `{ me { id } }`, overrideable via env) is compared against incoming webhook `userId` — matches are dropped before reaching the worker queue. (Replaces an earlier `last_reflow_hash`-column-based scheme that was architecturally broken.)

**Reflow**:
**Future capability — not implemented today.** The intended reactive re-planning of existing Slots in response to drift, capacity changes, or operator action. Phase 1 / 1.5 / 2 engine only does baton-pass push (ADR-0002) and observational drift stamping; everything else is stub. When built, Reflow will be the broader umbrella concept that includes (a) compact-on-early-finish, (b) re-place-on-capacity-loss, (c) re-prioritise-on-expedite, and (d) re-place-on-manual-reschedule.

**Baton-pass**:
The engine's mechanism for propagating actual completion times to dependent Stages. When a Stage finishes (`ActualEndReported` event), the engine finds Slots for dependent Stages of the same Order whose `planned_start` falls earlier than `event.actual_at + cross_stage_handoff_buffer_minutes` (default 30 min) and pushes their `planned_start` forward. **Push-only — never pulls a dependent earlier** (locked in ADR-0002). Cross-instance baton-passes route naturally through the per-instance write router. Recipe Stages cascade-push their dependent Packaging Stages; Packaging Stages do not cascade-push siblings (the engine gates on `event.stage_id` being a Recipe Stage). Immovable Slots (manually placed, Running, or Done) are skipped.

**Splittability**:
A property of a Stage's ProcessGroup — whether the engine may fan a single Stage across multiple Machines. Today, splittable means `{Sachet, Blister, Clamshell, Bottle}`. When a Stage's quantity ≥ `split_min_quantity` (default 50k) and ≥2 eligible Machines exist, the engine fans it across up to `split_max_machines` (default 4), proportional to capacity via largest-remainder. **Pressing never splits** — every press order goes to exactly one pressing machine (stable ops rule, locked 2026-05-30). Capsule is not splittable today; may be revisited as capsule volume grows.

**Machine**:
One row of the Capacity Engine board. Carries `process_group`, `capacity_per_hour`, `status` (MachineStatus), `hours_per_day`, working-window bounds, `changeover_minutes`, and routing flags (`dual_sided_only`, `max_job_size`, `force_route_condition` mini-DSL). A Machine is available for new placements when its status is Online AND it has nonzero `hours_per_day` AND nonzero `capacity_per_hour`.

**MachineStatus**:
Lifecycle of a Machine. Three values: `Online` (available for placement), `Down` (offline; not selectable), `Scheduled Maintenance` (planned downtime; not selectable).

**Snapshot**:
A point-in-time view of all engine-relevant Monday board state — Machines + Processes + Slots from both Instances. Built fresh at the start of every event handled by the Worker; the engine never caches state across events. Monday is the system of record.

**Plan**:
The output of the engine's pure core for one event — a tuple of SlotWrites + MachineWrites + human-readable notes. The IO shell diffs the Plan against the source Snapshot and emits only the writes that represent actual changes.

**SlotStatus**:
Lifecycle of a Slot. Four values: `Queued` (planned, not yet started), `Running` (`actual_start` stamped, work in progress), `Done` (`actual_end` stamped, work complete), `Blocked` (operator-flagged as on-hold; engine treats as Immovable). Engine sets Queued on create, transitions to Running on `ActualStartReported`, to Done on `ActualEndReported`. Blocked is operator-set only.

**Manually placed**:
A boolean column on each Schedule board that operators toggle to mark a Slot as taken out of engine control. When `True`, the engine treats the Slot as Immovable — Drift detection skips it, future Reflow will skip it, and no engine-driven Plan will write to it.

**Immovable**:
Derived property of a Slot: `True` if `manually_placed` OR `status in {Running, Done}`. Immovable Slots are exempt from any engine-driven Plan. The Slot is still read in every Snapshot (its placement still occupies its Machine for capacity math) but the engine treats it as a fixed constraint, not a candidate for change.

**Worker**:
The engine's single async worker that consumes events off a queue and dispatches them to handlers. Single-worker by design — writes to Monday are serialised so two events for the same Order can't race. Liveness + queue depth surface via `/health`.

**CTP** (Capable-to-Promise) / **`/simulate`**:
The read-only endpoint that answers "if we placed this hypothetical Order now, when could we promise it?" Takes a SimulateRequest (process key, quantity, packaging breakdown, etc.), runs the placement against a fresh Snapshot, returns a projected ship date with a 20% pad plus the binding Machine. Never writes — bypasses the Worker queue entirely.
_Avoid_: ATP (Available-to-Promise — a different industry concept; CTP includes capacity, ATP is inventory-only)

## Reactive scope

The engine reacts to the following events. All other event types in the codebase are placeholders that log only — they will be fleshed out in a later build.

| Event | Source | Engine action |
|---|---|---|
| `ScheduleNewOrder` | direct `/commit` call | Plan and write Slots for the new Order |
| `SpecSheetItemReady` | Monday automation webhook on Production Schedule | Read PS item's Spec Sheet Payload, derive a `ScheduleNewOrder`, plan and write Slots |
| `ActualStartReported` | Blend Records status → "Pressing" webhook | Stamp `actual_start` + `Status=Running` on the linked Slot |
| `ActualEndReported` | Blend Records status → "Done" webhook | Stamp `actual_end` + `Status=Done`, then Baton-pass push to dependent Stages |
| `DriftDetected` | 15-min Sweep | Stamp `drift_last_detected_at` on the late Slot. No Reflow. |
| `CapacityChanged`, `ManualReschedule`, `ExpediteRequested` | stubs | Log only. No engine action today. |
