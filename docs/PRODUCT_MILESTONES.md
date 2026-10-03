# Rynmesh Product Milestones

Status: active roadmap, reviewed 2026-10-03 against merged main
[`fa85833`](https://github.com/yeogirlyun/rynmesh/commit/fa85833f2cee8b4cfb70a0f7fc8c7a9a5e976c4e).
The published release remains `v0.7.0` (2026-09-15). Changes merged afterward
are not claimed to be present in its downloadable artifacts.

Rynmesh is alpha software. **Implemented** means code exists; **verified**
names its evidence; **released** means a published artifact includes it.
Neither implementation nor green CI proves production readiness. Historical
release notes and acceptance records retain the scope of their own snapshots.

Direction: provide infrastructure for both people/nodes that **provide value**
and those that **seek value**. Goods, hiring, consulting, AI media and network
services are examples, not a fixed category list. Any lawful, ethical activity
should be able to use shared identity, discovery, agreements, delivery and
payments. Participation is free and open source, with no middleman/platform
commission. Direct counterparties negotiate prices through demand and supply;
verified basic system work earns protocol-issued Ryncoin. Neutral AI models
apply public covenants to disputes, with corresponding disclosed Ryncoin fees
paid by the involved parties for the resolution work. The proposed
[network economy roadmap](NETWORK_ECONOMY_ROADMAP.md) details the extensions; it does not authorize implementation or reserve work.

## Current baseline: P1 Ryn Companion and P2 Friend Mesh

### Released in v0.7.0

- Self-contained macOS desktop app for Apple Silicon and Intel; the bundled
  node serves the web UI. The default experience needs no account, API key,
  model or connected peer.
- Real public-content recommendations and preference/feedback learning.
- Reading, bookmarks, progress, offline saves and local data management.
- Unified Ask Ryn, saved conversations, model selection, managed local AI
  setup and friend AI permissions; cloud models require explicit opt-in.
- Search across content, friend shares and conversations.
- Reviewed friend invite/QR pairing, first sharing, follow feed, encrypted
  messaging, revocation and owned-device reading/conversation sync.
- Signed publication/fetching, provenance, safety receipts, discovery,
  local control API, MCP tools and nontransferable contribution reputation.

See [v0.7.0 release notes](RELEASE_NOTES_0_7_0.md) for that release's limits.

### Additional capabilities implemented on main

- Friend publications ranked in For You with publisher/serving-node evidence
  and access checks (#62).
- Owner-triggered small-mesh diagnostics with fresh signed challenges and
  recovery guidance (#64); physical cross-network acceptance remains open.
- Friend publication offline saves and reviewed Ask handoff (#78), plus a
  private deterministic weekly friend recap (#79).
- Two-friend shared reading lists with durable offline operations (#80).
- Bounded sync storage and diagnostics (#67), invitation reach review (#71),
  and freshness-aware device sync health (#77).
- Reviewed category cleanup across selected owned devices, with receiving-owner
  approval and signed completion receipts (#69). It does not erase exports,
  backups, independent copies or records outside the reviewed scope.
- Direct Private AI streaming with cancellation, task recovery and final
  archiving (#60). Other delivery paths can return whole responses.
- Shared service descriptors/discovery/order lifecycle hooks across the catalog,
  Private AI, video and secure-web screens (#66). Generic marketplace/payment
  infrastructure is still proposed.

Evidence: [integration PR #85](https://github.com/yeogirlyun/rynmesh/pull/85),
1,661 backend and 383 webapp tests passed during integration; all nine
[post-merge CI jobs](https://github.com/yeogirlyun/rynmesh/actions/runs/37137335701)
passed, including packaged-node and both desktop architectures. This is a dated
snapshot, not a coverage percentage or a promise of physical acceptance.

### Remaining P1/P2 improvements

1. Consolidate recommendation contracts and profile state; expand explanation
   and undo of learned signals rather than rebuild already-tested feed screens.
2. Extend source recovery, document/media accessibility and viewer network
   privacy; preserve explicit content review before AI processing.
3. Finish installed-app and separate-machine/cross-network acceptance. Desktop
   invite deep links (#72) remain outside main pending installed macOS checks.
4. Harden multi-user egress credentials and real provider/revocation recovery.
5. Make contribution evidence/history inspectable; preserve the distinction
   between reputation and future spendable Ryncoin.
6. Complete cleanup inventory for shared reading and disclose independent-copy
   limits consistently.
7. Add Windows/Linux desktop packaging and Apple signing/notarization when
   maintainer credentials are available.

Frontend component tests, friend product E2E and direct ICE/peer transit exist;
these are no longer missing foundations. They do not substitute for physical
field validation. See [Testing strategy](TESTING_STRATEGY.md).

## P3: Shared work and service infrastructure

Goal: one durable infrastructure path supports providers and seekers across
many activities, with owner control and optional AI assistance.

Implemented foundations include encrypted service tasks, model publication and
discovery, native inference, supervised workers, shared service UI lifecycles and
ledger-backed **development** Task Balance. The first complete digital-work
journey now also has free offers/requests, exact signed terms, original-operation
recovery, encrypted delivery, escrow and internal Ryncoin settlement in a
configured alpha. See [Exchange alpha](EXCHANGE_ALPHA.md) and [issue #87](https://github.com/yeogirlyun/rynmesh/issues/87).
Video/Private AI workflows have not yet migrated to that new ledger.

Proposed extensions, before broad marketplace features:

1. Restart-safe order lookup, reconciliation and cancellation; remove the
   video form's current reload/purchase-recovery gap.
2. Participant identity across nodes, key rotation, device authority and recovery.
3. Versioned offers **and requests**, private discovery projections, negotiated
   terms, availability and matching.
4. Signed agreements, delivery adapters, milestones and dispute-ready evidence.
5. Open schema/adapter SDK and conformance tests; new activities reuse shared
   infrastructure and do not run arbitrary code by default.
6. Budgeted agents with explicit permissions, spending limits and local audit.

Gate: typed service execution, restart/replay/crash safety, private-data tests
and real distinct-egress acceptance. See [Service platform](SERVICE_PLATFORM_NEXT.md)
and proposal packages INF-01 through INF-05 in the economy roadmap.

## P4: Open-network hardening

Goal: support interaction beyond personally trusted nodes.

Existing primitives include EigenTrust, configurable sublinear reputation
weighting, exploration fraction, safety receipts, direct ICE/UDP transfer and
peer transit. They are not a completed adversarial defense or a scalable reward
system.

Extend authenticated discovery, bounded admission/quotas, anti-Sybil and
collusion defenses, verified useful-work/availability claims, stronger safety,
quarantine, network covenants, neutral AI resolution/appeals and privacy-preserving
observability. Define legal/ethical participation rules and activity-specific
requirements. Validate hostile clients, partitions, wash transactions, fake
views, brigading and concentrated validation power.

Gate: adversarial evidence and legal/operating review before unrestricted public
operation. Generic listings can be designed on a private mesh beforehand;
public payments and network rewards cannot bypass this gate.

## P5: Internal Ryncoin earn/spend economy — configured alpha implemented

Goal: participants earn Ryncoin from direct customers or protocol rewards for
useful network work, and spend it with other participants inside Rynmesh.

Keep three concepts separate: **Rynmesh Credits** remain nontransferable
reputation; **development Task Balance** stays simulated; **Ryncoin** is the
spendable unit in the opt-in configured alpha. No automatic migration, redemption, or minting from
existing reputation events or development balances.

Design wallet authority/recovery, integer amounts, supply/issuance, finality,
anti-double-spend rules, escrow, refunds, splits, reconciliation and protocol-issued
infrastructure rewards. Design decentralized consensus and validator incentives
explicitly; a collection of local signed files is insufficient, and a sole
platform settlement authority would conflict with the no-middleman principle.

Delivered subset: integer wallet, fixed-roster quorum certificates and durable
vote locks, capped one-time registry registration issuance, direct transfers,
price/fee escrow, encrypted text delivery, buyer acceptance, mutual refund,
three-model decision receipts, one independent-panel appeal and exact-operation
recovery. Tests exercise concurrent submissions, replay, quorum loss, conserved
supply, private data and real localhost HTTP. Model outputs in acceptance are
controlled. This is not production BFT or evaluated neutral AI.

Remaining gate: reviewed economy/operator design, conserved balances under concurrent
spending, validator failure and forks; neutral AI disputes, disclosed fees and
anti-abuse controls; then a controlled pilot with no fiat bridge. Unrestricted expansion also requires P4. Broader public-network changes remain proposals, not a release date.

## P6: General-purpose value exchange — proposed

Goal: any lawful, ethical activity can implement an offer/request schema and
fulfillment adapter over the same shared infrastructure.

Possible experiences include P2P commerce, hiring/resumes/recruiting,
consulting/part-time work, applications, commissioned AI drama/movies, licensed
media viewing and paid infrastructure services. Both sides can publish their
needs or capabilities. Hiring/application workflows need not charge applicants;
paid delivery uses the common Ryncoin settlement path.

Creator income comes from purchases, patronage or explicitly funded rewards;
uploading or fabricating views never grants automatic coins. Add rights-aware
entitlements, streaming/distribution, creator splits and private agreements.

Gate: providers can earn and spend across at least three unrelated activities;
new schemas pass privacy, policy, compatibility and settlement conformance
without introducing a new wallet or bespoke ledger. Each activity adds its own
lawful/ethical fulfillment requirements. This is an open capability platform,
not an exhaustive list of product categories.

## P7: Fiat conversion — much later

Only after a dependable internal economy, design separate exchange adapters
with demand/supply pricing, liquidity, expiry, custody and reconciled fiat/coin
settlement. No fixed peg or guaranteed redemption is assumed.

Plan for at least ten major currencies, including the owner's USD, CNY, HKD,
JPY and KRW examples. The [economy roadmap](NETWORK_ECONOMY_ROADMAP.md#7-fiat-exchange-much-later)
records candidate codes and selection sources. Fiat conversion remains disabled
through the initial network earn/spend stage and requires a separate decision,
qualified jurisdiction-specific review and operating readiness.

## Choosing a contribution

[GitHub Issues](https://github.com/yeogirlyun/rynmesh/issues) remains the executable
backlog. Proposal IDs and roadmap entries are suggestions for owner/design
review; they do not reserve work or claim milestone acceptance. Follow
[CONTRIBUTING.md](../CONTRIBUTING.md) before starting implementation.

Recommended next review after this digital alpha: audited round-changing
consensus, demand-backed infrastructure rewards, real independent-model dispute
acceptance, abandonment rules and adapter migration. Policy design continues
alongside these, and money-like behavior requires a reviewed design issue before
implementation. No autonomous or recurring work is authorized by this roadmap.
