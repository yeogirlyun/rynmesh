# Issue #30 development design

Status: core security and Webapp safety/review slices implemented; outbound Join and service ACL pending

## Architecture

`rynmesh/friends.py` owns protocol validation and durable state. Public friend
records live in `friends.json`; relationship credentials live only in the
owner-restricted `friends.secrets.json`. Writes use same-directory temporary
files plus `os.replace`, and a process/thread lock serializes invitation
consumption. A stale crash lock is recovered after 30 seconds.

The module exposes four boundaries:

- signed `rynmesh.friend-invite.v1` encoding and offline verification;
- atomic invite cancellation/consumption and credential rotation;
- `rynmesh.friend-auth.v1` HMAC binding peer, method, path, timestamp, nonce,
  and body digest, with persisted replay rejection;
- signed, idempotent `rynmesh.friend-revocation.v1` local/remote handling.

`peer_http.py` adds local create/review/list/cancel/revoke APIs and one narrowly
public `/api/peer/friends/accept` route. Invalid, expired, cancelled, replayed,
or rate-limited requests all receive the same generic 404. The endpoint accepts
a signed current peer record plus a fresh signed acceptance binding and returns
the rotated credential encrypted to the acceptor's X25519 key. Other peer
routes continue to require `RYNMESH_NETWORK_KEY` when configured.

## Secret boundaries

- The raw invite secret appears only in the signed link and in the acceptance
  request. The persisted invite contains an scrypt hash with a random salt.
- Local review removes the raw secret from its response.
- The relationship secret is never returned by list APIs, Registry, errors, or
  diagnostics. Acceptance returns it only inside X25519/ChaCha20-Poly1305
  ciphertext.
- Revocation deletes the local relationship secret before delivery is tried.

## Endpoint safety

Offline parsing rejects credentials, fragments, unsupported schemes,
localhost, link-local, multicast, unspecified, and metadata literals. Private
IP literals require explicit LAN review. Hostnames require a second
resolve-and-pin check at outbound Join; that transport integration is pending.

## Remaining implementation slices

1. Stack this branch on the accepted #28 Transport POST implementation.
2. Add outbound Join through `Transport.post_json`, resolve-and-pin protection,
   endpoint-change second review, and received-relationship persistence.
3. Add friend-auth middleware only to explicitly allowed peer/service routes.
4. Add signed best-effort remote revocation delivery and retry status.
5. Add `friends` Provider policy and pre-capacity Private AI ACL enforcement.
6. Complete Webapp Join after the create/review/list/cancel/revoke UI and local QR slice.
7. Add Tauri `rynmesh://` forwarding and paste fallback.
8. Include sanitized friend records in privacy export and revoke/delete in erase.
9. Run two-node and accessibility acceptance.

No merge should describe the Issue as complete before all nine slices and the
acceptance report are green.

## Webapp implementation slice

`FriendMeshPanel` is mounted in Peers but keeps friendship language and actions
separate from the existing identity trust-root drawer. `NodeClient` now exposes
the six local-control API operations for friends/invites in live and fixture
modes. No browser call is made to a peer or QR service.

Endpoint classification is a display/risk aid only. The local node remains the
authority for signed offline verification and endpoint policy. Hostnames are
shown as unresolved, private literals require explicit review, and local,
link-local, unsupported, or invalid endpoints prevent invitation creation.
Review never decodes or trusts the bearer link in browser code.

QR matrices come from exact dependency `qrcode@1.5.4`, locked in
`package-lock.json`, and are rendered into an SVG data URL locally. The SVG path
contains only matrix coordinates; link contents are not interpolated into SVG
markup. `@types/qrcode@1.5.5` is also pinned exactly.

Keyboard users receive native labeled form controls, disabled-state guidance,
and focus transfer to the created/reviewed result heading. Join is rendered
disabled with an explanatory description until outbound Join is real.
