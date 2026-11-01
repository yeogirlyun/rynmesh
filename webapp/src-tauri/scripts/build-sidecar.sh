#!/bin/sh
# Build the self-contained Ryn node daemon sidecar for the current Rust host and
# place it where Tauri's externalBin expects it:
# binaries/rynmesh-peer-<target-triple>.
set -eu

ROOT="$(CDPATH= cd -- "$(dirname -- "$0")/../../.." && pwd)"
SRC_TAURI="$ROOT/webapp/src-tauri"
OUT="$SRC_TAURI/binaries"
WEBUI="$ROOT/webapp/dist"
BUILD="$(mktemp -d)"
trap 'rm -rf "$BUILD"' EXIT

[ -f "$WEBUI/index.html" ] || {
  echo "production webapp is missing; run 'cd webapp && npm run build' first" >&2
  exit 1
}

RUSTC="$(command -v rustc || true)"
[ -n "$RUSTC" ] || { echo "rustc is not available on PATH" >&2; exit 1; }
TRIPLE="$("$RUSTC" -Vv | sed -n 's/^host: //p')"
[ -n "$TRIPLE" ] || { echo "could not resolve rust host triple" >&2; exit 1; }

"${RYNMESH_BUILD_PYTHON:-python3}" -m venv "$BUILD/venv"
. "$BUILD/venv/bin/activate"
pip install -q --upgrade pip
# Source archives can carry older mtimes than a previous setuptools build/lib
# cache. Refresh mtimes so build_py copies the current source, then verify it.
find "$ROOT/rynmesh" -type f -name '*.py' -exec touch {} +
pip install -q "${ROOT}[documents]" 'pyinstaller==6.22.3'
# Native P2P transport (hole punching + QUIC in Rust); maturin builds it with cargo.
PATH="$HOME/.cargo/bin:$PATH" pip install -q "$ROOT/native/ryn_transport"
python -c 'import ryn_transport, rynmesh.native_transport as n; assert n.enabled()'
# Fail before freezing if the selected interpreter cannot load the application.
python - "$ROOT" <<'PY'
from pathlib import Path
import sys
import rynmesh
import rynmesh.peer_http
import pypdf

source = Path(sys.argv[1]) / "rynmesh"
installed = Path(rynmesh.__file__).resolve().parent
for path in source.rglob("*.py"):
    counterpart = installed / path.relative_to(source)
    if not counterpart.is_file() or path.read_bytes() != counterpart.read_bytes():
        raise SystemExit(f"stale installed source: {path.relative_to(source)}")
PY

mkdir -p "$OUT"
set -- pyinstaller --onefile --noconfirm --clean
if [ "$(uname -s)" = "Darwin" ]; then
  set -- "$@" \
    --codesign-identity - \
    --osx-entitlements-file "$SRC_TAURI/sidecar/rynmesh-peer.entitlements.plist"
fi

"$@" \
  --name rynmesh-peer \
  --collect-submodules uvicorn \
  --collect-submodules rynmesh \
  --collect-submodules anyio \
  --collect-submodules aioquic \
  --collect-submodules pypdf \
  --hidden-import ifaddr \
  --hidden-import ryn_transport \
  --hidden-import rynmesh.agent_runtime.extensions.computer_use_server \
  --collect-data rynmesh --copy-metadata rynmesh \
  --collect-data jsonschema_specifications \
  --add-data "$WEBUI:rynmesh/webui" \
  --hidden-import uvicorn.lifespan.on \
  --hidden-import uvicorn.loops.asyncio \
  --hidden-import uvicorn.protocols.http.h11_impl \
  --distpath "$BUILD/dist" --workpath "$BUILD/work" --specpath "$BUILD" \
  "$SRC_TAURI/sidecar/rynmesh_peer_entry.py"

cp "$BUILD/dist/rynmesh-peer" "$OUT/rynmesh-peer-$TRIPLE"
chmod +x "$OUT/rynmesh-peer-$TRIPLE"
echo "SIDECAR_BUILT $OUT/rynmesh-peer-$TRIPLE"
