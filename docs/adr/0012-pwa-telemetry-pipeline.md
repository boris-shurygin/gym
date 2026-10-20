# ADR-0012: Best-effort PWA telemetry through Gym API and Monium

- **Status:** Accepted
- **Date:** 2026-10-20
- **Related:** [ADR-0002](0002-local-first-pwa.md), [ADR-0010](0010-ydb-serverless-cloud-functions.md), [ADR-0011](0011-github-actions-cicd.md)
- **Detailed design:** [PWA telemetry](../observability.md#client-pwa-telemetry-accepted-design-adr-0012)

## Context

Gym is an offline-first PWA. Browser errors, failed IndexedDB writes, sync latency and local queue health cannot always be inferred from server logs. Direct browser export to Monium would expose cloud credentials. Unlike workout records, diagnostic telemetry is expendable.

## Decision

1. Implement a small **TelemetryClient** in PWA collecting only whitelisted and sanitized error, sync, performance, app-start and service-worker diagnostic events. Aggregate counters locally.
2. **Never send PWA telemetry directly to Monium.** Buffer in a bounded/expiring local diagnostic queue **separate from durable workout outbox**.
3. Send small batches with an authenticated **`POST /api/v1/telemetry`** through Yandex API Gateway into a Python Cloud Function, typically about once per minute during active online use. Allow urgent best-effort sends for local storage errors.
4. Optional `sendBeacon()`/`fetch keepalive` on backgrounding; no guarantee of delivery or acknowledgment from the browser. Retry only with bounds and expiry; drop diagnostics rather than risking workout reliability.
5. Backend validates event names, versions, field allowlists, numeric bounds, payload size, authentication and rate limits. It forwards sanitized structured events and **fixed known** aggregated metrics/logs through a bounded OTLP/HTTP delivery into Yandex Monium. Keep credentials solely server-side; flush synchronously within the function execution or use a verified provider mechanism.
6. Diagnostic counts are **approximate** and may be lost, duplicated or spoofed. They cannot replace authoritative YDB records or audit, nor dictate training results or billing.
7. Neither telemetry failure nor Monium latency may affect the success of an already-saved set, user interaction, or durable workout sync. Show local queue/last ACK status in the PWA diagnostics UI; offline devices are invisible to server-side metrics until reconnection.
8. Do not collect raw workout data, user identifiers as metric labels, tokens, full request bodies or arbitrary exception messages/stacks. End-user privacy disclosures and collection/consent controls are still to be designed.

## Consequences

A single cheap serverless telemetry endpoint can cover client diagnostics without an always-on collector, while preserving safe credentials and local-first reliability. Events and custom metrics require volume limits, sanitization and explicit tests. Avoid direct per-event metric writes from the browser.

## Still open

Exact final telemetry JSON schema, batch quotas, consent/opt-out policy, authentication/cookie constraints for beacon, forwarding strategy and rate limits. The **broader observability plan** (server logs, platform dashboards, monitoring thresholds, alerts) remains a separate proposal rather than accepted by this ADR.
