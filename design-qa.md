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

## Services page redesign - 2026-10-18

What changed
- `/services` only (`PersonalServices`): a summary band (service count, device count, a ready / offline / not connected meter), a segmented filter with counts, one grouped table (service, provided by, status, action) and a detail panel with a connection strip (this device, route, provider). The rules are unchanged: same ordering (ready, offline, needs connection), filters, search, `?service=` selection, Overview / API access tabs, Connect device link, Manage access for owned devices only, and the API tab behaviour for AI and other services.
- Files: new `ServicesParts.tsx`, `services-refresh.css` (every selector, media and container queries included, is under `.rs-services`: 150 selectors, 0 outside), `ServicesView.test.tsx`; edited `Pages.tsx` (`PersonalServices`; the old `ServiceRow`, which only this page used, is removed), `personal.en.json` and `personal.zh-CN.json` (7 keys).
- Not touched: the section tabs above the page (they come from `Shell.tsx`), the Devices page, every other Services sub page and all dialogs.
- Font: the page inherits the app font. The design mock used Plus Jakarta Sans; using it in the app means bundling the font.

Evidence (scratch copy of this tree on a local disk, not the share)
- `tsc -b --noEmit` and `vite build` pass.
- `npx vitest run src/personal src/Localization.test.tsx --no-file-parallelism`: 17 files, 120 tests pass, including the global copy lint. `ServicesView.test.tsx` has 7 tests: summary, meter and list order; route text per row and filter counts hidden from button names; selecting from the row button or anywhere on the row; API tab rules; search and empty state; the sample-data dialog keeping the selection (added in review round 1); Chinese copy.
- Browser, fixture mode, with an offline and a needs-connection service added in the scratch copy only: 1520 px (table plus detail panel), 1300 px, 1024 px, 700 px and 390 px have no horizontal overflow and no clipped titles (script audit); light and dark checked on screenshots; Chinese checked at 1520 px.
- Review round 1 below adds a check in the real desktop window with real node services. Still not done: a computed-style fingerprint of the other pages against an untouched tree (scoped CSS check and the Home tests only).

Known gap
- The service section tabs and the Services sub pages keep their old look until they get their own pass.

### Review round 1 - 2026-10-18 (acceptance findings)

Findings and fixes
1. Dark-mode tag contrast (P2). Cause: in dark mode `.rs-tag` kept the light-mode hue ink (`--hd`), about 2.17:1 on the tag background. Fix, all under `.rs-services`: in dark, tags and device glyphs take `color-mix(in srgb, var(--h2) 58%, var(--rs-ink))`; the global `--hd` is not touched. The light-mode amber `--hd` measured 3.84:1, so it is now `#965a00`. Measured as computed colour against the composited background, in the page: light 4.68:1 or better for all seven hue classes (probe elements) and 5.92 to 6.8:1 on real rows; dark 8.03:1 or better for all hues and 8.09:1 on real rows (online API and Local CLI tags, the offline slate tag).
2. Rows at small list widths (P2). Cause: the row was a wrapping flex line, so service, provider, status and action stacked into three or four layers at 343 to 449 px lists. Fix: a grid with named areas. Default is two rows (service | status, provider | action); from a 420 px list the provider line is indented under the title; from a 700 px list it becomes four columns, and `.rs-lh` shares the exact same column template as `.rs-row`. Nothing is hidden, clipped or scrolled. Measured (script audit of each row's cells): browser 390 px (list 343), 700 px (list 449), 820 px and 1300 px give two rows; 1024 px and 1520 px give one band; desktop window lists of 693 px give two rows and 709, 733 and 1034 px give one band; no overlaps, nothing outside the row, no horizontal overflow.
3. Sample-data dialog selecting the row behind it (P3). Cause: the shared `Modal` renders inside the row's `article`, so clicks on dialog text bubbled to the row's click handler. Fix: the row ignores clicks that start in `a, button, dialog`, and the select button no longer calls `onSelect` twice. The shared `Modal` is unchanged. The regression test (Open on an unselected sample service keeps the selection; dialog text and backdrop clicks keep it; closing keeps it; the select button and plain row content still select) fails against the old handler and passes now.

Found while checking in the desktop window and fixed (not in the findings list)
- The table's last column was 100 px, but the English "Connect device" button needs about 115 px, so it spilled left to 1 px from the status pill. The column is now 126 px in both header and rows (27 px between pill and button).
- In the table the provider caption was cut with an ellipsis ("Route ..."), which hid the route status near 700 px. It now wraps.

Evidence (scratch copy on a local disk; the three source files and the locale files in it are byte-identical to the tree)
- `npx tsc -b --noEmit` exit 0; `npx vitest run src/personal src/Localization.test.tsx --no-file-parallelism` 17 files, 120 tests pass; `npx vite build` exit 0 (only the existing chunk-size and dynamic-import warnings). tsc, vitest and the build were run once more after the two final CSS-only edits listed above (column width, caption wrap), and the desktop checks of those two edits were made after them. Scope check 150 selectors, 0 outside `.rs-services`; `personal.css`, `secondary.css`, `Shell.tsx`, `components.tsx` and `model.tsx` have no changes.
- Real desktop (WebView2, Windows): the existing debug `Ryn.exe` embeds an older bundle (page URL `tauri.localhost`, no `.rs-services`, the old service rows), so it is not evidence for the redesign. It was started isolated (own `RYNMESH_HOME`, own WebView2 profile, its own sidecar; ports 8791 and 9222 were free before and after, and 5173 was used only by the dev server started for this check; no installed program, existing node or autostart touched), then the same window was pointed at a dev server serving this tree. Result: `.rs-services` present, three rows, Tauri IPC present. Real node services (Antigravity and ChatGPT as Local CLI, Z.ai GLM as API; ChatGPT and Z.ai show their brand logos, Antigravity the generic tile) checked in light and dark at 1280 px, near 960 to 1000 px and maximised; fixture data also checked near the 700 px threshold. All processes were stopped and the isolated profile deleted afterwards.
- Screenshots are kept outside the repository in the session scratchpad (`desktop` folder): old embedded page, current page at 1280 light and dark, maximised, and near the threshold.

Not verified
- A freshly compiled Tauri build that embeds the current `dist`. It needs a full cargo build and none was made, so the `tauri.localhost` origin and the packaged assets of a release build are untested with this page. The desktop check used the dev server inside the real WebView2 window.
- Ready and offline rows from a live node: the isolated node only had three not-connected services. Those states were checked on fixture data, and offline with services added temporarily to the scratch copy (restored; the tree's `model.tsx` was never changed).
- A 390 px width in the real desktop window (its minimum width is 960 px; 390 px was checked in browser emulation only).
- Chinese copy in the desktop window (browser at 1520 px and the unit test only).
- The sample-data dialog in the desktop window (the real node had no sample services; covered by the unit test and the browser).
- A computed-style fingerprint of the other pages against an untouched tree.
