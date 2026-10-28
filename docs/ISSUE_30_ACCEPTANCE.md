# Issue #30 acceptance report

Development decision: **ACCEPTED (LOCAL SOURCE BUILD)**

Release decision: **NOT YET ACCEPTED**

Core branch: `feature/issue-30-friend-mesh-core`
Webapp slice branch: `feature/issue-30-friend-mesh-ui`
Canonical branch: `feature/issue-30-friend-mesh`

Date: 2026-09-03

## Current result

The Issue's local development outcome is accepted. A no-Docker harness ran two
real node processes with independent homes/identities/ports plus a local HTTP
Registry. It completed create, offline review, Join, both-side persistence,
friends-only Private AI complete, online revoke, offline stop/restart/retry
convergence, and denial of the next order before inference. Sanitized evidence
is in `docs/evidence/issue30-local-two-node-e2e.json`.

Release is not yet accepted because installed desktop deep-link/scan evidence
and exact-commit release CI/package evidence remain outstanding. The final
integrated #23 streaming ACL is now locally proven by the two-process E2E evidence
at `docs/evidence/issue30-integration-two-node-e2e.json`. Cross-host physical repetition, three-OS testing,
and a separate maintainer sign-off were additions in earlier local documents,
not explicit Issue requirements; they are now correctly classified as release
hardening/governance rather than retroactive development blockers.

The Webapp create/offline-review/Join/list/cancel/revoke and local-QR slice is
implemented and automated.
The desktop scheme and launch/running-instance forwarding are implemented; an
installed package still needs QR/deep-link evidence on each declared supported
release target.

Core-slice evidence: 9/9 focused tests and Ruff passed; the full Windows run
reported 527 passed, 3 skipped, and 8 known platform/pre-existing failures with
no Friend Mesh failure. Linux exact-commit CI remains required.

The final integration-branch full backend rerun reports 555 passed, 3 skipped,
and 7 known Windows/platform failures, with no Friend Mesh, privacy, revocation,
Transport, or LLM ACL failure.

Integration evidence: the #28-based branch passes 44 Friend/Transport tests,
including simulated two-node Join and explicit endpoint-change review.
After friends-only Private AI integration, the combined focused regression is
88 passed with Ruff green.

Webapp Join/retry/deep-link integration passes its focused matrix and 51/51 full Webapp tests,
TypeScript lint, and the production build. Review makes no endpoint contact;
Join uses the local API, and changing the pasted link invalidates prior review.

Signed remote revocation now records offline delivery, retries after reconnect,
and converges idempotently. Sanitized privacy export and explicit friendship
erase are covered by the 47/47 Friend/Transport run and 90/90 combined
Friend/Transport/LLM regression.

The 2026-09-03 strict canonical audit adds 48/48 focused
Friend/HTTP/Transport tests, 100 passed/1 skipped expanded
Friend/Transport/LLM tests, 51/51 Webapp tests, and green Ruff, TypeScript lint,
and production build. The full backend reported 555 passed, 3 skipped, and 8
documented Windows-platform failures, with no Friend Mesh failure. It proves
stream-path HMAC separation at the protocol primitive, proxy rejection before
contact/invite consumption, and exact public/secret friendship-store erasure.
It also fixed an unhandled proxy Transport error at the Join boundary. Cargo
was unavailable for a current metadata rerun. This does not replace route-level
#23 streaming evidence or installed-package evidence.

The final local E2E run used source commit
`c40403bfaf8ea14f968153dff6a79a51c2a28401`, passed in 37.968 seconds, and
recorded zero invite-link/secret occurrences across both homes and sanitized
logs. The exact run stopped every child process and deleted its temporary homes.
The final focused suite passed 49 tests, the expanded Friend/Transport/LLM suite
passed 101 with one skip, and the full Webapp passed 51/51 plus lint and build.

The final combined #23+#30 implementation commit
`4bd2b8b56df6f69bd4aac2b995245b1205e98e0a` passed the schema-v2 two-node
stream/revoke/restart harness three consecutive times. The final 8.204-second
sanitized run records `rynmesh.llm.stream.v1`, `peer_http_direct`, post-revoke
stream denial before inference, offline-restart denial, zero invite/link secrets,
and complete child-process cleanup.

## Criteria status

- [x] Friendship state is separate from identity trust roots.
- [x] Signed/versioned short-lived invite with at least 256 random secret bits.
- [x] Persist only an scrypt invite-secret hash; rotate on one atomic consume.
- [x] Public acceptance uses signed peer/proof binding and X25519 encrypted response.
- [x] Invalid probes and replay receive the same generic 404 and are rate-limited.
- [x] Per-friend HMAC rejects wrong sender/path/body/time/nonce and revoked secrets.
- [x] Local revoke removes authorization and the relationship secret immediately.
- [x] Signed remote revocation application is idempotent for the exact pair.
- [x] Eight protocol/security decisions are documented; normal maintainer review
  remains merge governance rather than functional acceptance.
- [x] Outbound Join uses #28 Transport and blocks unsafe resolved addresses.
- [x] The Transport pins the validated DNS answer while preserving URL SNI/Host.
- [x] V1 explicitly excludes outbound proxy Join and fails closed before contact
  or invite consumption; actionable installed-app presentation remains a release item.
- [x] Changed endpoints require a second explicit review.
- [x] Friends-only complete-v1 admission denies before capacity/inference on
  the canonical #30 implementation.
- [x] Local integration: friends-only stream-v1 route uses the same Friend ACL on the final stacked
  commit; complete-route HMAC cannot authenticate the stream route; post-revoke
  next-stream admission is denied before capacity/inference.
- [x] Simulated two-app revocation delivery converges after offline/reconnect and
  is idempotent.
- [x] Two independent node processes with durable homes converge across actual
  process stop/restart and deny the next order before inference.
- [x] Webapp and Tauri source create/review/join/QR/deep-link/list/revoke flows complete.
  - [x] Webapp create/list/cancel with explicit endpoint/scope/expiry review.
  - [x] Pasted-link offline review shows signature, fingerprint, network, every endpoint/address class, scope, and expiry.
  - [x] QR is generated locally with a pinned dependency and a zero-network automated assertion.
  - [x] Friend active/revoked details and high-risk local-first revoke are implemented.
  - [x] Keyboard labels, disabled-state explanation, and create/review focus transfer are tested.
  - [x] Outbound Join and received-relationship persistence are integrated and enabled through the local node.
  - [x] Tauri scheme, launch/running-instance forwarding, strict validation, and paste fallback are implemented.
  - [ ] Release: installed desktop scan/deep-link acceptance is complete on declared supported targets.
- [x] Privacy export/erase behavior complete.
- [ ] Release: full backend/Webapp/Rust/package CI is green on the exact integrated commit.
- [x] Two clean local node processes pass use/revoke/next-order-denied including offline revoke.
- [x] Product, development, test, and local acceptance evidence are updated.

## Acceptance rule

Unchecked release items block the release decision, not the accepted local development
decision. Release may change to **ACCEPTED** only after the integrated/installed
items have exact-commit evidence and no invite/relationship secret appears in
the evidence bundle.
