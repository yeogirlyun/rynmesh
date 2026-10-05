# AI workbench polish QA — 2026-09-23

Result: **passed for the shared frontend**. Windows packaging/startup is verified separately; native WebView screenshot automation is unavailable in this session.

## Scope and visual evidence

- Reference: user screenshot `C:/Users/子陵/AppData/Local/Temp/codex-clipboard-16b76dd8-e5b7-41c1-b421-f9483577430d.png`.
- Source and implemented screenshots were emitted together in the conversation at 1446 × 800. The implementation uses a real Codex reply; browser and desktop histories are stored separately.
- Retained the existing application chrome and blue-gray palette. Refined only the workbench: labeled device/service/model controls, compact history, bounded conversation card, quieter message actions, and a larger composer with model and keyboard hints.
- Moved the raw Codex thread ID into expandable conversation details. The saved thread, copy action, and model selection remain available.
- Assistant Markdown now renders paragraphs, emphasis, lists, tables, links and code. User messages remain plain text. Raw HTML and remote image loading are disabled.
- Checked light and dark themes, plus a 960 × 700 viewport. At 960px, document width equals viewport width: no horizontal overflow. Restored the original theme and viewport after QA.

## Functional evidence

- Real request through the live Windows node to Codex GPT-6-Luna returned a sentence, two bold labels and a two-item list; formatting displayed correctly.
- Saved Codex thread: `01a0cd9d-31c4-7f81-b581-18ad3ad20d0a`.
- New conversation, model controls, sending, conversation details, and scrolling checked in the browser against the live node (no fixture client).
- 12 related frontend tests passed across PrivateAIChat, AIWorkspacePages and ChatMarkdown. Coverage includes history, Codex model/thread metadata, raw NAS output preservation, Markdown formatting, and unsafe content handling.
- Production TypeScript/Vite build passed. The bundler reports a size advisory for the main chunk.
- Windows release and NSIS package rebuilt successfully. Launched through the desktop Ryn shortcut; running window title is Ryn, PID 50860, executable is the newly built release. The bundled node reports Codex installed/configured/online and returns seven available models.
- Updated installer: `release/windows/Ryn-0.6.2-codex-cli-windows-x64-setup.exe`; SHA256 `33D85C8A8FC1D7843B90624C938985223648F8BA60D5640C79681AF7E2C6A991`.

## Limits

This polish does not change CLI routing or expand remote API capabilities. Visual verification covers the shared frontend in the browser, not a captured native Windows WebView. Existing NAS QA is outside this change.

## Follow-up: controls and ChatGPT identity

User targets: `codex-clipboard-d4c7b871-0483-4efb-b2c4-5673c96dc065.png` and `codex-clipboard-149148df-a9a6-4fa5-b72d-6974341a7841.png` in the user's Temp directory. These request corrections to the native select appearance and the assistant identity, rather than exact reproduction of the faulty UI.

- Replaced nested native selects with single-field buttons and styled listboxes for device, service and model. Menus include selection marks, arrow/Home/End navigation, Enter selection, Escape dismissal, outside-click dismissal and disabled states.
- Corrected an initial device-art height cascade discovered in rendered QA: all toolbar buttons now measure 62px high.
- ChatGPT name and original OpenAI brand SVG appear in the service selector, reply avatar, empty state and composer. The SVG comes from LobeHub Icons with its license retained. Codex CLI remains the actual adapter and is identified in conversation details; routing and stored service IDs are unchanged.
- Font sizes, field spacing, borders, focus/selected tokens and icon contrast checked in light/dark. Brand vector is crisp, without the former purple generic assistant tile. Original and implementation captures were emitted together; browser frame/crop and conversation content differ, so comparison focuses on the marked controls and reply identity.
- Revised screenshots show aligned controls, full model menu and ChatGPT reply avatar. Final result: **passed for this shared frontend scope**.
- 13 relevant tests passed across four files, including real-model selection propagation and keyboard/menu dismissal behavior. No additional paid inference was needed for presentation-only changes.
- Follow-up Windows production build and NSIS packaging passed; restarted through the desktop shortcut. Updated installer SHA256: `8EAA635C278DDD1BF06FA8D7E306B51303540497BD9B620BEFD66453E8DD2247` (supersedes the earlier polish package).
