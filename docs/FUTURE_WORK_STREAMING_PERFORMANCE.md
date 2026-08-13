# Future work — fastest-response egress datapath (no freezing while watching TV)

## Goal

Smooth video playback through the net.egress exit: **no rebuffering / freezing**,
fast channel-switch, stable bitrate. This is about the **transport datapath and
path selection**, and is independent of the auth work in
`FUTURE_WORK_EGRESS_MULTITENANT.md` (auth doesn't affect throughput).

## Why it freezes today (root causes)

1. **TCP-over-TCP (biggest).** `ssh -D` forwards each TCP stream *inside* the
   SSH TCP connection. On any packet loss, the outer TCP retransmits while the
   inner TCP *also* retransmits → head-of-line blocking and throughput collapse.
   Video players see this as a stall. This is the #1 cause of freezing.
2. **Extra hop / latency.** Path is `consumer → HK jump → SZ`, not direct. Every
   RTT added hurts adaptive-bitrate ramp-up and seek/switch latency.
3. **Bufferbloat** on the SSH channel — a single fat flow fills queues and
   spikes latency for the player's control traffic.
4. **Box / uplink contention.** Many tenants share one SZ exit's uplink; no QoS
   or per-session fairness today.
5. **No failover.** If the single tunnel drops, playback dies until manual
   reconnect (the `rynnode_egress_watchdog.zsh` script is a first step).

## Directions (highest impact first)

### A. Replace `ssh -D` with a UDP datapath — **WireGuard** ← biggest win
WireGuard tunnels at L3 over **UDP**, so there's no TCP-over-TCP: the player's
TCP runs end-to-end with normal congestion control. Result: dramatically fewer
stalls under loss, lower latency, higher sustained bitrate.
- Provider exposes a WG endpoint; broker hands the consumer a **per-session WG
  peer config** (ties cleanly into the ephemeral-credential design — option #1
  there becomes "return a WG keypair + allowed-ips + endpoint" instead of an SSH
  key).
- Scope routing so only the dedicated Chrome profile's traffic uses the tunnel
  (split tunnel), preserving the "rest of the machine is untouched" property.
- Alternative if WG is impractical: a **QUIC/HTTP-3 or masque proxy** — also
  UDP-based, avoids TCP-over-TCP, and is friendlier to restrictive networks.

### B. Shorten / optimize the path
- Offer **direct consumer → SZ** where reachable (drop the HK hop) and fall back
  to the jump only when needed.
- **Nearest/best-exit selection:** measure RTT + loss to candidate exits at
  connect time and pick the best; expose multiple SZ/CN exits.

### C. Fairness & capacity (provider side)
- Per-session **bandwidth floor + QoS** so one heavy user can't starve others.
- **Multiple exit boxes + load balancing**; advertise live capacity in the
  registry (the capacity endpoint already exists) and broker to the least-loaded.
- Scale-out signal: if `rebuffer ratio` rises fleet-wide, add exits.

### D. Resilience (hide blips from the viewer)
- **Fast auto-reconnect / failover** to a second exit without tearing the Chrome
  session (keep the SOCKS/WG port stable, swap the upstream).
- Health-driven pre-warm: keep a warm standby tunnel so failover is sub-second.

### E. Measure it (so we optimize the right thing)
Define and track per-session QoE metrics:
- **rebuffer ratio** (stall time / watch time) — the primary KPI,
- **startup latency** (click → first frame),
- **channel-switch latency**, sustained **throughput**, tunnel **RTT/loss**.
Instrument the consumer node; surface in the app status card next to uptime.

## Suggested sequencing
1. **Measure** (E) — get rebuffer ratio + startup latency from a real session.
2. **WireGuard datapath** (A) — expected to remove most freezing; pairs with the
   ephemeral-credential work.
3. **Best-exit selection + multi-exit** (B/C).
4. **Seamless failover** (D).

## Non-goals / notes
- Auth model (shared vs per-session key) is **not** a performance lever — see the
  perf section in `FUTURE_WORK_EGRESS_MULTITENANT.md`.
- Split-tunnel scope must be preserved: only the dedicated Chrome profile egresses
  through the tunnel.
