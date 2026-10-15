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
