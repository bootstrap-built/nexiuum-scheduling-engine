---
status: accepted
---

# Baton-pass is push-only

When an upstream Stage finishes (`ActualEndReported`), the engine pushes dependent Stages' `planned_start` forward if they were planned earlier than `event.actual_at + cross_stage_handoff_buffer_minutes`. It never pulls a dependent earlier — even when the upstream finishes ahead of schedule. The trade-off is schedule density for predictability: operators don't see a Slot's `planned_start` jump earlier without warning, and the engine avoids cascading re-plans (pulling X earlier means everything depending on X might also pull earlier, etc.). Pulling earlier is also rarely actionable at present — the downstream Machine probably can't actually start sooner than what the operator is already watching, so the visual change is noise.

## Consequences

- Schedules drift later, never earlier, over the course of a day. If a Stage finishes early, the gained time is left as visible slack on the chart rather than absorbed by an automatic compaction.
- A deliberate **compact** operator action (pull-everything-in-once, on demand) is the expected escape hatch if/when ops needs to reclaim that slack. Tracked in `.scratch/pending-issues.md` as a future-feature candidate; not in the current scope.
- This ADR may be revisited (or superseded) once we have signal from real operator usage about how often the lost density actually matters.
