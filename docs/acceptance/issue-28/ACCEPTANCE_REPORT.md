# Formal acceptance report: Transport-backed Private AI writes (#28)

Decision: pending remote CI
Reviewed commit: `933c312c6ef5e3e37799840078c9a1fd84a40e5c`
Branch: `codex/issue-28-transport-post`
Acceptance date: pending

## Scope under acceptance

This report accepts only issue #28: bounded POST support across the Transport
seam and migration of the three Private AI peer HTTP writes. Streaming,
settlement-ledger unification, background-worker refactoring, and public-WAN
P2P certification are excluded.

## Evidence collected

### Static and focused automated verification

| Check | Result | Evidence |
|---|---|---|
| Ruff on changed Python and focused tests | PASS | `All checks passed` |
| Transport unit suite | PASS | 25 passed |
| Transport + LLM focused regression | PASS | 66 passed |
| Git whitespace validation | PASS | `git diff --check` clean |
| Branch isolation | PASS | One issue-specific commit based on public main |

The focused tests cover exact body and content type, network authentication,
exact/max+1 response boundaries, redirect rejection, fronted Host behavior,
CDN-WebSocket framing, REALITY streaming bounds, meek inner envelopes, ECH
fallback, UTF-8/invalid JSON handling, plugin fail-closed behavior, and private
marker exclusion.

### Isolated multi-process acceptance

Docker Desktop could not start on the Windows host because its own stale
`dockerInference` runtime socket crashed the engine. No application code caused
that failure. An equivalent isolated five-process topology was run instead:
Registry, encrypted Relay, Provider node, Consumer node, and an existing local
OpenAI-compatible model server, each with separate data directories.

| Flow | Result | Safe evidence |
|---|---|---|
| Direct task creation | PASS | `peer_http_direct`, `relay_used=false`, non-empty output |
| Direct settlement | PASS | Provider contained exactly one matching earning event |
| Direct cancellation | PASS | Consumer reached `cancelled`; no output was returned |
| Encrypted Relay regression | PASS | `encrypted_relay`, `relay_used=true`, succeeded |
| Cleanup | PASS | All temporary node/registry processes stopped; test ports released |

No prompt or model output is reproduced in this report.

## Complete-suite status

The Windows full suite reached 531 passing tests and 3 skips. Seven remaining
failures are pre-existing platform assumptions: POSIX executable/0600 modes,
an unavailable WSL bash, and `select()` on a Windows subprocess pipe. They do
not execute changed #28 code. These are not treated as Linux CI evidence.

Formal acceptance therefore remains pending until the reviewed commit passes
the repository's Ubuntu backend and Docker LLM E2E jobs. The final CI URL and
job results will be recorded below before this report is signed.

## Acceptance checklist

- [x] Product specification is present and linked.
- [x] Traceable functional, security, and reliability requirements are present.
- [x] Development plan and rollback constraints are documented.
- [x] Transport exposes bounded POST bytes.
- [x] Every bundled Transport has a POST implementation.
- [x] `HttpPeerClient.post_json` has stable, bounded JSON behavior.
- [x] Task, settlement, and cancellation writes use the active Transport.
- [x] Authentication, profile, proxy, and redirect policies are preserved.
- [x] Private bodies are absent from public errors and acceptance evidence.
- [x] Direct and encrypted Relay multi-process flows pass.
- [x] Focused tests and lint pass.
- [ ] Required remote CI jobs pass on the reviewed commit.
- [ ] Final reviewer decision and acceptance date are recorded.

## Remote CI evidence

Pull request: pending
Reviewed CI commit: pending

| CI job | Result |
|---|---|
| contribution-workflow | pending |
| backend | pending |
| webapp | pending |
| llm-e2e | pending |
| packaged-node | pending |
| desktop-compile (x86_64) | pending |
| desktop-compile (aarch64) | pending |

## Final decision

Pending. Do not mark issue #28 formally accepted or merge it until all required
remote jobs are green and this report references the exact reviewed commit.

