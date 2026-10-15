# Native P2P transport (hole punching + QUIC in Rust)

Status: prototype, negotiated per connection, aioice/aioquic remains the
fallback. Crate: `native/ryn_transport` (Python module `ryn_transport`).
Python integration: `rynmesh/native_transport.py`.

## Why

Per-packet work in Python was the ceiling of the internet path. Measured on the
project's Windows VM (4 vCPU), same machine, loopback:

| data plane | throughput |
| --- | --- |
| aioquic (pure Python QUIC), tuned | 1-3 MiB/s |
| `ryn_transport` from Python (quinn) | 133 MiB/s |
| device drop end to end, aioice + aioquic | 1.6 MiB/s |
| device drop end to end, native, two processes | 35-46 MiB/s (receiver overlaps network reads with disk writes and fsyncs in the background; now bound by this VM's disk, which measured 38-165 MiB/s between runs) |

## What runs where

```
Python (unchanged roles)                      Rust (ryn_transport, off the GIL)
- registry signaling (signed work orders)     - UDP socket, 8 MiB buffers
- trust: who may connect                      - STUN: public mapping (parallel servers)
- drop protocol / AI task envelopes           - hole punching probes (HMAC)
- per-stream request/response framing        - QUIC (quinn): TLS 1.3, streams,
                                                congestion control, pacing, PMTUD
```

### Connection setup

1. Each side calls `open_session(role)`: binds one dual-stack UDP socket
   (`[::]`, IPv4 via v4-mapped addresses; IPv4-only where IPv6 is off), lists host
   candidates and asks the configured STUN servers (in parallel) for the public
   mapping. A server-role session also creates a per-session self-signed
   certificate.
2. `Session.info()` goes to the other side inside the existing signed signaling
   (drop: `params["native"]` / `result_refs["native"]`; AI: `IceSignal.native`).
   It contains a random 16-byte secret, the candidates and, for the server, the
   SHA-256 of its certificate.
3. `Session.connect(remote)`: both sides send probes to every candidate of the
   other side at once (`"RYNP" | type | role | nonce | HMAC-SHA256(key)[..16]`,
   key = SHA-256("ryn-punch-v1" | client secret | server secret)). Outbound
   probes open each NAT; a probe is answered with an ACK. The client takes the
   first address that ACKs, tells the server (SELECT), and both hand the *same*
   socket to quinn, so the NAT mapping the probes created is the one QUIC uses.
4. QUIC handshake: the client accepts only the certificate whose fingerprint
   came in the signed answer, and verifies the TLS 1.3 signature. The server
   accepts only a connection from the address that proved itself.

No TURN/relay is used. Evidence keeps `transport: "ice_udp_direct"`,
`relay_used: false` and adds `native: true`, `data_plane: "quic"`, the
candidate type of the chosen path, punch and handshake times.

## Negotiation and fallback (staged migration)

An offer carries native info **next to** the aioice candidates. The answering
side uses native when it has the extension and the offer is valid, otherwise it
answers with aioice as before. So every mix of old and new devices works:

| dialer | answerer | data plane |
| --- | --- | --- |
| native | native | native QUIC |
| native | without extension / older build | aioice (+ aioquic for drop) |
| without extension | any | aioice |

If a native attempt fails (e.g. a path only aioice's candidate set could
open), that peer gets aioice-only offers for 10 minutes.

Native is not offered for AI sessions when `RYNMESH_P2P_BIND_PORT` is set
(firewall rules name that one port) or in the strict public-NAT acceptance
modes (`RYNMESH_P2P_REQUIRE_PUBLIC*`), whose evidence is defined for aioice.

Rollout stages:

1. **now** — negotiated, aioice fallback, `RYNMESH_P2P_NATIVE=auto` by default
   when the extension is installed; device drop and pooled AI sessions.
2. after real cross-network runs — collect punch success, path kinds and loss
   statistics from evidence; one-shot (non-pooled) AI
   exchanges.
3. aioice/aioquic only as the fallback for devices without the extension;
   then retire the Python datagram framing.

## Congestion control: adaptive (default)

`RYNMESH_P2P_CC` = `adaptive` (default) | `cubic` | `bbr` | `newreno`.
Implementation: `native/ryn_transport/src/cc.rs`.

Loss-based CUBIC treats random loss (Wi-Fi, mobile, long international links)
as congestion; BBR does not, but quinn's BBR (v1 style) overshoots shallow
bottleneck queues. The adaptive controller tells the two apart with the RTT:
congestion fills a queue first (RTT rises), random loss happens at the RTT
floor.

- starts as CUBIC;
- per epoch (max(300 ms, 4 RTT)) it measures loss share, RTT/min RTT and the
  delivery rate;
- 2 of the last 3 epochs with loss >= 0.1 % and RTT/min RTT < 1.3 -> BBR;
- in BBR the window is capped at gain x measured BDP; gain starts at 2,
  x0.75 when loss exceeds max(3 %, 4 x the random loss CUBIC saw), x1.1 per
  clean epoch, within [1.1, 2.5];
- 2 of 3 epochs with RTT/min RTT > 2.5 (standing queue: competing traffic,
  bufferbloat) -> back to CUBIC; at least 1 s between switches;
- a switch keeps the current window (no new slow start);
- `RYN_CC_DEBUG=1` prints every epoch decision to stderr.

Bench: quinn through a shaping proxy (one-way delay, random loss, rate with a
100 ms drop-tail queue), 64 MiB per run (24 MiB for the last row), Windows VM.
MiB/s, and packets lost / sent:

| path | CUBIC | BBR | adaptive |
| --- | --- | --- | --- |
| 100 Mbit/s, 40 ms, 0 % loss | 10.6 (2.5 %) | 11.3 (52 %) | 10.4 (2.6 %), stays CUBIC |
| 100 Mbit/s, 40 ms, 0.5 % | 0.7 | 11.3 (50 %) | 8.1 (0.5 %) |
| 100 Mbit/s, 40 ms, 2 % | 0.2 | 11.1 (48 %) | 6.8 (2 %) |
| 50 Mbit/s, 150 ms, 1 % | 0.1 | 5.0 (44 %) | 2.7 (6 %) |

Averages include the initial CUBIC phase and the ramp; after the switch the
adaptive controller holds ~94 % of the link at the path's own loss rate. These
are single runs on an emulated path, not field measurements.

## Building

```
pip install ./native/ryn_transport        # maturin + cargo (Rust >= 1.77)
```

The sidecar build scripts (`webapp/src-tauri/scripts/build-windows.ps1`,
`build-sidecar.sh`) install it before PyInstaller and pass
`--hidden-import ryn_transport`. Without it the node runs unchanged on aioice.

Rust unit tests: `cargo test --no-default-features` in `native/ryn_transport`.
Python tests: `tests/test_native_transport.py`, and the `[native]` cases of
`tests/test_drop_p2p.py` (skipped when the extension is not installed).

## Not done yet

- IPv6 over real networks: host candidates include global/ULA IPv6 addresses
  and are tried first, but only the `::1` path was testable here (no global
  IPv6 on the VM). STUN asks for IPv4 mappings only.
- Field data: punch success rate and loss on real NATs and networks.
- Non-pooled (one-shot) AI exchanges still use aioice.
- Prebuilt wheels per platform in CI (today the build compiles the crate).
