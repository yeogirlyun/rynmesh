# Optional browser adapter

Ryn ships its first-party `scripts/cdp-pipe-server.mjs` bridge. It does not ship
the extracted desktop browser client/service, accessibility WASM, extension host,
icons or documentation from another product. Ordinary Agent tools and persistent
JavaScript execution work without these components. Built-in `ryn_browser_*`
tools remain the default browser path.

A separately supplied compatible adapter can be selected explicitly through
`RYN_CHROME_RUNTIME_DIR`. It must provide `scripts/browser-client.mjs`,
`scripts/browser-service.mjs`, and `browser-accessibility.wasm.br` under scripts
or assets, plus its own required dependencies and documentation. Supplying a
path does not install it, grant desktop access or establish redistribution rights.
Ryn never scans another application's installation to obtain it.

Missing adapters return `agent_browser_adapter_unavailable` before launching a
browser, with a suggestion to use the built-in tools. Adapter acceptance is
explicit: set `RYNMESH_TEST_EXTERNAL_BROWSER_RUNTIME=1` and provide the adapter
for the dedicated headless integration tests. The normal suite verifies graceful
absence and continued code/tool execution. A self-contained open adapter is a
separate design request to the contributor, not an included feature claim.
