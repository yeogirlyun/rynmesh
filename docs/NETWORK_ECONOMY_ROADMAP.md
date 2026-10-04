# General-purpose value exchange and Ryncoin infrastructure

Status: owner-approved direction and first digital-work alpha implementation,
2026-10-04: v2 protocol hardening adds immutable cases, bound deadline outcomes, admission/closing reserves and legacy replay. See [hardening design](EXCHANGE_PROTOCOL_HARDENING.md) and [issue #89](https://github.com/yeogirlyun/rynmesh/issues/89). [Exchange alpha](EXCHANGE_ALPHA.md) documents the delivered subset
and [issue #87](https://github.com/yeogirlyun/rynmesh/issues/87) records its design.
The remainder below is the broader roadmap, not a claim of public production
readiness. The baseline preceding this milestone was
[`fa85833`](https://github.com/yeogirlyun/rynmesh/commit/fa85833f2cee8b4cfb70a0f7fc8c7a9a5e976c4e).

Delivered subset: free profiles/offers/requests, signed digital agreements,
integer Ryncoin wallets, quorum-approved bounded registry registration issuance,
escrow/refunds/direct transfers, encrypted text delivery, model-receipt decisions
and one appeal. The roster is explicit and fixed; there is no round-changing
BFT or permissionless issuance defense. Real AI impartiality, ongoing useful-work
rewards and complete commerce/hiring/media adapters remain later gates.

## 1. Product direction

Rynmesh provides shared infrastructure for people and nodes that **provide
value** and those that **seek value**. Anyone may offer or request lawful,
ethical goods, work, content, experiences, applications, or services. A
participant can be a provider, a seeker, and an infrastructure contributor at
once. The activity examples below are not a category allowlist.

Owner-defined principles:

- Public source, schemas, covenants, fees, economic parameters and verifiable
  network decisions. Rules and reward calculations have no hidden privileges.
- Fair compensation: more verified useful contribution can earn more; greater
  consumption pays for the chosen quantity, quality or capacity at agreed prices.
- Open-source protocol, node and client; anyone may join and participate for
  free without a platform account, entry charge or platform commission.
- No middleman owns access, takes a rake or decides the network's commercial
  categories. Direct counterparties set terms through demand and supply.
- Nodes providing useful basic system work, including registry service, earn
  protocol-issued Ryncoin under transparent, verifiable issuance rules.
- Network covenants define lawful/ethical participation. Neutral AI models
  resolve network disputes under those covenants; the involved parties pay the
  corresponding disclosed Ryncoin resolution fees.

Free participation is compatible with voluntarily buying goods, content or
services and paying for actual dispute-resolution work. Those payments go to
contributors; they are not a platform access toll. No price, ranking privilege,
coin issuance or arbitration power is granted merely by owning the rails.

The intended loop is:

```text
publish an offer or request -> discover and match -> agree terms
-> reserve Ryncoin -> deliver and accept -> settle or use neutral AI resolution
-> retain receipts and earn reputation -> spend earnings elsewhere in Rynmesh
```

AI agents may assist with discovery, matching, preparation, and execution within
explicit owner permissions. People can also use the network directly. A new
activity must not require an autonomous agent or remote execution of unknown
code. Open participation does not waive applicable law, safety, consent,
intellectual-property rights, or transparent ethical rules.

Initial economic scope: earn and spend **Ryncoin inside Rynmesh**, including
payments between participants. Fiat purchase, withdrawal, redemption, and
currency exchange are a much later stage. An internal economy still requires
legal and operating review before launch; delaying fiat does not remove that
gate. This roadmap does not independently authorize implementation, service, automation, or monitoring.

Transparency includes open audit access to issuance, settlement proofs, fee
calculation, reward attribution, model/covenant references and governance changes.
The visibility of balances and personal messages, resumes, contracts and prompts
needs an explicit owner decision. The private-body designs below are a working
recommendation, pending that clarification; they do not reinterpret transparency
as a right to reveal another person's data or wallet keys. Existing encrypted
product behavior is not changed by this proposal.

## 2. What exists and what is missing

| Layer | Current implementation | Infrastructure extension proposed |
|---|---|---|
| Identity | Signed node identities, friend permissions, owned-device pairing | Participant identity across nodes, wallet authorization, recovery and organizational roles |
| Discovery | Public content, friend feed, signed service/provider discovery | Generic offers and requests, selective indexing, matching and availability |
| Communication | Encrypted peer messages, attachments and offline mailboxes | Order-bound negotiation, durable delivery acknowledgements and retention rules |
| Work | Private AI work orders, video workflow and service lifecycle hooks | Generic agreements, milestones, manual/digital/physical delivery adapters |
| Accounting | Nontransferable reputation plus development Task Balance events | Spendable Ryncoin, decentralized spend finality, escrow, refunds and protocol issuance |
| Content | Signed publication, hashes, provenance and verified fetching | Licensing, paid access, creator splits, metered distribution and streaming |
| Policy | Local permissions and alpha safety scanning | Network covenants, neutral AI resolution, appeals and jurisdiction-aware restrictions |

Task Balance already folds development accounting events through
`FileCreditLedger`; it is not an independent production currency. Sharing an
event store does not provide network-wide spend finality or make those units
Ryncoin. Signed receipts prove attribution and tamper evidence, not that a
service was useful, a delivery occurred, or a view was genuine.

## 3. Three separate concepts

| Concept | Purpose | Proposed treatment |
|---|---|---|
| Rynmesh Credits | Existing contribution reputation and distribution signals | Remain nontransferable; cannot be purchased through Ryncoin |
| Development Task Balance | Existing simulated service hold/settle/release accounting | Remains explicitly development-only; no automatic conversion to spendable Ryncoin |
| Ryncoin | Internal earn/spend unit implemented in the configured digital alpha | Transferable between participants for network transactions; no fiat bridge initially |

Paying a provider transfers existing Ryncoin. It does not also mint the same
amount again. Spending coins does not erase reputation. Buying coins must not
buy validation authority or default editorial rank. Keep coin balances,
service-specific quality evidence, and reputation separately inspectable.

Use integer minor units, an immutable asset identifier, explicit network IDs,
and versioned signed transaction envelopes. Precision, total supply, issuance
schedule, recovery rules, and governance remain design decisions. Do not promote
current floating-point reputation amounts into money-like balances.

## 4. Recommended infrastructure, in dependency order

### A. Durable identity and node operations

Separate the participant from a device, node, service, and wallet. Bind several
owned nodes to one participant through explicit authorization; support key
rotation, compromised-device revocation, recovery and organizational delegates.
A single device loss must not silently create a new seller history or let a
stale device spend after revocation. Specify what recovery can and cannot do.

Build on existing diagnostics and worker supervision: bounded queues and
quotas, restart recovery, protocol negotiation, authenticated registry writes,
backup/restore, redacted diagnostics and delivery acknowledgements. Record
physical cross-network and installed-desktop acceptance, not only loopback
results. Existing public discovery remains usable without a wallet.

### B. Offers, requests, discovery and matching

Define versioned signed **Offer** and **Request** envelopes with extensible
namespaced schemas. Shared fields cover issuer, revision, visibility, capability
or subject, availability, price/budget in Ryncoin, delivery mode, expiry,
licensing, eligibility and policy references. Providers advertise what they can
deliver; seekers publish what they need, including requests without an existing
matching offer. Replies can negotiate price and scope before an agreement.

Indexes consume only owner-approved discovery projections. Resume bodies,
contact details, addresses, prompts, contracts, applications and private
attachments stay encrypted and out of public indexes and registry logs. Provide
language, location/remote, time, skill, capacity and price filters where the
activity schema needs them; avoid inferring sensitive attributes for matching.

Discovery can federate across independent registries with signed revisions,
expiry, withdrawal tombstones, deduplication, pagination and abuse quotas.
Permission is rechecked before revealing protected details. Ranking explains
match evidence and leaves room for newcomers; payment is not the default rank
signal. Continuous matching or notifications require explicit opt-in.

### C. Agreements and durable fulfillment

A signed agreement binds both participants to exact offer/request revisions,
price, scope, deadlines, rights, delivery evidence, cancellation, refund,
dispute terms and the permitted budget. Changes require a new accepted revision.
Reuse service descriptors and work-order concepts through adapters rather than
making a second purchasing path for each activity.

Persist order identity, idempotency keys, original provider and accepted terms
before a financial operation. A timeout means an uncertain outcome: look up and
reconcile the original operation before retrying. Never create a second purchase
on reload. Use a durable state machine covering agreement, funding, work,
delivery, acceptance, dispute and settlement, with milestone and partial-refund
branches and atomic inventory/capacity reservations.

Support manual work, appointment/booking, digital delivery, physical delivery,
remote compute and long-running subscriptions through typed adapters. A file
hash is delivery evidence, not proof of quality or shipment. Explicit acceptance
criteria, agreed timeout rules and neutral AI covenant-based resolution handle
subjective work. Human challenge submissions and appeal rights remain available;
network outcomes do not claim to replace legal rights or court processes.
Candidate selection and a lawful employment agreement are separate from a paid
work order; applicants do not need to pay merely to submit a resume.

### D. Wallet, decentralized finality and escrow

The critical design question is **how independent peers finalize spending**.
Independently signed local balance files cannot stop the same coins being spent
on different peers. A sole platform operator finalizing payments would recreate
the middleman the network is meant to remove, so it is not the proposed path.

Recommend a replicated transaction log with a reviewed consensus protocol,
publicly verifiable state transitions and signed finality proofs. Compare an
existing open-source ledger implementation with an application-specific
implementation before choosing technology; whether this is a blockchain is an
open design decision. Do not invent or ship an unreviewed consensus scheme.
Neither a discovery registry nor an AI dispute model alone may mint, finalize,
reverse or censor all network payments.

Participation is free; transaction validation is a network contribution role,
not a prerequisite for browsing, publishing or trading. Validator eligibility,
Sybil resistance, selection/rotation, quorum/fault thresholds, incentives,
censorship escape and protocol-upgrade governance need explicit designs. A
small test network may use identified volunteer validators, but it must disclose
that limitation and cannot claim permissionless economic security. Open public
operation requires adversarial evidence for validator capture and collusion.

Specify canonical transaction ordering, authorization, account sequence numbers,
integer amounts, atomic balance changes, no negative available balances, held
funds, supply reconciliation and finality receipts. A partition that lacks the
required quorum stops finalization; offline drafts may queue but cannot claim
completed payment. Test conflicting spends from several owned devices, forks,
validator failure and stale snapshots after restore. Recovery must not make a
finalized payment spendable again.

Escrow is a consensus-enforced hold. Accepted delivery, agreed cancellation or
timeouts, and verifiable neutral-AI dispute decisions can release it under
preaccepted rules. Support partial refunds, milestone settlement and contributor
splits without duplicate payouts. An AI ruling proposes the contract outcome;
ledger peers verify its authorized panel, covenant version, fee limits,
evidence commitments, appeal/finality stage and signatures before executing it.
The model never holds wallet keys or edits balances directly. Private contract
bodies and personal information stay outside public ledger records.

### E. Earning, protocol issuance and infrastructure rewards

Two sources of income use one accounting model:

1. **Participant-funded payments:** goods, human work, content, applications,
   compute and other services paid from the seeker's existing balance at
   mutually accepted prices driven by demand and supply.
2. **Protocol-issued infrastructure rewards:** useful basic system services,
   including registry/discovery, peer transit, replication and availability,
   rewarded in newly issued Ryncoin under published network rules. Other
   network jobs may instead be funded from existing balances.

Only protocol-authorized issuance changes supply; a customer's payment does not
also mint the same amount. Rules specify eligible work, challenge/measurement
proofs, per-period issuance budgets, scarce-capacity priorities, validator
approval and deduplication. A registry owner cannot self-mint rewards for
announcing itself or serving fabricated requests. Demand determines useful
capacity and willingness to pay; supply issuance is governed by the covenant's
published economic rules, not an arbitrary operator decision.

Prototype issuance and conservation in an isolated test network first. Supply,
emission/decay parameters, bootstrap funding and reward rates require review;
there is no implicit Bitcoin-style schedule or fiat value. Existing reputation,
simulated balances, registration, idle uptime, uploads and self-reported jobs do
not automatically become spendable coins. Contracted/challenged availability can
earn; mere presence cannot.

Introduce bounded issuance claims, per-entity risk limits, self-dealing
exclusions, independent validation, anomaly detection and claim appeals. Detect
wash purchases, fake views, circular payments, colluding validators and fake
deliveries. A signature is not Sybil resistance. Reputation penalties, denied
issuance and coin refunds are separate actions governed by published rules.
Paid contribution rewards do not purchase validation or editorial authority.

Provider nodes explicitly opt into resource use with storage, bandwidth,
CPU/GPU, time and spending limits. Show cost, earnings, proof, available/held
balance, issuance provenance and rejected-claim reasons. No background
contribution worker starts merely because this roadmap recommends it.

### F. Covenants, neutral AI resolution and appeal

Publish versioned network covenants covering fraud, coercion, exploitation,
theft, malicious code, consent/privacy violations, prohibited trade and unlawful
or infringing material. Activity-specific and jurisdiction-specific requirements
must be visible before agreement. Covenants are public network rules, not a
company's discretionary product-category whitelist. Changes require transparent
network governance. An AI model is not an infallible legality classifier.

Recommend a neutral AI panel with disclosed model versions and evaluation
criteria. Select eligible independent models/providers through a transparent,
unpredictable process, exclude counterparties and known conflicting interests,
commit to evidence/covenant versions and permit reproducible decision review
where privacy allows. A model's brand or self-description is not proof of
neutrality. Test bias, collusion, bribery, forged evidence and prompt injection;
case material is untrusted input, never executable instructions.

The panel applies the accepted contract and covenant to a minimized encrypted
evidence bundle, produces reasons and a signed structured ruling, and supports
appeal to a separately selected panel. Define disagreement/tie handling,
uncertainty, unavailable models, evidence deadlines, appeal windows and when a
ruling becomes final. No model receives signing keys or autonomous authority to
spend. Ledger validation enforces the narrow authorized contract outcome.

Involved parties pay corresponding Ryncoin fees for actual resolution work.
Disclose model/provider quotes, fee allocation, budget caps, funding/escrow and
appeal costs before case acceptance, under the original agreement's dispute
terms. Fees are contributor compensation, not a platform commission. An
opponent cannot create an unbounded bill simply by filing repeated claims.
Insufficient balance, malicious filings, emergency safety reports and funded
access assistance need explicit rules; reporting abuse is not automatically a
paid contract arbitration case. Price competition must not let a party buy a
favorable ruling or choose its own judge.

Provide authenticated reports, redacted reasons, quarantine and contestable
reputation effects. Preserve evidence only within disclosed retention limits.
Network arbitration does not override applicable law, mandatory legal processes
or participants' external remedies. Generalized paid trading cannot launch
before impartiality, privacy, fee charging, appeal and ledger enforcement pass
conformance and adversarial tests.

### G. Extensibility and access delivery

Publish an open schema/adapter SDK, conformance suite and compatibility policy.
A new activity adds its schema, UI and fulfillment adapter while reusing
identity, discovery, agreements, payments, receipts and policy. Unknown schemas
are displayed safely or rejected; they do not execute arbitrary provider code.
Provider-run execution is the conservative first recommendation. Local execution
requires a separately reviewed sandbox and explicit resource/data permissions.

Extend the content plane with versioned licenses, access entitlements, encrypted
chunked distribution, resumable media delivery, creator/contributor splits and
policy-aware caches. Provide explicit free, paid-access, rental/subscription or
patronage terms through adapters; those models are proposals, not existing UI.
Purchased access cannot guarantee that a recipient never copies downloaded
material. Revocation cannot recall independently saved copies.

## 5. Examples enabled by shared infrastructure

| Example | Provider side | Seeker side | Additional adapter, not a separate economy |
|---|---|---|---|
| P2P goods | Publish goods and available quantity | Find items or post a wanted request | Inventory, delivery, acceptance and returns |
| Hiring | Publish openings; candidates publish consented profiles/resumes | Recruit, apply and agree scope | Applications, private candidate data and engagement terms |
| Consulting or part-time work | Offer skills, appointments or deliverables | Request help, compare proposals and commission work | Booking, milestones and subjective acceptance |
| AI drama/movie creation | Offer generation, editing, voice or rendering | Commission an episode or production task | Production stages, rights and contributor splits |
| Published AI drama/movie | Publish free or licensed viewing access | Discover, view, subscribe or support creators | Media delivery, entitlements and creator payouts |
| Network infrastructure | Offer useful capacity under resource limits | Request compute, delivery, storage or discovery | Measured jobs, challenges and reward-budget allocation |
| Other lawful, ethical value | Define an interoperable offer schema | Define a request and negotiate terms | Reuse the same shared contracts |

A creator earns from viewers' payments, patronage or a disclosed funded reward
program. Posting a movie or generating views does not automatically mint coins.
If a funded viewing reward is introduced, qualified-view evidence, consent,
budget caps and anti-collusion checks are required. AI provenance and likeness,
voice, music and other rights are part of the publishing policy, not evidence
that all such content is automatically lawful.

## 6. Suggested reviewable work packages

These are proposal IDs, not created GitHub issues or approved assignments.

| Order | Proposal | Depends on | Acceptance evidence |
|---|---|---|---|
| 1 | INF-01: durable orders and restart recovery | Current work-order and service hooks | Interrupt every write/ack stage; reload cannot duplicate a purchase |
| 2 | INF-02: participant identity, authorization and recovery design | Signed nodes and owned-device pairing | Revoked device cannot act; recovery preserves history without duplicate funds |
| 3 | INF-03: generic offers/requests and private discovery projections | INF-02 | New activity schema works; secret marker never appears in indexes/logs |
| 4 | ECO-01: Ryncoin asset, protocol issuance and decentralized consensus design | INF-02; covenant/governance review | Specify finality, partition behavior, validator capture defenses and conserved supply |
| 5 | ECO-02: wallet, escrow, refund and reconciliation pilot | ECO-01, INF-01 | Concurrent double-spends, replay, crash, forks and backup restore cannot create value |
| 6 | INF-04: agreements, milestones and dispute protocol | INF-01/03, ECO-02 | Exact terms bind parties; rejected/partial delivery reconciles funds once |
| 7 | ECO-03: network jobs and protocol-issued infrastructure rewards | ECO-02, INF-04 | Useful work verified; self-dealing, duplicate issuance and budget exhaustion rejected |
| 8 | INF-05: covenants, neutral AI disputes, SDK and conformance | Begin covenant design with INF-03; release after INF-04 | Conflicted judge, prompt injection, fee abuse, appeal and unknown schema handled safely |
| 9 | INF-06: licensed access and media distribution | INF-03/04/05, ECO-02 | Interrupted delivery resumes; payment/access/splits reconcile; rights policy enforced |
| 10 | ECO-04: broader internal-economy pilot | Earlier packages and activity-specific review | Providers earn and spend across at least three unrelated activities |
| Later | FX-01: fiat-exchange feasibility and adapters | Stable internal economy and separate approval | Jurisdiction, settlement, liquidity, custody and reconciliation review before launch |

Start with crash-safe work orders and the identity/settlement designs. Generic
listing work can be prototyped before payments, but paid trading must not launch
before escrow, dispute handling, anti-abuse controls and operating rules pass.
A controlled pilot and unrestricted public operation have different gates;
public expansion additionally requires P4 hardening. No calendar promises are
implied by this dependency order.

## 7. Fiat exchange: much later

Design an eventual separate exchange adapter for Ryncoin/fiat buy and sell
orders, quotes, expiry, liquidity, fees, deposits, withdrawals and reconciled
settlement. Rates should emerge from executable demand and supply; no fixed
USD peg, guaranteed redemption, fabricated liquidity or automatic present-day
fiat valuation. If an indicative display is introduced, show source, time and
non-executable status. Fiat and coin legs need explicit settlement-risk rules.

Plan for at least ten major currencies. Initial candidates are USD, EUR, JPY,
GBP, CNY (renminbi), CHF, AUD, CAD, HKD and SGD, with KRW included to cover the
owner's explicit Korean-won use case: eleven candidate codes in total. Final
launch coverage follows demand and jurisdiction readiness, not a permanent
claim that every candidate is in a global top ten. Currency codes and monetary
precision follow [SIX's ISO 4217 maintenance data](https://www.six-group.com/en/products-services/financial-information/market-reference-data/data-standards.html);
market prioritization can use the [BIS 2025 FX survey](https://www.bis.org/publications/202509-commentary-otc-derivatives).

Fiat purchase/redemption, custody and a public exchange require a separate
operating design and qualified jurisdiction-specific review. Keep these adapters
disabled during the initial earn/spend phase. This document neither establishes
an exchange rate nor authorizes a token sale or exchange deployment.

## 8. Decisions still requiring review

- Decentralized consensus, validator eligibility/incentives, quorum safety and governance.
- Wallet recovery, participant privacy, organizational delegation and key custody.
- Coin precision, supply, bootstrap allocation and protocol-issued infrastructure reward rules.
- Escrow deadlines, neutral AI selection, model disagreement, fee allocation and appeal finality.
- Minimum ethical rules, jurisdiction handling, eligibility, licensed activities and personal-data visibility.
- Reward validation, anti-Sybil thresholds, fair newcomer access and payout limits.
- SDK trust boundaries, code execution, schema governance and compatibility.
- Fiat-stage jurisdictions, currency coverage and settlement partners, much later.

Related: [Product milestones](PRODUCT_MILESTONES.md), [Vision](RYNMESH_VISION.md),
[Current architecture](ARCHITECTURE.md), [Service platform](SERVICE_PLATFORM_NEXT.md),
and [Testing strategy](TESTING_STRATEGY.md).
