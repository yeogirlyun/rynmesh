# AI sources desktop delivery — 2026-09-23

Implemented the approved simplified AI sources screen at `/services/sources` with three main navigation tabs. Existing API access, model mapping and sharing remain reachable. Source settings use a focused modal with collapsed connection, model and sharing sections.

## Included

- Multiple named OpenAI-compatible Chat Completions sources with presets for Z.ai, OpenAI, DeepSeek, Qwen and OpenRouter; custom HTTPS endpoint support. Ollama and LM Studio presets use their local compatibility endpoints.
- Source create/edit/delete/disable, encrypted node-owned configuration, secret redaction, optional key removal, model-list discovery and manual model IDs, context/output limits, allowlisted request defaults, save-and-test.
- A changed endpoint does not silently reuse the old endpoint's API key. No vendor API key is stored in browser persistence, published in discovery, or returned by the control API.
- Tested sources enter the local chat and inference API catalogs. The same model IDs can be used with the existing alias mapping and laptop API gateway. Unverified drafts are excluded.
- Existing Codex/Claude CLI discovery, verification and sharing are available from the new screen. CLI uses the existing logged-in OS account. No new CLI login or external installation was performed.
- Source sharing is opt-in and requires personal-space AI access; the provider enforces this requirement when handling peer requests.
- Windows `--ai-sources` opens the configuration screen and selects the Z.ai source. Ordinary launches retain their normal behavior.
- Installer: `release/windows/Ryn-0.6.2-ai-sources-windows-x64-setup.exe`; SHA256 `F3C5171C08C8C177FAAEEFA3398B9D49C8C1853A6E6526C24124A969A3C8EECC`.

## GLM configuration

- Name: `Z.ai · GLM`
- Root endpoint: `https://api.z.ai/api/paas/v4`
- Model: `glm-5.3-flash`
- Context: 1,000,000; maximum output: 128,000
- Defaults: `thinking.type=enabled`, `reasoning_effort=max`
- Credential: empty; status: `needs_key`; sharing: disabled
- Vendor specification checked against https://docs.z.ai/guides/vlm/glm-5.3-flash . Model capability is distinct from current Ryn input support.

The adapter now accepts an explicit API path prefix. This source requests `/api/paas/v4/chat/completions`, without appending a second `/v1`. Existing adapters retain their prior `/v1` default.

## Verification

- 56 existing CLI/LLM package tests passed.
- 37 source/inference API tests passed, including encrypted persistence, redaction, endpoint changes, invalid configuration rejection, exact Z.ai path and request defaults, unverified-source exclusion, local chat routing, API routing, sharing guard and deletion.
- 14 related frontend tests passed; source/menu tests rerun after final form adjustments, 3 passed.
- Production TypeScript/Vite build and Windows Tauri/NSIS packaging passed. Existing bundle-size advisory remains.
- Live desktop node returned the persisted GLM draft without a key. Existing Codex remains configured.
- Browser inspection against the actual desktop node checked source listing, add menu, light/dark configuration modal, exact GLM address/model/limits and a blank masked key field. Fixed bottom actions remain visible in a 1280 × 720 viewport. Native WebView screenshot automation was unavailable.

## Current boundaries

Actual Z.ai authentication/inference awaits the user's key. Automated inference tests used a controlled upstream; they are not proof of a live GLM completion. No second physical node was exercised. Remote sources can be selected for use but their credentials are configured on their owning device. Arbitrary custom CLI protocols, additional CLI account profiles and native Anthropic/Responses upstream protocols are not implemented by this source editor. Image/video/PDF input is not enabled in Ryn's text chat contract. The UI states these distinctions rather than inferring support from a model's vendor specification.

Service catalog polish: source names, brand icons, API / CLI badges and model details replace generic AI chat labels. Browser verified against the live node; frontend and Windows desktop builds passed. Desktop restarted and both GLM and Codex services remain online.
