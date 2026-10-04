# First digital-value exchange milestone

Implementation: `rynmesh/exchange/`, node owner/peer HTTP routes, the **Exchange**
webapp screen, and `rynmesh-exchange` invitation tooling. Design record:
[issue #87](https://github.com/yeogirlyun/rynmesh/issues/87).
Current protocol: **`ryn.exchange.v2`**, hardened in [issue #89](https://github.com/yeogirlyun/rynmesh/issues/89).
[Protocol hardening](EXCHANGE_PROTOCOL_HARDENING.md) records activation, deadlines, quotas and adversarial evidence.
This is an explicitly configured alpha network; it is not a public currency
launch or evidence of production BFT, permissionless rewards, or neutral AI.
It is newer than the v0.7.0 downloadable release.

## What a participant can do

1. Review a public network invitation and join without an account or entry fee.
2. Create a free public profile with a node signing identity and encryption key.
3. Post an **offer or request** under any descriptive category, with a price or
   budget in Ryncoin. Listings and proposed terms are public; no friendship is
   needed and no category whitelist exists.
4. Propose exact scope and a negotiated price. The other party reviews the exact
   terms, covenant, selected model identities, fees, appeal window and reserves.
5. Agree the signed terms. The buyer reserves the price plus their dispute
   reserve; the provider reserves their dispute reserve. Nothing is held merely
   for posting a listing or proposal. Platform commission is **zero**.
6. Deliver encrypted digital text (up to 32 KiB). Read and verify it, then accept
   that exact delivery hash and pay the provider. A later revision requires a
   new review. Unused dispute reserves return to both parties.
7. Request a mutual full refund, or submit a dispute with one immutable encrypted statement per party per round.
8. Review a three-model decision, appeal once to three different models, or
   settle after the appeal window. Both parties can waive the remaining window.
9. Review and explicitly apply the agreed deadline outcome when work, review or a case expires. Nothing settles automatically.
10. Spend earnings on another participant's work or make a reviewed direct
   transfer to a registered identity. Direct transfers have no escrow or dispute
   procedure. Wallet amounts use integer millionths of one Ryncoin.

The ledger, supply, covenant, order metadata, signatures, delivery commitments,
model names, fee schedules and ruling reason codes are inspectable. Delivery
and evidence plaintext are encrypted; private keys stay local. The UI labels
public fields and private submissions. This implements the working privacy
assumption in the roadmap; publishing everyone's private material was not
inferred from the owner's request for transparent rules.

## Starting a small network

Run distinct normal Ryn nodes on mutually reachable literal-IP HTTP/HTTPS
endpoints. This exchange protocol currently uses direct HTTP, not the existing
mailbox or ICE/transit as an automatic payment transport. Start with a LAN or
controlled routed network. Keep local owner controls private.

The public manifest fixes:

- Four to eight **distinct validator signing identities**, in proposer order.
- Either no AI panel, or six to twelve distinct judge node identities and exact,
  distinct Ollama model names. Judges cannot also be validators in this alpha.
- Issuance cap, one-time registration reward, quoted fees, and appeal duration.
- The versioned digital-service covenant in the source code.

Choose independently controlled operators and model families; different keys
or names alone do not prove independence. Eight judges allow two of those
identities to trade while leaving six independent judges available. With six
judges, buyers/providers must be outside those identities. An order selects the
first six eligible judges in published roster order, excluding its parties;
three decide and three hear an appeal. All selected fees bind the accepted terms.
A network with no judges supports acceptance and mutual refunds only; the terms
and UI disclose that limitation before agreement.

Create the public invitation without starting any additional service:

```sh
rynmesh-exchange --name "Our digital-work alpha" \
  --validator http://192.168.1.11:8791 \
  --validator http://192.168.1.12:8791 \
  --validator http://192.168.1.13:8791 \
  --validator http://192.168.1.14:8791 \
  --issuance-limit 100 --work-reward 1 --appeal-window 86400 \
  --output exchange-network-v2.json
```

Add six to twelve repeated `--judge ENDPOINT EXACT_MODEL FEE_IN_RYNCOIN`
arguments for paid AI disputes. These endpoints must be distinct node identities.
Verify the displayed identities with their operators; fetching a signed identity
proves key possession, not who operates it. Share the public invitation and have
**every validator, judge and participant** join it in Exchange. Joining fixes the
network fingerprint; it cannot overwrite an existing ledger with a new network.
An existing v1 ledger stays read-only; it cannot join v2 in place. Use separate fresh node homes and keys for a new pilot, and preserve the old keys/history. Balances are not imported or converted. Existing voting keys must never be reused in a reset network or restored with
outdated vote locks. A new isolated pilot needs new node keys and homes.

Each judge operator explicitly enables **quoted judge model** in Wallet &
network. The exact model must already be installed and reachable through the
node's configured Ollama. Missing models do not silently fall back. A judge
must provide private evidence processing acceptable to both parties.
No model downloads, judge opt-ins, polling, node launch or recurring reward jobs
happen automatically through this feature.

For isolated localhost testing only, use invitation tooling `--allow-loopback`
and node environment `RYNMESH_EXCHANGE_ALLOW_LOOPBACK=1`. Do not enable remote
owner control to make peer payments work; the peer and owner routes differ.
If an existing `RYNMESH_NETWORK_KEY` protects the group, all participating nodes
and the invitation tool need that same configured secret. It never appears in
the invitation.

## Earning from zero coins

Create a profile, explicitly enable **registry participation**, enter your
reachable node endpoint, and request verification. Independent validators
challenge that node to serve the exact public network manifest and sign their
receipts. More than two-thirds of the configured validators must attest, the
provider cannot attest its own work, and more than two-thirds then approve the
issuance operation. The published finite budget cannot be exceeded.

Private claimant endpoints outside the manifest roster are blocked by each witness unless its operator explicitly enables **private registry probes** in Wallet & network. This grants outbound LAN access for this controlled pilot; it is not enabled automatically. Public literal-IP endpoints and explicitly reviewed roster endpoints use the usual signed challenge.

Pending receipts are keyed by provider, not arbitrary job IDs. Retry the same verification ID; stale receipts and attempt counters are reclaimed after ten minutes on explicit operations. Completed registration receipts are reclaimed after reconciliation. At most three failed outbound attempts per provider per ten-minute window run.

This alpha rewards **one verified registry registration per signing identity**.
Repeated job IDs or new job IDs for the same identity cannot earn another reward.
It is an entry-path prototype, not proof of useful ongoing demand, uptime, or
Sybil resistance: a person can create multiple keys. Do not fund or advertise
an unrestricted public reward market with this rule. Later infrastructure work
needs demand-backed assignments, availability evidence and capture defenses.

Service payments and judge fees transfer existing coins; they do not mint new
coins. Nontransferable Rynmesh Credits and simulated Task Balance are unchanged
and cannot convert into Ryncoin. The wallet's earned counter describes received
coins, not a verified quality score or reputation weight.

## Disputes and transparent fees

Each party can submit **one immutable statement per round**. Both statements bind a single case; collected receipts cannot be invalidated by replacing evidence. Both parties must submit evidence for the current round. Their statements and
any readable delivery are sealed to that panel; the judges verify the delivery
against its original hash. Models apply the public covenant and agreed terms,
return a provider price share plus a small public reason code, and sign the
same case commitment. Evidence is untrusted model input and cannot authorize
tools or network actions. Raw prompts and evidence are not ledger records.

All three consistent receipts are required. The provider shares must be within
10 percentage points; the median share becomes the decision. Fees for that
round are paid only when that decision is committed. Each round's buyer pays
its rounded-up half and provider pays the remaining half. Agreement reserves
enough for **both rounds**, including odd-unit rounding. Unused reserves return
on acceptance, refund or final settlement. There is no separate platform fee.

The price stays held while a case is open. Missing evidence, unavailable models,
uncertainty and disagreement do not create a new ruling or charge judge fees.
Each judge permits at most three model attempts for that immutable case; successful
receipts are reused. No resubmission/reset of a case is allowed. Parties may agree
a full price refund at any unsettled stage; committed judge fees remain paid.
A first ruling permits one appeal to the other three judges. A party who waived
its appeal cannot then open one. The second ruling has a settlement window, with
no further appeal.

**Deadline outcomes bind the accepted terms:**

- Delivery: seven days from agreement. If no delivery is committed, either party
  can explicitly settle a full price refund after the deadline.
- Review: two days from the first delivery. At most three delivery revisions are
  allowed, within the original delivery/review windows; revisions do not extend
  review. After review expires, either party can explicitly pay the full price.
  Open any dispute before that deadline.
- Each dispute/appeal: two days. An unresolved first dispute with delivery splits
  the price 50/50. Without any delivery it refunds the full price. An unresolved
  appeal retains the first ruling. These are contractual fallbacks, not model
  findings; malicious or unavailable evidence can trigger them.
- A completed ruling uses the configured appeal window. After it expires, apply
  that ruling. Both parties can waive the window to finalize it sooner.

Unused reserves return and already committed judge fees remain paid. Every
finalization, waiver and deadline operation binds the outcome hash the user
reviewed; a changed ruling/deadline requires fresh review. Refresh manually after
expiry. No timer, automatic payout, polling or administrator override exists.

These rules require available consensus. They do **not** establish impartiality,
model robustness, correct legal conclusions, or resistance to shared bias.
Before public use, evaluate independent models/operators, fallback incentives,
service-specific deadlines, judge replacement and independently governed recovery.

## Failure, identity and audit

Every owner mutation binds the account identity the client reviewed; a stale
tab cannot silently pay from another selected account. Account switching is
serialized with in-flight mutations and blocked while an intent is pending.
Public keys use canonical encodings so aliases cannot count as distinct keys.
Every owner mutation retains its original operation ID and signed, encrypted
intent in SQLite. Manual **Refresh network** reconciles certified blocks;
**Resume saved operations** helps the exact existing proposal or intent.
An uncertain response never creates a replacement payment automatically.
Concurrent submissions are serialized by a rotating proposer. Every validator
persists one vote lock per height before responding. More than two-thirds of the
fixed roster must sign the same proposal; a certificate is verified before any
balance changes. Replaying the log verifies signatures, parent links, nonces,
terms, issuance, holds and conserved supply after restart.

This approval log deliberately has **no round-changing BFT protocol**. An offline
or malicious proposer can halt progress; conflicting durable votes can also
halt it. Never delete locks to force progress. Safety assumes honest validators
retain their current keys and vote history, and fewer than one-third of the
roster violate the rules. VM rollback or stale backups can break that assumption.
New votes require timestamps within 30 seconds of the local clock and enforce actual local deadlines, so backdating cannot reopen an expired appeal or pay an early timeout. Historical replay and exact already-persisted votes do not use current-clock freshness. A stale partial proposal lacking quorum can remain locked after an outage; this alpha has no safe round-change/retimestamp procedure. Keep clocks synchronized and do not delete locks.
No permissionless admission, validator changes, public finality guarantee,
anti-capture mechanism or consensus audit is claimed.

Root identities can grant/revoke signing devices. A grant authorizes spending;
it does not transfer private decryption keys. Deliveries and private evidence
require the original account's messaging key. Full key rotation/recovery is
future work. Back up the original node keys **and the entire exchange ledger**,
including SQLite WAL and durable locks, privately and consistently. Browser
cleanup is not ledger erasure. Local erasure/revocation cannot recall published
receipts or copies held by peers. A superseded intent did not execute because
another authorized operation used that account nonce first; inspect its receipt
and wallet before making a fresh request.

Bounds: at most 10,000 committed operations per pilot, 100 free admissions per
root identity, and six open orders across the pilot. Opening an order reserves
24 operations for its bounded lifecycle; ordinary profiles/listings/transfers
cannot spend that reserve. Closing returns unused capacity. Order progress and
closing remain possible when new activity is rejected by the admission budget.
Per-node admission also reserves 16 MiB per open order plus 16 MiB headroom within
the 128 MiB database/WAL budget. These are conservative pilot limits, not proof
of unlimited storage. Actual disk exhaustion can still halt a node.

Registry caches reclaim stale/completed work; judge caches follow the immutable
active cases and are reclaimed after explicit reconciliation closes them. Cache
cleanup never removes certificates, original intents or vote locks. Expiry
quotas limit resource abuse but do not solve Sybil attacks or fair public admission.
The first six eligible quoted judges still serve a case; independently governed
fair selection remains work. Replica verification applies each operation once
per block rather than twice; replay still copies state and has scaling limits.

## Examples and remaining scope

- **Translation → editing:** Alice earns a registration coin, requests a short
  translation for 0.25, and accepts Bob's private delivery. Bob spends his income
  on Alice's editing. The same ledger records both services, with zero commission.
- **Consulting:** post a request for a small technical review; agree public scope
  and a price, then deliver the written findings privately. Hiring advice,
  recruiting copy or resume review fit this written-delivery path. Never put a
  private resume into the public listing or agreed scope.
- **AI drama:** commission a script, storyboard text or subtitles, deliver it
  privately, and get paid. Full movies, rendering integration, paid viewing,
  rights/licensing, streaming, royalties and view-funded rewards remain adapters
  to implement; posting a movie or fabricating views currently mints no coins.

Physical commerce/fulfillment, hiring application workflows, automatic matching,
media entitlements/splits, multiple staged milestones, arbitrary executable
plugins, production governance and fiat conversion remain later stages.
The generic categories do not implement those entire product experiences.

Run the deterministic mechanics acceptance explicitly:

```sh
PYTHONPATH=. python scripts/exchange_e2e.py --output /tmp/exchange-result.json
python -m pytest tests/test_exchange.py -q
cd webapp && npm test -- --run src/screens/Exchange.test.tsx
```

The HTTP acceptance starts 12 temporary localhost surfaces, completes earn/buy/
deliver/spend/dispute/settle, checks agreement across replicas and absence of
private plaintext in SQLite, and stops every server before returning. Its
controlled model adapters test mechanics only. A separate six-node HTTP fault test exercises a nonproposer whose responses exceed the real four-second timeout and stops all temporary servers. Distinct-egress/NAT, installed
macOS UI use and actual independent model impartiality remain acceptance gates.
