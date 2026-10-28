# Issue #30 development design

Status: implementation complete for local source-build development acceptance

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

The complete encrypted response is constructed before invite consumption. An
invalid or low-order X25519 key therefore returns the generic failure without
burning the one-use invite; the final consume still chooses exactly one winner
under a race.

The integration branch additionally adds local outbound Join through #28's
bounded `Transport.post_json`. It re-verifies the link, resolves and classifies
all endpoint addresses immediately before contact, signs the acceptor record
and freshness proof, decrypts the rotated credential locally, and verifies the
returned inviter identity/network/scope. A changed endpoint is stored only as
`pending_endpoint_review`; exact approval activates it and rejection deletes
the credential. `get_pinned_transport` dials the validated address directly
while retaining the original hostname for TLS SNI/certificate verification and
HTTP Host routing, closing the resolution/connection TOCTOU window. Join fails
closed when an outbound proxy owns DNS because that path cannot yet prove the
same pinning guarantee.

The Private AI integration persists an explicit `network`/`friends` publication
policy and advertises only that policy, never friend IDs. In `friends` mode the
complete-v1 HTTP route requires the per-friend body/path/timestamp/nonce HMAC
even when a mesh network key is present, and `ProviderService` independently
checks the signed Consumer peer against `private-ai.use` before capacity
acquisition or inference. The HMAC primitive also proves that a signature for
`/api/peer/llm/tasks` cannot be replayed to
`/api/peer/llm/tasks/stream`. The canonical #30 branch does not contain #23's
stream-v1 route, so route-level streaming ACL evidence must be produced on the
final stacked/integration commit. A task already claimed before revocation
keeps its idempotent result; every new task is denied immediately after local
revocation.

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
resolve-and-pin check at outbound Join; the pinned Transport preserves the
original hostname for TLS SNI/certificate and HTTP Host validation.

## Development acceptance completed on 2026-09-03

`scripts/issue30_two_node_e2e.py` launches a local HTTP Registry and two real
`rynmesh.peer_http` child processes with separate temporary homes, ports, peer
identities, and durable stores. A deterministic OpenAI-compatible HTTP service
replaces only the external model runtime; node discovery, Friend HMAC, encrypted
task exchange, persistence, revocation, and inference admission all cross the
real local HTTP boundaries.

The canonical harness proves create -> offline review -> Join -> both active ->
friends-only Private AI complete -> online revoke -> next order denied before
inference. It then creates a fresh relationship, stops the friend process,
records `remote_unreachable`, restarts the same durable home in a new process,
retries the signed revoke, observes remote convergence, and proves the next
order is denied before inference. It correlates reviewed endpoint/DNS/socket
addresses and scans homes/logs/privacy export for invite or relationship
secrets before deleting the temporary workspace.

The run exposed a real offline-path defect: a pinned socket connect refusal
escaped `FrontedHttpsTransport` instead of becoming a bounded `TransportError`.
`rynmesh/transport.py` now normalizes that failure and
`test_pinned_transport_bounds_socket_dial_failure` prevents regression. The
harness also waits for the provider's real 30-second capacity refresh when a
point-in-time publication briefly reports `available=0`.

## Remaining release/integration slices

1. Surface the current fail-closed outbound-proxy exclusion as a specific,
   actionable installed-app diagnostic; proxy support itself is a later design.
2. Run installed-desktop deep-link/scan acceptance on each declared supported
   release target and optional physical cross-host network hardening.
3. Complete normal merge/security review, accessibility review, and exact-commit
   backend/Webapp/Rust/package CI.

The final integration branch additionally runs the same real two-node flow with
`stream-v1`, verifies the exact stream protocol, then proves the next streaming
order is denied before inference after revoke. That closes the former integration
item without requiring remote Git.

These are release/integration gates. They do not invalidate the completed local
development acceptance or weaken the mandatory endpoint, secret, and revocation
security properties.

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
and focus transfer to the created/reviewed result heading. After a successful
offline review, Join delegates exclusively to the local node. A changed signed
endpoint set is held in `pending_endpoint_review` until the user approves that
exact set or rejects it and deletes the credential.

## Desktop deep link

The bundle statically registers `rynmesh` with `tauri-plugin-deep-link` 2.4.10.
The existing single-instance plugin is first and enables its `deep-link` feature,
so Windows/Linux second-instance arguments become the same event used by macOS.
The frontend reads both launch and running-instance URLs, validates the exact
bounded join format, and places one bearer only in module memory. App navigation
uses `/peers` without a link query; `FriendMeshPanel` consumes the bearer once
and still requires local offline verification before any endpoint contact.

## Revocation delivery and privacy lifecycle

Local revoke deletes authorization and the relationship secret before network
delivery. The signed notice is posted through a freshly resolved and pinned
reviewed endpoint. Failure persists only `remote_unreachable`, never response
content; the local API and Friend panel can retry the same idempotent notice
after reconnect. A hostname is never allowed to gain private-network reach on
retry merely because DNS changed.

Privacy status/export includes only public invite and friend projections. The
export contains neither bearer links nor relationship secrets. Explicit
`friends` erase first revokes locally and attempts signed notice delivery, then
atomically removes invites, friendships, revocations, nonces, and credentials.

The strict completion audit found and fixed one local error-boundary defect:
`pinned_proxy_unsupported` previously escaped the Join handler as an unhandled
Transport error. Join now returns the bounded `friend_join_failed` response
before constructing an outbound client, and the invitation remains unused.
See `ISSUE_30_STRICT_COMPLETION_AUDIT.md` for the remaining proof boundary.
