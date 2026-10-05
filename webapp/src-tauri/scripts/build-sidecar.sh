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

"${RYNMESH_BUILD_PYTHON:-python3}" -m venv "$BUILD/venv"
. "$BUILD/venv/bin/activate"
pip install -q --upgrade pip
# Source archives can carry older mtimes than a previous setuptools build/lib
# cache. Refresh mtimes so build_py copies the current source, then verify it.
find "$ROOT/rynmesh" -type f -name '*.py' -exec touch {} +
pip install -q "$ROOT" 'pyinstaller==6.22.3'
# Fail before freezing if the selected interpreter cannot load the application.
python - "$ROOT" <<'PY'
from pathlib import Path
import sys
import rynmesh
import rynmesh.peer_http

source = Path(sys.argv[1]) / "rynmesh"
installed = Path(rynmesh.__file__).resolve().parent
for path in source.rglob("*.py"):
    counterpart = installed / path.relative_to(source)
    if not counterpart.is_file() or path.read_bytes() != counterpart.read_bytes():
        raise SystemExit(f"stale installed source: {path.relative_to(source)}")
PY

mkdir -p "$OUT"
pyinstaller --onefile --noconfirm --clean \
  --codesign-identity - \
  --osx-entitlements-file "$SRC_TAURI/sidecar/rynmesh-peer.entitlements.plist" \
  --name rynmesh-peer \
  --collect-submodules uvicorn \
  --collect-submodules rynmesh \
  --collect-submodules anyio \
  --hidden-import ifaddr \
  --collect-data rynmesh --copy-metadata rynmesh \
  --collect-data jsonschema_specifications \
  --hidden-import uvicorn.lifespan.on \
  --hidden-import uvicorn.loops.asyncio \
  --hidden-import uvicorn.protocols.http.h11_impl \
  --distpath "$BUILD/dist" --workpath "$BUILD/work" --specpath "$BUILD" \
  "$SRC_TAURI/sidecar/rynmesh_peer_entry.py"

cp "$BUILD/dist/rynmesh-peer" "$OUT/rynmesh-peer-$TRIPLE"
chmod +x "$OUT/rynmesh-peer-$TRIPLE"
echo "SIDECAR_BUILT $OUT/rynmesh-peer-$TRIPLE"
