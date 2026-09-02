# Issue #30 acceptance report

Decision: **NOT YET ACCEPTED**

Branch: `codex/issue-30-friend-mesh`

Date: 2026-09-02

## Current result

The security/store/public-acceptance foundation is implemented and has focused
automated evidence. The complete product is not accepted because outbound
Join, friends-only Private AI enforcement, remote revocation delivery, Webapp,
QR/deep-link handling, and two-node physical acceptance remain outstanding.

Core-slice evidence: 8/8 focused tests and Ruff passed; the full Windows run
reported 527 passed, 3 skipped, and 8 known platform/pre-existing failures with
no Friend Mesh failure. Linux exact-commit CI remains required.

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
- [ ] Privacy export/erase behavior complete.
- [ ] Full backend/Webapp/Rust/Linux/macOS CI green on the exact commit.
- [ ] Two clean nodes pass use/revoke/next-order-denied including offline revoke.
- [ ] Product, development, test, and final acceptance evidence reviewed.

## Acceptance rule

Unchecked items are release blockers, not advisory follow-ups. This report may
change to **ACCEPTED** only after each item has exact-commit or physical evidence
and no invite/relationship secret appears in the evidence bundle.
