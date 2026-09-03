# Issue #20 development design

## Implementation overview

The Linux package reuses the existing Tauri 2 shell and PyInstaller sidecar.
The implementation makes their platform seams explicit instead of cloning a
second desktop application.

## Runtime changes

`webapp/src-tauri/src/node.rs` now:

- uses `scutil` only on macOS and `hostname` elsewhere;
- discovers a LAN address through a standard UDP socket route decision;
- writes Linux shell logs beneath
  `${XDG_STATE_HOME:-~/.local/state}/rynmesh`;
- opens Linux logs with `xdg-open`;
- searches `PATH` itself instead of invoking `/usr/bin/which`;
- handles Linux SIGTERM/SIGINT through the event-loop watchdog and keeps Unix
  SIGTERM plus bounded kill/wait behavior for the managed child;
- accepts an adjacent packaged `rynmesh-peer`, or the exact
  `rynmesh-peer-<current-target-triple>` development binary, never the first
  prefix match from another architecture.

The app tray delegates log opening to the platform helper. The default release
resolution path never invokes system Python or Node: a missing/non-executable
packaged sidecar is a startup error. PATH/Python fallbacks remain limited to
debug source development; the explicit operator `RYNMESH_PEER_CMD` override is
still honored.

## Build changes

`build-sidecar.sh` requires a completed production Webapp build, resolves
`rustc` from `PATH`, creates an isolated temporary Python environment, embeds
`webapp/dist` at the node's `rynmesh/webui` package path, and applies PyInstaller
codesign/entitlement arguments only on macOS. Its final file name always
includes the Rust host triple expected by Tauri external binaries.

Pull-request CI has a fixed Ubuntu 24.04 x86_64 package job. It installs build
dependencies, builds and health-checks the frozen sidecar, runs Rust unit tests,
builds one `.deb`, inspects metadata/layout/ELF architecture, installs it, and
runs the desktop shell under an isolated DBus/Xvfb session. Tagged release CI
repeats final-artifact inspection/smoke checks, writes SHA-256, and uploads both
files without overwriting an existing artifact.

## Verification scripts

- `verify-sidecar.sh`: frozen daemon extraction and health.
- `verify-linux-deb.sh`: amd64/version, exact sidecar, ELF, runtime linkage,
  desktop entry, executable, and icon.
- `smoke-linux-desktop.sh`: installed shell/node health, bundled UI route,
  watchdog recovery, XDG log, clean exit, and orphan check.

`verify-sidecar.sh` explicitly removes `PYTHONPATH` and `RYNMESH_REPO_DIR`
before launch. A successful frozen-sidecar check therefore cannot silently use
the source checkout as its import path.

## Acceptance layers

Cross-platform development checks validate the Tauri external-binary contract,
bundle inputs, workflow connections, architecture agreement, lifecycle code,
documentation, and preservation of both macOS jobs. Linux CI then executes the
real ELF/`.deb` inspection and installed process lifecycle. The release workflow
performs the final publication. This split prevents a Windows static audit from
being mistaken for a Linux package run while still allowing implementation to
reach development-complete status.

## Build locally on the supported system

Use Ubuntu 24.04 x86_64 and install the same packages listed in
`.github/workflows/ci.yml`, then:

```bash
cd webapp
npm ci
./src-tauri/scripts/build-sidecar.sh
cargo test --manifest-path src-tauri/Cargo.toml --lib
npm run tauri build -- --bundles deb
./src-tauri/scripts/verify-linux-deb.sh src-tauri/target/release/bundle/deb/*.deb 0.6.2
```

Build-time Python, Node.js, Rust, and packaging libraries are intentionally not
runtime dependencies.
