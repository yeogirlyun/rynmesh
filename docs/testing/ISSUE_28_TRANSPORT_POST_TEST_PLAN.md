# Test plan: Transport-backed Private AI writes (#28)

Status: executed locally; final exact-commit CI pending
Last reviewed: 2026-09-02

## Purpose

This document defines the verification required for issue #28. It is separate
from the development plan: the plan explains how the feature is built, while
this document defines test layers, traceability, environments, pass/fail rules,
and the evidence that must exist before acceptance.

## Test environments

1. **Focused Windows verification** uses Python 3.12 and the repository's dev
   dependencies from an existing read-only virtual environment. Test output and
   caches may be written only inside this issue worktree.
2. **Ubuntu CI** installs `.[dev]` from scratch and runs the complete backend
   suite and Ruff.
3. **Docker LLM E2E CI** builds an isolated Registry, Provider, Consumer, Relay,
   and deterministic model stack, then tears it down with `llm_e2e.py down`.
4. **Desktop/package CI** proves the additive Python interface does not break
   the packaged node, web application, or both supported desktop architectures.

Windows-only POSIX permission checks, WSL shell checks, and Windows pipe/file
locking failures are not accepted as Linux evidence. They must be listed
exactly in the acceptance report and superseded by a green Ubuntu job.

## Functional test matrix

| Area | Required case | Automated evidence |
|---|---|---|
| Transport contract | `post_bytes` accepts body, headers, timeout, and response cap | Protocol and bundled implementation tests |
| Stdlib HTTPS | Exact body/content type; required auth cannot be overridden | Local HTTP server test |
| Response bound | Exactly `max_bytes` passes; `max_bytes + 1` fails | Boundary tests |
| Redirect policy | 3xx is rejected rather than followed | Stdlib/fronted tests |
| Fronted HTTPS | Connect host remains separate; Host and Connection cannot be overridden | Socket-level fronted test |
| CDN-WebSocket | Valid POST line, headers, Content-Length, separator, and exact body | Frame inspection test |
| REALITY | Streaming bounded read, no redirects, mandatory auth, response close | Mock session test |
| meek | Versioned inner request envelope with body and protected auth | Envelope decoding test |
| ECH | Fallback delegates POST; active malformed response is normalized | Delegate and active-context tests |
| Legacy plugin | Missing `post_bytes` fails closed | Peer-client unsupported test |
| Peer JSON | Compact UTF-8 round trip; invalid JSON/non-object rejected | `HttpPeerClient` tests |
| Privacy | Transport error and invalid response markers absent from formatted traceback | Unique-marker traceback tests |
| LLM task | Direct helper uses active Transport and the 2 MiB cap | Focused helper plus direct E2E |
| Settlement | Direct settlement posts to `/api/peer/llm/settlements` | Call-site regression plus E2E ledger check |
| Cancellation | Direct cancellation posts to `/api/peer/llm/cancellations` | Call-site regression plus multi-process cancellation |
| Mode separation | Direct, strict ICE/UDP, and encrypted Relay evidence stay distinct | Direct host acceptance and Docker E2E |

## Privacy assertions

- Tests use synthetic unique markers, never real prompts or outputs.
- The test fails if a marker appears in `str(exception)` or
  `traceback.format_exception(exception)`.
- Acceptance reports record hashes, state, counts, and transport metadata only.
- No request or response body may be copied into test evidence.

## Required commands

```text
python -m ruff check rynmesh/transport.py rynmesh/transport_plugins.py rynmesh/peer_http.py rynmesh/llm_package/routes.py tests/test_transport.py tests/test_llm_package.py tests/test_llm_hardening.py
python -m pytest tests/test_transport.py tests/test_llm_package.py tests/test_llm_hardening.py -q
python -m pytest tests/ -q
python scripts/llm_e2e.py run
python scripts/llm_e2e.py relay-run
python scripts/llm_e2e.py down
```

The Docker commands may be satisfied by the repository's Ubuntu CI when the
local Docker engine is unavailable. The accepted workflow URL and exact commit
must be recorded in the formal acceptance report.

## Pass/fail rules

- Focused tests and Ruff must have zero failures.
- Every changed behavior needs a regression test.
- Ubuntu backend, deterministic P2P/Relay E2E, packaged-node, webapp, and both
  desktop compile jobs must pass on the final reviewed branch head.
- A platform-excluded local failure must be demonstrably outside changed #28
  code and must have a corresponding passing CI job on the supported platform.
- The branch must be clean and `git diff --check` must pass.
- Any private marker in an error or formatted traceback is a release blocker.

## Evidence record

Evidence is authoritative only in
`docs/acceptance/issue-28/ACCEPTANCE_REPORT.md`. As of this review:

- focused Transport + LLM suite: **72 passed**;
- changed-file Ruff: **PASS**;
- whitespace validation: **PASS**;
- complete Windows suite with UTF-8 mode: **536 passed, 3 skipped, 8
  platform/unrelated failures**;
- prior baseline CI (`bf36072`): **PASS** in workflow run `33644630620`;
- final hardening commit (`5c1e690`): local verification **PASS**, exact-commit
  CI **pending**.

## Related documents

- Product specification: `docs/product/ISSUE_28_TRANSPORT_POST_PRODUCT_SPEC.md`
- Requirements: `docs/requirements/ISSUE_28_TRANSPORT_POST_REQUIREMENTS.md`
- Development plan: `docs/ISSUE_28_TRANSPORT_POST_WORK_PLAN.md`
- Acceptance report: `docs/acceptance/issue-28/ACCEPTANCE_REPORT.md`
