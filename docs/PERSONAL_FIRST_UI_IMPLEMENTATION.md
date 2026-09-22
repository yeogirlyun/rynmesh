# Personal-first UI implementation

Branch: `development/personal-first-implementation`

Design reference: `docs/design/personal-first-en-v1/`

Date: 2026-09-22

## Included

- English navigation: Home, Services, Devices, Tasks, Settings; Explore and Messages remain available under More.
- Light, Dark and System appearance. Explicit choices persist; System responds to OS changes.
- Shared monitor-only desktop artwork, separate laptop artwork, and consistent service icons.
- Home device cards, service list/detail view, device list/detail view, task filters/details, appearance settings and redesigned AI chat.
- Device edit, pairing and sharing dialogs; global search with Ctrl+K / Command+K; narrow-window navigation.
- Local device names save through the existing node API. Remote nicknames, private notes and icon preferences remain local and are scoped by node identity and client mode.
- AI chat continues using the existing conversation storage and order APIs. Explicitly requested missing providers do not fall back to another provider. Device/model selection is visible and disabled during a request.
- Tasks use consumer AI orders, provider AI orders and work results. Cancellation waits for the server response; unavailable provider information is not invented.
- Existing service setup, API keys, content, messaging and advanced settings remain accessible.

## Preview

From `webapp`, run:

```sh
npm run dev -- --host 127.0.0.1 --port 5186
```

Open `http://127.0.0.1:5186/?client=fixture` for the labelled design preview, or omit `client=fixture` to use the local node.

The preview includes Home PC, Laptop, Work PC, and illustrative document/transcription services. Preview data never appears as live device data. The UI is implemented as React components and vector assets, not screenshots.

## Existing API boundaries

These are visible limitations, not completed backend features:

| Design interaction | Current implementation |
| --- | --- |
| Pairing code | Dialog restored; live mode uses existing device discovery. Code pairing and mutual confirmation require a backend protocol. |
| My remote devices | Only the local device has verified management authority in live mode. Discovery and trust do not confer ownership. |
| Per-device service sharing / revocation | Selection UI restored. Live saving is unavailable until the node provides enforceable access APIs. Preview saving explicitly changes no real permissions. |
| Document conversion / transcription | Sample capabilities in preview only. |
| Startup / close preferences | Preview controls only; live controls show existing availability. Native startup configuration is not added. |
| Generic task results | AI text results can be downloaded when returned; a general file-result and work cancellation API is not implemented here. |

Remote desktop, NAS and household LAN access remain deferred as requested.

## Verification

- Production build: `npm run build` passed.
- All 46 frontend tests across 10 files passed, including new metadata/appearance/provider-selection coverage.
- Browser checks: both themes, Home, Services, Devices, Tasks, Settings, AI chat, pairing/sharing/edit dialogs; device edits reflected on other pages; fixture chat response.
- Responsive checks at 390px and 1280px widths; narrow navigation and dialog layout checked.

This is a frontend implementation pass. Live multi-computer pairing, service permissions and native desktop packaging still require separate integration acceptance.
