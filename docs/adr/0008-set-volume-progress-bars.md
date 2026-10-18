# ADR-0008: Set-volume progress bars and planned defaults

- **Status:** Accepted
- **Date:** 2026-10-18
- **Supersedes:** [ADR-0007](0007-planned-set-defaults-and-progress-bars.md)

## Context

ADR-0007 made the progress bar a comparison of actual vs planned **repetitions**. However, an individual set may use a higher weight with fewer repetitions. The user explicitly wants the bar to compare **set volume = weight × repetitions**, reflecting both inputs. The decision to prefill every new set from its planned target and to show a separate performed-set count remains valid.

## Decision

1. Every new working set defaults to the **planned** weight and suggested repetitions for that set, **not** the last actual values. Extra sets default to the last planned working set's values but remain explicitly labeled extra.
2. For a working set with a positive plan volume, calculate:
   - `planned_volume_kg = planned_weight_kg × planned_reps`
   - `actual_volume_kg = actual_weight_kg × actual_reps`
   - `completion_ratio = actual_volume_kg / planned_volume_kg`
3. A horizontal progress bar has a **visible 100% plan marker**; lower volume ends before the marker, and higher volume extends beyond it into an overflow area. Display actual/planned numbers and percentage so a capped overflow is not misleading.
4. Retain actual and planned weights and repetitions as readable numbers alongside the bar. Equal volumes do not imply equal strength, effort or training effect.
5. A separate indicator tracks **completed working-set count / planned working-set count**, including extra sets. Do not treat warm-up or unlogged planned sets as completed working sets.
6. If planned volume is zero/undefined (e.g. certain bodyweight, assisted or non-load modalities), do not divide by zero or fake a percentage. The supported measurement policy for such exercises is a separate design task.
7. Neither this bar nor its volume ratio is an automatic progression-success criterion. Progression rules run on raw recorded results and explicit conditions.

## Examples

| Planned | Actual | Planned volume | Actual volume | Ratio |
| --- | --- | ---: | ---: | ---: |
| 60 kg × 10 | 60 kg × 8 | 600 kg | 480 kg | 80% |
| 60 kg × 10 | 60 kg × 10 | 600 kg | 600 kg | 100% |
| 60 kg × 10 | 65 kg × 9 | 600 kg | 585 kg | 97.5% |
| 60 kg × 10 | 70 kg × 9 | 600 kg | 630 kg | 105% |

## Consequences

- Set plans/snapshots provide weight plus concrete suggested repetitions even when a rule also allows a repetition range.
- The client can compute the comparison **locally/offline**, without duplicating progression logic, by multiplying the snapshot and logged set values.
- Volume means *recorded tonnage* (kg × reps), not mechanical work. It should not be used to compare unrelated machines or declare strength improvement alone.
- Exact bar overflow scale, optional whole-exercise total-volume summary and measurement strategies for special exercise types remain open.
