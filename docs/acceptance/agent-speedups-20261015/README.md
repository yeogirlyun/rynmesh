# Agent step latency — 2026-10-15

Recorded tasks showed where an Agent step's time went (see `step_gaps.py`):
Codex CLI model calls took a median of 17 s per step, and the runtime added a
median of 1.2–6.4 s between a tool result and the next model call. Tools
themselves took under 1 s. This change addresses the runtime side. The Codex
adapter is handled separately (`../codex-session-reuse-20261015`).

## Changes

| Area | Before | Now |
| --- | --- | --- |
| Core tools (`tool_menu.py`) | 47 schemas exceed 8 KB, so directory mode applied. Only shell/read/search were preloaded, and each `load` replaced the previous set. | `CORE_TOOLS` (shell, read, search, apply_patch, edit, write, list, process read, plus project inspect for projects) are exposed on every step while they fit. `load` adds to the set, `unload` removes, and the least recently loaded schemas are released when space runs out. |
| Editing (`local_patch.py`, `ryn_apply_patch`) | Single exact replacement with a fresh `expected_sha256`. Writes limited to 16 000 characters. | Codex-style multi-file patch: add, update with context hunks, move, delete. All hunks are resolved before anything is written. CRLF, BOM and the final-newline convention are kept. Missing parents are created inside the workspace. The preview is bounded, and execution compares it again. Runs without a dialog in `workspace` mode. |
| Context budget | The 120 000-character ceiling applied even to large-window models. | 600 000-character ceiling. A model's declared window still bounds it, and models without a declared window keep 120 000. Old saved defaults of 120 000 are migrated once; values set explicitly later stay. |
| Scheduling (`run_continuously`) | Fixed 0.5 s worker interval between every model/tool step. | A worker keeps running a task's queued steps for up to 20 s. A failed step (including a scheduled retry) returns to the normal interval. `run_once` is still exactly one step. |
| Reads in one response (`_tool_batch`) | One per tick, serially. | Up to 8 consecutive local reads (file read, list, search, process read, project inspect, environment) run concurrently. They take two transactions in total, and receipts match the one-call path. Anything else falls back to the serial path. |
| Command output | First 6 000 characters only. | A finished command over 6 000 characters shows its first 2 000 and last 4 000. The middle is pageable from `next_offset`. |
| State storage (`store.py`) | One encrypted file holding every active task, re-encoded, encrypted and written on every event. Two full `deepcopy` calls per transaction. | Encrypted `sessions.json` root committing one encrypted file per task (`tasks/`) by sha256. Only changed tasks are rewritten, then the root. Copies use pickle. The single-file layout is read and converted on first write. Earlier builds refuse the v2 root instead of reading it as empty. |
| Idle and UI reads | Every idle worker tick copied all state twice. The UI downloaded the full task (every message and event) every 1.2 s. | Idle checks use `AgentStore.inspect` (no copy). Task summaries copy only summary fields. `GET /tasks/{id}?since_revision=N` returns only live fields when nothing changed, and the UI merges them into its cached copy. |

## Measured here (synthetic data, this VM)

`profile_store.py 20`: about 6.8–9 MB of plaintext state, one event on a small live task.

| | Before | After |
| --- | --- | --- |
| One-event transaction | 414–419 ms | 56–60 ms |

The earlier synthetic benchmark at about 16 MB of state measured 340 ms per transaction before the change and 22 ms after.

No live model latency was measured on this machine: Codex is not installed
here. See `CODEX_TEST_PROMPT.md` for the live measurement on a Codex machine.

## Tests run here (VirtualBox shared folder, so pytest selects by `-k`)

| Selection | Result |
| --- | --- |
| New: `test_agent_apply_patch`, `test_agent_speedups`, `test_agent_store_layout` | All pass |
| Runtime, lifecycle, results, capacity, control, retry, verification, tool menu, codex loop, projects, output policy, checkpoints, default workspace, compaction, long resources, timing, latency, reliability, cancellation, approvals, local tools, integrations, image input, native scope | 532 passed, 10 skipped. 5 failed in `test_agent_run_timing.py`: these predate this change, because the tests patch `runtime.time` while the step code now lives in `loop.py`. |
| `test_agent_reference_*` (pinned zcode reference runtime) | 47 passed. 12 failed at the first message to the external reference worker (broken pipe), before Ryn's runtime is involved. Not diagnosed here; rerun on the host. |
| Codex reuse (`test_codex_thread_reuse`, `test_inference_privacy`) with the window wiring | Pass |
| Frontend `tsc -b --noEmit` | Pass |
| Vitest: `RynAgent`, `rynAgent` (domain), `RynAgentSending`, `RynAgentArtifacts` | 62, 6, 23 and 15 passed. Each file was run singly; this VM times out starting several vitest workers at once. |

Browser and Windows accessibility tests were excluded: Chromium and a focusable desktop are not available here.

During development, the pinned core tools first squeezed 12 KB test windows and cost extra compaction calls. Pins now use at most a tenth of the window and yield under context pressure. Loads also accumulate only with 48 KB or more of schema space; smaller windows still replace the loaded set.

## Downgrade note

After the first write, the state is in layout v2. A build from before this
change reports `agent_state_version_unsupported` for the Agent and keeps the
files. It does not read or overwrite them.

## Follow-up after host acceptance (`live-results.md`)

The host run found that the system prompt added the operation-permission guidance only once a write
receipt existed, and dropped it again after a follow-up. That changed the prompt's prose, so the Codex
thread was reseeded after the first write and on a follow-up in the same window (scenario F).
The guidance is now always included. Only the JSON state line changes, and the thread continues
(`tests/test_agent_speedups.py::test_system_prompt_changes_only_in_state_lines_after_a_write`).
The host retest passed all three targeted scenarios C/F/B and all 80 selected
automated tests. C continued after shell/patch results, F's first call continued
the same thread, and B continued after its shell result. F's first adapter call
took 5.725 s versus C's 13.737 s cold first call (58.3% lower). The 10 scenario
calls recorded 2 starts, 8 resumes, no fallback and no prose changes.
See [the P2 retest in section 7](live-results.md#7-p2-复测) for all timings and
the original broad-suite failures; this is not a full-suite-green claim.
