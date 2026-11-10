# Linux desktop runbook

## Supported scope

The first native Linux release supports **Ubuntu 24.04 LTS on x86_64 desktop
systems**. The release artifact is a Debian package named
`Ryn-<version>-linux-x86_64.deb`. ARM64, older Ubuntu releases, Debian itself,
RPM-based distributions, AppImage, Flatpak, Snap, and headless servers are not
yet supported targets.

The package includes the Tauri shell, production web interface, and a frozen
`rynmesh-peer` sidecar. It does not use system Python, Node.js, a Vite server,
or a repository checkout at runtime. GTK, WebKitGTK, app-indicator, and related
desktop libraries remain normal package-managed system dependencies.

## Verify and install

Download the `.deb` and matching `.sha256` file from the same GitHub Release:

```bash
sha256sum --check Ryn-0.6.2-linux-x86_64.deb.sha256
sudo apt install ./Ryn-0.6.2-linux-x86_64.deb
```

Launch **Ryn** from the desktop application menu. The shell starts one managed
local node and serves the bundled UI on `http://127.0.0.1:8791`. An account,
model, Registry connection, and public network access are not required for the
package smoke test or the default catalog experience.

## Update and uninstall

There is no silent in-app updater. Verify a newer package and install it over
the existing version:

```bash
sudo apt install ./Ryn-NEW_VERSION-linux-x86_64.deb
```

Remove the installed application with:

```bash
sudo apt remove ryn
```

Package removal intentionally retains owner data. After confirming that no
needed identity, preferences, history, content, or logs remain, the owner may
remove the default data and desktop-log directories manually:

```bash
rm -r -- "$HOME/.rynmesh" "${XDG_STATE_HOME:-$HOME/.local/state}/rynmesh"
```

Those directories must not be removed as part of package uninstall or upgrade.

## Paths and operator overrides

- Node identity, preferences, cache, and content: `RYNMESH_HOME`, default
  beneath `~/.rynmesh`.
- Desktop shell log: `${XDG_STATE_HOME:-~/.local/state}/rynmesh/ryn-node.log`.
- Node port: `RYNMESH_PEER_PORT`, default `8791`.
- Registry access can be disabled with `RYNMESH_AUTO_REGISTER=0` for isolated
  testing.

The tray **Open Logs** action uses `xdg-open`. On minimal window managers with
no desktop opener, inspect the log path directly.

## Desktop environment notes

The acceptance baseline is Ubuntu 24.04 GNOME. WebKitGTK supports both Wayland
and X11 sessions, but tray presentation depends on the desktop environment's
app-indicator support. Closing the main window hides it while the tray process
and local node continue running; choose **Quit** from the tray to stop both.

This package is not a system service and is not supported on a headless server.
For a headless node, install the Python package and operate `rynmesh-peer`
directly instead.

## Troubleshooting

1. Confirm package architecture and version:

   ```bash
   dpkg-query -W -f='${Architecture} ${Version}\n' ryn
   ```

2. Confirm the node and bundled UI:

   ```bash
   curl -fsS http://127.0.0.1:8791/health
   curl -I http://127.0.0.1:8791/
   ```

3. Read `${XDG_STATE_HOME:-$HOME/.local/state}/rynmesh/ryn-node.log`.
4. If port 8791 is already used by a compatible desktop-managed Ryn node, the
   shell reuses it. Stop unrelated software using the port before retrying.
5. Do not install Python or Node.js to repair a packaged-app startup failure;
   report the package version, Ubuntu version, desktop session, and log instead.
