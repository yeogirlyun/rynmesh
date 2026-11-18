# Compact verified-export replies

Date: 2026-11-15 (America/New_York). Product implementation `a61beefe`, based on develop `fd30de9e`.

Receipt-based `ryn_table_export` now returns the original calculation receipt ID, the recomputed source/recipe/calculation hashes, status, row/check counts, raw totals with their non-business-metric warning, and the existing file/hash/byte-readback result. It avoids echoing the full preview rows and per-check details already returned by `ryn_table_calculate`. Those details remain in the encrypted calculation receipt and complete exported workbook. The explicit source/recipe/hash API retains its previous full reply.

This changes reply size, not validation: the host still requires a passed current-task/goal/scope/epoch receipt, recomputes from source, rejects changed inputs, serializes all data/checks/provenance and verifies written bytes. A smaller reply never makes unknown source truth or business policy verified. It does not remove planning, compaction, budget or permission checks.

## Verification

- Focused table/planning/strict-tool-schema suite: **78 passed, 2 existing optional skips**; Ruff and diff checks passed.
- Regression coverage compares compact versus full fingerprints/counts/totals, requires the full calculation to remain available, opens the full XLSX independently and retains changed-source, stale-scope, failed-check and no-overwrite rejection tests.
- Reprojecting the exact same retained representative export result shrank serialized response bytes from **4,910 to 1,606 (67.3%)**. This is a deterministic payload comparison, not an inference-cost estimate.

## Real-model controlled replay

Same synthetic company-report request, committed company MCP contracts/skill instructions, existing local Codex CLI configuration and owner budgets (12 model calls, 30 tools, 250,000 cumulative reported tokens, 240 seconds). Same real Agent runtime/bridge/adapter/file tools, with only the public source version changed. Outputs and model records stay outside Git; no production API access or financial operations.

| Source | Model calls | Compaction calls | Reported cumulative tokens | Seconds |
|---|---:|---:|---:|---:|
| develop `fd30de9e` | 9 | 2 | 198,450 | 112.17 |
| compact replies `a61beefe` | 8 | 1 | 184,003 | 102.69 |

Both completed with four business/local tool calls, one calculation and one export. Independent openpyxl inspection confirmed two teams, sales 100/900, operating profit 30/90, correct company weighted margin 12% rather than simple mean 20%, and seven declared checks with full Data/Checks/Provenance sheets. Answers identified synthetic data and missing currency; neither represented it as live company financial truth.

Observed reductions were one model call, **7.3% cumulative reported tokens** and **8.5% elapsed time**. A single pair is not a statistical speedup, success-rate study or SLA; model output differs between runs. The deterministic result is the smaller redundant reply and unchanged full artifact/verification contract. Remaining compaction and planning overhead remains a larger follow-up.

Final contract clarification: per-check details remain in the calculation receipt; exported output retains its existing format (CSV is still data rows, not an embedded check report). Receipt-mode regression now covers XLSX, CSV and JSON. The final table/request/planning/schema suite passed **96 tests with 2 existing optional skips**; Ruff/diff checks passed. This wording-only clarification followed the model replay and does not change validation or data payload fields.
