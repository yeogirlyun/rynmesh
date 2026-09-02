# Issue #30 test plan and evidence

Status: core security slice passing locally; full Issue matrix pending

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
- friend HMAC is a scoped alternative to the mesh key, and a replay is hidden
  behind the same generic 404;
- friends-only Provider denial occurs before capacity/inference for strangers
  and takes effect on the next task immediately after revoke.

## Commands

Run from the Issue worktree with the repository virtual environment:

```powershell
D:\code\rynmesh\.venv\Scripts\ruff.exe check rynmesh\friends.py rynmesh\peer_http.py tests\test_friends.py tests\test_friend_http.py
D:\code\rynmesh\.venv\Scripts\python.exe -m pytest tests\test_friends.py tests\test_friend_http.py -q
D:\code\rynmesh\.venv\Scripts\python.exe -m pytest tests\test_peer_http_auth.py tests\test_peer_messaging_http.py tests\test_llm_hardening.py -q
```

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

## Still required

- outbound-proxy support with an equivalent authenticated DNS pinning guarantee;
- Tauri deep-link integration tests beyond the completed Webapp paste/Join slice;
- Tauri deep-link tests;
- full backend/CI and two clean physical nodes, including offline revoke.

## Webapp slice matrix

`webapp/src/screens/Peers.friendMesh.test.tsx` covers:

- explicit endpoint, permission, expiry, and reachability acknowledgement;
- local QR creation, raw-link session boundary, invitation listing/cancellation;
- focus transfer after create and review;
- offline signature/fingerprint/network/scope/expiry/all-endpoint review;
- enabled outbound Join only after offline review, delegated to the local node;
- invalidation of the old review whenever the pasted link changes;
- pending signed-revocation retry without weakening local denial;
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
# 3 files passed, 9 tests passed

npm test
# 12 files passed, 47 tests passed

npm run lint
# TypeScript project check passed

npm run build
# TypeScript and Vite production build passed; 1770 modules transformed
```

Dependency installation/audit reported 0 vulnerabilities. These results cover
the Webapp create/review/local-Join slice and exact local-control API bodies;
they do not satisfy a physical two-node Join, Tauri deep-link, remote revoke,
or final product acceptance.

Revocation/privacy integration adds automated evidence that an offline notice
records a bounded error, the identical signed notice converges and remains
idempotent after reconnect, a configured mesh key is not required for that
exact signed route, privacy export excludes credentials, and explicit friend
erase removes both public and secret stores. Friend/Transport focused tests are
47/47; the combined Friend/Transport/LLM regression is 90/90.
