# Issue #20 acceptance record

## Scope of this decision

This record follows the seven acceptance criteria in
[upstream GitHub Issue #20](https://github.com/yeogirlyun/rynmesh/issues/20)
verbatim in substance. It deliberately separates implementation evidence that
can be accepted before merge from the two operations that require GitHub's
Linux/release environment. A tagged-release rehearsal, a named GNOME machine,
and physical install/upgrade/uninstall evidence can be useful release QA, but
they are not additional Issue #20 development-completion criteria.

## Local development acceptance

| # | Original Issue criterion | Local evidence | Decision |
|---|---|---|---|
| 1 | Select and document one initial Tauri Linux format | Ubuntu 24.04 x86_64 `.deb` is fixed in the product doc, runbook, Tauri command, CI, and release workflow | Pass |
| 2 | Bundle the Ryn node sidecar and webapp without requiring system Python or Node.js | PyInstaller `--onefile` embeds `webapp/dist`; Tauri has exactly one `externalBin`; standalone verification removes source fallbacks; Debian verifier rejects dynamic Python/Node linkage | Pass at implementation/contract level; final Linux execution belongs to release gate #3 |
| 4 | Verify sidecar architecture matches package architecture | Build names the sidecar from the Rust host triple; CI requires `x86_64-unknown-linux-gnu`; Debian verifier requires `amd64` plus x86-64 ELF shell and sidecar | Pass at implementation/contract level; executed by release gate #3 |
| 6 | Document installation, update behavior, system requirements, and known limits | `docs/LINUX_DESKTOP.md` and README cover checksum, install, update, uninstall, retained data, supported system, paths, and limits | Pass |
| 7 | Preserve existing macOS release verification | Both Intel and Apple Silicon compile/release jobs, DMG build, codesign verification, and sidecar verification remain wired; contract tests protect them | Pass |

The branch therefore meets the **local development acceptance** for all five
criteria that can be established from source, deterministic tests, and workflow
contracts. This means the implementation is development-complete; it does not
claim that an unpublished Linux package has already passed GitHub CI or exists
on a GitHub Release.

## Original Issue release acceptance still required

| # | Original Issue criterion | Required external evidence | Status |
|---|---|---|---|
| 3 | Verify daemon startup, health, UI serving, clean shutdown, and restart in CI | A successful `linux-desktop-package` GitHub Actions job for the candidate commit. Its installed-package smoke must show health, `/`, `/digest`, child replacement, SIGTERM cleanup, and no orphan. The same job also executes the architecture and runtime-closure verifiers from #2/#4. | Pending CI run |
| 5 | Publish checksums and the Linux artifact through the existing release workflow | A successful `linux-desktop` release job and GitHub Release assets containing `Ryn-<version>-linux-x86_64.deb` and its `.sha256`. | Pending release |

These are the only two remaining Issue-level acceptance operations. The
workflow implementation for both is present and locally contract-tested, but a
local Windows run cannot truthfully substitute for GitHub's Ubuntu runner or a
GitHub Release publication.

## Local verification evidence (2026-09-03)

| Check | Result |
|---|---|
| Original-criteria contract tests | Pass: `tests/test_linux_desktop_artifacts.py`, 8 tests |
| Shell syntax | Pass: Git Bash `bash -n` on all four desktop scripts |
| Workflow YAML | Pass: both CI and release workflows parsed with `yaml.safe_load` |
| Tauri configuration discovery | Config recognized: `npm run tauri -- info` resolves bundle mode, `frontendDist`, Tauri/plugin versions, and WebView2; it also correctly reports that Rust/MSVC are absent |
| Web tests/typecheck/build | Pass: 9 files / 38 tests; `npm run lint`; production build with 1,739 modules |
| Python lint | Pass: `ruff check rynmesh tests` |
| Portable Python regression | Pass: 529 passed, 3 skipped, 6 explicitly deselected Windows-inapplicable tests |
| Raw full Python regression | Environment-limited: 529 passed, 3 skipped, 6 failed; failures are two POSIX executable-bit assertions, three POSIX `0600` mode assertions, and one Windows-incompatible `select()`-on-pipe MCP test, none in Issue #20 code |
| Rust compile/unit tests | Not executable locally: this host has no Rust/MSVC toolchain |
| Linux `.deb` build/inspection | Not executable locally: Docker Desktop is installed but its Linux engine did not become available; WSL contains only the stopped internal `docker-desktop` distribution; no Ubuntu distro or Debian/Rust tools are installed |

No system-level software was installed to manufacture a result. Static checks
prove that the configuration and workflow are closed; they do not claim an ELF
binary, Debian package, installed shell, or Linux process was run locally.

## Reproduction of the two release gates

For candidate commit `<sha>`, preserve the Actions URL and logs for its normal
CI run. After the project performs its normal release, preserve the release URL
and asset listing:

```bash
gh run view <ci-run-id> --log
gh release view <tag> --json tagName,assets,url
```

The release mechanism happens to be tag-triggered today. That is an
implementation detail of the existing workflow, not a new eighth acceptance
criterion.

## Completion decision

**Development complete; release acceptance pending two external operations.**
Criteria #1, #2, #4, #6, and #7 are locally accepted. Close Issue #20 only after
the CI evidence for #3 and published release evidence for #5 both succeed.
