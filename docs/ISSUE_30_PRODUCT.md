# Issue #30 product specification: Friend Mesh

Status: implementation in progress; Webapp safety/review slice implemented, full Join pending
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

The Issue is complete only after two clean nodes pass create, offline review,
one-time join, friends-only Private AI use, online revoke, offline revoke, and
next-order denial, with sanitized evidence and all automated checks green.

## Webapp slice delivered on 2026-09-02

The Peers screen now has a visually and semantically separate Friend Mesh area.
It supports scoped, expiring invitation creation; explicit endpoint-risk review;
on-device QR and copy; outstanding-invite cancellation; offline pasted-link
review; and active/revoked friend inspection and high-risk revocation. Offline
review shows the local-node signature verdict, fingerprint, network, every
endpoint and its address class, permission scope, and expiry before any contact.

The Join control intentionally remains disabled and explains that outbound
Transport, DNS resolve-and-pin, and received-relationship persistence are not
integrated. This is a safety boundary, not a simulated success state. Tauri
deep-link forwarding is also outside this slice, so Issue #30 remains incomplete.
