# rynnet — Findings

Real rynmesh protocol behaviors surfaced by the transparent testbed. These are
*observations of the live alpha*, not testbed defects. They are candidates for
rynmesh-side investigation (out of testbed scope; flagged for review, not
auto-patched, because credit economics are vision/consensus-relevant).

## F1 — Provider serve-credit does not accrue in the direct peer-fetch path

**Observed (basic-fetch, partition-heal):** publisher `a` published 3 items
(score 25.0, event_count 4: publish×3 + register). Nodes `b` and `c` then
fetched 3 and 6 items respectively from `a` via the real
`store.fetch_peer_content_full` path. Across the full observation window `a`'s
scoreboard stayed at **event_count 4** and **score ~25 (only time-decay:
25.0 → 24.99995)** — i.e. **zero credit for serving 9 fetches**.

**Why it matters:** `ARCHITECTURE.md` lists "preview serving" and "full content
serving" as credit sources, and the vision's distribution-weight model assumes
serving useful bytes earns credit. The alpha apparently credits only
publish/registration events, not serving. If serving is uncredited, the
"plumbing/serving earns credit" pillar (RYNMESH_VISION §4/§5.1) is not yet
realized in the fetch path.

**Status:** flagged for rynmesh-side review. Not patched here.

## F2 — Duplicate peer records from double registration inflate discovery

**Observed (basic-fetch):** fetcher `c` retrieved **6** items though only 3 were
published. Cause: the publisher registers twice — once via boot
`RYNMESH_AUTO_REGISTER=1` and again via `lan_qa.py publish-sample --register` —
producing two peer records for the same `peer_id`/endpoint, so `fetch-matrix`
lists+fetches the same content per record. `discover_peers` does not dedupe by
`peer_id`.

**Why it matters:** duplicate records double-count a peer's reach/availability
and could be a cheap amplification vector; discovery should likely dedupe by
`peer_id` (keeping the freshest signed record).

**Status:** flagged for rynmesh-side review. Not patched here.

---

The testbed treats these as expected outputs (assertions verify credit
*persists* and fetch *succeeds*, and report `grew:false` / counts as data),
so runs stay green while the findings remain visible.
