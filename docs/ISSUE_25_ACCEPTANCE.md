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
| Focused Webapp | Included in full 59/59 pass |
| Full Webapp | 12 files, 59 tests passed |
| Python LLM-focused | 44 passed |
| Existing reader/steering | 19 passed |
| TypeScript / build | Passed; 1,741 modules built |
| Python lint | Passed |
| Handoff leakage marker | Absent from URL/local/session storage |
| Prompt injection fixture | One trusted closing marker; fake marker neutralized |
| Normal browser fixture | 207/207 characters, full context |
| Long browser fixture | 1,021/13,907 characters, 1/2 blocks |
| Provider URL after consume | `peer`, `service`, `network` only |
| Remove action | Card count 1 -> 0 |
| Provider storage failure | Bucket/draft preserved; switching released |
| Full backend run | 516 passed, 3 skipped; 13 baseline Windows-only failures |

## Non-blocking environment notes

The repository-wide Python run cannot be all-green on this Windows host because
13 tests assert POSIX locale/file-mode/bash/pipe behavior. Every failure was
reproduced on the untouched Issue #24 baseline. These tests do not exercise the
Issue #25 implementation; the relevant backend and Webapp suites are green.

## Release/merge note

This branch is ready for local integration review. It has not been pushed and
does not alter any other worktree or branch.
