# Issue #20 test plan

## Automated pull-request matrix

| Layer | Required assertion | Gate |
|---|---|---|
| Sidecar build | exact `x86_64-unknown-linux-gnu` file, x86-64 ELF | Linux CI |
| Sidecar runtime | isolated home, registration disabled, desktop-managed health | Linux CI |
| Rust helpers | exact sidecar resolution and XDG log path unit tests | Linux CI |
| Debian metadata | one package, `amd64`, expected version | Linux CI/release |
| Debian layout | desktop entry, executable, icon, exactly one executable sidecar | Linux CI/release |
| Runtime closure | sidecar has no dynamic system Python/Node dependency | Linux CI/release |
| Installed smoke | shell starts managed node and serves `/` plus `/digest` | Linux CI/release |
| Recovery | killing child yields a different healthy child | Linux CI/release |
| Shutdown | desktop exit leaves no managed child | Linux CI/release |
| Paths | shell writes `ryn-node.log` under isolated `XDG_STATE_HOME` | Linux CI/release |
| Regression | backend/webapp and both macOS desktop compile/release jobs unchanged | repository CI |

All network-independent smoke runs set isolated `RYNMESH_HOME` and
`RYNMESH_NETWORK_DIR`, bind loopback, and set `RYNMESH_AUTO_REGISTER=0`.

## Manual release matrix

Record the artifact SHA-256, Ubuntu point release, kernel, GNOME version, and
Wayland/X11 session. On a clean supported desktop:

1. Verify checksum and install the downloaded artifact.
2. Launch from the application menu with Python, Node, and source checkout
   unavailable; verify window and bundled recommendations UI.
3. Launch again and prove a second node is not created.
4. Exercise tray Open, Open Logs, Restart Node, close-to-tray, and Quit.
5. Upgrade from the previous test package; verify identity/preferences/history
   remain.
6. Uninstall; verify application files leave and owner data/logs remain.

## Failure policy

Missing CI logs, a package built on an unlisted distribution, a smoke test that
uses the source checkout, or a manual record without upgrade/uninstall evidence
is a failed acceptance—not a documentation exception.
