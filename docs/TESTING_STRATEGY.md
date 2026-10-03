# Testing strategy: what "working" means at each layer

Status: adopted 2026-09-04; implementation claims reviewed 2026-10-03.
This document defines the evidence a change must produce before it counts as delivered, and the gate each milestone must clear
before it is claimed. Section 5 separates the current automated foundation from
remaining physical and adversarial acceptance gaps.

Two audiences: a contributor deciding what to write alongside a feature, and a
maintainer deciding whether a milestone can be called done.

## 1. The layers, and what each one actually proves

| Layer | Runs | Proves | Cannot prove |
|---|---|---|---|
| Backend unit and integration (`tests/`, pytest) | Every push, CI `backend` | Node logic, protocol, crypto, storage, and route behaviour | That the desktop app starts, or that a user can complete anything |
| Webapp component (`webapp/src/**/*.test.tsx`, vitest) | Every push, CI `webapp` | A screen renders its states and its interactions call the right client methods | That the screen is reachable, or that the backend agrees with the fake client |
| Typecheck and build (`tsc -b`, `npm run build`) | Every push, CI `webapp` | The webapp compiles and bundles | Any runtime behaviour |
| Packaged node (CI `packaged-node`) | Every push | The release form serves its own UI with no dev server | Anything about the desktop shell |
| Desktop compile (CI `desktop-compile`, both macOS arches) | Every push | The Tauri shell builds, the bundled daemon boots and answers `/health`, and the bundled inference runtime resolves | That the app is usable |
| Service end to end (CI `llm-e2e`) | Every push | Two real nodes exchange private LLM work over strict P2P, over the encrypted relay, and now over the peer mailbox, in Docker | Behaviour across real networks or NAT |
| Friend product end to end (CI `friend-product-e2e`) | Every push | Fresh-node first sharing through an HTTP mailbox, quota/recovery without duplicate messages | Distinct public egress or every device-sync path |
| Peer transit (CI `peer-transit-e2e`) | Every push | Three-node direct ICE/transit and persistent-worker soak smoke | Arbitrary real NATs or production-scale availability |
| Digital exchange HTTP (`scripts/exchange_e2e.py`) | Explicit local run and CI backend | Twelve temporary HTTP surfaces; earn/buy/deliver/spend/dispute/settle, replica agreement and private plaintext checks | Public BFT, distinct-egress transport or real model neutrality |
| Physical acceptance (`docs/acceptance/`) | By hand, per gate | What only real machines on real networks can show | Nothing repeatable |

The table's right-hand column is the point. Each layer has a blind spot that
the next one covers, and the last two are where a shipped product actually
lives.

## 2. What every change ships with

**Backend change.** Tests that exercise real behaviour, not mocks of it. A
test that asserts a mock was called proves the test's own wiring. Where the
code touches the filesystem, the network, or a subprocess, drive the real
thing: a temp directory, a loopback server, a fake executable. Regression
tests name the defect they prevent, so a future reader knows what breaks if
they delete it.

**Webapp change.** A component test per state the user can actually reach,
including the failure states. A screen that renders a spinner, an error, and a
result needs all three covered, because the error path is the one users hit and
the one nobody exercises by hand.

**Anything with a schedule, a retry, or a background worker.** A test that
proves the timing contract, not just the happy call. The worker registry's own
suite is the model: it proves a delayed first run is actually delayed, that a
crash restarts, and that shutdown is bounded.

**Anything that writes a record.** A format-preservation test asserting the
bytes on disk, and a worst-case size test where the record can grow with use.

**Anything that carries a secret, a prompt, or a body.** An explicit test that
the secret does not appear in logs, status output, error messages, or the
public view of a manifest. Plant a marker string and assert its absence. This
is not optional: it is the one class of defect that cannot be noticed by using
the product.

## 3. What every user-facing feature ships with

A feature is not done when the code works. It is done when someone who has
never seen it can complete the task and we can prove it still works next month.

1. **A completable path.** One test that walks the user's actual sequence end
   to end, at the highest layer that can run it in CI.
2. **Every failure state reachable and handled.** Enumerate what can go wrong
   from the user's side, not the code's: no network, no model, no peer, an
   empty result, a slow result, a denied permission. Each gets a state the UI
   can show and a test that shows it.
3. **A cold-start check.** The feature works on a fresh node home with no
   prior state. Most feature bugs live in the empty case.
4. **Restart survival.** Whatever the feature persists survives a restart, and
   a partially completed flow resumes or fails visibly rather than silently.
5. **An acceptance script.** Numbered manual steps a person can follow on a
   real machine, stored under `docs/acceptance/<feature>/`, recording timings
   and safe error codes. Never message bodies, prompts, model output, invite
   secrets, keys, or absolute paths.
6. **A stated non-goal.** What the feature deliberately does not do yet, so
   the gap is a decision rather than a surprise.

## 4. Milestone gates

A milestone is claimed only when its gate is met with recorded evidence. These
extend, and do not replace, the safety and stewardship gates in
[`RYNMESH_VISION.md`](RYNMESH_VISION.md).

**P1 Ryn Companion.** A fresh install on a supported desktop reaches real
recommended content, opens an item, and records feedback, with no
configuration. Every screen on that path has component tests for its loading,
empty, error, and populated states. Cold start is measured on the packaged app,
not a dev server.

**P2 Friend Mesh.** Two nodes on separate networks complete invite, accept,
first send, and revoke, verified automatically by the two-node CI job and once
by hand from distinct public egress. A revoked friend's next request is
refused, and a rediscovered peer does not resurrect the old relationship.
Invite secrets appear in no log, export, or acceptance artifact.

**P3 Working Agent and Services.** The strict public peer-to-peer acceptance
run is recorded from a genuinely distinct egress. A managed local model
installs, answers, and survives a restart on a machine with no Docker. Task
settlement is idempotent under interruption at every step, proven by tests
that kill and resume.

**P4 Open-network hardening.** Adversarial simulation, not unit tests: Sybil
clusters, collusion, and brigading against the trust weighting, with results
recorded before any untrusted-peer interaction is enabled.

**P5 Internal Ryncoin (proposed).** Supply conservation, integer balances,
concurrent double-spend rejection, consensus forks/partitions/validator failure,
replay/crash/restore safety, escrow/refund/split correctness and deduplicated
protocol issuance. Neutral AI cases test conflicts, forged evidence, prompt
injection, model disagreement, fee limits, independent appeals and verifiable
ruling execution. Public economic operation additionally requires P4; a private
test network must disclose its validator/trust limitations.

**P6 General-purpose exchange (proposed).** Complete provider and seeker paths
across at least three unrelated activity schemas without separate ledgers.
Private-data canaries, licensing, inventory/capacity races, subjective acceptance,
cancellations and disputes are exercised against real persistence and transport.
Joining and basic participation introduce no entry fee or platform commission.

**P7 Fiat exchange (much later).** Separate jurisdiction/operating readiness,
quote expiry, liquidity and two-leg settlement/reconciliation evidence before
any live fiat adapter. No testing scheme implies permission to deploy.

## 5. Current evidence and remaining gaps

At main `fa85833` (2026-10-03), integration validation recorded **1,661 backend
tests and 383 webapp tests**; the frontend has **61 test files**, including Home,
Digest, Search, Settings, Ask, friend/device workflows and service experiences.
The [post-merge CI run](https://github.com/yeogirlyun/rynmesh/actions/runs/37137335701)
passed all nine jobs. Component tests and friend product E2E are implemented;
earlier statements that those foundations were absent are superseded.

Counts are a dated snapshot, not line/branch coverage or milestone acceptance.
Coverage measurement is not established here as proof of completeness.

**Physical acceptance remains incomplete.** Desktop compilation/sidecar smoke
does not drive the installed app as a user. Loopback/containers and automated
friend/transit checks do not prove two physical nodes across distinct public
networks. GPU, revocation/restart and installed deep-link evidence remain scoped
gaps, not automatically resolved by green CI.

**Service durability is incomplete.** Shared lifecycle hooks do not prove durable
video purchase recovery after reload. Real-provider delivery and cancellation
need recorded acceptance beyond fixtures.

**Open-network/economy evidence is future work.** Current suites do not establish
permissionless consensus, production Ryncoin, neutral AI fairness or legal/ethical
correctness. The new proposals need dedicated adversarial and conformance evidence
before implementation can be claimed complete. They start no monitoring or jobs.

## 6. Working rules

- A red test means a defect until proven otherwise. Decide whether the code or
  the test is now correct, and rewrite the test to the new contract rather than
  weakening, skipping, or deleting it.
- Never make a test pass by lowering an assertion. If a threshold moves, the
  moving of it is the change under review.
- Pre-existing failures are named explicitly in a report, never left to hide
  behind new ones.
- Acceptance evidence is a file in the repository, not a screenshot in a
  message, and it never contains user content.
- Every claim in a status document names the evidence behind it, so that
  "implemented" and "verified" stay distinguishable.

## First digital exchange evidence

`tests/test_exchange.py` exercises pure rules plus real Ed25519 signatures,
X25519 encryption and SQLite restarts: exact terms, supply and hold conservation,
free profiles, finite issuance, independent work witnesses, concurrent ordering,
vote locks, lost acknowledgements, replay, refund, partial decision, model
uncertainty/disagreement, private data, appeals, delegation and owner HTTP gates.
The Exchange component tests cover reviewed network joining, exact-price
agreement, verified delivery before acceptance, and resuming a persisted intent
without a replacement payment. The bounded one-time HTTP acceptance uses
controlled model adapters and stops all temporary servers before returning.
A green test does not establish real-model impartiality or public currency
readiness; the [alpha limitations](EXCHANGE_ALPHA.md) remain explicit.
