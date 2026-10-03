# Service platform: shared infrastructure for providers and seekers

Status: reviewed 2026-10-03 against main `fa85833`. Delivered foundations are
separated from proposed extensions. The owner requests a general-purpose value
network, with internal Ryncoin earning/spending first and fiat exchange much
later. See [Product milestones](PRODUCT_MILESTONES.md) and the detailed
[network economy roadmap](NETWORK_ECONOMY_ROADMAP.md).

## Delivered foundations

| Layer | Implemented | Remaining boundary |
|---|---|---|
| Node lifecycle | Supervised background-worker registry (#33) | New resource-consuming contribution jobs require owner opt-in and limits |
| Transport | Service peer POSTs use Transport/HttpPeerClient (#32); encrypted tasks/mailbox and direct peer transit | Distinct-machine/public-egress acceptance and field recovery |
| Development accounting | Task Balance view over FileCreditLedger development events (#29) | Simulated units; no decentralized Ryncoin spend finality |
| Native AI | Bundled/downloaded pinned llama.cpp runtime (#34), managed setup and provider permissions | Broader GPU, installed-desktop, restart and cross-network evidence |
| Private AI experience | Unified Ask, model selection, reviewed content context, direct streaming/cancel/archive (#60) | Whole-response fallback on other paths; real provider/network coverage |
| Service UI | Descriptors and shared provider/order hooks (#66) | Generic agreements, durable consumer-order lookup, paid fulfillment |
| Friend Mesh | Reviewed invite/QR, mailbox (#35), friend For You (#62), diagnostics (#64), offline Ask (#78), recap (#79), shared lists (#80) | Installed deep links (#72), physical cross-network evidence and egress credential isolation |

The shared UI hooks already provide bounded retries, nonoverlapping reads,
identity binding, terminal-state stopping and explicit writes. Discovery/order
snapshots remain memory-only. Video submission uncertainty is visible, but a
reload still loses transient recovery state: do not describe this as a durable
purchase ledger.

## Recommended extensions

1. **Durable work first (INF-01).** Persist original order IDs and exact terms;
   reconcile uncertain outcomes instead of resubmitting. Exercise reload,
   restart, cancellation and interruption at every settlement boundary.
2. **Participant identity (INF-02).** Separate an owner/organization from its
   nodes, devices, service packages and wallets. Specify delegation, revocation,
   rotation and recovery before balances become transferable.
3. **Offers and requests (INF-03).** Generalize provider advertisements and
   seeker requests through extensible schemas, selective public projections,
   negotiation and matching. Private bodies stay out of registries.
4. **Ryncoin design and pilot (ECO-01/02).** Introduce reviewed integer asset
   accounting, decentralized finality, escrow/refunds and reconciliation.
   Preserve reputation and simulated Task Balance as separate concepts.
5. **Agreements and fulfillment (INF-04).** Bind revisions, price, scope,
   milestones, rights, deadlines and dispute rules. Typed adapters support human,
   digital, physical and compute delivery without inventing separate wallets.
6. **Protocol infrastructure rewards (ECO-03).** Issue Ryncoin for verified
   basic system work such as registry/discovery, serving and availability under
   public issuance budgets. Other jobs can be participant-funded. Signed
   activity alone is neither useful-work proof nor permission to self-mint.
7. **Covenants and extension contracts (INF-05/06).** Add neutral AI dispute
   resolution, corresponding Ryncoin fees and appeals; SDK/conformance, licenses,
   paid access and media delivery. Safely handle unknown schemas; sandboxing is
   required before local execution of service code.

These enable arbitrary lawful, ethical activities. Joining and participation
are free; no platform charges a commission or controls the category list.
Providers and seekers agree prices through demand and supply. Dispute fees pay
neutral resolution providers under the published covenant, rather than a platform. Commerce, recruiting,
consulting and AI entertainment are examples of adapters, not privileged
hardcoded service classes. The same participant can provide and seek value.

## Ownership and readiness

System changes cover identity, transport, durability, settlement, discovery,
policy and conformance. User-facing adapters cover presenting offers/requests,
negotiating terms, delivery and reviewing receipts. Both use the local node as
the enforcement point; browsers and agents do not bypass it.

[GitHub Issues](https://github.com/yeogirlyun/rynmesh/issues) and
[CONTRIBUTING.md](../CONTRIBUTING.md) control accepted work. New proposal IDs
are review suggestions, not issue reservations. Cryptography, wallet authority,
issuance and settlement require approved designs before implementation.

## Stage boundaries

- Current reputation is nontransferable; development settlement is simulated.
- Internal participant-to-participant Ryncoin transfers and a complete written
  digital-service journey are implemented in the configured alpha.
  [Exchange alpha](EXCHANGE_ALPHA.md) records the limits; existing video/Private
  AI service accounting is not automatically converted or migrated.
- Paid launch needs finality, escrow, disputes, funding, abuse controls and
  operating review; unrestricted public use additionally requires P4 hardening.
- Fiat conversion and exchange-rate markets belong to P7, much later.
- Issue #87 records the owner-authorized first digital-work implementation.
  This roadmap independently starts no jobs, monitoring or automation.
