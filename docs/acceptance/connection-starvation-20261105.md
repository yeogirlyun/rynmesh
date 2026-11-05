# WebView connection starvation fix — acceptance, 2026-11-05

Plan: `docs/AGENT_CONNECTION_STARVATION_FIX_WORK_PLAN_ZH.md`. Branch `codex/connection-starvation-fix`, cut from `develop` 235f7c4.

## Problem

The Agent page showed "Could not refresh the task list", "Could not refresh this task" and "Could not load created files" together, and the turn timer stopped. The node answered direct requests quickly. On the affected machine five WebView connections to `127.0.0.1:8791` were busy with `/peers` and `/directory`, and one held an SSE stream. Chromium allows six HTTP/1.1 connections per host, so Agent GETs queued inside the browser until their 10 s deadline. The CORS preflights used a separate pool and succeeded.

Causes of the pile-up:

- Shell-wide polls fired on fixed intervals without waiting: `/peers?quick=1` every 2 s, `/directory` every 15 s.
- Every device row rebuilt the credit scoreboard, reading and verifying every event twice.
- Full reads waited inline on an unreachable public registry (10 s per call) and probed every device.

Found during this acceptance: `POST /api/local/recommendations`, which Home requests on every visit, scanned every device on the event loop. With unreachable devices the whole node stopped answering until the desktop watchdog restarted it.

## Changes

| Commit | Change |
|---|---|
| eeb6baf | Work plan |
| b786fac | One-at-a-time pollers with timeouts for shell-wide reads; `/directory` falls back only on 404/501; 60 s default limit for unbounded GETs |
| 0fdd2d0 | Agent list/detail polls keep running after any read error; focus refresh leaves a single dropped read to the poll; files listing retries non-transient errors 4 × 15 s |
| 7d0c3c6 | Credit ledger verifies each event once per change (fingerprint + 60 s TTL + append invalidation); one scoreboard per device list |
| a2310e6 | `/peers` and `/directory` read caches; a 2-thread background refresher updates registry data and presence at most every 15 s |
| d62253a | `\\localhost\C$` launches resolve to the installed sidecar; release builds never fall back to the source tree, PATH or python |
| 2fa075c | Recommendations, feedback, search-ask, peer discovery and content fetches run their network I/O off the event loop; content scans use cached device records, ask devices in parallel (8) with an 8 s listing bound, and share concurrent scans |
| b93cd01 | Test limit for the two focus-refresh outage tests |

`Devices.tsx` was left as is. It already runs one device-sync read at a time and ignores results after unmount, and `deviceSyncApi` takes no signal.

## Environment

These runs used the Windows VM (4 vCPU, 6 GB) that shares `D:\code`. They were not run on the affected desktop.

- Core installer built with `webapp/src-tauri/scripts/build-windows.ps1` from committed source:
  - Baseline: 235f7c4, `Ryn_0.8.0_x64-setup.exe` SHA-256 `FAEAAA33EE93210C411B46BABF6C5C7FE8E9971164E98319046B0230D3F63760`.
  - Final: 2fa075c, SHA-256 `D24CDDDF6C2780F595FFCA14F1D47B172A25FCF7559C4E65DAE5A33B3D58A3A6`, 60,991,138 bytes. The installed `rynmesh-peer.exe` SHA-256 `9BCF5B7392854B9226FFDCF95039BFE40302500FE160233C9532EDD95935AE56` matches the build.
- Installed silently into the VM user's physical `%LOCALAPPDATA%\Ryn`. There was no earlier physical installation to back up. Company and office plugins are not part of the core installer.
- Each run used an isolated `RYNMESH_HOME` and WebView2 profile copied from one seeded template. The template held 19 other devices registered at unreachable addresses (`192.0.2.x`), 20 rows with this node, and 499 credit events. The registry and relay URL were `http://192.0.2.1:9`, so every registry call waited for its 10 s timeout, as on the affected machine.
- Measurements:
  - WebView requests: DevTools protocol network events, from creation (browser queueing included) to finish, preflights and SSE excluded.
  - Endpoint latency: loopback HTTP.
  - Node stacks: `py-spy dump`.

## Results

| Measure | Baseline 235f7c4 | Final 2fa075c |
|---|---|---|
| Requests open at once (max / median / p95) | 190 / 97 / 182 | 3 / 0 / 2 |
| Seconds with ≥ 5 requests open | 172 of 180 | 0 of 240 |
| Agent reads failed (aborted at 10 s) | 12 of 19 | 0 of 72 (p95 30 ms, max 98 ms) |
| Task-list banner on the Agent page | shown | not shown (list, detail, files) |
| `/peers?quick=1` | no answer within 90 s | p50 44 ms, p95 118 ms, max 233 ms (90 samples) |
| `/peers` | — | p95 128 ms (20 samples) |
| `/directory?quick=1` | connection reset after 30 s | p95 79 ms |
| `/directory` | — | p95 87 ms; once 8.0 s at startup (bounded first refresh wait) |
| `/agent/tasks` | — | p95 26 ms |
| Node health during a fresh start | node restarted by the watchdog | 249 of 251 OK; the 2 failures were before the node had started |
| Launch through `\\localhost\C$\…\Ryn.exe` | ran `conn-base\webapp\src-tauri\binaries\rynmesh-peer-x86_64-pc-windows-msvc.exe` | ran `C:\Users\sguser\AppData\Local\Ryn\rynmesh-peer.exe` |

- **A, B, D, E, F met.**
  - D: the node was killed during a run. The banner appeared after 7 s, the watchdog had the node healthy after 25 s, and the banner cleared on its own after 34 s, without Refresh.
  - F: the desktop log records `node sidecar: C:\Users\sguser\AppData\Local\Ryn\rynmesh-peer.exe`.
- **Page switches.** Switching Agent → Devices → Services → Agent during a run showed no banner. The first fix build (d62253a) did not pass a fresh start: the node froze about a minute after start. py-spy showed the event loop in `POST /api/local/recommendations` → `network_content` → `list_peer_content`, which led to commit 2fa075c. With 2fa075c, Home's recommendations and content took 24.2 s with 19 unreachable devices, but no other request waited for them.
- **Background refresh.** One refresh took 34.6 s with the registry unreachable (devices 20 s, probe 4.5 s, services 10 s). It logged its phases and ran off the request path.

## Tests

- **Frontend (vitest).** New tests pass:
  - `polling` 8, `peerPolling` 3, `liveNodeClient` 6;
  - `directoryModel` 4 added;
  - `RynAgent` 3 added, 2 adjusted;
  - `RynAgentArtifacts` 1 added.
  The new Agent tests fail on 235f7c4, except one that matches an existing test name. `tsc -p tsconfig.app.json` passes.
- **Frontend, wide runs.** The Agent suites (`RynAgent`, `RynAgentSending`, `RynAgentReasoning`, `RynAgentConversationControls`, `RynAgentNavigationSending`) time out intermittently on this VM under load. Run under the same load, 235f7c4 failed a superset of the tests that failed on this branch.
- **Backend (pytest).** New tests pass: `test_credit_scoreboard_cache` 8, `test_peer_directory_refresh` 11, `test_peer_content_scan` 3. These fail on 235f7c4 too:
  - `test_peer_directory.py::test_directory_probes_so_the_device_poll_is_enough`, which fails at its first `/llm/services` assertion, so its directory part could not be re-run here;
  - `test_rynmesh.py::test_peer_http_local_control_api_uses_real_store_data`;
  - `test_rynmesh.py::test_http_registry_discovery_and_peer_fetch_without_shared_filesystem`.
  `test_direct_http_peer_transport_lists_and_fetches_media` fails only under load and passes alone on both trees.
- **Rust.** `cargo test --release --lib node::tests`: 5 passed, including the new admin-share test.

## Not verified

- **C, a real Agent task of five minutes or more.** This VM has no usable model. The Agent list and detail polls were exercised without a running task.
- **The affected desktop.** Physical-machine acceptance, plugin health and the company service were not checked there, because this VM has no access to it.
- **A reachable registry.** All registry measurements used an unreachable registry. Behaviour with a reachable registry was not measured separately.

## Follow-ups

- `FirstSuccessFlow` polls `/first-success` every 3 s without waiting for the previous request. It is mounted only until first-run completes. In a fresh profile, while the node was frozen, it queued 40 requests in 3 minutes. The other page-level pollers listed in section 8 of the plan remain as they were.
- A content scan still asks every device, including ones the presence cache already knows are offline.
- The watchdog's `/health` route is a sync route, so it shares the worker thread pool with other sync routes.
