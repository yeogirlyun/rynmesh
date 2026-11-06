# Full Windows package and two-host sharing — 2026-11-03

Scope confirmed by the user: seven installed sidebar apps, Excel Chinese glyphs
and blank-input formulas, existing friend/personal-space AI sharing, installation
on the local desktop and LAN Windows 82. Company-group AI sharing is excluded.

## Integrated sources

- Core: `5cf8073c83b2aeb79d047b89689d40a28c772ca8` on develop.
- Installer: `ed64351131198951d14ffe059bd61a9d65a56903` on develop.
- Company: `45e5d07c5b8d83b2ee12310ecba51eb04e370a5d` on the company repository's develop.
- Excel: `gridcraft-web-0.6.1-ryn-cjk-blank-2.zip`, SHA256
  `8db12ff172d14c18c784f965da4aa86eae6388ece2d559eba1cf9d15878e36ef`.

The core was rebuilt from committed source. Company binary and all manifest
entries were verified against the already built committed release. Current PDF,
Word and Excel Chinese compatibility resources are mandatory in the full payload.
No credentials, databases or user settings are included in the installer.

## Validation

- 42 focused installer, compatibility and LAN-discovery/backend checks passed.
  An initial pytest invocation completed its cases but failed while cleaning an
  old inaccessible temporary-directory link; the explicit isolated-temp rerun passed.
- 14 sidebar/system-app frontend tests passed; frontend and Windows native build passed.
- 15 installer tests passed after adding damaged-resource recovery, including
  three cases covering original web resources, compatibility resources and native
  engines, preservation of user files, rollback copies and idempotent upgrades.
- Frozen helper passed fresh and repeated installation: all four office editor
  pages and native version commands, all three company pages, company readiness,
  retained identity/key and retained disabled-plugin choices. Company outbound
  networking was blocked in this isolated check.
- Local installed desktop visibly showed the seven sidebar apps. A synthetic XLSX
  opened through the real Ryn Excel launcher rendered Chinese, blank IF results,
  SUM 200 and division 60. Selection of 120 and 80 showed Average 100 / Count 2 /
  Sum 200. `excel-local.jpg` contains only this synthetic workbook.
- LAN 82's authenticated consumer catalog verified personal-space bindings.
  A structured chat request used the discovered binding and returned
  `RYN_SHARE_OK` from the local desktop's DeepSeek service. Transport evidence:
  `peer_http_direct`, route/discovery `lan`, relay unused; provider duration 1172 ms.
  The first test harness incorrectly supplied the bare model binding instead of
  the full recipient/space binding and was rejected. A subsequent legacy prompt
  probe with a 32-token limit failed; the successful chat probe used the actual
  structured-chat contract and a 256-token limit. No authorization bypass was used.

## Upgrade findings

The initial R4 local install updated the core but failed during plugin provisioning.
The Codex MSIX filesystem overlay resolved the original cache's receipt into a
different physical directory than its payload, failing the integrity check. The
ordinary desktop-user installation environment avoids that mixed view. It was
used to restore the local installation and is used for final local acceptance.

The installer also now handles genuinely damaged existing program caches: it
stages verified replacement files, retains previous program resources under
`.full-repair-backups`, and replaces only named bundled resources. Preferences,
exported workbooks, registration and other compatibility versions remain intact.
The linked/escaping resource check stays enforced; the MSIX mismatch was not
worked around by weakening integrity or path validation.

Both hosts' initial upgrade checks retained node identity, messaging key, company
key and settings. The local deployment configuration also remained byte-identical.
Core rollback copies are in each installation's sibling `Ryn-install-backups`.

## Final R5 delivery

Both complete system installers returned exit code 0. The final local installation
is `C:/Users/子陵/AppData/Local/Ryn`; LAN 82 uses
`C:/Users/ziling/AppData/Local/Ryn`. The local ordinary-user registry selected this
normal directory, while the Codex registry view still pointed to the previous
MSIX cache directory. After identifying that difference, the original deployment
configuration was preserved byte-for-byte in the normal installation, the four
verified old-directory Ryn/peer/company processes were stopped, and the new normal
installation was started. No old-directory Ryn service remained at final inspection.

Both final installations report node `ok`, company `ready`, four office editors
installed/enabled/editable, and three company pages returning HTTP 200. Identity,
messaging key, company key, settings and the local deployment configuration match
the pre-upgrade values. Temporary scheduled verification tasks were removed.

Both installed sidecars exactly match the built sidecar. Both installed desktop
executables match the built desktop with the three-byte Tauri NSIS bundle marker
changed from `UNK` to `NSS`, as indicated by the packaging step; all remaining bytes
match. The final post-R5 shared chat returned `RYN_SHARE_OK` over LAN direct with
provider duration 921 ms.

Artifact: `output/full-package-20261103-r5/Ryn_0.8.0_Full_7Plugins_R5_x64.exe`,
672,140,732 bytes. SHA256:
`D19009E4CC3A2A32281C957109466DF99A85672AE228EEA947DEDCF0C6741965`.
Final R5 installer receipt and final two-host results are recorded in `result.json`.
The installed product version remains 0.8.0; R5 and source/hash receipts identify
this complete package. Existing personal-space sharing was exercised; cross-network
relay, company-group sharing and live messages to colleagues were not tested.
