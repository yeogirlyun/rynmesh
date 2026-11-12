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

## One-time performance sample

Synthetic CSV, 5,000 rows, amount/fee plus rounded derived total, one explicit
totals check: calculation **0.055 s**, XLSX serialization and readback validation
**0.060 s**, **76,385 bytes**, exact total **1500.00**. This is a local sample,
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
