# ADR-0009: Host Gym in Russia, prefer Yandex Cloud

- **Status:** Accepted (hosting geography and provider preference only)
- **Date:** 2026-10-19

## Context

The initial international hosting proposal (Railway in Amsterdam) does not satisfy the product owner's hosting requirements. The owner explicitly requires Russian hosting and prefers Yandex Cloud.

## Decision

- Gym's application and primary operational storage of personal workout data and backups must be **hosted in Russia**.
- **Yandex Cloud is the preferred hosting provider**, subject to confirmation of usable services, billing and operational suitability.
- **Do not** use Railway EU West or other non-Russian deployment for Gym production.

This ADR does **not** approve Compute Cloud vs Serverless Containers, self-hosted vs managed PostgreSQL, a particular VM size or billing account. See [hosting proposal](../hosting.md).

## Consequences

- Keep public application hosting, database, operational logs and backups in the Russian region.
- Choose infrastructure, identity integration and service accounts with this constraint in mind.
- Evaluate privacy law and any cross-border data flows (e.g. external identity providers or AI services) separately; physical placement alone does not establish compliance.
- Review the single-VM proposal's reliability and backup/restore trade-offs before real-user launch.

## Open details

Specific Yandex Cloud products and capacities; costs; domain/TLS; backup frequency; identity provider and legally appropriate data transfer processes.
