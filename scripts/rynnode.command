#!/bin/zsh
# Rynnode — double-click this file to start a local development node and UI.
set -uo pipefail

REPO="${0:A:h:h}"
REGISTRY="${RYNMESH_REGISTRY_URL:-}"
export PATH="$HOME/Library/Python/3.13/bin:$PATH"   # legacy PATH entry (rynmesh-vpn now ships with rynmesh)
cd "$REPO"

echo "── Rynnode launcher ──"

# 1. local rynnode (the consumer node the UI drives)
if lsof -nP -iTCP:8791 -sTCP:LISTEN >/dev/null 2>&1; then
  echo "✓ rynnode already running (:8791)"
else
  echo "• starting your rynnode…"
  RYNMESH_HOME="$HOME/.rynmesh/local" RYNMESH_REGISTRY_URL="$REGISTRY" RYNMESH_NETWORK_ID=rynmesh-main \
    RYNMESH_NODE_NAME="${RYNMESH_NODE_NAME:-local-node}" RYNMESH_AUTO_REGISTER="${RYNMESH_AUTO_REGISTER:-0}" RYNMESH_PEER_PORT=8791 \
    nohup python3 -m rynmesh.peer_http >/tmp/rynnode.log 2>&1 &
  sleep 5
  lsof -nP -iTCP:8791 -sTCP:LISTEN >/dev/null 2>&1 && echo "  ✓ rynnode up" || { echo "  ✗ failed — see /tmp/rynnode.log"; }
fi

# 2. the UI (webapp dev server; proxies /api/local to the rynnode)
if lsof -nP -iTCP:5173 -sTCP:LISTEN >/dev/null 2>&1; then
  echo "✓ UI already running (:5173)"
else
  echo "• starting the rynnode UI…"
  ( cd "$REPO/webapp" && nohup npm run dev >/tmp/rynnode-webapp.log 2>&1 & )
  sleep 7
  lsof -nP -iTCP:5173 -sTCP:LISTEN >/dev/null 2>&1 && echo "  ✓ UI up" || echo "  ✗ failed — see /tmp/rynnode-webapp.log"
fi

# 3. open the app
open "http://localhost:5173/"
echo ""
echo "Rynnode is open in your browser."
echo "→ Recommendations are discovered automatically; no registry or preferences are required."
