# P2P peer-transit acceptance matrix

This matrix maps every section of the acceptance contract in
`P2P_PEER_TRANSIT.md` to evidence that can independently prove it. A row is
complete only when its named producer and auditor both pass; a missing field is
not equivalent to a pass. Public-NAT claims additionally require the physical
three-network gate in `P2P_PEER_TRANSIT_RUNBOOK.md`.

| Contract | Required behavior | Authoritative automated evidence | Current status |
|---|---|---|---|
| A. Healthy direct | One non-TURN UDP pair, direct route, matching hash, zero peer-2 bytes | `healthy_direct_file`, `direct`, `_audit_post_recovery_direct_file`; `test_direct_file_path_uses_one_non_turn_ice_pair` | Local gate passed twice in r25/r26 |
| B. Direct blocked | Two non-TURN ICE/UDP legs through peer 2, signed identity continuity, matching byte counters and hash | `main_evidence`, `audit_peer_transit`; `test_two_direct_ice_legs_forward_only_ciphertext_without_turn` | Local gate passed twice in r25/r26; distinct-public-egress proof remains external |
| C. Direct degraded | Real 250-350 ms RTT shaping, 50-100 ms jitter, 15-20% loss, intact retries, adaptive peer-2 selection | `degraded_network`, `_audit_degraded_network_gate`; `test_datagram_impairment_is_deterministic_and_within_contract`, `test_degraded_network_auditor_recomputes_loss_and_binds_real_paths` | r25/r26 each attempted 342 datagrams and dropped 61 (17.84%); both independent audits passed |
| D. Recovery | Hysteresis-controlled return to direct, no flap, peer-2 counters stop | `route`, `healthy_direct_file`, `_audit_route_report`, `_audit_post_recovery_direct_file`; route-manager tests | Local gate passed twice in r25/r26 |
| E. Transit failure | Peer-2 loss produces recovered direct/alternate transit or bounded explicit failure with no partial file | `actual_hard_failure`, `unavailable`, `_audit_unavailable_gate`; adaptive hard-failure tests | Local gate passed twice in r25/r26; hard fallback remained below 1 second |
| F. No cloud relay | No TURN candidate/configuration, strict UDP/IP candidate validation, control-plane blackout survival, body-free registry | `control_plane_blackout`, `registry_control_plane`, candidate-filter tests, `_audit_hop`, `_audit_registry_control_plane` | Local gate passed twice in r25/r26; packet capture across three public egresses remains external |
| G. Confidentiality/integrity | No plaintext at peer 2; tamper, replay, forged identity, expiry and recursive hop rejected; no arbitrary application destination | transit frame/registry scans; signed-session, cipher replay/tamper and result-identity tests | Automated security gates passed; r13 continuous scans remain clean |
| H. Resources/performance | Establishment under 5 s, fallback under 10 s, <=15% protocol overhead, <=128 MiB for 1 GiB, 20 real worker overlaps, leak-free 24 h | `performance`, two worker timelines, `_audit_memory_gate`, `_audit_overhead_gate`, r13 soak and final one-GiB report | Isolated 1 GiB r30 and earlier 20-way gates passed; r13 24-hour duration and the final combined 1-GiB/20-way run are pending |

## Cross-cutting fail-closed checks

- `worker_control_errors` must exist for the main relay and target and contain
  `count=0`, empty `first`, and empty `last`.
- `worker_trace_complete=true` must bind every concurrent signed session ID to
  finished handler intervals at both workers. Caller wait overlap is ignored.
- Every work result is verified against its exact order, network, requester and
  provider before status or ICE signaling is trusted.
- Every acceptance work root is new or empty. Failed roots remain immutable and
  the next attempt uses a new directory.
- The final soak audit scans peer-2 storage, registry data, stdout/stderr and
  partial files, then proves worker threads, the process and UDP endpoints are
  gone.
- Runtime or soak-runner drift, or a changed upstream baseline, invalidates the
  accumulated r13 duration and requires a fresh zero-duration run.

## Evidence fixed points

- Data-plane runtime: `a378bad`.
- Soak runner blob: `1f8fe15de836702619911531d2c24b6e7e802a57`.
- Strengthened real-impairment acceptance/auditor: `700cc99`.
- r25 report: `1F1E9DAAA1B075A5A629A21A3013E6823CDAD579935DC1A33E801B79B991A7D1`.
- r26 report: `BA8FF94533F31920DD311A6BC1CAB9CBDF1D59DF4D0D2F2A99B3C6F82656D3A7`.
- r30 one-GiB report: `C64D64EFF5D76C7D9D14440C4D9A2962503060979B0A2C3547E46A696EB6C220`.
- r30 report audit: `D88AAE8178AF609205E599599B21BEAAC548FEC14D2A7341BAAFAA9EED999F53`.
- Warm OS baseline: `73BF96E6AA16C7350A7888629E55671B7D4D6E4DCCC8E45A178CCC1113EDDDE3`.
- 233-session OS checkpoint: `3AC7BB613DACA73987A0B321A9084CF743F3A25563341D414A42464F0C422657`.
- 471-session OS checkpoint: `09806C0970DAAA93D9B574BA93C1668950F8E09DC177C74CAA81C98DF4C014BC`.

## Completion rule

Local automated acceptance is complete only after the r13 row reaches 86,400
monotonic seconds, the strict soak audit passes after shutdown, and a fresh
one-GiB/20-session report passes both evidence and full-report audits plus all
regression and package gates. Public NAT traversal remains unclaimed until the
three-public-egress physical run supplies nominated srflx/prflx and packet-level
evidence.
