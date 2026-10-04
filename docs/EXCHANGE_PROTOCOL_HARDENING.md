# Exchange protocol hardening — configured v2 alpha

Design/acceptance record: [issue #89](https://github.com/yeogirlyun/rynmesh/issues/89).
Owner-authorized hardening after the review of #88; dated 2026-10-04.
This changes settlement rules, not the roadmap's product verticals or fiat scope.

## Activation and existing history

The old protocol allowed a participant to reopen a disputed or ruled order and
replace evidence. It also trusted a proposer timestamp to enforce appeal expiry.
These failures were reproduced before fixing them. Passing CI was insufficient
evidence for adversarial settlement guarantees.

A **new `ryn.exchange.v2` manifest and covenant** bind the corrected rules. No
height-dependent rule is silently inserted into a v1 chain. Frozen `legacy_v1.py`
verifies existing v1 certificates exactly, including transactions now prohibited
in v2. The new node can read/reconcile those certificates, inspect balances and
read decryptable deliveries, but cannot cast new v1 votes, submit owner spending,
resume v1 intents or replace that ledger's manifest.

**Existing v1 balances/holds are not repaired, converted, transferred or erased.**
A v1 price already stranded by the reset bug stays visible. An active/funded v1
pilot requires a separate explicit migration/recovery agreement; this patch does
not invent a unilateral administrator payout. Older unupgraded nodes may still
produce v1 certificates; upgrading one node does not secure their old network.
Coordinate operators, preserve the entire original ledger/WAL and keys, and use
fresh separate node homes/keys and a reviewed v2 invitation for a new pilot.
The downloadable v0.7.0 release predates both exchange versions.

## Contract and state rules

- Only working/delivered orders may enter their initial dispute; no reset of
  evidence, round, fee reserves or ruling is accepted.
- One immutable statement per party per round binds each model case. Different
  evidence is possible only in the one appeal. Exact successful receipts reuse
  the same case; failed model processing permits three attempts per judge.
- A waiver prevents that party from appealing that ruling. Second-round rulings
  cannot be appealed. Refund approvals and waivers cannot be spammed repeatedly.
- At most three delivery revisions run within fixed windows. Buyer acceptance
  still binds the exact reviewed plaintext hash.
- Seven days to deliver, two days from first delivery to review, and two days per
  dispute/appeal are stated in accepted terms. Ruling appeal windows remain the
  configured 60 seconds to seven days.
- Explicit timeout operations refund undelivered work, pay delivered work after
  review, split an unresolved first dispute with delivery equally (without
  delivery refund), or retain the first ruling on an unresolved appeal. Final
  rulings settle after the appeal window. These are accepted **fallbacks**, not
  claims that an AI evaluated the work. Committed model fees stay paid; unused
  reserves return. Opposing-party disappearance does not veto the fallback.
- Deadline settlement, finalization and waivers bind the reviewed order outcome
  hash. Changed decisions cannot silently use a stale confirmation.

None of these actions runs automatically. The UI requires review; users refresh
and explicitly settle. Uncertainty can still be induced; the bounded fallback
contains the hold, and its incentive/fairness tradeoffs need public-pilot review.

## Clock, consensus and recovery boundary

New votes require the proposal timestamp to be within 30 seconds of the local
clock. They also check the actual clock against action deadlines: a backdated
appeal cannot reopen a window and a forward-dated finalization/timeout cannot
pay early. Historical verification and returning an exact already-persisted vote
use the original certified rules/time, not today's clock. Replay applies each
transition once, preserving monetary checks while eliminating duplicate copies.

Durable locks stay in place. **A stale partial proposal without quorum can halt
the alpha after its freshness window**, even if all nodes reconnect. Previously
persisted votes/certificates remain recoverable, but remaining first votes may
be forbidden. Safe timed round-change, proposer replacement and recovery from
this state remain required before public use. Do not erase locks, force a new
timestamp or claim timeout settlement works without available consensus.

Votes whose parent/height are already current do not synchronize against every
other validator first. Proposer RPC budgets allow bounded synchronization. Model
requests run concurrently within one explicit operation and join before it
returns; they do not become workers, polls or automatic follow-ups. A real slow
HTTP response test covers the nonproposer/quorum case. An offline current
proposer still halts this fixed-roster protocol.

## Resource and outbound-network boundary

The pilot retains at most 10,000 committed operations, with 100 free admissions
per root identity and six simultaneously open orders. Each order reserves 24
operations before holding coins. New profiles/listings/transfers/agreements
cannot consume this budget; bounded order actions close through exhaustion and
release unused capacity. There is no participation or platform fee. New activity
can stop at the pilot budget; unlimited spend/admission is not promised.

Local admission reserves 16 MiB per open order plus 16 MiB headroom within the
128 MiB database/WAL budget. SQLite checkpoints reduce retained WAL overhead.
Closing is not rejected by the ordinary admission-size check. This is a
conservative reservation for the bounded payload/lifecycle, not a guarantee
against actual disk failure, hostile local storage changes or global scale.

Work caches are keyed by provider, with ten-minute expiry and three failed
outbound attempts per window. Changing job IDs cannot fill them. Successful
registration reclamation follows explicit reconciliation. Judge caches/attempts
follow active immutable cases; explicit reconciliation reclaims closed cases.
Successful receipt reuse and cleanup preserve original signed intents,
certificates and vote locks. Work and judge attempt budgets are separate.

Claimant private endpoints outside the reviewed roster require each witness's
explicit `registry_lan` owner opt-in. Literal public endpoints and reviewed
roster endpoints remain eligible. Metadata/link-local/unspecified/multicast and
unapproved loopback restrictions remain. LAN opt-in allows claimant-selected
requests inside the operator's LAN; it is not a global admission/category gate.

These limits do not solve Sybil abuse. Judge selection still uses the first six
eligible quoted identities, not a proposer-influenced hash. Fair assignment,
independent operators, model robustness, real neutrality evaluation,
permissionless admission, consensus audit and round-change remain future gates.

## Verification

- Original reset and evidence-replacement regressions fail on the pre-fix code.
- Role × status × round matrix verifies accepted operations and rejected inputs
  leave state, evidence, balances and nonces unchanged.
- Exact expiry, waived appeals, stale confirmations, backdated late appeals and
  forward-dated early settlement are exercised.
- Exhaustion closes existing holds; repeated work IDs, immutable cases, model
  attempts, revisions, refunds and waivers are bounded.
- Old v1 certified history, including the reset exploit, replays unchanged;
  v1 writes and in-place v2 replacement are rejected.
- Slow nonproposer HTTP responses exceed the real four-second timeout while the
  three healthy validators certify. Every temporary server is stopped.
- Existing earn/buy/deliver/spend/dispute/settle acceptance, package checks and
  full regressions remain required. Controlled models test mechanics only.
