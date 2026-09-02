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
| Existing deploy artifact tests | Environment-limited | 12 passed; 3 existing POSIX-mode/WSL checks cannot pass on this Windows checkout |
| Rust unit tests | Not run locally | no Rust toolchain is installed; the added Ubuntu CI gate runs them |
| Ubuntu 24.04 CI build and installed smoke | Not yet run | requires GitHub Linux runner |
| Tagged artifact and checksum | Not yet published | requires an actual release tag |
| macOS regression jobs | Not yet run | requires repository CI |
| Real Ubuntu 24.04 GNOME manual acceptance | Not yet run | requires supported desktop hardware/VM |

## Completion decision

**Not accepted yet.** The code and automation can be reviewed and executed, but
Issue #20 must remain open until Linux CI, existing macOS gates, a tagged final
artifact, and the real-desktop install/upgrade/uninstall record all pass. Static
inspection on a Windows development checkout is not a substitute for those
release gates.
