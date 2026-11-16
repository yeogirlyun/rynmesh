# Reuse loaded skills and verified table exports

Date: 2026-11-16 (America/New_York). Base: `033b6861`.

The built-in spreadsheet skill required reopening every output even though
`ryn_table_export` already validates serialized values, recomputes the declared
calculation fingerprint and reads back written bytes. Skill version 2 and the
export tool description explain when that receipt satisfies output verification.
Other writers still require readback; changes, failures, unresolved uncertainty
and explicitly requested additional checks still justify another check.

Loaded skills now say to reuse their supplied bodies and read only missing or
truncated sections and required reference files. Library inspection is needed
for custom processing, not a prerequisite to using supplied table tools.
No automatic execution, permission change, budget increase or model call is
introduced by these guidance changes. Source and business-rule verification
remain separate from arithmetic and byte verification.

## Validation

- Table, skill, request-context and Codex object-protocol regressions: **79 passed**.
- Ruff and diff checks passed.
- Same synthetic company query, local configured CLI route and explicit owner
  limits: 12 model calls, 30 tools, 250,000 reported tokens, 240 seconds. Both
  environments included the declared `xlrd` dependency. The earlier review run
  without it is excluded from this comparison.

| Source | Model calls | Tools | Reported tokens | Seconds | Task status |
|---|---:|---:|---:|---:|---|
| develop `033b6861` | 10 | 8 | 233,483 | 195.36 | budget exhausted |
| final guidance change | 9 | 5 | 208,992 | 136.89 | completed |

The final run avoided rereading the already loaded spreadsheet skill, listing
the workspace unnecessarily and recalculating the unchanged export. Both runs
made two compaction calls; the larger compaction/planning design is unchanged.
An intermediate export-only guidance run also hit its budget and did not
establish an efficiency gain; the final change includes loaded-skill reuse.

Independent `openpyxl` reads confirmed both workbooks contain two team rows,
sales totaling 1,000, operating profit totaling 120 and the correct 12% weighted
margin, with Data, Checks and Provenance sheets. The final workbook preserved
eight passed declared arithmetic checks (baseline seven) and the synthetic-data,
unknown-currency and source-coverage limitations. There were no live company
queries or business writes.

This pair shows one fewer model call, three fewer tool calls and 10.49% fewer
reported tokens for this task. It does not establish a general speedup, billing
savings, model equivalence or real employee acceptance. Failed/unknown business
writes, byte-progress streaming, durable forgetting, legacy budget provenance
and broader company rollout remain separate work.
