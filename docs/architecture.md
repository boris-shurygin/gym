# System architecture — working design

## Confirmed responsibilities

- **ChatGPT agent + runtime Skill:** discuss goals, inspect workout history, propose changes and set progression rules.
- **REST API and MCP adapter:** two entry points to the same validated application operations.
- **Backend:** authorization, program/version management, deterministic progression, session management and analytics.
- **Mobile PWA:** touch input, history/charts and a locally durable write queue.
- **Database:** user-scoped authoritative records and revision trail.

Neither MCP nor the mobile client should bypass domain validation. The backend is authoritative for published plans and historical facts.

## Candidate modular monolith (proposal)

Modules: identity, programs, progression engine, sessions, synchronization, analytics. Suggested but **not approved** technologies: React/TypeScript PWA + IndexedDB; Python/FastAPI; PostgreSQL; MCP SDK.

## Program flow

1. Agent reads current user-scoped program and history.
2. Agent proposes a change based on an explicit base version and explains why.
3. Backend validates; user reviews/approves.
4. Backend atomically publishes a new immutable version.
5. Future workout plans use the new version. In-progress sessions are not silently changed.

## Workout flow

1. Preview a workout with backend-calculated targets.
2. At session start, capture a snapshot of plan and rule versions.
3. Locally commit each set and a pending sync operation in one transaction.
4. Synchronize operations, with server-side validation, deduplication and acknowledgements.
5. On completion or later correction, refresh derived analytics and progression state.

**Open:** how a session starts with no connection; exact status transitions; future workout calendar vs on-demand creation.

## Security and sync boundaries

- The server derives the user identity from authentication credentials.
- MCP should use scoped delegated authorization, subject to supported client integration.
- Local data is partitioned by identity; device switching and logout with pending writes require an explicit policy.
- Each operation has an ID and is processed idempotently.
- Concurrent modifications need conflict detection, not blind last-write-wins.
- Browser storage is not a guaranteed backup; pending changes must remain visible.

## Background behavior

Best-effort background delivery may be used on supported Android browsers. iOS PWA background execution is limited and cannot be guaranteed after the app is closed. Both platforms must retry on foreground/resume and stay correct without background execution.

## Operational choices still open

Hosting, auth provider, offline protocol, local cache policy, deployment topology and schema details. A concrete **unapproved** proposal is in [hosting.md](hosting.md).
