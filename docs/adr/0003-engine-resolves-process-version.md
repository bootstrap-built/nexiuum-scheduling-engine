---
status: accepted
---

# Engine resolves the Active Process version

Upstreams (Phase 2D spec-sheet form / Phase 1 Monday automations) supply only the `process_key` for a new Order. The engine resolves to the currently-Active version of that key at scheduling time and pins `(process_key, process_version)` on the Order. The existing composite-key resolver still drives in-flight Slot lookups — a Slot pinned to v1 keeps running on v1 even after v1 is Retired and v2 is Active. This lets ops control version rollover entirely from the Process Recipe board (publish v2 as Active, retire v1) without coordinating upstream code changes, and eliminates the hardcoded `recipe_version=1` in `engine/core/spec_sheet.py:307` that today would break the moment a v2 row appears.

## Considered alternatives

- **Upstream supplies `(key, version)` explicitly.** Rejected — every upstream becomes a version-management surface (form, Monday automations, future integrations). Version rollovers require coordinated board edits plus upstream redeploys.
- **No versioning at all** — strip `recipe_version`, edit each Process row in place. Rejected — removes in-flight protection. A mid-day Process edit would silently change a running press job's stage DAG, which is exactly what pinning was designed to prevent.

## Consequences

- Invariant: at most one Active version per `process_key` at a time. Engine validates this on snapshot read and surfaces a clear error if violated, rather than silently picking one.
- `engine/core/spec_sheet.py` drops the hardcoded `recipe_version=1` and instead calls the new `select_active_version_for_key(snapshot, process_key)` resolver.
- The composite-key resolver in `_resolve_recipe` stays unchanged — in-flight Slot resolution and the Draft/Active/Retired status gate on new placement both continue to work as built.
- Phase 1 Gray Space Monday automations that supply `recipe_version` continue to work (the engine will use the supplied version if present); the Phase 2D path stops supplying it. Eventually all upstreams converge on supplying `process_key` only.
