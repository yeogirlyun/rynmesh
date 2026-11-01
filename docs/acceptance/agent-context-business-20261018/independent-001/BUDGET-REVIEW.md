> 公开交付副本（2026-10-19）：保留原验收结论、失败和范围；本机绝对路径已规范化，未提交的原始证据仅在本机留存。文中的旧快照/安装包 hash 不代表本次源码；原报告 SHA-256：`ca1ef7e4ae6aac9959966ca6470d2cea82724766223112957ef5a9e6583f4986`。

# Independent budget/context review fragment — 2026-10-18

Reviewer: `/root/budget_audit`, delegated read-only source review. Production files were not edited. All model results below are offline authored fixtures; no CLI subprocess, account, credential, old conversation, or paid inference was accessed.

## Judgment

**PASS for the new normal Agent auto/manual context admission path, with the explicitly stated heuristic limitation. FAIL for the complete “missing usage remains unknown” contract on the current Codex CLI path.** The latter is independently reproduced and existed in the pre-round snapshot; it is not claimed as a newly introduced regression. Business files are outside this fragment.

| Item | Result | Evidence |
| --- | --- | --- |
| New Agent default is null/auto and a declared 1M-token model can exceed the former 600,000/2,000,000-byte default ceilings | PASS | `store.py:35-42`, `output_policy.py:51-69`, focused encrypted runtime tests |
| Owner integer limits 4,000..2,000,000 remain supported; explicit auto/manual provenance survives restart; existing tasks keep their saved limit | PASS | `runtime.py:702-717`, `runtime.py:1006-1012`, focused tests |
| Ambiguous old 120,000/600,000 numeric values are preserved instead of inferred as automatic; schema-2 goals/messages/scope/approvals/receipts remain intact | PASS | `state.py:82-99`, four legacy migration cases |
| Full messages and tool schemas, serialized UTF-8 and JSON escaping, use ceil(bytes / 3) + 512; normal dispatch reserves the same estimate | PASS | `output_policy.py:37-48`, `runtime.py:367-406`; independent 9,763-byte request reserved 3,767 input + 4,096 output |
| Automatic input allowance reserves the complete configured output setting, including when transport later clamps the actual generation cap | PASS | `output_policy.py:51-69`, 128,000-output and zero-input-allowance tests |
| Known smaller model and unknown window recompute the input allowance; unknown fallback is labelled 32,768 tokens, known=false | PASS | `output_policy.py:20-29`, actual model-switch tests; UTF-8 oversized owner input dispatches zero model calls |
| Project guidance, independent checker and diagnostics accept nullable limits and use effective runtime allowance | PASS | `projects.py:380-381`, `verification.py:218-224`, `diagnostics.py:146`, focused actual checker test |
| Ordinary missing usage remains null, reserves estimated charges durably, and persists across encrypted store reopen | PASS | independent probe: `total_tokens=null`, `usage_complete=false`, `charged_tokens=7863` |
| Missing Codex CLI usage remains unknown throughout decoder → bridge → runtime | **FAIL, P2** | concrete finding below; independent expected-null assertion failed with 1,137 tokens |
| Existing call/tool/token admission, exact approvals, live permission tightening, cancellation, write receipts and waiting-approval restart behavior | PASS in the focused cases exercised | `budget-focused` 31 pass; `budget-retention` 31 pass |

## P2 — Codex CLI synthesized usage is presented and charged as measured tokens

`rynmesh/llm_package/codex_session.py:623-624` substitutes `len(prompt)//4` and `len(json.dumps(message))//4` when `usage` lacks `inputTokens`/`outputTokens`. It returns these numeric fields with no `reported_usage` distinction. `AgentModelBridge._response_metadata` in `rynmesh/agent_runtime/bridge.py:50-53` accepts those local top-level fields as usage; `runtime.py:452-453` settles them; `budget.py:123-131` then replaces reservations with the fabricated counters. Consequently `budget.py:68-79` exposes numeric actual fields and `usage_complete=true`.

The offline reproducer calls the shipped `_result` with `usage={}`, then the shipped bridge response decoder, and finally one actual encrypted `AgentRuntime` step. It uses no real Codex invocation. Observed result:

```json
{"input_tokens":1125,"output_tokens":12,"total_tokens":1137,"charged_tokens":1137,"usage_complete":true}
```

Required semantics for absent provider usage are `input_tokens/output_tokens/total_tokens=null`, `usage_complete=false`, while retaining the labelled dispatch reservation for charging. This issue both overstates certainty and can refund the durable reservation based on an estimate. The decoder can legitimately lack usage: `_turn` returns at an `item/tool/call` proposal (`codex_session.py:592-594`) without requiring a prior token-usage event; token-usage handling only occurs at `604-605`.

Attribution is explicit: `context-business-baseline-20261018/manifest.json` contains exactly the same current hashes for `codex_session.py` (`accd29e85d4e00588d7ee578a767e8d91a0b3c90947112d3d647f4b1fb144949`) and `bridge.py` (`60da845b851b94288aa0b774db162470653e496d4c8810f465de1a85f1af2285`). The baseline ZIP also contains both fallback expressions, and HEAD does too. This is a current contract gap inherited from earlier code, not a newly introduced context-auto defect.

## Heuristic and cumulative budget limit

The normal estimator is correctly labelled `serialized_utf8_bytes_div_3_ceil_plus_512_framing_heuristic` with `token_count_is_estimate=true`. It is not a tokenizer and is not a mathematically conservative upper bound on actual provider token usage. The 32,768-token unknown-window fallback is an assumption, not evidence of an unknown model's capacity.

An independent authored response reporting 25,000 input + 25 output tokens was admitted under a 3,766-input + 4,096-output reservation and a 20,000 lifetime token setting; settlement correctly preserved the reported actual total of 25,025 rather than falsifying or clamping it. This demonstrates that `max_total_tokens` is an admission policy over estimated reservations until actual usage arrives; it cannot promise a strict cap on actual inference tokens or money. Ordinary unknown responses correctly retain reservations, and cost stays null/unknown. The word “conservative” in `budget.public().reservation_policy` must be understood as retaining rather than refunding a reservation, not proof that the numeric estimate bounds real usage.

One adjacent exception was observed by static review, not added as a main-path failure: `runtime.py:505` uses `input_bytes + 512` for `_model_probe`, and `source_drafts.py:302` supplies bytes for the complete probe body. Source-draft probes therefore retain an older, larger input reservation formula rather than the new messages+tools formula. No new source probe or paid call was performed. The final report should scope any statement that *all* inference reservations use the unified formula accordingly.

## Execution evidence

| Artifact | Executed selection | Result | Fresh short basetemp |
| --- | --- | --- | --- |
| `budget-focused.log`, `budget-focused.junit.xml` | `tests/test_agent_model_context_budget.py` + `tests/test_agent_budget_dispatch.py` | 31 passed | `D:\tmp\ir1b869b98ef` |
| `budget-retention.log`, `budget-retention.junit.xml` | `tests/test_agent_output_policy.py` + four specifically named cancel/live-permission/approval-restart tests from `test_agent_runtime.py` | 31 passed | `D:\tmp\ir1b0401b3a5` |
| `budget-independent.log`, `budget-independent.junit.xml`, `test_budget_independent.py` | eight independent offline context/usage cases | 7 passed, **1 failed** | `D:\tmp\ir1bcbd52b53` |

**Total: 70 executed cases, 69 passed, 1 failed.** The deliberate expected-null assertion is a real failed contract test, not an xfail or a passing test that merely records defective behavior. Individual probe evidence is retained as JUnit properties. No whole-repository test run was performed. The project interpreter was `<repo>\.venv\Scripts\python.exe`. Two initial environment-only attempts with global interpreters could not collect tests (Python 3.12 had no pytest; global 3.10 could not import `datetime.UTC`); they are not production test failures and are excluded from counts.

`budget-source-hashes.json` records 15 reviewed production/test files with full SHA-256 and byte counts. `budget-frozen-comparison.json` independently compares all seven context-auto production files with `ryn-context-auto-summary.json`: **7/7 unchanged**. Key main-path hashes are:

| File | SHA-256 |
| --- | --- |
| `runtime.py` | `a4e310549922173f72aabe13e557589ee8db74957ddb7afeccdcc1f171a93583` |
| `output_policy.py` | `74b09f3a7c320e9979563a3641f289155d7a8d175914b78dc6d62da354744dd9` |
| `state.py` | `b38a4369f1901b8522ae765f880036fe09e44428fe23bf019deeef567b17ead4` |
| `budget.py` | `ab63cc42cb4ce1485e14c4ed80afe0697398a5c92a6f0ab2af05ffbac50815b6` |

This fragment signs only the unchanged source snapshot and the listed offline cases. It does not establish successful paid 1M-token inference, business artifact correctness, all-provider tokenization, or an exact token/cost ceiling.
