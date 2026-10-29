# Issue #30 product specification: Friend Mesh

Status: local source-build development acceptance passed; release validation pending
Issue: https://github.com/yeogirlyun/rynmesh/issues/30

## User outcome

A node owner can invite a known person, let that person review the exact node,
network, endpoint, expiry, and permission before contact, establish a scoped
friend relationship, and revoke it locally in one action. Friendship is an
authorization relationship; it is never an identity trust root.

## V1 reachability promise

V1 supports a reviewed same-LAN endpoint or an already publicly reachable
HTTPS endpoint. It does not promise NAT traversal from an invite link. A
hostname remains unresolved during offline review and must pass DNS/rebinding
checks immediately before outbound contact. Cross-public-egress P2P claims
remain gated by Issue #22 physical acceptance.

An explicitly configured outbound proxy is outside the current V1 Join contract
and is never silently used. The node fails closed because the proxy would own
DNS and the current Transport seam cannot prove that the reviewed address was
the address contacted. Proxy support requires authenticated pinning in a later
wire version; an installed release should expose the bounded failure as an
actionable diagnostic.

## User flows

### Create

The owner chooses a permission and expiry, reviews advertised endpoints, then
creates a signed one-use link and an on-device QR. The default permission is
`private-ai.use`; the default expiry is 15 minutes and the maximum is 24 hours.
Outstanding invitations can be cancelled. The raw link is shown only in the
creation session.

### Review and join

Scanning, deep-linking, or pasting opens the same offline review. Before any
network contact it shows signature state, inviter fingerprint, node name,
network, every endpoint and address class, permissions, expiry, and the V1
reachability warning. Join atomically consumes the invitation and rotates to a
different relationship credential.

### Diagnose and revoke

Friends show active/pending/revoked state, last contact, reviewed endpoints,
permissions, and safe delivery status. Revoke first removes local access, then
best-effort sends an idempotent signed notice. Offline delivery never delays
local denial. Tasks admitted before revocation may finish; later admission is
denied.

## Security and privacy decisions

1. Friendship and `trusted_roots` are separate.
2. No global network key, local control token, or model API key enters a link.
3. An invitation is signed, random, short-lived, one-use, and cancellable.
4. Offline review happens before contact.
5. Local revocation is authoritative.
6. Permissions are explicit and non-transferable.
7. A Provider may publish `network` or `friends` access policy.
8. V1 reachability is LAN/already-public only; relay acceptance is a later wire version.

These are the implementation baseline for this isolated branch. Normal code
review remains merge governance, not a user-facing functional acceptance item.

## Non-goals

- friend search, recommendations, money, file access, egress, or agent rights;
- distributing a friend list through Registry;
- silently trusting changed endpoints;
- hosted QR generation or URL shortening;
- exposing `/api/local` to a peer.

## Development acceptance and release hardening

Development acceptance requires two isolated node processes and homes to pass
create, offline review, one-time Join, friends-only Private AI use, online
revoke, offline/restart retry convergence, and next-order denial before
inference over real local TCP/HTTP boundaries. That bar is now met by the
sanitized no-Docker harness evidence.

Physical cross-host networking, signed installed-package deep-link/QR dispatch,
the final #23 streaming stack, and CI/package evidence for the project's
declared supported release targets remain release-hardening gates. The original
Issue does not independently require two physical machines, all three desktop
OSes, or a separate maintainer sign-off artifact, so those are not retroactive
functional blockers. DNS pinning, fail-closed proxy behavior, secret hygiene,
and immediate local revocation remain mandatory security requirements.

## Webapp slice delivered on 2026-09-02

The Peers screen now has a visually and semantically separate Friend Mesh area.
It supports scoped, expiring invitation creation; explicit endpoint-risk review;
on-device QR and copy; outstanding-invite cancellation; offline pasted-link
review; and active/revoked friend inspection and high-risk revocation. Offline
review shows the local-node signature verdict, fingerprint, network, every
endpoint and its address class, permission scope, and expiry before any contact.

After offline review, Join calls only the local node. The node performs the
outbound Transport request, DNS resolve-and-pin checks, one-use credential
rotation, and relationship persistence. If the inviter returns changed signed
endpoints, the browser requires a second exact approve/reject decision; reject
deletes the local credential. Editing the invitation or LAN-risk choice clears
the old review so a different link cannot inherit it.

The desktop bundle registers `rynmesh://`. A launch URL or running-instance URL
is accepted only when it exactly matches the bounded `rynmesh://join/<base64url>`
shape, then is held once in memory and opens the same offline-review form. The
bearer is never placed in browser history or a router query. Pasting remains the
cross-platform fallback. Installed-desktop scan/deep-link evidence remains a
release gate; local source-build development acceptance is recorded in
`docs/evidence/issue30-local-two-node-e2e.json`.
