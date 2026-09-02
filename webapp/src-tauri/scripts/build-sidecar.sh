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

PYTHON="${PYTHON:-python3}"
"$PYTHON" -m venv "$BUILD/venv"
. "$BUILD/venv/bin/activate"
pip install -q --upgrade pip
pip install -q "$ROOT" pyinstaller

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
  --add-data "$WEBUI:rynmesh/webui" \
  --hidden-import uvicorn.lifespan.on \
  --hidden-import uvicorn.loops.asyncio \
  --hidden-import uvicorn.protocols.http.h11_impl \
  --distpath "$BUILD/dist" --workpath "$BUILD/work" --specpath "$BUILD" \
  "$SRC_TAURI/sidecar/rynmesh_peer_entry.py"

cp "$BUILD/dist/rynmesh-peer" "$OUT/rynmesh-peer-$TRIPLE"
chmod +x "$OUT/rynmesh-peer-$TRIPLE"
echo "SIDECAR_BUILT $OUT/rynmesh-peer-$TRIPLE"
