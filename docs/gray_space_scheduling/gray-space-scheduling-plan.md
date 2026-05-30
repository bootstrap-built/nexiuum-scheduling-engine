# Gray Space + Nexiuum Capacity & Scheduling — Build Plan (v3)

**Version:** 3 (2026-05-20). Supersedes v2 by integrating Codex's adversarial review.
**Status:** Ready for implementation.
**Hosting target:** Nexiuum EC2 (existence/provisioning still TBD — see Open Items).
**Phase 1 done definition:** Pressing engine **live in production** — scheduling real new orders, writing real planned dates to Monday, source-board webhooks flowing actuals, operators using it. Bar is high; iteration expected once running with real data.

**Audit trail:**
- v2 plan: see prior commit
- Codex adversarial review: `codex-review-v2.md`
- Resolutions integrated here: `resolutions-v2-to-v3.md`

---

## What changed from v2

1. **Concurrency model defined.** Single async worker, serialized write queue, CTP `/simulate` reads bypass.
2. **Job state machine defined.** Engine writes Status transitions atomically with actuals. `Blocked` reserved for operator override + auto-flag on dangling Job References.
3. **Polling sweep idempotency.** New `drift_last_detected_at` column on Schedule items; 60-min suppression window prevents reflow storms.
4. **`Predecessor` → `Dependent On`.** Drop Predecessor Connect-Boards column. Use Monday's native `dependency` column type (`Dependent On`) so Gantt apps render arrows.
5. **Recipe version pinning codified as engine invariant.** Engine always reads recipes by `recipe_key` AND `recipe_version` from the Schedule item. Dangling recipe → `Blocked`. Startup integrity check warns on duplicate composite keys.
6. **SSE dropped; Monday SDK board events are the change channel** (locked 2026-05-20 after 1.5B0 spike). Engine has no live-push channel. View subscribes to Monday board events via `monday.listen('events', cb)`, then re-fetches via the SDK on each event. Uses Monday's own real-time infrastructure — zero external connect-src, no SSE plumbing, no CSP risk. The data contract for `nodes[]` (with `actual_t0`/`actual_t1`/`drift_state`) is unchanged; it's just delivered via SDK fetch rather than SSE push.

Everything else from v2 — three boards + engine, flat Schedule items, Monday as system of record, capacity-changes-via-formula, machine inventory, smart routing rules, two-phase delivery — stays.

---

## Locked decisions (carried + new)

1. **`Job Reference` source board (Phase 1) = Gray Space `Blend Records` (board 18404836849).** Single-account for Phase 1. Cross-account work deferred to Phase 2.
2. **`Recipe Key` assignment = Monday automation** on the upstream order board. Engine just reads. Subject to revisit if brittle.
3. **`Hold` node kind = renderer-only artifact in v1.** Engine never emits `kind: hold` nodes.
4. **Phase 1 done bar = live in production.** Real orders, real actuals, operators using it.
5. **Status transitions = engine-via-webhook (new, locked 2026-05-20).** User's status flip on Blend Records is the source-of-truth event. Engine receives webhook, resolves Connect-Boards link, writes `actual_start`/`actual_end` + `Status` atomically to all linked Schedule items.
6. **Change channel = Monday SDK board events (locked 2026-05-20 after spike).** Embedded view subscribes via `monday.listen('events', cb)` and re-fetches via Monday SDK GraphQL on each event. No SSE, no WebSocket, no external connect-src. Spike confirmed CSP allowed external SSE (Wikimedia stream succeeded), but Monday SDK events is simpler and uses Monday's own real-time bus. Sub-second push for free, no engine-side connection management.

### Open later (not blocking implementation)
- **Recipe authoring UX.** JSON-in-column is fine for the build but Zane/Michaela can't hand-edit JSON. Sketched approach: Process Recipe stages become subitems with `machine_class` + `depends_on` columns; engine-side serializer rolls them up into canonical JSON. Decide when the first non-engineering recipe edit is needed.
- **Monitoring & alerting.** Detection of engine failure, missed webhooks, stuck polling sweep. Recommended: Sentry + a `/health` endpoint that polls Monday read latency.

---

## Phase 2 addendum — locked 2026-05-25

Five junction decisions resolved while greenlighting Phase 2 build agents. These supersede any conflicting phrasing in the Phase 2 sequencing section below.

7. **Spec Sheet form → engine trigger.** Form writes payload to Nexiuum Production Schedule item; Monday automation on that board fires HTTP webhook to engine `/commit` when the Recipe Key column populates. Engine reads the Spec Sheet Payload JSON from `long_text_mm3bbhcv` to drive stage explosion. (Resolves the "Coordinate so this lands by 2.A" note in Parallel — Spec Sheet Form.)

8. **Recipe topology = single recipe spans both instances.** A multi-stage recipe lives on **Nexiuum's** Process Recipe board (Nexiuum owns customer orders). Stages reference `machine_class`. The engine knows which instance's Capacity Engine owns each class (e.g., `Pressing` → Gray Space, `Blister` → Nexiuum) and routes each stage to the right instance. **No duplicate recipes across instances.**

9. **Two Schedule boards, one unified view (revised 2026-05-25 — resolves conflict with 2.C).** Each instance keeps its own Schedule board. Press slots write to **Gray Space Schedule** (native in-account Connect-Boards → Gray Space Capacity Engine). Packaging slots write to **Nexiuum Schedule** (native in-account Connect-Boards → Nexiuum Capacity Engine). No cross-account Connect-Boards anywhere — honors 2.C's rejection of cross-account links as fragile. The Marey chart on the Nexiuum-side embedded view reads engine `/schedule.json`, which fans across BOTH Schedule boards and returns a unified, instance-tagged slot list. Operators in each workspace see their own slots natively in their workspace; the unified Marey view lives where Nexiuum AMs need it. **Prior Phase 2 addendum wording ("single Nexiuum-side Schedule board holds all slots") was an overreach that contradicted 2.C and is superseded by this.**

10. **Manufacturing Route → stages mapping (working defaults).** Recipe selection key derived from the form's `Manufacturing Route` value:
    - `Manufacturing` → press stage only (Gray Space)
    - `Manufacturing + Packaging` → press → packaging DAG (Gray Space → Nexiuum)
    - `Packaging` → packaging only (Nexiuum, no press stage)
    - `Ship Bulk` → press only, emits `kind: handoff` terminal node
    - `Keep for Packaging` → press only, packaging deferred (no packaging slot scheduled until status changes)
    - `Hot Shot` / `Samples` → **TBD with Makayla**, treat as press-only until resolved

11. **Production Schedule board (8196668916) is read-only for the engine and all build agents.** No item creation, column updates, or test writes without Josh's explicit per-touch approval. This is the live customer-order intake. The engine ingests by reading the Spec Sheet Payload column and reacting to the Monday automation HTTP webhook — never by writing back.

12. **Bella PO upload intake coexists.** The existing PO xlsx → Gray Space Blend Records flow stays running through Phase 2. Spec Sheet form is the new intake path for new orders. Sunset path for the legacy flow is out of scope for Phase 2; revisit after end-to-end verify.

13. **Hosting interim stays bb-infra-01.** Migration to Nexiuum EC2 deferred until full Phase 2 verifies end-to-end. Service-account work (Open item #6 in resume-state) is therefore not Phase 2-blocking.

---

## Three boards — final schemas

### Board 1: Capacity Engine

One item per machine. Operator-facing.

| Column | Type | Purpose |
|---|---|---|
| Name | Name | Machine identifier |
| Process Group | Status | Pressing / Capsule / Sachet / Blister / Clamshell / Bottle / Lot Coder / Hand-pack |
| Status | Status | Online / Down / Scheduled Maintenance |
| Capacity (units/hr) | Numbers | Per hour |
| Hours per day | Numbers | Default 16 |
| Working window start / end | Numbers | Hour-of-day |
| Changeover buffer (min) | Numbers | Default 30 |
| Dual-sided only | Checkbox | TRUE on Penn & Teller |
| Max job size | Numbers | 10,000 on Copperfield |
| Force-route condition | Text | e.g., `active_mg > 80` on Lancelot |
| Last job ended at | Date+hour | Engine-written |
| Notes | Long text | — |

Phase 1: Gray Space Monday, loaded with 6 presses + Elphaba. Phase 2: Nexiuum instance with packaging machines.

### Board 2: Process Recipe

One item per recipe **version**. Identified by `recipe_key` + `version`. Edits create new versions; in-flight jobs keep their original version pin.

| Column | Type | Purpose |
|---|---|---|
| Name | Name | Human label (e.g., `Clamshell tablet — kratom`) |
| Recipe Key | Text | Stable key across versions |
| Version | Numbers | Increments on edit; old versions retained |
| Status | Status | Draft / Active / Retired |
| Stages | Long text (JSON) | Ordered list of stages — schema below |
| Notes | Long text | — |

**Stages JSON schema:**

```json
[
  { "id": "press",     "machine_class": "Pressing",  "depends_on": [] },
  { "id": "blister",   "machine_class": "Blister",   "depends_on": ["press"] },
  { "id": "lotcode",   "machine_class": "Lot Coder", "depends_on": ["press"] },
  { "id": "clamshell", "machine_class": "Clamshell", "depends_on": ["blister", "lotcode"] }
]
```

Recipes name **classes**, not instances. Router picks instance at scheduling time.

**Phase 1 recipes:** one-stage, press-only. **Phase 2 recipes:** multi-stage packaging DAGs.

**Integrity check on engine startup:** scan board for duplicate (`recipe_key`, `version`) pairs; warn to logs/monitoring. Don't crash.

### Board 3: Schedule (flat items)

One row per machine-job pairing.

| Column | Type | Purpose | Change vs v2 |
|---|---|---|---|
| Name | Name | `{Job ref} → {Machine}` | — |
| Job Reference | Connect-Boards | Link to source job/blend item | — |
| Machine | Connect-Boards | Link to Capacity Engine item | — |
| Stage ID | Text | Which recipe stage (`press`, `blister`, etc.) | — |
| Recipe Key | Text | Stamped at instantiation | — |
| Recipe Version | Numbers | Stamped at instantiation | — |
| Quantity | Numbers | — | — |
| Capacity (mirror) | Mirror | From Machine | — |
| Duration (hrs) | Formula | `Quantity / Capacity` | — |
| `planned_start` | Date+hour | Engine-written, canonical | — |
| `planned_end` | Date+hour | Engine-written, canonical | — |
| `actual_start` | Date+hour | Engine-written from source-board webhook | — |
| `actual_end` | Date+hour | Engine-written from source-board webhook | — |
| **Dependent On** | dependency | Native Monday dependency column; renders arrows in Gantt apps | **REPLACES Predecessor (was Connect-Boards in v2)** |
| Status | Status | Queued / Running / Done / Blocked | — |
| Manually placed | Checkbox | Engine respects; doesn't auto-reflow this item on capacity/new-order changes. Expedite still bumps. | — |
| Priority | Status | Normal / Expedite | — |
| `last_reflow_hash` | Text | Engine idempotency marker (webhook path) | — |
| **`drift_last_detected_at`** | Date+hour | Engine-written; polling sweep idempotency marker | **NEW** |

---

## Engine architecture

### Pure core + IO shell

Placement is a **pure function**:

```
(board_snapshot, hypothetical_order_or_none) → schedule_plan
```

Pure core reused unchanged for:
- Live placement (real order → write back to Monday)
- CTP simulation (hypothetical order → return projected dates, no writeback)

### Concurrency model

**Single async worker, serialized write queue. Read-only paths bypass.**

- **Write path:** Webhook handlers + polling sweep both push events into one `asyncio.Queue`. A single worker coroutine drains the queue serially. One reflow runs at a time.
- **Read path:** CTP `/simulate` HTTP endpoint runs concurrently. Reads Monday state fresh, runs pure core, returns the projected date. No queue contention with the writer.
- **Snapshot freshness:** Worker reads current Monday board state at the start of each event (not from a cached snapshot). Prevents stale-read races.

```python
async def on_webhook(event):
    await event_queue.put(event)
    return {"accepted": True}

async def worker():
    while True:
        event = await event_queue.get()
        try:
            snapshot = await read_monday_state()
            plan = pure_core(snapshot, event)
            await apply_plan(plan)  # batched writeback
        except Exception:
            log.exception("reflow failed for event %s", event.id)
        finally:
            event_queue.task_done()
```

Throughput is not a concern at Phase 1 scale. Correctness via serialization is cheap. Scale to a worker pool later only if queue depth exceeds 10.

### Job state machine

| From | To | Trigger | Writer |
|---|---|---|---|
| (init) | `Queued` | Schedule item created by engine | Engine |
| `Queued` | `Running` | Source-board webhook posts `actual_start` to Schedule item | Engine (atomic with `actual_start`) |
| `Running` | `Done` | Source-board webhook posts `actual_end` | Engine (atomic with `actual_end`) |
| `Queued` / `Running` | `Blocked` | Manual operator action OR engine detects dangling Job Reference | Operator OR Engine |
| `Blocked` | `Queued` | Manual operator action | Operator |

**Engine behavior per status:**
- `Queued`: visible to reflow; eligible for placement decisions
- `Running`: **immutable** — engine cannot move or shift; only respects `actual_start` and continues reflow around it
- `Done`: visible only for `Last job ended at` calculation on the machine; otherwise ignored
- `Blocked`: invisible to reflow; engine treats as if the slot doesn't exist; polling sweep ignores

### Timezone convention

Monday's Date columns store time as **UTC**; the UI displays in the viewer's account timezone. The engine works internally in local time (`America/Denver` — MDT/MST with DST) since the factory operates on local clock. At every write boundary to a Monday Date column, the engine converts local → UTC. At every read boundary, the engine converts UTC → local.

```python
from zoneinfo import ZoneInfo
from datetime import datetime

FACTORY_TZ = ZoneInfo("America/Denver")
UTC = ZoneInfo("UTC")

def local_to_monday(local_dt: datetime) -> dict:
    """Convert local datetime to Monday's date+time column format (UTC)."""
    utc = local_dt.replace(tzinfo=FACTORY_TZ).astimezone(UTC)
    return {"date": utc.strftime("%Y-%m-%d"), "time": utc.strftime("%H:%M:%S")}

def monday_to_local(date_value: dict) -> datetime:
    """Convert Monday's date+time column value to local datetime."""
    utc = datetime.fromisoformat(f"{date_value['date']}T{date_value['time']}").replace(tzinfo=UTC)
    return utc.astimezone(FACTORY_TZ)
```

Webhook timestamps (e.g., source-board status flip times) are already UTC and write directly to `actual_start`/`actual_end` without conversion. Display layer handles local rendering automatically via Monday's account timezone setting.

### Read / write contract with Monday

**Monday is system of record.** Engine state is fully reconstructible from board state — no scheduling-relevant data lives only in engine memory.

**Writeback discipline:**
- Diff against the read snapshot before writing. Only write changed items.
- Batch writes per reflow. Monday's GraphQL complexity budget will throttle a naive full-rewrite at 100+ jobs.
- Engine stamps `last_reflow_hash` on every item it writes.

**Echo guard (webhook path):**
- The engine's own writes fire `Schedule item modified` webhooks. Engine must not reflow in response to its own writes.
- On webhook receipt, compare `last_reflow_hash` against the engine's last-written value. If match, ignore.
- Applies only to Schedule board. Source-board events (Blend Records, Press Room Tracking, packaging-side) never collide with this guard.

**Polling sweep idempotency:**
- See "Actuals" section. The `drift_last_detected_at` column provides per-item suppression.

**Crash recovery:**
- On startup, engine reads board state and rebuilds. No local persistence required for correctness.

### Reflow scope

**Local-only, with expedite as the one exception.**

| Trigger | Reflow scope |
|---|---|
| New Production Order | Slot into open space on eligible machines. Never displaces queued jobs. |
| Capacity change (capacity, hours, window, status→Down) | Only that machine's queue reflows. Jobs redistributed to other eligible machines if needed. |
| Schedule item dragged manually | `Manually placed = TRUE` set. Other jobs on that machine reflow around it. |
| Schedule item Status → Done | Update `Last job ended at` on the machine. No reflow. |
| `actual_start` >15 min late | Local reflow on that machine. Downstream slots (via `Dependent On` chain) shift accordingly. |
| `actual_end` >15 min late | Same as above. |
| Priority → Expedite | Expedited job claims next-available slot on its target machine, bumping queued jobs (including `Manually placed`) later on the same machine. No cascade to other machines. Bumping a `Manually placed` slot emits a Monday Update on that slot naming the expedited job and the new `planned_start`. |

**Never:** global re-optimize across machines. **AM-driven rebalancing:** AMs drag jobs manually if expedite leaves a machine lopsided.

### Routing rules

Unchanged from v2.

**Hard (forced) — applied first, in order:**
1. Dual-sided tablets → Penn & Teller
2. High-active blends (>80 mg) → Lancelot
3. Jobs <10,000 tabs → Copperfield (priority, not exclusive)

**Soft — applied when no hard rule fires:**
1. Round-robin to next available eligible machine
2. Same-job multi-flavor distribution across machines so they finish around the same time

**Manual override:** always available. **Expedite permissions:** Jason / Zane / Michaela.

### Recipe lookup invariant

Engine MUST read recipes using BOTH `recipe_key` AND `recipe_version` from the Schedule item. Never re-query by key alone.

```python
def load_recipe_for_slot(slot):
    recipe = process_recipe_board.find(
        recipe_key=slot.recipe_key,
        recipe_version=slot.recipe_version,
    )
    if recipe is None:
        raise DanglingRecipeError(slot)  # engine flips Status → Blocked
    return recipe
```

If the exact composite key can't be found (recipe deleted, version retired and removed, etc.), the engine raises `DanglingRecipeError` and marks the affected Schedule item `Status=Blocked` with a Monday Update explaining what happened. Engine does not silently fall back to a newer version.

### Webhook triggers

| Source | Trigger | Engine action |
|---|---|---|
| Schedule (own board) | Production Order created/modified | Stage explosion + routing + placement |
| Capacity Engine | Status → Down, Hours/Window/Capacity changed | Local reflow that machine |
| Schedule | Priority → Expedite | Local reflow with bump |
| Schedule | Drag-to-reschedule | Mark `Manually placed`, reflow others around it |
| Schedule | Status → Done | Write `Last job ended at`, no reflow |
| Source boards (Blend Records, Press Room Tracking, packaging-side) | Status → "in progress" equivalent | Resolve linked Schedule items via Job Reference; write `actual_start` + `Status=Running` atomically. If drift >15 min, trigger local reflow. |
| Source boards | Status → "complete" equivalent | Resolve linked Schedule items; write `actual_end` + `Status=Done` atomically. If drift >15 min, trigger local reflow. |

Every Schedule-item-modified event passes through the echo guard before any reflow logic runs.

---

## Actuals — event-driven with polling safety net

### Trigger flow (user-originated)

The **user** originates the timing event. A user moves a Blend Records item from "Blending" → "Pressing" — that flip is the source-of-truth signal that work has started. The engine doesn't decide when work happens; it bridges the user's action to the Schedule board.

```
User flips Blend Records status: Blending → Pressing
       ↓
Monday fires webhook to engine
       ↓
Engine resolves Job Reference (1 Blend Record → 1..N Schedule items)
       ↓
For each linked Schedule item with Stage = "press":
  Write actual_start = webhook timestamp
  Write Status = Running
  (one GraphQL mutation, atomic)
       ↓
Engine computes drift = actual_start − planned_start
If |drift| > 15 min: local reflow on that machine
```

**Multi-flavor fan-out:** A single Blend Records item may link to multiple Schedule items (e.g., a 5-flavor PO produces 5 Schedule items, all with `Job Reference = same Blend Record`). The engine resolves the full set and writes actuals to each. This is explicit, not implicit.

### Phase 1 source-board mapping

**Press stage:** Status column on Blend Records board (18404836849). Exact status values and column ID to be confirmed during 1.5B0 discovery with Jason/Zane. Expected pattern:
- "Blending" → "Pressing" = press `actual_start`
- "Pressing" → "Complete" (or equivalent) = press `actual_end`

### Phase 2 source-board mapping

To be discovered with Michaela for packaging stages. Same mechanism; different source boards (likely Blend Records continuing, plus packaging-side boards).

### Polling safety net (every 15 minutes)

Catches missed webhooks and never-started jobs.

```python
async def polling_sweep():
    now = utcnow()
    suppression = timedelta(minutes=60)

    # Late starts
    late_starts = await query_schedule_items(
        status="Queued",
        planned_start__lt=now - timedelta(minutes=15),
        actual_start__isnull=True,
    )
    for item in late_starts:
        if item.drift_last_detected_at and (now - item.drift_last_detected_at) < suppression:
            continue  # already handled within suppression window
        await event_queue.put(DriftEvent(item, kind="late_start"))
        # Worker writes drift_last_detected_at as part of reflow

    # Late ends — same shape against Status=Running, planned_end past, actual_end empty
    ...
```

**60-min suppression window:** A persistently-late job triggers at most one reflow per hour. The webhook path naturally clears the late condition when the real status flip arrives (actual_start becomes non-null, polling query no longer matches). No explicit cleanup of `drift_last_detected_at` needed.

**Configurable:** Suppression window via env var if Jason wants finer/coarser cadence.

---

## Embedded view contract

The embedded view (Marey / string-line) reads schedule data from Monday via the Monday SDK and subscribes to board events for change notifications. **No SSE, no WebSocket, no engine-to-view connection.** All live updates ride Monday's own real-time bus.

### Change-notification flow

```
Engine writes to Monday Schedule board (or Capacity Engine, etc.)
       ↓
Monday's real-time bus fans out the change event to all subscribers
       ↓
Embedded view receives event via monday.listen('events', cb)
       ↓
View re-fetches affected board(s) via monday.api() GraphQL
       ↓
View re-renders the Marey diagram
```

### Data the view reads

The embedded view reads from three Monday boards directly via the SDK:
- Schedule (planned/actual times, machine assignment, predecessors)
- Capacity Engine (machine names, capacities — for lane labels and capacity ripple)
- Process Recipe (stage DAG resolution — only when a job's recipe is unfamiliar)

### Renderer data shape (after SDK normalization)

The view normalizes raw Monday data into this internal shape for rendering. Engine never emits this format — it's purely a view-side construct.

```js
{
  lanes: [{ id, name, group: 'press' | 'pkg', capacity: string }],
  jobs:  [{
    id, label, color,
    nodes: [{
      id, lane, t0, t1, kind: 'run',
      actual_t0?: number | null,        // present when actual_start written on the slot
      actual_t1?: number | null,        // present when actual_end written
      drift_state?: 'on_time' | 'late_start' | 'late_end' | 'completed' | null,
    }],
    edges: [[from_node_id, to_node_id, 'flow']],
    meta:  { order, qty, pin }
  }],
  epoch: ISO string,
  span:  [min_offset, max_offset]
}
```

**Renderer rules:**

| `drift_state` | Visual |
|---|---|
| `null` (planned, not yet relevant) | Bar at `[t0, t1]`, normal opacity |
| `on_time` (actual_t0 set, actual_t1 empty, now between them) | Bar at `[actual_t0, t1]` with pulsing "running" indicator |
| `late_start` (now > t0, actual_t0 empty) | Bar at `[t0, t1]` with red left-edge marker / LATE badge |
| `late_end` (actual_t0 set, now > t1, actual_t1 empty) | Bar extending past planned t1 with hatched overflow region |
| `completed` (both actuals set) | Bar at `[actual_t0, actual_t1]`, reduced opacity |

`drift_state` is computed view-side from the planned/actual columns. The view subscribes to `now` ticks (every 30s) to recompute `drift_state` even without a Monday event.

`nodes.lane` resolves to Capacity Engine items. `edges` come from joining the job's `Recipe Key` + `Recipe Version` to the Process Recipe board — never stored redundantly per-slot. `kind: 'hold'` reserved for future use; engine does not emit it in v1.

### Why not SSE

The 1.5B0 spike confirmed Monday's CSP **allows** external EventSource (the Wikimedia stream succeeded inside a Monday board view). SSE would work. But:

- Monday SDK events provide push semantics for free, no external connect-src
- Engine doesn't need to maintain long-lived connections or handle reconnect logic
- The Schedule's update rate (tens of events per day) doesn't need sub-second push
- One less moving part in the architecture

Spike record: `~/projects/clients/nexiuum/scheduling-engine/spike/`.

### Phase 1 vs Phase 2

**Phase 1 footprint:** single lane group (press), 6+ lanes, one stage per recipe. Exercises the full pipeline (engine writes → Monday → SDK event → renderer).

**Phase 2 footprint:** adds packaging lanes, multi-stage recipes, fork/merge geometry.

### Hosting

Custom Monday Apps Framework board view served from Nexiuum EC2 (or an interim host until that's available). All UI logic in the iframe; data sourced from Monday SDK calls within the iframe. No direct engine connection from the iframe at all in v1 — even the CTP `/simulate` lookup is a separate Smart Embed View (different iframe, different lifecycle, simple fetch POST to the engine).

---

## Sequencing

### Phase 1 — Gray Space (this week)

**1.5B0 — Setup & Monday-mechanics probe**
- Verify or provision Nexiuum EC2.
- ~~SSE-from-iframe spike~~ — **resolved 2026-05-20.** Spike confirmed Monday CSP allows external EventSource (Wikimedia stream worked), but we pivoted to Monday SDK events instead. Spike artifact retained at `~/projects/clients/nexiuum/scheduling-engine/spike/` as reference.
- Confirm Blend Records status column ID and value transitions for press `actual_start`/`actual_end` mapping.

**1.5B1 — Three boards live in Gray Space**
- Capacity Engine (6 presses + Elphaba), Process Recipe (one-stage press recipes), Schedule (with `Dependent On` + `drift_last_detected_at`).
- Manual data entry test — confirm mirrors, formulas, recipe-version stamping work with no engine yet.

**1.5B2 — Pressing engine MVP (pure core + IO shell)**
- Single async worker + write queue.
- Stage explosion against Process Recipe (composite-key pinning).
- Routing + local-only placement.
- Echo guard + batched writeback.
- Source-board webhook handler: resolve Job Reference, fan out to multi-flavor slots, write actuals + Status atomically.
- 15-minute polling sweep with `drift_last_detected_at` suppression.
- Drift-triggered local reflow.
- Manual override + expedite-with-notification.
- Operations team uses native Monday board views while embedded view is built in parallel.

**1.5B3 — Embedded view (pipeline validation)**
- Custom Monday Apps Framework board view served from Nexiuum EC2.
- View subscribes to Monday SDK board events; re-fetches Schedule + Capacity Engine via SDK on each event; re-renders Marey diagram.
- Single-stage Marey rendering (Gray Space pressing). Proves the full pipeline end-to-end (engine writes → Monday SDK event → view re-fetch → render) before Phase 2 adds DAG complexity.

**1.5B4 — AM pre-quote lookup (CTP) — Gray Space scope**
- Smart Embed View on Nexiuum EC2 inside Gray Space Monday.
- Engine endpoint: `simulate(hypothetical_order) → { projected_ship_date, padded_ship_date, binding_constraint_machine }`. Pure core, no writeback.
- AM enters product mix + qty → returns earliest realistic press completion date with a pad and the identified constraint machine.

### Phase 2 — Nexiuum (next week)

**2.A — Nexiuum boards mirror Gray Space pattern**
- Capacity Engine instance in Nexiuum Monday loaded with packaging machines.
- Process Recipe instance with multi-stage packaging DAGs (blister ∥ lot-coder → clamshell, etc.).
- Schedule board.

**2.B — Packaging engine + actuals**
- Same engine, second target Monday instance.
- Source-board mapping for packaging stages (discovery with Michaela).
- Same routing/reflow/expedite mechanics, now exercising fork/merge.

**2.C — Cross-account stitching**
- **Mechanism (decided):** engine-mediated. The engine holds credentials for both Monday accounts and writes derived items across the boundary. Connect-Boards cross-account considered and rejected due to authorization fragility (either admin can revoke).
- Gray Space press completion writes flow into Nexiuum Monday via the engine. Packaging schedules see press predecessors.
- End state: Nexiuum AMs see the combined press → packaging timeline natively.

**2.D — Combined CTP**
- AM pre-quote endpoint extended to span both entities. Returns ship date accounting for press + packaging + cross-account handoff.

### Parallel — Spec Sheet Form (Adrian)

Form must produce structured packaging breakdown and a `Recipe Key` for each line item (assigned via Monday automation per Locked Decision #2). Coordinate so this lands by 2.A.

---

## Open items

1. **Nexiuum EC2** — exists or provision? Blocking 1.5B2. Flag urgency.
2. ~~SSE-from-iframe CSP/CORS~~ — **resolved 2026-05-20.** CSP allows external EventSource, but we dropped SSE in favor of Monday SDK board events.
3. **Blend Records status column ID + value transitions** — Phase 1 discovery with Jason/Zane. Fallback if delayed: hard-code expected values during dev, finalize during operator test.
4. **Packaging source-board status mapping** — Phase 2 discovery with Michaela.
5. **Spec-sheet form ship date** — gating for 2.A.
6. **Recipe authoring UX** — JSON-in-column is fine for now. Subitems-with-rollup pattern when ops needs to edit.
7. **Monitoring & alerting** — Sentry + `/health` recommended. Not blocking.

---

## Reference

- Codex adversarial review: `codex-review-v2.md`
- Resolution drafts: `resolutions-v2-to-v3.md`
- Renderer prototype: `scheduling-flow-view.html`
- Notion project: https://www.notion.so/364347ea284d81aba6afd5484479b9db
- Source meeting transcript: Otter `GMgXDAovwODdUdpfNOcgTGoJifQ`
- Earlier cross-account sync design: `~/projects/bootstrap-built/wiki/sources/transcripts/2026-03-30-room-scheduling-process-review.md`
- Monday Apps Framework: https://developer.monday.com/docs/custom-app-features
- Monday GraphQL API: https://developer.monday.com/api-reference/docs
