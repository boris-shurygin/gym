# Open questions

This list deliberately separates **unresolved** design questions from accepted ADRs.

## Mobile / synchronization

- Which program templates and computed target snapshots should be prefetched so a chosen template can start offline? What if the cached program version is stale?
- What is the minimal local history cache?
- How are concurrent changes to the same set from two devices reconciled?
- How are unsynced local changes handled on logout, account switch, browser data deletion and app upgrade?
- Accepted navigation: compact overview (normally 4–5 exercises) → dedicated exercise logger, with plan-based defaults and **per-set volume bars (weight × reps; ADR-0008)**. Open: intended vs actual set count editing; editing/removing set results; display geometry, modality-specific metrics and target-rep selection within a range.

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

## Logging and monitoring (proposal)

- **Accepted (ADR-0012):** PWA TelemetryClient → bounded separate best-effort buffer → batched authenticated `/api/v1/telemetry` → Cloud Functions validation → Monium. Never put diagnostic retries in the workout outbox.
- Review the **remaining** [observability proposal](observability.md): platform/backend logs, custom metric strategy, sparse-traffic alerting and dashboard design. These parts are not yet accepted.
- Validate Cloud Functions log delivery directly into Monium rather than relying on the deprecated Cloud Logging pipeline; choose an OTLP strategy safe under function termination.
- Define production notification recipients, synthetic health checks, backup age/restore verification, actual cost budgets and **PWA telemetry consent/opt-out policy, final schema and rate limits**.

## Implementation

- Selected infrastructure: Russian Yandex Cloud, YDB Serverless/SQLAlchemy 2.0 with Cloud Functions API (ADR-0010). See [hosting](hosting.md) and accepted [CI/CD](ci-cd.md) (ADR-0011).
- Confirm real MCP write/modify availability for intended ChatGPT accounts; write support in ChatGPT is plan/workspace dependent.
- Choose identity provider and OAuth strategy before publicly onboarding friends; check storage/data flows and relevant Russian personal-data rules.
- Choose frontend HTTPS domain/API origin, backup/restore requirements, Cloud Functions release permissions and staging/production isolation.
- Implement a one-off **Yandex Cloud-hosted** migration/backfill job triggered and tracked from GitHub Actions; decide YDB migration revision bookkeeping and partial-DDL recovery.
- Define retention/support window for old offline PWA clients and API contracts; keep backward compatibility until explicitly changed.
- Monitor GitHub Actions artifact/cache storage quotas; re-evaluate billing if this currently public repository becomes private or if larger runners are needed.
- Validate migrations and recovery when YDB DDL partially succeeds; old offline PWA clients must remain compatible.
- Formal REST/MCP contracts and client operation envelope.
- MVP cutline and acceptance tests across iOS and Android.

## Current design work

The proposed mobile workout flow, screen responsibilities, independent session/sync states, failure cases and acceptance scenarios are in [mobile-app.md](mobile-app.md). It is a **draft**, not a set of approved UX decisions.

Next review these priorities:

1. Offline prefetch/freshness policy for any selectable program template (no ad-hoc session creation in MVP).
2. Exact interaction for logging/editing/removing sets and changing intended vs actual set count on the dedicated exercise screen.
3. How backend plans provide exact suggested reps for ranges; set-volume bar overflow scale; whether an aggregate exercise-volume bar adds value; how to handle bodyweight/assisted/non-kg exercises (ADR-0008).
4. Completion with skipped/incomplete exercises.
5. Cross-device sync conflicts and account switching with pending operations.

Then detail wireframes and the offline operation envelope before coding.
