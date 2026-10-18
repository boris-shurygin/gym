# Requirements — working draft

## Product goal

Track progress in strength training while minimizing interaction during workouts. Use an AI agent for conversational planning, analysis and program modification, and a button-first mobile app for workout logging.

## Confirmed requirements

- Multi-user service: every person has their own account and private history.
- Support both iOS/iPhone and Android.
- Poor or absent connectivity must not block recording a set.
- The mobile UI supports active workouts, past sessions, statistics and graphs.
- A personal AI agent modifies programs via API/MCP; a full visual program editor can wait.
- Hybrid progression: agent defines/revises rules; backend calculates prescribed loads deterministically.
- Plans and actual workout results are distinct. Program changes do not rewrite earlier results.
- REST/API and MCP are first-class interfaces to common application logic.
- MVP workouts start from a **user-selectable template in the current program** (e.g. leg day, back and shoulders, chest and shoulders, upper body). Free-form/empty workout creation is excluded. Template names and number are user/program-defined; no fixed rotation is required.
- During a workout, use a compact list (typically 4–5 exercises, without a hard limit) and a **dedicated exercise detail screen** to log/correct weights, repetitions and sets. Actual performed set count can differ from planned set count without rewriting the program.
- Each new working set defaults to **planned** weight/repetitions, not the last completed actual set. Display each working set's **volume (weight × repetitions)** compared to its planned volume, including shortfall and overflow beyond 100%, plus a separate planned-vs-actual working-set count. Show actual weight and repetitions alongside the volume bar; the bar is not a strength/progression verdict.

## Candidate MVP capabilities (not yet approved in detail)

- Sign-in and individual program; one active program at a time.
- Choose any template from the current program; start/resume/finish a session; record, undo and correct a set; warm-up vs working sets.
- Weight and repetitions prefilled, adjusted with large buttons.
- See previous results; reorder or substitute an exercise for an active session.
- Queue offline edits and show explicit sync status.
- History; exercise charts for weight, reps, volume and estimated 1RM; weekly training frequency.
- Agent can read program/history, draft and validate a change, and apply approved changes.
- Initial rule types: fixed, double progression, explicit/manual target.

## Quality attributes

- **Usability:** very few taps per set; text/voice input not required in the gym.
- **Durability:** record saved locally before UI acknowledges it; sync retries are safe.
- **Security:** separate ownership, server-side access checks; never trust user_id in a client payload.
- **Auditability:** versions and corrections are traceable.
- **Portability:** mobile-first installable PWA where supported.

## Not in initial scope

Free-form/empty workout creation, social feed, leaderboards, shared sessions, shared programs, native applications, advanced desktop editor or local AI inference.

## To decide

Exact MVP priority, account provider, sync conflict semantics, progression rule details and technology selection. See [open questions](open-questions.md).
