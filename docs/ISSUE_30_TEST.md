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
- network-key protection remaining active on other peer routes.

## Commands

Run from the Issue worktree with the repository virtual environment:

```powershell
D:\code\rynmesh\.venv\Scripts\ruff.exe check rynmesh\friends.py rynmesh\peer_http.py tests\test_friends.py tests\test_friend_http.py
D:\code\rynmesh\.venv\Scripts\python.exe -m pytest tests\test_friends.py tests\test_friend_http.py -q
D:\code\rynmesh\.venv\Scripts\python.exe -m pytest tests\test_peer_http_auth.py tests\test_peer_messaging_http.py tests\test_llm_hardening.py -q
```

## Evidence on 2026-09-02

- Friend-focused tests: 8 passed on the exact core-slice commit candidate.
- Ruff on changed Python files: passed before documentation.
- Related auth/messaging/LLM run: 35 passed and one Windows-only pre-existing
  POSIX-mode assertion failed (`0o666` vs `0o600`). This is not counted as a
  pass and must be green on Linux CI.

## Still required

- outbound Transport/DNS rebinding and endpoint-change tests;
- per-friend middleware and friends-only LLM admission/capacity tests;
- revocation delivery/reconnect tests;
- Webapp outbound-Join and Tauri integration tests beyond the completed safety/review slice;
- Tauri deep-link tests;
- full backend/CI and two clean physical nodes, including offline revoke.

## Webapp slice matrix

`webapp/src/screens/Peers.friendMesh.test.tsx` covers:

- explicit endpoint, permission, expiry, and reachability acknowledgement;
- local QR creation, raw-link session boundary, invitation listing/cancellation;
- focus transfer after create and review;
- offline signature/fingerprint/network/scope/expiry/all-endpoint review;
- disabled, truthfully explained outbound Join;
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
# 3 files passed, 7 tests passed

npm test
# 12 files passed, 45 tests passed

npm run lint
# TypeScript project check passed

npm run build
# TypeScript and Vite production build passed; 1770 modules transformed
```

Dependency installation/audit reported 0 vulnerabilities. These results accept
only the Webapp safety/review slice; they do not satisfy physical Join, Tauri
deep-link, service ACL, or two-node acceptance.
