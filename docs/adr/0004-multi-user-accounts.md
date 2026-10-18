# ADR-0004: Individual multi-user accounts

- **Status:** Accepted
- **Date:** 2026-10-18

## Context

The product will be used by the owner and friends, some on iOS and others on Android, each with their own training program and history.

## Decision

Support multiple personal accounts from the first release. User-owned programs, workout results, measurements and analytics are private by default. Identity and authorization are enforced at the backend, including for MCP access. Shared workouts and social features are not part of the first version.

## Consequences

- User-scoped data and server-side authorization are fundamental.
- Offline caches must be account-isolated and safe on sign-out/switch.
- Future sharing needs explicit permission models rather than accidental cross-account access.

## Not decided

Identity provider, invitation policy, future friend sharing and social functionality.
