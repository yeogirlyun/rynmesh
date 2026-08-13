#!/bin/zsh
set -euo pipefail

export PATH="/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"

REPO_DIR="${0:A:h:h}"
WEBAPP_DIR="$REPO_DIR/webapp"
LOG_DIR="$HOME/Library/Logs/Rynmesh"
WEBAPP_URL="http://127.0.0.1:5173/"
PEER_URL="http://127.0.0.1:8791/health"
REGISTRY_PORT="${RYNMESH_REGISTRY_PORT:-8790}"
PEER_PORT="${RYNMESH_PEER_PORT:-8791}"
NETWORK_ID="${RYNMESH_NETWORK_ID:-rynmesh-main}"
REGISTRY_URL="${RYNMESH_REGISTRY_URL:-https://registry.rynmesh.ai}"
PYTHON_BIN="${RYNMESH_PYTHON:-python3}"

mkdir -p "$LOG_DIR"

if [[ ! -d "$REPO_DIR" ]]; then
  /usr/bin/osascript -e 'display dialog "The Rynmesh repository could not be resolved from the launcher path." buttons {"OK"} default button "OK" with icon caution'
  exit 1
fi

if [[ ! -d "$WEBAPP_DIR/node_modules" ]]; then
  /usr/bin/osascript -e 'display dialog "Ryn Webapp dependencies are not installed. Run npm install inside the checkout webapp directory, then relaunch." buttons {"OK"} default button "OK" with icon caution'
  exit 1
fi

machine_name="$(/usr/sbin/scutil --get ComputerName 2>/dev/null || /bin/hostname -s)"
lan_interface="$(/sbin/route -n get default 2>/dev/null | /usr/bin/awk '/interface:/{print $2; exit}')"
lan_ip=""
if [[ -n "$lan_interface" ]]; then
  lan_ip="$(/usr/sbin/ipconfig getifaddr "$lan_interface" 2>/dev/null || true)"
fi
if [[ -z "$lan_ip" ]]; then
  lan_ip="127.0.0.1"
fi

export PYTHONPATH="$REPO_DIR"
export RYNMESH_DESKTOP_MODE="${RYNMESH_DESKTOP_MODE:-1}"
export RYNMESH_NODE_NAME="${RYNMESH_NODE_NAME:-$machine_name}"
export RYNMESH_MACHINE_NAME="${RYNMESH_MACHINE_NAME:-$machine_name}"
export RYNMESH_MACHINE_IP="${RYNMESH_MACHINE_IP:-$lan_ip}"
export RYNMESH_NETWORK_ID="$NETWORK_ID"
export RYNMESH_PEER_HOST="${RYNMESH_PEER_HOST:-0.0.0.0}"
export RYNMESH_PEER_PORT="$PEER_PORT"
export RYNMESH_PEER_PUBLIC_HOST="${RYNMESH_PEER_PUBLIC_HOST:-$lan_ip}"
export RYNMESH_PEER_ENDPOINT="${RYNMESH_PEER_ENDPOINT:-http://$lan_ip:$PEER_PORT}"
export RYNMESH_AUTO_REGISTER="${RYNMESH_AUTO_REGISTER:-1}"
export RYNMESH_REGISTRY_URL="$REGISTRY_URL"
export RYNMESH_RELAY_URL="${RYNMESH_RELAY_URL:-$REGISTRY_URL}"

if [[ "${RYNMESH_START_LOCAL_REGISTRY:-}" == "1" ]]; then
  export RYNMESH_REGISTRY_HOST="${RYNMESH_REGISTRY_HOST:-0.0.0.0}"
  export RYNMESH_REGISTRY_PORT="$REGISTRY_PORT"
  if ! /usr/bin/curl -fsS --max-time 1 "http://127.0.0.1:$REGISTRY_PORT/health" >/dev/null 2>&1; then
    cd "$REPO_DIR"
    nohup "$PYTHON_BIN" -c 'from rynmesh.registry_http import main; raise SystemExit(main())' >> "$LOG_DIR/ryn-registry.log" 2>&1 &!
  fi
  export RYNMESH_REGISTRY_URL="http://$lan_ip:$REGISTRY_PORT"
fi

if ! /usr/bin/curl -fsS --max-time 1 "$PEER_URL" >/dev/null 2>&1; then
  cd "$REPO_DIR"
  nohup "$PYTHON_BIN" -c 'from rynmesh.peer_http import main; raise SystemExit(main())' >> "$LOG_DIR/ryn-node.log" 2>&1 &!
fi

for _ in {1..80}; do
  if /usr/bin/curl -fsS --max-time 1 "$PEER_URL" >/dev/null 2>&1; then
    break
  fi
  sleep 0.15
done

if ! /usr/bin/curl -fsS --max-time 1 "$WEBAPP_URL" >/dev/null 2>&1; then
  cd "$WEBAPP_DIR"
  BROWSER=none nohup npm run dev -- --host 127.0.0.1 --port 5173 --strictPort >> "$LOG_DIR/ryn-webapp.log" 2>&1 &!
fi

for _ in {1..80}; do
  if /usr/bin/curl -fsS --max-time 1 "$WEBAPP_URL" >/dev/null 2>&1; then
    /usr/bin/open "$WEBAPP_URL"
    if [[ "${RYNMESH_LAUNCH_ONCE:-}" == "1" ]]; then
      exit 0
    fi
    while true; do
      sleep 3600
    done
  fi
  sleep 0.15
done

/usr/bin/open "$LOG_DIR"
/usr/bin/osascript -e 'display dialog "Ryn Webapp did not start. The Rynmesh log folder has been opened." buttons {"OK"} default button "OK" with icon caution'
exit 1
