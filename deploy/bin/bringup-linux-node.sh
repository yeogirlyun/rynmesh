#!/usr/bin/env bash
set -euo pipefail
# Install + enable a rynmesh systemd node on Linux (HK or SZ). Run with sudo.
# Usage: sudo ./bringup-linux-node.sh <hk|sz> <filled-peer-env-file> [filled-tunnel-env-file]
ROLE="${1:?usage: bringup-linux-node.sh <hk|sz> <peer-env-file> [tunnel-env-file]}"
[[ "$ROLE" == "hk" || "$ROLE" == "sz" ]] || { echo "ROLE must be 'hk' or 'sz' (got '$ROLE')"; exit 1; }
PEER_ENV="${2:?provide the filled peer env file}"
TUNNEL_ENV="${3:-}"
HERE="$(cd "$(dirname "$0")" && pwd)"

install -d -m 755 /etc/rynmesh
install -m 600 "$PEER_ENV" /etc/rynmesh/rynmesh-peer.env
install -m 644 "$HERE/../systemd/rynmesh-peer.service" /etc/systemd/system/rynmesh-peer.service
systemctl daemon-reload
systemctl enable --now rynmesh-peer.service

if [ "$ROLE" = "sz" ]; then
  : "${TUNNEL_ENV:?sz role requires a filled tunnel env file as the 3rd argument}"
  install -m 600 "$TUNNEL_ENV" /etc/rynmesh/rynmesh-sz-tunnel.env
  install -m 644 "$HERE/../systemd/rynmesh-sz-tunnel.service" /etc/systemd/system/rynmesh-sz-tunnel.service
  systemctl daemon-reload
  systemctl enable --now rynmesh-sz-tunnel.service
fi

{ systemctl --no-pager status rynmesh-peer.service || true; } | head -n 5 || true
