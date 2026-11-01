> 公开交付副本（2026-10-19）：保留原验收结论、失败和范围；本机绝对路径已规范化，未提交的原始证据仅在本机留存。文中的旧快照/安装包 hash 不代表本次源码；原报告 SHA-256：`1aa23e935a84ddd5a431f06122f91a65f7e5fdf0da5528262c4d75bfb19d7726`。

# Independent business event and HTTP client review

Verdict: **PASS within this subreview's scope.** No blocking issue found in the recorded Gemini Word/PDF task provenance, event accounting, HTTP wrapper, or fresh Word-to-PDF input chain. This is an independent offline review of the raw own-task snapshots and client code; the parent review owns document arithmetic/rendering, installation preservation, and the final overall verdict.

Reviewed on 2026-10-18 (Asia/Hong_Kong). Evidence root `W = <user-home>/Documents/Codex/2026-10-18/new-chat-4/work/ryn-gemini-fix-20261018`.

## Independently established results

| Check | Word | PDF |
|---|---:|---:|
| Task ID | `agent_f7496d559ac74fe9998ff81fcb76ccb8` | `agent_86bbb6e76a1f4f92bb269031dd9f1d3a` |
| Status / run state | completed / ended | completed / ended |
| Contiguous events | 1–67 | 1–103 |
| Model started / completed / assistant events | 5 / 5 / 5 | 7 / 7 / 7 |
| Assistant tool proposals / paired results | 15 / 15 | 26 / 26 |
| Host handler dispatches | 14 | 25 |
| Automatic workspace authorizations | 4 | 5 |
| Additional human business prompts | 0 | 0 |
| Model response rejected events | 0 | 0 |
| `not_executed` / error-flag results | 0 / 0 | 0 / 0 |
| Explicit approval actions | 0 | 0 |
| Reported elapsed seconds | 147.578 | 198.234 |

The aggregate is **12 model calls, 41 paired tool results, 39 host handler dispatches, and 9 automatic workspace authorization events**. Every call ID occurs once in assistant proposals, tool messages, and tool-result events. All model-start attempts are `attempt=1`, and the model counts also match lifetime budgets. The two additional paired results are internal `ryn_discover_tools` results (Word seq 12, PDF seq 15), explaining the difference from handler counts. Reads and schema inspection are included in the handler totals. Neither total measures native successful writes or confirmed receipts.

Both task snapshots freeze `model_id` and `model_destination.catalog_model_id` to `local/antigravity-cli/model/gemini-4-argon-low`, use the same recorded owner peer and destination revision, and agree with the result's `antigravity_cli` adapter / `gemini-4-argon-low` model ID. The wrapper rejects requested model changes, missing Gemini, a wrong adapter, and a wrong underlying model. `context_window_basis=unknown_fallback` describes context metadata; it is not evidence of a model fallback.

Each task contains exactly one user message. Its bytes equal the original pinned client's generated prompt and its hash equals the recorded prompt hash. There are no later user messages, input requests, response rejections, failed/error events, pending approvals, or corrections in these complete event sequences. The Word prompt contains source business inputs without adding the computed totals as hints. No GLM substitute was used in the reviewed task bindings or client execution path.

## Source and execution evidence

The wrapper's `load_client()` validates the original client and helper SHA before loading them. It narrows cases to Word/PDF and enforces the exact Gemini identity. The original client imports standard-library modules and the helper's pure functions; it does not import the Ryn product runtime, instantiate `AgentRuntime`, or invoke the helper's live-worker routines. The helper file contains older live-worker functions but they are not called by this HTTP execution path.

The client has one task-creation call site at `installed_http_business.py:308`. Settings are read with GET at line 238. There is no PUT call site. Other writes create the per-task projects, restore UI project selection when necessary, or optionally approve/deny/cancel the same task. The actual reviewed runs did not need approval or timeout cancellation. There is no endpoint that posts a follow-up business message. The only business-file operation performed by the client is the required byte copy of the freshly generated Word into the new PDF workspace. Client evidence JSON is saved separately.

Every actual write-effect tool-start event is `ryn_craft_action`. Assistant proposals do not call `ryn_shell_execute`, `ryn_file_write`, `ryn_file_edit`, or `ryn_apply_patch`. Word Craft operations are `new_document`, `type_text`, `save_document`, and `open_document`. PDF Craft operations are WordCraft `open_document` / `save_document`, followed by PdfCraft `doc_open`, `page_add_text`, and `doc_save`. The final files match server Craft export events with operation IDs and the task artifacts API's created-by-task file SHA/receipt-SHA fields.

`business-outside.ps1` invokes the pinned wrapper using the repository virtualenv Python with `--run --live`, the exact Gemini model, installed endpoint `http://127.0.0.1:8791`, this round's installation proof, and run ID `gemini-installed-01`. `gemini-installed-01-exit.json` records **exit 0**, helper PID 12340, and completion `2026-10-18T11:35:19.5970791Z`; stderr is 0 bytes.

Before and after both tasks, the recorded own health keepalive connection's reverse ESTABLISHED tuple is attributed to PID **42636** by both `Get-NetTCPConnection` and `netstat`, with full backend SHA `8ad9968858ceeb1e5f43df2d993a211fc68c80389c3f9630574b23b6ac6f3a63`. Each record binds the installation proof SHA. The proof binds the outside identity file SHA; that identity records the ordinary installed chain **42424 → 48608 → 42636** with executable paths under `<user-home>/AppData/Local/Ryn`. Listener PID 0 does not override the exact established connection owner. This review deliberately did not hash the normal installed path from Codex's possible VFS shadow.

## Fresh file chain

| File | Bytes | SHA256 |
|---|---:|---|
| New Word output | 7217 | `c64d87e5409745fd3a8baf70386d1017b2d406348d725918bfd412c1cff28605` |
| PDF input DOCX copy | 7217 | `c64d87e5409745fd3a8baf70386d1017b2d406348d725918bfd412c1cff28605` |
| Final PDF | 46092 | `f5458049a705b917a3a96853cedfd199f8a0503618ad8fa2177b18bac6595e57` |

I read these actual files to recompute their SHA. The PDF result's source/copy SHA and unchanged flag agree, and the automatic authorization for WordCraft opening the PDF input records the same fresh Word SHA. The final PDF is `sales_pdf/simulated-sales-report.pdf`; the draft PDF is a separate intermediate and was not substituted. The root `outputs` directory here is `<user-home>/Documents/Codex/2026-10-18/new-chat-4/outputs/Ryn-Gemini工具链验收-20261018/gemini-installed-01`.

## Independent commands and offline checks

Main reproducible check executed from `<repo>`:

```powershell
python -B -X utf8 '<repo>/docs/acceptance/gemini-tool-fix-20261018/independent-001/business-events/audit_business_events.py'
```

Actual exit: **0**. The script writes only its own `findings.json` and isolated missing-input test evidence under this review folder. It blocks socket connection calls, `subprocess.run` / `Popen`, and the HTTP client's request method before loading/calling any helper. It does not call `main()`, run a model, contact the node, alter settings, or author business documents.

Its nine independently rerun offline guard checks all passed: cases limited to Word/PDF; correct Gemini selected; changed requested model rejected; missing Gemini rejected; wrong adapter rejected; wrong underlying model rejected; Word prompt unchanged; no calculated-total hint added; missing fresh Word source causes `new_agent_word_input_absent` before any node request. These are offline guard checks and are not additional real business trials or model calls.

Additional actual inspection commands, all exit **0**:

```powershell
Get-Content -LiteralPath '<user-home>/Documents/Codex/2026-10-18/new-chat-4/work/ryn-gemini-fix-20261018/business-harness/gemini_word_pdf_http.py'
Get-Content -LiteralPath '<repo>/docs/acceptance/agent-context-business-20261018/installed_http_business.py'
rg -n 'model.*(invalid|rejected|reject)|proposal_feedback|invalid_tool' rynmesh/agent_runtime rynmesh/llm_package/antigravity_session.py
```

Python inline JSON/event inventory, request-callsite AST inventory, and SHA manifest print commands also exited **0**. The first broad `rg --files` inspection exited 1 only because the not-yet-created report destination did not exist; it still enumerated the evidence paths. This was not a task/test failure.

## Evidence SHA256

The full path/size/SHA manifest is in `findings.json`. Key values:

| Evidence | SHA256 |
|---|---|
| Wrapper | `0065fae24f4b7241362e5eb11c44853418def5478a630351a5245bdd935d716a` |
| Pinned HTTP client | `70b8508746d3300a15aed4a61e86c5609d8007625e52d189882b569f47a2cd69` |
| Pinned helpers | `2539268ee891315cb2bfd721a87a405a48b1d8592ff623d71a391d9f94bf9af8` |
| Installation proof | `c27af528f37a0d10b0cd436e9ea63b30791f9adc4314a941d7bae1e9f84203dc` |
| Outside running identity | `1b0ef8e40aafb016b459b4b42b19ca802a2555d9db7977f07eae94d7922b2187` |
| HTTP job exit record | `f17cd8334cc05904c0fa9c6b35f302d99f24c602d6880ce6814af65dc7021989` |
| Word own-task public API | `af4916d12ac13ad31ca40a9e719a000e9f9ddde391532794d3fa1412e7a178c9` |
| Word own-task artifacts | `86d246e6b32e13e07e447c4bf67852939c58b61ee7b32a15265508ecd6e9d3c6` |
| Word result | `0a42df6d4f18af733448b4ff8cb83f0edc9684930ecccde2b08f4c60c8f6039c` |
| PDF own-task public API | `deaef40d4b6fc26df59c16b974c967d0b1804826f954627c834ea498f678290f` |
| PDF own-task artifacts | `042daa939210e889d0c89f5a206f30e7d6898ae24c08bd20a938e01ccc6cc3d0` |
| PDF result | `34c2bedca3da5bcdb8a2d8fa159fbe316a4e7352835ece07b09e047f9db4f3df` |
| Independent audit script | `6f2a15d8358cc527d9094f8a3a48386b1e054abe5cd8f1725ee0a51d8eaa5e19` |
| Independent findings JSON | `f6f9ae494c08fd1f57a5f26d3e1b47e010529074d2478f4f34d75b58e385e99f` |

## Explicit boundaries

- The complete public snapshots, source code, recorded job, and pre/post socket bindings establish the recorded execution path. There is no separate raw transport capture of every provider request, nor continuous per-request PID capture over the full task duration.
- Confirmed full internal receipt count is unknown. The artifacts API exposes receipt SHA for files, not the full receipt ledger. Cost is `null` / `unknown`, not zero spending.
- No model request was repeated. Two recorded successes do not establish general model reliability or identify which of JSON syntax, name, or schema caused the prior three invalid replies.
- HTTP summary/status fields saying QA `pending` describe the client stage. The task outcome also says independent goal assessment was not requested. This subreview establishes provenance/execution, not independent document content or visual quality; those are separately reviewed.
- I did not install, start a node, read credentials or old task history, modify product/configuration/permissions, modify original artifacts, or run Git mutation commands.
