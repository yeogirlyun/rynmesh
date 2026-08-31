# P2P peer-transit acceptance results

Branch: `codex/p2p-peer-transit`

Protocol: `rynmesh.peer-transit.v1`

This file records measured evidence, not planned results. The hermetic gates
run three independent node identities and real ICE/UDP sockets on one host.
They prove protocol behavior but do not replace the physical three-public-
network release gate in `P2P_PEER_TRANSIT.md`.

## Completed gates

| Gate | Result | Evidence |
| --- | --- | --- |
| Complete Python suite | Pass | 525 passed, 3 skipped |
| Web tests | Pass | 38 passed |
| Web production build | Pass | TypeScript and Vite build completed |
| Healthy direct file path | Pass | Source/target SHA-256 equal; transit byte counter unchanged |
| Direct failure fallback | Pass | Real direct operation rejected; peer-transit delivery completed in 0.532 s |
| Two non-TURN ICE legs | Pass | Both nominated candidate pairs were host/UDP; `relay_used=false` |
| One GiB streamed transfer | Pass | 1,073,741,824 bytes; source/target SHA-256 equal |
| Bounded memory | Pass | Peak traced Python memory 5,504,736 bytes during the one-GiB run |
| Concurrent callers | Pass | 20/20 completed in 5.875 s without deadlock |
| Session establishment | Pass | 0.109 s, below the five-second gate |
| Encryption framing overhead | Pass | 0.0824%, below the 15% gate |
| Confidentiality | Pass | Plaintext marker absent from transit frames and registry files |
| Signed evidence audit | Pass | Independent fail-closed auditor accepted the one-GiB evidence |
| Full-report audit | Pass | Independent auditor enforces direct, fallback, route, resource and concurrency gates |
| Transit unavailable | Pass | Bounded explicit failure, no committed partial target file |

The full resource report and signed evidence are generated locally under
`.codex-tmp/peer-transit-acceptance-full/` and are intentionally not committed
because the directory contains two one-GiB test artifacts.

## Running gate

The persistent 24-hour worker soak started at 2026-08-31 18:10 Hong Kong time
and is scheduled to finish at 2026-09-01 18:10. It writes atomic progress to
`.codex-tmp/peer-transit-soak-24h/progress.json`. Final acceptance requires:

- the full 86,400-second duration;
- zero failed sessions and no plaintext marker exposure;
- no `.part` files after worker shutdown;
- both worker threads stopped;
- traced Python memory growth no greater than 32 MiB after warm-up.

This document must be updated with the final session count and memory result
after the soak reports `pass`.

## External release gate

Public NAT traversal remains unclaimed until the runbook is executed with
peer 1, peer 2 and peer 3 behind three distinct public egress networks. That
run must nominate server-reflexive or peer-reflexive UDP candidates on both
legs, block direct peer-1/peer-3 traffic, and capture packet-level evidence
that payload volume crosses only peer 1 to peer 2 and peer 2 to peer 3.
