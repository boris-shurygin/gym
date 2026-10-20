# CI/CD and YDB schema migrations

**Status: Accepted CI/CD architecture (ADR-0011), not yet implemented.** Updated 2026-10-20. Exact migration-runner implementation, compatibility window, authentication and observability stack remain open.

## Target workflow

```text
feature branch → PR checks (no prod access) → merge main
                                            ↓
                         immutable build and test artifacts
                                            ↓
                   staging: DB migrate → API version → PWA
                                            ↓
                             smoke/integration tests
                                            ↓
                          manual production approval
                                            ↓
              prod: DB migrate → API version/tag → PWA
                                            ↓
                             release smoke tests
```

Use **GitHub Actions**, without owning a permanent CI worker, and `web/`, `api/`, `db/migrations/`, `infra/`, `tests/`, `.github/workflows/` in one repository. Optional path filters avoid work for unrelated changes; shared contracts or lockfiles trigger broader validation.

## Pull request validation

| Changed scope | Checks |
| --- | --- |
| `web/` | ESLint, TypeScript, unit/component tests, production build, browser smoke tests, client/API contract tests |
| `api/` | Ruff, Python typing/pytest, build function package, isolated YDB integration tests for reads/writes, cross-user auth, idempotency |
| `db/` | YDB-specific migration review; apply empty→head and last-released→head against disposable YDB; post-upgrade compatibility tests |
| `infra/` | OpenAPI/YAML validation; Terraform validation when used; permission and gateway integration checks |
| shared API schemas | Run both web and API checks; check old-client compatibility |

PRs must not have production credentials or write to the shared staging DB. Use only synthetic data and local/disposable YDB instances for PR database tests. Untrusted PRs (including forks) must not be able to mint production deployment credentials.

## Merge main → staging

1. Package versioned Python function artifact, compiled PWA hashed assets and migration set; record Git SHA.
2. Only one deployment/migration runs in the same environment at a time.
3. Apply pending additive YDB migrations, with a reviewed plan and tracked revision.
4. Publish a new Cloud Function version, smoke-test it through a staging route/tag; point staging gateway to it.
5. Upload new hashed JS/CSS/static assets; publish `index.html`, manifest and service worker last; retain older assets for clients still loading cached HTML.
6. Run end-to-end staging tests: program preview, set insert, replay same operation ID, completion, correction, history, cross-user access denial, offline queue survival and API compatibility.
7. Store an immutable release manifest (SHA + artifact hashes + DB migration revision + deployed function version).

## Staging → production

Explicit **manual approval** required. Deploy **the same built artifacts**, not rebuild from the branch. Gate production migrations behind the approval and serialize production releases. Promote in sequence: schema expansion → new API version → UI assets/entrypoint. Run smoke tests after each exposed change. If a migration fails, stop the deployment; do not change the API tag/frontend. If production API fails, repoint function's gateway tag to previously working version, while maintaining schema compatibility.

API Gateway integration can target a Cloud Function **tag**; it defaults to `$latest`. Production should use a controlled tag such as `stable`; creating a new version never by itself redirects production. Retain old versions and move the tag for rollback.

## YDB migration discipline

- Use SQLAlchemy 2.0 `ydb-sqlalchemy` and Alembic with YDB-specific implementation in `env.py`; inspect every generated migration/YQL change.
- **Never run migrations on Cloud Function startup/invocation.** Production schema migrations and backfills must **execute inside Yandex Cloud** on a privileged, one-off job triggered/orchestrated by GitHub Actions, with an explicit release order and job result. The exact Yandex execution service is not selected yet; the GitHub-hosted runner must not read/download production data or run production DDL itself.
- YDB DDL executes outside ordinary DML transactions. Multiple DDL operations do not provide all-or-nothing rollback. Design each step for observability and safe recovery from partial completion. Don't silently mark revisions successful until schema verification passes.
- Apply **expand → deploy → backfill → contract**. Add nullable columns and maintain backwards-compatible reads/writes; deploy API supporting old and new; run restartable backfills as a separate job; make destructive operations only in later releases.
- Validate upgrade both from empty and from previous released schema. Isolate migrations on PR; serialize shared staging/production migrations with a GitHub Actions concurrency lock.
- On schema failure inspect actual DB state, repair it using an approved script; don't assume a normal Alembic downgrade or a code rollback restores old schema.
- Backups and restore testing protect against data loss; they are not a substitute for compatible schema evolution.

### Example: warm-up flag

1. `sets.set_kind` is added as optional.
2. Backend reads missing values as `working` and accepts old requests without the field.
3. Backfill existing rows in bounded, idempotent batches.
4. Only after the old PWA/API compatibility window may obsolete behavior be removed.

## Frontend and offline PWA release

- Fingerprinted assets (`app.<hash>.js`) uploaded first, with long caching; HTML, manifest and service worker uploaded last with short/no caching. Yandex Object Storage supports `Cache-Control` object metadata.
- Retain old hashed assets for a declared client compatibility window.
- Never force service-worker replacement that loses a user's IndexedDB outbox. Test upgrades from older offline clients and delayed retries.
- Maintain a versioned `/api/v1` request/response contract. Old clients can remain active after production deployment; API changes must be compatible with their operations.
- Rollback frontend via older entrypoint/manifest; existing open browser tabs may continue running newer JS until reloaded.

## Deployment identities and protection

Use Yandex IAM Workload Identity Federation with GitHub Actions OIDC (temporary tokens, not static service account keys), restricting identity by repository/branch/environment. Staging and production service accounts have separate least-privilege roles. The migration deploy identity needs schema rights but ordinary runtime functions should not. Keep application user permissions enforced within API.

## GitHub Actions runner choice and billing

- The current repository `boris-shurygin/gym` is **public** (verified 2026-10-20). Standard GitHub-hosted **Ubuntu runners** have free runner minutes for public repositories. We do **not** rent a GitHub runner or run a permanent self-hosted CI VM.
- **Larger runners cost money** even for public repositories. Keep workflows on standard runners unless separately approved.
- Free runner minutes are not the same as unlimited storage. As of 2026-10-20 GitHub Free includes **500 MB artifact storage** (shared with GitHub Packages) and **10 GB Actions cache per repository**. Limit artifact retention and clean obsolete caches; do not store production backups, user data or long-lived secrets in CI.
- If the repository becomes **private**, GitHub Free currently includes **2,000 hosted-runner minutes/month** before applicable extra charges. Reevaluate GitHub billing before changing visibility or selecting paid runner types. For the current public repository and included storage, expected GitHub runner spend is **0 ₽**; Yandex Cloud usage is billed independently.
- Official [GitHub Actions billing](https://docs.github.com/en/billing/concepts/product-billing/github-actions) and [runner reference](https://docs.github.com/en/actions/reference/runners/github-hosted-runners).

## Workflows to implement

- `.github/workflows/ci.yml`: PR checks, artifacts, disposable YDB tests.
- `.github/workflows/deploy-staging.yml`: merge main, additive migrations, function, PWA, smoke tests.
- `.github/workflows/deploy-production.yml`: approved manual promote the exact build revision, serialized migration/release, smoke tests.

## Locked workflow decisions vs open implementation details

**Accepted:** GitHub Actions CI/CD on standard GitHub-hosted runners; PR checks with disposable synthetic-data YDB; automatic staging deployment on merge to `main`; manual, serialized production promotion of the *same tested artifacts*; OIDC federation with separate least-privilege identities; production migrations executing **within Yandex Cloud**; additive expand → deploy → backfill → contract migrations; API/function version-tag and PWA asset rollback; old offline clients remain supported across changes.

**Not implemented:** the three workflow files, cloud infrastructure and migration job. These documents are not evidence of a live deployment.

## Open decisions

Actual function build package method, Alembic revision bookkeeping and partial-migration recovery scripts, runtime identity provider, approval mechanism, PWA/API compatibility window, domain/CORS/cookies, and available recovery RPO.

**Logging, metrics, counters, alerting and dashboards are outside the accepted CI/CD decisions.** A separate [observability proposal](observability.md) is available for review. CI requires pass/fail checks and minimal release/smoke-test signals.

## Provider docs

- https://ydb.tech/docs/ru/integrations/orm/sqlalchemy
- https://ydb.tech/docs/ru/concepts/transactions
- https://yandex.cloud/ru/docs/api-gateway/concepts/extensions/cloud-functions
- https://yandex.cloud/ru/docs/functions/operations/function/tag-add
- https://yandex.cloud/ru/docs/iam/tutorials/ci-cd-github-functions
- https://yandex.cloud/ru/docs/storage/qa
