# Mobile PWA — UX and offline behavior (draft)

## Confirmed intent

A cross-platform mobile-first web app for iOS and Android. It is primarily a workout logger and statistics viewer. Permanent program edits are done conversationally through the AI agent.

### Three areas

1. **Workout:** today's exercise list, suggested loads and reps, previous result, large set-entry controls.
2. **History:** past sessions with per-exercise results and a way to correct errors.
3. **Statistics:** exercise progress charts and overall training summaries.

## Proposed in-gym interaction

List-first workout overview, with a focused set-entry screen when an exercise is selected. Suggested weight/reps are prefilled. A prominent **Set done** action logs locally, and quick undo is available. The order of exercises can change because equipment may be busy. A replacement exercise should not silently alter the long-term program.

The example UI discussed in chat used fictional values, not a formal interaction contract.

## Local-first write path

1. Commit set input plus outbound operation in one IndexedDB transaction.
2. Immediately show “saved on this device.”
3. Retry when connected, on app foreground/resume and on network changes.
4. Use optional browser background sync only where supported.
5. Mark “synced” after explicit server confirmation; display the number of pending operations.

Backend deduplicates client operations by stable unique IDs and detects concurrent edits. The app must not silently discard local data on conflict.

## Mobile OS constraints

Background execution is not guaranteed, especially on iOS. IndexedDB may be cleared by user action or storage eviction. Do not promise that unsynced local changes are an indefinite backup. Show important unsynced state and avoid unsafe logout/account switches.

## Statistics ideas

Working weight, reps, estimated 1RM (labeled as an estimate), volume, set count and frequency, with sensible periods and exercise/equipment filters. Exclude warm-ups where appropriate and avoid directly comparing unrelated machines.

## Decisions still needed

- Detailed interaction/state diagrams.
- Behavior when creating a session fully offline.
- Multi-device editing conflicts and recovery screen.
- Local history retention and cache.
- Default navigation and accessibility targets.
