# Observability proposal — Gym on Yandex Cloud

**Status: Proposed, not accepted or implemented.** Reviewed 2026-10-20.

Scope: structured backend logs, platform and custom metrics, alarms, dashboards, release correlation, client offline diagnosis and cost control. Selected infrastructure remains **Yandex API Gateway → Cloud Functions → YDB Serverless**, plus static PWA (ADR-0010) and GitHub Actions CI/CD (ADR-0011). This document does not change accepted architecture.

## Key provider fact: prefer Monium, not a new Cloud Logging-only integration

Yandex Cloud Logging is scheduled to be discontinued in **Q2 2027**. Start with **Yandex Monium** (Metrics, Logs and, when needed, Traces) rather than investing in a soon-to-be retired logging pipeline.

Monium can display Cloud Logging records today, but writing those records to the old Cloud Logging ingest endpoint retains its old pricing; for direct Monium logging, use its supported OpenTelemetry/OTLP ingestion endpoint. **Verify the real Cloud Functions → Monium delivery path in an integration spike**: provider stdout/native logs and an OTel exporter can have different costs and delivery semantics. In an ephemeral function, background buffers may not flush after returning HTTP response, so flush with a bounded timeout or use a validated supported ingestion mechanism, without breaking user requests if logging fails. Do not add a permanently running collector/server for MVP.

[Provider announcement](https://yandex.cloud/ru/docs/monium/logs/logging-migration); [Monium overview](https://yandex.cloud/ru/docs/monium/overview); [OTLP log ingest](https://yandex.cloud/ru/docs/monium/logs/quickstart).

## What each storage/service is responsible for

| Data | Location | Reason |
| --- | --- | --- |
| Operational structured logs | Monium Logs | Error diagnosis and individual request investigation, short retention |
| Built-in cloud technical metrics | Monium Metrics | API Gateway 5xx/latency, Function errors/initializations, YDB RU |
| Selected app counters and distributions | Monium Metrics and/or vetted log-derived aggregates | Sync outcomes, failed publication, latency |
| Optional request traces | Monium Traces (OTLP/OpenTelemetry) | Debug latency spanning API/domain/YDB |
| Durable business audit | **YDB application tables** | Who changed a set or published a program; not overwritten/deleted by log TTL |
| Client offline diagnostic state | IndexedDB local to PWA | Pending queue length, oldest unsynced operation, recent client errors |
| Cloud control-plane/security audit | Evaluate Yandex Audit Trails later | Infrastructure IAM/admin changes, distinct from app audit events |

Monium Logs currently has **31-day fixed TTL**. Audit records for training changes must not rely on that log retention.

## Logging contract

- Emit one JSON request summary on success and structured error events for failures. Avoid verbose SQL query logs, every-repetition INFO events or repeated exceptions in retry loops.
- Suggested fields: `timestamp`, `level`, `service`, `environment`, `release_sha`, `event`, `request_id`, `route_template`, `method`, `http_status`, `duration_ms`, `error_code`.
- For sync outcomes, add `operation_count`, `applied`, `duplicates`, `conflicts`, `rejected`; optionally a random operation ID for authorized incident tracing, **never as a metric label**.
- Generate/propagate a request/trace correlation ID from API Gateway/function. Release SHA/Cloud Function version from deploy metadata links spikes to specific releases.
- **Do not log** token/authorization headers, passwords, email addresses, workout contents (weights, reps, session notes), arbitrary request/response bodies, identifying path parameters or user IDs by default. Route names like `/sessions/{session_id}/sets` are safe, literal IDs are not.
- Whitelist event fields; central redaction and safe exception formatting. Scrub accidental secrets and personal data before sending telemetry.
- Error taxonomy: stable low-cardinality `error_code` and `component`. Distinguish expected 4xx/duplicate/conflict from actual 5xx and broken sync guarantees.
- Correlation IDs can be high-cardinality **log fields**, but never dimension labels on metrics. Metric labels should be fixed small sets: `environment`, `component`, `route_template`, `outcome`, `error_code` or `release_sha` only if cardinality remains bounded by retained releases.
- Even when a request successfully commits to YDB, telemetry delivery may fail; logging must never cause a repeated user write or mark a successful operation as failed.

Example log (fictional):

```json
{
  "level": "INFO",
  "event": "sync_batch_completed",
  "service": "gym-api",
  "environment": "production",
  "release_sha": "ab1234",
  "request_id": "req-8c29",
  "operation_count": 4,
  "applied": 3,
  "duplicates": 1,
  "conflicts": 0,
  "duration_ms": 84
}
```

## Metrics and counters

### Automatically available platform metrics (priority P0)

- **API Gateway:** request count by status, 5xx counts, latency histogram / p95. Do not treat ordinary 401/404 as application outage.
- **Cloud Functions:** invocations, function errors, execution-time distribution, concurrent requests, initializations/cold starts and throttles where supported.
- **YDB Serverless:** API errors, request latency and **RU per second/consumption**, separate per-method usage if needed.
- **Object Storage / static PWA:** test reachability of index/assets and HTTP failures using a synthetic check rather than relying on platform's app health metric alone.
- **Billing:** cost by service/category. A budget alarm **notifies but does not automatically enforce a hard spending ceiling**.

[API Gateway metrics](https://yandex.cloud/ru/docs/api-gateway/metrics); [Functions metrics](https://yandex.cloud/ru/docs/monitoring/metrics-ref/functions-ref); [YDB metrics](https://yandex.cloud/ru/docs/ydb/metrics).

### Custom application outcomes (priority P1)

| Name (suggested) | Type/meaning | Important interpretation |
| --- | --- | --- |
| `gym_sync_operations_total{outcome}` | counter of applied / duplicate / conflict / rejected / retryable_error | duplicate is **not** an application failure |
| `gym_sync_batch_duration_ms` | per-batch latency distribution | includes YDB roundtrips |
| `gym_program_publish_total{outcome}` | success/failure count | store authoritative audit in YDB |
| `gym_sessions_completed_total` | completed sessions count | avoid per-user labels and medical/workout details |
| `gym_client_reports_total{kind}` | opted-in/de-identified client failure reports | capped submission frequency; no infinite retry |
| `gym_backup_last_success_timestamp` | last independently verified backup timestamp | check staleness with scheduled validation |

Start with platform metrics plus event logs. For app counters, choose **OTLP custom metrics** or carefully vetted **Monium log-based derived metrics** after testing delivery, cost, correctness and behavior on idle serverless traffic. Monium log-derived metrics are currently **Preview**; avoid making a critical alarm depend solely on them before validation. Do not make synchronous external telemetry calls on every recorded set if they noticeably increase API latency.

## Offline PWA client diagnostics

Store locally a bounded diagnostic ring buffer (e.g. last 50–100 non-sensitive events) separate from the **durable workout sync outbox**. Expose a Diagnostics screen: app/release version, online/offline state, unsynced operation count, age of oldest unsynced operation, last server ACK, and last IndexedDB/local save problem.

The server cannot know about a device that has been offline for days. It must not claim a zero global backlog based solely on inbound sync requests. On reconnect, only report safe aggregated diagnostics with user control and bounded frequency; do not allow diagnostic-report retries to clog the main workout outbox or cause recursive failures.

Proposed low-risk events: IndexedDB storage failure, unhandled JS error, repeated failed sync attempts after reconnect, incompatible API error. Never send raw training data or account identifiers in unaudited analytics payloads.

## Minimal dashboards

**(A) Production health:** request throughput and 5xx, latency (p50/p95 with low-traffic caveat), function init/error counts, deployed release version and synthetic probe status.

**(B) Data integrity and sync:** operation outcomes split by applied/duplicate/conflict/reject; recent repair-required errors, server sync latency, backup last success and recovery tests. Pending queue per offline phone is local and shown in the PWA, **not an accurate server-side global gauge**.

**(C) Costs:** YDB RU consumption, Functions invocation and active/prepared instance usage, API Gateway requests, volume of ingested logs/traces, cloud spend versus budget.

Each dashboard can filter staging vs production. Low-cardinality dimensions only. Begin with platform service dashboards, then add custom panels.

## Starter alerts — suggested thresholds, tune after pilot

| Signal | Candidate condition | Severity/delivery |
| --- | --- | --- |
| API unavailable | Synthetic `/health/ready` probe fails **3 consecutive times** at ~5-minute cadence, checking a cheap bounded YDB query | Critical, Telegram + email |
| HTTP 5xx burst | **≥3 gateway 5xx over 10 minutes** (tune for very low traffic) | Warning, Telegram |
| Persistent backend/YDB failure | Several genuine failures/timeout errors over 10–15 minutes, not ordinary validation 4xx | Critical, Telegram |
| Sync integrity issue | Unexpected op-commit divergence, duplicate applied twice or unrecoverable conflict (if detected) | Critical, Telegram |
| Backup stale | No successful/verified backup in ~36–48 h when target cadence is daily | Critical, Telegram |
| Abnormal resource consumption | YDB RU or request throughput substantially exceeds normal baseline for sustained period | Warning, email/Telegram |
| Cloud billing | Configured monetary thresholds, e.g. 50% / 80% / 100% of explicit monthly budget | Email/Telegram, spending **not auto-stopped** |

Do **not** alert merely because Gym has no incoming traffic: inactivity is expected. Nor use percentage-only error rates for sparse traffic (1 error out of 2 requests = 50% noise). Initial thresholds are **examples**, not accepted SLOs. Synthetic health checks must run independently of ordinary user traffic; a timer-triggered function may be sufficient but is still in the same cloud failure domain, so decide if independent external vantage checks are warranted later.

Monium alert notifications can go to Telegram and email. Throttle repeated notifications and provide incident links/release context. Test a deliberate synthetic failure in staging before enabling production paging.

## Distributed traces (priority P2)

Monium Traces accepts OpenTelemetry OTLP. Begin **without full tracing for every request**. Add sampled traces (always sample selected failures, low proportion of successful requests) when latency investigation warrants it. In serverless, verify bounded exporter flush so spans are not abandoned at function return, and do not block the user's logged set because tracing backend is slow. Avoid sensitive span attributes.

## Cost, TTL and volume discipline

As documented in October 2026, approximate Russian Monium prices include:

- **Monium Logs ingest: 4.40 ₽ per GB**; same ingestion rate for Traces in pricing examples. Monium Logs storage itself is not billed, **TTL 31 days**.
- **User-supplied metric values** charged by ingestion volume; **automatic Yandex resource metrics ingestion and Monium console reads are not billed**.
- **Alert evaluation:** 1.5 ₽ per 1,000 alert-hours since June 18, 2026; persistent alert definitions incur a small recurring fee.
- SMS/phone delivery may be extra; start with Telegram + email.
- Cloud Functions native logging may initially use old Cloud Logging delivery and charges unless Monium direct ingestion is configured; run a proof-of-delivery check and avoid dual ingestion.
- Avoid logging every SQL statement, excessive stack traces or high-cardinality metric dimensions. Budget alerts in Yandex Cloud notify only; prevent unbounded YDB RU consumption with appropriate app-level limits/quotas as separate controls.
- Provider rates change: verify actual billing after deployment, not only this planning estimate.

Sources: [Monium pricing](https://yandex.cloud/ru/docs/monium/pricing), [Monium Logs retention](https://yandex.cloud/ru/docs/monium/logs/concepts/ttl), [Cloud Logging migration](https://yandex.cloud/ru/docs/monium/logs/logging-migration), [Billing budgets](https://yandex.cloud/ru/docs/billing/concepts/budget).

## Rollout and acceptance checks

**MVP first release (P0)**
1. Structured JSON request/error logs with redaction and release/request correlation; verify receipt via **Monium** in real Cloud Functions runtime.
2. Built-in API Gateway / Cloud Functions / YDB metrics and a basic health/cost dashboard.
3. Telegram/email notifications for repeated service errors, synthetic availability, stale backup and budget thresholds.
4. Minimal IndexedDB / sync status display on each phone; never depend on telemetry to commit a set.
5. Durable audit entries for published programs and historical corrections in YDB.

**Next iteration (P1)**
1. Custom sync outcome counters and latency, client diagnostic events with rate limits, root-cause examples.
2. Train on baseline traffic; adjust low-volume thresholds. Verify alerts with simulated failures.
3. More detailed RU budget and per-route performance views.

**Later if needed (P2)**
1. OTLP tracing with sampling, more sophisticated SLOs, security/cloud Audit Trails.
2. External availability vantage check and richer analytics where concrete incidents justify them.

## Open choices for a follow-up decision

- Approve Monium-first observability; final mechanism of structured Cloud Functions logs → Monium.
- Alert destinations and who is on-call (Telegram vs email); severity thresholds after observing traffic.
- Client crash-report policy and opt-in/privacy approach.
- Synthetic probe implementation and external/out-of-cloud checks if desired.
- How backup verification and its last-success metric are scheduled.
- Whether app counters are sent directly as OTLP metrics or derived from logs.
- Confirm production budget amounts and thresholds once a billing account exists.

This is a **proposal** for review. Do not mark it Accepted or create an ADR until discussed.
