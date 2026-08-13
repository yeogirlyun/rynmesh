# RynMesh Product Milestones — From First Launchable Product to the Full Network

Status: proposal (2026-07-30). Companion to `RYNMESH_VISION.md` §7 (M0–M6). Where VISION.md
describes the *protocol* milestones, this document describes the *product* milestones: what we
ship, to whom, and why they would use it — starting from what is actually working in the repo
today.

---

## 1. Where we actually are (honest inventory)

**Strong and working today:**
- Node daemon (`rynmesh-peer`): identity (Ed25519), content store/publish, signed manifests,
  provenance, credits-as-reputation ledger, registry with fallback chains, censorship-resistant
  transport ladder, Nebula overlay.
- **VPN egress** — working end-to-end, verified from mainland China, 3 transports, launcher in
  the webapp. (Single-tenant only: `FUTURE_WORK_EGRESS_MULTITENANT.md`.)
- **Peer messaging** — E2E-encrypted 1:1 chat with media, just merged and verified.
- **Desktop app** — Tauri `Ryn.app` with tray, sidecar daemon, auto-update with rollback.
- **MCP server** — 31 tools; any MCP-capable agent can already drive a node.

**Built but not wired (the biggest cheap wins):**
- `recommender.py` — full sourcing→ranking→filtering pipeline, tested, imported *only by tests*.
  `POST /api/local/recommendations` returns a hard-coded `[]`.
- `eigentrust.py` — tested library, used only by the simulator.

**Vision items with no code:** autonomous agent loop, model adapters (local or cloud), services
primitive (M3), credit sinks/issuance epochs, anti-Sybil, Avaryn as a real external trust root
(receipts are currently self-minted), any token/coin (correctly absent).

**Known defect to fix early:** F2 (`rynnet/FINDINGS.md`) — `discover_peers` doesn't dedupe by
`peer_id`; double registration double-counts content. Cheap amplification vector.

**The strategic problem:** the vision is a network, and networks have a cold-start problem.
A content mesh with 0 users has no content, so "agents curating mesh content" delivers nothing
on day 1. The first product must be valuable **single-player**, become more valuable with 2–5
friends, and only then depend on network effects.

---

## 2. The wedge: what we launch first

**P1 — "Ryn: your personal agent, on your machine" (the launchable first product)**

One download. Inside:

1. **The Daily Digest (the hero feature).** The node's agent reads sources *you* configure —
   RSS feeds, YouTube channels, subreddits, newsletters, arbitrary URLs — plus anything your
   mesh peers publish, ranks it with the existing recommender pipeline, and presents a
   receipt-backed digest each morning: every item carries *why it was picked* (source, score
   features, your feedback history). This is exactly the vision's step 1 ("agent collects
   content from various sources and presents it to the user") — and it works with zero other
   users because the open web is the initial source set.
2. **Search & Ask** — wired to a real model via a pluggable adapter (see P1 scope).
3. **VPN egress** — the already-working utility. This is the feature that keeps the node
   *installed and running 24/7*, which the agent needs anyway. For users in censored regions it
   is the reason to install at all.
4. **Peer chat** — already shipped; the social seed.

Positioning: *"A private AI that reads the internet for you. Runs on your machine. No feed
algorithm, no ads, no platform."* The anti-feed framing is the emotional hook; the digest is
the daily-return habit; egress is the utility anchor.

Why this wedge and not the alternatives:
- *Messaging-first*: hopeless against Signal/WhatsApp; keep it as a supporting feature.
- *Publishing-first*: cold start — nobody to publish to.
- *VPN-first*: real demand but legally narrow and commoditized; great anchor feature, weak
  identity for the whole product.
- *Digest-first* is the only wedge that is (a) single-player-valuable, (b) on the direct path
  to the vision (the same recommender later ranks mesh content), and (c) mostly built already.

---

## 3. Milestone ladder

### P1 — Ryn Companion (launch to first outside users)
*Goal: 1 download → daily habit for a single user. Private mesh / invite-only TestFlight-style.*

**Progress (2026-07-30):** shipped — recommender wiring (item 1), the Daily Digest
service (RSS/YouTube/Reddit ingestion, feedback loop, exploration slots), the
ModelProvider adapter (item 2: Ollama local + BYO Anthropic key) powering digest
briefings, per-item AI summaries, and a real Search & Ask, plus read-it-later,
page watchers, and the MCP agent gateway (digest/read-later/watcher/messaging
tools). Remaining below: publish-flow completion (4), F2 fix (5), onboarding (6),
and a background refresh schedule.

Scope (roughly ordered; items 1–3 are the critical path):
1. **Wire the recommender.** Make `/api/local/recommendations` call `recommender.py` for real.
   Recommendations screen renders real output. (The single highest-leverage change in the repo.)
2. **Model adapter seam.** One `ModelProvider` interface, two implementations:
   local (Ollama) and bring-your-own-key (Anthropic/OpenAI). Powers Search & Ask, digest
   summarization, and later the agent loop. No bundled model yet (defer FR-7.1).
3. **Source ingestion + Daily Digest job.** Config for RSS/YouTube/Reddit/URL sources; a
   scheduled fetch→rank→summarize job; digest presented in Recommendations with receipts;
   thumbs up/down feeding back into ranker features.
4. **Finish the publish flow** (real safety outcome + manifest hash in `prepare`, not stubs).
5. **Fix F2** (discover_peers dedupe by peer_id).
6. **Onboarding polish**: first-run wizard (name the node, pick sources, optional model key),
   signed DMG, auto-update already done.

Explicitly *out*: autonomous spending agent, services primitive, any economy work, Avaryn
external receipts (keep self-minted, labeled "provenance recorded locally").

Success gate: ≥20 outside users; ≥40% open the digest 3+ days/week in week 4.

### P2 — Friend Mesh (the invite loop)
*Goal: each user pulls in 2–5 trusted peers; mesh content enters the digest.*

1. **One-click invite.** An invite bundle (network key + bootstrap registry + inviter endpoint)
   as a link/QR; accepting joins the private mesh and opens a chat with the inviter.
2. **Mesh as a digest source.** Friends' published clips/posts ranked alongside web sources;
   "your friend's node served this" receipts visible.
3. **Multi-tenant egress** (per `FUTURE_WORK_EGRESS_MULTITENANT.md`: provider-issued ephemeral
   session credentials). Now "share your exit with friends" is safe — the viral utility:
   one friend abroad = VPN for the whole group.
4. **Credits become visible.** The existing ledger surfaces: "your node earned N serving
   peers this week." Still non-transferable reputation — but now it's *felt*.
5. Serve-receipt propagation on by default (`RYNMESH_PROPAGATE_SERVE_RECEIPTS`).

Success gate: median user has ≥2 active peers; ≥25% of digest items are mesh-origin in
established meshes.

### P3 — Working Agent + Services Primitive (VISION M2 + M3)
*Goal: the agent acts, not just curates; nodes sell capabilities to each other.*

1. **Budgeted agent loop**: background scheduler with an approval envelope (credits/day,
   actions allowed), full audit log in the UI. Conservative-active defaults per VISION M1.
2. **`RYNMESH_SERVICES.md` + service manifest**: generalize the Signal50 work-order path into
   a real primitive — manifest, invocation, metering, result verification. First real services:
   `net.egress` (retrofit), `llm.generate` (Ollama-backed worker replacing the stub),
   `media.transcode`.
3. **Credits as metering**: service invocations debit/credit the ledger between nodes
   (still non-transferable; this is the spend path that makes credits an economy rather than
   a scoreboard — `work_order_completed` weight stops being 0.0).
4. Agent-to-agent: your agent can commission a friend's node ("summarize this 2-hour video on
   your GPU") within its budget.

### P4 — Open Network Hardening (VISION M4)
*Goal: safe to let strangers in.*

- Anti-Sybil: port sim finding F4 into `credits.py` (sublinear credit→weight saturation),
  newcomer discovery carve-out for real, wire EigenTrust into the credit/ranking path.
- Registry tiers + authenticated writes; proof-of-availability emitter; peer quarantine.
- Safety packs beyond the keyword scanner; moderation/appeals workflow.
- Avaryn: decided 2026-07-30 (see `DECISION_AVARYN_SEPARATION.md`) — rynmesh is fully
  self-contained MIT with a generic manifest `attestations` seam; Avaryn returns as an
  *optional* premium attestation/service provider, earning trust weight like any issuer.
- Public onboarding (no invite needed) only after this milestone — per VISION's own gate
  (M4 + M6 together).

### P5 — Economy Maturation → Credit Transferability (VISION M5 + M6)
*Goal: credits become worth something — carefully.*

- Sinks and pricing, issuance epochs/decay, category scoreboards.
- Only after sustained real service demand: revisit transferable credit ("Ryn credit as
  money") behind the legal/regulatory review VISION §4 already mandates. Sequencing rule:
  **utility first, transferability last.** A token before real usage attracts speculators and
  regulators, not users.

---

## 4. What to start on Monday

The immediate work, in order:

1. Wire `/api/local/recommendations` → `recommender.py` (small, unblocks the whole P1 UI).
2. `ModelProvider` adapter + Ollama backend + BYO-key backend; connect Search & Ask.
3. Source-ingestion module (start with RSS only) + digest scheduler + digest UI state.
4. F2 dedupe fix + publish-prepare completion.
5. First-run onboarding wizard in the webapp.

Each item is independently shippable and testable on the existing 6-node private mesh.
