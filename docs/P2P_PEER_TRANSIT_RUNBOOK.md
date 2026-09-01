# P2P peer-transit runbook

This runbook operates `rynmesh.peer-transit.v1`: direct P2P first, then an
ordinary Rynmesh peer as a single encrypted transit hop.  TURN is not used.
See `P2P_PEER_TRANSIT.md` for the protocol and release gates.

## Prerequisites

- all three nodes run the same Rynmesh revision;
- all use the same `RYNMESH_REGISTRY_URL` and `RYNMESH_NETWORK_ID`;
- outbound UDP is permitted;
- `RYNMESH_P2P_STUN` names a STUN server reachable from each network;
- no TURN URL or credential is configured;
- clocks are synchronized because session opens expire and are signed.

Install or refresh the environment after pulling:

```powershell
python -m pip install -e ".[dev]"
```

## Start the target (peer 3)

```powershell
$env:RYNMESH_HOME = "D:\rynmesh\peer-3"
$env:RYNMESH_NETWORK_ID = "three-node-production"
$env:RYNMESH_REGISTRY_URL = "https://registry.example.net"
$env:RYNMESH_P2P_STUN = "stun.example.net:3478"
$env:RYNMESH_P2P_REQUIRE_PUBLIC = "1"
$env:RYNMESH_P2P_REQUIRE_DISTINCT_PUBLIC = "1"
rynmesh-transit worker --role target --network-id three-node-production `
  --inbox D:\rynmesh\peer-3\transit-inbox --timeout 180
```

The startup line prints peer 3's Ed25519 peer ID.  Its signed capacity contains
the X25519 messaging public key; the private key remains beneath
`RYNMESH_HOME`.

The worker refreshes this signed capacity every 15 minutes; long-running nodes
do not require manual re-registration.

## Start the transit peer (peer 2)

```powershell
$env:RYNMESH_HOME = "D:\rynmesh\peer-2"
$env:RYNMESH_NETWORK_ID = "three-node-production"
$env:RYNMESH_REGISTRY_URL = "https://registry.example.net"
$env:RYNMESH_P2P_STUN = "stun.example.net:3478"
$env:RYNMESH_P2P_REQUIRE_PUBLIC = "1"
$env:RYNMESH_P2P_REQUIRE_DISTINCT_PUBLIC = "1"
rynmesh-transit worker --role transit --network-id three-node-production --timeout 180
```

Peer 2 needs no target private key and does not store transferred files.  It
creates one ICE connection to peer 1 and another to peer 3, then forwards
bounded ciphertext frames.

## Send from peer 1

Configure the source independently on peer 1; do not assume environment values
from either worker host:

```powershell
$env:RYNMESH_HOME = "D:\rynmesh\peer-1"
$env:RYNMESH_NETWORK_ID = "three-node-production"
$env:RYNMESH_REGISTRY_URL = "https://registry.example.net"
$env:RYNMESH_P2P_STUN = "stun.example.net:3478"
$env:RYNMESH_P2P_REQUIRE_PUBLIC = "1"
$env:RYNMESH_P2P_REQUIRE_DISTINCT_PUBLIC = "1"
```

Direct only:

```powershell
rynmesh-transit send-file-direct .\artifact.bin `
  --target-peer "<peer-3-id>" --network-id three-node-production `
  --timeout 180 --evidence .\direct-evidence.json
```

Force peer 2 for a diagnosed bad route:

```powershell
rynmesh-transit send-file .\artifact.bin `
  --relay-peer "<peer-2-id>" --target-peer "<peer-3-id>" `
  --network-id three-node-production --timeout 180 `
  --evidence .\transit-evidence.json
```

Direct first with automatic hard-failure fallback:

```powershell
rynmesh-transit send-file-adaptive .\artifact.bin `
  --relay-peer "<peer-2-id>" --target-peer "<peer-3-id>" `
  --network-id three-node-production --timeout 180 --direct-timeout 8 `
  --evidence .\adaptive-evidence.json
```

The Python API additionally accepts rolling direct/transit `PathMetrics`, so a
caller can switch proactively after the loss/latency hold period rather than
waiting for a hard direct failure.

Route thresholds are operator-tunable without changing code:

| Variable | Default | Meaning |
| --- | ---: | --- |
| `RYNMESH_TRANSIT_HARD_FAILURE_COUNT` | `3` | Consecutive failures before immediate transit |
| `RYNMESH_TRANSIT_LOSS_THRESHOLD` | `0.08` | Direct-path loss ratio that is degraded |
| `RYNMESH_TRANSIT_LATENCY_THRESHOLD_MS` | `250` | Direct P95 latency ceiling |
| `RYNMESH_TRANSIT_IMPROVEMENT_RATIO` | `0.25` | Required transit score improvement |
| `RYNMESH_TRANSIT_DEGRADED_HOLD_S` | `30` | Poor-path hold before switching |
| `RYNMESH_TRANSIT_MIN_HOLD_S` | `60` | Minimum time kept on transit |
| `RYNMESH_TRANSIT_RECOVERY_HOLD_S` | `120` | Healthy-direct hold before recovery |
| `RYNMESH_TRANSIT_RECOVERY_PROBES` | `5` | Healthy probes required for recovery |

The CLI performs bounded hard-failure fallback. Proactive poor-quality routing
uses rolling `PathMetrics` supplied by the caller or node telemetry loop; the
route manager applies the thresholds and hysteresis above.

## Hermetic acceptance

The deterministic test uses real local ICE sockets, disables external STUN,
creates three independent node identities, transfers ciphertext over two ICE
legs, scans transit frames and registry files for a unique marker, verifies
signed work results, exercises route recovery and checks concurrent callers.
The target worker denies the direct operation during the hard-failure case so
the real adaptive client must fail direct and complete through peer 2 within
10 seconds; this is not inferred only from the route state machine.

Smoke gate:

```powershell
python scripts/run_peer_transit_acceptance.py `
  --size-mib 8 --concurrent 3 --timeout 120 `
  --output .codex-tmp\peer-transit-report.json `
  --evidence .codex-tmp\peer-transit-evidence.json
python scripts/audit_peer_transit.py .codex-tmp\peer-transit-evidence.json
python scripts/audit_peer_transit.py --report --min-concurrent 3 `
  .codex-tmp\peer-transit-report.json
```

Full resource gate:

```powershell
python scripts/run_peer_transit_acceptance.py `
  --size-mib 1024 --concurrent 20 --timeout 1800 `
  --output .codex-tmp\peer-transit-full-report.json `
  --evidence .codex-tmp\peer-transit-full-evidence.json
python scripts/audit_peer_transit.py --report --require-one-gib `
  --min-concurrent 20 .codex-tmp\peer-transit-full-report.json
```

Run the 24-hour persistent-worker soak separately. It keeps the same three node
identities alive, repeatedly opens encrypted sessions, deletes delivered test
payloads, and records memory growth, thread cleanup, partial files and live
progress in JSON:

```powershell
python scripts/run_peer_transit_soak.py `
  --duration-hours 24 --interval-seconds 10 --payload-kib 64 `
  --work-root .codex-tmp\peer-transit-soak `
  --progress .codex-tmp\peer-transit-soak\progress.json
python scripts/audit_peer_transit.py --soak-report `
  --require-duration-seconds 86400 --min-sessions 3 `
  .codex-tmp\peer-transit-soak\progress.json
```

## Physical three-network acceptance

Run peer 1, peer 2 and peer 3 behind three distinct public egress networks with
both strict public variables enabled.  Capture UDP on each host and the
registry:

1. establish a healthy direct peer-1-to-peer-3 session;
2. block only the peer-1/peer-3 address pair;
3. send adaptively through peer 2;
4. confirm both nominated hops use `srflx`/`prflx`, never `relay`;
5. after ICE nomination, block registry and STUN access and continue the file;
6. verify peer 2 ingress/egress counters cover the payload;
7. scan peer 2 packet capture, logs and storage for the unique plaintext marker;
8. restore peer 1/peer 3 and verify hysteresis returns the route to direct.

The hermetic report proves the implementation.  Only this physical run may be
used to claim public-NAT traversal across three real networks.

## Failure interpretation

- `server-reflexive STUN candidate` missing: outbound UDP/STUN is blocked.
- `distinct public egress`: two nodes appear behind the same public address;
  move them to separate networks for strict acceptance.
- `timed out waiting for transit result`: the selected peer worker is offline
  or did not advertise the required role.
- `TURN/relay candidate`: fail closed; remove the TURN configuration.
- `host must be an IP literal` or `not a usable unicast address`: the signed
  remote ICE signal is malformed or attempts a forbidden network destination.
- hash mismatch: target deletes the `.part` file and the release gate fails.
