# Interactive mobile UX prototype (v0.2)

Open **[index.html](index.html)** in a modern browser (locally or via VS Code Live Preview/Live Server). It is a self-contained HTML/CSS/JS prototype with no build dependencies or external requests.

## Screens and clickable interactions

- Workout home and selection among program templates (Chest & Shoulders, Legs, Back & Shoulders, Upper).
- Preview a program template and start the workout.
- Active workout with a list of 4–5 exercises, exercise detail and planned targets.
- Log a set, adjust weight/reps with readable decimal values, see **per-set tonnage (weight × reps) progress bars**, edit/remove recorded sets, and switch an already logged set between **working** and **warm-up**. Only working sets count toward planned working-set totals. New sets default to **planned**, not previous actual values.
- Complete/review a partial workout, then inspect it in History.
- History details: **tap a full-width set row to open the same detailed exercise editor**, or add a new set to a completed workout. No tiny pencil controls or cramped editing modal.
- Statistics: exercise selector, volume/working-weight/estimated-1RM graph, period filters and tap-to-inspect data points.
- Settings: simulate offline, sync queue, switching between two isolated demo accounts.

## Fixes in v0.2

- Numeric weight input uses unlocalized decimal values for HTML number fields, eliminating disappearing values at fractional weights (e.g. 62.5 kg). Localized formatting is retained for display labels.
- Set-type selection is available when **creating and editing** sets in active or completed workouts.
- Warm-ups are excluded when counting/labeling planned working sets; e.g. 1 warm-up + 3 working sets satisfies a 3-working-set target, while a fourth working set is extra.
- Completed workout details have large tappable rows and use the same full-screen editor, supporting correction, deletion and addition of sets.

## Limitations

- Pure **UX mockup**, not the actual PWA or backend.
- All workouts, names, dates, program targets, stats and calculations are fictional examples.
- The local-state/session/sync behaviors are **simulations in memory only**: no actual IndexedDB, authentication, MCP, background synchronization, networking or real-data persistence.
- Reloading the page resets the demo.
- Some detailed UX decisions remain open for review; see [mobile app specification](../docs/mobile-app.md).

## Suggested review

Start a workout, adjust a set above/below plan, log several sets, correct one, switch exercise, finish with incomplete exercises, inspect History/Statistics, then try the offline toggle in Settings.
