# ADR-0011: GitHub Actions CI/CD and Yandex Cloud deployment

- **Status:** Accepted (deployment process and security model)
- **Date:** 2026-10-20
- **Related:** [ADR-0009](0009-russian-hosting.md), [ADR-0010](0010-ydb-serverless-cloud-functions.md)
- **Detailed workflow:** [docs/ci-cd.md](../ci-cd.md)

## Context

Gym is a small offline-first PWA with Python Cloud Functions and YDB Serverless on Yandex Cloud in Russia. We need repeatable CI/CD without running or renting a permanent CI server. The source repository `boris-shurygin/gym` is public.

## Decisions

1. **GitHub Actions** is the CI/CD orchestrator on **standard ephemeral GitHub-hosted Ubuntu runners**. No dedicated/self-hosted CI infrastructure.
2. **Pull Requests** run lint/typecheck, frontend/browser, backend/unit, contract, YDB integration and migration tests as relevant, using synthetic data and disposable YDB. Untrusted PRs have no production identities.
3. Merge to **`main` automatically deploys to staging** after required checks and creates immutable tested release artifacts.
4. **Production requires manual approval**, receives the *same* artifacts that passed staging, and serializes deployments and schema changes.
5. GitHub Actions uses **OIDC → Yandex Workload Identity Federation → short-lived IAM credentials**. Separate least-privilege staging/production accounts; no persistent authorized keys committed or stored in GitHub.
6. GitHub orchestrates but **production YDB migrations/backfills execute within Yandex Cloud**, with a separate privileged job, not on Cloud Function request/startup and not by copying production data to GitHub runners. The exact one-off execution product is not yet chosen.
7. Use compatible **expand → deploy → backfill → contract** schema evolution. Older cached/offline PWA clients must still be able to sync after rollout. Code rollback does not imply DB schema rollback.
8. Production API Gateway points to a **controlled Cloud Function version tag**, not automatic latest; assets are immutable/fingerprinted and PWA entrypoints/service worker published last.
9. Log collection, counters, dashboards, alerts and monitoring are deferred to a separate design discussion.

## Billing

For public repositories, **standard GitHub-hosted runners have free, unlimited monthly runner minutes**, confirmed in GitHub documentation as of 2026-10-20. **Larger runners are paid**, and artifact/cache storage have finite limits. GitHub Free currently includes **500 MB of artifact storage** (shared with GitHub Packages) and **10 GB cache per repository**. If the repository becomes private, GitHub Free's included runner allowance is currently 2,000 minutes/month; reassess costs. For the current public repo and standard runners within storage limits, expected GitHub Actions runner budget is **0 ₽**. Yandex Cloud has separate billing.

## Status and open implementation details

**This approves the architecture but does not deploy any workflows or cloud resources.** Still to implement: workflow files, migration runner service, schema revision/recovery strategy, production approval settings, release artifact retention, offline-client compatibility window and identity/domain setup. See [CI/CD](../ci-cd.md).

## Sources

- [GitHub Actions billing](https://docs.github.com/en/billing/concepts/product-billing/github-actions)
- [GitHub runners reference](https://docs.github.com/en/actions/reference/runners/github-hosted-runners)
- [Yandex OIDC federation](https://yandex.cloud/en/docs/iam/operations/wlif/setup-wlif)
- [Yandex GitHub CI/CD tutorial](https://yandex.cloud/en/docs/tutorials/serverless/ci-cd-github-functions)
