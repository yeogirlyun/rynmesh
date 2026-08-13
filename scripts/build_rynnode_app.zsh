#!/bin/zsh
# Build a double-clickable Rynnode.app (macOS) that launches the local rynnode + UI
# and opens the app, then pin it to the Dock. Re-runnable. No Electron/Tauri build —
# a lightweight .app that wraps the launcher with the rynmesh icon.
#
# Usage:  ./scripts/build_rynnode_app.zsh [node-name]
#   node-name defaults to this machine's short hostname (e.g. ms-1).
set -euo pipefail

# Derive the repo root from this script's own location (works wherever the repo lives).
REPO="${0:A:h:h}"
REGISTRY="${RYNMESH_REGISTRY_URL:-http://203.0.113.10:8790}"
NODE_NAME="${1:-$(scutil --get LocalHostName 2>/dev/null | tr 'A-Z' 'a-z' || hostname -s)}"
APP="/Applications/Rynnode.app"

# Resolve real tool paths (a Dock-launched .app runs with a minimal PATH).
# Prefer the repo's own venv python and a locally-installed Node over whatever
# happens to be on PATH (the system python3 user-site may be the wrong arch).
if [[ -x "$REPO/.venv/bin/python" ]]; then
  PY="$REPO/.venv/bin/python"
else
  PY="$(command -v python3 || echo /usr/bin/python3)"
fi
if command -v npm >/dev/null 2>&1; then
  NPM="$(command -v npm)"
elif [[ -x "$HOME/.local/node/bin/npm" ]]; then
  NPM="$HOME/.local/node/bin/npm"
else
  NPM="/opt/homebrew/bin/npm"
fi
NPM_DIR="${NPM:h}"

echo "Building $APP  (node-name=$NODE_NAME, registry=$REGISTRY)"
rm -rf "$APP"
mkdir -p "$APP/Contents/MacOS" "$APP/Contents/Resources"
# Build the app icon from the rynmesh brand asset (NOT the generic Tauri default icon.icns).
ICON_SRC="$REPO/webapp/public/brand/png/app-icon-webapp-1024.png"
ICONSET="$(mktemp -d)/Rynnode.iconset"
mkdir -p "$ICONSET"
for s in 16 32 128 256 512; do
  sips -z $s $s "$ICON_SRC" --out "$ICONSET/icon_${s}x${s}.png" >/dev/null 2>&1
  sips -z $((s*2)) $((s*2)) "$ICON_SRC" --out "$ICONSET/icon_${s}x${s}@2x.png" >/dev/null 2>&1
done
iconutil -c icns "$ICONSET" -o "$APP/Contents/Resources/Rynnode.icns"

cat > "$APP/Contents/Info.plist" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>CFBundleName</key><string>Rynnode</string>
  <key>CFBundleDisplayName</key><string>Rynnode</string>
  <key>CFBundleIdentifier</key><string>ai.rynmesh.rynnode</string>
  <key>CFBundleVersion</key><string>1.0</string>
  <key>CFBundleShortVersionString</key><string>1.0</string>
  <key>CFBundlePackageType</key><string>APPL</string>
  <key>CFBundleExecutable</key><string>Rynnode</string>
  <key>CFBundleIconFile</key><string>Rynnode</string>
  <key>NSHighResolutionCapable</key><true/>
  <key>LSMinimumSystemVersion</key><string>10.13</string>
</dict>
</plist>
PLIST

cat > "$APP/Contents/MacOS/Rynnode" <<LAUNCH
#!/bin/zsh
PY="$PY"
NPM="$NPM"
export PATH="$NPM_DIR:${PY:h}:\$HOME/Library/Python/3.13/bin:/usr/bin:/bin:/usr/sbin:/sbin"
REPO="$REPO"
REGISTRY="$REGISTRY"
cd "\$REPO" 2>/dev/null || { osascript -e 'display alert "Rynnode" message "Project not found at $REPO"'; exit 1; }
# A Dock-launched .app can inherit an x86_64 (Rosetta) preference; pin the native
# slice on Apple Silicon so arm64-only wheels (pydantic_core, cryptography) load.
# NOTE: under Rosetta \`uname -m\` lies (reports x86_64), so detect the real hardware
# via sysctl, which reports arm64 even when this process is being translated.
# zsh does not word-split a scalar, so use an array (empty array expands to nothing).
ARCH_PREFIX=()
[[ "\$(/usr/sbin/sysctl -n hw.optional.arm64 2>/dev/null)" == "1" ]] && ARCH_PREFIX=(arch -arm64)
# Point rynmesh-vpn (the VPN data plane) at an available SSH identity for the
# egress gateway; falls back through known key names so it works across machines.
for _k in "\$HOME/.ssh/rynmesh_example_key" "\$HOME/.ssh/rynmesh"; do
  [[ -f "\$_k" ]] && { export RYNMESH_VPN_KEY="\$_k"; break; }
done
if ! /usr/sbin/lsof -nP -iTCP:8791 -sTCP:LISTEN >/dev/null 2>&1; then
  RYNMESH_HOME="\$HOME/.rynmesh/mac" RYNMESH_REGISTRY_URL="\$REGISTRY" RYNMESH_NETWORK_ID=rynmesh-main \\
    RYNMESH_NODE_NAME="$NODE_NAME" RYNMESH_AUTO_REGISTER=1 RYNMESH_PEER_PORT=8791 \\
    nohup \$ARCH_PREFIX "\$PY" -m rynmesh.peer_http >/tmp/rynnode.log 2>&1 &
  sleep 5
fi
if ! /usr/sbin/lsof -nP -iTCP:5173 -sTCP:LISTEN >/dev/null 2>&1; then
  ( cd "\$REPO/webapp" && nohup "\$NPM" run dev >/tmp/rynnode-webapp.log 2>&1 & )
  sleep 8
fi
open "http://localhost:5173/"
LAUNCH
chmod +x "$APP/Contents/MacOS/Rynnode"

# Register + pin to the Dock (no duplicate).
/System/Library/Frameworks/CoreServices.framework/Frameworks/LaunchServices.framework/Support/lsregister -f "$APP" 2>/dev/null || true
if ! defaults read com.apple.dock persistent-apps 2>/dev/null | grep -q "Rynnode.app"; then
  defaults write com.apple.dock persistent-apps -array-add \
    '<dict><key>tile-data</key><dict><key>file-data</key><dict><key>_CFURLString</key><string>/Applications/Rynnode.app</string><key>_CFURLStringType</key><integer>0</integer></dict><key>file-label</key><string>Rynnode</string></dict></dict>'
  killall Dock
fi
echo "✓ Rynnode.app built and pinned to the Dock. Click it to launch rynnode."
