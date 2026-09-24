#!/bin/sh
# Build and verify the native Apple Silicon desktop and its bundled Python node.
set -eu

ROOT="$(CDPATH= cd -- "$(dirname -- "$0")/../../.." && pwd)"
export PATH="$HOME/.cargo/bin:$PATH"
[ "$(uname -s)" = Darwin ] && [ "$(uname -m)" = arm64 ] || {
  echo 'Run on an Apple Silicon Mac, using a native arm64 terminal.' >&2
  exit 1
}
[ "$(rustc -Vv | sed -n 's/^host: //p')" = aarch64-apple-darwin ] || {
  echo 'A native aarch64-apple-darwin Rust toolchain is required.' >&2
  exit 1
}
"${RYNMESH_BUILD_PYTHON:-python3}" -c 'import platform, sys; assert platform.machine() == "arm64" and sys.version_info >= (3, 12), "Native arm64 Python 3.12+ required"'
xcode-select -p >/dev/null
cd "$ROOT/webapp"
npm ci
sh src-tauri/scripts/build-sidecar.sh
npm run tauri -- build --target aarch64-apple-darwin --bundles app,dmg

BUNDLE="$ROOT/webapp/src-tauri/target/aarch64-apple-darwin/release/bundle"
APP="$BUNDLE/macos/Ryn.app"
codesign --verify --deep --strict --verbose=2 "$APP"
APP_EXE="$(plutil -extract CFBundleExecutable raw "$APP/Contents/Info.plist")"
lipo "$APP/Contents/MacOS/$APP_EXE" -verify_arch arm64
lipo "$APP/Contents/MacOS/rynmesh-peer" -verify_arch arm64
sh "$ROOT/webapp/src-tauri/scripts/verify-sidecar.sh" "$APP/Contents/MacOS/rynmesh-peer"
# Exercise the subprocess entry used by CLI isolation inside the frozen daemon.
"$APP/Contents/MacOS/rynmesh-peer" --ryn-private-cli-worker 10 /usr/bin/true

VERSION="$(node -p 'require("./package.json").version')"
OUT="$ROOT/release/macos"
mkdir -p "$OUT"
DMG="$(find "$BUNDLE/dmg" -maxdepth 1 -name '*.dmg' -print -quit)"
[ -n "$DMG" ] || { echo 'DMG missing' >&2; exit 1; }
DEST="Ryn-$VERSION-cli-discovery-macos-arm64.dmg"
cp "$DMG" "$OUT/$DEST"
(cd "$OUT" && shasum -a 256 "$DEST" > "$DEST.sha256")
echo "MACOS_PACKAGE_VERIFIED $OUT/$DEST"
