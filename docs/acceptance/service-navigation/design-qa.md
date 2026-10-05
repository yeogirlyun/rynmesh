# Service navigation / AI sources — 2026-09-24

final result: passed

Scope: the selected second design, implemented in the existing application. This does not supersede earlier NAS native-window acceptance limitations. No backend, stored configuration, application route, service implementation, or desktop packaging was changed for this redesign.

## Visual evidence

- Source visual truth: `C:/Users/子陵/.codex/generated_images/01a0d25e-8b55-74a1-8401-c042965c64d5/exec-383ca298-65d1-4dd4-9af5-9151536379f2.png`.
- Source raster: 1586 × 992, proportional design target 1280 × 800 CSS pixels.
- Implementation: `empty-desktop.png`, `empty-dark.png`, `populated-desktop.png`, `empty-mobile-dark.png` in this folder.
- Desktop CSS viewport: 1280 × 800, devicePixelRatio 1. Browser screenshot exports scale the 1280-pixel capture to 1265 pixels (1265 × 791); compare source and implementation proportionally rather than treating export scaling as font drift.
- Mobile CSS viewport: 390 × 844. Document client and scroll widths both 375 pixels (15-pixel scrollbar), so there is no horizontal overflow. All six service links remain visible in two rows.
- State: AI sources, Chinese, no configured source for the reference comparison. Populated state was separately checked against the user's live node. Empty state uses a read-only local QA proxy with explicit empty AI responses; no user sources were deleted.
- Source and final implementation images were opened together in one comparison tool input. Typography, labels and navigation are readable at that resolution, so no additional cropped-region comparison was necessary.

## Findings and comparison history

1. P2 fixed: global shell heading styles overrode the first-use heading (18 px instead of 22 px) and paragraph spacing. Scoped the new heading and paragraph styles to the AI sources page. `empty-desktop-initial.png` records the initial state; `empty-desktop.png` confirms the corrected hierarchy and spacing.
2. P2 fixed: connection action text inherited muted styling. Corrected selector specificity; final actions use the existing theme's blue token in both appearances.
3. Moved the empty-state Add source action into the onboarding panel to match the selected composition. Populated-state Add source remains in the page header so users can add more sources without a welcome panel.
4. Post-fix comparison: no actionable P0/P1/P2 visual findings. Retaining Browse services and the sharing/mapping footer is an intentional change from the mock to meet the user's explicit requirement to preserve functionality.

## Required fidelity surfaces

- Typography: retained the application's Segoe UI / Chinese system fallback; 30 px page title, 22 px onboarding heading, 20 px connection labels, 14 px descriptions, 14 px desktop navigation. No global font replacement.
- Layout: original sidebar and topbar; stable horizontal service navigation with an active underline and right-aligned settings entry; three equal connection columns on desktop, stacked on mobile; one API explanation panel. All buttons are real controls.
- Colors: existing light/dark tokens, subtle blue surface, blue actions, thin borders. Dark mode preserves readable contrast and active states.
- Imagery: retained the supplied product logo and existing provider assets. Standard Cloud/Server/Terminal/Code icons come from the installed Lucide library. No generated decorative assets or rasterized UI.
- Copy: Chinese and English translations added for the new explanatory text. Sources connect models/providers; API access supplies endpoints/model names/project keys. No new unsupported capability claims.

## Functional preservation

| Existing capability | Entry after redesign |
| --- | --- |
| My services and service detail actions | Fixed My services link; original page retained |
| Service catalog, video rendering, secure web access | Fixed Browse services link; original routes retained |
| AI conversations | Fixed AI workspace link; original chat screen retained |
| Source create/edit/delete, test, model list, limits, custom parameters, sharing, enable/disable | AI sources; original configuration dialogs and handlers retained |
| Native GGUF import, start/stop, runtime/settings, existing Ollama/LM Studio connection | Local model entry; original NativeModelPanel retained |
| Codex/Claude CLI detection, setup and sharing | Local CLI entry; original handlers retained |
| API protocols, address copy, project keys, code examples | Fixed API access link; original page retained |
| Model aliases and CLI sharing management | AI sources footer and existing API mapping link |
| Devices, tasks, NAS, explore, messages, settings | Original primary navigation and routes retained |

## Validation

- 60 tests passed across 10 suites: service navigation, AI sources, AI workspace/API, LLM service management, service catalog, service experiences, private AI chat, native models, personal localization and UI localization.
- After final presentation adjustments: 9 focused navigation/source tests passed again.
- `npm run build` passed (TypeScript plus Vite). Existing large-bundle advisory remains.
- Browser checked: live sources; API endpoint/protocol/key/model controls; mapping link; stable catalog navigation; first-use provider, native model, existing local service and CLI dialogs; light/dark; 390-pixel layout. Configuration writes were not exercised against the user's node.
- Browser error logs were empty during the main checks. Near the end the external local node briefly stopped listening on port 8791, causing a fetch failure; no node process was stopped or restarted by this task. An independent `?client=fixture` read-only preview was prepared to avoid that dependency.
- The node subsequently recovered independently. Final live verification showed the existing GLM source, local Codex CLI and a remote MacBook Codex source; the populated screenshot was refreshed after recovery.
- Service catalog discovery was still loading during its browser check; catalog behavior is covered by the existing unit suite, not claimed as a completed live network discovery.
- Native desktop packaging and a new Windows installer are outside this change; this is the shared frontend implementation with local browser preview.

## Preview

- Live local application: `http://127.0.0.1:5173/services/sources`.
- Independent first-use preview: `http://127.0.0.1:18849/services/sources?client=fixture` (sample data; writes rejected).
- QA-only proxy: `.codex-tmp/service-navigation-preview.cjs`. It is not part of the app bundle.

## Implementation checklist

- [x] Fixed navigation before and after configuration.
- [x] Preserve all existing routes and service handlers.
- [x] Reuse working forms for all three connection choices.
- [x] Preserve populated-state search, device filters and advanced entries.
- [x] Check translations, dark mode, narrow layout and keyboard-visible focus styles.
- [x] Regression tests, production build and post-fix visual comparison.

Follow-up polish: none required for this scope.
