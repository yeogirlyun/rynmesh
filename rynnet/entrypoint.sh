#!/bin/sh
# Transparent shaper + exec the UNMODIFIED node.
#
# Transparency contract: this script touches only the kernel qdisc on eth0
# (out of the node's process), then `exec`s the stock rynmesh console script
# so PID 1 *is* the unmodified node. The node uses real sockets/DNS/HTTP and
# cannot observe that a netem qdisc exists. Live faults (partition, netem
# change, NAT) are applied by the orchestrator via `docker exec` — also out
# of the node's process.
set -e

IFACE="${RYNNET_IFACE:-eth0}"

# RYNNET_NETEM example: "delay 50ms 10ms distribution normal loss 1% rate 10mbit"
if [ -n "${RYNNET_NETEM:-}" ]; then
  # Best-effort: shaping must never prevent the node from starting.
  tc qdisc add dev "$IFACE" root netem ${RYNNET_NETEM} 2>/dev/null \
    || tc qdisc replace dev "$IFACE" root netem ${RYNNET_NETEM} 2>/dev/null \
    || echo "rynnet: WARN could not apply netem '${RYNNET_NETEM}' on ${IFACE}" >&2
fi

case "${RYNNET_ROLE:-peer}" in
  registry) exec rynmesh-registry ;;
  peer)     exec rynmesh-peer ;;
  *)        echo "rynnet: unknown RYNNET_ROLE='${RYNNET_ROLE}'" >&2; exit 64 ;;
esac
