# ADR-0006: Focused exercise logger

- **Status:** Accepted
- **Date:** 2026-10-18

## Context

Workouts are normally short (around 4–5 exercises), so a compact list fits comfortably on a phone. However, each exercise needs enough room to adjust weight, repetitions and the number of sets, and to correct actual results. Placing all of these controls directly into the overview would make it crowded.

## Decision

Use a **compact workout exercise list** for navigation, with a **dedicated screen for each selected exercise**. That screen is the primary place to log, edit and remove individual set results and to perform fewer or more sets than planned. Show the distinction between planned sets and actual performed sets. The user may open exercises in any order and return to the list without losing input.

A normal 4–5-exercise workout is an expected UX case, **not a hard exercise limit**.

## Consequences

- The overview favors status/progress/navigation over dense editing controls; no quick-log action on the overview is required in MVP.
- A one-tap set completion action is available in the detailed screen for matching suggested values.
- Planned set count remains in the session snapshot while the actual count comes from recorded results; completing extra sets does not update the persistent template.
- Locally committed set entries and edits follow the offline operation queue protocol.

## Still open

- Next-set value prefilling; step size rules for weight controls.
- UX distinction between changing the intended number of sets and recording extra/missed actual sets.
- Confirmation, undo/removal, and correcting completed-session results.
- Optional RIR/RPE, timer and small-screen accessibility behavior.
