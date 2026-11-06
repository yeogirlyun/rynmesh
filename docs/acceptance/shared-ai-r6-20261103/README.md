# R6 shared AI stability and latency (2026-11-03)

Source: develop `efd9fa13f8c2b501f689b6b04deec754cd79b8fb`.
Company payload unchanged: `45e5d07c5b8d83b2ee12310ecba51eb04e370a5d`.

## Reproduced before repair

- Local provider: all 40 samples listed five configured services.
- LAN 82: 40 samples at five-second intervals; 5 samples listed two shared services, 19 listed three, only 16 listed four. Codex/Antigravity disappeared when the CLI metadata TTL elapsed between publication refreshes. DeepSeek Pro is not shared under the current settings; four is the expected shared count.
- The user's LAN 82 Agent greeting failed after 648.453 seconds. Preparation took minutes; successive text checkpoints were about 44 seconds apart. Agent metadata lookup synchronously consulted the registry, and even cancellation checks constructed full execution contexts that resolved model metadata repeatedly.

## Changes

- Independent CLI inventory workers refresh before metadata expiration, without depending on registry publication or UI reads. Cached-only bindings still reject expired inventory.
- A broken CLI does not fail the whole signed catalog. Transport failures preserve the last verified snapshot only until its original 120-second expiration; membership generation changes still invalidate it immediately. Invalid signed responses still clear the cache.
- Agent discovery and bound dispatch read current local/signed records while registry locator refresh runs in the background. Cancellation checks read retained grants and local control state without network discovery or creating new grants. Actual dispatch retains destination and permission checks.
- Sidebar lists only the three company apps. Other installed tools remain in Apps. Existing full-package Excel Chinese resources are retained.

## Automated verification

- Catalog/CLI/background suites: 83 passed; worker-name inventory updated and the remaining test passed on rerun (84 total).
- Agent latency, space bindings, model switching and data policy: 72 passed.
- Sidebar: 2 passed; frontend type/build checked by Windows release build.
- Broader exploratory run found three pre-existing failures, independently reproduced at the baseline commit: default-workspace file-review storage fixture, peer-scope dispatch fixture, inference-context dispatch fixture. These are not claimed as passing. The newly added cancellation-context test passed after its fixture explicitly required destination binding.

Live installation, sustained sampling and Agent greeting results are recorded in result.json after verification.

## Final live acceptance

- Both ordinary Windows user installations completed with exit code 0 at `%LOCALAPPDATA%/Ryn`; desktop and peer hashes match the committed build, and company health plus all seven plugin checks pass. Identity keys, messaging keys, settings and company key are unchanged.
- Local file verification runs outside the Codex MSIX filesystem overlay: a direct tool-environment read returned an old virtualized binary copy. A normal interactive-user verification task confirmed the actual installed files match on both hosts.
- Three real Agent `hi` turns from LAN 82 through the shared DeepSeek Flash completed in 3.560, 3.625 and 3.561 seconds; observed first text was 2.594, 2.377 and 2.596 seconds. The last turn ran after final complete-package installation.
- Catalog cold-start observation: five initial samples were empty, then discovery recovered after about 25 seconds. All 67 later samples contained the same four ready, authenticated services. This is a bounded observation, not a guarantee against future network outages.
- Agent model picker: 18 samples over approximately 85 seconds retained the same 26 available model options.
- Local and LAN 82 desktop sidebars were visually checked: only the three company plugins appear. Further computer-use input stopped when the user pressed Esc; remaining verification used APIs and ordinary-user filesystem checks.
- The separate WeCom `TransportError` screenshot is not claimed fixed by this release; no third-party messages were sent during verification.

Installer: `output/full-package-20261103-r6/Ryn_0.8.0_Full_7Plugins_R6_x64.exe`.
SHA-256: `1AA3D1FCF227F687A8DCB5955E98F5EE49174EE52377A8D4EDDB0815F901E5FC`.
