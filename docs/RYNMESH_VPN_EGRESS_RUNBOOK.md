# Rynmesh VPN Egress — Two-Node Runbook (HK exit ↔ here)

This is the hands-on procedure to stand up the `net.egress` MVP: a **provider**
node on/near the HK gateway and a **consumer** node on your machine, then browse
through the HK exit. Design + layering: see
[`RYNMESH_NET_SERVICES_DESIGN.md`](RYNMESH_NET_SERVICES_DESIGN.md). The data-plane
is the bundled `rynmesh-vpn` primitive; rynmesh does the brokering.

> **MVP scope (deliberately simple):** discovery picks the other node (or pass
> `--provider`); the consumer authenticates to the exit with its **existing
> shared key** (`~/.ssh/rynmesh_example_key`); credits are a local JSONL line.
> Per-session ephemeral SSH certs, signed credit events, and tunnel-over-peer
> transport are future phases.

## 0. Prerequisites (both nodes)
- `pip install rynmesh` (this repo). The VPN data plane ships with the package:
  the `rynmesh-vpn` console script (bundled at `rynmesh/services/rynmesh-vpn`).
  No separate avaryn install is needed.
- Consumer has `~/.ssh/rynmesh_example_key` and the HK exit authorizes it (it does
  today — that's what `rynmesh-vpn` already uses).
- A reachable **registry** both nodes share (e.g. a private `rynmesh-registry`,
  or `relay.rynmesh.ai`). Set `RYNMESH_REGISTRY_URL` on both.

## 1. Provider node (HK exit)

Run a `rynmesh-peer` on the HK gateway (or a sidecar box on the HK LAN — keep the
sensitive gateway minimal). Then run the egress worker pointing at the exit:

```bash
export RYNMESH_HOME="$HOME/.rynmesh/hk"
export RYNMESH_NODE_NAME="hk-egress"
export RYNMESH_NETWORK_ID="rynmesh-main"
export RYNMESH_REGISTRY_URL="https://relay.rynmesh.ai"     # or your private registry
rynmesh-peer &                                             # registers this node

# egress provider config (the exit whose IP you appear from):
export RYNMESH_EGRESS_EXIT_HOST="203.0.113.10"   # HK gateway (residential HKBN / AS9269)
export RYNMESH_EGRESS_EXIT_USER="rynmesh"
export RYNMESH_EGRESS_REGION="HK"
export RYNMESH_EGRESS_SOCKS_PORT="1080"
export RYNMESH_EGRESS_PRICE_CREDITS="1.0"
rynmesh-egress-worker        # advertises net.egress, serves open_session orders
```

Note this node's `peer_id` (printed by `rynmesh-peer` / its `/api/local/node/status`).

## 2. Consumer node (here)

```bash
export RYNMESH_HOME="$HOME/.rynmesh/home"
export RYNMESH_NODE_NAME="home"
export RYNMESH_NETWORK_ID="rynmesh-main"
export RYNMESH_REGISTRY_URL="https://relay.rynmesh.ai"
rynmesh-peer &

# Open an HK egress session and launch the dedicated HK Chrome through it:
rynmesh-egress --region HK --provider <hk_peer_id> --mode chrome --url https://iq.com
```

What happens: the consumer submits an `open_session` work order → the HK worker
returns a session descriptor → the client sets
`RYNMESH_VPN_GATEWAY=rynmesh@203.0.113.10`, `RYNMESH_VPN_PORT=1080` and runs
`rynmesh-vpn chrome <url>`. Your normal browser stays on your local ISP; only the
dedicated `~/.rynmesh-vpn-chrome` profile exits via HK (fail-closed kill-switch
from `rynmesh-vpn`).

Dry-run first to see the plan without tunneling:

```bash
rynmesh-egress --region HK --provider <hk_peer_id> --dry-run
```

## 3. Verify the exit

In the HK Chrome profile (or via `rynmesh-vpn verify`): Cloudflare trace should
show `loc=HK`, Bilibili region `852`, and HK/intl streaming (iQiyi Intl, WeTV,
Viu HK, Netflix HK, Disney+ HK) should unlock.

## 4. Toward a China exit (the end goal)

Stand up a third `rynmesh-peer` at a mainland contact's home (CGNAT → it dials
*out* to the registry/relay as rendezvous — exactly rynmesh's NAT-safe path),
running `rynmesh-egress-worker` with `RYNMESH_EGRESS_REGION=CN` and that box's
own exit config. Then from here:

```bash
rynmesh-egress --region CN --provider <cn_peer_id> --mode chrome --url https://tv.cctv.com/live/cctv5
```

Same UX; the region is just whichever mesh node offers it. (Mainland self-host
options: see `hk-server/docs/09-vpn-mainland-exit.md`.)

## Troubleshooting
- `rynmesh-vpn not found` → reinstall the `rynmesh` package (it ships the
  `rynmesh-vpn` console script; the bundled script lives at
  `rynmesh/services/rynmesh-vpn`).
- `no provider peer found` → pass `--provider <peer_id>` explicitly, or confirm
  both nodes share `RYNMESH_REGISTRY_URL` and the worker is running.
- `timed out waiting for egress session` → the provider worker isn't polling or
  isn't on the same `--network-id`; check `rynmesh-egress-worker` logs.
- Session opens but the browser can't reach the net → the exit didn't authorize
  your key, or SOCKS port `1080` is busy locally (`lsof -iTCP:1080`).
- Credit/session record: provider appends to `$RYNMESH_HOME/egress_sessions.jsonl`.
