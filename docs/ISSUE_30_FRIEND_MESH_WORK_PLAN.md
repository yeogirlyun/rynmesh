# Issue #30 work plan: Friend Mesh invite links, QR joining, and revocation

Status: design-first; implementation-ready only after security decisions are approved  
Issue: https://github.com/yeogirlyun/rynmesh/issues/30  
Recommended order: after #28/#24/#25/#23 mainline work and security approval

## 中文执行摘要

Rynmesh 已有签名节点身份、Registry 发现、X25519 消息密钥和 Private AI 服务，
但“发现到一个节点”不等于“这是我的朋友”。当前没有好友关系存储、一次性邀请、
按好友授权、撤销协议或真实可用的 Peers 信任操作；`trusted_roots` 也只是身份
证据策略，不能拿来代替好友关系。

第一版 Friend Mesh 应使用短期、一次性的签名邀请。接受者在连接前必须看到并
确认网络、节点指纹、端点、权限和有效期。接受成功后双方保存独立的好友记录和
可轮换的点对点访问凭证；邀请秘密立即失效。撤销先在本地生效，再尽力通知对方，
并立即阻止被撤销节点使用仅限好友的 Private AI。

不能把全网共用的 `RYNMESH_NETWORK_KEY` 塞进邀请链接：它无法按好友撤销，泄漏
后会开放整个私有 mesh。二维码必须在本地生成，不得调用第三方二维码服务。

## 1. Verified current state (2026-09-02)

- GitHub Issue is open with `status:available` and has no matching online or
  local branch, PR, or implementation commit.
- Signed Ed25519 peer records already carry peer ID, name, endpoints, network,
  capabilities, and metadata.
- Peer discovery verifies signatures and can use Registry, bootstrap files,
  URLs, or peer exchange.
- X25519 peer messaging keys and encrypted peer/LLM payloads already exist.
- Private AI request envelopes identify and sign the Consumer peer.
- The Peers screen can inspect discovered nodes, but its trust/quarantine
  buttons currently only show notifications; they do not persist a decision.
- `trusted_roots` controls identity evidence policy. It is not a bilateral
  friend list or service ACL.
- `RYNMESH_NETWORK_KEY` is one shared mesh-wide active-probe credential.
- There is no friend store, invite store, one-time acceptance endpoint,
  per-friend credential, scoped capability grant, revocation record, or QR/deep
  link handler.

The cryptographic identity foundation is reusable, but the authorization layer
required by this Issue does not exist.

## 2. Security decisions requiring approval

Approve these decisions on the Issue before endpoint implementation:

1. **Friendship is separate from trusted-root status.** Accepting an invite
   never elevates the inviter to a trust root and never accepts their third-
   party attestations automatically.
2. **No global network key in links.** Invites create a scoped relationship
   credential; they do not reveal or replace `RYNMESH_NETWORK_KEY`.
3. **Bearer possession authorizes one acceptance.** Invites are random,
   short-lived, one-use, and locally revocable before use.
4. **Explicit pre-contact review.** The accepting user sees signed identity,
   network, endpoints, requested permissions, expiry, and risk warnings before
   their node contacts any endpoint.
5. **Local revocation is authoritative.** Revoking immediately removes local
   service access even when the remote node is offline. Remote notification is
   useful but not required for local safety.
6. **Permissions are scoped.** Initial scope is `private-ai.use` plus basic
   peer messaging/discovery as separately shown; no egress, filesystem, agent,
   trust-root, or transferable-credit permission is implied.
7. **Friend-only service mode is explicit.** Existing development/demo network
   behavior remains compatible, while a Provider may publish Private AI as
   friends-only and enforce the ACL on every request.
8. **The v1 reachability boundary is explicit.** Either limit v1 joining to
   same-LAN or already-publicly-reachable reviewed endpoints, or design an
   end-to-end encrypted Registry/Relay/P2P acceptance channel. The product must
   not promise cross-network one-click joining while invite acceptance still
   requires a directly reachable HTTP endpoint. If strict public P2P is part of
   the promise, #22's distinct-public-egress physical acceptance is a release
   prerequisite.

If maintainers choose a different credential, relationship, or reachability
model, update the wire version and threat model before code.

## 3. Goals

- Create a short-lived invite link and locally rendered QR code.
- Parse and verify an invite without contacting the inviter.
- Show a safe review screen before joining.
- Accept exactly once and establish a bilateral friend record.
- Give the accepted friend a scoped way to reach allowed peer/service routes
  without disclosing a mesh-wide secret.
- Revoke locally in one action and deliver a signed remote notice best effort.
- Make friend status, endpoint health, last contact, permissions, and revocation
  understandable to non-technical users.
- Allow an invited friend to use a friends-only published Private AI model.
- Preserve signed identities, node-only control APIs, E2EE task bodies, and
  active-probe resistance.

## 4. Non-goals

- Do not build open-network friend search or social recommendations.
- Do not add friend-attributed For You ranking; that is P2 item 2.
- Do not share the global network key, local-control token, model API key, or
  Registry credentials.
- Do not make friends trusted roots.
- Do not add multi-user egress, agent permissions, file-system access, or money.
- Do not depend on a hosted QR-generation or link-shortening service.
- Do not promise delivery of revocation to an offline peer before local access
  has already been cut.
- Do not expose `/api/local` remotely.

## 5. Threat model

The design must handle:

- a leaked or forwarded unused invite link;
- replay of an already accepted invite;
- forged peer identity, endpoint substitution, or modified permission scope;
- QR/link content that targets loopback, link-local, metadata, or unexpected
  private endpoints;
- an active scanner probing the acceptance endpoint;
- an inviter behind NAT whose reviewed HTTP endpoint is not reachable from the
  acceptor's network;
- a former friend continuing to use an old credential after revocation;
- a malicious accepted friend sending requests signed as another peer;
- acceptance races from two devices;
- offline inviter/acceptor and partial bilateral state;
- logs, analytics, browser history, screenshots, and crash reports leaking an
  invite secret;
- clock skew around expiry;
- denial of service through invite creation or acceptance attempts.

Possession of an unused invite is intentionally a limited bearer capability.
The owner mitigations are short expiry, one use, visible outstanding invites,
and immediate cancellation.

## 6. Versioned invite format

Use a signed, base64url-encoded payload carried by a custom link such as
`rynmesh://join/<payload>`. Provide a copy/paste fallback when OS deep linking
is unavailable.

Suggested signed fields:

```json
{
  "version": "rynmesh.friend-invite.v1",
  "invite_id": "invite_<random>",
  "inviter_peer_id": "<ed25519 public key>",
  "node_name": "Alice's Ryn",
  "network_id": "rynmesh-main",
  "endpoints": ["https://example:8791"],
  "permissions": ["private-ai.use"],
  "issued_at": "...",
  "expires_at": "...",
  "one_time_secret": "<at least 256 random bits>"
}
```

The inviter signs the complete payload. Locally persist only a slow/keyed hash
of the one-time secret, never the raw link. Default expiry should be short
(recommended 15 minutes), maximum expiry bounded (recommended 24 hours), and
default use count exactly one.

The review screen verifies the signature and that
`inviter_peer_id == signed.public_key` before showing an identity as verified.
An invite with an unsupported version, invalid time, invalid signature, empty
endpoint, or broadened permission is rejected before network access.

## 7. Acceptance and relationship credential

### 7.1 Acceptance request

Add a narrowly exposed peer endpoint for invite acceptance. It must accept the
one-time invite secret as an alternative active-probe credential only for this
route. Invalid, expired, used, cancelled, or malformed requests return the same
generic not-found response and do not reveal Rynmesh.

The request includes:

- invite ID and proof of the one-time secret;
- the acceptor's signed peer record;
- acceptor X25519 public key;
- the exact requested permission list;
- a fresh nonce/timestamp signed by the acceptor.

The inviter atomically marks the invite used, verifies the peer record and
signature, records the friend, and creates a new random relationship secret.
Return that secret encrypted to the acceptor's X25519 key along with the
inviter's signed current peer record and confirmed permissions. Never continue
using the secret embedded in the link after acceptance.

### 7.2 Relationship authentication

Store the rotated relationship secret per friend with restricted file
permissions. Derive an HTTP authorization value using a domain-separated HMAC;
do not send the raw secret. Include peer ID, request method/path, timestamp,
nonce, and body digest in the MAC so captured headers cannot be replayed on
another route or body.

The peer middleware may accept either:

- the existing valid mesh-wide network key; or
- a valid, active per-friend request MAC for routes that explicitly allow it.

The route must still verify that the signed application payload's sender equals
the authenticated friend peer. A bearer/MAC alone cannot authorize an LLM task
signed by a different identity.

Extend `HttpPeerClient` after #28 to add the per-friend header through the
Transport seam. Do not create a new raw-urllib code path.

## 8. Local stores

Add a focused module such as `rynmesh/friends.py` with atomic JSON stores or the
repository's standard durable pattern.

### Invite record

- invite ID/version;
- secret hash only;
- issued/expiry/used/cancelled timestamps;
- signed payload hash;
- approved permissions and endpoints;
- accepted peer ID when used.

### Friend record

- peer ID and last verified signed peer record;
- display name, network, reviewed endpoints;
- granted and received permissions;
- relationship-secret material or protected reference;
- active/revoked/pending state;
- created, accepted, last-contact, and revoked timestamps;
- last safe delivery error code;
- revocation ID/version.

Writes must be atomic and locked. Never place relationship secrets in exported
diagnostics, API responses, logs, Registry metadata, or Webapp state.

Friend data needs explicit inclusion in personal-data export and erase behavior
without exporting live secrets by default. Erase/revoke semantics must be
documented separately.

## 9. Revocation

Revocation sequence:

1. require high-risk local confirmation showing peer and affected permissions;
2. atomically mark the local relationship revoked;
3. remove/rotate the relationship credential and deny new service requests;
4. cancel any unused invites created for that peer where identifiable;
5. create a signed `rynmesh.friend-revocation.v1` record containing revocation
   ID, both peer IDs, relationship/invite reference, timestamp, and reason code;
6. deliver it directly using the last reviewed endpoint and friend credential
   when possible;
7. retain body-free retry status for offline delivery;
8. on receipt, verify the signer is one side of the relationship and mark the
   remote record revoked.

Local denial does not wait for step 6. A later rediscovery or a new invite from
the same peer does not silently reactivate a revoked relationship; the owner
must explicitly accept a new invite.

The #27 background-worker registry is a suitable optional home for bounded
revocation retry after it merges, but it is not a prerequisite for immediate
local revocation.

## 10. Private AI integration

Add an explicit Provider access policy to local configuration and public
service metadata:

- `network` for current backward-compatible development behavior;
- `friends` to require an active friend with `private-ai.use` permission.

For friends-only mode, `ProviderService.handle()` checks the authenticated and
signed Consumer peer ID against the active Friend Store before capacity
admission or inference. Fail with a generic authorization result that does not
reveal model availability to unauthorized probes.

Revocation must take effect before the next admission. An already running task
may finish under the permission snapshot taken at admission, or be cancelled;
choose and document one rule. Recommended v1 behavior: allow an admitted task
to finish and bill normally, deny all later tasks, and show this in the revoke
confirmation.

Publishing a friends-only model must not place the friend list or friend IDs in
Registry metadata. Discovery can advertise `access_policy: friends`; actual
authorization happens only at the Provider.

## 11. Webapp and desktop flow

### 11.1 Create invite

Add a Friend Mesh area reachable from Peers:

- choose permissions (default `private-ai.use` when a model is published);
- choose a short expiry within policy;
- show current network and advertised endpoints;
- warn when no endpoint is reachable or an endpoint is LAN/private-only;
- create/copy link and render QR entirely on device;
- list outstanding invites with expiry and Cancel action.

Do not render the raw secret again after leaving the creation view.

### 11.2 Join

Register the `rynmesh://` scheme in Tauri with single-instance forwarding, and
provide a paste field fallback. The review screen shows:

- verified/unverified signature result and inviter fingerprint;
- inviter name, network, all endpoints and their address class;
- requested permissions in plain language;
- expiry and single-use status;
- what the inviter can and cannot access;
- Cancel and Join actions.

No endpoint is contacted until Join. Endpoint changes returned during
acceptance require a second explicit review rather than silent replacement.

### 11.3 Friends and revoke

Show active/pending/revoked state, last contact, endpoint health, granted
permissions, accessible services, diagnostics, and a high-risk Revoke action.
Do not reuse the existing “Trust as root” label or behavior.

Use a local QR library that performs no network request. Add a dependency
review and test that creation does not call an external QR/shortener service.

## 12. Implementation slices

1. Versioned invite/friend/revocation types, atomic stores, and unit tests.
2. Invite creation/cancel local APIs and safe public response models.
3. Acceptance endpoint, one-time atomic consume, X25519 credential rotation,
   replay protection, and endpoint validation.
4. Per-friend authenticated `HttpPeerClient`/middleware integration on top of
   #28.
5. Revocation local-first behavior and best-effort signed delivery.
6. Friends-only Private AI admission and publication policy.
7. Webapp create/review/join/list/revoke flows and local QR.
8. Tauri deep-link registration plus copy/paste fallback.
9. Two-node end-to-end and non-technical usability acceptance.

Keep the security/store/protocol slice separate from the large UI pull request
so it can be reviewed independently.

## 13. Test plan

### Protocol and storage

- valid invite creation, signature, expiry, cancellation, and one-time consume;
- concurrent double acceptance allows exactly one winner;
- wrong secret/signature/peer/network/permission/version/expiry fails closed;
- raw invite and relationship secrets never appear in persisted public fields,
  logs, errors, diagnostics, or API responses;
- relationship MAC rejects replay, body/path substitution, clock skew, and a
  mismatched signed sender;
- atomic restart recovery for pending/active/revoked records;
- local revocation denies immediately while remote is offline;
- signed remote revocation is idempotent and cannot revoke an unrelated pair.

### Endpoint safety

- credentials, fragments, unsupported schemes, link-local, metadata-service,
  loopback, and disallowed private endpoints are rejected or require the exact
  documented explicit LAN review;
- DNS resolution/rebinding protections match existing peer transport policy;
- invalid acceptance probes return generic not-found responses;
- rate and size limits prevent invite-endpoint abuse.

### Private AI

- an active friend with permission can use a friends-only model;
- a discovered non-friend, revoked friend, wrong permission, and spoofed peer
  cannot enter inference or consume capacity;
- Registry metadata contains no friend IDs or secrets;
- current `network` policy remains backward compatible;
- settlement remains idempotent and revocation does not create a free-running
  task loophole.

### Webapp/desktop

- link and QR are equivalent and generated without network calls;
- review appears before any endpoint contact;
- network, endpoints, fingerprint, permissions, and expiry are visible;
- deep link and paste fallback reach the same verified flow;
- create, cancel, accept, offline, used, expired, and revoke states are clear;
- accepting never changes `trusted_roots`;
- screen-reader, keyboard, focus, and high-risk confirmation behavior works.

### Two-node acceptance

On two clean nodes:

1. Provider publishes a friends-only model.
2. Consumer cannot order before friendship.
3. Provider creates a short invite; Consumer scans/pastes it and reviews.
4. Join succeeds once and both nodes show the relationship.
5. Consumer discovers and uses the Provider model successfully.
6. Provider revokes while Consumer is online; the next order is denied.
7. Repeat revocation with Consumer offline; local denial still applies and the
   remote state converges after reconnect.

Record sanitized evidence without invite or relationship secrets.

## 14. Acceptance criteria

- [ ] The eight security decisions in section 2 are explicitly approved before
  network endpoint implementation is merged.
- [ ] A non-technical user can create, review, accept, diagnose, and revoke a
  friend relationship without editing configuration files.
- [ ] Invite links are signed, versioned, short-lived, one-use, cancellable,
  rate-limited, and rendered as QR entirely on device.
- [ ] Join review shows verified fingerprint, network, every endpoint/address
  class, requested permissions, expiry, and clear Cancel/Join actions before
  any endpoint contact.
- [ ] Acceptance atomically consumes the invite once and rotates to a distinct
  per-friend relationship credential.
- [ ] No global network key, local-control token, raw invite secret, or raw
  relationship secret is shared, logged, exported, or placed in Registry.
- [ ] Friendship is stored separately and never changes `trusted_roots`.
- [ ] Per-friend request authentication rejects replay, wrong path/body,
  timestamp/nonce reuse, mismatched signed peer, and revoked credentials.
- [ ] Revocation cuts local authorization immediately, works while the remote
  peer is offline, and converges remotely through an idempotent signed notice.
- [ ] An active friend with `private-ai.use` can use a friends-only model;
  non-friends, revoked friends, wrong-permission friends, and spoofed peers
  cannot enter inference or consume capacity.
- [ ] All peer/service bodies remain node-mediated and end-to-end encrypted
  under existing privacy boundaries.
- [ ] Two clean nodes pass create → review → join → Private AI use → revoke →
  next-order denied, including a separate offline-revocation run.
- [ ] Protocol/storage/security tests, focused/full backend tests, ruff, Webapp
  tests, typecheck, production build, and desktop deep-link tests pass.

### Required acceptance evidence

- Link the approved security-decision comment/design revision.
- Attach sanitized concurrent double-acceptance, replay, expiry, cancellation,
  wrong-peer, wrong-permission, endpoint-safety, and offline-revocation tests.
- Attach a two-node acceptance report with peer IDs shortened and every invite/
  relationship secret redacted.
- Attach before/after evidence that `trusted_roots` is unchanged.
- Attach friends-only Provider admission evidence showing allowed and denied
  identities without exposing model prompt/output.
- Confirm QR generation made no third-party network request.
- Record the implementation commits, migration/rollback behavior, and any
  remaining limitations.

## 15. Readiness assessment

**Can coding start immediately: only partially.** Store types, threat-model
tests, UI wireframes, and invite parsing can begin, but network endpoints and
credentials should not be implemented until the eight security decisions in
section 2 are approved. #28 should merge before acceptance/service POSTs so the
feature uses the shared Transport and authentication headers. Cross-network
one-click joining additionally requires an approved encrypted acceptance path;
if that path relies on strict public P2P, #22 must pass its distinct-egress
physical acceptance first. The existing identity, discovery, messaging, and
Private AI foundations are otherwise sufficient after those design gates.
