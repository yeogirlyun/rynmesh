# Agent storage capacity, phase 1 — 2026-11-06

## Problem

A Codex/GPT task on the owner's desktop failed with `agent_storage_limit`
(task `agent_cc3ad368…`, model `local/codex-cli/model/gpt-6.1-sol`). Its events
show the streamed `ryn_plugin_info` read finishing, then the step's commit
failing; the UI showed Ryn's internal English note "Recovered completed read
calls from a partial model response…" as if the model had said it.

Cause: all active (non-archived) Agent tasks share one encrypted store capped
at 24 MiB of decrypted state (`store.MAX_PLAINTEXT`), and a tool result was
kept up to four times (tool message, receipt, `tool_result` event, and the
streamed `tool_execution_completed` event). The cap is a speed guard: every
read and commit copies the whole active state, so it is not simply raised here.
Phase 2 (load and copy only the changed task) is what removes the total cap.

## Change (phase 1)

- `storage_relief.py` + `AgentRuntime._relieve_storage`: when the active store
  passes 16 MiB or 190 tasks, the least recently updated finished tasks move to
  the existing per-task archives until it is at or below 12 MiB / 180 tasks.
  Never archived: the 10 newest conversations, running or waiting tasks, tasks
  an active automation targets or an unsettled automation run uses, parents
  and children of subagents. One commit archives the whole batch.
  Triggers: idle worker (30 s after start, then at most once a minute, on its
  own thread), before each step reserves output, before create / fork /
  restore. If a model step's commit still fails with `agent_storage_limit`,
  Ryn archives and retries that step once from its recorded evidence.
- Archived rows carry `archive_reason: "storage"` and an `archived` event with
  `reason`; the task page explains it and offers Restore.
- `protocol.event`: a `tool_result` / `tool_execution_completed` result of
  1 KiB or more that is identical to its receipt's result is stored as its
  short scalar fields plus a marker. `public_task`, the events endpoint and the
  two backend readers that use nested fields (`loop` compaction receipts,
  subagent timeline) read the full value from the receipt.
- UI: `agent_storage_limit` has its own message; the recovered-partial-reply
  note (new `ui.kind: partial_recovery`, or the legacy text) is shown in the
  owner's language.

No on-disk format change and no migration. Older versions read every new
record: extra archive/index fields are ignored and compact events still carry
`error` / `path` / `status`.

## Verification (VM, no real model)

- New tests: `tests/test_agent_storage_relief.py` (8),
  `webapp/src/screens/RynAgentStorageNotes.test.tsx`, a case in
  `RynAgentRecords.test.tsx`.
- Upgrade and rollback on one store (old code → new code → old code): old
  tasks open with full evidence after upgrade; the new task's event is compact
  and shown in full; 5 tasks auto-archived; the old code then lists 10 active +
  5 archived, opens all, restores an auto-archived task and runs a new task.
- Near-full store (125 tasks, 22.8 MiB): first relief archived 60 tasks in
  2.2 s and left 11.9 MiB; one commit went from 0.22 s to 0.12 s.
- Simulated four-call task: 200.8 KB → 153.3 KB stored (−24%).
- Full suite and pre-existing failures: see "Test results" below.

## Test results

- `tests/test_agent_storage_relief.py` + `tests/test_agent_runtime_lifecycle.py`: 14 passed.
- Store, streamed-read, retry, runtime, subagent and evidence suites: the only
  failures (4 + 1 error) fail identically on the unmodified base `e81e26e`
  (`agent_tool_schema_limit` / context-budget expectations).
- `tsc -p tsconfig.app.json --noEmit`: clean. Vitest (serial):
  `RynAgentStorageNotes`, `RynAgentRecords`, `RynAgentProgress`, `RynAgentStream`,
  `RynAgentNavigationSending`, `domain/rynAgent` pass; one 5 s typing timeout in
  `RynAgentConversationControls` also times out (twice) on the base.
- The full `tests/test_agent*.py` run was still in progress at merge time; its
  comparison against the base is recorded in the follow-up commit.

## Not verified here

A real Codex/GPT task on the owner's desktop, and the colleague's separate
`agent_inference_failed` (their `get-task-failure.ps1` output is still needed).
