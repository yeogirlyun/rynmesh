#!/bin/sh
# Build the self-contained Ryn node daemon sidecar (macOS arm64) and place it
# where Tauri's externalBin expects it: binaries/rynmesh-peer-<target-triple>.
set -e

ROOT="$(CDPATH= cd -- "$(dirname -- "$0")/../../.." && pwd)"
SRC_TAURI="$ROOT/webapp/src-tauri"
OUT="$SRC_TAURI/binaries"
BUILD="$(mktemp -d)"
trap 'rm -rf "$BUILD"' EXIT

TRIPLE="$("$HOME/.cargo/bin/rustc" -Vv | sed -n 's/^host: //p')"
[ -n "$TRIPLE" ] || { echo "could not resolve rust host triple" >&2; exit 1; }

python3 -m venv "$BUILD/venv"
. "$BUILD/venv/bin/activate"
pip install -q --upgrade pip
pip install -q "$ROOT" pyinstaller

mkdir -p "$OUT"
pyinstaller --onefile --noconfirm --clean \
  --name rynmesh-peer \
  --collect-submodules uvicorn \
  --collect-submodules rynmesh \
  --collect-submodules anyio \
  --hidden-import uvicorn.lifespan.on \
  --hidden-import uvicorn.loops.asyncio \
  --hidden-import uvicorn.protocols.http.h11_impl \
  --distpath "$BUILD/dist" --workpath "$BUILD/work" --specpath "$BUILD" \
  "$SRC_TAURI/sidecar/rynmesh_peer_entry.py"

cp "$BUILD/dist/rynmesh-peer" "$OUT/rynmesh-peer-$TRIPLE"
chmod +x "$OUT/rynmesh-peer-$TRIPLE"
echo "SIDECAR_BUILT $OUT/rynmesh-peer-$TRIPLE"
