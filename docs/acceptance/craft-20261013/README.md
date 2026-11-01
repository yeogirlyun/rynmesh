# Craft migration acceptance — 2026-10-13

Implementation: `946b35d25224b6e5c2c6317dda018883ef2679cf`, local branch `codex/clawpad-node-apps`. Clawpad source `e725fa980d2ebeda3afda3325727e76fc766551b`. All implementation, packaging and QA ran locally; no cloud coding task or new cloud CI run was created.

## Completed checks

- **568 backend tests passed** in the final focused regression invocation, covering Craft/node apps, Antigravity, assistant approval/control/budget/resource/sharing/recovery, Exchange, authenticated friends and web serving. This includes **45 app/adapter tests**. Command uses the checkout explicitly in `PYTHONPATH` so subprocess death/recovery cases import this source. The initial invocation omitted that environment and three child processes imported an older installed package; the corrected run passed without changing their tests.
- **666 frontend tests passed** in 101 files on Node 22.23.2; TypeScript/Vite production build passed. The existing bundle-size advisory remains; no build error.
- Ruff across `rynmesh tests scripts` and Git whitespace checks passed.
- Actual public upstream API confirms all 13 pinned release tags; all 12 pinned web archives downloaded over verified HTTPS, matched their recorded SHA-256, safely extracted and passed installed-file verification. FilmCraft's recorded release is native-only. `install-result.json`, `upstream-release-result.json` and `retained-notices.json` preserve the result and retained license/notice file hashes. No DMG was installed, no native app launched, no user documents copied.
- Two temporary real HTTP Rynmesh nodes passed the headless catalog UI flow: unshared apps hidden, explicit friend grants for all 13, catalog selection, review before `editor_info`, read-only receipt, FilmCraft native-only distinction and Chinese UI. No page errors, global UI input, model calls or coin transfers. See `catalog-browser-result.json` and catalog screenshots.
- All **12 WebAssembly editors started and rendered a canvas** on dedicated temporary static origins. **11 had clean startup checks**. **PhotoCraft requested missing `fonts/manifest.txt` (404)**: its UI starts but font coverage remains for follow-up. See `editor-startup-result.json` and editor screenshots. This is runtime startup evidence; it does not establish editing, every listed format, saved document integrity or end-to-end delivery.
- The local wheel includes `craft_catalog.py`, `craft_host.py`, the `rynmesh-craft` CLI entry point and the current built Node Apps UI. `package-result.json` records its hash. It is not a published release or installed production upgrade.

The startup harness uses each app's actual readiness signal. EffectCraft and LightCraft do not emit the Trunk startup event used by the other ten. A forced software-GPU follow-up stalled and was stopped along with only its own descendants; default headless GPU startup passed for both. No screen, OS input, browser permissions, loopback aliases or firewall settings were changed. One-time test servers and browsers were stopped. The two pre-existing pilot app services were preserved.

## Scope and remaining delivery

Craft `editor_info` goes through the existing encrypted app/grant/approval/receipt boundary. Editor WASM runs in the user's browser on a separate origin; the host serves program files only. There is no document upload, encrypted workspace sync, user launch configuration, generic agent editing bridge, billing or new currency issuance. Browser storage can be evicted: explicit export/backup round trips still need validation.

The full Mac backend suite was not rerun and is not represented as green. Previously recorded broader platform/baseline failures remain in `docs/reviews/ZILING_UPDATE_2026-10-12.md`. No new physical MS-2 rollout is claimed. Ziling's next scope is in [the Chinese handoff](../../CRAFT_ZILING_HANDOFF_ZH.md): user entry and approved launch targets, chosen-node deployment, document round trips, fonts and scoped editing tools. Keep actual progress distinct from proposed work.
