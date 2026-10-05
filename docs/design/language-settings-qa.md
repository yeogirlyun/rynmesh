# Language settings design QA — 2026-09-23

final result: passed

## Visual evidence

- Source visual truth: `C:/Users/子陵/.codex/generated_images/01a0ccf8-e28b-71b3-a1fc-22ebeb96fe9e/exec-d0fb0264-1bf7-4359-b108-f73439c3370d.png` (1488 × 1058).
- Implementation: `http://127.0.0.1:5173/settings?client=fixture`, captured through the Codex in-app Browser and displayed beside the source image in this task. The browser screenshot API did not expose a local file path.
- Comparison viewport: 1488 × 1058 CSS pixels at devicePixelRatio 1. Both source and implementation were viewed in dark appearance, Simplified Chinese, with the language menu open and Simplified Chinese selected.
- Focused region: the language row, trigger, and three menu choices were legible in the full-size comparison; no separate crop was needed.

## Findings and iteration

- [P2, fixed] The language row initially lacked the reference's blue focus surface. Added a muted selection background and border, then recaptured the open menu.
- Existing Ryn navigation and the current Settings subnavigation remain present. The generated mock simplified these areas. Preserving the real app's structure is intentional.
- Existing personal-space names remain user data and are displayed verbatim.

## Required fidelity surfaces

- Typography: existing Ryn font stack and weights retained; Chinese heading and option labels remain readable at 1488 px and the Windows minimum width of 960 px.
- Layout: compact language row sits above appearance; the menu is anchored to its trigger and does not cause horizontal overflow at 960 px.
- Color: dark graphite background and muted slate-blue selection follow the current app tokens and the selected mock.
- Imagery: this settings interaction uses only existing Ryn and Lucide UI icons; no raster content or invented illustrations are needed.
- Copy: only Follow system, 简体中文, and English appear as language choices; the settings and navigation labels change with the selected locale.

## Functional verification

- Language menu opens, switches between Chinese and English immediately, and retains English after page reload.
- Focused tests cover language detection and saved selection: 2 passed.
- Frontend production build passed; browser console reported no errors.
- Browser at 960 × 640 showed no horizontal overflow or hidden persistent controls.
- System appearance was selected in the rendered UI: the current system light theme appeared immediately and the selection remained after reload. Existing `useAppearance` tests simulate a system dark-mode change, verify the UI follows it, and verify an explicit override wins; 9 focused tests passed.
- Windows Tauri `Ryn.exe` and NSIS installer were rebuilt on 2026-09-23 with the shared frontend. The generated `installer.nsi` includes both `English` and `SimpChinese`. The rebuilt desktop process restarted, and its bundled node listened on port 8791 and returned a running status. Native WebView visual capture was unavailable, so the native window's pixels were not separately inspected.
