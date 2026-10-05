#!/bin/sh
# Prove that a frozen Ryn node can extract its runtime and serve requests.
# With --check-llm it also asserts the node sees a native inference runtime,
# which is how CI proves the bundled llama.cpp reaches the daemon.
set -eu

SIDECAR=""
CHECK_LLM=0
for arg in "$@"; do
  case "$arg" in
    --check-llm) CHECK_LLM=1 ;;
    -*) echo "unknown argument: $arg" >&2; exit 1 ;;
    *) [ -n "$SIDECAR" ] || SIDECAR="$arg" ;;
  esac
done
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

healthy=0
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
    healthy=1
    break
  fi
  if ! kill -0 "$DAEMON_PID" 2>/dev/null; then
    break
  fi
  attempt=$((attempt + 1))
  sleep 0.25
done

if [ "$healthy" -eq 0 ]; then
  cat "$VERIFY_DIR/daemon.log" >&2
  echo "sidecar did not become healthy on port $VERIFY_PORT" >&2
  exit 1
fi
echo "SIDECAR_HEALTHY $(cat "$VERIFY_DIR/health.json")"

if [ "$CHECK_LLM" -eq 1 ]; then
  if ! curl -fsS "http://127.0.0.1:$VERIFY_PORT/api/local/llm/hardware" \
      >"$VERIFY_DIR/hardware.json" 2>/dev/null; then
    cat "$VERIFY_DIR/daemon.log" >&2
    echo "sidecar did not answer the hardware probe" >&2
    exit 1
  fi
  # `native_runtime_present`, not `native_runtime_available`: the latter is true
  # wherever the pinned release could be downloaded, so it would pass even with
  # an empty resources/llama.
  if ! grep -q '"native_runtime_present":[[:space:]]*true' "$VERIFY_DIR/hardware.json"; then
    cat "$VERIFY_DIR/hardware.json" >&2
    echo "node did not resolve a native inference runtime" >&2
    exit 1
  fi
  echo "SIDECAR_NATIVE_RUNTIME_OK"
fi
