# Agent shared infrastructure: bounded recovery and verifiable tables

Owner direction, 2026-11-12: take ownership of the shared system work. The public
runtime must serve personal and company nodes; company policies and business
implementations stay in private `gshopper-ryn-node`.

This increment addresses concrete execution and delivery failures. It does not
claim to fix every model reasoning failure or to replace the existing runtime.

## What is implemented

### Conversation continuation

When existing compaction cannot fit another owner message, creation of the child
and both continuation links commit in one encrypted-store transaction. A retried
identical request returns the existing child. A different request to a superseded
source returns an explicit conflict; subsequent work belongs in the child.

The child retains the task's language, reasoning setting, skills, workspace/project,
permission intersection, destination, scope and cumulative budget records/owner
limits. It starts a new execution round, without copying approvals, process
handles, dispatch queues, provider sessions or verification success. The source
cannot resume, edit, switch models or reset its copied budget after continuation.
Unknown writes cannot be escaped by creating a continuation.

Summary provenance retains resource-origin validation under the child ID. An
incompatible task-bound reference fails explicitly and leaves the source/link
unchanged, as with explicit forks. This does **not** implement portable cloud
references, a full history-search interface, old result-reference migration, or
lossless context transfer. Prior messages, receipts, original attachments and file
links remain in the linked source; the existing bounded continuation summary is
still a lossy projection. The model must not treat that summary as full evidence.

### Bounded file reads

`ryn_file_wait` makes up to three failed attempts for the same normalized file in
the current goal/scope/context, counting earlier reads. Delay is at most two
seconds between attempts; cancellation is checked during waits. Missing input is
reported explicitly. A success ends the failure streak. Attempts are visible in
the result; no recurring monitor or background job is created.

Before shell preview/approval, the host rejects recognizable unbounded loops that
read a file which already failed. This covers the PowerShell `ReadAllText` loop
recorded in [the failed UI acceptance](acceptance/develop-release-185fa22-case5-20261112.md),
plus tested `Get-Content` and POSIX cases. Quoted explanations, comments, bounded
loops and unrelated diagnostics are distinguished by focused tests.

The guard is a limited pattern check, **not a shell sandbox or general program
verifier**. Indirection, separately launched scripts and arbitrary equivalent
programs are not all recognized. Existing process lifetime/cancellation controls
remain necessary. Windows desktop/model-driven repetition of the original UI
scenario is still required before calling that user-visible case accepted.

### Deterministic table calculations and delivery

`ryn_table_calculate` reads an authorized workspace UTF-8 CSV or JSON array and
executes a declarative recipe with Python Decimal, at precision 50. It supports
typed/aliased columns, derived columns, basic arithmetic, explicit rounding,
ISO-date differences and declared row/total comparisons. No expression executes
general Python, a shell, a network request or an Excel formula.

Missing fields return `needs_input`, never invented zeroes. Failed checks prevent
export. Source bytes and recipe have hashes; export recomputes both and requires
the prior calculation fingerprint. Changed input/rules require recalculation.
The model receives bounded previews (eight rows, text cells clipped to 160
characters, result under 24 KB); complete values remain in the output.

`ryn_table_export` creates a **new** workspace `.xlsx`, `.csv` or `.json` through
existing approval, operation-ledger and file-review machinery. It verifies
serialized content before writing, compares bytes after writing, supports existing
mechanical verification, and publishes the file through task artifacts. Existing
files are not overwritten. CSV text is protected from formula interpretation.
XLSX has Data, Checks and Provenance sheets, with literal calculated values rather
than formulas lacking cached results; checks and the full recipe are included.
XLSX values beyond 15 significant digits or text beyond 32,767 characters are
rejected; use JSON/CSV for data that cannot be represented faithfully in Excel.

Bounds: source at most 1 MiB, 5,000 rows, 32 source columns, 16 derived columns,
32 checks, recipe at most 24 KB, output at most 8 MiB. No production dependency
was added. An independent `openpyxl` reader is used only for test validation.

**A passed calculation verifies the declared arithmetic/checks, not source truth,
business-policy correctness, holiday calendars or an optimal financing plan.**
No company-specific interest, repayment, authorization or scheduling rule is
invented by this public tool. Initial ingestion is CSV/JSON, not arbitrary XLSX,
PDF or scanned-table extraction. Excel styling/charting/editing is not added.

## Example

Source `amounts.csv`:

```csv
name,amount,fee
First,0.1,0.2
Second,0.2,0.1
```

Calculate with:

```json
{
  "source_path": "amounts.csv",
  "recipe": {
    "columns": {"name": "text", "amount": "decimal", "fee": "decimal"},
    "column_order": ["name", "amount", "fee", "total"],
    "derived": [{"column": "total", "expression": "round(amount + fee, 2)"}],
    "rounding": "half_even",
    "checks": [{"name": "Reconciles", "scope": "totals", "expression": "sum_total == 0.60"}]
  }
}
```

Both rows produce `0.30`, with a total of `0.60`. Call `ryn_table_export` with the
same source/recipe, returned `calculation_sha256` as
`expected_calculation_sha256`, and a new output `path`, such as `report.xlsx`.
Tools use existing discovery; spreadsheet-work guidance points the model to them.
No new app screen is necessary to use the Agent tool path.

Personal use: calculate expenses or project quantities and download the checked
file. Employee use: obtain an authorized report from the company plugin, save its
CSV/JSON into the task workspace, apply the company's declared rules, then review
and deliver the checked workbook. End-to-end live plugin export is a separate
acceptance step; this increment does not claim all Dashboard tools emit that format.

## Review of Clawpad / Avaryn patterns

Read-only comparison used local Clawpad `1e6238a6b` and Avaryn `a58654a`; their
unrelated working-tree changes were preserved. Per-step failure state, output
contracts and deterministic verification are useful patterns. Ryn already has
receipts, unknown-write recovery, budgets and compaction, so another executor was
not transplanted. No source from those repositories was copied in this increment.

Do not copy QA clean-state deletion, guessed required-parameter defaults or assume
that writing Excel formulas calculates them. Missing financial/business inputs
must be explicit; existing private business services remain the rule authority.
Separate provider-circuit tuning and cross-plugin continuation are future system
work to justify with measured failure cases, not prerequisites added here.

## Ownership and next acceptance

- Shared runtime/recovery/calculation work: our responsibility, delivered in small
  tested increments. Follow up on portable references/history/context only with
  explicit contracts and tests, rather than claiming this summary is lossless.
- Ziling: integrate the user path and private employee workflows. Supply anonymized
  reproducible cases with source inputs, actual model/tool trace, required company
  rules, expected outputs and the failure step. Do not implement a parallel Agent
  engine in company plugins.
- Private node: expose business queries and validated calculations through the
  existing plugin/MCP boundary; test two ordinary employee identities from their
  own personal nodes, including login, query, weekly report, export, reopen and
  history/context after restart. Keep Gshopper data/policies in the private repo.
- Windows acceptance: repeat the original missing-file request without manually
  rejecting a proposed loop; test table download/open and three long-conversation
  rollovers in the installed app. Record model route and build commit.
- Prefer these useful personal/employee loops before stranger discovery, a shared
  MCP marketplace or Ryncoin. No coin issuance, exchange, deployment or updater
  publication is part of this increment.

## Verification

Local macOS/Python 3.13 testing and limits are recorded in
[the acceptance note](acceptance/agent-reliability-20261112.md).
The CI `agent-reliability` job also exercises focused tests under Python 3.12/Linux
and an independent XLSX reader; it does not replace the broader existing jobs.
