# Open questions

This list deliberately separates **unresolved** design questions from accepted ADRs.

## Mobile / synchronization

- How does a user start a brand-new workout entirely offline?
- What is the minimal local history cache?
- How are concurrent changes to the same set from two devices reconciled?
- How are unsynced local changes handled on logout, account switch, browser data deletion and app upgrade?
- Default interaction: exercise list + focused logger is a proposal, not a final UX decision.

## Data / training logic

- Create workouts on demand or pre-schedule calendar sessions?
- How can a one-time agent override modify targets without changing the standing progression rule?
- Exact qualifying criteria for progressions: mixed weights, incomplete workouts, deloads, unavailable plates.
- How should historical corrections be audited and replayed?
- How should equivalent exercises and equipment changes be identified?
- Which workout stats are useful and valid for comparison?

## Access / integration

- Which authentication provider and sign-in methods?
- Which ChatGPT/MCP integration mode supports delegated writes in the intended environment?
- What scopes and explicit approval policy are required for agent changes?
- Shared program templates or social features after MVP?

## Implementation

- Approve stack: PWA framework, backend runtime, storage, hosting.
- Formal REST/MCP contracts and client operation envelope.
- MVP cutline and acceptance tests across iOS and Android.

## Suggested next design session

Specify the **workout UX as a state machine** (open, start, record, modify, complete, recover) together with the minimal offline sync protocol.
