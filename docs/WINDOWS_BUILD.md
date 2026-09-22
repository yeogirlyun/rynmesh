# Windows desktop test build

Branch: `development/personal-first-implementation`.
Version: 0.6.2; x64; English; light, dark and system appearance.

## Build

Prerequisites: Windows x64, Python 3.12, Node/npm, Rust MSVC x64, Visual Studio C++ build tools and Windows SDK. The end user's machine does not need Python, Node or Rust.

From the repository root:

```powershell
./webapp/src-tauri/scripts/build-windows.ps1
```

The script freezes the Python node with its package metadata, builds the React frontend and Tauri shell, and creates:

`webapp/src-tauri/target/x86_64-pc-windows-msvc/release/bundle/nsis/Ryn_0.6.2_x64-setup.exe`

UI-only baseline: `release/windows/Ryn-0.6.2-windows-x64-setup.exe`.

Personal-space build: `release/windows/Ryn-0.6.2-personal-space-windows-x64-setup.exe`, with its own SHA256 file alongside it. The UI-only and personal-space baseline packages both report app version 0.6.2; the newer P2P package is listed below.

Latest P2P test build: `release/windows/Ryn-0.6.2-personal-space-p2p-windows-x64-setup.exe`.
It also reports version 0.6.2; use this filename for automatic direct/ICE connection
selection and network-address refresh. See `PERSONAL_SPACE_CONNECTIVITY.md` for
the three-computer acceptance steps and current limits. All three computers should
use this build; no relay deployment is included.

## Package behavior

- Installs for the current Windows user. WebView2 is required; the installer downloads the Microsoft bootstrapper if the runtime is missing, so that case requires internet access.
- Starts its bundled node, hides its console, provides a tray menu and focuses the existing window on repeat launch.
- Settings control launch at startup and whether closing the window keeps the node running. Quit stops the owned node process tree.
- Local WebView2 requests are accepted only from exact desktop origins on unforwarded loopback connections. Existing remote authentication stays enforced.
- Node port can be configured using `RYNMESH_PEER_PORT`; every desktop API client uses the same port.
- The frozen node does not run pip/wheel auto-updates. Update the desktop app using a newer installer.
- Native node logs: `%LOCALAPPDATA%/Ryn/logs`; desktop preferences: `%APPDATA%/ai.rynmesh.desktop/desktop.json`.

## Acceptance and limits

The generated native executable was tested with an isolated node home and port: startup, actual Home/onboarding, light/dark themes, Windows startup registration and removal, background operation, and full exit cleanup. The final frozen node's update status reports manual installer updates.

This is an unsigned development package. Windows can show an unknown-publisher warning. Public release still needs signing and clean-machine install/upgrade/uninstall verification. An existing Ryn installation was detected on the development machine, so the installer was not run over that installation during this task.

Personal-space invitations, signed membership, management roles, removal, AI local/space access and encrypted recovery are implemented. See `PERSONAL_SPACE.md` for usage, coordinator requirements and the 24-hour offline permission limit. Per-service guest grants, remote desktop, NAS and LAN resource sharing remain deferred. No physical cross-network AI performance or connectivity acceptance is claimed by this implementation pass.
