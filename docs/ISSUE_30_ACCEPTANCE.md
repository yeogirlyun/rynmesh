# Issue #30 acceptance report

Decision: **NOT YET ACCEPTED**

Core branch: `codex/issue-30-friend-mesh`
Webapp slice branch: `codex/issue-30-friend-mesh-ui`
Canonical audit branch: `codex/issue-30-friend-mesh-integration`

Date: 2026-09-03

## Current result

The security/store/public-acceptance foundation is implemented and has focused
automated evidence. The complete product is not accepted because installed
desktop deep-link/scan evidence, exact-commit CI, maintainer protocol review,
and two-node physical acceptance remain outstanding.

The Webapp create/offline-review/Join/list/cancel/revoke and local-QR slice is
now implemented and automated, but this does not change the overall decision.
The desktop scheme and launch/running-instance forwarding are implemented; an
installed package still needs physical QR/deep-link evidence on each platform.

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
#23 streaming evidence or any physical/installed-package evidence.

## Criteria status

- [x] Friendship state is separate from identity trust roots.
- [x] Signed/versioned short-lived invite with at least 256 random secret bits.
- [x] Persist only an scrypt invite-secret hash; rotate on one atomic consume.
- [x] Public acceptance uses signed peer/proof binding and X25519 encrypted response.
- [x] Invalid probes and replay receive the same generic 404 and are rate-limited.
- [x] Per-friend HMAC rejects wrong sender/path/body/time/nonce and revoked secrets.
- [x] Local revoke removes authorization and the relationship secret immediately.
- [x] Signed remote revocation application is idempotent for the exact pair.
- [ ] Eight protocol decisions reviewed by maintainers before endpoint merge.
- [x] Outbound Join uses #28 Transport and blocks unsafe resolved addresses.
- [x] The Transport pins the validated DNS answer while preserving URL SNI/Host.
- [ ] Outbound-proxy disposition is approved: equivalent authenticated pinning,
  or an explicit V1 exclusion with a specific actionable UI diagnostic.
- [x] Changed endpoints require a second explicit review.
- [x] Friends-only complete-v1 admission denies before capacity/inference on
  the canonical #30 implementation.
- [ ] Friends-only stream-v1 route uses the same Friend ACL on the final stacked
  commit; complete-route HMAC cannot authenticate the stream route; post-revoke
  next-stream admission is denied before capacity/inference.
- [x] Simulated two-app revocation delivery converges after offline/reconnect and
  is idempotent.
- [ ] Physical two-node revocation convergence is recorded across disconnect,
  restart/reconnect, and next-order denial.
- [ ] Webapp and Tauri create/review/join/QR/deep-link/list/revoke flows complete.
  - [x] Webapp create/list/cancel with explicit endpoint/scope/expiry review.
  - [x] Pasted-link offline review shows signature, fingerprint, network, every endpoint/address class, scope, and expiry.
  - [x] QR is generated locally with a pinned dependency and a zero-network automated assertion.
  - [x] Friend active/revoked details and high-risk local-first revoke are implemented.
  - [x] Keyboard labels, disabled-state explanation, and create/review focus transfer are tested.
  - [x] Outbound Join and received-relationship persistence are integrated and enabled through the local node.
  - [x] Tauri scheme, launch/running-instance forwarding, strict validation, and paste fallback are implemented.
  - [ ] Installed desktop physical scan/deep-link acceptance is complete on Windows/Linux/macOS.
- [x] Privacy export/erase behavior complete.
- [ ] Full backend/Webapp/Rust/Linux/macOS CI green on the exact commit.
- [ ] Two clean nodes pass use/revoke/next-order-denied including offline revoke.
- [ ] Product, development, test, and final acceptance evidence reviewed.

## Acceptance rule

Unchecked items are release blockers, not advisory follow-ups. This report may
change to **ACCEPTED** only after each item has exact-commit or physical evidence
and no invite/relationship secret appears in the evidence bundle.
