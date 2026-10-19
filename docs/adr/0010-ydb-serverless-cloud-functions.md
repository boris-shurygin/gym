# ADR-0010: YDB Serverless and Cloud Functions for Gym

- **Status:** Accepted (technology direction)
- **Date:** 2026-10-19
- **Related:** [ADR-0009](0009-russian-hosting.md)

## Context

Gym has low, intermittent traffic. The previous one-VM/PostgreSQL topology has idle-time compute and maintenance costs. The project owner prefers YDB through SQLAlchemy, and Yandex Cloud Functions for REST with usage-based billing.

## Decision

Use **YDB Serverless with SQLAlchemy 2.0 + ydb-sqlalchemy**, and **Python Cloud Functions behind Yandex API Gateway** for the REST API. Host the static offline-first PWA on Yandex Cloud, likely Object Storage. Do not provision a persistent Compute Cloud VM or PostgreSQL for MVP. Domain logic must remain reusable by a remote MCP adapter, whose transport hosting is not yet decided.

An optional prepared/provisioned Cloud Function instance can be evaluated as a latency/cost trade-off after benchmarking.

## Consequences and remaining choices

YDB/YQL differs from PostgreSQL; prove schema/index queries, idempotency, transaction retries and migrations with YDB integration tests. Schema migrations must run independently of function requests. Keep old cached mobile clients compatible across releases. The proposed CI/CD process is described in [ci-cd.md](../ci-cd.md) and is **not accepted by this ADR**.

Open: exact functions and API routing, auth, YDB model and migrations, frontend HTTPS/CORS, MCP host/streaming, backup and production approval.
