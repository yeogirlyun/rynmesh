# Issue #30 test plan and evidence

Status: local source-build development matrix passed; release matrix pending

## Automated matrix

`tests/test_friends.py` covers:

- offline signature/fingerprint verification and secret hashing;
- unsupported/tampered/expired/cancelled invitation rejection;
- literal endpoint safety and explicit private-LAN review;
- exactly one winner under concurrent acceptance;
- distinct relationship credential rotation;
- acceptance proof binding to peer record, invite, X25519 key, network, scope,
  timestamp, and nonce;
- HMAC path/body/sender/timestamp binding and nonce replay rejection;
- explicit complete-route to streaming-route HMAC replay rejection and a
  separately signed streaming path acceptance at the protocol primitive;
- immediate secret deletion on local revoke;
- idempotent remote revoke and unrelated-pair rejection;
- stale crash-lock recovery.

`tests/test_friend_http.py` covers:

- local invite creation and sanitized offline review;
- acceptance while the mesh-wide key remains configured but undisclosed;
- encrypted credential rotation/decryption by only the acceptor;
- no raw invite/relationship secret in public state;
- generic 404 on replay/wrong secret;
- invalid/low-order X25519 response keys cannot consume an invitation;
- network-key protection remaining active on other peer routes.
- two independent in-memory nodes complete Join through the Transport seam and
  both persist the exact active relationship;
- private or mixed DNS answers fail before contact;
- changed endpoints remain pending until exact approval, while rejection
  deletes the local credential.
- an explicitly configured outbound proxy fails before an outbound client is
  constructed, before local relationship persistence, and before the remote
  invite is consumed;
- friend HMAC is a scoped alternative to the mesh key, and a replay is hidden
  behind the same generic 404;
- friends-only Provider denial occurs before capacity/inference for strangers
  and takes effect on the next task immediately after revoke.
- privacy erase leaves exact empty public and secret schemas, including
  outstanding invitations, friends, revocations, nonces, and credentials.

## Commands

Run from the Issue worktree with the repository virtual environment:

```powershell
D:\code\rynmesh\.venv\Scripts\ruff.exe check rynmesh\friends.py rynmesh\peer_http.py tests\test_friends.py tests\test_friend_http.py
D:\code\rynmesh\.venv\Scripts\python.exe -m pytest tests\test_friends.py tests\test_friend_http.py -q
D:\code\rynmesh\.venv\Scripts\python.exe -m pytest tests\test_peer_http_auth.py tests\test_peer_messaging_http.py tests\test_llm_hardening.py -q
D:\code\rynmesh\.venv\Scripts\python.exe scripts\issue30_two_node_e2e.py --output docs\evidence\issue30-local-two-node-e2e.json
```

## Local two-process E2E evidence on 2026-09-03

Exact source commit `c40403bfaf8ea14f968153dff6a79a51c2a28401` passed the
no-Docker harness in 37.968 seconds. The sanitized JSON is
`docs/evidence/issue30-local-two-node-e2e.json`; it records two distinct child
processes/homes/ports, reviewed private-LAN DNS-to-socket correlation, real HTTP
Registry and peer transport, friends-only complete inference, online revoke,
offline stop/restart/retry convergence, and both post-revoke orders denied
before the deterministic model call count changed. Both relationship secret
sets were empty after convergence, raw invite/link scans were zero, every child
process stopped, and the temporary workspace was removed.

The harness was repeated 10 consecutive times after adding readiness for the
real 30-second provider capacity publication; all 10 passed. The focused
Friend/HTTP/Transport regression passed 49 tests; the expanded
Friend/Transport/LLM regression passed 101 with one skip. The full Webapp passed
51/51 tests, TypeScript lint, and production build. Ruff/`py_compile` passed.

## Evidence on 2026-09-02

- Friend/Transport-focused integration run: 44 passed after outbound Join and
  endpoint-review tests (`test_friends.py`, `test_friend_http.py`, and
`test_transport.py`).

Friend/Transport/LLM regression after ACL integration: 88 passed across the
focused Friend, Transport, LLM hardening, and full LLM package suites.
- Ruff on changed Python files: passed before documentation.
- Related auth/messaging/LLM run: 35 passed and one Windows-only pre-existing
  POSIX-mode assertion failed (`0o666` vs `0o600`). This is not counted as a
  pass and must be green on Linux CI.
- Full backend with `PYTHONUTF8=1`: 527 passed, 3 skipped, 8 failed in 62.38s.
  The failures are the same platform/pre-existing classes independently seen
  on the #28 baseline: three Windows executable/WSL checks, three POSIX `0600`
  mode assertions, one existing Windows `os.replace` reader race, and one
  Windows `select()`-on-pipe limitation. No Friend Mesh test failed.

Exact integration branch full backend rerun on 2026-09-03: `555 passed,
3 skipped, 7 failed` in 59.05s. The seven failures are the same Windows-only
environment classes: executable-bit assertions, unavailable WSL bash, POSIX
`0600` mode assertions, and `select()` on a pipe. All Friend Mesh, privacy,
revocation, Transport, and LLM ACL tests passed.

## Release evidence still required

- actionable installed-app presentation of the V1 fail-closed proxy exclusion;
- installed Tauri package deep-link/QR scan tests on declared supported targets;
- full exact-commit backend/Webapp/Rust/package CI;
- final integrated #23 stream-v1 Friend ACL and post-revoke denial;
- optional physical cross-host repetition for release network hardening.

## Webapp slice matrix

`webapp/src/screens/Peers.friendMesh.test.tsx` covers:

- explicit endpoint, permission, expiry, and reachability acknowledgement;
- local QR creation, raw-link session boundary, invitation listing/cancellation;
- focus transfer after create and review;
- offline signature/fingerprint/network/scope/expiry/all-endpoint review;
- enabled outbound Join only after offline review, delegated to the local node;
- invalidation of the old review whenever the pasted link changes;
- pending signed-revocation retry without weakening local denial;
- strict desktop deep-link validation, launch/runtime forwarding, one-use memory
  handoff, and offline-review prefill with zero endpoint contact;
- high-risk local-first revoke and safe delivery status;
- separation from trust-root actions and blocked local endpoints.

`webapp/src/domain/friendMesh.test.ts` asserts address classification and proves
QR creation makes zero `fetch` calls. `liveNodeClient.friendMesh.test.ts` asserts
all Friend Mesh browser operations remain under `/api/local` with encoded route
IDs and exact request bodies.

## Webapp evidence on 2026-09-02

Executed in `codex/issue-30-friend-mesh-ui`:

```powershell
npm test -- --run src/domain/friendMesh.test.ts src/domain/liveNodeClient.friendMesh.test.ts src/screens/Peers.friendMesh.test.tsx
# Friend Mesh/deep-link focused files passed, including 10/10 deep-link + panel tests

npm test
# 13 files passed, 51 tests passed

npm run lint
# TypeScript project check passed

npm run build
# TypeScript and Vite production build passed; 1773 modules transformed
```

Dependency installation/audit reported 0 vulnerabilities. These results cover
the Webapp create/review/local-Join slice and exact local-control API bodies;
they do not satisfy an installed-desktop QR/deep-link test. The later
multiprocess harness supplies the separate node/runtime evidence.

Revocation/privacy integration adds automated evidence that an offline notice
records a bounded error, the identical signed notice converges and remains
idempotent after reconnect, a configured mesh key is not required for that
exact signed route, privacy export excludes credentials, and explicit friend
erase removes both public and secret stores. Friend/Transport focused tests are
47/47; the combined Friend/Transport/LLM regression is 90/90.

Tauri configuration/package inspection detects Rust and JS deep-link 2.4.10,
the `rynmesh` static scheme, single-instance deep-link feature, and required
capability. `cargo metadata --locked` passes. `cargo check --locked` resolves
and starts compiling the exact graph, then stops because this Windows host has
no Visual C++ `link.exe`; installed-platform Rust CI remains required.

## Strict completion audit evidence on 2026-09-03

The canonical #30 worktree passed 48/48 Friend/HTTP/Transport tests after adding
stream-path HMAC replay, proxy fail-before-contact, and exact privacy-store
erasure assertions. The expanded Friend/Transport/LLM regression passed 100
tests with one skipped. The full Webapp passed 51/51 tests, TypeScript lint, and
the production build. Ruff passed on the touched Python implementation and test
files. The proxy test also exposed and now covers a fixed exception-boundary
defect: `TransportError(pinned_proxy_unsupported)` is converted to the bounded
Join failure instead of escaping as a server error.

The full backend on this canonical audit commit reported 555 passed, 3 skipped,
and 8 failed in 69.72 seconds. The failures are the documented Windows classes:
three executable-bit/WSL assertions, three POSIX `0600` assertions, one
`os.replace` reader race, and one `select()`-on-pipe limitation. No Friend Mesh
test failed. Cargo was not installed or on PATH on this audit host, so locked
metadata was not rerun; the earlier recorded metadata result is historical
evidence, not an exact-audit rerun.

The added multiprocess run now proves a real TCP socket between two independent
node processes, durable restart/revocation convergence, and complete-v1 Friend
ACL behavior. It does not prove cross-host routing, installed OS deep-link
dispatch, or #23's streaming route on the final integrated commit. Those are
release/integration artifacts in `ISSUE_30_STRICT_COMPLETION_AUDIT.md`.
