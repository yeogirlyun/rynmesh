# Windows source handoff — 2026-11-02

This branch delivers the current local Ryn work for review. It is not a release,
a production deployment, or completion of the outstanding milestone issues.

## Provenance and integration

- Public destination: `yeogirlyun/rynmesh`, branch `codex/workspace-handoff-20261101`
  (the delivery started on November 1, Asia/Hong_Kong).
- Local source: `feature/ryn-integrated-workspace` at
  `ac1c5c4ca712594f13f789a2968969d4719eb016`, plus 51 pending source/test/design files.
- Includes PR #113 head `e612727be2085f918bdc967a8c0d05b6d80fba67`.
  Its optional-browser packaging boundary and settings-lock fix are preserved.
- The source checkout and its existing dirty state were not reset or switched.
- The three Ryn company plugins (Gshopper workspace, Dashboard and affiliate)
  are delivered separately in private `yeogirlyun/gshopper-ryn-node`.
  The complete chat-backend and Tracker projects remain in their own existing
  repositories; this handoff does not move them into the company-plugin repository.

## Delivered implementation

- Long-task continuation and explicit owner budgets, per-model prompt notes,
  calibrated model capacity, durable file review and conversation forks.
- Windows terminal-session integration and continuation provenance.
- Tool dispatch coordination, bounded streamed reads, usage settlement,
  permission-change handling and extension web capability updates.
- Agent overview, preview, welcome and subagent UI, with focused tests.
- The handoff fixes a demonstrated capacity mismatch: tool-menu projection now
  uses the same calibrated effective capacity as model request admission.
  The acceptance test was retained and failed before the implementation fix.

## Verification scope

- Nine focused Python suites: **125 passed**, after the capacity fix.
- Conversation-fork regression suite after fixture-import cleanup: **16 passed**.
- Web TypeScript: `node node_modules/typescript/bin/tsc -b --noEmit` passed.
- Overview/preview/subagent UI: seven tests passed; extension web app: six passed.
- `RynAgent.test.tsx`: **81 passed** in a separate run. Its existing mocks emit
  unhandled `/api/local/agent/automations` request warnings; assertions pass, but
  this is not automation-flow acceptance.
- Full `ruff check rynmesh tests` and `git diff --check` passed. Import-only lint
  repairs preserve the existing assertions and resource fixture binding.
- A first multi-file frontend run returned exit 1 while its JSON contained only
  three successful suites. It is not counted as a successful complete run;
  remaining screen suites are checked separately and recorded below.
- The initial Python run hit Windows global pytest-temp cleanup permissions.
  The final run used a dedicated handoff `--basetemp` and completed normally.
- No live-provider, installed-desktop, browser-adapter distribution, complete
  backend regression, or user-invitation acceptance is claimed by this delivery.

## Remaining work

- #116: continue classification and repair of the broader regression failures
  documented in `INTEGRATION_HANDOFF_2026-10-29.md`; this focused run does not close it.
- #114: independently distributable browser adapter; no extracted developer-local
  Codex implementation is reintroduced by this handoff.
- #115: invitation to useful Word/PDF output, save/share and repeat-use acceptance,
  consistent Apps state and unsaved-document handoff, GridCraft empty formulas.
- #112 remains the common vocabulary proposal; do not create another parallel
  install system. Node / Service / API-MCP / Plugin / App / Extension retain their
  assigned responsibilities.

## Review and rollback

Review against the PR #113 integration branch, keeping its fixes in ancestry.
No main branch, tag, installed build, user session or deployment was changed.
Source rollback is selecting the preceding reviewed commit; any future installed
release needs its own packaging, acceptance and rollback procedure.

Untracked `artifacts/` trees were intentionally excluded: they contain local QA,
company evidence, runtime exports and extracted third-party inspection outputs.
Source, tests and the design preview are included. Historical validation claims
in existing documents remain historical claims, not current-head acceptance.
