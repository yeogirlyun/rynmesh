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
| Complete Python suite | Pass | 531 passed, 3 skipped |
| Web tests | Pass | 38 passed |
| Web production build | Pass | TypeScript and Vite build completed |
| Python sdist and wheel | Pass | Isolated PEP 517 build completed; wheel installed into a clean virtual environment and its `rynmesh-transit --help` entry point loaded both installed transit modules |
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
| Atomic capacity-refresh stress | Pass | 63/63 sessions in 60.956 s while both workers refreshed every 0.1 s; independent soak audit accepted zero failures, bounded memory, clean thread shutdown, no plaintext and no partial files |
| Established data plane without control plane | Pass | Registry access was actively denied for 1.718 s after both ICE legs were nominated; a 4 MiB encrypted request completed during the blackout with matching hashes, no TURN and clean worker shutdown |

The full resource report and signed evidence are generated locally under
`.codex-tmp/peer-transit-acceptance-full/` and are intentionally not committed
because the directory contains two one-GiB test artifacts.

## Running gate

The first persistent run correctly failed closed after 3,604 seconds and 360
successful sessions because the one-hour signed capacity record expired. The
worker had advertised only at startup. Commit `4e729f8` adds a 15-minute
capacity heartbeat, atomic capacity-record replacement, and regression plus
accelerated integration coverage. The pre-fix elapsed time is invalidated.

The second run started from zero at 2026-08-31 19:18 Hong Kong time. It
completed 2,067 successful sessions over 50,861 seconds before failing at
2026-09-01 09:26. The failure happened while the transit worker was atomically
refreshing its otherwise valid capacity record: on Windows, a reader can
briefly receive a sharing `OSError` during `os.replace()`, and the registry
correctly skipped that one unreadable record. Discovery previously treated
the single empty read as permanent absence. Commit `ef64838` adds a bounded
five-attempt discovery retry (200 ms maximum) and regression coverage. The
second run is invalidated rather than combined with a later run.

The third run started from zero at 2026-09-01 09:33 Hong Kong time. It crossed
the first 15-minute refresh boundary and completed 114 sessions over 1,133
seconds with zero session failures. A subsequent acceptance-contract audit
added a real post-ICE registry blackout gate. While exercising that gate, an
immediate duplicate capacity publication exposed the corresponding Windows
write-side sharing race: `os.replace()` can itself receive a transient
`PermissionError` when a reader has the destination open. Commit `2fb3033`
adds a bounded five-attempt atomic-replace retry, a regression test, the real
control-plane blackout scenario, and fail-closed report auditing. Because this
changes runtime registry code, the third run is invalidated despite its clean
intermediate result.

The fourth 24-hour worker soak started from zero at 2026-09-01 09:52 Hong Kong
time and is scheduled to finish at approximately 2026-09-02 09:52. It writes
atomic progress to `.codex-tmp/peer-transit-soak-24h-r4/progress.json`. Its
initial checkpoint completed eight sessions with zero failures, all three
worker threads alive, and no plaintext marker exposure. Final acceptance
requires:

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
