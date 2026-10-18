# Instructions for development agents

This repository is currently in the **design phase**.

## Source of truth

- `docs/adr/` records architectural decisions accepted by the user.
- Other files in `docs/` are evolving specifications. Distinguish confirmed requirements from proposals and open questions.
- `docs/open-questions.md` tracks unresolved choices.
- Do not treat sample schemas, example workloads or suggested technologies as approved designs.

## Workflow

1. Read README.md and the relevant docs before proposing code or architecture.
2. Before changing an accepted decision, discuss trade-offs and record the superseding decision in an ADR.
3. Prefer small reviewable changes, with related documentation updates.
4. Do not start application implementation until the user asks for it; do not finalize a technical stack by assumption.
5. Explicitly document new assumptions, missing invariants and operational risks.
6. Prefer tests for progression, authorization, synchronization/idempotency and historical corrections when implementation begins.

## Core product rules

- Personal programs, workouts and analytics belong to individual users. Authenticate and authorize every operation server-side.
- Logging sets must work offline; background sync is best-effort, never a correctness dependency.
- The server is authoritative for the published program and progression calculations.
- Separate planned targets from performed results. Updating a program must not rewrite historical workouts.
- Agent writes go through application services and validation, with explicit approval for program publication.
- Preserve a trail of revisions and corrections.

## Terminology

`AGENTS.md` guides **development agents**. A future runtime **training-agent Skill** describes how an AI coach should use MCP tools; these are different things.
