# Codex Adversarial Review — Gray Space Scheduling Plan v2

**Reviewer:** Codex (OpenAI) via `codex:codex-rescue`
**Date:** 2026-05-20
**Reviewed:** `gray-space-scheduling-plan.md` + `scheduling-flow-view.html` in this folder
**Posture:** Adversarial — surface what's wrong, missing, or underspecified. Not a rewrite.

---

## 1. Engine Correctness Invariants

- **No conflict guarantee is asserted nowhere in the plan.** The placement logic ("slots into open space") is described narratively but there is no invariant statement about how the engine detects overlap on a single machine. If two webhooks trigger near-simultaneous placement, the in-memory snapshot used by each run may not reflect the other's writes yet — overlap is possible before Monday confirms and the echo guard fires.
- **`last_reflow_hash` does not protect the polling sweep.** The echo guard is described as operating on webhook receipt. The 15-minute polling sweep is a separate code path that queries Monday directly. If the sweep triggers a reflow and that reflow writes back to Monday, the resulting `Schedule item modified` webhook will arrive after the fact. The echo guard only protects the webhook path. The polling sweep has no guard against re-triggering its own writes on the next sweep cycle.
- **Recipe version pinning on reflow: not specified.** Plan says version is "stamped at instantiation." On reflow, does the engine re-read the `Recipe Key` + `Recipe Version` columns from the Schedule item (safe), or does it re-query the Process Recipe board by key (potentially pulling the newest version)? If the engine re-queries Process Recipe by `recipe_key` without pinning `recipe_version`, a recipe edit mid-flight silently changes the stage graph for in-progress jobs during reflow.
- **`planned_start` / `actual_start` coherence under reflow.** If `actual_start` is already populated (job is running) and the engine reflows that machine, what does it do with the running job's `planned_start`? Overwriting it would destroy the delta-from-plan signal.

## 2. Concurrency / Race Conditions

- **No mutex or queue is specified.** Plan describes the engine as a pure function but doesn't describe how the IO shell serializes concurrent webhook events. Two simultaneous webhooks (e.g., capacity change + new order) operating on the same in-memory snapshot will produce conflicting writes with no defined winner.
- **Polling sweep vs. in-flight webhook reflow.** If the polling sweep fires while a webhook-triggered reflow is writing back to Monday, the sweep's Monday read may return a partially-written state.
- **Partial batch write.** Plan says "batch writes per reflow" but Monday GraphQL mutations don't guarantee atomicity. If the batch is partially applied (e.g., Monday rate-limits mid-batch), the Schedule board is left in an inconsistent intermediate state.
- **Double expedite.** If two operators set Priority → Expedite on different jobs targeting the same machine within the same webhook delivery window, both webhook handlers run against the same snapshot. The "bump" logic runs twice, potentially placing both expedited jobs in the same slot.
- **Running → Done during mid-sweep.** If a job's source-board status flips Running → Done while the polling sweep is mid-query, the sweep may have already read that item as `Status = Running` with `planned_end < now`. The sweep then triggers a late-end drift event and reflow. Simultaneously, the Done webhook fires the correct `actual_end` write. The reflow was unnecessary and runs against stale state.

## 3. Recipe Versioning Trap Doors

- **Version-N job with no slots yet.** If a job is instantiated but slots haven't been created in the Schedule board, and someone edits the recipe (creating version N+1) before the engine completes slot creation, the plan's recovery path is undefined.
- **Active → Retired mid-instantiation.** Plan says "old versions retained" but doesn't say what the engine does when it reads a Process Recipe item with `Status = Retired` during slot creation.
- **Hard delete of a recipe version.** Monday doesn't prevent item deletion. If a Recipe item is deleted, the `Recipe Key` + `Recipe Version` stamped on existing Schedule items becomes a dangling reference.
- **Composite key uniqueness.** Monday has no native composite-key constraint. Nothing prevents two Process Recipe items with identical `recipe_key` + `version` values.

## 4. Actuals Join via Job Reference Connect-Boards

- **Blend Records item deleted.** The `Job Reference` Connect-Boards link becomes a null/empty column on the Schedule item. Subsequent source-board webhooks have no item to resolve to.
- **Link changed manually.** Monday allows operators to manually edit Connect-Boards columns. If someone changes the `Job Reference` link, all future actuals go to the wrong source item.
- **Multi-flavor fan-out.** A single Blend Records item corresponding to a multi-flavor PO spawns multiple Schedule items. When a status flip fires on the Blend Records item, the engine must resolve all Schedule items that link to it and write actuals to each. The plan's "resolve by following that link" phrasing implies one-to-one. Multi-flavor jobs need explicit handling.

## 5. Polling Sweep + Drift Threshold Interaction

- **14-min → 16-min window is a real gap.** Effective detection latency is up to 30 minutes for borderline cases. May be acceptable, but should be a deliberate choice.
- **Persistently-late job triggers reflow every sweep.** If a job is late and reflow produces a new schedule but the job never actually starts (machine broken, operator absent), every 15-minute sweep will re-detect and trigger another reflow. There is no "already reflowed for this miss" guard.
- **Drift threshold is hardcoded.** No operator-visible or config-file control. When operations asks for a 30-minute threshold for a specific machine class, this requires a code change.

## 6. Cross-Account Stitching (Phase 2)

Four mechanism options:
- **Monday native cross-workspace mirror columns** — read-only, no write-back, authorization fragile
- **Monday Connect-Boards across accounts** — requires both accounts to authorize; can be revoked unilaterally; the plan's Phase 1 architecture naturally extends to this
- **Engine-mediated** — engine holds credentials for both accounts; most robust technically; the architecture most naturally points here
- **Webhook bridge** — adds dependency, no delivery/ordering guarantees

Plan's Phase 1 architecture most naturally points toward **engine-mediated** for Phase 2. Leaving it open means Phase 2 discovery starts without an architecture decision.

## 7. HTML Prototype Data Contract

- **Manual-override flag missing.** Contract doesn't include a field indicating whether the machine assignment was engine-chosen vs. manually overridden.
- **Partially-completed job (actual_start exists, actual_end doesn't).** The contract has `t0`/`t1` (planned offsets) but no `actual_t0`/`actual_t1` fields. The renderer can't distinguish "planned t0 has passed and the job is confirmed running" vs. "planned t0 has passed and the job hasn't actually started (late)."
- **Delta vs. full snapshot over SSE.** Plan doesn't commit. At Phase 1 scale full snapshots are fine; Phase 2 needs revisit.
- **`meta.pin` is a display hint.** The engine's routing rules aren't encoded in the renderer — pin is a label, not a re-derivable field.
- **`epoch` is a string label, not parseable.** In production, the engine must send a parseable epoch (ISO) or pre-formatted strings. Current contract doesn't enforce this.

## 8. Scheduling Literature Anti-Patterns

- **Local-only reflow + FIFO creates avoidable machine starvation.** High-speed machines sit idle while slower ones backlog. Plan acknowledges "AMs drag jobs manually" as recovery, but schedule quality degrades silently until human intervention.
- **Drum-buffer-rope mismatch.** Plan treats all machines as peers for routing; Gray Space's actual constraint is the drum (likely Merlin or Gandalf for high-volume runs). Round-robin distributes load across machines, starving the fastest machines for large orders.
- **CTP without constraint identification.** `simulate()` returns "earliest realistic press completion date" without identifying which machine is the binding constraint. Plan doesn't describe how CTP handles a fully-loaded drum.

## 9. Underspecified Bits

- **Job state machine transitions are not defined.** Plan lists `Status` values but doesn't specify what writes `Running`, what writes `Done`, whether `Blocked` is ever used and by what.
- **`Manually placed` + capacity change interaction.** `Duration (hrs)` is a formula; `Capacity` is a mirror. If capacity changes, formula recalculates in Monday's UI, changing `Duration`. The `planned_end` column is engine-written and won't auto-update. Staleness not addressed.
- **`Predecessor` vs. `Dependent On`.** The Schedule board schema lists `Predecessor | Connect-Boards`. The Monday testing session added `Dependent On`. These appear to be the same concept but reconciliation is missing.
- **`Last job ended at` on Capacity Engine.** No guard against two-jobs-running-concurrently-on-same-machine.

## 10. Other Production Concerns

- **EC2 provisioning blocking 1.5B2** — not flagged with urgency
- **SSE in Monday Apps Framework iframe** — Monday's CSP/CORS may block SSE to nexiuum-ec2. Common production surprise.
- **Monday GraphQL complexity budget at batch write time** — no analysis of expected mutation complexity per reflow
- **Press Room Tracking discovery is a dependency for 1.5B0** — no fallback if discovery is delayed
- **No monitoring or alerting** — no mechanism for detecting engine failure, missed webhooks, stuck polling sweep

---

## Top 5 Priorities Before Writing Engine Code

1. **Define the IO shell's concurrency model.** The plan describes a pure core but says nothing about how the IO shell serializes concurrent webhook events and polling sweeps. Without a queue/mutex decision, concurrent reflows will produce race conditions and partial writes that corrupt the schedule.

2. **Add `actual_t0`/`actual_t1` fields to the SSE data contract and define full-snapshot vs. delta semantics.** The current contract can't express a late/not-yet-started job vs. a confirmed-running job. This distinction drives drift detection in the renderer and determines whether the SSE payload needs a patch protocol or can send full snapshots.

3. **Specify the job state machine: what writes each status, who owns it, and whether `Blocked` is used.** The plan lists status values without defining transitions or owners. Without this, the webhook trigger table and polling sweep both have undefined behavior on edge cases.

4. **Add a polling-sweep idempotency guard equivalent to `last_reflow_hash`.** A persistently-late or never-started job will trigger reflow on every 15-minute sweep indefinitely. This is the most likely source of runaway reflow storms in early production, and it's entirely unaddressed.

5. **Resolve `Predecessor` vs. `Dependent On` column identity and confirm the recipe version pinning behavior during reflow.** Both are underspecified and both affect the core scheduling logic. Writing the engine before these are resolved means the data model may need to change after the engine is partially built.
