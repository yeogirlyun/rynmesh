# Ryn Agent streaming and per-turn duration — 2026-10-17

## Changes

- The open executing task polls sequentially at 120 ms instead of 1,200 ms. A transition from an idle task to an accepted follow-up immediately wakes this loop; hidden tabs and waiting tasks retain slower polling.
- Provider callbacks update an in-memory `streaming` projection immediately. Unchanged task responses include it without rewriting encrypted state or downloading the transcript. Existing durable text checkpoints remain batched for recovery.
- Equal-revision reads from focus/selection cannot replace newer append-only stream text or roll back the elapsed clock.
- Initial text coalesces for 80 ms. Bursts render in short frames and catch up with received text within about 180 ms. Completed text flushes immediately; reduced-motion preference skips animation; changing runs isolates pending text.
- Each user turn has a runtime label before its reply: running, queued, paused, waiting for approval/input, or final duration. New events persist `run_elapsed_seconds`; `run_settled` records final accounting when an in-flight operation finishes stopping. Neither field changes execution authority.
- Historical records without counters use explicitly approximate measured activity intervals, excluding input/approval waits and overlapping tool intervals. Unattributable records display unavailable instead of an invented duration.

## Validation

- Backend streaming/runtime/timing/control/deadline/speedup/integration regression batch: 172 passed.
- Additional stopped-operation settlement/control/completion regression batch: 73 passed (overlaps the batch above).
- Browser component preview checked both running and completed Chinese labels. `duration-preview.png` uses an isolated component fixture with synthetic disk values; it is not a screenshot of a newly installed client or a new disk diagnosis.
- Final frontend regression: 127 passed across domain, conversation, send/recovery/navigation, stream presentation and duration suites (32 + 95 tests, sequential workers).
- Production build (`npm run build`, including TypeScript): passed. Existing bundle-size and mixed notification-import warnings remain.
- Scoped `git diff HEAD --check`: passed.

## Scope

Source and web production bundle are updated. This work does not install a desktop release or restart the user's current client. Model-side inference latency remains dependent on its provider; the change removes the app's avoidable buffering and refresh delays.
