# Hosting and deployment — proposal

**Status: Proposed / not accepted.** Last reviewed: 2026-10-19. No live infrastructure has been provisioned.

## Purpose

Host a small, multi-user, offline-first workout PWA with a REST API, a planned remote MCP adapter, and PostgreSQL. Optimize for simple operation, reproducibility and data durability rather than premature scaling.

## Recommended MVP topology (to approve)

- **Railway Hobby**: one long-running **FastAPI** service, in the **EU West (Amsterdam)** region.
- The same app origin serves production-built **React/TypeScript PWA** assets at `/`, authenticated REST routes at `/api/v1`, and remote MCP tools at `/mcp` (only after they are implemented and tested).
- A **PostgreSQL** service in the same Railway project/environment, reachable only through private networking. It is the source of truth; never expose its public TCP proxy unless explicitly required for administrative access.
- Source of application truth: this GitHub repo. GitHub Actions checks on PR; approved changes to `main` deploy to Railway, with migrations and health checks. Keep production secrets in Railway variables, not in Git.
- First address: platform-provided HTTPS domain. Use a custom domain later, without redesigning the app.

The production app is **not** the existing `prototype/index.html`; that prototype is a static in-memory simulation. As an optional short-term step, publish the existing public prototype with GitHub Pages for review, keeping it visibly separate from the real app.

### Why start with one application origin?

Simpler TLS, cookie/session handling, URL routing, CI/deploys and no cross-origin PWA/API calls. The frontend may later move to a static CDN without changing business logic.

## Authentication, authorizations and privacy

- Use an established identity provider; do **not** implement a password database from scratch. Exact provider and sign-in methods (email magic link, social login) are still open.
- For the PWA, favor a secure HttpOnly cookie session with CSRF protections and explicit user/device isolation. Never include secrets in the public JS bundle.
- Each operation in the backend derives its owner from verified identity; do not trust arbitrary user IDs.
- MCP needs **its own scoped, delegated OAuth** integration suitable for remote clients. Authenticate every tool call and require explicit consent for published program changes.
- Important: as of 2026-10-19, ChatGPT documentation describes full custom MCP write/modify support for **Business / Enterprise / Edu** workspaces; do not assume Plus/Pro ChatGPT clients can edit programs through this MCP server. Validate intended accounts/client capability before relying on agent-driven modifications. Build the common API independently so an alternate capable MCP client or application-based authoring can be integrated later.
- The service should follow EU privacy/data deletion principles for users, with encrypted transport and appropriate provider terms.

## Data safety and offline requirements

- Persist training data in PostgreSQL, not an ephemeral application container filesystem.
- Enable Railway volume backups and a separate periodic `pg_dump` export kept offsite; test restoring a dump into an isolated database. Consider PITR later if warranted.
- The mobile PWA must save each set and its outbound operation atomically in IndexedDB, sync idempotently upon reconnection, and visibly distinguish locally-saved from server-confirmed.
- Avoid deployments that discard pending changes in a running local PWA; version cached assets carefully and test app updates/offline restart.
- Keep test/staging data separate from production users. Logs must not expose tokens or private workout details.

## Initial delivery sequence

1. **Technical skeleton**: choose stack, create API/SPA layout, Dockerfile, local Compose+Postgres, test suite and a health endpoint.
2. **Authoritative backend**: user identity/ownership, migrations, program/template versions, session snapshots, set writes and server-side progression rules.
3. **Working PWA**: migrate prototype UI into React/TypeScript, implement durable IndexedDB/outbox, installability, correct active-session resumption and historical corrections.
4. **First hosted environment**: Railway service + private PostgreSQL + HTTPS, secrets, backup/restore check, smoke tests from iOS and Android.
5. **Agent bridge**: add MCP adapter using the same services; validate remote OAuth and client-specific read/write availability separately before promising ChatGPT agent edits.
6. **Small pilot**: invite friends, monitor logs/errors, exercise offline recovery and cross-device conflicts, review cost and restore reliability.

A lower-effort alternative for static UI preview is GitHub Pages; **it cannot replace** the authenticated backend, database or remote MCP endpoint.

## Costs (indicative, not a quote)

Railway Hobby starts at **US$5/month** with **US$5 included resource usage**. Additional billing depends on CPU, RAM, disk, backups and outbound traffic. A preliminary small-instance budget for API + always-on Postgres is **US$10–25/month** before optional paid identity, domain, email, and AI API costs. Verify using live billing after deployment rather than assuming a guaranteed minimum.

## Alternative worth reconsidering

**Render** can run an API and managed PostgreSQL in **Frankfurt**, with separately priced services and static hosting. It is a reasonable choice if German region is preferable to Amsterdam or if the operator values its managed database setup more than keeping costs low.

## Open decisions

1. Accept Railway/EU West or select another host; allocate hosting accounts and billing.
2. Exact auth provider, user-invite onboarding and recovery mechanism; MCP OAuth provider/scopes.
3. Whether to deploy static frontend separately later.
4. Backups/retention/privacy policy and alerting.
5. ChatGPT plan/client capabilities for remote MCP modifications; fallback agent workflow if unavailable.

### Documentation reviewed

- [Railway deployment regions](https://docs.railway.com/deployments/regions)
- [Railway pricing](https://docs.railway.com/pricing)
- [Railway PostgreSQL](https://docs.railway.com/databases/postgresql)
- [Railway Postgres backups](https://docs.railway.com/guides/postgres-backups-restores)
- [Render regions](https://render.com/docs/regions)
- [ChatGPT custom MCP server availability](https://help.openai.com/en/articles/12584461-connecting-custom-mcp-servers-in-chatgpt)
