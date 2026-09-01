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
| Complete Python suite | Pass | 555 passed, 3 skipped on the current candidate after the monotonic-clock, evidence-auditor and clean acceptance-root regressions |
| Web tests | Pass | 38 passed |
| Web production build | Pass | TypeScript and Vite build completed |
| Python sdist and wheel | Pass | The current r7 candidate built a 344,141-byte sdist and 273,757-byte wheel in an isolated PEP 517 environment; the wheel installed with dependencies into a new virtual environment, both transit modules imported as version 0.6.2 with protocol `rynmesh.peer-transit.v1`, and `rynmesh-transit --help` exposed worker, transit, direct and adaptive commands |
| Healthy direct file path | Pass | Source/target SHA-256 equal; transit byte counter unchanged |
| Direct failure fallback | Pass | Real direct operation rejected; peer-transit delivery completed in 0.532 s |
| Adaptive degradation and recovery | Pass | Independent audit enforced 330 ms/75 ms jitter/18% direct impairment versus 80 ms/1% transit metrics, a 30-second switch, 61-second minimum transit hold, 120-second recovery hold, five recovery probes, an exact no-flap transition sequence, and unchanged transit counters on the post-recovery direct file |
| Two non-TURN ICE legs | Pass | Both nominated candidate pairs were host/UDP and `relay_used=false`; a constructor regression proves that even injected TURN URL/username/password environment values are ignored and no TURN argument reaches `aioice.Connection` |
| One GiB streamed transfer | Pass | 1,073,741,824 bytes; source/target SHA-256 equal |
| Bounded memory | Pass | Peak traced Python memory 5,504,736 bytes during the one-GiB run |
| Concurrent callers | Pass | The latest timeline preflight completed 20/20 in 5.937 s with 20 unique signed sessions and an independently recomputed peak overlap of 20; the earlier full-resource run completed 20/20 in 5.875 s, and the final one-GiB run will repeat this combined gate |
| Session establishment | Pass | 0.109 s, below the five-second gate |
| Encryption framing overhead | Pass | 0.0824%, below the 15% gate |
| Confidentiality | Pass | Plaintext marker absent from transit frames and registry files; the independent report audit recomputes registry record count, maximum and total size from the emitted size list, enforces a fixed 64 KiB per-control-record ceiling, and requires zero application payload bytes |
| Signed evidence audit | Pass | Independent fail-closed auditor accepted the one-GiB evidence |
| Full-report audit | Pass | Independent auditor enforces direct, fallback, route, resource and concurrency gates |
| Acceptance evidence isolation | Pass | The runner accepts a new or empty work root and rejects a non-empty root before writing, preventing stale target files or registry records from being mixed into a rerun; a fresh 1 MiB/two-concurrent real-ICE run and both independent audits passed |
| Transit unavailable | Pass | A real advertised peer-2 worker was started, stopped and joined before the request; its still-valid capacity was discovered, the request failed explicitly within the bounded timeout, and no committed or partial target file remained |
| Atomic capacity-refresh stress | Pass | Repeated after the read/write retry fixes: 63/63 sessions in 60.705 s while both workers refreshed every 0.1 s; independent soak audit accepted zero failures, 910,156-byte memory growth, clean thread shutdown, no plaintext and no partial files |
| Established data plane without control plane | Pass | Registry access was actively denied for 1.718 s after both ICE legs were nominated; a 4 MiB encrypted request completed during the blackout with matching hashes, no TURN and clean worker shutdown |

The full resource report and signed evidence are generated locally under
`.codex-tmp/peer-transit-acceptance-full/` and are intentionally not committed
because the directory contains two one-GiB test artifacts.

## Running gate

The first persistent run correctly failed closed after 3,604 seconds and 360
successful sessions because the one-hour signed capacity record expired. The
worker had advertised only at startup. Commit `a8458cd` adds a 15-minute
capacity heartbeat, atomic capacity-record replacement, and regression plus
accelerated integration coverage. The pre-fix elapsed time is invalidated.

The second run started from zero at 2026-08-31 19:18 Hong Kong time. It
completed 2,067 successful sessions over 50,861 seconds before failing at
2026-09-01 09:26. The failure happened while the transit worker was atomically
refreshing its otherwise valid capacity record: on Windows, a reader can
briefly receive a sharing `OSError` during `os.replace()`, and the registry
correctly skipped that one unreadable record. Discovery previously treated
the single empty read as permanent absence. Commit `14f347f` adds a bounded
five-attempt discovery retry (200 ms maximum) and regression coverage. The
second run is invalidated rather than combined with a later run.

The third run started from zero at 2026-09-01 09:33 Hong Kong time. It crossed
the first 15-minute refresh boundary and completed 114 sessions over 1,133
seconds with zero session failures. A subsequent acceptance-contract audit
added a real post-ICE registry blackout gate. While exercising that gate, an
immediate duplicate capacity publication exposed the corresponding Windows
write-side sharing race: `os.replace()` can itself receive a transient
`PermissionError` when a reader has the destination open. Commit `cf45f16`
adds a bounded five-attempt atomic-replace retry, a regression test, the real
control-plane blackout scenario, and fail-closed report auditing. Because this
changes runtime registry code, the third run is invalidated despite its clean
intermediate result.

The fourth 24-hour worker soak started from zero at 2026-09-01 09:52 Hong Kong
time. It completed 286 sessions over 2,857.924 seconds with zero failures,
crossed three production-interval capacity refreshes, kept all three worker
threads alive, stayed within the memory gate, and exposed neither plaintext
markers nor partial files. A later upstream fetch advanced `upstream/main`
from `95d5bac` to `b0b17c1` with three LLM-service hardening commits, including
changes overlapping the shared P2P and registry modules. The fourth run was
therefore stopped and invalidated; its clean duration is not combined with the
final run.

The branch was rebased onto `upstream/main` at `b0b17c1`. The merged candidate
preserves the upstream request/response lost-ACK recovery and the peer-transit
opaque-byte selective-ACK transport as separate APIs. Targeted tests for both
paths passed 19/19, related Ruff passed, and the complete Python suite passed
546 tests with three skips after the evidence-auditor hardening. An 8
MiB/three-concurrent real-ICE preflight and both independent auditors passed.
The full-report auditor now re-verifies signed healthy-direct, hard-failure,
concurrent-session and post-ICE registry-blackout evidence rather than trusting
producer summary booleans.

The fifth 24-hour worker soak started from zero at 2026-09-01 10:45:48 Hong
Kong time and is scheduled to finish at approximately 2026-09-02 10:45:48. It
writes atomic progress to
`.codex-tmp/peer-transit-soak-24h-r5/progress.json`. Its first minute completed
seven sessions with zero failures, both signed capacity records present, all
three worker threads alive, memory growth of 1,365,176 bytes, no plaintext
marker exposure, no partial files, and empty stderr.

Both r5 workers completed their first production-interval capacity refresh at
2026-09-01 11:00:48--11:00:49 Hong Kong time. The soak had completed 90
sessions with zero failures at the refresh boundary and six more sessions
immediately afterward. The Python process, three worker threads, memory gate,
plaintext scan, partial-file scan and stderr all remained healthy.

The fifth run was deliberately stopped at 2026-09-01 13:23 Hong Kong time
after 9,439 seconds and 762 successful sessions with zero failures. An
implementation audit proved that a remote `relay` candidate could still be
parsed and added to the ICE agent before the already-existing nominated-pair
check rejected it. Application payload could not traverse that candidate, but
the acceptance contract forbids both signaled and nominated TURN candidates.
The shared ICE runtime was therefore changed to reject `relay`, non-direct and
non-UDP candidates both while parsing signed signaling and again while applying
programmatically constructed signaling. This runtime change invalidates r5.

The replacement candidate passed 23 targeted shared-P2P/peer-transit tests,
related Ruff, the complete Python suite (547 passed, three skipped), and a new
8 MiB/three-concurrent real-ICE preflight with both independent auditors. A new
24-hour run must start from zero on the fixed runtime.

The sixth 24-hour worker soak started from commit `9dd0966` at 2026-09-01
13:27:44 Hong Kong time and is scheduled to finish at approximately 2026-09-02
13:27:44. It writes atomic progress to
`.codex-tmp/peer-transit-soak-24h-r6/progress.json`. Its first 40.4 seconds
completed five sessions with zero failures, established the post-warm-up memory
baseline, kept all three worker threads alive, published both signed capacity
records, and exposed neither plaintext markers, partial files nor stderr.

Both r6 workers completed their first production-interval capacity refresh at
2026-09-01 13:42:44 Hong Kong time. The soak passed the boundary with 101
completed sessions and zero failures; the refreshed `target` and `transit`
records were independently discovered and Ed25519-verified through the project
registry API. The process, three worker threads, memory gate, plaintext scan and
stderr remained healthy.

The sixth run was invalidated after 6,765.169 seconds and 628 successful sessions
with zero failures because its deadline and elapsed duration were based on the
wall clock. An NTP or manual clock adjustment could therefore make a nominal
86,400-second report finish early. The soak runner now uses `time.monotonic()`
for its loop, elapsed duration, interval remainder and completion decision; the
independent auditor rejects reports that do not declare this monotonic clock
source. A black-box regression advances the wall clock by one hour during a
real short soak and proves that completion still follows the unchanged
monotonic elapsed time. A seventh run must start from zero on the corrected
runner, and no r6 time may be combined with it.

The seventh 24-hour worker soak started from commit `2681f0c` with soak-runner
blob `ab819905e1505073a4081221a385c53e60f41bc1` at 2026-09-01 15:22:41 Hong
Kong time and is scheduled to finish at approximately 2026-09-02 15:22:41. It
writes atomic progress to
`.codex-tmp/peer-transit-soak-24h-r7/progress.json`. At 120.656 monotonic
seconds it had completed 13 sessions with zero failures, established its
post-warm-up memory baseline, retained all three worker threads, accumulated 39
transit frames, and exposed neither plaintext, partial files nor stderr. Both
signed capacity records were independently discovered and Ed25519-verified
through the project registry API. This run starts from zero and does not include
any elapsed time from r6.

Both r7 workers completed their first 15-minute production-interval capacity
refresh at 2026-09-01 15:37:41--15:37:42 Hong Kong time. The project registry
API independently discovered and Ed25519-verified the refreshed `target` and
`transit` records. At 962.375 monotonic seconds the run had completed 97
sessions with zero failures; all 291 expected frames and the minimum payload
bytes were covered, memory and thread gates remained healthy, and plaintext,
partial-file and stderr checks remained clean.

The second r7 production refresh completed for both workers at 2026-09-01
15:52:42 Hong Kong time and was again independently discovered and
Ed25519-verified. At 1,855.656 monotonic seconds the run had completed 186
sessions with zero failures; memory, threads, frame/byte coverage, plaintext,
partial-file and stderr checks all remained healthy.

The third r7 production refresh completed for both workers at 2026-09-01
16:07:42--16:07:44 Hong Kong time. Both 852-byte capacity records advertised
`rynmesh.peer-transit.v1` with `max_concurrent=8` and independently passed the
project's Ed25519 verification. At 2,757.5 monotonic seconds the run had
completed 276 sessions with zero failures, accumulated 828 transit frames and
18,266,508 transit bytes, kept all three worker threads alive, and remained
clean for plaintext, partial files and stderr. Traced memory growth was
1,986,235 bytes against the 32 MiB limit.

The fourth r7 refresh crossed the one-hour boundary at 2026-09-01
16:22:44--16:22:46 Hong Kong time. The two workers replaced their capacity
records independently, both new records passed Ed25519 verification, and
sessions continued without a discovery gap during the two-second offset. At
3,631.0 monotonic seconds the run had completed 363 sessions with zero failures,
1,089 transit frames and 24,024,429 transit bytes. All three worker threads
remained alive and traced memory growth was 2,404,944 bytes.

Final acceptance requires:

- the full 86,400-second duration;
- zero failed sessions and no plaintext marker exposure;
- cumulative frame and byte counts covering every fixed-size completed session,
  with per-session request/response frame counts bound to signed relay evidence;
- an independent filesystem scan of peer-2 storage, registry files and
  stdout/stderr for the unique soak plaintext marker, plus an empty stderr and
  `.part` scan;
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
