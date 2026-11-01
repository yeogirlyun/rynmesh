# Fork reference regression fixes — 2026-10-31

Base: `431a31dd69bb37f87f16692d924e6bbee5b0253c`, in the existing
`codex/task-change-review` worktree. No merge or shared-main-workspace edits.

## Changes

- Removed regex scanning of serialized conversations and encrypted result bodies.
  Result migration now follows structured `result_ref` / `result_id` fields with
  complete IDs. User/assistant prose, checkpoint summaries, code examples and
  tool-output text fields cannot initiate result-file reads merely by mentioning
  an ID. Explicit missing references still fail closed; nested real references
  still get authenticated, copied and resealed into child storage.
- Decode tool-call JSON arguments before checking resource capabilities, including
  dictionaries nested in arrays. Source-bound resource/directory/listing/cursor
  references reject the whole fork with `agent_fork_reference_requires_source_task`.
  Validation does not execute the recorded tool or read the referenced remote
  resource. Rejection leaves the source and task registry unchanged, including on
  repeated requests.
- Undecodable or non-object historical tool arguments fail closed with
  `agent_fork_invalid_tool_arguments`. Choose an earlier complete boundary if a
  task contains such a record. Ordinary textual tool results remain supported.

Only production file changed: `rynmesh/agent_runtime/task_forks.py`. No changes to
public runtime/control/routes, UI, compression, prompts, budgets or PTY.

## Actual validation

- Ran the supplied external `D:/code/ryn-fork-tests/review-20261031/test_fork_review_regressions.py`
  unmodified: **2 failed before the fix**, reproducing the reported 409 and 200
  responses; **2 passed after the fix** (5.72 seconds).
  Run from that external directory with `--rootdir . --confcutdir .` to avoid pytest
  searching unrelated directories across the C: / D: roots.
- Combined installed-API regression: **77 passed** (227.88 seconds):
  `test_agent_task_fork_references.py`, `test_agent_task_forks.py`,
  `test_agent_runtime_model_switch.py`, `test_agent_file_changes.py`,
  `test_agent_input_attachments.py`. This includes the original 60 cases covering
  restart recovery, interrupted creates, parent/child independence, permission
  changes, actual file writes and undo conflict protection.
- Final version of the new reference suite: **17 passed** (28.36 seconds), after
  refining the text-slot isolation and structured-reference cases. Uses real
  temporary Git workspaces, encrypted files, installed fork/results HTTP endpoints,
  deterministic model fixtures and authenticated resource tokens. Cases cover
  prose in six history locations; five nested capability fields; recursive real
  result migration/readback after removing the parent's test blobs; explicit
  missing results; invalid arguments; unchanged parent/registry; no tool replay.
- One long-basetemp attempt returned `agent_fork_storage_unavailable`; the generated
  staging filename exceeded Windows' traditional 260-character path limit. The
  final reference run used `--basetemp D:/code/ryn-fork-tests/refs2`. Storage errors
  remain reported failures, never successful partial forks.
- The initial frontend command for `RynAgentFork.test.tsx` and
  `RynAgentRecords.test.tsx` failed to start both Vitest thread workers (120 seconds,
  no tests executed). This attempt is not counted as passing. Retried each file
  separately using `node node_modules/vitest/vitest.mjs run <file> --pool=threads
  --maxWorkers=1 --no-file-parallelism --reporter=verbose`: **4 fork tests passed**
  (46.52 seconds), **9 record tests passed** (7.35 seconds).
- Ruff checks on both changed Python files passed after import cleanup; Git
  whitespace checks passed.

No actual development UI interaction, real model inference or packaged-app test
was repeated for this backend fix. The previous development UI acceptance remains
documented separately; it is not claimed as a new run. Workspace sharing and the
existing refusal to migrate source-bound external capabilities remain unchanged.
