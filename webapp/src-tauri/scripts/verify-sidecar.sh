#!/bin/sh
# Prove that a frozen Ryn node can extract its runtime and serve requests.
set -eu

SIDECAR="${1:-}"
[ -x "$SIDECAR" ] || { echo "sidecar is not executable: $SIDECAR" >&2; exit 1; }

VERIFY_DIR="$(mktemp -d)"
VERIFY_PORT="${RYNMESH_VERIFY_PORT:-18791}"
DAEMON_PID=""

cleanup() {
  if [ -n "$DAEMON_PID" ]; then
    kill "$DAEMON_PID" 2>/dev/null || true
    wait "$DAEMON_PID" 2>/dev/null || true
  fi
  rm -rf "$VERIFY_DIR"
}
trap cleanup EXIT HUP INT TERM

env -u PYTHONPATH -u RYNMESH_REPO_DIR \
RYNMESH_HOME="$VERIFY_DIR/node" \
RYNMESH_NETWORK_DIR="$VERIFY_DIR/mesh" \
RYNMESH_DESKTOP_MODE=1 \
RYNMESH_PEER_HOST=127.0.0.1 \
RYNMESH_PEER_PORT="$VERIFY_PORT" \
RYNMESH_AUTO_REGISTER=0 \
RYNMESH_REGISTRY_URL=http://127.0.0.1:9 \
RYNMESH_RELAY_URL=http://127.0.0.1:9 \
  "$SIDECAR" >"$VERIFY_DIR/daemon.log" 2>&1 &
DAEMON_PID=$!

attempt=0
while [ "$attempt" -lt 120 ]; do
  if curl -fsS "http://127.0.0.1:$VERIFY_PORT/health" >"$VERIFY_DIR/health.json" 2>/dev/null && \
      grep -q 'peer_id' "$VERIFY_DIR/health.json" && \
      grep -q '"desktop_managed":true' "$VERIFY_DIR/health.json"; then
    curl -fsS "http://127.0.0.1:$VERIFY_PORT/" | grep -Eqi '<!doctype html|<html'
    [ "$(curl -sS -o /dev/null -w '%{http_code}' "http://127.0.0.1:$VERIFY_PORT/digest")" = 200 ]
    echo "SIDECAR_HEALTHY $(cat "$VERIFY_DIR/health.json")"
    exit 0
  fi
  if ! kill -0 "$DAEMON_PID" 2>/dev/null; then
    break
  fi
  attempt=$((attempt + 1))
  sleep 0.25
done

cat "$VERIFY_DIR/daemon.log" >&2
echo "sidecar did not become healthy on port $VERIFY_PORT" >&2
exit 1
