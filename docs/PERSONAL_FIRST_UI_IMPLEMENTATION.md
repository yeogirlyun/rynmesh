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
- Explore/library, content details, For you, Search & ask, Publish, Messages, connection details, service catalog/setup/API access, service experience pages, advanced settings, welcome and connection recovery now share the new visual system.
- Windows desktop: bundled node, tray, single instance, background preference, launch at startup, dynamic node port, WebView2 local authentication, and installer-only updates for the frozen node.

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
| Device pairing | Create a personal space and redeem a signed, encrypted, single-use invitation. Coordinator must be online. No numeric code or cloud account required. |
| My remote devices | Signed membership supplies device affiliation; discovery/trust alone does not confer ownership. Remote control is not granted by membership. |
| Service sharing / revocation | Live local/space AI access is enforced by the provider. Managers can remove devices; offline membership expires within 24 hours. Per-service guest grants remain outside this pass. |
| Document conversion / transcription | Sample capabilities in preview only. |
| Startup / close preferences | Functional in the Windows client; browser controls explain their desktop-only availability. |
| Generic task results | AI text results can be downloaded when returned; a general file-result and work cancellation API is not implemented here. |

Remote desktop, NAS and household LAN access remain deferred as requested.

## Verification

- Production build: `npm run build` passed.
- Personal-space update: all 52 frontend tests across 12 files passed, including membership-derived ownership, management confirmations and preview isolation.
- Personal-space update: 118 focused Python checks passed across space membership, node authorization, AI provider hardening and inference API coverage. Earlier desktop authentication/default checks also passed during the UI/package pass.
- Browser checks: both themes, Home, Services, Devices, Tasks, Settings, AI chat, pairing/sharing/edit dialogs; device edits reflected on other pages; fixture chat response.
- Responsive checks at 390px and 1280px widths; narrow navigation and dialog layout checked.

- Windows x64 NSIS installer built successfully; bundled executable launched without an external Python runtime. Actual native checks covered Home, onboarding, both themes, startup registration/removal, background operation, and node shutdown when the app exits.

Personal-space pairing and permissions are covered in `PERSONAL_SPACE.md`; physical cross-network acceptance remains separate. The Windows package is an unsigned test build; clean-machine installation and signed public distribution remain release acceptance tasks. See `WINDOWS_BUILD.md`.
