# Chrome runtime

Install the pinned Node.js dependencies before running browser control from a source checkout:

```sh
cd rynmesh/agent_runtime/chrome_runtime
npm ci --omit=dev
```

The desktop sidecar build scripts install these dependencies before packaging. Python package data includes the browser assets and installed runtime dependencies. The ClassicLevel adapter lives in `scripts/` so `npm ci` does not remove it.
