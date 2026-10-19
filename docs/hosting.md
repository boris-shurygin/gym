# Hosting and deployment — Russia, Yandex Cloud proposal

**Status:** Russian hosting is **required** (ADR-0009); **Yandex Cloud is preferred**. The VM-based topology and exact products are **proposed**, not deployed or accepted as a final infrastructure specification.

**Reviewed:** 2026-10-19.

## Requirements and provider preference

- The Gym application, its operational database, user-associated logs and backups should reside in the **Russian region**, not in a European Yandex Cloud zone or an overseas provider.
- Prefer **Yandex Cloud**, using region `ru-central1`; `ru-central1-d` is a reasonable initial zone (Yandex currently recommends it for new projects).
- This geography requirement does not imply that every third-party integration or data export is lawful; review applicable personal-data and cross-border-transfer obligations (including Russian 152-FZ if applicable) before inviting real users.
- Keep GitHub as the source code repository. Do not store private workout data, access tokens or database backups in Git.

## Recommended MVP: one VM, three containers (proposed)

**Yandex Compute Cloud**, one Ubuntu Linux VM with a static public IP and persistent SSD-backed disk(s), in `ru-central1`:

| Component | Implementation | Access |
| --- | --- | --- |
| HTTPS / frontend | Caddy (or nginx) and the production-built React/TypeScript PWA | public 443 |
| API + MCP | FastAPI process/container with `/api/v1`, eventual `/mcp` and `/health` | internal reverse-proxy upstream only |
| Database | PostgreSQL in Docker with its own persistent volume/disk | **private Docker network only**, never published publicly |
| Database backups | scheduled `pg_dump` + encryption + upload to private Yandex Object Storage bucket in Russia | private service account privileges |

Provision with Docker Compose, deployment scripts and (optionally) Terraform committed in the repo. Only the ingress web service exposes ports 80/443; enable a host firewall/security groups; SSH access should be limited and key-only, ideally via an operator-approved secure path. Production credentials are supplied through secured cloud/runtime configuration and never exposed to the frontend.

The domain should use HTTPS (a custom domain is easiest for predictable PWA and OAuth callbacks). Caddy can obtain and renew Let's Encrypt certificates once DNS points to the reserved VM address. A dedicated Yandex Application Load Balancer and Certificate Manager are unnecessary for a single-VM MVP.

### Initial capacity to test

- **Proposed:** 2 vCPU, **4 GB RAM**, 20–30 GB SSD, Ubuntu LTS. Add a separate persistent SSD data disk for PostgreSQL if doing so improves recoverability and allows replacing the application VM independently.
- **Lower-cost test only:** Yandex's preconfigured 2 vCPU / 2 GB example is less comfortable when Postgres and a web API run together.
- This is capacity planning, not a guaranteed benchmark; measure actual memory pressure, disk usage, concurrent sessions and sync spikes before sizing production.
- No load balancer, Kubernetes, container orchestration service or managed database is needed for the initial pilot.

## Why self-hosted PostgreSQL first?

At tiny pilot scale it avoids a second, separately priced always-on managed service. In exchange, **we** own updates, database maintenance, monitoring, point-in-time availability constraints, backup and restore. A single VM remains a single failure domain. The normal offline-first app can keep logging during server downtime, but those records remain unsynced until the backend is restored.

Move PostgreSQL to **Yandex Managed Service for PostgreSQL** (with a private network connection) if backup/SLA/maintenance needs outweigh the added price. Evaluate real managed-service prices using the calculator; a multi-host configuration is substantially more expensive than this first VM.

## Data durability, backups and upgrades

1. Database data lives on persistent disk rather than an ephemeral container layer.
2. Run **daily** application-consistent PostgreSQL dumps to a **private Object Storage bucket in Yandex's Russian region**. Set a reasonable initial retention (e.g. 14–30 days), rotate backups, encrypt them and grant the upload service account minimal permissions.
3. Optionally take scheduled VM/data-disk snapshots, but do **not** treat a raw online database disk snapshot as a substitute for a consistent PostgreSQL backup.
4. Test restore into an isolated VM/DB and validate exercise/workout counts before accepting real users.
5. Maintain an explicit RPO (daily backups can lose up to roughly 24 hours of server-confirmed changes) and recovery procedure; improve backup frequency if actual use requires it.
6. Version PWA assets and service worker carefully, ensuring client IndexedDB outbox migrations do not discard pending entries.
7. Use separate staging data or a separate environment for changes that may affect the database schema.

## CI, deploy, and ops

- GitHub Actions on PR: lint, type checks, tests, build frontend/backend images.
- Controlled deployment on an approved merge to `main`: transfer a versioned container image to the VM through an authenticated deployment channel (or pull from an approved registry), run tested schema migrations, health-check and restart services. Avoid baking secrets into the image.
- Verify reachability from the intended operator's network and from Russian mobile networks. GitHub Actions and any international package registries must be tested for actual connectivity from the VM.
- Enable health/status checks, free disk/RAM monitoring and backup success/failure alerts.
- Set **Yandex Cloud Billing budget alerts**. Note: budget notifications do **not** automatically stop resource consumption.

## Costs — preliminary, not a quote

The published Yandex Compute Cloud examples for Russian region include:
- roughly **1,659 ₽/month** for a minimal 2 vCPU / 2 GB with HDD and a dynamic IP;
- roughly **2,513 ₽/month** for a 2 vCPU / 2 GB with SSD and a dynamic IP.

Our suggested **2 vCPU / 4 GB + SSD**, plus a reserved static IP, backup storage and related services, will cost more. For a small pilot, use a **3,000–5,000 ₽/month planning envelope**, *not* a guaranteed tariff. Confirm the exact bill with the live Yandex Cloud calculator before provisioning. Domain/identity provider, extra backup retention, disk snapshots, email, outbound traffic, and AI/API calls may add costs.

Object Storage has a small standard-storage free tier (first 1 GB and included operations) and usage-based storage/operation pricing. Compute Cloud outbound traffic has a published included monthly allowance, subject to provider rules.

## Rollout plan

1. Confirm Yandex Cloud billing account, Russian region, domain and identity provider; keep secrets out of chat/repo.
2. Scaffold Docker Compose with the frontend build, API, PostgreSQL, reverse proxy and initial migration tool.
3. Run smoke tests locally; provision test VM + static IP + private Object Storage backups in Yandex Cloud.
4. Connect GitHub Actions, HTTPS/domain, uptime/error/disk monitoring, restore procedure and budget alerts.
5. Convert prototype functionality to production React/TypeScript PWA + FastAPI REST + IndexedDB outbox and verify offline behavior on iPhone and Android.
6. Add progression services and MCP adapter; validate client-specific authentication/write permissions separately.
7. Invite a few users after verifying multi-tenant authorization, data retention, backups and actual connectivity.

## Pending decisions

- Approve the specific Yandex Cloud VM-based layout (provider preference and Russian location already captured).
- Exact VM size and disks after calculator quote.
- Domain name, signup/identity provider, where user identity data is stored and legal data flows.
- Whether to add shorter backup RPO, snapshots or a managed database.
- CI registry/artifact route and monitoring tools.

## Official provider documentation (reviewed 2026-10-19)

- [Yandex Cloud regions](https://yandex.cloud/ru/docs/overview/concepts/region)
- [Availability zones](https://yandex.cloud/ru/docs/overview/concepts/geo-scope)
- [Compute Cloud preconfigured prices](https://yandex.cloud/ru/services/compute)
- [Compute Cloud prices](https://yandex.cloud/ru/docs/compute/pricing)
- [Static public IPv4](https://yandex.cloud/ru/docs/compute/operations/vm-control/vm-set-static-ip)
- [Managed PostgreSQL prices](https://yandex.cloud/ru/docs/managed-postgresql/pricing)
- [Object Storage prices](https://yandex.cloud/ru/docs/storage/pricing)
- [Disk snapshots and consistency](https://yandex.cloud/ru/docs/compute/concepts/snapshot)
- [Cloud billing budgets](https://yandex.cloud/ru/docs/billing/concepts/budget)
