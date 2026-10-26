# Chrome Runtime (`rynmesh/agent_runtime/chrome_runtime`)

Install the pinned Node.js dependencies before running browser control from a source checkout:

```sh
cd rynmesh/agent_runtime/chrome_runtime
npm ci --omit=dev
```

The desktop sidecar build scripts install these dependencies before packaging. Python package data includes the browser assets and installed runtime dependencies. The ClassicLevel adapter lives in `scripts/` so `npm ci` does not remove it.

## Provenance, Licenses, and Redistribution

See [`docs/THIRD_PARTY_RUNTIME_PROVENANCE.md`](../../../docs/THIRD_PARTY_RUNTIME_PROVENANCE.md) for the complete provenance, version, SHA-256 digest, license, and redistribution basis of:

- **First-party Rynmesh scripts (`MIT`)**: `scripts/cdp-pipe-server.mjs` (local loopback Chrome DevTools Protocol pipe server), `scripts/classic-level.mjs`, and `__init__.py`.
- **Open-source dependencies (`MIT` / `Apache-2.0`)**:
  - `classic-level@3.0.0` (`MIT`, pinned in `package.json` / `package-lock.json`)
  - `scripts/zxing_reader.wasm` (`Apache-2.0` / `MIT`, `zxing-cpp` 2.2.x via `zxing-wasm`, SHA-256 `0e8d688d71932ebb6b8b33f700d43d3cb997f59ed9cab3c05102d7f10288a392`)
- **Bundled Codex browser-use runtime assets (`v1.0.0`)**:
  - `scripts/browser-client.mjs`, `scripts/browser-service.mjs`, `scripts/chromium-browser-diagnostics.mjs`, `scripts/installManifest.mjs`, and `docs/**`
  - `scripts/browser-accessibility.wasm.br` (SHA-256 `97d2773f1f0d3f890e2dc8bbf7da5745cfb9c2686d893579ffc35bb9608185ee`)
  - `extension-host/windows/x64/extension-host.exe` (SHA-256 `9ff686a2418bf1f994aa135f0698a660342346a57265cb073723da18a718550e`, optional native messaging host; not invoked when Rynmesh uses `cdp-pipe-server.mjs` loopback CDP fallback)
