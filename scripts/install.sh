#!/bin/sh
# Ryn node installer — https://github.com/yeogirlyun/rynmesh
#
#   curl -fsSL https://github.com/yeogirlyun/rynmesh/releases/latest/download/install.sh | sh
#
# Installs (or updates) the Ryn node into its own isolated environment, sets up
# a free local AI model via Ollama, registers a login agent, and opens the app.
# Re-running it upgrades in place — this script is both installer and updater.
#
# It never touches your system Python, and everything it creates lives in
# ~/.rynmesh/app (+ one launch agent and one .app launcher).
set -eu

BASE_URL="${RYNMESH_BASE_URL:-https://github.com/yeogirlyun/rynmesh/releases/latest/download}"
PREFIX="${RYNMESH_PREFIX:-$HOME/.rynmesh/app}"
PORT="${RYNMESH_PEER_PORT:-8791}"
MODEL="${RYNMESH_OLLAMA_MODEL:-gemma3:4b}"
NO_MODEL="${RYNMESH_SKIP_MODEL:-0}"

green() { printf '\033[32m%s\033[0m\n' "$1"; }
info()  { printf '\033[32m▸\033[0m %s\n' "$1"; }
warn()  { printf '\033[33m!\033[0m %s\n' "$1"; }
die()   { printf '\033[31m✗\033[0m %s\n' "$1" >&2; exit 1; }

# A Ryn node identifies itself: /health returns {"status":"ok","peer_id":"..."}.
# Checking for peer_id (not just HTTP 200) means an unrelated service sitting on
# the port can never be mistaken for our node.
is_ryn_node() {
    curl -fsS --max-time 2 "http://127.0.0.1:$1/health" 2>/dev/null | grep -q '"peer_id"'
}
port_busy() {
    if command -v lsof >/dev/null 2>&1; then
        lsof -nP -iTCP:"$1" -sTCP:LISTEN >/dev/null 2>&1
    else
        curl -fsS --max-time 1 "http://127.0.0.1:$1/" >/dev/null 2>&1
    fi
}

printf '\n'
green "Ryn — your personal AI agent node"
printf '  Installing to %s\n\n' "$PREFIX"

# ---- 0. pick a port we can actually own --------------------------------------
if port_busy "$PORT" && ! is_ryn_node "$PORT"; then
    warn "port $PORT is already used by another program"
    ALT=""
    n=8801
    while [ "$n" -lt 8830 ]; do
        if ! port_busy "$n"; then ALT="$n"; break; fi
        n=$((n+1))
    done
    [ -n "$ALT" ] || die "no free port found between 8801 and 8829. Free one, or set RYNMESH_PEER_PORT."
    PORT="$ALT"
    info "using port $PORT instead"
fi

# ---- 1. Python ---------------------------------------------------------------
PY=""
for candidate in python3.13 python3.12 python3.11 python3.10 python3; do
    if command -v "$candidate" >/dev/null 2>&1; then
        if "$candidate" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3,10) else 1)' 2>/dev/null; then
            PY="$candidate"; break
        fi
    fi
done
[ -n "$PY" ] || die "Python 3.10+ is required. Install it from https://python.org/downloads and re-run."
info "using $($PY -V 2>&1)"

# ---- 2. isolated environment -------------------------------------------------
# A dedicated venv is the whole point: the node's deps can never go missing
# because some other tool changed your system Python.
if [ ! -x "$PREFIX/bin/python" ]; then
    info "creating an isolated environment"
    mkdir -p "$(dirname "$PREFIX")"
    "$PY" -m venv "$PREFIX" || die "could not create a virtualenv at $PREFIX"
fi
"$PREFIX/bin/python" -m pip install --quiet --upgrade pip >/dev/null 2>&1 || true

# ---- 3. the node ------------------------------------------------------------
WHEEL_URL="${RYNMESH_WHEEL_URL:-}"
if [ -z "$WHEEL_URL" ]; then
    LATEST="$(curl -fsSL "$BASE_URL/latest.json" 2>/dev/null || true)"
    [ -n "$LATEST" ] || die "could not reach $BASE_URL/latest.json — check your connection."
    WHEEL_URL=$(printf '%s' "$LATEST" | sed -n 's/.*"wheel"[[:space:]]*:[[:space:]]*"\([^"]*\)".*/\1/p')
    VERSION=$(printf '%s' "$LATEST" | sed -n 's/.*"version"[[:space:]]*:[[:space:]]*"\([^"]*\)".*/\1/p')
    EXPECT_SHA=$(printf '%s' "$LATEST" | sed -n 's/.*"sha256"[[:space:]]*:[[:space:]]*"\([^"]*\)".*/\1/p')
    [ -n "$WHEEL_URL" ] || die "latest.json did not name a wheel."
    info "installing Ryn node ${VERSION:-latest}"
else
    EXPECT_SHA=""
    info "installing Ryn node from $WHEEL_URL"
fi

TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT
case "$WHEEL_URL" in
    file://*) cp "${WHEEL_URL#file://}" "$TMP/" ;;
    /*)       cp "$WHEEL_URL" "$TMP/" ;;
    *)        curl -fsSL "$WHEEL_URL" -o "$TMP/$(basename "$WHEEL_URL")" || die "download failed: $WHEEL_URL" ;;
esac
WHEEL="$TMP/$(basename "$WHEEL_URL")"

# Verify the download before executing anything from it.
if [ -n "${EXPECT_SHA:-}" ]; then
    if command -v shasum >/dev/null 2>&1; then GOT="$(shasum -a 256 "$WHEEL" | awk '{print $1}')"
    elif command -v sha256sum >/dev/null 2>&1; then GOT="$(sha256sum "$WHEEL" | awk '{print $1}')"
    else GOT=""; warn "no shasum tool found — skipping checksum verification"; fi
    if [ -n "$GOT" ] && [ "$GOT" != "$EXPECT_SHA" ]; then
        die "checksum mismatch — refusing to install (expected $EXPECT_SHA, got $GOT)"
    fi
    [ -n "$GOT" ] && info "checksum verified"
fi

# Two passes on purpose. The first resolves and upgrades dependencies. The
# second guarantees our own package bytes are fresh: pip treats an equal
# version as "already satisfied", so a rebuilt wheel carrying a new web UI
# would otherwise install nothing and the app would look unchanged.
"$PREFIX/bin/python" -m pip install --quiet --upgrade "$WHEEL" || die "install failed"
"$PREFIX/bin/python" -m pip install --quiet --force-reinstall --no-deps "$WHEEL" \
    || die "install failed"
# PDF rendering for the daily recap. Non-fatal: the node runs without it.
"$PREFIX/bin/python" -m pip install --quiet "reportlab>=4.0.0" >/dev/null 2>&1 \
    || warn "reportlab unavailable — recap emails will send without the PDF"
INSTALLED="$("$PREFIX/bin/python" -c 'from importlib.metadata import version; print(version("rynmesh"))' 2>/dev/null || echo '?')"
info "Ryn node $INSTALLED installed"

# ---- 4. local AI model (Ollama) ----------------------------------------------
if [ "$NO_MODEL" = "1" ]; then
    warn "skipping the local AI model (RYNMESH_SKIP_MODEL=1)"
elif command -v ollama >/dev/null 2>&1; then
    info "Ollama found"
    if ! ollama list 2>/dev/null | tail -n +2 | grep -q .; then
        info "pulling $MODEL (a few GB, one time — this is your private AI)"
        ollama pull "$MODEL" || warn "could not pull $MODEL; add a model later with: ollama pull $MODEL"
    else
        info "a local model is already installed"
    fi
else
    warn "Ollama is not installed — the node works, but AI briefings and Search & Ask stay off."
    printf '    Install it (free, local, private):  \033[36mhttps://ollama.com/download\033[0m\n'
    printf '    Then run:  ollama pull %s\n' "$MODEL"
    printf '    Or use a cloud model instead by setting ANTHROPIC_API_KEY.\n'
fi

# ---- 5. run at login + app launcher ------------------------------------------
# The login agent is per-user and session-global: launchd runs it with the real
# account's HOME regardless of what HOME this script saw. So a throwaway install
# into a custom prefix must NOT register it, or it silently takes over the
# user's actual node. Only the canonical prefix owns the agent.
UNAME="$(uname -s)"
if [ "$PREFIX" != "$HOME/.rynmesh/app" ] || [ "${RYNMESH_NO_AGENT:-0}" = "1" ]; then
    warn "custom prefix — skipping login agent and app launcher (portable install)"
    printf '    Start it with:  RYNMESH_PEER_PORT=%s %s/bin/rynmesh-peer\n' "$PORT" "$PREFIX"
    UNAME="portable"
fi
if [ "$UNAME" = "Darwin" ]; then
    LABEL="ai.rynmesh.node"
    PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
    mkdir -p "$HOME/Library/LaunchAgents" "$HOME/Library/Logs"
    cat > "$PLIST" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>$LABEL</string>
  <key>ProgramArguments</key>
  <array><string>$PREFIX/bin/rynmesh-peer</string></array>
  <key>EnvironmentVariables</key>
  <dict>
    <key>RYNMESH_DESKTOP_MODE</key><string>1</string>
    <key>RYNMESH_PEER_PORT</key><string>$PORT</string>
    <key>PATH</key><string>/usr/local/bin:/opt/homebrew/bin:/usr/bin:/bin</string>
  </dict>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>StandardOutPath</key><string>$HOME/Library/Logs/rynmesh-node.log</string>
  <key>StandardErrorPath</key><string>$HOME/Library/Logs/rynmesh-node.log</string>
</dict>
</plist>
PLIST
    launchctl unload "$PLIST" >/dev/null 2>&1 || true
    launchctl load "$PLIST" >/dev/null 2>&1 && info "node will start automatically at login" \
        || warn "could not register the login agent; start manually: $PREFIX/bin/rynmesh-peer"
    # An update rewrites files on disk, but a node that is already running keeps
    # the old code in memory: the install reports success and nothing changes.
    launchctl kickstart -k "gui/$(id -u)/$LABEL" >/dev/null 2>&1 || true

    APP="$HOME/Applications/Ryn.app"
    mkdir -p "$APP/Contents/MacOS" "$APP/Contents/Resources"
    cat > "$APP/Contents/Info.plist" <<'PLIST'
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>CFBundleName</key><string>Ryn</string>
  <key>CFBundleDisplayName</key><string>Ryn</string>
  <key>CFBundleIdentifier</key><string>ai.rynmesh.launcher</string>
  <key>CFBundleExecutable</key><string>ryn</string>
  <key>CFBundleIconFile</key><string>Ryn</string>
  <key>CFBundlePackageType</key><string>APPL</string>
  <key>LSUIElement</key><false/>
</dict>
</plist>
PLIST
    cat > "$APP/Contents/MacOS/ryn" <<LAUNCH
#!/bin/sh
# Bring the node up if it isn't, then open the app.
if ! /usr/bin/curl -fsS --max-time 2 "http://127.0.0.1:$PORT/health" >/dev/null 2>&1; then
    /bin/launchctl load "$HOME/Library/LaunchAgents/ai.rynmesh.node.plist" >/dev/null 2>&1 || true
    RYNMESH_DESKTOP_MODE=1 RYNMESH_PEER_PORT=$PORT "$PREFIX/bin/rynmesh-peer" >>"$HOME/Library/Logs/rynmesh-node.log" 2>&1 &
    n=0
    while [ \$n -lt 30 ]; do
        /usr/bin/curl -fsS --max-time 1 "http://127.0.0.1:$PORT/health" >/dev/null 2>&1 && break
        sleep 0.5; n=\$((n+1))
    done
fi
exec /usr/bin/open "http://127.0.0.1:$PORT/"
LAUNCH
    chmod +x "$APP/Contents/MacOS/ryn"
    ICON_SRC="$PREFIX/lib/python"*"/site-packages/rynmesh/webui/brand/png/app-icon-webapp-1024.png"
    for candidate in $ICON_SRC; do
        [ -f "$candidate" ] || continue
        ICONSET="$TMP/Ryn.iconset"; mkdir -p "$ICONSET"
        for s in 16 32 128 256 512; do
            sips -z $s $s "$candidate" --out "$ICONSET/icon_${s}x${s}.png" >/dev/null 2>&1 || true
            sips -z $((s*2)) $((s*2)) "$candidate" --out "$ICONSET/icon_${s}x${s}@2x.png" >/dev/null 2>&1 || true
        done
        iconutil -c icns "$ICONSET" -o "$APP/Contents/Resources/Ryn.icns" >/dev/null 2>&1 || true
        break
    done
    touch "$APP"
    info "installed $APP"
elif [ "$UNAME" = "Linux" ] && command -v systemctl >/dev/null 2>&1; then
    mkdir -p "$HOME/.config/systemd/user"
    cat > "$HOME/.config/systemd/user/rynmesh-node.service" <<UNIT
[Unit]
Description=Ryn node
[Service]
ExecStart=$PREFIX/bin/rynmesh-peer
Environment=RYNMESH_PEER_PORT=$PORT
Environment=RYNMESH_DESKTOP_MODE=1
Restart=always
[Install]
WantedBy=default.target
UNIT
    systemctl --user daemon-reload >/dev/null 2>&1 || true
    systemctl --user enable --now rynmesh-node >/dev/null 2>&1 && info "node enabled as a user service" \
        || warn "start manually: $PREFIX/bin/rynmesh-peer"
fi

# ---- 6. up and open ----------------------------------------------------------
n=0
while [ $n -lt 40 ]; do
    is_ryn_node "$PORT" && break
    sleep 0.5; n=$((n+1))
done

printf '\n'
if is_ryn_node "$PORT"; then
    green "Ryn is running — http://127.0.0.1:$PORT"
    [ "$UNAME" = "Darwin" ] && open "http://127.0.0.1:$PORT/" >/dev/null 2>&1 || true
else
    warn "the node did not answer on port $PORT."
    printf '    Start it by hand to see why:  RYNMESH_DESKTOP_MODE=1 RYNMESH_PEER_PORT=%s %s/bin/rynmesh-peer\n' "$PORT" "$PREFIX"
    printf '    Log: %s/Library/Logs/rynmesh-node.log\n' "$HOME"
fi
printf '\n  Recommendations start automatically from Ryn\047s built-in public catalog.\n'
printf '  Add a YouTube channel, subreddit, or RSS feed only when you want a personal source.\n'
printf '  Update any time by re-running this installer.\n\n'
