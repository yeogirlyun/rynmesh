#!/usr/bin/env zsh
set -euo pipefail
# Install + load the rynmesh peer launchd agent on a macOS office node.
# Usage: ./bringup-office-mac.zsh <node-name> [src-env-file]
NODE_NAME="${1:?usage: bringup-office-mac.zsh <node-name> [src-env-file]}"
HERE="${0:A:h}"
SRC_ENV="${2:-$HERE/../env/office-mac.env.example}"
CFG_DIR="$HOME/.config/rynmesh"
LOG_DIR="$HOME/Library/Logs"
PLIST_SRC="$HERE/../launchd/ai.rynmesh.peer.plist"
PLIST_DST="$HOME/Library/LaunchAgents/ai.rynmesh.peer.plist"

command -v rynmesh-peer >/dev/null || { print -r -- "rynmesh-peer not on PATH — install rynmesh first"; exit 1; }
mkdir -p "$CFG_DIR" "$LOG_DIR" "$HOME/Library/LaunchAgents"
# Materialize the env file with this host's node name
sed "s/^RYNMESH_NODE_NAME=.*/RYNMESH_NODE_NAME=$NODE_NAME/" "$SRC_ENV" > "$CFG_DIR/office-mac.env"
chmod 600 "$CFG_DIR/office-mac.env"  # may hold RYNMESH_NETWORK_KEY
# Materialize the plist with the real home dir
sed "s#__HOME__#$HOME#g" "$PLIST_SRC" > "$PLIST_DST"
launchctl unload "$PLIST_DST" 2>/dev/null || true
launchctl load "$PLIST_DST"
print -r -- "Loaded ai.rynmesh.peer for node '$NODE_NAME'. Logs: $LOG_DIR/rynmesh-peer.log"
