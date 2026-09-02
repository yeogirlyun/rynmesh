# Formal acceptance report: Transport-backed Private AI writes (#28)

Decision: PENDING EXACT-COMMIT CI
Reviewed feature baseline: `bf3607222e6e817a85cc507e01ff24ca8ee28c51`
Reviewed hardening commit: `5c1e690`
Branch: `codex/issue-28-acceptance-isolated`
Review date: 2026-09-02
Acceptance date: pending

## Decision summary

The feature behavior is implemented and the independent local acceptance suite
passes. A second review found three gaps in the previously accepted baseline:

1. chained transport and JSON-decode exceptions could reproduce private body
   markers in a normally formatted traceback;
2. a caller header could override the fronted HTTPS Host/Connection metadata;
3. malformed fronted/ECH HTTP responses were not consistently normalized.

Commit `5c1e690` fixes those gaps and adds regression coverage, including direct
settlement and cancellation call-site tests. The earlier `ACCEPTED` decision is
therefore replaced by this pending decision until the repository CI runs on a
branch head containing the hardening commit and this acceptance package.

## Scope under acceptance

This report covers only issue #28: bounded POST support across the Transport
seam and migration of Private AI task, settlement, and cancellation peer HTTP
writes. Streaming, settlement-ledger unification, background-worker
refactoring, and public-WAN P2P certification remain excluded.

## Documentation package

| Artifact | Path | Status |
|---|---|---|
| Product specification | `docs/product/ISSUE_28_TRANSPORT_POST_PRODUCT_SPEC.md` | complete |
| Development plan | `docs/ISSUE_28_TRANSPORT_POST_WORK_PLAN.md` | complete |
| Test plan | `docs/testing/ISSUE_28_TRANSPORT_POST_TEST_PLAN.md` | complete |
| Requirements traceability | `docs/requirements/ISSUE_28_TRANSPORT_POST_REQUIREMENTS.md` | complete |
| Acceptance report | this file | final CI pending |

## Independent local evidence

### Focused and static verification

| Check | Result | Evidence |
|---|---|---|
| Changed-file Ruff | PASS | `All checks passed!` |
| Transport + LLM focused suite | PASS | 72 passed, 1 deprecation warning |
| Git whitespace validation | PASS | `git diff --check` clean |
| Branch isolation | PASS | dedicated worktree and `codex/issue-28-acceptance-isolated` branch |

Focused verification command:

```text
D:\code\rynmesh\.venv\Scripts\python.exe -m pytest tests/test_transport.py tests/test_llm_package.py tests/test_llm_hardening.py -q
```

The 72 tests include exact/max+1 response boundaries, redirect rejection,
mandatory authentication, fronted Host protection, CDN-WebSocket framing,
REALITY/meek/ECH paths, plugin fail-closed behavior, UTF-8/object-only JSON,
formatted-traceback privacy markers, the 2 MiB LLM cap, and explicit settlement
and cancellation route assertions.

### Complete Windows suite

With `PYTHONUTF8=1`, the complete suite produced:

```text
8 failed, 536 passed, 3 skipped
```

The eight failures are outside changed #28 code:

- three POSIX executable-bit or unavailable-WSL shell assertions;
- three POSIX `0600` mode assertions on Windows;
- one pre-existing Windows atomic file-replace/read concurrency failure in
  Signal50 media operations;
- one `select()`-on-Windows-subprocess-pipe failure in the MCP smoke test.

The focused #28 suite has zero failures. These local exclusions are not Linux
evidence and require the Ubuntu backend job to pass on the final commit.

### Local E2E availability

Docker Desktop was unavailable during this independent review: the configured
Linux engine named pipe did not exist. No Docker stack was started, so no cleanup
was necessary. The earlier isolated direct task/settlement/cancellation evidence
is retained as prior-review evidence, but this review does not mislabel it as a
new execution. Direct settlement/cancellation routing was independently
rechecked with new call-site regression tests.

## Verified remote baseline evidence

The public GitHub run for baseline `bf36072` was independently inspected:

- pull request: https://github.com/yeogirlyun/rynmesh/pull/32
- workflow: https://github.com/yeogirlyun/rynmesh/actions/runs/33644630620
- result: Success

| CI job on `bf36072` | Result |
|---|---|
| contribution-workflow | PASS |
| backend | PASS |
| webapp | PASS |
| llm-e2e | PASS |
| packaged-node | PASS |
| desktop-compile (x86_64) | PASS |
| desktop-compile (aarch64) | PASS |

This proves the pre-hardening feature baseline, including Ubuntu full-suite and
Docker P2P/Relay regressions. It is not substituted for CI on `5c1e690`.

## Acceptance checklist

- [x] Product specification is complete and linked.
- [x] Development plan and rollback constraints are documented.
- [x] Independent test plan and pass/fail rules are documented.
- [x] Traceable functional, security, privacy, and reliability requirements exist.
- [x] Transport exposes bounded POST bytes.
- [x] Every bundled Transport has a POST implementation.
- [x] `HttpPeerClient.post_json` is bounded, object-only, and metadata-error-only.
- [x] Task, settlement, and cancellation writes use the active Transport.
- [x] Authentication, fronting, proxy, and redirect policies are preserved.
- [x] Private body markers are absent from formatted public tracebacks.
- [x] Focused tests, Ruff, and whitespace validation pass locally.
- [x] Pre-hardening baseline CI and E2E evidence were independently verified.
- [ ] Required CI jobs pass on a head containing `5c1e690` and this report.
- [ ] Final reviewed head, workflow URL, reviewer decision, and acceptance date are recorded.

## Final gate

Do not merge or close issue #28 based on this report yet. Cherry-pick the
hardening and documentation commits onto PR #32 (or open an equivalent isolated
PR), run the complete required workflow, and update this report with the exact
green head and workflow URL. Only then may `Decision` change to `ACCEPTED`.
