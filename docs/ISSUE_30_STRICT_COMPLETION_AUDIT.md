# Issue #30 strict completion audit

Decision: **NOT YET ACCEPTED**

Audit date: 2026-09-03

Scope: GitHub Issue #30, the product/development/test/acceptance documents, the
canonical #30 code and tests, and the cross-feature contracts needed from #23
and #28. This audit distinguishes implemented behavior, locally automated
proof, and release acceptance. A checked unit/integration test is not treated as
physical or installed-package evidence.

## Traceability result

| Area | Implementation and local proof | Strict result |
| --- | --- | --- |
| Invite/create/review/consume | Signed, bounded, short-lived one-use invite; scrypt hash at rest; offline review; atomic consume; rotated X25519-encrypted credential; tamper/expiry/cancel/race tests | Locally proven |
| Endpoint and DNS safety | All resolved addresses classified; unsafe/mixed answers rejected; validated address pinned while URL Host/SNI is retained; actual Host/connect split tested | Locally proven; physical socket correlation pending |
| Outbound proxy | Explicit Rynmesh proxies are rejected because the proxy would own DNS. The audit test proves rejection before client construction, relationship write, or invite consumption. Join now bounds the resulting Transport error. | Fail-closed proven; product/maintainer disposition pending |
| Web QR and offline review | Pinned local QR dependency, zero-fetch test, all signed fields/address classes shown, Join delegated only to local API | Locally proven |
| Installed deep link | Static scheme, plugin wiring, strict bounded parser, in-memory one-use handoff, cold/runtime URL mocks | Source/unit proof only; installed OS dispatch pending |
| Two-node Join | Two independent stores/apps exchange through an in-memory bridge and persist the active relationship | Simulation only; physical network proof pending |
| Revocation | Local secret removed before delivery; signed notice; bounded offline error; retry/idempotence; DNS rechecked; simulated convergence | Local semantics proven; physical disconnect/restart convergence pending |
| Privacy export/erase | Export excludes bearer/relationship secrets. Erase remains local-authoritative if notice delivery fails and writes exact empty invite/friend/revocation/nonce/credential schemas. | Locally proven |
| Friends-only Private AI | Complete-v1 route and Provider admission denial are covered. HMAC primitive binds the stream path separately from complete. | Stream-v1 route proof pending on final #23+#30 commit |
| CI/release | Focused backend and full Web evidence exists; historical Rust metadata resolved. Current Windows full backend has documented platform failures. | Exact-commit three-OS CI/package matrix pending |
| Protocol governance | Eight product/security decisions are written down. | Maintainer sign-off pending |

The GitHub Issue remains open and requires invited-friend Private AI use plus
safe invitation/revocation UX. The implementation materially covers that scope,
but the release definition in the four canonical documents adds necessary
network, installed-app, and exact-commit evidence that is not reproducible on a
single Windows worktree.

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

## Required external evidence

### A. Maintainer protocol decision record

Attach an approved review comment or ADR at an exact commit covering all eight
decisions in `ISSUE_30_PRODUCT.md`, invitation/revocation wire versions, the
LAN/already-public reachability promise, and one of these proxy outcomes:

- an authenticated proxy mechanism that proves the contacted address matches
  the reviewed resolution; or
- an explicit V1 exclusion, a stable error code, and an actionable Web/desktop
  diagnostic telling the user how to use a direct supported endpoint.

### B. Two clean physical nodes

Use two newly initialized homes and two machines/VMs with distinct peer IDs.
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

An in-process `TestClient` bridge is not acceptable for this item.

### C. Installed desktop deep link and QR

For signed Windows, Linux, and macOS packages, record installer/package hashes
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
Cargo metadata/check/test, and platform packaging on Windows, Linux, and macOS.
No known platform failure may be counted green. Publish test reports, package
hashes, the physical evidence above, accessibility keyboard/focus results, and
secret-scanned sanitized logs. Then obtain product, development, test, and
final acceptance review against that same commit.

## Acceptance rule

The decision stays **NOT YET ACCEPTED** until A-D are attached to the exact
integrated commit. If proxy use is excluded from V1, that exclusion must be an
approved product behavior with a specific user diagnostic; silent fallback or
a generic server failure is not acceptable.
