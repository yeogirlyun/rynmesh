# Issue #20 acceptance record

## Acceptance checklist

- [ ] Tagged release publishes `Ryn-<version>-linux-x86_64.deb` and `.sha256`.
- [ ] Final package reports the tag version and `amd64` architecture.
- [ ] Package contains the desktop executable, desktop entry, icon, and exactly
  one executable x86-64 `rynmesh-peer`.
- [ ] Installed application runs without system Python, Node.js, Vite, or a
  source checkout.
- [ ] Managed node reports healthy and serves the bundled `/` and `/digest` UI.
- [ ] Single-instance, watchdog restart, tray restart, close-to-tray, quit, and
  orphan cleanup pass.
- [ ] Linux log is written under the documented XDG state path.
- [ ] Clean install, data-preserving upgrade, and uninstall retention pass on a
  named Ubuntu 24.04 GNOME desktop.
- [ ] README/runbook cover checksum, requirements, install, update, uninstall,
  retained data, paths, and desktop limitations.
- [ ] Linux CI/release jobs and existing macOS Intel/Apple Silicon gates pass.

## Evidence status in this branch

| Evidence | Status | Record |
|---|---|---|
| Cross-platform implementation and CI/release definitions | Implemented | source diff on this branch |
| Web dependency install | Passed 2026-09-02 | `npm ci`, 175 packages audited, 0 vulnerabilities |
| Web unit tests | Passed 2026-09-02 | 9 files, 38 tests |
| Web typecheck/production build | Passed 2026-09-02 | `npm run lint`; `npm run build`, 1,739 modules transformed |
| Python lint regression | Passed 2026-09-02 | `ruff check rynmesh tests` |
| New/existing desktop shell script syntax | Passed 2026-09-02 | Git Bash `bash -n` on build, sidecar verify, Debian verify, and installed smoke scripts |
| Workflow YAML parse | Passed 2026-09-02 | Python `yaml.safe_load` for CI and release workflows |
| Linux artifact contract tests | Passed 2026-09-03 | `tests/test_linux_desktop_artifacts.py`: 5 passed; checks config, CI/release wiring, smoke coverage, and script entrypoints |
| Cargo dependency graph | Passed 2026-09-03 | `cargo metadata --locked --no-deps --format-version 1` |
| Existing deploy artifact tests | Environment-limited 2026-09-03 | raw Windows run: 11 passed, 9 failed; failures are default-GBK UTF-8 reads, POSIX executable bits, and unusable WSL `bash.exe`, not Issue #20 files |
| Rust compile/unit tests | Environment-limited | `cargo check --locked` reaches compilation but fails because MSVC `link.exe` is absent; Linux-only helper tests still require Ubuntu CI |
| Ubuntu 24.04 CI build and installed smoke | Not yet run | requires GitHub Linux runner |
| Tagged artifact and checksum | Not yet published | requires an actual release tag |
| macOS regression jobs | Not yet run | requires repository CI |
| Real Ubuntu 24.04 GNOME manual acceptance | Not yet run | requires supported desktop hardware/VM |

## GitHub acceptance-criteria audit

| Upstream criterion | Implementation evidence | Completion evidence |
|---|---|---|
| Select one Tauri Linux format | Ubuntu 24.04 x86_64 `.deb` in product/runbook and both workflows | Implemented; final artifact still external |
| Bundle node and Webapp without runtime Python/Node | production UI embedded into one PyInstaller sidecar; exact Tauri `externalBin` | Static contract passed; installed-package proof pending |
| Startup, health, UI, clean shutdown, restart in CI | `smoke-linux-desktop.sh` plus fixed Ubuntu package job | Workflow wired; GitHub job not yet run on this commit |
| Sidecar/package architecture match | exact Rust triple build plus `dpkg-deb`, `file`, and layout verifier | Workflow wired; final `.deb` inspection pending |
| Publish artifact and checksum | tagged release job uploads `.deb` and `.sha256` | Not complete until a real tag release succeeds |
| Document install/update/requirements/limits | README and `docs/LINUX_DESKTOP.md` | Implemented and statically reviewable |
| Preserve macOS verification | Intel/Apple Silicon compile and tagged DMG jobs remain | Definitions present; exact-commit jobs pending |

## Required external evidence

For an exact candidate commit `<sha>` and workflow run `<run-id>`, preserve:

```bash
gh run watch <run-id> --exit-status
gh run view <run-id> --log > issue-20-github-run.log
gh run download <run-id> -n Ryn-linux-x86_64 -D issue-20-artifact
cd issue-20-artifact && sha256sum -c Ryn-linux-x86_64.deb.sha256
dpkg-deb --field Ryn-linux-x86_64.deb Version Architecture
```

The log must show the Linux package/smoke job and both macOS compile gates for
the same `<sha>`. On a named Ubuntu 24.04 GNOME machine, save `lsb_release -a`,
`uname -a`, `gnome-shell --version`, `$XDG_SESSION_TYPE`, checksum output, and a
screen recording or timestamped checklist covering menu launch, second launch,
tray Open/Open Logs/Restart/Quit, close-to-tray, prior-version upgrade with data
retention, and uninstall with application removal plus owner-data/log retention.
Finally save `gh release view <tag> --json tagName,assets,url` proving the final
`.deb` and checksum are published.

## Completion decision

**Not accepted yet.** The code and automation can be reviewed and executed, but
Issue #20 must remain open until Linux CI, existing macOS gates, a tagged final
artifact, and the real-desktop install/upgrade/uninstall record all pass. Static
inspection on a Windows development checkout is not a substitute for those
release gates.
