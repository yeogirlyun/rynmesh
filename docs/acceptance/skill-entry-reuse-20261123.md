# Avoid redundant skill entry reads

Date: 2026-11-23 (America/New_York). Base: `70cf9778`.

The host supplied skill bodies while also instructing the model to read listed
files, including the same `SKILL.md`, before acting. The shared usage guidance
now explicitly counts a complete supplied body as read. The current entry path
is excluded from the additional-reference list. Missing/truncated content and
required reference files still require reads; the 8,000-byte preview and its
continuation notice, permissions and durable skill snapshots are unchanged.

## Validation

- Focused skill/context regressions: 52 passed.
- Full local reliability list plus skill usage/host-note tests: **337 passed,
  3 existing skips**. Ruff and diff checks passed.
- Added regressions preserve required resources, exclude the entry itself,
  retain continuation for truncated entries and leave stored snapshots unchanged.

## Real-model replay: no net token reduction established

The same synthetic company query ran through the actual local Ryn harness, MCP
stdio transport and configured CLI route with unchanged explicit limits:
12 model calls, 30 tools, 250,000 tokens and 240 seconds. The fixture deliberately
forces pagination and contains no live company data. It does not implement the
current production server's XLSX export; this exercises local fallback delivery,
not the production export path. Actual provider usage is reported after calls.

| Source | Model calls | Tools | Reported tokens | Seconds | Task status |
| --- | ---: | ---: | ---: | ---: | --- |
| `70cf9778` | 10 | 7 | 240,449 | 152.25 | token budget exhausted |
| skill-entry reuse | 10 | 6 | 252,521 | 146.46 | token budget exhausted |

Both runs still needed one read of the truncated company skill and two fixture
pages. The changed run exported a workbook; independent `openpyxl` reads found
both teams (sales 100/900, profit 30/90), an explicitly labelled report-total row
and the correct 12% weighted margin. Its source labels say synthetic and currency
unspecified. Baseline produced only CSV intermediates. Task completion still
failed, so artifact creation must not be reported as full task success.

This pair does **not** establish fewer model calls, lower total tokens or a
general speedup. The fix removes contradictory read instructions; broader
context/planning/admission/delivery behavior remains separate work. No budget
was increased and no production program or service was installed or restarted.
