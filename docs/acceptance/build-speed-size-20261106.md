# Windows packaging time and size — acceptance, 2026-11-06

Branch `claude/build-speed-size-20261106`, cut from `develop` d5bc19a. Measured on the Claude VM (4 vCPU, 6 GB RAM, worktree on local disk). The host build at `D:\code\rynmesh` was not re-run.

## Why packaging was slow and large

From the 11-06 release logs (`artifacts/develop-release-20261106-90228de`):

- Every build ran `npm ci`, which deletes `node_modules` and reinstalls it (53 s to 3 min).
- The frontend was built twice: once by the script, once by Tauri's `beforeBuildCommand`.
- The Tauri shell embeds the web UI, so the `app` crate recompiled in full on every release even without Rust changes (2-5.5 min on the host).
- The native P2P transport was rebuilt by maturin on every build.
- setuptools reuses `<repo>/build/lib` and never deletes files that left the source tree. Old web UI bundles from that folder shipped inside `rynmesh-peer.exe`; the wheel was 16.3 MB.
- The node bundled Tk/Tcl (930 files), which only `rynmesh/windows_app.py` uses.
- The complete installer's helper was frozen from the developer `.venv`, which pulled numpy and test tools in. It took about 10 minutes on 11-06.
- Each acceptance run left a ~1 GB install under `.codex-tmp/full-qa-*`.
- Codex switched the complete installer from LZMA to zlib on 11-06 for speed: 700 MB (11-05) -> 860 MB.

## Changes

`webapp/src-tauri/scripts/build-windows.ps1`

- `npm ci` runs only when the lockfile, npm or node changed (stamp in `node_modules`); `-CleanInstall` forces it.
- `tauri.prebuilt-frontend.conf.json` clears `beforeBuildCommand`, so the frontend is built once.
- `build/lib` and `build/bdist.win-amd64` are removed before the wheel build.
- PyInstaller excludes `tkinter`/`_tkinter` for the node.
- The P2P transport is reinstalled only when its source hash changes (or with `-CleanInstall`).
- The Tauri step runs with `CARGO_INCREMENTAL=1`.

`webapp/tsconfig.app.json`: `"incremental": true`.

`scripts/build_full_windows.ps1`

- The helper is frozen with the release venv `.codex-tmp/windows-package/venv`. `python -m` keeps the repository first on `sys.path`, so rynmesh still comes from HEAD.
- The helper is reused when its inputs are unchanged: script hash, `HEAD:rynmesh`, `pip freeze`, Python version and arguments. Untracked files under `rynmesh/` always force a rebuild.
- The QA install is deleted after a passing run; a failed run keeps it.
- `-Compression lzma|zlib`. lzma now uses a 128 MB dictionary.

Tried and not kept: dropping the mobile-only `staticlib`/`cdylib` crate types. Re-timed fairly, it was not faster (3:43 vs 3:04).

## Results

Core build, VM, all changes, after a small web UI edit (`.codex-tmp/build-run3.log`): **4 min 34 s**. Earlier memory notes record 10-13 min for an incremental build of the old script on this VM.

| Step | Before | After |
|---|---|---|
| `npm ci` (webapp + chrome runtime) | 53 s - 3 min (host logs) | skipped |
| Second frontend build | 1.5 s vite + tsc (host) | removed |
| Tauri `app` crate | 3:04-4:26 (VM), 2-5.5 min (host) | 0:43 (VM, web UI changed) |
| P2P transport | 2:07 (VM, path change), <1 min (host) | skipped when unchanged |
| Type-check, frontend unchanged | 66 s | 0.4 s |

Type-check after editing a widely imported file (`src/i18n.ts`) still takes about 60 s.

Sizes:

| Artifact | Before | After |
|---|---|---|
| rynmesh wheel | 16.3 MB | 10.2 MB |
| `rynmesh-peer.exe` | 60.5 MB (host, 11-06) | 50.3 MB |
| Node bundle entries | 1,671 | 661 |
| Core installer `Ryn_0.8.0_x64-setup.exe` | 67.6 MB (11-06) | 58.0 MB |
| Installer helper | 121.8 MB / 1,578 files | 84.5 MB / 553 files |
| Installer helper build | ~10 min (host, 11-06) | 1:26 (VM) |

Complete installer on the VM, same inputs (new core and helper, test payload below):

| Compression | Time | Size |
|---|---|---|
| zlib | 3.6 min | 790 MB |
| lzma, 128 MB dictionary | 20.4 min (3.6 min of it shared the CPU with the zlib run) | 500 MB |

The 11-06 release was 860 MB (zlib); 11-05 was 700 MB (lzma, 8 MB dictionary). Use `-Compression zlib` for test builds and the default lzma for the build people download.

makensis on the Word CLI+GUI pair alone (176.5 MB): zlib 21 s / 89.7 MB, lzma 8 MB 124 s / 66.6 MB, lzma 128 MB 112 s / 37.2 MB.

## Verification

- `tests/test_computer_use_packaging.py` and `tests/test_linux_desktop_artifacts.py`: 16 passed.
- `build-windows.ps1` end to end on the VM: exit 0 (twice).
- New `rynmesh-peer.exe` in an isolated `RYNMESH_HOME`: `/health` ok after 14 s; `/` serves the current `index-*.js`.
- `qa/full_package_acceptance.py` with the new helper: fresh install, repeat install keeps identity, key and disabled plugins; 4 Office editors and CLIs; 3 company pages; company service ready; outbound network blocked.
  - Test payload: the 11-03 r7 company bundle plus the complete Office set from the 11-06 QA run `full-qa-42016de5`, with the finance OAuth `client_id` set by the current `prepare_full_package.py` rule and the manifest regenerated. The 11-03 payloads alone fail the current compatibility checks.
- makensis with both compressor settings. This found a bug before merge: a one-item argument list was splatted as single characters, so `-Compression zlib` printed the makensis usage. Fixed in 89dae10.

## Not verified

- Host timings, and a Codex release run of `build_full_windows.ps1` as a whole. The script needs `develop` checked out and git refuses to run in the VM worktree.
- The helper reuse path (stamp hit).
- Installing an LZMA-128 complete installer on a machine. Solid LZMA unpacks more slowly than zlib at install time.

## Open size decisions

- Offline WebView2 installer: 206 MB, already compressed, so ~40% of the 500 MB installer. Windows 11 ships WebView2, the installer only runs it when WebView2 is missing, and the core installer already uses the download bootstrapper.
- Office desktop GUI programs: about 310 MB raw. Ryn only runs `*-cli.exe`, but runtime verification pins the complete upstream archive. With the 128 MB lzma dictionary each GUI adds only ~5 MB next to its CLI, so dropping them is not worth the verification change.
- `frida` in the company service: 162 MB raw. Used on purpose (`collect_all('frida')`, WeCom native send); company repository.
- About 13 old `.codex-tmp/full-qa-*` folders (~1 GB each) remain on the host; not deleted.
