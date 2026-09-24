#!/bin/sh
# Prove that a frozen Ryn node can extract its runtime and serve requests.
set -eu

SIDECAR="${1:-}"
[ -x "$SIDECAR" ] || { echo "sidecar is not executable: $SIDECAR" >&2; exit 1; }

VERIFY_DIR="$(mktemp -d)"
VERIFY_PORT="${RYNMESH_VERIFY_PORT:-18791}"
DAEMON_PID=""
VERIFY_CODEX=0

cleanup() {
  if [ -n "$DAEMON_PID" ]; then
    kill "$DAEMON_PID" 2>/dev/null || true
    wait "$DAEMON_PID" 2>/dev/null || true
  fi
  rm -rf "$VERIFY_DIR"
}
trap cleanup EXIT HUP INT TERM

# Exercise discovery inside the frozen daemon, with the minimal PATH inherited
# by GUI launches. Use a disposable HOME, never the build user's CLI credentials.
if [ "$(uname -s)" = Darwin ]; then
  VERIFY_CODEX=1
  CODEX_FIXTURE="$VERIFY_DIR/home/.local/bin/codex"
  mkdir -p "$(dirname "$CODEX_FIXTURE")"
  printf '#!/bin/sh\nexit 0\n' > "$CODEX_FIXTURE"
  chmod +x "$CODEX_FIXTURE"
fi

HOME="$VERIFY_DIR/home" \
PATH="/usr/bin:/bin:/usr/sbin:/sbin" \
RYNMESH_HOME="$VERIFY_DIR/node" \
RYNMESH_NETWORK_DIR="$VERIFY_DIR/mesh" \
RYNMESH_DESKTOP_MODE=1 \
RYNMESH_PEER_HOST=127.0.0.1 \
RYNMESH_PEER_PORT="$VERIFY_PORT" \
RYNMESH_AUTO_REGISTER=0 \
  "$SIDECAR" >"$VERIFY_DIR/daemon.log" 2>&1 &
DAEMON_PID=$!

attempt=0
while [ "$attempt" -lt 120 ]; do
  if curl -fsS "http://127.0.0.1:$VERIFY_PORT/health" >"$VERIFY_DIR/health.json" 2>/dev/null && \
      grep -q 'peer_id' "$VERIFY_DIR/health.json"; then
    echo "SIDECAR_HEALTHY $(cat "$VERIFY_DIR/health.json")"
    if [ "$VERIFY_CODEX" = 1 ]; then
      curl -fsS "http://127.0.0.1:$VERIFY_PORT/api/local/llm/cli-services" > "$VERIFY_DIR/cli.json"
      python3 - "$VERIFY_DIR/cli.json" <<'PY'
import json
import sys
with open(sys.argv[1]) as source:
    services = json.load(source)["services"]
assert any(s["kind"] == "codex_cli" and s["installed"] for s in services), "Packaged node did not detect Codex without shell PATH"
print("PACKAGED_CODEX_DISCOVERY_OK (fixture; not an account/login test)")
PY
    fi
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
