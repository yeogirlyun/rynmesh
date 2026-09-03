# Issue #30 strict completion audit

Development decision: **ACCEPTED (LOCAL SOURCE BUILD)**

Release decision: **NOT YET ACCEPTED**

Audit date: 2026-09-03

Scope: GitHub Issue #30, the product/development/test/acceptance documents, the
canonical #30 code and tests, and the cross-feature contracts needed from #23
and #28. This audit distinguishes implemented behavior, locally automated
proof, and release acceptance. A checked unit/integration test is not treated as
installed-package evidence; the new multiprocess harness is separately identified
from both in-process simulation and cross-host physical testing.

## Traceability result

| Area | Implementation and local proof | Strict result |
| --- | --- | --- |
| Invite/create/review/consume | Signed, bounded, short-lived one-use invite; scrypt hash at rest; offline review; atomic consume; rotated X25519-encrypted credential; tamper/expiry/cancel/race tests | Locally proven |
| Endpoint and DNS safety | All resolved addresses classified; unsafe/mixed answers rejected; validated address pinned while URL Host/SNI is retained; actual Host/connect split tested; multiprocess evidence correlates reviewed address with actual socket peer | Local development proven; cross-host hardening optional |
| Outbound proxy | Explicit Rynmesh proxies are rejected because the proxy would own DNS. The audit test proves rejection before client construction, relationship write, or invite consumption. Join bounds the resulting Transport error. | V1 fail-closed contract proven; installed diagnostic pending |
| Web QR and offline review | Pinned local QR dependency, zero-fetch test, all signed fields/address classes shown, Join delegated only to local API | Locally proven |
| Installed deep link | Static scheme, plugin wiring, strict bounded parser, in-memory one-use handoff, cold/runtime URL mocks | Source/unit proof only; installed OS dispatch pending |
| Two-node Join | Two independent `peer_http` processes, homes, identities and ports exchange over real TCP/HTTP and persist the active relationship | Local development proven |
| Revocation | Local secret removed before delivery; signed notice; bounded offline error; retry/idempotence; DNS rechecked; process stop/restart convergence and next-order denial before inference | Local development proven |
| Privacy export/erase | Export excludes bearer/relationship secrets. Erase remains local-authoritative if notice delivery fails and writes exact empty invite/friend/revocation/nonce/credential schemas. | Locally proven |
| Friends-only Private AI | Complete-v1 route and Provider admission denial are covered. HMAC primitive binds the stream path separately from complete. | Stream-v1 route proof pending on final #23+#30 commit |
| CI/release | Focused backend and full Web evidence exists; historical Rust metadata resolved. Current Windows full backend has documented platform failures. | Exact-commit supported-target CI/package matrix pending |
| Protocol governance | Eight product/security decisions are written down. | Normal merge review; not a functional blocker |

The GitHub Issue remains open and requires invited-friend Private AI use plus
safe invitation/revocation UX. The implementation materially covers that scope,
and the local source-build development bar is now proven. Installed-app,
streaming-integration, and exact-commit package evidence remain separate release
gates.

Earlier versions of these documents elevated two physical machines, mandatory
Windows/Linux/macOS execution, and a standalone maintainer approval artifact to
Issue acceptance criteria. Those were not explicit in Issue #30. They are now
classified as cross-host/release hardening and merge governance. This scope
correction does not relax DNS pinning, proxy fail-closed behavior, secret
non-disclosure, or immediate local revocation.

## Locally added audit proof

The strict audit adds these regression assertions:

1. A valid Friend HMAC for `/api/peer/llm/tasks` fails on
   `/api/peer/llm/tasks/stream`, while a separately signed streaming-path
   request verifies at the protocol primitive.
2. With `RYNMESH_HTTPS_PROXY` configured, Join fails before constructing an
   outbound peer client, creates no local friendship, and leaves the provider's
   invitation unused.
3. Friend privacy erase succeeds despite offline notice delivery and leaves
   exact empty public and secret schemas, including outstanding invitations,
   revocations, nonces, and relationship credentials.

Command results:

- 48 passed in the focused Friend/HTTP/Transport suite;
- 100 passed, 1 skipped in the expanded Friend/Transport/LLM suite;
- 51/51 Webapp tests, TypeScript lint, and production build passed;
- Ruff passed on all touched Python files; and
- full backend: 555 passed, 3 skipped, 8 documented Windows-platform failures
  (three executable-bit/WSL, three POSIX-mode, one `os.replace` reader race,
  and one `select()`-on-pipe failure), with no Friend Mesh failure.

Cargo was unavailable on this audit host, so locked metadata was not rerun. The
metadata result in the test plan predates this audit and is not counted as
exact-commit proof.

## Local multiprocess acceptance proof

Commit `c40403bfaf8ea14f968153dff6a79a51c2a28401` passed
`scripts/issue30_two_node_e2e.py` with two real node child processes, a local
HTTP Registry, separate temporary homes/ports, and a deterministic local HTTP
model service. The model service substitutes only external inference; every
friendship, discovery, encrypted-task, ACL, persistence and revocation boundary
uses the running nodes' HTTP APIs.

The sanitized artifact `docs/evidence/issue30-local-two-node-e2e.json` records
online and offline/restart revoke convergence, post-revoke denial before the
model call counter changes, reviewed address-to-socket correlation, empty
relationship-key sets after convergence, zero raw invite/link occurrences,
child-process cleanup, and temporary-home deletion. A real refused socket also
exposed and drove the `FrontedHttpsTransport` error-normalization regression fix.

## Remaining release/governance evidence

### A. Normal merge/security review

Review the documented decisions, invitation/revocation wire versions, the
LAN/already-public reachability promise, and explicit V1 proxy exclusion during
the normal merge/security process. This is governance, not an additional user
acceptance flow. A later proxy design would require one of these outcomes:

- an authenticated proxy mechanism that proves the contacted address matches
  the reviewed resolution; or
- an explicit V1 exclusion, a stable error code, and an actionable Web/desktop
  diagnostic telling the user how to use a direct supported endpoint.

### B. Optional cross-host network hardening

For release confidence, repeat on two machines/VMs with distinct peer IDs.
Record the exact commit, package hashes, OS versions, node IDs, network topology,
and UTC timestamps. Evidence must show:

1. Node A creates one invitation; Node B reviews signature fingerprint,
   network, every endpoint/address class, scope, and expiry before contact.
2. Resolver capture lists every answer and socket capture/log proves the actual
   peer address is one reviewed, allowed answer while TLS SNI/certificate and
   HTTP Host remain bound to the invitation hostname.
3. Join succeeds once; replay fails; both nodes store active public records and
   no evidence/log/export contains the bearer or relationship credential.
4. With `access_policy=friends`, the friend succeeds on both complete-v1 and
   stream-v1 Private AI; an unpaired peer is denied before capacity/inference.
5. Online revoke immediately deletes local authorization, converges remotely,
   and the next complete and streaming orders are denied before
   capacity/inference.
6. Repeat with the remote offline, restart both sides as appropriate, reconnect,
   deliver the same signed revocation idempotently, and prove the next orders are
   denied. Capture both secret stores after convergence using only presence/
   absence assertions, never secret values.

The committed local multiprocess harness already satisfies development
acceptance; this section adds cross-host topology evidence only.

### C. Installed desktop deep link and QR

For signed packages on each declared supported release target, record package hashes
and perform both cold-app and already-running dispatch of a real
`rynmesh://join/<base64url>` link. On each platform prove:

- OS registration opens the installed app and transfers the bearer once into
  the Peers offline-review flow without browser-history/router-query storage;
- a camera/OS QR scan reaches the same review, and paste remains available;
- invalid scheme/host/path, query/fragment, oversized payload, tampered,
  expired, and cancelled links fail without endpoint contact;
- accepted links still require the explicit review and Join action; and
- sanitized process/app logs and crash/relaunch behavior do not persist or
  replay the bearer.

Source configuration and mocked plugin callbacks do not satisfy this item.

### D. Exact integrated CI and acceptance bundle

Choose one immutable final commit containing #23 streaming, #28 Transport, and
#30 Friend Mesh. Run full backend, Web tests, lint, production build, locked
Cargo metadata/check/test, and packaging on each declared supported target.
No known platform failure may be counted green. Publish test reports, package
hashes, any cross-host hardening evidence, accessibility keyboard/focus results,
and secret-scanned sanitized logs. Then obtain product, development, test, and
final acceptance review against that same commit.

## Acceptance rule

The local development decision is accepted. The release decision stays **NOT
YET ACCEPTED** until the required installed-app, integrated streaming, and
exact-commit package evidence is attached. Cross-host repetition and review
artifacts strengthen the release but do not retroactively redefine Issue #30.
Proxy Join remains fail-closed; silent fallback is never acceptable.
