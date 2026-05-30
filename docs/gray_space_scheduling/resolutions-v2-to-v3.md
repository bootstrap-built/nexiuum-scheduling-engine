# Resolutions to Codex's Top 5 — draft for review

**Purpose:** Resolve the five blocking concerns from `codex-review-v2.md` so we can promote v2 → v3 and start engine code with no underspecified core mechanics.

Each resolution has: the problem in one line, the proposal, rationale, implementation note, and any trade-off worth flagging.

---

## Resolution 1: IO shell concurrency model

**Problem:** Plan describes a pure core but says nothing about how concurrent webhook events + polling sweeps are serialized. Race conditions on simultaneous writes corrupt the schedule.

**Proposal:** Single async worker with a serialized write queue. Read-only paths bypass.

- **Write path** (any code that mutates Monday): webhook handlers and the polling sweep both push events to a single in-process `asyncio.Queue`. A single worker coroutine drains the queue serially. One reflow runs at a time.
- **Read path** (CTP `/simulate` endpoint): runs concurrently on the HTTP server, does not enqueue, never writes. Reads Monday state fresh, runs the pure core, returns the projected date. No lock contention with the writer.
- **Snapshot freshness:** the worker reads the current Monday board state at the start of each event processing (not from a cached in-memory snapshot). Prevents stale-read races.

**Rationale:** At Phase 1 scale (6 presses, single-digit reflows/hour, <100 active jobs) throughput is irrelevant. Correctness via serialization is cheap. Async + single worker means we get clean Python code with no thread locks, and we can scale to a worker pool later if we ever need to.

**Implementation note:**
```python
# Webhook handler (FastAPI/Flask route)
async def on_webhook(event):
    await event_queue.put(event)
    return {"accepted": True}

# Single worker
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

**Trade-off:** If reflows are slow (e.g., Monday API latency spikes), the queue can back up. Acceptable for Phase 1; revisit if queue depth exceeds 10.

---

## Resolution 2: SSE data contract — actual_t0 / actual_t1 + snapshot semantics

**Problem:** Prototype contract has planned offsets (`t0`/`t1`) but can't express a confirmed-running vs. late-but-not-started job. Renderer can't show drift state.

**Proposal:** Add three optional fields to each `nodes[]` entry. Commit to full snapshots over SSE for Phase 1.

**Updated node shape:**
```js
nodes: [{
  id, lane, t0, t1, kind: 'run',         // planned (unchanged)
  actual_t0?: number | null,             // engine writes when source webhook posts actual_start
  actual_t1?: number | null,             // engine writes when source webhook posts actual_end
  drift_state?: 'on_time' | 'late_start' | 'late_end' | 'completed' | null,
}]
```

**Renderer rules:**
- `drift_state = null` (planned, not yet relevant): render bar at `[t0, t1]` normal opacity
- `drift_state = 'on_time'`: `actual_t0` set, `actual_t1` empty, `now` between them — render bar at `[actual_t0, t1]` with the pulsing "running" indicator (already in prototype)
- `drift_state = 'late_start'`: `now > t0`, `actual_t0` empty — render bar at `[t0, t1]` with a red left-edge marker / "LATE" badge
- `drift_state = 'late_end'`: `actual_t0` set, `now > t1`, `actual_t1` empty — render bar extending past planned `t1` with hatched overflow region
- `drift_state = 'completed'`: both actuals set — render bar at `[actual_t0, actual_t1]` at reduced opacity (matches existing `past` styling)

`drift_state` is engine-computed convenience so the renderer doesn't have to re-derive logic.

**SSE semantics:** **Full snapshot per event, not deltas.** Each SSE message carries the complete current schedule for the connected client's filter (e.g., all machines, or a single Production Order). At Phase 1 scale (~20 active jobs) snapshots are <10KB; delta protocol isn't worth the implementation cost. Revisit if Phase 2 schedule size exceeds ~500 jobs.

**Implementation note:** Engine emits SSE on every successful `apply_plan()` write. SSE client (the embedded view) replaces its in-memory state with each snapshot. Simple, correct, no patch protocol.

**Trade-off:** Full snapshots over SSE are mildly wasteful but eliminate an entire class of patch-protocol bugs. Worth the bytes.

---

## Resolution 3: Job state machine — explicit transitions

**Problem:** Plan lists `Queued / Running / Done / Blocked` but doesn't define what writes each, who owns it, or whether `Blocked` is used.

**Decision (locked 2026-05-20):** **Engine-via-webhook.** The user's status flip on the source board (Blend Records: "Blending" → "Pressing") is the source-of-truth event. Monday fires a webhook to the engine; the engine resolves the Connect-Boards link and writes `actual_start` + `Status=Running` to all linked Schedule items in one mutation.

**Why not Monday-automation only:** Monday automations don't have a clean primitive for reverse-lookup-and-write (Blend Records status changes → find Schedule items where Job Reference points HERE → write timestamp on them). It's especially fragile for multi-flavor fan-out (one Blend Record → 5 Schedule items). The engine's already processing the webhook for drift detection and possible reflow — adding the actual_start write to that same call is the simplest path. The user still owns the timing decision (when they flip the status); the engine just bridges to the Schedule board.

**Proposal:** Engine writes Status transitions atomically with actuals. Blocked is a reserved escape hatch, never written automatically.

**State transitions:**

| From | To | Trigger | Writer |
|---|---|---|---|
| (init) | `Queued` | Schedule item created by engine | Engine |
| `Queued` | `Running` | Source-board webhook posts `actual_start` to Schedule item | Engine (atomic with `actual_start` write) |
| `Running` | `Done` | Source-board webhook posts `actual_end` to Schedule item | Engine (atomic with `actual_end` write) |
| `Queued` / `Running` | `Blocked` | Manual operator action on the Schedule item OR engine detects dangling `Job Reference` | Operator OR Engine (auto on dangling reference) |
| `Blocked` | `Queued` | Manual operator action | Operator |

**Engine behavior on each status:**
- `Queued`: visible to reflow; eligible for placement decisions
- `Running`: visible to reflow but **immutable** — engine cannot move or shift a Running slot, only respect its `actual_start` and continue reflow around it
- `Done`: visible only for `Last job ended at` calculation on the machine, otherwise ignored
- `Blocked`: invisible to reflow; engine treats as if the slot doesn't exist. Polling sweep ignores Blocked items.

**Atomic-with-actuals reasoning:** The source-board webhook arrives, engine reads it, engine writes `actual_start` AND `Status=Running` to the Schedule item in the same GraphQL mutation. No race window where actual is set but Status hasn't caught up.

**Trade-off — alternative considered:** Could have used a Monday automation rule ("when `actual_start` non-empty → Status=Running") for declarative simplicity. Rejected because the engine is already processing the webhook and needs to do other work (drift detection, possible reflow); doing Status in the same call is simpler than relying on Monday's automation timing. **If you'd prefer the Monday-automation approach** (matching Recipe Key assignment pattern), say so — it's a one-day swap.

---

## Resolution 4: Polling sweep idempotency guard

**Problem:** A persistently-late or never-started job retriggers reflow every 15 minutes indefinitely. `last_reflow_hash` only protects the webhook path.

**Proposal:** New `drift_last_detected_at` Date+hour column on Schedule items. Polling sweep refuses to retrigger if a drift event was already handled recently.

**Add column to Schedule board:**

| Column | Type | Purpose |
|---|---|---|
| `drift_last_detected_at` | Date+hour | Engine-written timestamp of last drift-triggered reflow on this item |

**Polling sweep logic:**

```python
async def polling_sweep():
    now = utcnow()
    suppression_window = timedelta(minutes=60)  # configurable

    # Late starts
    late_starts = await query_schedule_items(
        status="Queued",
        planned_start__lt=now - timedelta(minutes=15),
        actual_start__isnull=True,
    )
    for item in late_starts:
        if item.drift_last_detected_at and (now - item.drift_last_detected_at) < suppression_window:
            continue  # already handled recently
        await event_queue.put(DriftEvent(item, kind="late_start"))
        # event handler writes drift_last_detected_at as part of its reflow

    # Late ends — same shape
    ...
```

**Suppression window: 60 minutes** for v1. Polling runs every 15 min, but a single drift event triggers at most one reflow per hour. If the job genuinely never starts, operations sees a single notification per hour rather than four. Configurable via env var if Jason wants finer/coarser cadence.

**The webhook path is unchanged.** A real status flip (job actually starts, fires webhook) clears the late condition naturally — `actual_start` becomes non-null, the polling query no longer matches it. No need to clear `drift_last_detected_at` explicitly; it just stops mattering.

**Trade-off:** Suppression window means a job that's late, gets reflowed, and is *still* late 30 minutes later won't re-reflow until the 60-min window expires. Acceptable for the "operator forgot to start it" case; possibly slow for "machine broke and we need to immediately reroute." Mitigated because that scenario triggers a *different* event (operator marks machine Status=Down on Capacity Engine), which has its own reflow path with no suppression.

---

## Resolution 5: Predecessor vs. Dependent On — drop Predecessor, codify version pinning

**Problem:** Plan's Schedule board schema lists `Predecessor` (Connect-Boards) but the Monday testing session showed that Gantt apps require the native `Dependency` column type. Two columns for the same concept = data drift inevitable.

**Proposal:** Drop `Predecessor` from the schema entirely. Use Monday's native `Dependent On` (`dependency` column type). Codify recipe version pinning behavior as an engine invariant.

**Schedule board schema change (Board 3 in the plan):**

| Column | Type | Purpose | Change |
|---|---|---|---|
| ~~Predecessor~~ | ~~Connect-Boards~~ | ~~Per-job dependency arrows~~ | **REMOVED** |
| **Dependent On** | dependency | Per-job dependency arrows; Gantt apps render arrows | **NEW (replaces Predecessor)** |

`Dependent On` uses Monday's native `dependency` column type with `dependency_mode: "flexible"`. Renders as dependency arrows in Ganttly, native Gantt views, and our custom Marey view via the same data shape.

**Engine writes to `Dependent On`** when creating Schedule items per the Process Recipe's `depends_on` array.

**Recipe version pinning — engine invariant:**

> When the engine reflows a Schedule item, it MUST read the recipe's stage graph using BOTH `recipe_key` AND `recipe_version` stamped on the Schedule item. It MUST NOT re-query Process Recipe by `recipe_key` alone (which would pull the newest version). This guarantees in-flight jobs survive recipe edits with their original DAG intact.

**Implementation:**
```python
def load_recipe_for_slot(slot):
    return process_recipe_board.find(
        recipe_key=slot.recipe_key,
        recipe_version=slot.recipe_version,  # explicit pin
    )
```

If the engine cannot find the exact `recipe_key` + `recipe_version` combination (e.g., Recipe item was hard-deleted), it raises a `DanglingRecipeError` and marks the affected Schedule items `Status=Blocked` with a note. Operations must intervene. Engine does not silently fall back to a newer version.

**Composite-key uniqueness:** since Monday has no native composite-key constraint, add a startup-time integrity check. On engine boot, scan Process Recipe board and warn if duplicate (`recipe_key`, `recipe_version`) pairs exist. Don't crash; surface to logs/monitoring for human resolution.

**Trade-off:** A Recipe item deletion now puts dependent jobs into `Blocked`. This is intentional — silently using a different recipe would be much worse. Operations needs to know when this happens (the monitoring item, separate concern).

---

## Summary of board changes from v2 → v3

**Schedule board:**
- Drop `Predecessor` (Connect-Boards)
- Use `Dependent On` (dependency column, native type)
- Add `drift_last_detected_at` (Date+hour)

**Data contract (SSE):**
- Add `actual_t0`, `actual_t1`, `drift_state` to `nodes[]`
- Commit to full-snapshot semantics (not deltas)

**Engine behaviors codified:**
- Single async worker for all writes; CTP reads bypass
- Status transitions atomic with actuals writes
- Polling sweep respects 60-min suppression window via `drift_last_detected_at`
- Recipe lookup always pins on `recipe_version`; dangling recipes → `Blocked`
- Startup-time integrity check for recipe composite-key duplicates

---

## Status

All five resolutions accepted by Josh 2026-05-20. Integrating into v3 of the build plan.

Engine-via-webhook confirmed for Resolution 3 with the framing: the user originates the event (status flip on source board); the engine is the bridge that translates that event into Schedule-item writes. Monday-automation alternative ruled out due to the reverse-lookup limitation with multi-flavor fan-out.
