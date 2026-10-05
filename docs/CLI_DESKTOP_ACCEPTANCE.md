# Windows desktop Codex CLI acceptance

> Superseded on 2026-09-24: persistent Codex conversations and thread resume described below have been removed for privacy. This document records historical acceptance and installer results. Current behavior and pending acceptance are documented in [Inference privacy and origin tools](INFERENCE_PRIVACY_ORIGIN_TOOLS.md). The installer mentioned below does not include these new changes.

Date: 2026-09-23. Machine: current Windows development desktop.

## Follow-up: persistent workbench sessions and real model selection

The desktop was rebuilt and successfully restarted from its Desktop shortcut after the first acceptance below. The latest frozen node SHA256 is `351C685624F9E361532C55E0AA683F6BEEF0852F580D4D02AA4E48221F80F590`. The installer and checksum named below were updated.

- The workbench now uses Codex app-server `thread/start`, `thread/resume`, and `turn/start`. Each Ryn conversation owns a persisted mapping to a Codex thread; client input never directly selects another existing Codex thread.
- `model/list` supplies the real model dropdown. The installed CLI returned seven visible models. The selected model is sent to Codex and saved with the Ryn conversation.
- A live browser UI test, connected to the freshly packaged desktop node on port 8791 (no fixture), selected GPT-6-Sol and sent a Chinese request to remember `青山`. The response was `已记住`.
- The UI switched to GPT-6-Luna and asked for the word, sending only the new message. The response was `青山`, with the same Codex thread ID: `01a0cd8e-018f-7c22-b071-5b9d0382a84d`.
- The Codex desktop's own read-thread interface returned both completed turns under the title `Ryn · Ryn 桌面验收：请记住口令“青山”，只回复“已记住”。`. Read-only inspection of that thread's rollout confirmed first-turn model `gpt-6-sol` and second-turn model `gpt-6-luna`.
- UI shows the saved Codex thread ID and explains that clearing Ryn history does not delete Codex history. Screenshot inspection confirmed separate device, service, and model selectors.
- Backend suites: 85 passed. Updated chat suite: 8 passed; the unchanged AI workspace suite's 3 tests also passed during this turn. Production frontend and Windows installer builds passed.

This follow-up applies to the local Codex workbench. Stateless API calls retain their separate text-only execution path. Remote workbench model discovery and physical cross-device session persistence are not claimed. Browser interaction exercised the same frontend against the packaged node; native WebView click-through was not automated.

## Build and execution

- Built the real Tauri release app and NSIS installer, including the frozen Python node.
- Running app: `webapp/src-tauri/target/x86_64-pc-windows-msvc/release/Ryn.exe`.
- Verified port 8791 belongs to the bundled `rynmesh-peer.exe` in that release directory.
- Verified bundled node SHA256 matches the freshly built node: `069E3DF9A181961627489CC33A042B617DE1D78698D88A4414F5BFF29C4B4574`.
- Installer: `release/windows/Ryn-0.6.2-codex-cli-windows-x64-setup.exe`; checksum alongside it.
- Updated existing Desktop and Start Menu Ryn shortcuts to this release app. Original shortcuts are backed up under `.codex-tmp/cli-desktop-shortcut-backup/`.

## Real requests, without fixtures or mocked CLI

1. `GET /api/local/llm/cli-services` detected installed Codex and Claude executables. Claude was not configured or tested.
2. `POST /api/local/llm/cli-services/codex_cli/setup` completed a real Codex self-test using the current user's existing login. Codex then reported installed/configured/online all true, with sharing disabled.
3. `GET /api/local/llm/services` returned local `codex-cli`, online, on the current desktop. Local use works without joining a personal space or publishing a service.
4. Submitted an asynchronous order through the same node endpoint used by the desktop chat screen. Task `desktop-codex-acceptance-20260923` succeeded with transport `local_process`, output `RYN_DESKTOP_CODEX_OK`, duration 10,141 ms, input/output usage 16,022/11 tokens.
5. `GET /api/local/llm/api-access` returned `http://127.0.0.1:8791/v1` and model `local/codex-cli`.
6. Created a temporary project key, called `/v1/chat/completions`, and received a standard `chat.completion` with assistant content `RYN_API_CODEX_OK`, model `local/codex-cli`, and input/output usage 16,023/10 tokens. Revoked the temporary key immediately after the test; no key is recorded here.

## Automated checks

- Backend CLI, inference API, and LLM package suites: 85 passed.
- Frontend AI workspace and Private AI chat suites: 10 passed.
- TypeScript and production frontend build: passed.
- Tauri Windows release and NSIS packaging: passed.
- Codex lookup includes its versioned Windows desktop installation directory when PATH lookup fails; unit test covers this fallback.

## Scope and remaining verification

This verifies installed CLI discovery, real Codex login/inference, the desktop node chat endpoint, and the local API text path. The running desktop is the freshly built executable. Native window click-through was not automated. An optional restart-from-shortcut check and a launch with a temporarily replaced PATH were blocked by automatic approval policy; neither is claimed as passed. The already-running app remains online.

No physical second computer or cross-network CLI call was tested. Personal-space sharing remains disabled. CLI mode currently supports text requests; streaming, client tools, images, and workspace file/command approval are not implemented. Codex uses its logged-in cloud service; this does not imply local model weights were discovered or local GPU inference was tested. Users can create their own project key from Services → API access.
