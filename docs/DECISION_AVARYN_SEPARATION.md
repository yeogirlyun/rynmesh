# Decision: Rynmesh is fully open source; Avaryn is an optional add-on service

Date: 2026-07-30 · Status: adopted · Ships in: 0.5.0

## Decision

Rynmesh stands alone as a completely MIT-licensed, self-contained project with **zero
proprietary dependencies**. Avaryn is repositioned from "the trust root Rynmesh is powered by"
to **an optional attestation/service provider that works well with Rynmesh** — one issuer among
potentially many, competing on the quality of its attestations rather than on being mandatory.

## Rationale

1. **The hard dependency was an illusory moat.** Rynmesh is MIT; anyone could fork it and stub
   out a required `avaryn` package. The "Avaryn receipt" was in practice self-minted locally by
   the publishing node (no external issuer, no Avaryn key, no verification), so a stub would
   have been functionally identical. Requiring it bought friction and optics costs, not security.
2. **A closed binary on a user-controlled machine cannot be a trust root.** Client-side secrecy
   is obfuscation, not security. Trust in the mesh comes from peer-signed manifests, provenance
   chains, identity tiers, and credit/EigenTrust — all open and verifiable.
3. **Clean open-source positioning.** "Fully open, no strings" is the story that earns
   contributors and users for a decentralization project.
4. **Better business position for Avaryn.** As a premium attestation/service provider (via the
   services primitive and the manifest `attestations` field), Avaryn earns trust weight the same
   way every participant does — and keeps its engine closed without contaminating Rynmesh.

## What changed in 0.5.0 (wire-format break)

Pre-0.5 manifests/provenance chains are **not** accepted by 0.5 nodes (private pre-launch mesh;
re-publish test content after upgrading all nodes together).

- `AvarynRunReceipt` → `RunReceipt`; manifest key `avaryn_receipt` → `run_receipt`. The receipt
  is explicitly a *publisher-signed* record, not a third-party proof.
- Provenance: genesis field `avaryn_run_id` → `run_id`; link type `purr_envelope` →
  `work_envelope` with field `purr_id` → `work_id`; `PROVENANCE_VERSION` → v0.2.
- Validation errors `missing_avaryn_*` → `missing_run_id` / `missing_work_id` /
  `missing_envelope_hash`.
- Default safety scanner id `avaryn.keyword` → `rynmesh.keyword`.
- **New manifest field `attestations`** (optional, default empty): a list of `SignedPayload`
  dicts from third-party issuers. Receivers choose which issuer keys to trust and how to weight
  them in ranking. This is the seam where Avaryn (or any vendor) plugs in.
- **VPN data plane ported in-tree.** The `avaryn-vpn` tunnel script is now bundled as
  `rynmesh-vpn` (`rynmesh/services/rynmesh-vpn`, console script `rynmesh-vpn`), with env vars
  renamed `AVARYN_VPN_*` → `RYNMESH_VPN_*` and personal defaults removed
  (`RYNMESH_VPN_GATEWAY` is now required for the ssh transport; `RYNMESH_VPN_KEY` optional).
  `pip install rynmesh` is fully sufficient for VPN egress; no avaryn install step.
- Setup scripts no longer install avaryn. The Signal50/Veo worker keeps its *optional*
  `avaryn.lens` integration (env-gated; absent avaryn simply disables that worker) — this is
  the model for all future Avaryn integrations: additive, never required.
- Legacy egress Chrome profile `~/.avaryn-sz-chrome` is still used if present (preserves
  logins); fresh installs use `~/.rynmesh-sz-chrome`.

## Follow-ups

- Sweep `docs/RYNMESH_VISION.md`, `docs/ARCHITECTURE.md`, `docs/REQUIREMENTS.md` (esp.
  NFR-2.2, OQ-9/OQ-10/OQ-11) to reflect this decision; they still describe Avaryn as the trust
  spine.
- When the services primitive lands (P3), define the attestation-issuer registration flow so
  attesters advertise pubkeys + attestation kinds like any other service provider.
