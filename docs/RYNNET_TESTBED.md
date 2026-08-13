# rynnet — Transparent Virtual Network Testbed

A close-to-real virtual network for spawning many **unmodified** Ryn nodes and
observing real protocol behavior: discovery, fetch, relay, credit accrual, and
distribution-weight adjustment under realistic network conditions.

Status: design + build (workstream parallel to M1; supports the vision's M4
scale/safety hardening and validates the M1 node lifecycle).

## Core requirement — transparency

**A node must not be able to tell it is in the testbed.** This is the hard
constraint and it dictates the whole design:

- Each node is the **stock `rynmesh-peer`** installed from the package
  (`pip install rynmesh`), configured *only* through `RYNMESH_*` env. No code
  injection, no monkey-patched transport, no sim-aware build. This explicitly
  rejects the "mock `urlopen` + sleep" approach — that instruments the node and
  changes its code path.
- Network conditions (latency, jitter, loss, bandwidth, partition, NAT) are
  imposed **outside the process**, at the OS network layer (`tc netem`,
  `iptables`) inside each container's own network namespace. The node sees a
  normal interface with (degraded) characteristics; it uses real sockets, real
  DNS, real HTTP.
- Observation is done the way a real operator or peer would: poll the node's
  **normal public peer HTTP API** (`/health`, `/api/v1/node`,
  `/api/v1/content`, `/api/v1/credits`) and read its on-disk ledger from a
  mounted volume. Observing does not perturb the node.

Result: identical node binary and behavior in testbed and production — only the
(virtual) network around it differs, exactly as a real degraded network would.

## Substrate

macOS host → OS-level shaping needs Linux → **Colima** (headless Linux VM,
docker CLI). Each node = a container with its own real IP on a user-defined
docker network. `tc`/`iptables` run inside each container (requires
`--cap-add=NET_ADMIN`). Colima's Linux VM provides `sch_netem`.

Scale target: **10–50 real nodes** (full fidelity for protocol/interaction/
weight behavior). The abstract `sim/` model remains the tool for million-scale
economic/Sybil questions — clear division of labor, not a replacement.

## Topology

```
            ┌─────────────── rynnet docker network (real IPs) ───────────────┐
            │                                                                │
  in-testbed registry (default)        node-a ── node-b ── node-c ...         │
   or  --real-registry → registry.rynmesh.ai (dedicated network_id, TTL)      │
            │                                                                │
            │   NAT segment:  [ nat-gw (MASQUERADE) ] ── nat-node-1 ...       │
            └─── NAT'd nodes cannot accept inbound → exercise the real        │
                 relay path (rynmesh's current NAT story; full traversal      │
                 is an explicit ARCHITECTURE non-goal)                        │
```

## Registry modes

- **in-testbed (default)** — a `rynmesh-registry` container. Reproducible,
  isolated, safe. The whole run is hermetic.
- **`--real-registry`** — nodes point `RYNMESH_REGISTRY_URL` at
  `https://registry.rynmesh.ai` under a dedicated `RYNMESH_NETWORK_ID`
  (`rynnet-test-<run-id>`), short record TTL, and **teardown deregistration**.
  Honest caveat: this writes self-signed peer records advertising
  virtual/unreachable endpoints into the production discovery plane; real peers
  on other network_ids are unaffected, but the test network_id must be unique
  per run and cleaned up. Opt-in only.

## Scenario spec (YAML)

A scenario declares the topology, faults, steps, observation, and assertions:

```yaml
name: basic-fetch
registry: in-testbed          # or: real
relay: true
nodes:
  - id: a
    netem: { delay: 50ms, jitter: 10ms, loss: "1%", rate: 10mbit }
  - id: b
  - id: nat1
    nat: true                 # behind nat-gw → relay path
steps:
  - publish:  { node: a, count: 3 }
  - discover: { node: b }
  - fetch:    { from: b, min: 3, timeout_s: 20 }
faults:
  - at_s: 20, partition: [b], duration_s: 10
observe:
  poll_interval_s: 2
  duration_s: 60
  capture: [credits, content, node, health]
assert:
  - fetch_success:                { node: b, min: 3 }
  - distribution_weight_increases:{ node: a }
  - partition_heals:              { node: b }
```

## Components

| Path | Role |
|---|---|
| `rynnet/Dockerfile` | stock `rynmesh` image (peer/registry via env) + `iproute2`/`iptables` |
| `rynnet/entrypoint.sh` | applies declared `tc`/`iptables` to `eth0`, then `exec`s the unmodified node (transparent) |
| `rynnet/orchestrator.py` | scenario → docker network/containers/registry/NAT; live faults; teardown |
| `rynnet/observe.py` | polls peer HTTP + ledger → time-series artifacts |
| `rynnet/scenarios/*.yaml` | declarative scenarios |
| `rynnet/runs/<id>/` | captured time-series, logs, assertion report (gitignored) |

## Non-goals (this testbed)

- Not a replacement for `sim/` (million-scale economics stays abstract).
- Not full NAT traversal (rynmesh uses relay; testbed exercises the relay path).
- Not multi-tier registry routing yet (single registry tier; tiering is a
  vision M4 item).
- Not a perf/throughput benchmark — it validates *protocol behavior and weight
  dynamics under realistic conditions*, not raw MB/s.
