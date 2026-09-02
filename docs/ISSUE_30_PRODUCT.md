# Issue #30 product specification: Friend Mesh

Status: implementation in progress; Webapp create/review/Join slice implemented
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

An explicitly configured outbound proxy is not silently used for Join in the
current V1. The node fails closed because the proxy would own DNS and the
current Transport seam cannot prove that the reviewed address was the address
contacted. Before Issue completion, maintainers must choose and document either
an authenticated proxy pinning design or a supported-V1 exclusion with a clear
user-facing diagnostic; a generic Join failure is not final product evidence.

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

These are the implementation baseline for this isolated branch. Maintainer
protocol review is still required before its public endpoint is merged.

## Non-goals

- friend search, recommendations, money, file access, egress, or agent rights;
- distributing a friend list through Registry;
- silently trusting changed endpoints;
- hosted QR generation or URL shortening;
- exposing `/api/local` to a peer.

## Release definition

The Issue is complete only after two clean physical nodes pass create, offline
review, one-time join, friends-only Private AI complete and streaming use,
online revoke, offline/reconnect revoke convergence, and next-order denial
before capacity/inference, with sanitized evidence and all automated checks
green on the exact integrated commit. Installed-package deep links and the V1
proxy disposition are part of that decision; in-memory clients and source
configuration inspection are supporting evidence, not substitutes.

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
cross-platform fallback. Installed-desktop scan/deep-link and physical two-node
acceptance remain.
