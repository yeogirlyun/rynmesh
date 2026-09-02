# Issue #30 acceptance report

Decision: **NOT YET ACCEPTED**

Core branch: `codex/issue-30-friend-mesh`
Webapp slice branch: `codex/issue-30-friend-mesh-ui`

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
- [x] Changed endpoints require a second explicit review.
- [x] Friends-only Private AI admission denies before capacity/inference.
- [x] Revocation delivery converges after online and offline cases.
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
