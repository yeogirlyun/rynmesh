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
| G. Confidentiality/integrity | No plaintext at peer 2; tamper, replay, forged identity, expiry and recursive hop rejected; no arbitrary application destination | transit frame/registry scans; signed-session, cipher replay/tamper and result-identity tests | Automated security gates passed; invalidated r13 scans remained clean through its stop point |
| H. Resources/performance | Establishment under 5 s, fallback under 10 s, <=15% protocol overhead, <=128 MiB for 1 GiB, 20 real worker overlaps, leak-free 24 h | `performance`, two worker timelines, `_audit_memory_gate`, `_audit_overhead_gate`, replacement soak and final one-GiB report | New-runtime 1 GiB + 20-way r33 passed; replacement r14 soak and post-soak repeat are pending |

## Cross-cutting fail-closed checks

- `worker_control_errors` must exist for the main relay and target and contain
  `count=0`, empty `first`, and empty `last`.
- `worker_trace_complete=true` must bind every concurrent signed session ID to
  finished handler intervals at both workers. Caller wait overlap is ignored.
- Every work result is verified against its exact order, network, requester and
  provider before status or ICE signaling is trusted.
- `verified_chunk_resume` must prove a forced connection loss after a positive
  checkpoint, continuation on fresh signed session IDs from exact continuous
  boundaries, one hash-matching final file, and zero remaining `.part` or
  `.resume.json` state. Duplicate delivery must be idempotent.
- Every acceptance work root is new or empty. Failed roots remain immutable and
  the next attempt uses a new directory.
- The final soak audit scans peer-2 storage, registry data, stdout/stderr and
  partial files, then proves worker threads, the process and UDP endpoints are
  gone.
- Runtime or soak-runner drift, or a changed upstream baseline, invalidates any
  accumulated soak duration and requires a fresh zero-duration run.

## Evidence fixed points

- Data-plane runtime with verified-boundary resume: `ded37d2`.
- Soak runner blob: `1f8fe15de836702619911531d2c24b6e7e802a57`.
- Strengthened real-impairment acceptance/auditor: `700cc99`.
- r25 report: `1F1E9DAAA1B075A5A629A21A3013E6823CDAD579935DC1A33E801B79B991A7D1`.
- r26 report: `BA8FF94533F31920DD311A6BC1CAB9CBDF1D59DF4D0D2F2A99B3C6F82656D3A7`.
- r30 one-GiB report: `C64D64EFF5D76C7D9D14440C4D9A2962503060979B0A2C3547E46A696EB6C220`.
- r30 report audit: `D88AAE8178AF609205E599599B21BEAAC548FEC14D2A7341BAAFAA9EED999F53`.
- Invalidated r13 stop snapshot: `6BDD8C7D2E46C8A33F888FB6F9A6754B93BF93928B9E7F3CF746B8C36DDC0A28`.
- r31 resume-preflight report: `7150EFB72F689B7D88B313BC261A8D9D8D2B4735C2DBBBDA85C364EA75A581C9`.
- r31 resume-preflight report audit: `E95C10A9C92E03019CB4F2E8DDDF08611D68DD3AE31005A53D87C8FC8F7C28CC`.
- r32 default multi-segment report: `25B997A0DEFFDD2E9604B7468F132A52FB564DB7CBC081BC20585BDF1543EF67`.
- r32 default multi-segment report audit: `173584310D273EA981D74D77F8FB22801FC43D59208EBA6C81A4FC40FC925AE7`.
- r33 one-GiB/20-way report: `72AE22BB25431E70CBD741BFBDE0D5DA6864F1BA1FA60E83BE2B52FBC738FEC4`.
- r33 main evidence: `C460B4D5A732CA4023CE94C1EE4CA671F5E7A9C2CC4B6D05D01554F9C0C9625B`.
- r33 report audit: `44391ADA90B94383FB2358977CDD6788EE10E0C494DA789F844FDDC0C08CE328`.
- r33 evidence audit: `ADDF37071EA398EF7B8AFC12F27A9FEBEBE369637FBF7DC4C12AE5C67570EA84`.
- r14 replacement soak: started from zero at 2026-09-02 11:42:10
  Asia/Hong_Kong; runtime and runner fixed points above remain mandatory.
- r14 warm idle OS baseline: `F37A820FB0AFEAFB33D6839DD664B1881AD03C2CFB923C614662144859CAA606`.
- Warm OS baseline: `73BF96E6AA16C7350A7888629E55671B7D4D6E4DCCC8E45A178CCC1113EDDDE3`.
- 233-session OS checkpoint: `3AC7BB613DACA73987A0B321A9084CF743F3A25563341D414A42464F0C422657`.
- 471-session OS checkpoint: `09806C0970DAAA93D9B574BA93C1668950F8E09DC177C74CAA81C98DF4C014BC`.

## Completion rule

Local automated acceptance is complete only after a replacement soak on the
verified-resume runtime reaches 86,400 monotonic seconds, the strict soak audit
passes after shutdown, and a fresh
one-GiB/20-session report passes both evidence and full-report audits plus all
regression and package gates. Public NAT traversal remains unclaimed until the
three-public-egress physical run supplies nominated srflx/prflx and packet-level
evidence.
