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
| Complete Python suite | Pass | 574 passed, 3 skipped on the UDP-window-eight, one-MiB concurrent-probe and failure-report candidate |
| Web tests | Pass | 38 passed |
| Web production build | Pass | TypeScript and Vite build completed |
| Python sdist and wheel | Pass | The `ed83e80` candidate built a 348,389-byte sdist and 275,343-byte wheel in an isolated PEP 517 environment; the wheel installed with dependencies into a fresh virtual environment, imported from `site-packages`, rejected a hostname candidate, exposed the adaptive CLI, retained the eight-packet window and reached/drained an installed worker peak of 20 |
| Healthy direct file path | Pass | Source/target SHA-256 equal; transit byte counter unchanged |
| Direct failure fallback | Pass | Real direct operation rejected; peer-transit delivery completed in 0.532 s |
| Adaptive degradation and recovery | Pass | Independent audit enforced 330 ms/75 ms jitter/18% direct impairment versus 80 ms/1% transit metrics, a 30-second switch, 61-second minimum transit hold, 120-second recovery hold, five recovery probes, an exact no-flap transition sequence, and unchanged transit counters on the post-recovery direct file |
| Two non-TURN ICE legs | Pass | Both nominated candidate pairs were host/UDP and `relay_used=false`; a constructor regression proves that even injected TURN URL/username/password environment values are ignored and no TURN argument reaches `aioice.Connection` |
| One GiB streamed transfer | Pending repeat | An earlier runtime passed 1,073,741,824 bytes with matching hashes; commit `ed83e80` must repeat this gate after the fresh 24-hour soak |
| Bounded memory | Pass / final repeat pending | r18 peak traced Python memory was 5,527,397 bytes; the final one-GiB run must remain below 128 MiB |
| Concurrent callers | Pass | r18 completed 20/20 one-MiB sessions in 25.812 s; 20 unique signed sessions and independent relay/target production-worker timelines both recomputed a peak of 20 |
| Session establishment | Pass | 0.187 s in r18, below the five-second gate |
| Encryption framing overhead | Pass | 0.0830% in r18, below the 15% gate |
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

An independent 30-second Windows resource sample after 500 sessions observed
302--326 process handles (304 first, 302 last), seven or eight OS threads, three
to twelve UDP endpoints (three first and last), and 44.9--48.5 MiB of private
memory. The transient increases aligned with active ICE sessions and returned
to their between-session baselines. The same OS-level sample will be repeated
at completion in addition to the runner's traced-memory and worker-thread gates.

The seventh run was invalidated after 7,015.969 monotonic seconds and 640
successful sessions with zero failures. A security audit showed that the
underlying SDP parser accepted a hostname as a remote ICE candidate address.
Although source orders select an authenticated target peer ID rather than a raw
destination, a malicious signed target signal could still make the transit peer
resolve or attempt an arbitrary hostname, contradicting the documented abuse
boundary. The fixed parser requires component 1, valid UDP ports and unicast IP
literals for both candidate and related addresses; it rejects hostnames,
unspecified, multicast and IPv4 broadcast destinations before they reach the
ICE agent. The invalidated progress snapshot has SHA-256
`3639226EE2CF9CB5530538F96D234CDF0AC32F094B9E96CDB455AF004D68E6CB`.
No r7 duration may be combined with its replacement.

The eighth 24-hour worker soak started from runtime commit `562eee9` with
soak-runner blob `ab819905e1505073a4081221a385c53e60f41bc1` at 2026-09-01
17:30:30 Hong Kong time and is scheduled to finish at approximately 2026-09-02
17:30:30. It writes atomic progress to
`.codex-tmp/peer-transit-soak-24h-r8/progress.json`. At 20.313 monotonic
seconds it had completed three sessions with zero failures, established its
post-warm-up memory baseline, retained all three worker threads, accumulated
nine transit frames and 198,549 transit bytes, and exposed neither plaintext,
partial files nor stderr. Both 852-byte signed capacity records were
independently Ed25519-verified through the project API. This run starts from
zero and includes no elapsed time from r7.

Both r8 workers completed their first production-interval capacity refresh at
2026-09-01 17:45:31 Hong Kong time. The two 852-byte replacement records were
independently Ed25519-verified and continued to advertise
`rynmesh.peer-transit.v1` with `max_concurrent=8`. Sessions completed across the
refresh boundary without a discovery gap; the next observed checkpoint had 96
successful sessions, zero failures, 288 transit frames and 6,353,568 transit
bytes, with healthy memory and worker-thread gates and no plaintext, partial
files or stderr.

An independent 30-second Windows resource sample established an r8-specific
baseline after more than 150 sessions. Idle samples had 295--296 process
handles, six OS threads and zero UDP endpoints; active ICE sessions peaked at
326 handles, eight threads and nine UDP endpoints, then returned to the idle
baseline. Private memory remained between approximately 37.3 and 40.5 MiB.
Completion will repeat this same OS-level comparison rather than reuse any r7
resource measurements.

The second r8 production refresh completed at 2026-09-01
18:00:31--18:00:32 Hong Kong time. Both capacity records again passed the
project's Ed25519 verification, and post-refresh sessions continued without a
discovery gap. The observed checkpoint had 203 successful sessions, zero
failures, exactly 609 transit frames and 13,435,149 transit bytes. Traced memory
growth was 1,876,570 bytes, all three worker threads remained healthy, and the
plaintext, partial-file and stderr checks stayed clean.

The third r8 production refresh completed at 2026-09-01
18:15:32--18:15:33 Hong Kong time. Both new capacity records passed independent
Ed25519 verification, and the next completed-session checkpoint reached 271
successful sessions with zero failures, 813 transit frames and 17,935,593
transit bytes. Memory, worker threads, plaintext, partial-file and stderr gates
remained healthy across the atomic replacements.

The fourth r8 production refresh completed at 2026-09-01
18:30:32--18:30:35 Hong Kong time. Both 852-byte capacity records were
independently discovered and Ed25519-verified through the project registry API;
each continued to advertise only `rynmesh.peer-transit.v1` with
`max_concurrent=8`. The post-refresh checkpoint reached 359 successful
sessions with zero failures, exactly 1,077 transit frames and 23,759,697
transit bytes. Traced memory growth was 1,813,203 bytes, all three worker
threads remained healthy, and the plaintext, partial-file and stderr gates
stayed clean.

The r8 run was deliberately invalidated at 2026-09-01 18:38:13 Hong Kong time
after a manual control-plane authentication audit found that result polling
verified each result signature but did not also constrain the result to the
work order's expected provider and requester identities. An observer capable of
publishing to the registry could therefore race an observed order ID with a
separately signed `accepted` or `failed` result. The preserved snapshot is
`.codex-tmp/peer-transit-soak-24h-r8/progress.invalidated-result-provider-binding.json`
with SHA-256
`CE557274C771679A8239521896D297BD1839E4D6EA79F588173A7C39C8E2FDA8`.
It records 4,062.891 monotonic seconds, 405 successful sessions, zero failures,
1,215 frames, 26,804,115 transit bytes, 2,762,014 bytes of traced memory growth,
a 6,166,427-byte traced peak, all three worker threads and no plaintext
exposure. Both r8 processes were stopped and verified absent. No r8 elapsed time
may be combined with its replacement.

The result-polling fix now supplies registry filters and independently compares
the signed work-order ID, network ID, provider peer ID and requester peer ID
before interpreting any status or result body. A unit adversarial test feeds a
filter-ignoring store legitimate and forged-provider, forged-requester,
wrong-order and wrong-network results; only the exact binding is accepted. A
second integration test publishes a real Ed25519-signed `failed` result from an
attacker identity alongside the legitimate provider's signed `accepted` result
in the file registry and confirms that polling returns only the latter. The
focused peer-transit/LLM suite passed 74 tests and the full Python suite passed
569 tests with three skips after the fix.

A fresh post-fix r9 preflight at
`.codex-tmp/peer-transit-acceptance-r9-result-binding` transferred 8,388,608
bytes with matching source/target SHA-256, 129 request frames, one response
frame and 8,396,260 bytes on each transit direction. Three concurrent sessions
overlapped and completed; setup took 0.157 s, the main transfer 8.5 s, hard
direct failure fell back through the peer in 0.547 s, traced memory peaked at
5,495,377 bytes and protocol overhead was 0.0831%. TURN rejection, registry
control sizing, plaintext scans, post-nomination registry/STUN blackout,
unavailable-relay atomic failure and both independent evidence/report audits
passed. `report.json` has SHA-256
`2A3215FE0C5FC7A87B5A89D0F9B12C68872A44D7E69F317325F396F432658B52` and
`evidence.json` has SHA-256
`791BBF2052EB4386063E8FDA6085F202AACA07C31DE7402D2DB3600FD550F019`.

The r9 candidate also passed an isolated PEP 517 source/wheel build and clean
dependency installation into `.codex-tmp/venv-peer-transit-r9-start`. The
274,233-byte wheel has SHA-256
`2C7C6DC5C9D96213A6A446A9580E94CA39781CB7032160DF0C0CBF199CA87D1C`;
the 345,893-byte sdist has SHA-256
`3466595FEFB287203EC07487AAEDD1AE6A669DD3724A0C2CB8E0A9625F900D82`.
An installed-package black-box check imported protocol
`rynmesh.peer-transit.v1`, rejected a hostname ICE candidate, ignored a forged
failure from the wrong provider while accepting the exact provider/requester
binding, and confirmed that `rynmesh-transit --help` exposes the adaptive
ordinary-peer path.

The ninth 24-hour worker soak started from result-identity-binding runtime
commit `7dc7528` with soak-runner blob
`ab819905e1505073a4081221a385c53e60f41bc1` at 2026-09-01 19:00:22 Hong Kong
time and is scheduled to finish at approximately 2026-09-02 19:00:22. It writes
fresh atomic progress to
`.codex-tmp/peer-transit-soak-24h-r9/progress.json`; launcher PID 50716 owns
Python worker PID 51412. At 20.328 monotonic seconds it had completed three
sessions with zero failures, established a 559,216-byte post-warm-up traced
baseline, retained all three worker threads, accumulated exactly nine frames
and 198,549 transit bytes, and exposed no plaintext, partial files or stderr.
Both initial 852-byte capacity records were independently discovered and
Ed25519-verified through the project API. This run starts from zero and includes
no elapsed time from r8.

The r9 run was deliberately invalidated at 2026-09-01 19:05:13 Hong Kong time
when the follow-up open-queue audit found a second result-binding gap. Although
the client now ignored a forged result, `FilePeerRegistry.list_work_orders`
previously considered the latest result from any signed provider when deciding
whether an order was still open. A registry observer could therefore publish a
result under its own identity for an observed order ID and hide that order from
the intended provider. The preserved snapshot is
`.codex-tmp/peer-transit-soak-24h-r9/progress.invalidated-open-order-result-binding.json`
with SHA-256
`EAA7FEA280DB2ADEAB52F3EFD095D076853660D24C90688CD5EDE471E2CBC52D`.
It records 290.953 monotonic seconds, 30 successful sessions, zero failures, 90
frames, 1,985,490 transit bytes, 1,393,654 bytes of traced memory growth, a
6,140,620-byte traced peak, three healthy worker threads and no plaintext.
Both r9 processes were stopped and verified absent; no r9 duration may be
combined with its replacement.

The registry fix now creates work-order files exclusively, permits only an
identical idempotent resubmission, rejects a different signed payload reusing an
existing order ID, rejects orphan results, and accepts a result only when its
signed order/network/provider/requester fields match the immutable order. Open
queue status applies the same four-field binding and checks returned records
again even if the lower-level result filter is ignored. Adversarial tests cover
wrong-provider publication, queue hiding, order-ID overwrite and orphan
results. The full Python suite passed 571 tests with three skips after this
hardening.

A fresh post-fix r10 preflight at
`.codex-tmp/peer-transit-acceptance-r10-order-binding` transferred 8,388,608
bytes with matching source/target hashes, 129 request frames, one response
frame and 8,396,260 bytes per transit direction. Three concurrent sessions
overlapped and completed; setup took 0.110 s, the main transfer 7.188 s, hard
direct failure fell back through the peer in 0.468 s, traced memory peaked at
5,499,780 bytes and protocol overhead was 0.0831%. TURN rejection, registry
control sizing, plaintext scans, post-nomination registry/STUN blackout,
unavailable-relay atomic failure and both independent audits passed.
`report.json` has SHA-256
`561D14E965594EDA5BB2436935F18971064B25B2537F647694AD040DEC05B1DD` and
`evidence.json` has SHA-256
`62988EDAE44A5FA1A2393EC72408A89297432DA2582239D18F8565846AFB08ED`.

The r10 candidate passed an isolated PEP 517 build and clean dependency install
into `.codex-tmp/venv-peer-transit-r10-start`. The 274,569-byte wheel has
SHA-256
`72AD504F65CCC1196DD09E843382AADC4DD019C0158A4CE38CBC6CAA218D3A24`;
the 346,861-byte sdist has SHA-256
`0B0B0AE7121281B36699B6A79A69E665F88991BD5A469761FC09127A23EDBDB1`.
An installed-package black-box scenario created three real identities, proved
that the registry rejects the attacker's signed result while leaving the order
visible to its designated provider, imported `rynmesh.peer-transit.v1`, and
confirmed that the installed CLI exposes adaptive ordinary-peer transit.

The tenth 24-hour worker soak started from registry-lifecycle runtime commit
`f55d93f` with soak-runner blob
`ab819905e1505073a4081221a385c53e60f41bc1` at 2026-09-01 19:20:54 Hong Kong
time and is scheduled to finish at approximately 2026-09-02 19:20:54. It writes
fresh atomic progress to
`.codex-tmp/peer-transit-soak-24h-r10/progress.json`; launcher PID 45440 owns
Python worker PID 44272. At 20.328 monotonic seconds it had completed three
sessions with zero failures, established a 569,754-byte post-warm-up traced
baseline, retained all three worker threads, accumulated exactly nine frames
and 198,552 transit bytes, and exposed no plaintext, partial files or stderr.
Both initial 852-byte capacity records were independently discovered and
Ed25519-verified through the project API. This run starts from zero and includes
no elapsed time from r9 or any earlier run.

The r10 run was deliberately invalidated at 2026-09-01 19:25:36 Hong Kong time
after the production-worker concurrency audit found that `max_concurrent` was
advertised but `serve_forever()` synchronously processed each complete session.
The old acceptance timeline measured overlapping source callers, so it could
report a peak of 20 while relay and target workers actually handled one session
at a time. The preserved snapshot is
`.codex-tmp/peer-transit-soak-24h-r10/progress.invalidated-worker-concurrency.json`
with SHA-256
`F5F4959629D00F865F78AF16AC80EA9031B149766A257E21202541B05AFEA139`.
It records 281.219 monotonic seconds, 29 successful sessions, zero failures, 87
frames, 1,919,336 transit bytes, 1,337,879 bytes of traced memory growth, a
6,184,341-byte traced peak, three worker threads and no plaintext. Both r10
processes were stopped and verified absent; no r10 duration may be combined
with its replacement.

The worker now uses a bounded executor sized by `max_concurrent`, deduplicates
in-flight order IDs and drains active handlers on shutdown. Session audit hooks
record handler entry/exit inside both relay and target workers. Frame counters
used by concurrent acceptance are lock-protected, and repeated registry outage
errors are rate-limited rather than flooding logs. The acceptance producer now
emits separate relay-worker, target-worker and diagnostic caller timelines;
the independent auditor requires both worker timelines to contain exactly the
signed concurrent session IDs and independently recomputes both peaks. It
rejects old caller-only reports, missing target timelines and forged target
peaks. The full Python suite passed 572 tests with three skips after the change.

A fresh thread-safe concurrency preflight at
`.codex-tmp/peer-transit-acceptance-r12-threadsafe-concurrency` transferred
8,388,608 bytes with matching source/target hashes, 129 request frames, one
response frame and 8,396,269 bytes per transit direction. All three signed
sessions overlapped inside relay handlers from 0.062--0.531 s and inside target
handlers from 0.172--0.531 s, producing independently audited peaks of three on
both workers. Setup took 0.156 s, the main transfer 8.359 s, hard direct failure
fell back in 0.531 s, traced memory peaked at 5,518,825 bytes and protocol
overhead was 0.0831%. All TURN, blackout, unavailable-peer, plaintext,
control-size and hash gates passed. `report.json` has SHA-256
`074324F3DBA795050DED790A2DE07E1BAF85A81C521BFE337393D697988A5A1F` and
`evidence.json` has SHA-256
`D65891E7E30AB61839BEE49B47165B54F0E223E4FB2323AC27F245B20B0E30A8`.

After adding fail-safe cleanup for a completed worker future, the exact final
candidate was rerun from the fresh
`.codex-tmp/peer-transit-acceptance-r13-final-concurrency-preflight` root. It
again passed with 8,388,608 source bytes, 130 transit frames, 8,396,274 bytes
per transit direction, three completed signed sessions and independently
recomputed relay/target worker peaks of three. Setup took 0.203 s, the main
transfer 8.422 s, hard-failure fallback 0.485 s, peak traced memory was
5,525,350 bytes and overhead was 0.0831%. Both independent audits passed;
`report.json` has SHA-256
`8DB8698D8065F585D93F8C0ACAC5E6B1880A79E30BE50A416C7F37C75181C1B6` and
`evidence.json` has SHA-256
`F8AC897AE006D93CB39343F79C97D97BA8C078687CA17767F086C33F6F4B7CED`.

The worker-concurrency candidate passed an isolated PEP 517 build and clean
install into `.codex-tmp/venv-peer-transit-r11-start`. The 275,255-byte wheel
has SHA-256
`8C6EE39D34A70B3854D6F4698757D853B9755F69E7122E08924A6668E8F9F162`;
the 347,883-byte sdist has SHA-256
`95F5DD30471C048CB6536DCB17E49872AFD0B7C7BD6A80FA741C59492DB10831`.
An installed-package black-box worker dispatched three distinct orders to a
measured internal peak of three, deduplicated every order, drained to zero
active handlers and exposed the session-audit parameter. The installed CLI
continued to expose adaptive ordinary-peer transit.

The exact worker-concurrency candidate, including completed-future cleanup,
also passed the full Python regression suite with 572 tests passed, three
skipped and no failures. Ruff passed for every transit runtime, registry,
package adapter, acceptance/audit runner and focused test file. The web client
passed all 38 tests in nine files, TypeScript no-emit checking and a Vite
production build.

The eleventh 24-hour worker soak started from the concurrency-enforcing runtime
commit `58456e5` with soak-runner blob
`e1164671aec7b55278420fdf43720d7337317620` at 2026-09-01 19:49:14.695465 Hong
Kong time and is scheduled to finish at 2026-09-02 19:49:14.695465. It writes
fresh atomic progress to
`.codex-tmp/peer-transit-soak-24h-r11/progress.json`; launcher PID 40600 owns
Python worker PID 35120. At 140.750 monotonic seconds it had completed 15
sessions with zero failures, accumulated 45 frames and 992,760 transit bytes,
retained all worker threads, stayed below the 32 MiB post-warm-up memory-growth
limit, and exposed no plaintext, partial files or stderr. The project registry
API independently discovered and Ed25519-verified both 852-byte capacity
records: the ordinary relay advertised the `transit` role and the ordinary
target advertised the `target` role, each with `max_concurrent=8`. Runtime
files are required to remain identical to `58456e5`, the soak runner must keep
the stated blob identity, and `upstream/main` must remain at `b0b17c1`; a
change to any fixed point invalidates the run. This run starts from zero and
includes no elapsed time from r10 or any earlier run.

The r11 run was deliberately invalidated at 2026-09-01 20:12 Hong Kong time
after a stronger 20-session preflight replaced the previous 64 KiB concurrency
payload with one MiB per session. The preserved snapshot is
`.codex-tmp/peer-transit-soak-24h-r11/progress.invalidated-20-concurrency-stream-instability.json`
with SHA-256
`DC7FC6143E8E6BFCF72CA75AEA4C138395BB1839FE60FC81852EFFD392D5CC00`.
It records 1,355.047 monotonic seconds, 136 successful sessions, zero soak
failures, 408 frames, 9,001,024 transit bytes, 1,498,585 bytes of traced memory
growth, a 5,962,610-byte traced peak and no plaintext. Both r11 processes were
stopped and verified absent; none of this duration may be combined with the
replacement run.

The first 20-way diagnostic completed all 64 KiB transfers but observed a
relay-worker peak of 20 and target-worker peak of only 16, proving that such a
small payload did not reliably exercise target concurrency. Increasing every
probe to one MiB then exposed UDP burst congestion: the previous 32-fragment
per-connection window caused connection loss across simultaneous two-hop
streams. A rejected global four-slot experiment also demonstrated that a
single-process hermetic test must not make its three logical nodes compete for
one artificial process-wide pool. All failed roots remain preserved as r14
through r17 evidence and are not counted as passing runs.

Commit `ed83e80` instead limits each ICE connection to an eight-fragment
reliable-send window, keeps all 20 sessions independently active, raises the
audited concurrent payload floor to one MiB and preserves producer failure
reports when final self-audit rejects a completed report. The fresh r18 run at
`.codex-tmp/peer-transit-acceptance-r18-20worker-window8-preflight` passed both
independent audits. Its 8,388,608-byte main transfer completed in 8.984 s with
130 transit frames and 8,396,292 transit bytes per direction. All 20 one-MiB
sessions completed in 25.812 s; signed relay and target handler timelines each
recomputed a peak of 20. Hard direct failure fell back in 1.063 s, traced
memory peaked at 5,527,397 bytes, hashes matched and no TURN or plaintext was
observed. `report.json` has SHA-256
`8C21974F15C9CF138FAB2FFA5AC7BA5424B82A0823EECB72F5A6F6EC287B5D7D` and
`evidence.json` has SHA-256
`7D43CC216418127E73CAA8AE2F3868C8ECA843E24C0B5157BEF136D38DC4C41A`.
The exact candidate passed 574 Python tests with three skips and all relevant
Ruff checks.

An independent repeat at
`.codex-tmp/peer-transit-acceptance-r19-20worker-window8-repeat` also passed.
Its 8,388,608-byte main transfer completed in 9.000 s; all 20 one-MiB sessions
completed in 27.016 s and the independently audited relay and target worker
peaks were again 20. Hard-failure fallback took 1.297 s and traced memory
peaked at 5,521,176 bytes. `report.json` has SHA-256
`E4DC87DB32A47DD851C73D904DB36218562A7F0A5B248A2D3B7E6A26B26ECA61` and
`evidence.json` has SHA-256
`9970C11F4B1CAE308EE091AFAB00FAF6084EDA40D621C7926F443B99A127A665`.

The acceptance CLI now preserves a structured `result=error` report when an
operational exception interrupts a run after a fresh work root was prepared.
It records the exception, traceback and remaining partial-file count. A
regression also proves that a pre-existing non-empty root is rejected without
writing the requested report or changing its sentinel evidence.

The `ed83e80` package candidate passed an isolated PEP 517 build and clean
dependency installation into `.codex-tmp/venv-peer-transit-r12-start`. The
275,343-byte wheel has SHA-256
`81907F6B9B1CD17238A1F8593106DDB498607B1E2364FEA40CFD7C9945475A3A`;
the 348,389-byte sdist has SHA-256
`A757A317BBD5AE5FDDF6FAECB812A3BA55621AD951E7F770741601D03B8A37A8`.
The installed package imported from its virtual-environment `site-packages`,
reported the eight-fragment window, rejected hostname signaling, exposed all
four transit CLI modes and dispatched/drained 20 internal worker handlers with
a measured peak of 20.

The twelfth 24-hour worker soak started from runtime commit `ed83e80` with
soak-runner blob `e1164671aec7b55278420fdf43720d7337317620` at
2026-09-01 20:39:53.368742 Hong Kong time and is scheduled to finish at
2026-09-02 20:39:53.368742. It writes fresh atomic progress to
`.codex-tmp/peer-transit-soak-24h-r12/progress.json`; launcher PID 39496 owns
Python worker PID 49244. At 130.719 monotonic seconds it had completed 14
sessions with zero failures, accumulated 42 frames and 926,576 transit bytes,
kept memory growth at 931,934 bytes, retained all worker threads and exposed no
plaintext, partial files or stderr. The project registry API discovered and
Ed25519-verified the ordinary relay and target capacity records, both 852 bytes
with `max_concurrent=8`. Runtime files must remain identical to `ed83e80`, the
runner must retain the stated blob and `upstream/main` must remain at `b0b17c1`;
otherwise r12 is invalidated and restarted from zero. No earlier duration is
included.

The first r12 production capacity refresh completed at 2026-09-01
20:54:53--20:54:54 Hong Kong time. Both atomic replacement files remained 852
bytes, retained `max_concurrent=8` and were independently discovered and
Ed25519-verified through the project API. At the following checkpoint the soak
had reached 993.594 monotonic seconds and 100 successful sessions with zero
failures, 300 frames, 6,618,400 transit bytes, 1,344,711 bytes of traced memory
growth, all worker threads present, no plaintext, no partial files and empty
stderr. Monotonic and wall elapsed measurements differed by only 0.008 s.

The second scheduled capacity refresh completed at 2026-09-01 21:09:54 Hong
Kong time without interrupting the data plane. The relay record has SHA-256
`0732D706D0F59D14E39B25BC2ED4C8308E80B6CA0355523195EAC797B8B83400` and the
target record has SHA-256
`D5D1ACDEB0C4970A8F3F3A4250E6CB03E0803D0D07CCA6DB7E259AE416A1A4C8`.
Both signed files remained 852 bytes, retained their `transit` and `target`
roles respectively, advertised `max_concurrent=8`, and were independently
parsed and signature-verified. At the subsequent 2,046.656-second checkpoint,
205 sessions had completed with zero failures, 615 frames and 13,567,720
transit bytes. Traced memory growth was 1,944,084 bytes with a 6,106,431-byte
peak; an idle OS sample had zero UDP endpoints and partial files, eight OS
threads, 302 handles, 39,706,624 private bytes and 56,561,664 working-set bytes.
Stderr remained empty and no plaintext was observed.

An independent registry replay at the 2,517.187-second checkpoint verified all
504 persisted work-order signatures and all 1,260 persisted work-result
signatures rather than trusting the live counter. The 252 completed sessions
each had exactly one `open_peer_transit` and one `accept_peer_transit` order,
with five correctly identity-bound result stages per session. The same replay
verified both capacity signatures through the registry API and scanned 1,772
relay, relay-network, registry and log files containing 3,330,315 bytes; it
found no soak plaintext marker, partial file or stderr. Successive live polls
showed the single open order changing and clearing as sessions advanced, which
confirms it was the current in-flight session rather than an abandoned order.

The third scheduled capacity refresh completed at 2026-09-01
21:24:54--21:24:55 Hong Kong time. The relay record has SHA-256
`2FF23D9B3F21121A11994779E029635D52DB8CE0F049C001FC104F5CD4DC832B` and the
target record has SHA-256
`FB05D3363505320565FA2DF82F2D3FD309BBBC0FF0676FC58358C3DCEE2138E6`.
Both 852-byte records passed independent API signature verification with their
expected roles and `max_concurrent=8`. At 2,698.469 monotonic seconds the soak
had completed 270 sessions with zero failures, 810 frames, 17,869,680 transit
bytes, 2,268,027 bytes of traced memory growth, no plaintext, no partial files
and empty stderr. A fresh chunked scan covered 1,905 transit-side files and
3,581,039 bytes without finding the marker.

The final one-GiB gate was also preflighted without starting it alongside the
soak: its fresh root
`.codex-tmp/peer-transit-acceptance-final-r12` did not exist, the D drive had
124,723,048,448 free bytes, and the Python 3.12.10 environment exposed the
required one-GiB, 20-concurrent and fail-closed audit flags. The final run must
still use a never-before-used root and begins only after r12 passes its full
duration, so this readiness check is not counted as transfer evidence.

An idle operating-system baseline was captured at 1,214.000 monotonic seconds
after 122 sessions and stored in
`.codex-tmp/peer-transit-soak-24h-r12/os-resource-baseline.json`. Worker PID
49244 had 298 handles, eight OS threads, 38,215,680 private bytes and
53,858,304 working-set bytes; UDP endpoints and partial files had both returned
to zero. The baseline file has SHA-256
`809CEB4EEFF8E782BB32BE783FA8BA75A6123E9F2B4950B1F16E78354A197B7B`.
Final shutdown evidence must verify that hash, repeat these fields and explain
any material growth rather than relying only on traced Python allocations.

A same-condition idle comparison at 2,969.484 monotonic seconds and 297
completed sessions found 301 handles, eight OS threads, 43,491,328 private
bytes, 59,826,176 working-set bytes, zero UDP endpoints and zero partial files.
Relative to the 1,214-second baseline this is three additional handles, no
additional OS threads, 5,275,648 additional private bytes and 5,967,872
additional working-set bytes after 175 more sessions; traced Python growth
increased by 1,140,634 bytes. Failures and stderr remained zero. This is an
intermediate resource observation, not a substitute for the final post-run
shutdown and process-absence checks.

The fourth r12 capacity refresh completed at 2026-09-01
21:39:55--21:39:56 Hong Kong time, but r12 was intentionally invalidated soon
afterward when a growing-history benchmark exposed a long-run scalability
defect. With about 337 completed sessions, twelve cold-history open-order polls
had relay and target medians of 957.144 ms and 748.606 ms respectively, with a
relay p95 of 979.557 ms. A separate 20.018-second sample showed worker PID
49244 consuming 26.578 CPU seconds, or 132.77% of one core, while completing
only two scheduled sessions. The registry was rereading and Ed25519-verifying
every immutable historical order and result on every 20 ms worker poll, so the
cost would continue increasing through a full-day run.

The preserved r12 snapshot is
`.codex-tmp/peer-transit-soak-24h-r12/progress.invalidated-registry-poll-scaling.json`
with SHA-256
`DD899785294C602E5E69CEC965AAA1DD3A4A4E1D60700A8C44F6CED10DDDA11B`.
It records 3,669.594 monotonic seconds, 367 completed sessions, zero failures,
1,101 frames, 24,289,528 transit bytes, 2,966,224 bytes of traced memory growth
and no plaintext. Both r12 PIDs were stopped and verified absent; partial files
and stderr were zero. None of this duration is eligible for a replacement
soak.

The replacement runtime preserves canonical signed history but adds a
versioned, per-provider `open-work-orders` marker index. Every returned item is
still loaded from the canonical file, signature-verified and checked against
its latest signed result; markers provide no trust. Results remove their
marker, stale markers are repaired on read and legacy registries rebuild once.
On an isolated copy of all 367-session r12 registry data, the legacy rebuild
took 364.588 ms. One hundred steady-state polls per provider then measured
relay/target medians of 0.106/0.104 ms and p95 values of 0.119/0.120 ms, with no
historical objects retained in memory. Two index regressions and the full
Python suite passed with 576 tests, three skips and no failures.

The first indexed-runtime preflight at
`.codex-tmp/peer-transit-acceptance-r20-open-index-preflight` passed its
producer and both then-current independent audits. It transferred 8,388,608
bytes in 7.750 s and completed all 20 one-MiB sessions in 26.610 s with relay
and target internal peaks of 20. Peak traced memory was 5,533,493 bytes and
hard-failure fallback took 0.750 s. `report.json` has SHA-256
`63C62922D2C81E77B01453EFFCDE5CFD58601330ABFD5D33C52D454AE50DA4A3` and
`evidence.json` has SHA-256
`6CB8C86607499913116423C4F161FB4A5D9B72E736E106E2E3A1DE882A2FFB25`.

The independent repeat at
`.codex-tmp/peer-transit-acceptance-r21-open-index-repeat` exposed a Windows
marker-cleanup race even though its old producer wrote `result=pass`: the main
worker logged `PermissionError: [WinError 5]` while deleting an
`open-work-orders` marker. Its report has SHA-256
`43017B2D9A9341C28C7761DB238A2FDEE0F94659699C3A6FB7A99DEC533C83A2`; the
preserved invalidation record has SHA-256
`9AC70DA2C55EE6DFE3A87687EF6EEE7181952A232E4D100BF220B80A60855667`.
The cleanup now treats a denied deletion as a safe stale-marker retry, and
workers expose structured control-error counts. Producer reports and both
auditors require zero errors from the main relay and target. The strengthened
auditor rejects the old r21 report, and the fixed candidate passes 578 Python
tests with three skips. Neither r20 nor r21 is accepted as evidence for the
new fixed point.

The next fresh run at
`.codex-tmp/peer-transit-acceptance-r22-index-worker-error-preflight` proved
that the strengthened producer fails closed: both main worker error snapshots
were clean, but it reported `result=fail` because the target timeline measured
20 while the relay timeline contained only 19 complete entries. The report
has SHA-256
`AF263E409B2EEB8718828B5172756826A024D1AD9B8330E7354D48C7F80C8FC8` and
the main evidence has SHA-256
`AF5B98ADDFBA1DD7F9DEFB91DB00A0A96A8AB718CF6B4EA58873DAD63AC6F549`.
Inspection showed that the final client had received its signed relay result
while that same relay handler's `finished` audit callback had not yet executed;
the report sampled the trace in that few-millisecond window. The producer now
waits at most five seconds for callbacks from the already-returned signed
sessions, without delaying handler starts or creating artificial overlap. Both
producer and auditor require `worker_trace_complete=true`; the focused suite
passes 39 tests.

The corrected fixed point `a378bad` then passed two fresh runs and both
independent audits. In r23 at
`.codex-tmp/peer-transit-acceptance-r23-trace-drain-preflight`, the 8,388,608
byte main transfer took 9.313 s and all 20 one-MiB sessions completed in
36.172 s; relay and target peaks were both 20, the trace was complete, both
main control-error counts were zero, peak traced memory was 5,541,312 bytes and
hard-failure fallback took 0.844 s. `report.json` has SHA-256
`1008573FD5C1EA8456334C9E91F4F3088CCCCD067A2372547CE0249DF68C33EA` and
`evidence.json` has SHA-256
`54F8C4FE3EC5897D9224F299B44DEF48827CFC80163833B81C3CB50DDC3EB969`.

The independent r24 repeat at
`.codex-tmp/peer-transit-acceptance-r24-trace-drain-repeat` completed its main
transfer in 9.703 s and all 20 one-MiB sessions in 35.579 s. Both internal
peaks were again 20, the trace was complete, control-error counts were zero,
peak traced memory was 5,518,301 bytes and hard-failure fallback took 0.828 s.
Its `report.json` has SHA-256
`BEEBEF3EEBA79FB6093AC57CA3DDAB806C89778DAA2C50987BBC0A683D4B18A4` and
`evidence.json` has SHA-256
`CF6B4CDB1C610D90FE774846CF19A26D1808BCF6961DBD79B7260CDE37759141`.
The exact fixed point passes 579 Python tests with three skips and all relevant
Ruff checks.

The thirteenth 24-hour worker soak started from runtime fixed point `a378bad`
and soak-runner blob `1f8fe15de836702619911531d2c24b6e7e802a57` at
2026-09-02 09:13:46.593722 Hong Kong time. It is scheduled to finish at
2026-09-03 09:13:46.593722 and writes fresh progress to
`.codex-tmp/peer-transit-soak-24h-r13/progress.json`; launcher PID 16424 owns
Python worker PID 47884. The initial 852-byte relay and target capacity files
have SHA-256
`D94C0E971452613EC34C16531671D0F48F21C482A8204160190E08AC68000361` and
`7F9C069419C5C8938DDABB7FE4417CD006F3F8E6FCFFAA371228C589CE74959F`
respectively, and both passed project API signature verification with the
expected roles and `max_concurrent=8`. At 110.531 monotonic seconds, r13 had
completed 12 sessions with zero failures, 36 frames, 794,208 transit bytes,
857,164 bytes of traced memory growth, zero main worker control errors, no
plaintext, no open markers, no partial files and empty stderr. Fifty
all-provider open-order polls measured a 0.440 ms median and 0.734 ms p95.
`upstream/main` remained at `b0b17c1`; any runtime, runner or upstream change
invalidates r13 and requires another zero-duration start.

After r13 passed 100 completed sessions, 100 signed canonical open-order polls
for each provider measured 0.223 ms and 0.224 ms medians, 0.263 ms and 0.251 ms
p95, and maxima below 0.53 ms. A simultaneous 20.036 s operating-system sample
of the persistent soak process consumed 1.078 CPU seconds, or 5.38% of one
core, while normal sessions continued. At the end of that sample r13 had
completed 108 sessions with zero failures, zero worker control errors and
2,173,108 bytes of traced memory growth. This is the long-history regression
check for the defect that previously reached 132.77% of one core and roughly
one-second provider polls.

The first scheduled capacity refresh also completed during r13. Both 852-byte
records were re-signed at 2026-09-02 09:28:46 Hong Kong time, roughly fifteen
minutes after launch, and the project verification API accepted both with
capability `rynmesh.peer-transit.v1` and `max_concurrent=8`. Their refreshed
SHA-256 values are
`955720B512B26BED2EF5B10528CBD480211B0F6E94C2ECF5470CEE532EA7870A` and
`610C49C7F8930CB3F2C1872A86A6F2323B6DC44633571D67981443E12DD62CB2`.
At the following checkpoint r13 had completed 129 sessions with zero failures,
zero worker control errors and no plaintext exposure.

The warm idle operating-system baseline is preserved at
`.codex-tmp/peer-transit-soak-24h-r13/os-baseline-warmup.json`, SHA-256
`73BF96E6AA16C7350A7888629E55671B7D4D6E4DCCC8E45A178CCC1113EDDDE3`.
At 134 completed sessions it recorded 309 handles, eight OS threads,
35,938,304 private bytes and a 52,518,912-byte working set for the persistent
process. The idle sample had zero UDP endpoints, open markers, partial files
and stderr bytes, with both worker error snapshots still zero. Final shutdown
verification must use the same fields and prove that the process no longer
exists rather than treating a lower resource count as sufficient.

An independent same-field checkpoint after 233 sessions is preserved at
`.codex-tmp/peer-transit-soak-24h-r13/os-checkpoint-233.json`, SHA-256
`3AC7BB613DACA73987A0B321A9084CF743F3A25563341D414A42464F0C422657`.
Across the additional 99 sessions, handles changed from 309 to 307, OS threads
remained at eight, private bytes decreased by 270,336 and working set decreased
by 1,404,928 bytes. UDP endpoints, open markers, partial files, stderr, failures
and worker errors were all zero at the idle checkpoint.

The `a378bad` package candidate also passed an isolated PEP 517 build and clean
dependency installation into `.codex-tmp/venv-peer-transit-r13-start`. The
276,476-byte wheel has SHA-256
`F73C249D066408CF19CC0B6F2AD49D8A1E89AA6C941A7DBFD6974904B19B8D33`; the
351,184-byte sdist has SHA-256
`E9A087D817845D142190E9A459543CA7F1D76AE00AA22722A9AB6A5799A4A645`.
From outside the source tree, the installed module imported from its virtual
environment `site-packages`, exposed all four transit CLI modes, retained the
eight-fragment send window, rejected hostname candidates, rejected a result
from the wrong provider identity, indexed only one active order across ten
closed historical orders, drained the marker after completion and reported a
zeroed worker control-error snapshot. The live r13 soak continued through this
build and install load with zero failures, control errors, partial files and
stderr.

The current branch web application also passed its release gates while r13
continued running: Vitest passed 38 tests across nine files, `tsc -b --noEmit`
reported no type errors, and `tsc -b && vite build` produced the production
bundle successfully. These checks did not change the transit runtime or soak
runner fixed point.

A requirement-by-requirement audit found that r23/r24 supplied route-quality
metrics to the hysteresis state machine but did not impair the real direct UDP
application datagrams. The acceptance producer and independent auditor now
fail closed unless a real nominated direct pair is shaped, its loss is
recomputed from attempted/dropped datagrams, its retransmitted file is unique
and intact, and a subsequent adaptive request produces signed peer-transit
evidence with increased relay counters. The strengthened auditor rejects r24
with `real_degraded_network` missing.

Two fresh strengthened preflights passed. r25 at
`.codex-tmp/peer-transit-acceptance-r25-real-impairment-preflight` shaped 342
application datagram sends across a 250-350 ms scheduled RTT range and dropped
61 (17.836%). The impaired direct file completed in 6.782 s; the actual adaptive
request selected peer 2 in 0.516 s. The 8 MiB main transfer took 7.860 s, both
worker peaks were 20, trace completion and worker error gates passed, and both
independent audits passed. `report.json` has SHA-256
`1F1E9DAAA1B075A5A629A21A3013E6823CDAD579935DC1A33E801B79B991A7D1` and
`evidence.json` has SHA-256
`CDE524F96BCDB0A74EF7D22A38F1C73CD89DD43CBF3A8D5C331BB6BFAAD5589D`;
the report and evidence audit hashes are
`3B8FC7D39091445908057B713B2DC7F7146811D5E4B0EF956BDF75568E0034E1` and
`BB06B9DE2FA65DEE3ECA512E97CE8C5266EDAFF9385FF600B87F58FABA9865ED`.

The independent r26 repeat at
`.codex-tmp/peer-transit-acceptance-r26-real-impairment-repeat` again attempted
342 datagram sends and dropped 61 over the full 250-350 ms range. Its impaired
direct file took 6.797 s, adaptive transit took 0.578 s, the main transfer took
7.828 s, all 20 one-MiB sessions completed in 27.156 s, relay and target peaks
were both 20, the trace was complete, both worker error counts were zero and
peak traced memory was 5,499,623 bytes. Its `report.json` has SHA-256
`BA8FF94533F31920DD311A6BC1CAB9CBDF1D59DF4D0D2F2A99B3C6F82656D3A7` and
`evidence.json` has SHA-256
`DD4573CA05367D3B0B0762088E90C3D7DA1C049CDD994927BB5E505C8C9799A4`;
the report and evidence audit hashes are
`7AF21E502E28464A461915F17DA217F2AF7D800373F2E49273137B9FFA5638FD`
and `A5E6B097270B490750503A6C3EFFD44BC20D31F31142E13844CE51CF36E3B64C`.

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
