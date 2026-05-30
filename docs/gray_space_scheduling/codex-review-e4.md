# Codex Adversarial Review — Engine E4 (IO Shell)

**Reviewer:** Codex (OpenAI) via `codex exec -c 'model="gpt-5.5"'`
**Date:** 2026-05-21
**Reviewed commit:** `E4 complete: worker, echo guard, /commit, /webhook/monday`
**Layer:** IO shell — async worker, echo guard, /commit, /webhook/monday, Plan-to-Monday writeback
**Posture:** Adversarial — find correctness bugs, race conditions, design holes, security issues.

---

## Findings

### Blocker — Echo guard suppresses real operator changes
`engine/routes/webhook.py:67`, `engine/routes/webhook.py:120`

Any operator edit on a Schedule item whose `last_reflow_hash` is still in the 256-entry guard will be treated as engine-originated, because operator edits do not clear/change `last_reflow_hash`. That means E5 Priority changes, drag-to-reschedule, Status changes can silently disappear for recently engine-written items.

**Do:** suppress only specific engine-write echoes — ideally `item + hash + column + short TTL` — or use Monday event metadata if it reliably identifies the actor/app. Do not use "current item has recent hash" as a general operator-change discriminator.

### Blocker — No webhook signature verification
`engine/routes/webhook.py:40`

No verification that webhook requests came from Monday. Monday documents outbound request JWT verification using the `Authorization` header, with board webhooks signed by the app Signing Secret. This endpoint is currently a public write-trigger surface.

**Do:** require and verify Monday JWT / signing secret before accepting challenge/events.
Source: https://developer.monday.com/apps/docs/integration-authorization

### Serious — Worker cancellation leaves Futures unresolved
`engine/io/worker.py:120`, `engine/io/worker.py:155`

`CancelledError` is not caught by `except Exception`; `finally` calls `task_done()`, but `submission.future` is never completed. A `/commit` request can hang during shutdown/reload while the worker is cancelled inside `process_event()` or `apply_plan()`.

**Do:** catch cancellation around the active submission, set exception/cancel the future, then re-raise. On shutdown, also drain/cancel queued submissions.

### Serious — Module-level singletons are test- and event-loop-hostile
`engine/io/worker.py:55`, `engine/io/worker.py:59`, `engine/config.py:99`

`_queue` survives app lifespans and tests; queued `_Submission.future`s are tied to the loop that created them. `get_settings()` can cache the placeholder token from `tests/conftest.py:18` and poison later integration tests.

**Do:** bind worker state to FastAPI app lifespan/state, expose reset helpers for tests, and clear `get_settings` between tests that mutate env.

### Serious — `submit_event()` has no timeout or worker-liveness guard
`engine/io/worker.py:139`

Called without a live worker, it awaits forever. `/commit` can also block behind arbitrary queued events with no HTTP timeout.

**Do:** fail fast if the worker is not alive; add per-request timeout and return 503/504 with an idempotency story.

### Serious — Webhook handler awaits processing instead of enqueue-only
`engine/routes/webhook.py:64`, `engine/io/worker.py:80`

Comment claims "returns 200 fast" but it awaits `submit_event`. Worse, even stubbed `CapacityChanged` does a full `read_snapshot()` before returning no-op. Slow Monday reads can make Monday retry, creating duplicate events.

**Do:** enqueue-only for webhooks, return immediately, and let worker process asynchronously. If caller needs completion, that is `/commit`, not webhooks.

### Serious — Monday aliased mutations are NOT atomic
`engine/io/apply.py:262`

Monday's GraphQL docs say multiple operations in one request execute "one after the other," not transactionally. A partial application can leave some items stamped with `last_reflow_hash` and others not.
Source: https://developer.monday.com/api-reference/docs/introduction-to-graphql

**Do:** treat batches as non-atomic. Add reconciliation/idempotency after failed writes, verify all aliases returned, and do not mark the reflow clean unless every expected write is confirmed.

### Serious — `process_event()` doesn't check `apply_plan` success
`engine/io/apply.py:278`, `engine/io/worker.py:87`

`apply_plan()` converts GraphQL failures into `ApplyResult(errors=...)`, but `process_event()` does not check `success`; it remembers the failed `reflow_hash` and returns the result. `/commit` then reports 500 only because no created IDs exist, while echo guard state is polluted.

**Do:** raise on apply failure or check `result.success` before remembering hash/returning success.

### Serious — `Plan.machine_writes` is silently ignored
`engine/io/apply.py:273`, `engine/models.py:312`

The Plan type implies support that the IO shell drops. That is a trap for E5 Status/actuals and "Last job ended at" updates.

**Do:** either implement machine writes now or fail loudly when `machine_writes` is non-empty.

### Serious — `job_reference_id` not validated as numeric
`engine/routes/commit.py:32`, `engine/io/apply.py:85`

Any non-empty string is accepted at the HTTP boundary, then `apply.py` does `int(write.job_reference_id)`. Bad input becomes a worker exception/500. `recipe_key` also has no length/charset bound.

**Do:** validate Monday item IDs as numeric strings at the HTTP boundary; constrain `recipe_key`.

### Serious — `/health` doesn't reflect actual liveness
`engine/main.py:46`

Says ok even if the worker died or the queue is wedged. There is no liveness signal, queue depth, last success/failure, or Monday latency probe.

**Do:** expose worker running/done exception, queue depth, last processed event, and last Monday read/write status before deploy.

### Minor — Whitespace hashes accepted by echo guard
`engine/io/echo_guard.py:43`

Low probability, but sloppy for an idempotency primitive.

**Do:** normalize with `.strip()` and reject empty post-strip.

### Minor — Blend Records handler doesn't filter by columnId
`engine/routes/webhook.py:81`

Acks all Blend Records changes equally. E5 needs `col_blend_status`; right now the dispatch path is too broad and tests lock in broad acknowledgement.

**Do:** add column filtering before implementing actuals.

---

## Highest-Priority Fixes Before E5

1. **Replace the echo guard design** so real Schedule operator edits cannot be suppressed by stale `last_reflow_hash`.
2. **Add Monday webhook JWT / signing-secret verification.**
3. **Fix worker cancellation/shutdown** and add `submit_event` liveness/timeout behavior.
4. **Make webhooks enqueue-only** and return immediately.
5. **Treat Monday batched mutations as non-atomic:** verify every alias, reconcile failures, and only remember hashes after confirmed success.
6. **Move worker queue/settings/echo singleton state** into app/test-managed lifecycle or add explicit reset hooks.
