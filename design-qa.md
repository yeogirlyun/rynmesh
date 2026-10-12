# Ryn agent conversation QA — 2026-10-12

final result: passed

Scope: Codex-style conversation hierarchy and removal of task administration from the message body. This records the chat work only. The earlier NAS report, including its unresolved native acceptance result, is preserved at `.codex-tmp/design-qa-before-chat-ui.md`.

## Visual evidence

- Reference: `.codex-tmp/codex-chat-reference.png`, actual Codex desktop, 1621 × 799 capture.
- User problem screenshot: `C:/Users/子陵/AppData/Local/Temp/codex-clipboard-16e307bf-4fb3-49e8-9807-b30bad0e34fe.png`.
- Implementation: `.codex-tmp/ryn-chat-ui-final.jpg`, native Windows client, 1282 × 832 capture.
- Additional states: `.codex-tmp/ryn-chat-ui-after.jpg` (expanded main navigation), `.codex-tmp/ryn-chat-menu-after.jpg` (task controls menu).
- Source and implementation opened together in the same comparison input. Native captures use their original pixels; no claim of matching screenshot density or pixel-perfect geometry. Comparison concerns the chat column, approximately 737 px in Codex and 780 px in Ryn, rather than different desktop chrome. Codex is running this long conversation; Ryn displays the user's completed `hi`. The running controls are checked in the UI regression test without starting a paid task.

## Findings resolved

- P1: Execution details and record management occupied the conversation. Both now live in the header actions menu; no administrative section remains in the message scroll area.
- P1: A banner for unrelated waiting tasks displaced the selected conversation. It now lives inside the actions menu; task state remains available in history.
- P2: Scope and skill configuration displaced the welcome/chat experience. New-task configuration now lives in composer Options; existing-task model, scope, budget and plan remain in the header menu.
- P2: Running tasks lacked a composer Stop control. Empty running composer now shows Stop using the existing authorized cancel action, respecting control revision and grant checks. Drafts retain the existing send/steer behavior.

## Fidelity surfaces

- Typography: existing system typography retained, readable message body; removed repeated assistant/model headings and technical result headings. Exact Codex font rasterization is not claimed.
- Spacing: right-aligned user bubble, open assistant text, centered chat column, anchored compact composer. Main navigation can be collapsed without removing access; final capture uses the focused layout.
- Colors: existing Ryn light/dark tokens retained; pale user bubble, quiet borders and menu shadow. This is a hierarchy/interaction adaptation, not a replacement of the application theme.
- Assets: standard Lucide icons for actions, send and stop; no generated artwork needed.
- Copy: task administration absent from the conversation; original approval, input, error, artifact and verification semantics preserved. Detailed goal verification remains accessible through the menu.

## Validation and delivery

- TypeScript check passed; all 48 RynAgent tests passed, including controls, approvals, scope, continuation and receipts.
- Windows production build passed. Native existing conversation and header menu opened successfully; Escape closes the menu and returns focus to its trigger.
- Desktop shortcut now points to `C:/Users/子陵/AppData/Local/Programs/Ryn-Preview/20261011-chat-ui/Ryn.exe`; client launched and agent page left open.
- Backend binary unchanged (matching SHA-256 AC5AFC96BC77A2256A923FF49011114AF73FB70C469AF14888CE38A7843335E6). No new model call or paused Codex task was started.

## Remaining limits

- Exact pixel cloning and identical content/state are not claimed; product navigation and Ryn-specific approval/settings remain functional.
- Running UI checked with realistic server fixtures; no extra paid inference started for visual testing.
