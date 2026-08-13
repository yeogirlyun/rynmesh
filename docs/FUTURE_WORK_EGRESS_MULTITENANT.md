# Future work — multi-tenant net.egress (let any downloaded user connect)

## Problem

Today the net.egress data plane is a raw SSH tunnel from the consumer to the
Shenzhen box:

```
ssh -D 2080 ops@sz-egress-exit      # ProxyJump HK → SZ
```

It requires two things that only exist on **hand-provisioned, trusted machines**:

1. `~/.ssh/rynmesh_example_key` — a **long-lived private key** to our infra.
2. `~/.ssh/config` with the `sz-egress-exit` ProxyJump alias.

The brokered session descriptor carries only `exit_host` / `exit_user` /
`socks_port` — **no transport credential**. It assumes the consumer already
holds the key. So:

- A user who just downloads rynnode has no key → `Permission denied` at the jump.
- The single private key is shared across machines — a security smell; it must
  never be baked into a downloaded client.

This is a **trusted-operator MVP**, not a shippable multi-tenant service. Credit
metering and registry brokering already work for arbitrary users; the wall is
**transport authentication**.

## Principle

> Trust = mesh identity (Ed25519 peer keys) + credits.
> Any credential to reach the provider's transport must be **ephemeral,
> per-session, scoped, and provider-issued** — never pre-shared, never shipped
> in the client.

## Options (cheapest → cleanest)

### 1. Provider-issued ephemeral SSH key per session  ← recommended next step
On `open_session`, the SZ provider mints a throwaway keypair (or a one-shot
`authorized_keys` line: `restrict,permitopen="…",command=""`, forwarding-only),
valid for the session TTL, and returns the **private key in the session
descriptor**. The consumer's rynnode writes it to a temp file and passes
`RYNMESH_VPN_KEY` to `rynmesh-vpn`. Auto-expires; locked to port-forwarding; no
shared secret. Smallest change — fits the existing descriptor flow.

**Touch points:**
- `rynmesh/services/net_egress.py` (provider): generate + install + return key;
  reap on expiry.
- `rynmesh/services/net_egress_client.py` / `egress_control.py` (consumer):
  persist the temp key (0600), set `RYNMESH_VPN_KEY`, delete on disconnect.

### 2. SSH certificates (CA-signed, short TTL)
Provider runs an SSH CA; on grant it signs the consumer's own pubkey into a
short-lived cert with `force-command` / `source-address` / principal limits. No
`authorized_keys` churn, expiry built in. Cleaner than #1 at scale.

### 3. Mesh-native relay (long-term "right" answer)
No SSH credential crosses at all. Egress rides the **already-authenticated
rynmesh peer channel** (Ed25519 + registry-brokered, credit-metered work
orders); the provider node egresses locally. Trust flows entirely through mesh
identity — exactly what rynmesh is for. Requires an in-overlay proxy/relay.

## Roadmap
- **Now → next:** implement #1.
- **Later:** #2 if `authorized_keys` churn or revocation becomes painful.
- **Strategic:** #3 — the model that truly scales to strangers.

## Performance note (auth method vs throughput)

Authentication does **not** affect bandwidth/latency — it happens once at setup.

- **#1 / #2 are the *same dedicated SSH tunnel* as today**, just with different
  key handling → **identical throughput and latency.** A per-session key is the
  same cipher and same `-D` forward as a pre-shared key.
- **#3 (mesh relay) is a different transport.** A userspace relay over the mesh
  framing typically adds overhead vs a raw `ssh -D` forward, so expect **equal
  or somewhat slower** unless built on an efficient datapath (e.g. WireGuard).

Real bottlenecks live in the **path and the box**, not the auth:
- The current path already hops **consumer → HK jump → SZ** (not "direct to SZ").
- `ssh -D` forwards TCP streams → **TCP-over-TCP**, which can collapse
  throughput under packet loss (head-of-line blocking). A WireGuard/raw datapath
  would avoid this — a separate, larger optimization from the auth work.
- Many tenants share one SZ box's uplink → **capacity/contention** is a
  provider-side scaling problem, independent of how users authenticate.
