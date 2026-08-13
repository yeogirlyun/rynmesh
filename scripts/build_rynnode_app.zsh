#!/bin/zsh
# Build the self-contained macOS Ryn desktop app and DMG from source.
#
# The resulting app contains the frozen rynmesh-peer daemon and production web
# interface. It does not depend on this checkout, Python, Node.js, or a Vite
# server after installation.
set -euo pipefail

REPO="${0:A:h:h}"
TARGET_DIR="$(mktemp -d)"
MOUNT_DIR="$(mktemp -d)"
MOUNTED=0
OUTPUT_DIR="$REPO/dist/desktop"

cleanup() {
  (( MOUNTED )) && hdiutil detach "$MOUNT_DIR" >/dev/null 2>&1 || true
  rm -rf -- "$TARGET_DIR" "$MOUNT_DIR"
}
trap cleanup EXIT

for command in python3 npm cargo; do
  command -v "$command" >/dev/null 2>&1 || {
    print -u2 "Missing build dependency: $command"
    exit 1
  }
done

print "Building Ryn desktop dependencies"
(cd "$REPO/webapp" && npm ci)

print "Freezing the local Ryn node daemon"
"$REPO/webapp/src-tauri/scripts/build-sidecar.sh"

print "Building the signed application and disk image"
(cd "$REPO/webapp" && CARGO_TARGET_DIR="$TARGET_DIR" npm run tauri build -- --bundles dmg)

DMG="$(find "$TARGET_DIR/release/bundle/dmg" -name '*.dmg' -print -quit)"
[[ -n "$DMG" && -f "$DMG" ]] || {
  print -u2 "Desktop build completed without a DMG artifact"
  exit 1
}

hdiutil attach "$DMG" -mountpoint "$MOUNT_DIR" -nobrowse -readonly >/dev/null
MOUNTED=1
APP="$MOUNT_DIR/Ryn.app"
codesign --verify --deep --strict --verbose=2 "$APP"
[[ -x "$APP/Contents/MacOS/rynmesh-peer" ]]
hdiutil detach "$MOUNT_DIR" >/dev/null
MOUNTED=0

mkdir -p "$OUTPUT_DIR"
cp "$DMG" "$OUTPUT_DIR/$(basename "$DMG")"
print "Built $OUTPUT_DIR/$(basename "$DMG")"
