# macOS installer: local CLI discovery

The desktop runs the frozen `rynmesh-peer` bundled inside `Ryn.app`. CLI discovery
belongs to that daemon, not to the web preview or a user shell startup script.
Rebuild the sidecar and the DMG together to deliver this change.

Discovery preserves explicit executable settings and PATH precedence. On macOS,
when the default CLI is absent from PATH, it asks macOS Launch Services for the
actual application URLs associated with OpenAI's bundle identifiers. This works
with renamed apps and apps outside Applications; no absolute app install path is
assumed. The CLI is located relative to the discovered bundle's Resources folder.
Homebrew, `/usr/local/bin`, `~/.local/bin`, `~/.npm-global/bin`, and `~/.volta/bin`
remain fallback command search directories for standalone CLI installations.
Candidates must be executable files. Ryn does not install Codex or copy its login
credentials into the installer. Detection is separate from login/model testing.

## Build and verify

- Apple Silicon: `sh webapp/src-tauri/scripts/build-macos-arm64.sh` rebuilds the
  sidecar, desktop app and DMG. Output:
  `release/macos/Ryn-0.6.2-cli-discovery-macos-arm64.dmg` plus SHA-256 checksum.
- Release CI also builds Apple Silicon and Intel DMGs, then mounts each DMG and
  runs `verify-sidecar.sh` against the daemon **inside the mounted installer**.
- `verify-sidecar.sh` now uses a temporary HOME and minimal GUI-style PATH. On
  macOS it places a fake Codex executable in the temporary user's `.local/bin`
  directory and checks `/api/local/llm/cli-services` for `installed: true`.
  This checks packaged discovery, not real login or inference.

## Frozen worker compatibility

The private CLI environment intentionally excludes the owner's environment. For
the bundled Python worker only, retain the parent bootloader's existing `_PYI_`
context so `sys.executable` reuses its extracted runtime. Remove these variables
before executing the external CLI. This prevents a second runtime extraction
from consuming the CLI session's 64 MiB temporary-storage budget. Limits and
account/config isolation are unchanged. See the [PyInstaller worker environment
documentation](https://pyinstaller.org/en/v6.22.3/advanced-topics.html#private-environment-variables).

The actual Mac CLI also creates `apply_patch`, `applypatch` and an exec-wrapper
symlink to its 235 MB installed executable. The old `stat()` sum charged that
external executable three times to the private directory. Temporary storage now
counts entries without following links (including directory links/reparse
points); actual generated files still count toward the unchanged 64 MiB limit.

## Validation status — 2026-09-24

- Windows and native Apple Silicon: 39 CLI/worker/privacy tests passed, including
  registry lookup, a renamed application outside Applications, non-executable
  rejection, explicit-path precedence and private worker environment boundaries.
- With a minimal PATH, native discovery resolved the user's installed ChatGPT
  bundle through Launch Services. The first mounted-DMG test found the real CLI
  but exposed the storage-accounting issue above. The corrected source on the
  actual Mac successfully read seven models.
- Final DMG mounted read-only and its own bundled daemon started on a separate
  loopback port with minimal PATH: real Codex detected and model listing returned
  seven models. App signature and arm64 binary verification passed. No installed
  Ryn app was replaced or restarted; concurrent Mac work was left running.
- Delivered to the Mac desktop and
  `release/macos/Ryn-0.6.2-cli-discovery-macos-arm64.dmg` (31,500,400 bytes).
  SHA-256: `80aa9e3d093290a0b41122411c4a2ecd1b8598cdd0c143bba642415fb734b4ed`.
  The transferred local copy matches the Mac build's checksum. Model discovery
  was tested; this acceptance did not submit a paid inference request.

## Installed on the user's Mac

At the user's subsequent request, replaced `~/Applications/Ryn.app` with the
verified DMG's app and restarted that installed instance. The previous app is
retained at `~/ryn-builds/desktop-backups/Ryn-before-cli-1790235002.app`; node
configuration and account data were not replaced. The installed node on port
8791 reports `codex_cli.installed=true` and returns seven models. CLI service
configuration remains disabled (`configured=false`); installation did not
enable sharing or change the user's AI access policy.
