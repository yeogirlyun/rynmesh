# Office downloaded after install instead of bundled — 2026-11-09

Branch `claude/office-download-20261109`, cut from `develop` 57b807f. Checked on the Claude VM; not run on the host.

## Why

Office is about 900 MB of the 1.1 GB complete-installer input (company plugins about 210 MB). Reinstalling the core keeps all plugins: the Tauri uninstaller deletes only its own executables and removes `$INSTDIR` only when empty, and the Office cache (`%LOCALAPPDATA%\Ryn\plugins`), `plugin-services` and `~/.rynmesh` are left alone. Ryn can already download the Office editors after installation from `github.com/storytold`. What was missing was the Chinese-font builds: upstream web builds deliberately embed no Chinese font (`crates/fonts/build.rs` in each upstream repository), and Ryn installed its own builds only from local files.

## Changes

- `craft_compat.py`: `download_install` fetches the pinned build from `https://github.com/yeogirlyun/rynmesh/releases/download/office-cjk-1/<slug>-web-<version>-<id>.zip` and installs it through the existing checks. `applies` limits this to the upstream release each build was made for.
- The original build archives were not kept. The release assets were repacked from verified installs (`.codex-tmp/full-qa-42016de5`), so each spec now also pins `release_sha256`. Both archives extract to exactly the pinned `files_manifest_sha256`; receipts record the archive actually used, and existing installs from the original archives still verify.
- `PluginManager.install`: after the upstream editor, also installs the Chinese-font build. A failed download fails the install (the editor stays uninstalled and can be retried) instead of showing missing-glyph boxes.
- Complete installer: no Office payload. `prepare_full_package.py`, `full_package_install.py` and `qa/full_package_acceptance.py` handle the company plugins only. The helper no longer marks plugin setup complete, and the installer still records the four Office editors as selected, so Ryn opens Apps on first start with them selected for download.
- Error messages for `craft_compat_checksum_mismatch` and `craft_compat_not_verified` (zh-CN, en).

## Release assets

Files are in `artifacts/office-cjk-1/` (not committed) with upload steps. Publish them as a **pre-release** with tag `office-cjk-1`: the desktop updater reads `releases/latest` of the same repository.

| Asset | Bytes | SHA-256 |
|---|---|---|
| deckcraft-web-0.6.1-ryn-cjk-1.zip | 20,419,339 | 01ef3bf81754c4d3049fc8f3ca912c71d7c8f22cef755ffee7b0387eb4818505 |
| gridcraft-web-0.6.1-ryn-cjk-blank-2.zip | 21,075,119 | 460cf42ca6ada606582b1af92ec014bc894481078bf021e1dc13862945682351 |
| wordcraft-web-0.6.1-ryn-cjk-1.zip | 31,978,208 | 190f7dbb3d86c74cbe4f37dd80c11191e9754e3d3da88869a48b7e401c95b13f |
| pdfcraft-web-0.7.1-ryn-cjk-1.zip | 32,075,224 | bf7267ff689a884aa4831f2a863ee6bf0c615a4ff6b669e6b1cff8f00a3f21a5 |

## Verification

- Each asset was installed with the real pins on top of the real upstream editor install, through `download_install` with the download served from the local file. `craft_host.installed` then selects the Chinese-font build. The original installed builds still verify with the new code.
- New tests: release download, tampered download, upstream-first rule, two archive pins, manager install fetches the build, failed download leaves the editor uninstalled until retried, packager stages no Office.
- `test_craft_compat.py`, `test_full_package_install.py`, `test_craft_plugins.py` and nine other plugin/packaging suites pass. `test_agent_plugin_discovery.py::test_current_apps_first_menu_and_discovered_info_use_live_read_only_tools[12000]` fails with `agent_compaction_failed` on unchanged `develop` 57b807f as well.

## Not verified

- A real download from GitHub: the assets are not published yet. Until they are, installing an Office editor with this Ryn fails.
- A complete installer built and installed without Office, and the first-start Apps download on a real desktop.
- Download speed from mainland China without a VPN.
- The agent document tools (`*-cli.exe`, 50-120 MB each from storytold) are still a separate "install" button per editor; the complete installer used to include them.
