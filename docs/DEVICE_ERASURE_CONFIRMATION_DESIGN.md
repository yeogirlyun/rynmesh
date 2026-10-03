# Owned-device cleanup proposals and verified completion

Status: implemented in PR #69 for issue #68, with receiving-owner review. Ordinary device sync never grants unattended deletion authority.

## Concrete scope

The initiating owner selects one cleanup category and 1–16 existing owned-device pairings. Each receiver reviews **all local records in that category**, including records absent from the requester. Its existing cleanup preview binds the exact local generation, records, managed backups and phases. Only a second, destructive confirmation on that receiver starts that plan. The initiating computer is not implicitly a target.

This is category-wide cleanup coordination. The earlier sketch proposed cross-device matching of selected record IDs and versions; that mapping is not implemented or claimed. It is unnecessary for the existing whole-category privacy actions and must not be inferred from a completion receipt. Both proposal and receiving review explicitly disclose this boundary.

Supported typed adapters:

| Category | Required local completion phases |
|---|---|
| Ask Ryn history | source, replica, known backups, search index, retained order results |
| Bookmarks and reading history | source, replica, known backups, search index |
| Private imported documents | source catalogue and reviewed managed files |
| Friend feed | source metadata and known backups |
| Friend sharing cards | source metadata and reviewed legacy files |
| Offline downloads | metadata/worker fence and reviewed managed files |

No arbitrary paths, remote file discovery, friend-device deletion, browser cleanup or operating-system snapshot deletion is accepted. Shared-list copies and unrelated categories are outside these adapters. Exports, independent friend copies, user-created backups and newly created generations are expressly excluded.

## Durable protocol

The initiating journal saves an immutable target set and category before dispatch. Its manifest digest includes request ID, each pairing identity, target peer and normalized local/remote policy epoch. A separate signed, sealed protocol uses the existing owned-device identity keys, pairing capability and a dedicated domain-separated channel. Responses bind the fresh nonce and complete request hash, preventing an old or different receipt from completing a request.

Peer requests can only persist a proposal or return its current receipt. They never invoke cleanup. Owner-only APIs separately preview, approve, resume the exact approved plan or decline. The approval token is durably stored before calling a cleanup adapter. A crash between cleanup commit and receipt commit safely retries that same token. Existing cleanup journals preserve phase progress and deletion barriers.

All originally selected targets must return authenticated completion for the exact category, manifest and current pairing epoch. Zero targets are rejected. An offline, declined, unsupported, stale-policy, partial or otherwise unconfirmed target cannot disappear from the aggregate or count as success. The worker contacts one unresolved target per turn and does no destructive work. Final receipts are retained in the encrypted journal; completed target states are historical, as-of confirmations.

The receiver's explicit local approval authorizes its local cleanup operation. Pairing is checked before execution and before issuing a completion receipt. A local atomic phase already authorized by that owner may finish if pairing is revoked during it; no receipt is issued under the changed epoch. Declining after execution starts cannot restore deleted data. Locks are never held across network I/O, and the coordinator does not invert the existing cleanup/pairing lock order.

## Recovery and truthfulness

A changed local plan fails the existing review-token comparison. It is not broadened automatically: decline the old request and create/review a new one. A partially completed plan only offers continuation of the same approval. Changed backups/files use the existing local Privacy controls for an additional review before continuation. Active tasks or unavailable retained-result cleanup prevent completion.

The old source-only cleanup receipts continue to expose their original `remote_confirmed: false`; they have no target ledger. The new aggregate jobs alone expose `remote_confirmed: true` after a nonempty immutable target set is confirmed. The UI says selected node-managed copies confirmed, never complete device wipe or proof that future copies cannot exist.

The journal retains up to 128 incoming and 128 outgoing requests within 4 MiB; at most 16 targets per request and 64 KiB per wire envelope. It refuses new work at capacity rather than evicting unfinished targets or anti-replay receipts. Peer transport uses the existing bounded five-second request deadline. Owner browser requests have a 30-second deadline and retries retain request identity.

## APIs and delivery

- `GET /api/local/device-erasure`: owner-only, no-store aggregate and incoming status.
- `POST /api/local/device-erasure/action`: bounded JSON actions for begin, preview, approve, resume, reject and exchange; identities and review tokens remain in bodies.
- `POST /api/peer/device-erasure`: signed/sealed proposals and receipt polling; no owner approval action.
- My devices presents the selected targets, local review counts, exclusions and per-device outcomes.

See `docs/acceptance/device-erasure/README.md` for tests, actual HTTP evidence and remaining physical-environment acceptance. Maintainer review and merge remain separate from implementation completion.
