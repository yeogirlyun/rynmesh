# Shared Agent reliability acceptance — 2026-11-12

Base: `305f474299f918c0c6634a60f7072708d6ae15ca` (`develop`).
Implementation branch: `codex/agent-reliability-20261112`.
Local worktree: `/Volumes/ExternalSSD/rynmesh-reviews/agent-clawpad-review-20261112`.
Scope/design: [shared Agent reliability](../AGENT_RELIABILITY_20261112.md).

## Executed locally

- Python 3.13/macOS, isolated pytest directories and operation ledgers; no live
  owner workspace, company records, runtime restart or model quota was used.
- Related regression suite: **370 passed, 3 skipped, 2 deselected**, 40.36 seconds.
  Covered long tasks/conversations, integration continuation, tool errors, unknown
  writes, controls, budgets, model switches, forks/references, local/NAS/peer
  scopes, bounded reads, workspace artifacts, local tools/execution reliability,
  table pipeline and business/runtime/feedback verification. The excluded tests
  were the actual browser DOM and Windows accessibility tests; platform skips
  remain visible in pytest output.
- Final focused CI-equivalent suite after preview/origin and platform-test changes: **118 passed,
  1 skipped**. The first Linux CI run exposed an existing file-reveal test that
  incorrectly required a file path when Linux opens its containing folder. The
  test now exercises mocked Linux/macOS dispatch and asserts exact platform
  commands, without changing application behavior or opening a native window.
- The first broader webapp CI run had one failure (1,439 passed): an unchanged
  storage-note test still expected fixed-capacity wording. Source inspection
  confirms `AgentStore._require_disk` checks actual free disk space, and the
  current UI already explains that correctly. Updated the test to require the
  disk-space guidance and conversation-preservation message; no UI/runtime
  storage policy was changed.
- Storage/error-help tests: **4 passed** locally; typecheck and production web
  build passed. The broader local frontend run under Node 25 (native webstorage
  disabled for jsdom compatibility) had **1,435 passed, 5 failed** in unchanged
  InviteDeepLinks, PrivateAIChat and RynAgentSending tests, including three
  timeouts. These are not a green full-frontend result. Linux CI uses Node 22;
  its first run's only failure was the stale storage-note assertion above.
- The focused Linux/Python 3.12 CI job passed **118 tests, 1 skip**, plus changed
  Python-file lint, on `806332dd`. Later fixture reuse is module-local, avoiding
  global pytest plugin registration. Local focused revalidation: **118 passed,
  1 skipped**. See PR #130 for final commit/check status.
- New tests exercise three successive automatic rollovers and restart; retained
  language/skills/workspace and cumulative owner budget; exhausted-budget refusal;
  atomic failure/no orphan child; duplicate request reuse; unknown-write refusal;
  superseded-source controls and retained resource-origin validation.
- Missing-file tests exercise the recorded PowerShell loop and a POSIX equivalent
  at the host boundary, before preview/approval; real filesystem recovery on a
  later bounded attempt; shared earlier-attempt accounting and cancellation.
- Tables exercise Decimal results, explicit date/rounding rules, aliases/Chinese
  identifiers, missing/invalid inputs, failed checks, expression rejection,
  deterministic/readable XLSX, precision limits, CSV formula protection, source
  changes before/after approval, workspace escape and existing-file refusal.
- An integration test uses real tool discovery, consumer file operations,
  approval, ledger, readback, mechanical verification and artifact download
  listing with a **scripted provider boundary**. It is not a live model-quality
  evaluation. `openpyxl` independently reopens the workbook, including numeric
  values and Data/Checks/Provenance sheets.
- Changed Python files pass Ruff; `git diff --check` passes. Repository-wide
  Ruff was also inspected and has pre-existing failures outside this increment.

## Full-suite findings and final protocol fix

The first full backend CI run completed with **40 failed, 6,883 passed, 143
skipped**. It exposed a regression in this increment: an open dictionary/`oneOf`
table recipe could not use the Codex strict object protocol. The unreleased tool
wire format now uses an ordered array of closed `{name,type,source?}` column
definitions. The strict protocol test passes on the original base and now passes
on this branch. Added nested optional-field restoration and duplicate-column
tests, and included the complete object-protocol test module in focused CI.

Final local expanded run after the schema correction: **420 passed, 1 failed,
5 skipped, 2 GUI tests deselected**. The remaining failure was the existing
shell exception/timeout cleanup test; it passes in isolation on both exact base
and branch. No exception/retry authority was loosened to satisfy it.

Additional exact-base comparisons:

- All **36** core runtime tests pass on base and branch.
- Of **16** selected non-platform failures from the full CI run, base and branch
  reproduce exactly the same **15 failing test IDs**, with one passing case.
  These cover usage accounting, subagents, consumer results, extension compaction
  policy, model/service discovery, node-app approval and peer-directory cases.
- Other full-CI failures include Windows-only native/QuickJS assumptions on
  Linux, missing Codex installation, a version expectation and a floating-point
  exact-equality assertion. Those are not claimed fixed by this increment.
- Full frontend CI on `69770a0d` passed after the storage-note fixture correction,
  including typecheck/build. The three unchanged files implicated in the local
  frontend run also passed all **46** tests on a sequential rerun.

This is evidence for integrating the bounded increment into `develop`, not a
claim that the repository-wide backend suite or installed user workflows are
fully accepted. Final head/check status remains attached to PR #130.

## One-time performance sample

Synthetic CSV, 5,000 rows, amount/fee plus rounded derived total, one explicit
totals check with the final ordered-column recipe: calculation **0.046 s**, XLSX
serialization and readback validation **0.051 s**, **76,460 bytes**, exact total
**1500.00**. This is a local sample,
not an SLA or an end-to-end model/plugin/network benchmark. Output metadata and
the recipe are included; model-facing previews are bounded separately.

## Not established

- Original Windows installed-app UI failure is not yet reaccepted with a live
  model. Pattern checks do not prove that every possible shell loop is bounded.
- Continuation preserves configuration/accounting/provenance but retains an
  existing lossy summary. Full histories/results/attachments are not all migrated;
  incompatible task-bound resource references fail explicitly.
- Input truth, company business rules, complex financing plans and ordinary
  employee end-to-end journeys require domain cases and live acceptance.
- No GUI, live Gshopper plugin operation, production deployment, signed update,
  release package or installation was performed. Do not label this as released.
- Local related tests do not establish a completely green repository-wide suite.
  PR CI results must be reported separately.
