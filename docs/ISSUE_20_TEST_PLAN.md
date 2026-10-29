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
| Workflow contract | package job, release upload, macOS preservation, and smoke wiring stay connected | cross-platform pytest |
| Regression | backend/webapp and both macOS desktop compile/release jobs unchanged | repository CI |

All network-independent smoke runs set isolated `RYNMESH_HOME` and
`RYNMESH_NETWORK_DIR`, bind loopback, and set `RYNMESH_AUTO_REGISTER=0`.

## Optional release QA matrix

This matrix is useful product QA but is not an additional Issue #20 completion
gate. On a clean supported desktop:

1. Verify checksum and install the downloaded artifact.
2. Launch from the application menu with Python, Node, and source checkout
   unavailable; verify window and bundled recommendations UI.
3. Launch again and prove a second node is not created.
4. Exercise tray Open, Open Logs, Restart Node, close-to-tray, and Quit.
5. Upgrade from the previous test package; verify identity/preferences/history
   remain.
6. Uninstall; verify application files leave and owner data/logs remain.

## Issue acceptance policy

The five locally decidable criteria fail if their implementation, tests, or
workflow contracts fail. The two external criteria fail or remain pending when
the Linux CI lifecycle job or release publication is missing. Absence of the
optional physical-desktop QA record does not redefine development completion.

## Cross-platform audit checks

`tests/test_linux_desktop_artifacts.py` is deliberately static: it can run on
Windows/macOS and prevents removal or renaming of the Tauri sidecar closure,
architecture agreement, lifecycle contract, Ubuntu package job, Debian
verifier, installed smoke, checksum upload, and existing macOS verification.
It does **not** prove that a `.deb` installs or runs. That evidence is the
original Issue's CI criterion (#3), not a locally fabricated substitute.

```bash
python -m pytest tests/test_linux_desktop_artifacts.py -q  # 8 contract tests
bash -n webapp/src-tauri/scripts/{build-sidecar,verify-sidecar,verify-linux-deb,smoke-linux-desktop}.sh
cargo metadata --manifest-path webapp/src-tauri/Cargo.toml --locked --no-deps
```
