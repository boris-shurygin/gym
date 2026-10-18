# ADR-0001: Agent-oriented service boundaries

- **Status:** Accepted
- **Date:** 2026-10-18

## Context

Program editing should happen through an AI agent rather than through a complex program editor in the mobile app. The workout app still needs reliable normal application operations.

## Decision

Build an application backend with a first-class API and an MCP adapter. An agent can read history and edit programs through validated domain operations. A runtime Skill will specify agent procedures. The mobile UI and MCP adapter share backend business logic and authorization, rather than independently implementing program rules.

## Consequences

- Agent-based planning and execution are decoupled.
- We can add desktop or alternative clients later.
- Security, audit and version checks belong in application services.
- We must define stable, task-oriented MCP and REST contracts.

## Not decided

Hosting, detailed protocols, implementation stack and specific MCP client permissions.
