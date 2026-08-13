# Rynmesh `net.*` Services — Regional Egress / VPN as a Mesh Service

Status: design draft, 2026-06-02. Updated 2026-07-30: the data-plane primitive
formerly shipped as `avaryn-vpn` now ships in-tree as `rynmesh-vpn` (bundled at
`rynmesh/services/rynmesh-vpn`, console script `rynmesh-vpn`, env vars
`RYNMESH_VPN_*`) — see `DECISION_AVARYN_SEPARATION.md`. Rynmesh is fully
self-contained (MIT); Avaryn is an optional attestation/service provider, not a
required component. This spec frames the work so the
"appear in Hong Kong" VPN is **not a one-off** but a reusable Rynmesh *service*
(like `signal50_service`), with the same building blocks powering a whole family
of network services.

## 1. Motivation

We already have a working but **standalone** browser VPN (`rynmesh-vpn`: an
`ssh -D` SOCKS5 tunnel through the fixed HK gateway `203.0.113.10`). It works, but:

- it's hard-wired to one server and one credential,
- it can only do "appear in HK" — nothing else,
- nothing else in the stack benefits from it.

Rynmesh already moves signed content between nodes with discovery, NAT
traversal, identity, trust, and credit accounting. If we model **network egress
as a capability a node offers**, the VPN becomes a *service among nodes* — a HK
node (or a mainland-China node) advertises egress; your home node consumes it —
and the same infrastructure enables many other regional-network services.

## 2. What rynmesh already gives us (reuse, don't rebuild)

| Need for a P2P VPN | Rynmesh primitive (existing) |
|--------------------|------------------------------|
| Find a node in region X | Registry-assisted discovery (`registry.py`, `/api/local/peers/discover`), self-signed peer records |
| Reach a node behind NAT | Work-order **mailbox** + hash-addressed **relay** blobs (`relay.py`, `rynmesh_submit_work_order` / `rynmesh_poll_work_orders` / `rynmesh_upload_relay_artifact`) |
| Authenticated requests | Signed records + `identity.py` / `crypto.py` (`WorkOrder.signed`, `verify_*`) |
| "Offer X, priced" | Capability model: `JobCapacityRecord` advertises `capabilities=(...)` with `price_credits` (see `signal50_service.py`) |
| Pay / meter / deter abuse | Non-transferable **Rynmesh Credits** (`credits.py`) + **eigentrust** ranking (`eigentrust.py`) + slashing hooks |
| Service runner skeleton | `signal50_service.py` (`_service_once`: register capacity → poll capability → run order → return result/relay) |

**The only missing piece is a data-plane** — actually forwarding bytes — and for
much of what we want we don't even need a tunnel (see Tier 0).

## 2b. Layering contract — `rynmesh-vpn` provides the basics, rynmesh orchestrates

This is the agreed division of labor:

- **`rynmesh-vpn` = the data-plane *basics* (a reusable primitive).** It already
  knows how to bring up an SSH-SOCKS5 tunnel, launch a scoped dedicated-browser
  profile, fail closed (kill-switch), and verify the geo of the exit. Crucially
  it is **already parameterized by env** — `RYNMESH_VPN_GATEWAY`, `RYNMESH_VPN_KEY`,
  `RYNMESH_VPN_PORT`, `RYNMESH_VPN_PROFILE_DIR`, `RYNMESH_VPN_CHROME` — so the exit
  host/key/port are *not* hard-wired to HK. It ships **inside the rynmesh
  package** (`rynmesh/services/rynmesh-vpn`, console script `rynmesh-vpn`);
  `pip install rynmesh` is all that's needed.
- **rynmesh = the control plane + orchestration.** It does discovery (which node,
  which region), NAT traversal, identity/auth, per-session credentials + TTL,
  consent/ACL, accounting (credits), and trust (eigentrust). For a session it
  simply **selects an exit node and invokes the `rynmesh-vpn` primitive with the
  per-session env** (gateway = the chosen provider, key = the ephemeral session
  key, etc.). The brokering layer never re-implements the tunnel; `rynmesh-vpn`
  never decides *which* exit or *who* may use it.

So the seam is already there: rynmesh's `net.egress` provider/consumer shells out
to (or imports) the parameterized `rynmesh-vpn` basics; only the brokering around
it is new. As `rynmesh-vpn` grows (WireGuard mode, multi-hop), it stays a
mesh-agnostic primitive that the `net.egress` broker drives.

**End goal:** browse from a **mainland-China exit** by selecting a `region:CN`
provider node on the rynmesh network; rynmesh brokers the session and the
`rynmesh-vpn` primitive carries the dedicated-browser traffic out through that
node — the same UX as today's HK tunnel, but any region a mesh node offers.
(Where earlier drafts referenced an `avaryn.vpn` primitive: that role is filled
by the in-tree `rynmesh-vpn`; Avaryn remains only an optional attestation/service
provider on the mesh.)

## 3. Two tiers

### Tier 0 — `net.fetch` (works on rynmesh *today*, no new data-plane)

Insight: a lot of "I need a HK/CN IP" is really *"fetch this region-locked URL /
hit this region-locked API and give me the bytes"* — which is just a **work
order**, not a live tunnel.

- **Capability:** `net.fetch`
- **Provider** (HK / CN node) advertises `net.fetch` with a per-MB/req
  `price_credits`.
- **Consumer** submits a signed `WorkOrder{capability:"net.fetch", params:{method,
  url, headers, body_ref?, max_bytes, timeout}}`.
- Provider performs the request **from its region**, returns a
  `WorkResult{status, headers, body_ref}` where `body_ref` is a `sha256:` relay
  artifact (consumer verifies the hash). Credits transfer on success.
- **Zero new transport** — this is the existing job pipeline. Solves region-locked
  REST APIs, geo-checks, content pulls, "is this stream up from HK?" etc.

This alone is worth shipping first; it's small and reuses everything.

### Tier 1 — `net.egress` (interactive VPN; adds the data-plane)

For *live, interactive* browser traffic (streaming, logged-in sessions) you need
a real forwarding channel. Model it as a brokered, metered session.

- **Capability:** `net.egress.socks5` (v1), optionally `net.egress.wireguard` (v2).
- **Provider** advertises `net.egress.socks5` with `{region, asn, price_credits/GB,
  max_sessions, allowed_dest_policy}`.
- **Session brokering (control plane = rynmesh):**
  1. Consumer discovers providers for the desired `region` (registry), ranks by
     eigentrust + price.
  2. Consumer submits a signed `WorkOrder{capability:"net.egress.socks5",
     params:{client_pubkey, ttl, bandwidth_cap, dest_policy_ack}}`.
  3. Provider authorizes (consent + ACL + credit balance check), spins a
     **short-lived, scoped SOCKS5/CONNECT listener** bound to an encrypted tunnel,
     and returns a `WorkResult` with the session descriptor (endpoint, ephemeral
     creds, expiry).
- **Data plane (the new part):** the actual tunnel between consumer and the
  provider's SOCKS exit. v1 options, lightest first:
  1. **Brokered `ssh -D`** — reuse exactly what `rynmesh-vpn` proved (fail-closed
     kill-switch, browser-only scope), but the host/credential/lifetime are
     issued *dynamically per session* by the rynmesh broker instead of hard-coded.
  2. **Tunnel over the peer channel** — carry SOCKS frames inside an
     authenticated WebSocket/HTTP-CONNECT stream on the existing peer transport
     (no extra inbound port; works with the relay for NAT'd providers).
  3. **(v2) Userspace WireGuard** (`wireguard-go`/`boringtun`) with rynmesh as the
     Tailscale-style coordination/key-exchange plane → full machine-wide L3 VPN,
     multiple exit nodes, ACLs.
- **Consumer side:** keep the `rynmesh-vpn` UX — a dedicated browser profile
  pointed at the local end of the brokered tunnel, with the same fail-closed
  kill-switch so a dropped session never leaks the real IP.
- **Metering:** provider counts bytes; credits debit per GB; eigentrust + slashing
  punish bad exits (leaking, MITM, downtime) and bad consumers (abuse).

## 4. Concrete two-node deployment (the immediate goal)

```
 Home node (consumer)                         Provider node (HK and/or CN)
 ┌───────────────────────┐                    ┌─────────────────────────────┐
 │ rynmesh-peer (M5/MS-1) │   signed work      │ rynmesh-peer on HK gateway   │
 │  + dedicated browser   │   order (egress)   │  (203.0.113.10) or a CN box    │
 │  profile → local SOCKS │ ─────────────────► │  advertises net.egress +     │
 │  end of brokered tunnel│ ◄───────────────── │  net.fetch; runs SOCKS exit  │
 └───────────────────────┘   session + bytes  └─────────────────────────────┘
        registry (relay.rynmesh.ai or private) brokers discovery + NAT + mailbox
```

- **HK provider node:** install the stock `rynmesh-peer` on (or beside) the HK
  gateway; it advertises `net.egress.socks5{region:HK, asn:AS9269}` — i.e. the
  same residential HKBN exit `rynmesh-vpn` uses, now offered as a metered service.
- **China provider node:** a `rynmesh-peer` at a mainland contact's home dials
  **out** to the registry/relay as rendezvous (China home is CGNAT — relay/mailbox
  is exactly what rynmesh's NAT-safe path is for), advertising
  `net.egress{region:CN}`. This is the clean, non-one-off version of the
  "mainland exit" options in `hk-server/docs/09-vpn-mainland-exit.md`.
- **Home consumer node:** already runs `rynmesh-peer`; gains a `net.egress`
  client that requests a session and wires the dedicated browser profile.

## 5. Other services the same infrastructure unlocks

Once "a node offers regional network access as a metered, signed, trust-ranked
service" exists, you get a family for ~free:

- **Geo-probe / region monitoring** — "from HK and CN, is `iqiyi.com` reachable /
  what does it return?" (Tier 0 fan-out across regions).
- **Region-locked API access** — programmatic calls that must originate in a
  region (payments, maps, telco APIs) via `net.fetch`.
- **Distributed crawling / data collection** from multiple regional vantage points.
- **Censorship-resilient relay** — chain `net.egress` hops; rynmesh relay already
  hash-addresses artifacts.
- **Latency/availability SLA checks** for the webops domains from outside.
- **Bandwidth marketplace** — credits already model "I provide, you consume,"
  making egress a first-class tradeable capacity alongside compute/media.

## 6. Security, consent, and legal (must-haves)

- **Explicit provider consent + ACL** — a node is *opt-in* as an exit; a
  `dest_policy` (allow/deny lists, no-SMTP, rate caps) bounds what it will relay.
  Operating an exit carries liability — make it deliberate, logged, and revocable.
- **Fail-closed** on the consumer (port the `rynmesh-vpn` kill-switch).
- **Per-session ephemeral creds**, short TTL, scoped to one consumer pubkey.
- **No new public ports** on sensitive boxes (the HK gateway already runs the
  reverse proxy + PostgreSQL — prefer the relay/mailbox + outbound dial, exactly
  why `rynmesh-vpn` avoided WireGuard there).
- **Abuse handling** via credits + eigentrust slashing; rate/byte caps per session.

## 7. Phasing

1. **Tier 0 `net.fetch`** — new capability handler in the `services/` pattern +
   relay return. Smallest, reuses the whole job pipeline. Validate HK↔home.
2. **`net.egress.socks5` brokering (control plane only)** — issue/expire scoped
   `ssh -D` sessions per signed work order; reuse `rynmesh-vpn` for the actual
   tunnel + kill-switch. Validate HK provider ↔ home consumer.
3. **Tunnel-over-peer-channel** — remove the `ssh` dependency; SOCKS over the
   authenticated peer transport + relay (true P2P, NAT-safe).
4. **CN provider node** via relay rendezvous; multi-region selection.
5. **(later) `net.egress.wireguard`** for machine-wide L3 + exit-node ACLs.
6. **Credits/eigentrust integration** for metering + abuse control throughout.

## 8. Open questions

- Tunnel framing for step 3 — WebSocket vs HTTP CONNECT over the existing peer
  app; MTU/throughput vs the relay's hash-addressed model (relay is request/
  response, not a stream — a live tunnel likely needs a direct or relay-brokered
  *stream*, a new transport mode worth prototyping in `rynnet`).
- Credit pricing unit for egress (per-GB vs per-session vs per-hour).
- Whether the HK exit should be the gateway itself or a sidecar node (keep the
  sensitive gateway minimal — lean sidecar).
- Reuse of `rynnet` (the tc-netem/iptables testbed) to validate egress under real
  NAT/loss before deploying the CN node.
