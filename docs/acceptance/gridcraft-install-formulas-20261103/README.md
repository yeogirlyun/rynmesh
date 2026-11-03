# GridCraft installation and empty-input formula repair — 2026-11-03

The full Windows package now requires the current pinned Office compatibility
builds. Missing or corrupt font bundles fail packaging/provisioning, and upgrade
preflight checks them before stopping or uninstalling the old app. Upgrades copy
the current compatibility directory even when the upstream version already exists;
existing user settings, enable/disable state and original release files are kept.
A core-only update still accepts the previous verified GridCraft CJK build until
the new package arrives; full installers require `ryn-cjk-blank-2`.

The GridCraft compatibility builder embeds the same pinned OFL Chinese font and
applies `scripts/patches/gridcraft-empty-inline.patch`. Only empty literal XLSX
`inlineStr` inputs become `Value::Empty`. Whitespace, numeric-looking strings,
shared strings, and formula-produced empty strings retain their existing types.
This fixes the known `IF(A1=0,"",B1/A1)` error without coercing identifiers into
numbers. The patch includes Rust reader regression tests.

## Validation

- 134 XLSX reader/writer tests passed, including the new empty-inline regression.
- `scripts/check_gridcraft_formulas.py` reproduced the error with the pinned old
  CLI and passed with the newly built CLI: IF guards, SUM, numeric division,
  selection statistics, Chinese text, leading zeros, spaces and empty formula
  results. Save followed by a fresh-process reopen also passed. See `formulas.json`.
- Actual pinned archives were installed in isolated fresh and existing caches;
  repeated upgrade retained the original receipt and preferences. See `install.json`.
- Chromium loaded the real installed WASM through `craft_host`, with no page
  errors. Chinese text rendered correctly; selecting 120 and 80 displayed
  Sum 200 / Count 2 / Average 100. See `browser-selection.png`.
- 45 focused Python tests passed for installer, package validation and compatibility
  host, including rejection before process shutdown and legacy CJK fallback.

## Build and delivery

- Upstream: `fc45b7f6d8ab79ef20f74e3c8347a2fd731e6af5` (0.6.1).
- Rust 1.98.1; wasm-bindgen 0.2.129; locked release WASM build succeeded.
- Archive: `gridcraft-web-0.6.1-ryn-cjk-blank-2.zip`, 21,075,119 bytes.
- SHA-256: `8db12ff172d14c18c784f965da4aa86eae6388ece2d559eba1cf9d15878e36ef`.
- Canonical payload digest: `e15340cd0046a6852652a02170b561733f33ca9a6ede3a22d6ebfa204601738c`.
- Generated archive remains outside Git in `D:/codex-temp/gridcraft-excel-output-20261103/`.

Rebuild with `scripts/craft_gridcraft_cjk.py build` using the pinned upstream
checkout and external font inputs. Pass the resulting archive folder to
`prepare_full_package.py --office-compat-dir <folder>`; archive names follow
`APP-web-VERSION-COMPAT_ID.zip`. A verified current compatibility cache also works.
The ordinary core and full installer must be rebuilt from the integrated commit
before distribution. The existing R3 installer and the user's running installation
were not overwritten. The separate upstream native automation binary is unchanged;
the tested rebuilt CLI is an acceptance harness, while the shipped repair targets
the interactive WASM spreadsheet editor.

The original screenshot workbook was not available. This acceptance uses a
synthetic workbook and addresses the confirmed blank-input compatibility defect;
it does not assert that all Excel formulas or text-number imports are repaired.
