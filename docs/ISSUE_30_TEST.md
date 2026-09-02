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
- Webapp unit, type, lint, build, accessibility, and no-network QR tests;
- Tauri deep-link tests;
- full backend/CI and two clean physical nodes, including offline revoke.
