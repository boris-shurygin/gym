# ADR-0002: Local-first cross-platform PWA

- **Status:** Accepted
- **Date:** 2026-10-18

## Context

Gym internet connectivity can be unreliable. The app must support both iPhone and Android for multiple users.

## Decision

Use a mobile-first PWA with locally durable workout recording and later synchronization. Saving a set must not require immediate connectivity. Browser background synchronization is optional and best-effort; correctness must not rely on background execution, particularly on iOS.

## Consequences

- Need a local data store and an outbound operation queue.
- Need server-side idempotency, acknowledgements, user isolation and conflict handling.
- Pending/synced states must be clear in the UI.
- Local browser storage is not an infallible backup; support recovery/precautions.
- Native apps are not necessary for the first iteration.

## Not decided

Detailed sync protocol, conflict UX and whether specific work must also be cached for full offline session creation.
