# Rynmesh Requirements

Functional and non-functional requirements derived from
[`RYNMESH_VISION.md`](RYNMESH_VISION.md) (the North Star) and
[`ARCHITECTURE.md`](ARCHITECTURE.md) (the built system). Each requirement is
labelled by its M-milestone in the vision's M0-M6 sequencing. **Implemented**
items reference the module that satisfies them; **gated** items reference the
vision open question they answer.

## Founding principles (binding on every requirement)

- **First principle**: anyone with valuable content, app, or service earns
  adequately for it; no central intermediary extracts value without itself
  contributing.
- **Earn only by validated value** — content, services, or plumbing (incl.
  sustained early availability), measured by demonstrated work, never by
  a registration timestamp.
- **Non-transferable credits** — reputation now; transferable / redeemable
  money is a distinct future stage (vision Q12) with a hard legal gate.
- **Power sublinear at extreme single-entity concentration** — uncapped
  earning, bounded editorial control (vision Q14, currently unsatisfied —
  see `sim/FINDINGS.md` F3).
- **Fully open core** — Rynmesh protocol/node/client are MIT with zero
  proprietary dependencies. Third-party attestation services (e.g.
  Avaryn) plug in through the optional manifest `attestations` field
  and are never required (`DECISION_AVARYN_SEPARATION.md`).

## Functional requirements

### FR-1 — Node and identity (M0, implemented)
- FR-1.1 Each node holds a local Ed25519 identity key; the public key is
  the peer ID. `rynmesh/identity.py`, `rynmesh/store.py`.
- FR-1.2 Identity tiers `unverified | attested | staked | proven` are
  resolved locally from signed evidence (vouches, stake commitments,
  proof-of-resource).

### FR-2 — Content & services as first-class (M0 content; M3 services)
- FR-2.1 Content publish + signed manifest + provenance chain + safety
  receipts. `rynmesh/store.py`, `rynmesh/manifest.py`,
  `rynmesh/provenance.py`, `rynmesh/safety.py`.
- FR-2.2 Direct peer HTTP transport for manifest / preview / bytes.
  `rynmesh/peer_http.py` `/api/v1/content/*`.
- FR-2.3 Services as first-class invocable capabilities — generalized
  from the Signal50 work-order / relay primitive. **Specified, not yet
  generalized** — `RYNMESH_SERVICES.md` is the dedicated M3 document.

### FR-3 — Discovery via registries (M0, implemented; tiering M4)
- FR-3.1 Self-signed peer records, verified locally by each receiver
  (`rynmesh/registry.py`, `rynmesh/registry_http.py`).
- FR-3.2 Multi-tier registry topology + tier reputation. **Specified,
  pending M4.**
- FR-3.3 **Censorship-resistant discovery** (M0, implemented):
  - `FallbackRegistryChain` — `RYNMESH_REGISTRY_URLS` tried in order;
    blocking the primary registry does not cut peer discovery.
  - `bootstrap_peers_from_path / bootstrap_peers_from_url` — load
    Ed25519-verified peer records out-of-band (local file, CDN URL, QR)
    when all registries are unreachable.
  - `GET /api/v1/peers` gossip endpoint — one reachable peer IP is enough
    to discover the whole mesh; full registry blackout does not isolate
    a node. `rynmesh/registry_resilience.py`, `rynmesh/peer_http.py`.

### FR-4 — Credits and validation
- FR-4.1 Signed credit events; non-transferable; weighted by issuer
  identity tier; per-category accounts. `rynmesh/credits.py`.
- FR-4.2 Server-side validation gating: `verify_credit_event_with_policy`.
- FR-4.3 **Consumer-attested serve receipts propagate to provider**
  (closes the F1 propagation gap): the consumer's signed
  `preview_served` / `full_served` event is POSTed to the provider's
  `/api/v1/credits/append`; the provider verifies, rejects
  self-attestation, dedupes, appends. Env-gated on
  `RYNMESH_PROPAGATE_SERVE_RECEIPTS`. `rynmesh/peer_http.py`,
  `rynmesh/store.py`.
- FR-4.4 Provider self-credit on serve is forbidden (gameable) —
  enforced by the append endpoint rejecting `issuer == subject`.
- FR-4.5 **EigenTrust over the consumer-attestation graph** produces a
  global per-peer trust value (vision §4 linchpin). Pure-Python sparse
  implementation, `O(edges + n)` per iteration. `rynmesh/eigentrust.py`.
- FR-4.6 **Sublinear / saturating trust → distribution-weight transform**
  so extreme legitimate concentration cannot buy editorial / validation
  control. **Specified, pending; tracked as vision Q14;** empirically
  required per `sim/FINDINGS.md` F3.

### FR-5 — Ryn agent recommendations
- FR-5.1 A node's agent must surface content & services curated to its
  owner using the EigenTrust trust signal + user feedback + content
  features. `rynmesh/recommender.py`.
- FR-5.2 Architecture follows the X / xAI 3-stage pipeline
  (sourcing → ranking → filtering); the `Ranker` Protocol is the
  Phoenix-class learned-model swap-in seam (vision Q5 / agent contract).
- FR-5.3 Recommendations dedupe at the filter layer (also masks the
  F2 duplicate-peer-record symptom while F2 is repaired upstream).

### FR-6 — Local control API (webapp surface)
- FR-6.1 `/api/local/*` exposes node status, registry status, peers,
  content, jobs, recommendations, publish drafts, settings.
  `rynmesh/peer_http.py`.
- FR-6.2 **Loopback-gated** — the peer port may bind `0.0.0.0` for P2P
  but the control API must be unreachable from the LAN. Loopback check
  + optional `RYNMESH_LOCAL_TOKEN` (the desktop shell can inject it).
  Verified end-to-end in the rynnet testbed (LAN `/api/local` → 403).

### FR-7 — Agent intelligence — zero-cost floor + frontier ceiling
- FR-7.1 The node must ship with a bundled local model so anyone can
  contribute and earn without paying for a frontier model. **Specified
  (vision §2 stance); not yet bundled in the rynmesh package.**
- FR-7.2 Optional plug-in of a user-supplied frontier-model API key with
  no platform markup. **Specified, hook through `Ranker`.**

### FR-8 — Desktop on-ramp (M1)
- FR-8.1 One signed download → node daemon + UI + tray + safe defaults.
  Tauri-based; sidecar is the unmodified `rynmesh-peer` frozen via
  PyInstaller. `webapp/src-tauri/`.
- FR-8.2 Lifecycle: spawn / health-poll / restart / graceful shutdown;
  closing the window keeps the node in the tray; Quit stops the daemon.
- FR-8.3 Single-instance — second launch focuses the running window.

## Non-functional requirements

### NFR-1 — Transparency of test environments
- NFR-1.1 The `rynnet` testbed must run **unmodified** `rynmesh-peer`
  binaries — a node cannot tell it is simulated. Shaping at the
  container's `eth0` (tc netem / iptables), not in-process.
  `rynnet/Dockerfile`, `rynnet/entrypoint.sh`.

### NFR-2 — Receiver-side enforcement primacy
- NFR-2.1 Client-side guards (official client refusing to emit
  policy-violating content) are defense-in-depth, never the security
  boundary. Every receiver validates regardless of which client produced
  the bytes — open MIT protocol, hostile reimplementations expected.
- NFR-2.2 Receipts and attestations must be **verifiable by the open
  spec using the issuing party's public keys**; trust depends on key
  custody, never algorithmic secrecy.

### NFR-3 — Scale targets
- NFR-3.1 The protocol-fidelity testbed (`rynnet`) targets ~10-50 real
  containerized nodes per host with full fidelity (transparent shaping,
  NAT/relay, partition).
- NFR-3.2 The economic-dynamics simulator (`sim/scale_sim.py`) targets
  ~1K-10K abstract nodes per run (validated: 1K in ~8s, 10K in
  ~17 min on a 4-core/6 GB Colima VM with sparse EigenTrust).
- NFR-3.3 Anomaly detection must surface concentration / inequality /
  newcomer-lockout indicators each run (`sim/scale_sim.py`).
- NFR-3.4 Scaling beyond 10K requires pre-pooled candidate sampling
  and/or batched / vectored compute. **Specified, optional.**

### NFR-4 — Privacy
- NFR-4.1 Local files do not leave the machine unless the user publishes
  them or explicitly sends them to a configured cloud model.
- NFR-4.2 Local-only mode must prevent cloud model calls.

### NFR-5 — Testability
- NFR-5.1 Every protocol primitive has unit + integration test
  coverage; `pytest` suite must remain green. Current: 164/164 (non-flaky).
- NFR-5.2 The testbed and the simulator must produce reproducible runs
  (seeded RNG; pinned scenario JSON).

### NFR-6 — Stdlib-first
- NFR-6.1 New rynmesh modules (`eigentrust.py`, `recommender.py`,
  `sim/scale_sim.py`) are stdlib-only to keep the package light and
  deployable; numpy/scipy may be added as opt-in dev/perf extras.

### NFR-7 — Censorship resistance (M0, implemented)
- NFR-7.1 Peer traffic must not self-identify as Rynmesh to a passive
  observer. Default transport (`camouflage`) uses browser-like User-Agent +
  Accept headers, TLS 1.2+, browser ALPN. `rynmesh/transport.py`.
- NFR-7.2 The transport layer must be pluggable so heavier obfuscation
  strategies can be deployed without touching protocol or business logic.
  `register_transport()` in `rynmesh/transport.py`.
- NFR-7.3 The peer server must not reveal it runs Rynmesh to an active
  probe when a network key is configured. `RYNMESH_NETWORK_KEY` causes
  unauthenticated requests on `/api/v1/*` + `/health` to receive HTTP 404
  with no identifying headers. `rynmesh/peer_http.py`.
- NFR-7.4 A node must remain discoverable when the primary registry is
  blocked: fallback registry chain, out-of-band bootstrap, and peer gossip
  each independently provide a path to the network. `rynmesh/registry_resilience.py`.
- NFR-7.5 A node behind a CDN must be reachable via standard WebSocket
  upgrade (`cdn-ws` transport) so the visible SNI/IP belongs to a CDN
  domain with high collateral-damage cost. `rynmesh/transport_plugins.py`.
- NFR-7.6 TLS fingerprint mimicry (`reality` transport via `curl_cffi`)
  must produce a ClientHello byte-identical to Chrome 124 so passive DPI
  cannot distinguish it from browser traffic. Optional dep. `rynmesh/transport_plugins.py`.
- NFR-7.7 ECH (Encrypted Client Hello) must activate automatically when
  the Python runtime exposes the API (OpenSSL 3.5+ via future CPython);
  until then it falls back to SNI/connect/Host fronting with a log notice.
  `rynmesh/transport_plugins.py`.

## Open requirements (tracked in vision § "Open Questions")

| ID  | Requirement                                                 | M  |
|-----|-------------------------------------------------------------|----|
| OQ-1  | Services execution model + invocation contract             | M3 |
| OQ-2  | Validation-weighting curve / collusion / brigading defense | M4 |
| OQ-3  | Accountable steward of protocol + root registry            | M6 |
| OQ-4  | Credit issuance / decay / pricing + early-emission curve   | M5 |
| OQ-5  | Agent default-profile values + policy language             | M2 |
| OQ-6  | Definition / scope of "unhealthy" and registry policy fork | M4/6 |
| OQ-7  | Governance of protocol changes in a company-less network   | -  |
| OQ-8  | Client attestation & admission (collective approval)       | M4 |
| OQ-9  | Avaryn open-core structural assurance                      | **resolved: moot — rynmesh fully open, Avaryn optional** |
| OQ-10 | Where Avaryn runs during proprietary period                | **resolved: external to protocol (optional attester)** |
| OQ-11 | Open-standard transition for Avaryn/PURR                   | **resolved: attestations field is the open standard** |
| OQ-12 | Contributor *money* (transferable value) — legal gate      | post-M6 |
| OQ-13 | Staged-opening triggers + structural commitment            | M6 |
| OQ-14 | **Concentration safeguard (sublinear weight)**             | M5 (empirically required by F3) |
| OQ-15 | Local-model improvement mechanism + equity floor           | M2 |
| OQ-16 | Snowflake/WebRTC pluggable-transport bridge (full implementation) | M2 |
| OQ-17 | NAT traversal / relay for censored-region nodes            | M2 |
