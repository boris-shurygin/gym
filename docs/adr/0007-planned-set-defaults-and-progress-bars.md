# ADR-0007: Planned set defaults and progress bars

- **Status:** Superseded by [ADR-0008](0008-set-volume-progress-bars.md)
- **Note:** ADR-0008 preserves plan-based input defaults and the separate set-count indicator, but replaces repetition-only bars with kg × reps volume bars. This record remains for historical context.
- **Date:** 2026-10-18

## Context

In the dedicated exercise logger users record multiple sets and frequently perform fewer or more repetitions than prescribed. Inheriting the previous **actual** result can make deviations propagate into the next entry. The user instead wants the program's expectations to stay visible and requests visual progress strips that make under- and over-fulfilment obvious.

## Decision

- Every new working-set entry starts with its **planned weight and suggested repetition count**, independent of the preceding actual set. Targets may vary by planned set.
- The editable values are actual inputs, never edits to the published program or frozen session plan.
- For working sets beyond the prescribed count, use the last planned working-set target as an explicitly marked comparison/prefill baseline.
- Show a **per-set progress bar** for actual repetitions relative to the prescribed/suggested reps, with a visible 100% marker, under-target shortfall and **over-target overflow beyond 100%**, accompanied by actual/planned numeric labels and delta.
- Show a **separate working-set count** relative to prescribed sets, also distinguishing incomplete and extra sets. Warm-ups do not count as completed working sets.
- If performed weight differs from planned weight, label the weight deviation separately; do **not** interpret a higher repetition ratio at lower weight as overall overperformance.
- The backend must provide explicit suggested repetitions for each planned set even if the rule has a rep range; the mobile client must not guess a concrete suggestion from a range.

## Consequences

- The logger does not inherit a previous actual result as its next default, reducing accidental propagation of a missed target.
- Both per-set repetition deviation and overall working-set completion are visible without conflating the two measurements.
- Planning/snapshot data need per-set or deterministically resolvable planned targets. Extra sets may use a reference target while remaining factual extra sets.
- Actual performance and progression decisions remain separate: the backend uses original data and rule criteria, not visual percentages.
- Color is not the only indicator: numeric targets/deltas and labels support accessibility.

## Deferred UI details

Bar shape/scale and capped overflow behavior, exact numeric input affordances, and any aggregate volume/repetition indicator need UX testing. The decision to use planned defaults and visible under-/over-target bars is accepted.
