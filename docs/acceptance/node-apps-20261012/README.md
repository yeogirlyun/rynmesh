# Node apps acceptance — 2026-10-12

Implementation commit: `916724512c02994453b807a36eb0b5cd192fcea0`. Tests and packaging ran on the owner's local workstation; provider acceptance ran on physical MS-2. No cloud coding or new cloud CI run was used.

- Final focused backend suite: **573 passed**, including the 27 new app/Antigravity adapter cases and agent, sharing, resources, policies, Exchange, web UI and friend regressions.
- Frontend: **664 passed** in 101 files; TypeScript and Vite production build passed using Node 22.23.2.
- Ruff and Git whitespace checks passed.
- `browser-result.json` and Resume/Recruit/Desk/Chinese screenshots: two temporary HTTP nodes, headless real UI workflows, review before writes, reload and language switch. No browser errors, global input, model calls, workers or plaintext app records.
- `physical-result.json`: real MS-1/MS-2 pairing, app grants, all three workflows and exact retry without duplication. Temporary identity revoked and listener stopped. Owner workspaces remain empty; test records stay encrypted under the separate revoked identity.
- `installed-browser-result.json` and `installed-ms1-ms2.png`: installed-wheel MS-1 UI selected actual MS-2; all three owner workspaces empty; no browser errors or global input. This smoke test was read-only.
- `source-manifest.json`: all 250 installed Python modules verified on both hosts before startup. Wheel SHA256: `f75a65f3662da682030b1e07d39515090069c449aa0eb071d9a8de43b70093aa`.

No actual model-provider calls, Ryncoin transfers or coin ledger creation were required. The installed hosts run only apps and authenticated friends, not the assistant or Exchange. They have no automatic startup or monitoring.

The wider Mac backend attempt did not complete and was not green (48 failed, 670 passed, 21 skipped before stopping). See [Ziling's review](../../reviews/ZILING_UPDATE_2026-10-12.md) for the reproduced original-head fixture issue and remaining triage. These records do not establish full-backend, public-network, independent security or model-neutrality acceptance.
