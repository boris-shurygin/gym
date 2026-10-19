# Hosting: Yandex Cloud serverless

**Accepted direction:** Russian hosting with Yandex Cloud (ADR-0009), YDB Serverless via SQLAlchemy 2.0 and Python Cloud Functions for REST (ADR-0010). **Not yet deployed.** Details below remain design proposals.

## Planned topology

- Mobile PWA (React/TypeScript, offline IndexedDB) served via HTTPS from Yandex Object Storage.
- API Gateway `/api/v1` → Python Cloud Function implementing modular application services.
- YDB Serverless for user-owned programs, sessions, sets and sync operation IDs; SQLAlchemy 2.0 with `ydb-sqlalchemy` dialect.
- Optional Cloud Function provisioned instance to improve cold-start latency; select memory and reserve capacity only after benchmarking the function package and reviewing fixed costs.
- MCP adapter shares business logic, but the final hosting of Streamable HTTP endpoint requires separate transport validation (possibly Serverless Containers).
- GitHub Actions (OIDC federation) for deployment, separate staging and production resources; an API version tag controls promotion.

No VM/PostgreSQL is required for this MVP. A prior single-VM plan is superseded.

## Data and operation constraints

The ORM does not make YDB equivalent to PostgreSQL. Use explicit primary keys, carefully designed indexes and integration tests for operation deduplication, transaction retries, RU cost and authorization. Migration scripts must be tested against YDB DDL semantics. No DDL on function startup or user HTTP requests.

Object Storage static hosting requires suitable HTTPS/domain configuration. Plan for old, offline PWA clients and cached JS continuing to use supported API versions after a release.

Backups of YDB, restore verification, least-privilege cloud identities, isolated environments and private account data are requirements before the first real-user pilot.

See [CI/CD and migrations](ci-cd.md) for the **proposed**, not-yet-accepted release workflow and [open questions](open-questions.md) for unfinished choices.

## References

- https://ydb.tech/docs/ru/integrations/orm/sqlalchemy
- https://yandex.cloud/ru/docs/api-gateway/concepts/extensions/cloud-functions
- https://yandex.cloud/ru/docs/storage/concepts/hosting
- https://yandex.cloud/ru/docs/iam/tutorials/ci-cd-github-functions
