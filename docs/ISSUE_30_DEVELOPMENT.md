# Issue #30 development design

Status: core security slice implemented; end-user flow and service ACL pending

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
HTTP route requires the per-friend body/path/timestamp/nonce HMAC even when a
mesh network key is present, and `ProviderService` independently checks the
signed Consumer peer against `private-ai.use` before capacity acquisition or
inference. A task already claimed before revocation keeps its idempotent result;
every new task is denied immediately after local revocation.

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

1. Add signed best-effort remote revocation delivery and retry status.
2. Add Webapp create/review/join/list/revoke and a reviewed local QR dependency.
3. Add Tauri `rynmesh://` forwarding and paste fallback.
4. Include sanitized friend records in privacy export and revoke/delete in erase.
5. Run two-node and accessibility acceptance.

No merge should describe the Issue as complete before all remaining slices and the
acceptance report are green.
