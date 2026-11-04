# DeckCraft Chinese text rendering

Opened the same synthetic PPTX in Chromium via Ctrl+O on the original and repaired
DeckCraft 0.6.1 web builds. The original showed missing-glyph boxes in the filename,
slide and thumbnail. The repaired build renders simplified and traditional Chinese
in all three locations. See before.png and after.png. The ribbon is in English in
both captures. Word, Grid and PDF have separate pinned compatibility builds.

## Rebuild

Use Rust 1.98.1, wasm32-unknown-unknown and wasm-bindgen CLI 0.2.129.
Check out storytold/deckcraft at 688c7ef592c901ca4541c25e210c1b302e4dc066.
Apply the repository patch scripts/patches/deckcraft-cjk.patch. Supply the release's
storytold/craft-fonts manifests at 43913056ce82a3fdad0ef69f6b88d1dc922a8104
and the Inter Regular, Inter Bold and BIZ UDPGothic Regular files they reference.
Add LXGWWenKaiTC-Regular.ttf from google/fonts at
590eaaa6aa9447b4b7165d7768511a3ace35af5a (ofl/lxgwwenkaitc) beside those
manifests' parent fonts directory. Append the following line to fonts/manifest.txt.

    LXGW WenKai TC | Regular | LXGWWenKaiTC-Regular.ttf | Hans,Hant,Latn

Set CRAFT_FONTS_DIR to this craft-fonts root and CRAFT_FONTS_REQUIRED=1. In the
patched DeckCraft checkout run the following commands.

    cargo build --release --locked --target wasm32-unknown-unknown -p deckcraft-web
    wasm-bindgen --target web --no-typescript --out-name deckcraft_web --out-dir <dist> target/wasm32-unknown-unknown/release/deckcraft-web.wasm

Package against the pinned upstream release archive using the Ryn packager.

    python scripts/package_deckcraft_cjk.py <upstream.zip> <dist> <LXGWWenKaiTC-Regular.ttf> <OFL.txt> <output>/deckcraft-web-0.6.1-ryn-cjk-1.zip

The packager checks the upstream ZIP, the font embedded in WASM and the accompanying
OFL, then updates the shell's asset URLs and integrity attributes. The ZIP and
canonical manifest pins are in craft_compat.py. Full installer preparation accepts
the ZIP via --office-compat-dir and requires its verified payload.
