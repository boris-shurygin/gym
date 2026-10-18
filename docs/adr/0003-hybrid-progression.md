# ADR-0003: Hybrid progression

- **Status:** Accepted
- **Date:** 2026-10-18

## Context

The agent should be able to adapt long-term training while ordinary workouts do not require a conversation or model call. Calculations should be reproducible and explainable.

## Decision

The AI agent sets and revises progression rules and may propose program edits; the backend deterministically computes the concrete planned loads and targets from those rules and workout history. Record version information sufficient to explain a recommendation. Keep the workout plan distinct from actual results.

## Consequences

- Rules must have a typed/versioned schema and server-side validation.
- Computed progression state should be reproducible from authoritative information.
- Session snapshots prevent program changes from retroactively rewriting workouts.
- Agent-specific heuristic reasoning stays separate from the calculation engine.

## Not decided

Available rule types, exact failure/recovery behavior, deload handling and per-session overrides.
