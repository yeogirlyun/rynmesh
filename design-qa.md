# Agent conversation and CLI capability QA — 2026-10-13

Scope: correct the repeated progress disclosures highlighted in the supplied Ryn screenshots and verify local CLI assistant readiness. This is not a pixel fidelity certification against Codex: no screenshot of the requested Codex reference state was supplied.

Reference: supplied screenshot 1, reproduced as docs/acceptance/antigravity-agent-20261013/before.jpg. Implementation: after.jpg. Both are 1065 x 766 CSS pixels, same task, light theme, navigation collapsed, top of transcript. comparison.jpg places both captures together.

Findings fixed:
- P1: five separate disclosures consumed roughly 280 pixels before the answer. One 26-pixel activity row now represents seven underlying calls, including calls without commentary. The result remains directly visible, and complete commentary and tool labels are available on expansion.
- P1: CLI installation and ordinary chat readiness were mistaken for assistant readiness. Antigravity now has a current passing two-turn capability record. Codex uses constrained structured proposals validated against allowed tool names and argument schemas; provider environments remain empty and host execution requests are rejected. The new running node reports both as available, agent_supported=true, tool_support=supported.
- P2: verbose capability errors were embedded in dropdown options. Options now show the model name and a short status when necessary. Both model options were verified enabled in the rendered new-task page.
- P2: Antigravity service was labeled API. It now uses the local CLI label.

Typography, spacing, colors, images, and copy: existing product typography/color tokens and supplied assets were preserved; activity uses muted 13px text, a 14px library chevron, and a single compact row. No raster assets were added. This checks the requested scoped correction, not the unavailable Codex visual source.

Validation: 70 relevant backend tests and 62 frontend tests passed; production frontend and Windows desktop builds passed. Real Codex and Antigravity tool probes passed; no ordinary user conversation was submitted during browser QA.

final result: passed (scoped corrections)
Codex pixel-for-pixel comparison: blocked pending a screenshot of the corresponding Codex state.

## Codex composer and model selection — 2026-10-15

Scope: the user's supplied Codex screenshot and Ryn input-area complaint. Preserve the Ryn application navigation and brand; match the project welcome, external context strip, input card, and compact model control. The old section above describes an earlier request and is retained as history.

Source: `C:/Users/子陵/AppData/Local/Temp/codex-clipboard-0868a8f5-b7b8-4831-9c97-83248bd0216d.png` (1378 × 807), and `codex-clipboard-96712708-cd75-45f1-a8a1-853d5285731f.png` for the reported nested focus borders. Current evidence: `output/agent-codex-composer-20261015/`.

Comparison history:
- `comparison.png` contains source and implementation together, cropped to the approximately 740 CSS pixel composer. Both page viewports were 1378 × 807, DPR 1. The source contains an attachment and multiple text lines; the implementation uses a short draft without an attachment. Compare the context strip and toolbar, rather than treating the different body height as a mismatch. IAB's overridden viewport capture is resampled; it is suitable for layout inspection, not a pixel-error score.
- `final-composer.png` and `final-model-menu.png` are sharp production-build captures at the browser's actual 616 × 687 viewport, DPR 1, connected to the newly installed native backend. No horizontal overflow (document scroll width 616). `final-composer-detail.png` is the corresponding 588-pixel input area crop. The 390-pixel responsive layout was checked by DOM geometry (composer width 362); its resampled screenshot is not used for fidelity claims.

Findings and resolutions:
- P1 fixed: global focus styles produced a second blue textarea border/shadow. The textarea now has zero border, no outline and no box shadow, including focused state. The single outer card retains its neutral border.
- P1 fixed: project details were inside the editable card and repeated the folder name. A separate gray 39-pixel context strip now shows the path once, followed by the device and settings controls.
- P1 fixed: a wide native model select conflicted with the reference. The compact trigger now shows the model and actual reasoning level, with a bounded custom model list, selected check, source groups, and only supported reasoning choices.
- P1 fixed during actual browser acceptance: clicking a visible reasoning label blurred the trigger and closed the menu before the native radio change. Preventing the label's mouse-down focus change preserves selection; both mouse selection and keyboard Space were verified. Added regression checks for callback, closure, focus restoration and selected value.
- P2 fixed: inherited order placed the plus button after permissions; inherited size made send 34 pixels. Scoped styles restore plus then permissions and a 30 × 30 circular send control.
- Welcome typography now uses a regular-weight project question and underlined path, with muted Ryn brand icon and no extra project suggestions. Input typography, neutral surface, 23-pixel card radius and toolbar spacing follow the source. Ryn's existing icon library supplies folder, computer, settings, microphone and arrow icons; no new raster assets were needed.

Functional evidence: 140 frontend and 226 backend tests pass. Production frontend and Windows NSIS builds pass. The real installed CLI exposes low, medium, high, xhigh, max and ultra, default low. A production browser conversation selected high by mouse, submitted `reasoning_effort=high` to the installed backend, completed, and displayed “对话界面和模型强度验证通过。” (`conversation-verification.json`). Original 12 task IDs, both saved projects and identity were preserved; the additional acceptance conversation is retained. Unsupported models have no invented reasoning options. Existing task model changes retain the guarded server flow.

Independent visual review found no remaining P0–P2 overlap, clipping, hierarchy or overflow issue in the scoped regions. The supplied source shows the model selector closed; expanded-menu behavior was verified, but its pixel equivalence to an unavailable Codex expanded state is not claimed. Native installation is verified by process identity, executable hashes and real backend response; production browser screenshots are labeled as browser evidence, not native-window captures.

final result: passed

## Home and Apps redesign — 2026-10-16

Scope: the Home page (`/`) and the Apps page (`/apps`) only. Reference: the approved design canvas (direction B: device workbench with a List / Map switch, and an installed / not-installed app launcher with one drawn icon per app). Branch `design/home-apps-refresh`, built in an isolated worktree from `c74813d`; nothing from the main working tree's uncommitted changes is included.

What changed
- Home: summary chips, List view (grouped device rail, selected-device card, service cards) and Map view (the existing connection map, restyled in place via two new optional `DeviceTopology` props that default to the old behaviour). The chosen view is remembered per browser (`ryn.home.view`, storage failures ignored).
- Apps: installed apps and not-installed apps are separate sections, category tabs became filter chips, and each of the 17 apps has its own drawn icon and colour. The first-run installer, the disable / uninstall / details / Agent dialogs, launch verification and `/settings/plugins` are untouched.
- Files of this change are listed in `redesign.files.txt` (new: `homeModel.ts`, `HomeParts.tsx`, `home-refresh.css`, `appIconData.ts`, `appIcons.tsx`, `AppsLauncher.tsx`, `craft-apps-refresh.css` and three test files; edited: `Pages.tsx` (`PersonalHome` only), `DeviceTopology.tsx`, `CraftApps.tsx` (non-manage view), `personal.*.json`, `ui.*.json`).

Review round 1 — fixed
1. The Apps filter is owned by `CraftApps`, so it survives the installer replacing the launcher (installing or going Back no longer resets it to "All"). Regression: `CraftAppsFilter.test.tsx`.
2. Home counts as offline only a device that reports `online === false`; unknown status (`null`) is neither online nor offline. Tests in `HomeViews.test.tsx`.
3. Every selector in `home-refresh.css` is under `.rh-home`, media queries included (148 selectors, 0 outside). `craft-apps-refresh.css` is under `.craft-apps-v2` (86, 0 outside). Checked with a script that fails on any rule outside the root.
4. English narrow layouts: the font warning wraps inside the card (at 700 px a card is 197 px wide and the warning was 284 px); below 700 px the footer note is a two-column grid (icon + text) and the Friend apps link takes its own row (the text had been squeezed to 30 px, one word per line). Home rows (service header, browse row, nearby list) also wrap instead of squeezing. The audit that found both problems reproduces them when the old styles are injected.
5. "Available to install" counted native-only apps that cannot be installed here. The section and the header chip now say "Not installed"; native-only apps stay in it with their "Native only" label.

Evidence (redesign-only tree = pristine `c74813d` + the files above)
- `tsc -b --noEmit` passes; `vite build` passes.
- `src/personal`, `CraftApps.test.tsx`, `AppsLauncher.test.tsx`, `CraftAppsFilter.test.tsx` and the copy-lint `Localization.test.tsx`: 19 files, 146 tests pass (run serially). The same 146 pass on the worktree with the system-apps work stacked on top.
- Layout audit in a real desktop window (15 px scrollbar), English and Chinese, 1440 / 1024 / 700 / 390 px: no horizontal overflow, nothing escaping its card, no squeezed text, for Home List, Home Map and Apps (24 combinations). Light and dark checked on screenshots at 1440.
- Other pages unchanged: for `/settings/plugins`, `/services`, `/devices` and `/apps/workspaces` (1440 px English light and 700 px Chinese dark) a fingerprint of every element's computed layout and colours is identical between this tree and an untouched `c74813d` tree.
- Windows desktop proof: six captures of the real "Ryn" window in `docs/acceptance/home-apps-redesign-20261016/` (Home List, Home Map, Apps, Apps not-installed section, Home dark, Apps dark in Chinese). They come from the debug desktop build in this worktree (`src-tauri/target/debug/app.exe`) loading the redesign-only frontend from the dev server on :5173, a freshly started node (`RYNMESH_HOME` in a throwaway folder, `RYNMESH_AUTO_REGISTER=0`, `RYNMESH_PEER_HOST=127.0.0.1`), a 1280x800 window, captured with PrintWindow. To reach the launcher with some installed apps, three built-in tools (resume, recruit, desk; registration only, no download) were added to that throwaway profile; nothing was installed from the network. This is not a packaged installer build.
- Not verified: opening a real editor from Apps (unit tests only), a packaged release build of the desktop app.

Known issues, not part of this change
- While an operation is running (`busy`), the Agent button of an installed app stays clickable. The old Apps page behaved the same; the redesign keeps it and a test documents it.
- `RynAgent.test.tsx` and `RynAgentSending.test.tsx` time out on this VM, also on an untouched `c74813d`.

Not part of this acceptance
- The system-apps work found in the same worktree (Start menu / Launchpad / "Open with" apps, `src-tauri/scripts/generate-app-icons.mjs`, `src-tauri/icons/apps/*`, Rust and Tauri changes, `CraftSystemApp`, `craft.*` strings, `craftApps.css`). It is kept intact and listed in `native-and-system-apps.files.txt`; its two edits inside `CraftApps.tsx` are in `CraftApps.system-apps.patch`. It builds on this change, never the other way round.

Notes for merging
- The main working tree has uncommitted edits to `CraftApps.tsx`, `CraftApps.test.tsx`, `craft.*.json` and `craftPlugins.ts`. This branch was cut from the commit, so `CraftApps.tsx` will conflict in the non-manage view; keep the launcher and re-apply the other edits. `AppsLauncher` already shows `officeAgentInfo` when that key exists.
- The design's miniature map thumbnail in the device list was replaced by a "Connection view" button that switches to Map.

final result: passed (scoped), with the pre-existing Agent test timeouts noted above
