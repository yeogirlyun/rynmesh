# Ryn Excel Chinese glyph fallback

The pinned GridCraft web release reads Chinese workbook text but has no Chinese font.
Its `wasm32` font fallback returns an empty list. WPS uses installed system fonts;
the browser WASM build cannot discover those fonts through GridCraft's native code.

This compatibility build changes only `crates/ui-egui/src/theme.rs` in GridCraft
v0.6.1. The WASM fallback embeds an unmodified OFL font from an external build-input
directory. It participates in the existing font-definition factory, including the
default families, so opening a workbook or rebuilding fonts retains Chinese glyphs.
It keeps the existing menu language and does not change workbook imports, formulas,
values, styles or saved workbooks. The single Regular fallback does not reproduce
Microsoft YaHei's exact metrics or its bold and italic faces.

## Fixed inputs

| Input | Pin |
| --- | --- |
| GridCraft source | `fc45b7f6d8ab79ef20f74e3c8347a2fd731e6af5` (`v0.6.1`) |
| Original web archive SHA-256 | `86048ba175689ff852c141d6115e5491f0856f891300c3e1f91274b9c2709a42` |
| Noto CJK source commit | `f8d157532fbfaeda587e826d4cd5b21a49186f7c` |
| Noto Sans CJK SC Regular SHA-256 | `2c76254f6fc379fddfce0a7e84fb5385bb135d3e399294f6eeb6680d0365b74b` |
| Font OFL license SHA-256 | `6a73f9541c2de74158c0e7cf6b0a58ef774f5a780bf191f2d7ec9cc53efe2bf2` |
| wasm-bindgen CLI | `0.2.129` (matches source Cargo.lock) |
| Reviewed patch SHA-256 | `12817a0632aede415363dbd5c07b823dcd74e11d54e291fd2520b615ffce7d09` |

Sources: [GridCraft source](https://github.com/storytold/gridcraft/tree/fc45b7f6d8ab79ef20f74e3c8347a2fd731e6af5),
[original web archive](https://github.com/storytold/gridcraft/releases/download/v0.6.1/gridcraft-web-0.6.1.zip),
[font](https://raw.githubusercontent.com/notofonts/noto-cjk/f8d157532fbfaeda587e826d4cd5b21a49186f7c/Sans/OTF/SimplifiedChinese/NotoSansCJKsc-Regular.otf),
[font license](https://raw.githubusercontent.com/notofonts/noto-cjk/f8d157532fbfaeda587e826d4cd5b21a49186f7c/Sans/LICENSE).

## Rebuild

Use an exact checkout at the source commit above and a font folder outside that
checkout. The builder requires Python 3.10+, Git, Rust with the
`wasm32-unknown-unknown` target, and wasm-bindgen CLI 0.2.129. It installs no tools.

```powershell
python scripts/craft_gridcraft_cjk.py prepare --source <checkout> --font-dir <external-font-folder>
python scripts/craft_gridcraft_cjk.py build --source <checkout> --font-dir <external-font-folder> --wasm-bindgen <wasm-bindgen.exe> --dist <new-empty-dist> --output <archive-folder>
```

`prepare` verifies the checkout, downloads and verifies the font/license if absent,
and applies the exact patch idempotently. Unexpected tracked changes are rejected.
`build` sets `CRAFT_FONTS_DIR` to an absolute path and runs Cargo with `--locked`.
It runs wasm-bindgen with `--target web --no-typescript`; it does not require Trunk.
The upstream generated web bootstrap is reused from the hash-verified original web
archive. Only the JS/WASM filenames and their expired link integrity attributes
change; the favicon and its integrity metadata remain original.

For an already compiled WASM, set `CRAFT_FONTS_DIR` before the Cargo invocation, then:

```powershell
python scripts/craft_gridcraft_cjk.py assemble --source <checkout> --font-dir <external-font-folder> --wasm <gridcraft-web.wasm> --wasm-bindgen <wasm-bindgen.exe> --dist <new-empty-dist>
python scripts/craft_gridcraft_cjk.py package --source <checkout> --font-dir <external-font-folder> --wasm-bindgen <wasm-bindgen.exe> --dist <assembled-dist> --output <archive-folder>
```

`assemble` downloads the original web ZIP beside the checkout if it is absent; use
`--base-web-zip` to choose a different cache path. Existing inputs must match their
pins. Existing distribution files are never replaced by assembly.

## Package contract

The ZIP contains exactly one `index.html` under
`gridcraft-web-0.6.1-ryn-cjk-1/`. It retains upstream `LICENSE-MIT`, `LICENSE-APACHE`,
`NOTICE`, `ATTRIBUTION.md` and adds the OFL license, source patch, provenance JSON and
per-file checksums. Packaging rejects a WASM without the exact embedded font bytes.
It uses sorted entries, fixed timestamps and permissions. Identical distribution
files and recorded tool versions produce the same archive bytes.

The command prints the compatibility identity `ryn-cjk-1`, base version and archive
SHA-256, plus the host's payload-manifest digest. That digest is SHA-256 of UTF-8
JSON with `sort_keys=True`, `separators=(',', ':')`, `ensure_ascii=True` for:

```json
{"entry":"gridcraft-web-0.6.1-ryn-cjk-1","files":{"gridcraft-web-0.6.1-ryn-cjk-1/index.html":"<sha256>","...":"..."}}
```

Every ZIP payload file, including notices and provenance, is included in `files`.
`CJK-PROVENANCE.json` itself records hashes of application files and the source
patch; `SHA256SUMS.txt` also covers that provenance file. The archive checksum is
published beside the ZIP. Full compiler-level reproducibility requires the same
Rust, wasm-bindgen and linker toolchain; the archive records the available versions.

## Completed build and browser acceptance

The real Cargo release build completed successfully in 7 minutes 16 seconds with
`--locked --target wasm32-unknown-unknown --package gridcraft-web`. Manual binding
generation with wasm-bindgen CLI 0.2.129 also succeeded.

The resulting local archive is
`.codex-tmp/gridcraft-cjk-20261018/output/gridcraft-web-0.6.1-ryn-cjk-1.zip`.
Its size and both digests were independently recomputed from the actual ZIP:

| Output | Verified value |
| --- | --- |
| Size | `21,073,960` bytes |
| Archive SHA-256 | `beebcc8c9467845f4c2e404d64cf33c2cf2e08cec3d59f5bfdb43d36baced3c0` |
| Payload-manifest SHA-256 | `8a82fd16ada4265ba906dfdb17f5dee78de5a3751d0492127093eff5b0ce783c` |

The compatibility editor was served locally on port 18926 and opened the user's
original workbook. Chinese text rendered correctly in the filename, all four
worksheet tabs and the visible worksheet body. The saved visual evidence is
local screenshot `chinese-fixed.png`. This screenshot and the user's workbook are
retained locally and excluded from Git. The committed evidence consists of this
report, the reviewed source patch and the builder check summary.

The final focused test run passed all 30 tests in 5.72 seconds.

## Installed desktop acceptance

The ordinary Windows installation was updated and verified outside the Codex Store
filesystem redirection. Only `craft_host` was changed in its Python archive and
`craft_compat` was added; the other 2,073 existing PYZ modules, 217 non-PYZ archive
payloads and the PE bootloader bytes were preserved exactly. The installed sidecar
SHA-256 is `6d0bebe8361ee155260038ff406af48e30d36374fb6e4650413096ffa6336db6`.
The previous sidecar is retained as `rynmesh-peer.before-cjk-20261018.exe` beside
the installed executable.

Before activation, desktop and agent APIs showed no running or queued AI tasks
and no model setup in progress. Ryn's owned sidecar tree was restarted; the main
desktop process remained running and recovered a healthy node. The compatibility
bundle was installed beside the original release, whose payload and receipt were
verified unchanged. Port 8926 served the fixed bundle after the update.

The original workbook was reopened through the installed Ryn Excel launcher.
The native window displayed the Chinese filename, title, instructions and four
worksheet labels correctly. The screenshot `native-chinese-fixed.png` is retained
locally and excluded from Git because it shows the user's original workbook.
No workbook save or content edit was performed. Source workbook SHA-256 observed
after acceptance: `043ddba974afb934f64468dfff8487f6c01db1218359bbbcb4ccd40a4521e739`.

### Separate observed formula limitation

The monthly summary's G4:G9 show `#VALUE!` after recalculation in upstream
GridCraft. These cells use `IF(B4=0,"",C4/B4)` (copied down); their blank inputs
are serialized as empty `inlineStr`. The pinned XLSX reader imports them as
`Text("")`, so the numeric-zero guard is false and division fails. The font patch
does not change the XLSX reader, comparison or calculation code. This is an
independent compatibility issue and remains unresolved by this font repair.

## Builder verification

Checked syntax, exact and idempotent source patch, pinned font and license hashes,
deterministic ZIP packaging across two output directories, the host's manifest
digest calculation, preservation of required notices, rejection of duplicate HTML
entries, and rejection of a WASM fixture lacking the pinned font.

The initial packaging checks used a synthetic distribution fixture. They are
supplemented by the real build, archive digest verification and browser acceptance
above. The builder does not itself modify installed payloads, `craft_catalog.py`,
`craft_host.py` or user files.
