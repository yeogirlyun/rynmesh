> 公开交付副本（2026-10-19）：保留原验收结论、失败和范围；本机绝对路径已规范化，未提交的原始证据仅在本机留存。文中的旧快照/安装包 hash 不代表本次源码；原报告 SHA-256：`0aecfb04c5dfcb3f39dbe92e478fb6ec5b26b13fa6f81044663317d8360090ca`。

# Ryn Agent core rewrite — 2026-10-18

Ryn implements its own runtime from the confirmed Codex mechanisms. This work
does not run Codex as the Agent or nest another Agent loop inside Ryn. Existing
model providers remain adapters. Existing Ryn app/task APIs, tool handlers,
permissions, encrypted state and operation receipts remain product contracts.

## Seven acceptance conditions

1. Layered instructions, response/tool dispatch, call/result writeback,
   continuation/completion, compaction and recovery follow confirmed Codex
   mechanisms, implemented and executed in Ryn.
2. Two available models from different sources complete multi-step tool tasks.
3. An existing Ryn app retains discovery, execution and result delivery through
   existing task APIs and tools; one real app workflow succeeds end to end.
4. Project investigation, an actual file edit with verification, and an app
   operation produce observable results.
5. Recoverable tool errors return to the model; steering, cancellation and
   continuation after long-context compaction work.
6. Old sessions remain readable, settings/app grants remain, and confirmed
   writes are not replayed on retry.
7. Compare a few fixed tasks on the same models before/after: completion,
   elapsed time, tool calls and manual intervention. Record regressions rather
   than promising an arbitrary improvement percentage.

Use focused existing tests plus 3–5 real tasks. No new framework, mandatory
reviewer inside the product, universal model parity, or 100% coverage gate.

## Implementation sequence

1. Replace the actual runtime request/dispatch path with Ryn-owned instruction
   layers, history normalization, response events, tool routing and explicit
   turn decisions. Preserve host execution authorization and durable receipts.
2. Wire streaming tool completion and ordered result draining; retain bounded
   adapters for non-streaming/legacy providers. Validate apps, error recovery,
   controls, compaction and state compatibility using real local IO.
3. Run fixed real-model tasks and before/after comparisons. After each completed
   development batch create a separate Codex chat using **gpt-6.1-sol, ultra**
   to independently assess all seven conditions. Repair failures and repeat;
   finish only after independent acceptance.

## Reference and change boundary

- Fixed public source: openai/codex commit
  `8c076942cb70fba8e5c4732c23f1faabc4aed484`.
- Local analysis: `Codex-Agent-完整研究方案` and its evidence/source maps in
  the originating research chat. Static, runtime and source findings retain
  their original confidence boundaries; unconfirmed desktop/server behavior
  is not presented as reproduced.
- Worktree baseline: branch `feature/ryn-integrated-workspace`, commit
  `0146c0255fb056dcbb1b4939e27cd81c05073ef7`, with 31 pre-existing changed or
  untracked files captured before this rewrite. Do not reset or clean them.
- Research workspace holds the byte-identical pre-existing file archive,
  tracked diff, manifest and a runnable before-version for comparisons.

## Progress

- [x] Capture pre-existing changes and identify current model/app contracts.
- [x] Core runtime implementation connected to production task execution.
- [x] Focused automated acceptance and compatibility checks; known baseline capacity failures recorded.
- [x] Two real model sources and 4 real tasks with comparisons (all attempts retained).
- [x] First independent gpt-6.1-sol ultra acceptance: all seven pass.
- [x] Fresh second gpt-6.1-sol ultra review: six pass; application recovery finding recorded and fixed.
- [x] Repair uncertain application responses and settle the original consumer intent from its verified receipt without dispatching a write again.
- [x] Fresh third gpt-6.1-sol ultra final verification: all seven pass, including strengthened automatic encrypted-wire recovery and subsequent Agent-task probes.

The first pre-change test run reached the end of all selected test bodies, but
pytest exited unsuccessfully while cleaning a shared Windows temp directory.
It is not recorded as a clean pass. Subsequent runs use explicit isolated
`--basetemp` directories.

The clean pre-change baseline was 118 passed. Core wiring, actual stream/control
and provider regressions have passed focused runs. The broad 464-case run found
six failures: outdated layer/router assertions, an observer continuation leak
(fixed), and two 12 KB cases independently reproduced before the rewrite because
fixed owner/permission/reference data exceed the window even with an empty
checkpoint. Neither evidence nor permissions are dropped to bypass that limit.
Final validation follows fixes rather than reusing the initial failed run.

An independent gpt-6.1-sol ultra chat (01a14dde-adda-7fd3-96c5-1c94c30cfdaf)
found malformed-history compaction and missing read correction feedback; both
were repaired with actual-loop regressions. The second independent review
(01a14def-b4b1-7073-be6c-fc26101a1727) passed six conditions but failed data/retry
safety: a committed app operation with a lost/invalid response was incorrectly
recorded as not executed; receipt reconciliation also left its consumer intent
pending. That failed report is retained without changing its conclusion.

The repair keeps all uncertain response classes unknown until the existing
authenticated receipt is found, verifies the original command/preview binding,
and settles only that existing consumer intent. It never invokes or resumes the
business operation. Missing, changed or rejected evidence cannot create an
intent or acknowledge another command. New regressions cover all three response
classes, restart, repeated confirmation, the next legitimate operation and a
known precondition failure that really was not executed. A third fresh review
(01a14dfb-685d-7083-94ea-b3e1b3dc636f) completed at 2026-10-18T15:56:06+08:00:
all seven PASS, no open substantive finding. It independently ran 141 key
regressions, 24 new binding/receipt probes and actual encrypted-wire fault
recovery, with an original task and a subsequent fresh-approved task completing.
An additional overlapping 28-case audit is not added to claim unique coverage.
No further paid inference was used. All 295 production Python files were stable
throughout this review; the complete sorted source/hash manifest fingerprint is
`b8b267ccef9f16debecb5f0c71dc38ef0be0fc8e3bd21340a7d7c005e984830c`.
See `INDEPENDENT-REVIEW-003.md` for the signed scope and limitations. Source
changes are ready in this worktree; the running installed app was not replaced.
