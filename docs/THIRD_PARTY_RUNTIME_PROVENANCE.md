# Runtime distribution boundary

Updated 2026-10-29 for the local integration candidate.

The current tree removes 54 bundled browser-adapter files from the earlier
delivery, including the extracted desktop client/service scripts, native host,
accessibility WASM, barcode WASM, product icons, documentation and skill. Earlier
commit history is unchanged; this statement describes the candidate tree and
its artifacts, not an audit or erasure of all remote history.

No independent redistribution basis was established for the extracted desktop
bundle. Public package data now explicitly includes only the first-party
`chrome_runtime/README.md` and `scripts/cdp-pipe-server.mjs`, rather than wildcard
adapter scripts/assets/binaries/dependencies. Desktop builds no longer install
the removed adapter's npm dependencies.

| Component | Delivery | Basis / limit |
| --- | --- | --- |
| Ryn adapters and CDP pipe bridge | Repository source | Repository MIT license; normal code execution needs no external browser adapter |
| `native/ryn_computer_use` | Rust source; separately built helper | Repository MIT license; dependencies retain upstream licenses; platform acceptance is separate |
| Optional browser adapter | Explicit local `RYN_CHROME_RUNTIME_DIR`; absent by default | Supplied separately with its own license/dependencies; never bundled or automatically extracted |
| llama.cpp / Ollama / CLI providers | Existing managed or configured provider paths | Existing provider-specific packaging/license procedures still apply |
| Craft editors / office engines | Explicit pinned downloads | Existing hashes, payload manifests and upstream notices; separate from the browser adapter |

See [optional adapter behavior](../rynmesh/agent_runtime/chrome_runtime/README.md).
A self-contained browser adapter from redistributable components is a separate
design request. Compatibility does not establish third-party redistribution rights.
