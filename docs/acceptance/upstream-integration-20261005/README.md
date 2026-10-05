# Personal AI workspace / upstream integration

Date: 2026-10-05. This is development-branch integration evidence, not a release
or completion of the physical acceptance gates in `docs/TESTING_STRATEGY.md`.

## Inputs and history

- Upstream `main`: `9db06f436e64d87c7060db0934392768c42c8187`.
- Personal AI branch: `6d8ba18ace97caf75d68e8181e6390c36f435f48`.
- Local chat waiting/elapsed-time edits saved in snapshot `0dab711`.
- Integration branch: `codex/personal-ai-upstream-integration`.
- Merge `be5a8bf` has upstream and the personal snapshot as its two parents.
  The original feature branch remains unchanged. No history was force-pushed.

## Integration decisions

| Area | Result |
| --- | --- |
| Navigation | Personal shell retains upstream Ask Ryn, friends, exchange, reading, search and device-sync entry points. |
| Conversations | `/ask` retains upstream node-encrypted history, reviewed context, run recovery and sync. `/services/private-ai/chat` retains the personal workspace with CLI selection, Markdown, NAS context and browser-encrypted history. These stores remain separate. |
| Inference | Preserve upstream signed `stream-v1` verification, bounded replay, settlement and durable orders alongside structured chat/tools/SSE, local CLI/model sources, LAN-first discovery and strict P2P support. |
| Authorization | Preserve upstream friend grants and rechecks, with explicit owned-device/personal-space authorization where applicable. Discovery alone grants no access. |
| Lifecycle | Register personal-space polling through the shared background-worker registry and retain bounded shutdown and owned-runtime cleanup. |
| Desktop | Keep the upstream pinned llama.cpp resource bundle on targets supported by the shell staging script. The Apple Silicon build script stages it; Windows retains no bundled resources, while its Python runtime installer can obtain a pinned Windows runtime separately. |
| Localization | Personal UI remains English/Chinese. Existing upstream-only English text is recorded as exact file/text baseline pairs; new translation keys must exist in both catalogs. This is not full translation of upstream's new modules. |

Integration regressions corrected include duplicate SSE cache headers, archived
answers incorrectly appearing as failed after cleanup, plain completion requests
entering the structured streaming path, missing frontend client request types,
fixture API drift, and test expectations for the additional P2P route/worker.

## Verification

- Full backend: `python -m pytest tests/ -q --tb=short` — **2,250 passed,
  29 skipped**, with four existing FastAPI deprecation warnings.
- Webapp: `npm test -- --maxWorkers=2 --reporter=dot` — **497 passed in 81 files**.
- Webapp: `npm run build` — typecheck and production bundle passed.
- Backend targeted regression set — **66 passed**, including real HTTP streaming,
  consumer archival/replay, runtime concurrency and worker lifecycle.
- Follow-up desktop CI dependency fix: delay structured-chat imports until chat
  execution so the stdlib-only runtime staging interpreter can read pinned assets.
  A fresh `python -S` regression plus native-runtime/API/HTTP-streaming tests passed
  (**83 passed, 13 skipped**). This addresses both macOS architectures' first CI
  failure without installing application dependencies into the staging interpreter.
- `python -m ruff check rynmesh/ tests/` — passed.
- `python scripts/exchange_e2e.py` — passed; replicas agree and the private
  plaintext SQLite canary remains absent. Real-model neutrality was not evaluated.
- Windows: `cargo test --lib --locked --target x86_64-pc-windows-msvc` — **4 passed**
  with the actual Windows config and no test-only resource override.
- `bash -n webapp/src-tauri/scripts/build-macos-arm64.sh` — passed.
- Built UI served directly by an isolated node, without a Vite server: `/`,
  `/ask`, `/exchange`, `/devices/sync`, `/services/private-ai/chat`,
  `/services/sources` and `/digest` returned the app with valid hashed assets;
  missing assets returned 404 and the local status API returned 200.
- Browser walkthrough confirmed the personal shell, Ask Ryn, live exchange join
  screen, personal workspace empty state and device-sync screen, without console
  errors. No exchange network was joined and no real personal profile was used.

## Repeat the UI check

1. Build `webapp` and start a node with a fresh temporary home, separate port and
   `RYNMESH_WEBUI_DIR` pointing to `webapp/dist`. Disable automatic registration,
   LAN discovery and default content discovery for this isolated smoke check.
2. Open the node URL directly. Dismiss first-use guidance if present.
3. Follow **Ask Ryn**, **Exchange**, **Services → AI workspace**, and
   **Devices → Device sync**. Each should render its appropriate fresh-node state.
4. Check that a nonexistent `/assets/` file returns 404, not the SPA document.
5. Stop the isolated node after inspection; retain the ordinary development and
   installed profiles unchanged.

## Limits

No new signed installer was built or installed. macOS builds, Docker transport
acceptance, physical cross-network/GPU behavior and CI are separate checks; local
Windows results do not establish those gates. Production bundling still emits a
large JavaScript chunk warning. Previously recorded September acceptance files
describe their original builds and are not new physical evidence for this merge.
If upstream advances during review, merge it into this branch and rerun affected
checks before asking the maintainer to merge.
