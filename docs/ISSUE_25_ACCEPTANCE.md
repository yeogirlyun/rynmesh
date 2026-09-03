# Issue #25 acceptance record

Decision: **accepted**

Date: 2026-09-02

Baseline: Issue #24 commit `ef817bc`

## Acceptance criteria

- [x] A readable For You item opens a new grounded Private AI conversation in
  one user action.
- [x] The Browser sends only through the local Consumer node and never contacts
  the Provider directly.
- [x] Identical input and Provider limits produce deterministic Unicode-safe
  truncation with a visible pre-send notice.
- [x] Article text remains inside a neutralized, explicitly untrusted
  `ARTICLE_CONTEXT` boundary.
- [x] Article title/body/source markers do not enter URL parameters,
  `history.state`, localStorage, sessionStorage, Registry data, or normal logs.
- [x] The opaque handoff expires, is non-enumerable, and is consumed once.
- [x] Grounding is saved only inside the encrypted conversation and is
  removable before later sends.
- [x] Reader failure, empty extraction, no Provider, too-small context, expired
  handoff, offline/busy Provider, storage failure, and request failure have
  safe user-readable behavior.
- [x] Safety input + output cap + 128 margin never exceeds the selected context
  window in Webapp construction or Consumer admission.
- [x] ASCII, Chinese, combining-mark, and emoji fixtures use the same UTF-8-byte
  policy; chars/4 remains pricing-only.
- [x] Grounded conversation/history remains isolated by Provider peer ID and
  package ID across Issue #24 switching.
- [x] Focused tests, full Webapp tests, TypeScript check, Python LLM tests,
  Ruff, production build, and real-browser flow pass.

## Evidence

| Evidence | Result |
| --- | --- |
| Focused Webapp | 5 files, 32 tests passed |
| Full Webapp | 12 files, 63 tests passed |
| Python LLM + reader focused | 63 passed |
| TypeScript / build | Passed; 1,741 modules built |
| Python lint | Passed |
| Handoff leakage marker | Absent from URL/local/session storage |
| Prompt injection fixture | One trusted closing marker; fake marker neutralized |
| Normal browser fixture | 207/207 characters, full context |
| Long browser fixture | 1,021/13,907 characters, 1/2 blocks |
| Provider URL after consume | `peer`, `service`, `network` only |
| Remove action | Card count 1 -> 0 |
| Provider storage failure | Bucket/draft preserved; switching released |
| Full backend run | 521 passed, 3 skipped; 8 Windows platform/unrelated failures |

## Non-blocking environment notes

The repository-wide Python run cannot be all-green on this Windows host. The
current eight failures cover POSIX executable/file-mode behavior, unavailable
WSL bash, one Windows file-lock race, and `select()` on a Windows subprocess
pipe. These tests do not exercise the Issue #25 implementation; the relevant
backend and Webapp suites are green. Ubuntu repository CI remains the supported
platform gate for those exclusions.

## 2026-09-03 completion audit

- Added deterministic same-millisecond Provider round-trip coverage.
- Added actionable too-small-context UI and component coverage.
- Added expired handoff plus failed/empty reader extraction coverage.
- Re-ran focused Webapp (32/32), full Webapp (63/63), TypeScript, production
  build, focused Python (63/63), Ruff, and the complete Windows backend suite.
- Historical browser observations remain documented, but their screenshot
  files are not present in this branch. A fresh browser evidence package
  requires the isolated Consumer/reader/provider fixture on port 8791.

## Release/merge note

This branch is ready for local integration review. It has not been pushed and
does not alter any other worktree or branch.
