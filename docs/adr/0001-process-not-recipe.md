---
status: accepted
---

# Engine uses "Process," not "Recipe"

The Monday board the engine reads from is named "Process Recipe," and the engine's first code generation called the entity `Recipe` to match. That conflicts with the ops-floor meaning of "recipe" — operators use "recipe" for blending ratios (active-ingredient milligrams per tablet, etc.), which is a different thing the engine does not model. To stop that overload, the engine renames its entity to **Process** (full term: **Production Process**) — the ordered, versioned sequence of Stages an Order moves through. The Monday board name stays "Process Recipe" externally (operators know it by that name and renaming the board is a separate, higher-coordination decision).

## Consequences

- Engine-side rename touches `Recipe` → `Process`, `RecipeStage` → `Stage`, `recipe_key` → `process_key`, `recipe_version` → `process_version`, `RecipeStatus` → `ProcessStatus`, and ~265 test references. Single mechanical pass plus tests; done as one PR.
- The Monday board keeps its current name. Anyone bridging engine ↔ board reads must accept that asymmetry. Documented in CONTEXT.md.
- The word "recipe" remains valid in conversation when it means **blending ratios** (the ops-floor sense). Engine-side code and docs must not use it.
- Future ADRs that talk about "the Recipe board" are referring to the Monday board's external name only, not an engine entity.
