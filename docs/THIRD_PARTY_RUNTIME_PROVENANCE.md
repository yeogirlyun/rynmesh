# Browser Runtime, WASM, and Native Binary Provenance (`codex/plugin-host-delivery-20261023`)

This document records the upstream origin, version, cryptographic digest, license, and redistribution basis for the browser control runtime, WebAssembly modules, and native binaries included in or referenced by Rynmesh.

---

## 1. Summary Inventory

| Path / Component | Kind | Version | SHA-256 (`size`) | License | Redistribution Basis & Runtime Role |
| :--- | :--- | :--- | :--- | :--- | :--- |
| `rynmesh/agent_runtime/chrome_runtime/scripts/zxing_reader.wasm` | WASM binary | `zxing-cpp` 2.2.x / `zxing-wasm` | `0e8d688d71932ebb6b8b33f700d43d3cb997f59ed9cab3c05102d7f10288a392` (`1,065,866` B) | Apache-2.0 (`zxing-cpp`) / MIT (`zxing-wasm`) | Open-source redistributable under Apache-2.0 / MIT with attribution. Used for QR/barcode decoding in browser automation. |
| `rynmesh/agent_runtime/chrome_runtime/scripts/browser-accessibility.wasm.br` | Brotli-compressed WASM | `oai_browser_use` `1.0.0` | `97d2773f1f0d3f890e2dc8bbf7da5745cfb9c2686d893579ffc35bb9608185ee` (`4,084,148` B) | Bundled Codex desktop browser-use runtime asset | Loaded via `BROWSER_USE_ACCESSIBILITY_CORE_WASM_PATH` by `browser-service.mjs` for DOM/AX tree snapshotting. See Section 2 for redistribution boundary. |
| `rynmesh/agent_runtime/chrome_runtime/extension-host/windows/x64/extension-host.exe` | Windows x64 PE executable | `1.0.0` (`com.openai.codexextension`) | `9ff686a2418bf1f994aa135f0698a660342346a57265cb073723da18a718550e` (`879,408` B) | Bundled Codex desktop native messaging host | Optional Chromium extension native messaging bridge. Not required when using Rynmesh's first-party local CDP pipe server (`cdp-pipe-server.mjs`). See Section 2 for redistribution boundary. |
| `rynmesh/agent_runtime/chrome_runtime/scripts/cdp-pipe-server.mjs` | First-party ESM script | `0.7.0` | Tracked source | MIT (Rynmesh `LICENSE`) | First-party Rynmesh local Chrome DevTools Protocol (CDP) fallback server over loopback (`127.0.0.1`). |
| `rynmesh/agent_runtime/chrome_runtime/scripts/classic-level.mjs` + `package.json` | NPM dependency adapter | `classic-level` `3.0.0` | Tracked source + lockfile | MIT (`Level/classic-level`) | Open-source redistributable under MIT. Installed via `npm ci --omit=dev`. |
| `native/ryn_computer_use/` | First-party Rust crate | `0.1.0` | Source-only (no committed binary) | MIT (Rynmesh `LICENSE`) | First-party Rust crate compiled locally via `cargo build`. All crate dependencies (`serde`, `png`, `sha2`, ` base64`, `hex`, `windows 0.61.3`) are MIT OR Apache-2.0. |

---

## 2. Browser Runtime (`rynmesh/agent_runtime/chrome_runtime/`)

### 2.1 Components
- **First-party Rynmesh adapters (MIT License)**:
  - `rynmesh/agent_runtime/chrome_runtime/__init__.py`
  - `rynmesh/agent_runtime/chrome_runtime/scripts/cdp-pipe-server.mjs`
  - `rynmesh/agent_runtime/chrome_runtime/scripts/classic-level.mjs`
- **Third-party open-source NPM dependency (MIT License)**:
  - `classic-level@3.0.0` (declared in `rynmesh/agent_runtime/chrome_runtime/package.json` and pinned in `package-lock.json`).
- **Codex-compatible browser-use runtime scripts & docs (`1.0.0`)**:
  - `scripts/browser-client.mjs`, `scripts/browser-service.mjs`, `scripts/chromium-browser-diagnostics.mjs`, `scripts/installManifest.mjs`, `scripts/check-extension-installed.js`, `scripts/check-native-host-manifest.js`, `scripts/chrome-is-running.js`, `scripts/installed-browsers.js`, `scripts/open-chrome-window.js`, `scripts/extension-ids.json`
  - `docs/**`, `assets/**`, `skills/control-chrome/SKILL.md`

### 2.2 Provenance & Redistribution Basis
1. **Origin**: The `browser-client.mjs` / `browser-service.mjs` bundle (`_r = "1.0.0"`), `browser-accessibility.wasm.br`, and `extension-host/windows/x64/extension-host.exe` originate from the Codex Desktop browser-control runtime (`oai_browser_use` / `com.openai.codexextension`).
2. **Runtime Execution Model in Rynmesh**:
   - Rynmesh (`rynmesh/agent_runtime/code_mode.py`) enforces `local_execution.desktop_access=true` before initializing any browser session.
   - By default, when no external extension pipe is active or when `RYN_CHROME_FORCE_CDP_FALLBACK=1` is set, Rynmesh uses its **first-party MIT `cdp-pipe-server.mjs`** to launch or attach to a user-installed local Chrome/Chromium instance over loopback CDP (`127.0.0.1`) without executing `extension-host.exe`.
3. **Licensing & Public Redistribution Policy**:
   - While OpenAI's `@openai/codex` repository is licensed under Apache-2.0, prebuilt desktop sidecar artifacts (`extension-host.exe`, `browser-accessibility.wasm.br`, and bundled desktop scripts) are extracted from the local Codex desktop distribution for internal/local compatibility verification and do not carry a standalone open-source redistribution grant.
   - Before publishing an external open-source release artifact or public binary installer, packagers should either:
     - Rely on the user's locally installed runtime via `RYN_CHROME_RUNTIME_DIR` / local Codex installation, or
     - Strip prebuilt proprietary sidecar binaries (`extension-host.exe`, `browser-accessibility.wasm.br`) and use the first-party MIT `cdp-pipe-server.mjs` + open-source `zxing_reader.wasm`.

---

## 3. WebAssembly (`.wasm` / `.wasm.br`) Artifacts

### 3.1 `zxing_reader.wasm`
- **Path**: `rynmesh/agent_runtime/chrome_runtime/scripts/zxing_reader.wasm`
- **Size**: `1,065,866` bytes
- **SHA-256**: `0e8d688d71932ebb6b8b33f700d43d3cb997f59ed9cab3c05102d7f10288a392`
- **Upstream Source**:
  - `zxing-cpp` (`https://github.com/zxing-cpp/zxing-cpp`) compiled to WebAssembly via `zxing-wasm` (`https://github.com/Sec-ant/zxing-wasm`, v2.2.x reader module).
- **License**:
  - `zxing-cpp`: Apache License 2.0 (`https://www.apache.org/licenses/LICENSE-2.0`)
  - `zxing-wasm`: MIT License (`https://opensource.org/licenses/MIT`)
- **Redistribution Basis**: Permitted in both source and binary form under Apache-2.0 and MIT licenses.

### 3.2 `browser-accessibility.wasm.br`
- **Path**: `rynmesh/agent_runtime/chrome_runtime/scripts/browser-accessibility.wasm.br`
- **Size**: `4,084,148` bytes (Brotli-compressed WebAssembly module)
- **SHA-256**: `97d2773f1f0d3f890e2dc8bbf7da5745cfb9c2686d893579ffc35bb9608185ee`
- **Upstream Source**: Bundled accessibility snapshot core from the `oai_browser_use` `1.0.0` runtime, decompressed at runtime by `browser-service.mjs` via `zlib.brotliDecompressSync`.
- **Redistribution Basis**: Same boundary as Section 2.2 above (for local workstation integration; external open-source distributions should supply via local runtime path or omit prebuilt proprietary assets).

---

## 4. Native Binaries & Rust Crates

### 4.1 `extension-host.exe`
- **Path**: `rynmesh/agent_runtime/chrome_runtime/extension-host/windows/x64/extension-host.exe`
- **Size**: `879,408` bytes
- **SHA-256**: `9ff686a2418bf1f994aa135f0698a660342346a57265cb073723da18a718550e`
- **Target**: Windows `x86_64-pc-windows-msvc`
- **Upstream Source**: Chromium Native Messaging Host (`com.openai.codexextension`) from the Codex desktop browser integration (`1.0.0`).
- **Runtime Usage**: Only referenced by `scripts/installManifest.mjs` when registering a Chromium Native Messaging Host manifest. Rynmesh's `cdp-pipe-server.mjs` fallback does not invoke this executable.

### 4.2 `native/ryn_computer_use` (First-Party Source Crate)
- **Path**: `native/ryn_computer_use/` (`Cargo.toml`, `src/lib.rs`, `src/main.rs`, `src/bin/ryn_computer_use_fixture.rs`)
- **Version**: `0.1.0`
- **License**: MIT License (first-party Rynmesh code).
- **Third-Party Rust Dependencies**:
  - `base64` `0.22` (MIT OR Apache-2.0)
  - `hex` `0.4` (MIT OR Apache-2.0)
  - `png` `0.17` (MIT OR Apache-2.0)
  - `serde` `1.0` / `serde_json` `1.0` (MIT OR Apache-2.0)
  - `sha2` `0.10` (MIT OR Apache-2.0)
  - `windows` `0.61.3` (MIT OR Apache-2.0)
- **Redistribution Basis**: 100% first-party MIT source with permissive (MIT / Apache-2.0) dependencies. No compiled binary is checked into Git; binaries are built from source during local development or CI packaging.

### 4.3 External Local Inference Binaries (`llama-server`, `ollama`, `codex`)
- **Paths**: Not vendored in the repository. Resolved dynamically at runtime from system `PATH` or user-configured runtime directories (`rynmesh/llm_package/`).
- **Licenses**: Upstream open-source licenses (`ggml-org/llama.cpp` under MIT License; `@openai/codex` CLI under Apache-2.0).
