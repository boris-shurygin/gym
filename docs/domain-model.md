# Domain model — draft

Conceptual entities; these are **not** final database schemas.

## Definitions

- **User:** account and settings; owns personal records.
- **Exercise:** logical exercise plus measurement convention/equipment.
- **Program:** logical training program with a published current version.
- **ProgramVersion:** immutable snapshot of a published program, author and rationale.
- **WorkoutTemplate:** a user/program-defined named session type within a program version (e.g. “Leg day” or “Upper body”). In MVP a session is started by choosing one of these templates, not from an empty ad-hoc workout.
- **TemplateExercise:** an exercise occurrence/slot within a template, with target ranges and progression rule.
- **ProgressionRule:** versioned type and parameters; used by backend.
- **ProgramChange:** draft patch, base version, justification, validation and publication status.

An Exercise is not a TemplateExercise. A bench press in A and B may have distinct goals. Logical slot identifiers should remain stable across compatible revisions to retain continuity; replacements that are not comparable may require a new slot.

## Execution

- **WorkoutSession:** owner, session status, selected template reference, program version and saved plan snapshot. Template selection is user-driven; strict template rotation is not required.
- **SessionExercise:** actual slot/order and planned vs performed exercise, including skip/replacement.
- **SetResult:** set ID, working/warm-up type, weight, reps, optional RIR/RPE and timestamps.
- **SessionEvent:** audited changes and corrections.
- **BodyMeasurement:** optional non-set measurement.
- **SyncOperation:** device-issued operation with a unique ID, scope and processing outcome.

## Proposed state machine

`planned -> in_progress -> completed`. Alternatives: `cancelled`, `abandoned`. Corrections of completed sessions must be traceable and cause derived metrics to be recalculated; exact state changes are still open.

## Confirmed invariants

1. Program changes do not modify historical results.
2. Planned targets and actual results remain distinct.
3. User isolation is always enforced server-side.
4. Rule inputs, version and basis of a recommended load should be explainable.
5. Retried operations must not create duplicate sets.

## Progression: proposed initial types

- `fixed`: unchanged targets until the program is revised.
- `double_progression`: after all qualifying working sets meet the configured high rep target at the planned load, increment weight.
- `manual`: an explicit target rather than algorithmic auto-progression.

Illustrative only: 3 sets, 8–12 reps, increase +2.5 kg after all 3 reach 12. Neither the example nor its handling of failure/partial sessions is an approved universal rule.

**Principle:** progression state/analytics are derived from authoritative published rules, workout snapshots and results. Cached state must be invalidated after historical corrections.

## Open questions

- State representation and replay strategy.
- How to treat missed sets, mixed weights, deloads and weight rounding.
- Per-session overrides.
- Calendar scheduling vs generating the next workout on demand.
- Conflict semantics for simultaneous edits and post-completion corrections.
