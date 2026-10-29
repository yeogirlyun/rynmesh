#!/usr/bin/env bash
# Install-time smoke test for a packaged Linux desktop application. It proves
# that the shell starts its frozen sidecar, serves the production UI, recovers
# the daemon after a crash, writes XDG logs, and cleans up the managed child.
set -euo pipefail

DESKTOP_FILE="${1:-}"
[ -f "$DESKTOP_FILE" ] || { echo "desktop entry not found: $DESKTOP_FILE" >&2; exit 1; }
APP="$(sed -n 's/^Exec=\([^ %]*\).*/\1/p' "$DESKTOP_FILE" | head -n 1)"
[ -n "$APP" ] || { echo "desktop entry has no Exec command" >&2; exit 1; }
if [[ "$APP" != /* ]]; then
  APP="$(command -v "$APP")"
fi
[ -x "$APP" ] || { echo "desktop executable not found: $APP" >&2; exit 1; }

SMOKE_ROOT="$(mktemp -d)"
PORT="${RYNMESH_VERIFY_PORT:-18792}"
WRAPPER_PID=""
APP_PID=""

cleanup() {
  if [ -n "$APP_PID" ]; then
    kill -TERM "$APP_PID" 2>/dev/null || true
  fi
  if [ -n "$WRAPPER_PID" ]; then
    kill -TERM "$WRAPPER_PID" 2>/dev/null || true
    wait "$WRAPPER_PID" 2>/dev/null || true
  fi
  rm -rf "$SMOKE_ROOT"
}
trap cleanup EXIT HUP INT TERM
mkdir -p "$SMOKE_ROOT/home" "$SMOKE_ROOT/state"

env -u PYTHONPATH -u RYNMESH_REPO_DIR \
HOME="$SMOKE_ROOT/home" \
XDG_STATE_HOME="$SMOKE_ROOT/state" \
RYNMESH_HOME="$SMOKE_ROOT/node" \
RYNMESH_NETWORK_DIR="$SMOKE_ROOT/mesh" \
RYNMESH_PEER_HOST=127.0.0.1 \
RYNMESH_PEER_PUBLIC_HOST=127.0.0.1 \
RYNMESH_PEER_ENDPOINT="http://127.0.0.1:$PORT" \
RYNMESH_PEER_PORT="$PORT" \
RYNMESH_AUTO_REGISTER=0 \
RYNMESH_REGISTRY_URL=http://127.0.0.1:9 \
RYNMESH_RELAY_URL=http://127.0.0.1:9 \
  xvfb-run -a "$APP" >"$SMOKE_ROOT/desktop.log" 2>&1 &
WRAPPER_PID=$!

for _ in $(seq 1 120); do
  if curl -fsS "http://127.0.0.1:$PORT/health" >"$SMOKE_ROOT/health.json" 2>/dev/null &&
      grep -q '"desktop_managed":true' "$SMOKE_ROOT/health.json"; then
    break
  fi
  kill -0 "$WRAPPER_PID" 2>/dev/null || {
    cat "$SMOKE_ROOT/desktop.log" >&2
    echo "desktop shell exited before its node became healthy" >&2
    exit 1
  }
  sleep 0.25
done
grep -q '"desktop_managed":true' "$SMOKE_ROOT/health.json"
curl -fsS "http://127.0.0.1:$PORT/" | grep -Eqi '<!doctype html|<html'
test "$(curl -sS -o /dev/null -w '%{http_code}' "http://127.0.0.1:$PORT/digest")" = 200

APP_PID="$(pgrep -P "$WRAPPER_PID" -f "$APP" | head -n 1 || true)"
if [ -z "$APP_PID" ]; then
  APP_PID="$(pgrep -f "^$APP( |$)" | head -n 1 || true)"
fi
[ -n "$APP_PID" ] || { echo "could not identify desktop process" >&2; exit 1; }
OLD_NODE="$(pgrep -P "$APP_PID" -f 'rynmesh-peer' | head -n 1 || true)"
[ -n "$OLD_NODE" ] || { echo "could not identify managed sidecar" >&2; exit 1; }
kill -KILL "$OLD_NODE"

NEW_NODE=""
for _ in $(seq 1 100); do
  NEW_NODE="$(pgrep -P "$APP_PID" -f 'rynmesh-peer' | head -n 1 || true)"
  if [ -n "$NEW_NODE" ] && [ "$NEW_NODE" != "$OLD_NODE" ] &&
      curl -fsS "http://127.0.0.1:$PORT/health" 2>/dev/null | grep -q '"desktop_managed":true'; then
    break
  fi
  sleep 0.25
done
[ -n "$NEW_NODE" ] && [ "$NEW_NODE" != "$OLD_NODE" ] || {
  echo "managed sidecar did not restart" >&2
  exit 1
}
[ -f "$SMOKE_ROOT/state/rynmesh/ryn-node.log" ] || {
  echo "desktop log was not written beneath XDG_STATE_HOME" >&2
  exit 1
}

kill -TERM "$APP_PID"
for _ in $(seq 1 40); do
  kill -0 "$APP_PID" 2>/dev/null || break
  sleep 0.25
done
if kill -0 "$APP_PID" 2>/dev/null; then
  echo "desktop shell did not exit after SIGTERM" >&2
  exit 1
fi
APP_PID=""
sleep 0.5
if kill -0 "$NEW_NODE" 2>/dev/null; then
  echo "managed sidecar remained after desktop exit" >&2
  exit 1
fi

echo "LINUX_DESKTOP_SMOKE_OK health=$(cat "$SMOKE_ROOT/health.json") old_node=$OLD_NODE new_node=$NEW_NODE"
