# Rynmesh Vision (North Star)

Status: direction updated from owner instructions, 2026-10-03. This document
states intended direction; [Architecture](ARCHITECTURE.md) describes current
implementation. [Product milestones](PRODUCT_MILESTONES.md) separates released,
merged and proposed work. The detailed infrastructure recommendations are in
[Network economy roadmap](NETWORK_ECONOMY_ROADMAP.md).

**Principles** below are owner-defined direction. **Open designs** are unresolved
and must not be silently settled in code. Ryncoin, generalized trading and neutral
AI arbitration are planned, not capabilities of the current release.

## 1. Purpose: infrastructure for providers and seekers

Rynmesh is an open-source peer network where people and nodes provide value,
find value and exchange value directly. Anyone can enter and participate for
free. Participants may offer or seek content, goods, work, applications,
experiences, services or other lawful, ethical value. Commerce, recruiting,
consulting and AI drama/movies are examples, not an approved-category ceiling.
The same participant may be a provider, seeker and infrastructure contributor.

There is no platform middleman collecting a commission, owning the audience or
controlling the network's permissible commercial categories. Prices and
commercial terms are determined by demand, supply and agreement between parties.
Network covenants establish published legal/ethical principles rather than a
company's discretionary rules. Free participation does not mean that other
people's labor, goods, compute or content must be provided without compensation.

Those who contribute more verified useful value can earn more. Those who consume
more services pay for the amount, capacity or quality they choose to use under
accepted prices. There is no promised revenue for uploading, registering a node
or generating fake activity. Technical and economic rules must prevent
self-dealing and fabricated contribution from substituting for actual value.

The node, client, protocol, schemas and rules are open. No proprietary component
is a required trust root. The [Avaryn separation decision](DECISION_AVARYN_SEPARATION.md)
continues to apply: any third-party attestation provider is optional and competes
through open verification seams. Human users can participate directly; AI can
assist within explicit owner permissions.

## 2. Owner, agent, node and network roles

- **Owner/participant:** controls identity, data, permissions, resource limits,
  commercial terms and wallet authority. Can provide and seek simultaneously.
- **Agent:** optional assistant for discovery, drafting, matching and work;
  operates within a bounded owner-approved envelope, never owns the wallet.
- **Ryn node:** local enforcement point for identity, privacy, storage,
  verification, peer communication and authorized actions.
- **Infrastructure contributor:** supplies useful registry, discovery,
  serving/transit, availability, storage, compute or other network capacity.
- **Ledger validator:** verifies protocol-authorized state transitions under a
  separately reviewed decentralized consensus design.
- **Neutral AI resolution provider:** applies covenants and accepted agreements
  to bounded cases and produces signed rulings; cannot edit balances directly.

Existing automatic public-content discovery is current product behavior. New
paid participation, infrastructure jobs, continuous matching, monitoring or
other autonomous activity require explicit owner opt-in and limits. Installing
an app or reading this roadmap is not authorization to spend, publish, run
contribution workers or schedule recurring work.

Agent budgets cover Ryncoin, model cost, bandwidth, storage, CPU/GPU and time.
High-risk actions retain explicit approval boundaries. Local audit records must
explain what happened, why, which authority allowed it and what it cost.

## 3. One shared infrastructure, extensible activities

The network should support both signed **offers** and signed **requests**:
providers publish capabilities or value; seekers publish needs. Discovery and
matching lead to direct negotiation, an accepted agreement, delivery and
settlement. No activity needs a bespoke coin or a platform-owned marketplace.

Common infrastructure covers participant identity, selective discovery,
availability, negotiation, versioned agreements, resource/inventory reservation,
durable orders, delivery evidence, escrow, refunds, contributor splits,
reputation, covenant enforcement and appeal. New activities extend an open
schema and fulfillment adapter and pass conformance tests.

Current signed content and service work orders are foundations. They do not yet
provide generalized commerce, employment agreements or paid media access.
Provider-side execution is the recommended conservative extension; arbitrary
service code must not execute on a seeker's machine without a reviewed sandbox
and explicit consent.

Content publishing continues to use signatures, hashes, provenance and safety
receipts. Future licensing, paid access, streaming and creator splits reuse the
same economic and policy infrastructure. Copies already downloaded cannot be
recalled by revocation. A hash proves byte identity, not ownership, legality,
quality or a person's satisfaction.

## 4. Ryncoin economy: earn and spend inside the network first

Keep these concepts distinct:

| Concept | Status and purpose |
|---|---|
| **Rynmesh Credits** | Implemented nontransferable reputation/distribution evidence |
| **Development Task Balance** | Implemented simulated hold/settle/release accounting, isolated from reputation scoring |
| **Ryncoin** | Planned spendable unit for direct participant payments and network rewards |

**Principles:**

- Providers earn Ryncoin from buyers/seekers for agreed useful value.
- Basic system services, including useful registry operation, earn Ryncoin
  issued by the protocol under open, measurable contribution rules.
- Paid service prices emerge from supply and demand. There is no platform rake
  or obligatory participation/entry charge.
- More verified contribution can earn more; more service consumption pays for
  more accepted work. Quantity, quality, capacity and scarcity affect agreements.
- Customer payments transfer existing coins. Only authorized protocol issuance
  creates new coins; registration, self-attestation, uploads or fake views do not.
- Coin ownership does not buy reputation, covenant exceptions, editorial control
  or favorable dispute rulings. Contribution evidence remains separately visible.
- Issuance, reward computation, balances/finality proofs and protocol changes
  must be verifiable under public rules. No single registry or company can mint
  or finalize all payments at its own discretion.
- Start with network earn/spend transactions. Fiat exchange, redemption and
  conversion belong to a much later, separately reviewed stage.

Decentralized spend finality, integer amounts, wallet recovery, conserved supply,
escrow, partial settlement and refunds require a reviewed design. Local signed
files cannot independently prevent double-spending across peers. Whether the
shared ledger uses a blockchain or another consensus design remains open; the
no-middleman direction does not authorize an untested consensus implementation.

Supply, reward issuance/decay, bootstrap allocation and scarce-capacity rules
remain open. Earlier halving-style suggestions are options for review, not an
adopted schedule. Existing reputation and simulated balances do not automatically
convert into Ryncoin. Internal usage still needs legal and operating review;
delaying fiat does not make those requirements disappear.

## 5. Transparent, fair and lawful participation

### 5.1 Open entry and transparent rules

Anyone may join and participate for free, with conformant open-source software.
Running many nodes is not itself a contribution or a reason for exclusion.
Useful work is rewarded; abuse is evaluated through published receiver-side
rules. Reviewing a build does not prove that a remote peer runs trusted code.
A proprietary approved-client list or a platform account must not become the
universal entry gate.

Code, schemas, covenants, economic parameters, fee calculation, governance and
verifiable decision procedures are public and inspectable. No hidden ranking
rules, secret reward multipliers or undocumented platform privileges. Transparent
network auditing must reconcile issuance, payments and dispute outcomes.
The precise visibility of personal messages, resumes, prompts and contract
bodies is an explicit open design question; public audit commitments alone do
not imply their plaintext is public.

Fairness requires anti-Sybil, anti-collusion, newcomer discovery and bounded
validation/distribution concentration. Current EigenTrust and sublinear weighting
are primitives, not proof of these properties. Buying or farming coins must not
purchase network control. Validator eligibility and governance need their own
public threat model and accountable rules, without charging users to enter.

### 5.2 Covenants and neutral AI dispute resolution

Publish versioned network covenants for lawful, ethical conduct: consent,
privacy, rights, honesty, non-exploitation and prohibited harmful behavior.
Activities may require jurisdiction-specific eligibility or credentials. Rule
changes follow transparent governance; they are not a middleman's commercial
veto. No model can guarantee universal legal or ethical correctness.

Neutral AI models resolve network disputes by applying the accepted agreement
and covenants to evidence. Selection must be transparent and resistant to
counterparty conflicts, purchased outcomes and provider/model collusion.
Models, versions, covenant references, authorized panel, fee terms and reasoned
rulings are auditable. Case content is untrusted data, including prompt-injection
attempts. Uncertainty, model disagreement and unavailable judges need defined
handling rather than silent arbitrary judgment.

Involved parties pay disclosed corresponding resolution fees in Ryncoin for the
work performed. Terms define allocation, caps, case funding and appeal costs;
filing a claim must not impose an unbounded bill on another participant.
Resolution fees compensate providers, not a platform. Appeals use independent
selection and explicit finality stages. Validating ledger peers execute only
authorized contract outcomes; AI judges never hold wallet keys.

Network resolutions do not supersede applicable law or external remedies.
Emergency abuse reports are distinct from paid commercial arbitration.
Operating responsibility, jurisdiction handling, evidence retention and safety
controls require review before public economic use. The alpha keyword scanner
is insufficient for these purposes.

### 5.3 Infrastructure, scale and useful-work proof

Direct HTTP, encrypted relay/mailbox paths, direct ICE/UDP and ordinary-peer
transit are implemented foundations. Physical NAT/public-egress validation,
registry federation, availability/delivery challenges, capacity allocation,
bounded queues, quota enforcement and adversarial scale testing remain work.

Infrastructure rewards need independent evidence of useful demand served under
protocol rules. A node cannot earn merely by claiming to operate a registry.
The network must reject fabricated requests, duplicate claims and colluding
validators. Resource use is opt-in and constrained by the owner's limits.

## 6. Stage boundaries

Current alpha has no production Ryncoin, universal paid marketplace or neutral
AI arbitration. The proposed extensions must not relabel reputation or simulated
Task Balance as an existing currency. Fiat conversion is not an initial feature.

Free open participation, compensated useful work and no platform commission are
compatible. Paid model calls, goods, licensed content, infrastructure work and
dispute work are prices for contribution, not a fee to belong to the network.
Open code does not itself solve double-spending, privacy, collusion or fairness.
Protocol safety must hold against hostile implementations.

## 7. Milestone sequencing

Use the same milestones as [Product milestones](PRODUCT_MILESTONES.md):

| Stage | Scope | Gate |
|---|---|---|
| P1/P2 | Companion and trusted Friend Mesh, implemented with remaining field gaps | Recorded release/physical acceptance and recovery evidence |
| P3 | Durable generic work, identity, offers/requests, agreements and SDK | Crash/privacy/conformance evidence; approved designs |
| P4 | Open-network safety, covenants, anti-Sybil/collusion and scale | Adversarial evidence plus operating/legal review |
| P5 | Decentralized internal Ryncoin, escrow, protocol rewards and AI disputes | Reviewed consensus/economy; conserved supply and fair paid resolution |
| P6 | Extensible value exchange across unrelated activities | Reuse common contracts without bespoke wallets; activity-specific readiness |
| P7 | Demand/supply fiat exchange, much later | Separate operating decision, jurisdiction review and reconciled settlement |

Designs and private test networks can proceed before public readiness. Public
unrestricted operation requires P4; paid pilots need the relevant P5 controls.
No milestone is achieved by a roadmap entry or by test count alone. New proposals
are review material, not approved issues, implementation or launch dates.

## 8. Open designs requiring explicit review

1. Participant identity, wallet custody/recovery and organizational delegation.
2. Decentralized consensus, validator selection/rotation and capture resistance.
3. Coin precision, supply, bootstrap allocation and protocol reward issuance.
4. Useful-work measurement, anti-Sybil claims and fair discovery/concentration limits.
5. Generic schema governance, compatibility and service execution boundaries.
6. Covenant governance, jurisdiction handling and public versus personal-data visibility.
7. Neutral AI selection, evidence privacy, fees, appeal and enforceable finality.
8. Agent budgets, explicit opt-in and audit semantics.
9. Fiat-exchange design and currency coverage, at the much later P7 stage.

The optional-attestation boundary is resolved by the existing
[Avaryn separation decision](DECISION_AVARYN_SEPARATION.md); no proprietary
service or algorithm becomes the network's trust root.

## 9. Related documents

- [Architecture](ARCHITECTURE.md): current components and implementation limits.
- [Product milestones](PRODUCT_MILESTONES.md): dated baseline and staged proposals.
- [Network economy roadmap](NETWORK_ECONOMY_ROADMAP.md): infrastructure, work packages and acceptance requirements.
- [Service platform](SERVICE_PLATFORM_NEXT.md): delivered seams and next extensions.
- [Testing strategy](TESTING_STRATEGY.md): evidence required for each claim.
- [Contributing](../CONTRIBUTING.md): design approval, issue and PR workflow.
