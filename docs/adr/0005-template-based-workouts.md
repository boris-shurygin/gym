# ADR-0005: Template-based workouts in MVP

- **Status:** Accepted
- **Date:** 2026-10-18

## Context

A free-form workout builder would add exercise search, dynamic plan construction and extra offline behavior to the first version. The user expects a sufficiently varied set of named workouts (for example, leg day, back and shoulders, chest and shoulders, upper body) to cover routine flexibility.

## Decision

In the initial release a workout is **started only by selecting a template from the user's published training program**. The user can pick **any** available template rather than follow a mandatory fixed rotation. Template names, number and composition are configurable by the program/agent, not a hard-coded set of muscle-group types. Starting an empty, ad-hoc workout is out of scope for the initial release.

The agent creates and edits persistent templates through API/MCP; the backend calculates prescribed targets. Execution-time changes to recorded loads, set counts and exercise order are distinct from permanent template modifications. The precise permitted session-level substitutions/additions are still to be decided.

## Consequences

- Home screen needs template selection (and optional suggestion), followed by the chosen workout preview/start.
- The session stores the selected template and a snapshot of its plan.
- Starting offline requires a locally usable copy of the chosen template **and its backend-calculated targets**; offline plans cannot be invented by the client.
- Prefetch breadth, cache staleness and whether to schedule workouts on a calendar remain open.
- Adding free-form workout creation later is possible without changing the accepted model of template-backed sessions.
