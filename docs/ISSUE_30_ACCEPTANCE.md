# Issue #30 acceptance report

Decision: **NOT YET ACCEPTED**

Core branch: `codex/issue-30-friend-mesh`
Webapp slice branch: `codex/issue-30-friend-mesh-ui`

Date: 2026-09-02

## Current result

The security/store/public-acceptance foundation is implemented and has focused
automated evidence. The complete product is not accepted because outbound
Join, friends-only Private AI enforcement, remote revocation delivery, Tauri
deep-link handling, and two-node physical acceptance remain outstanding.

The Webapp create/offline-review/list/cancel/revoke and local-QR slice is now
implemented and automated, but this does not change the overall decision:
outbound Join and Tauri deep links are deliberately unavailable.

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
- [ ] Outbound Join uses #28 Transport with resolve-and-pin/rebinding defense.
- [ ] Changed endpoints require a second explicit review.
- [ ] Friends-only Private AI admission denies before capacity/inference.
- [ ] Revocation delivery converges after online and offline cases.
- [ ] Webapp and Tauri create/review/join/QR/deep-link/list/revoke flows complete.
  - [x] Webapp create/list/cancel with explicit endpoint/scope/expiry review.
  - [x] Pasted-link offline review shows signature, fingerprint, network, every endpoint/address class, scope, and expiry.
  - [x] QR is generated locally with a pinned dependency and a zero-network automated assertion.
  - [x] Friend active/revoked details and high-risk local-first revoke are implemented.
  - [x] Keyboard labels, disabled-state explanation, and create/review focus transfer are tested.
  - [ ] Outbound Join and received-relationship persistence are integrated and enabled.
  - [ ] Tauri deep-link forwarding and physical scan/deep-link acceptance are complete.
- [ ] Privacy export/erase behavior complete.
- [ ] Full backend/Webapp/Rust/Linux/macOS CI green on the exact commit.
- [ ] Two clean nodes pass use/revoke/next-order-denied including offline revoke.
- [ ] Product, development, test, and final acceptance evidence reviewed.

## Acceptance rule

Unchecked items are release blockers, not advisory follow-ups. This report may
change to **ACCEPTED** only after each item has exact-commit or physical evidence
and no invite/relationship secret appears in the evidence bundle.
