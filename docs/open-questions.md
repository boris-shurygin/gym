# Open questions

This list deliberately separates **unresolved** design questions from accepted ADRs.

## Mobile / synchronization

- Which program templates and computed target snapshots should be prefetched so a chosen template can start offline? What if the cached program version is stale?
- What is the minimal local history cache?
- How are concurrent changes to the same set from two devices reconciled?
- How are unsynced local changes handled on logout, account switch, browser data deletion and app upgrade?
- Accepted navigation: compact overview (normally 4–5 exercises) → dedicated exercise logger. Open: how to adjust intended set count separately from actual extra/missing sets and the edit/undo UX.

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

## Current design work

The proposed mobile workout flow, screen responsibilities, independent session/sync states, failure cases and acceptance scenarios are in [mobile-app.md](mobile-app.md). It is a **draft**, not a set of approved UX decisions.

Next review these priorities:

1. Offline prefetch/freshness policy for any selectable program template (no ad-hoc session creation in MVP).
2. Exact interaction for logging/editing/removing sets and changing intended vs actual set count on the dedicated exercise screen.
3. How next-set defaults should work (planned vs last performed).
4. Completion with skipped/incomplete exercises.
5. Cross-device sync conflicts and account switching with pending operations.

Then detail wireframes and the offline operation envelope before coding.
