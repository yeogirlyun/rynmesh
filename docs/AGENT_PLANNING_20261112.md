# Shared task planning for personal and company nodes

Date: 2026-11-12. Implementation: `bf718442`, based on `994959eb`.

Ryn uses the existing selected model, Agent loop, permissions, budgets, tools and encrypted task store. Planning is shared infrastructure. Company rules, metrics and workflow implementations remain in company plugins; there is no separate company Agent executor.

## Request to deliverable

For nontrivial work the model identifies deliverables, constraints, available data/tools and essential missing facts. It builds up to twelve dependency-ordered steps, each with an expected result and a verification method. The host checks the graph and records progress; it does not infer business rules from keywords. Simple questions can still finish in one inference without creating a plan.

`ryn_update_plan` now supports `track_execution=true`, `expected_plan_revision`, deliverables, constraints and step states `pending`, `running`, `blocked`, `done`. Existing brief-plan calls remain supported. Initial plans use `steps`; subsequent progress uses short `step_updates`, avoiding repeated descriptions and output contracts. Independent reads can still be batched through the existing executor.

The host checks unique IDs, acyclic/topological dependencies, known criterion IDs, prerequisite completion and current plan revisions. Changing the step graph, deliverables or constraints requires `replan_reason`; updating progress or explanatory wording does not. Blocked steps need a concrete blocker. Completed steps need actual current receipt references or a summary explicitly classified as model-only work. Prior contract/progress is preserved when omitted from an update. The last three plan snapshots are retained in encrypted storage.

Receipt references must belong to the current task, goal, resource scope and context epoch. Failed, unknown, refused or explicit business-error results cannot support a finished step. Receipt linkage means the observation exists, not that the model interpreted it correctly. Planning cannot grant permission, change owner goals/criteria, settle uncertain writes or declare independent verification.

The model repairs recoverable errors or revises its approach. Essential missing inputs use the existing structured input flow. Existing authorization and unknown-write reconciliation still govern every operation. Plan dependencies validate declared progress; this is not a new DAG scheduler that binds or executes arbitrary tool calls automatically.

Before closing an actionable unfinished plan, the host requests at most one reconciliation turn per goal, within the same owner budget. If work remains, the outcome records a partial result with unfinished step IDs. Already blocked plans can report their blockers directly. A complete checklist alone never makes the goal independently verified. Existing optional independent goal assessment remains separate.

## Efficient verified table delivery

After `ryn_table_calculate`, export with the actual calculation receipt ID and a new output path:

```json
{"calculation_receipt_id":"receipt_op_ACTUAL_ID_FROM_THIS_TASK","path":"report.xlsx"}
```

The host retrieves the original source path, exact recipe and expected calculation hash from its confirmed passed receipt. It rechecks task/goal/scope/context binding, recomputes the calculation, enforces the fingerprint, verifies serialization and reads back the written bytes. Changed source data still prevents export; existing files are never overwritten. This avoids reconstructing long recipes after compaction or repeating successful calculations. The previous explicit source/recipe/hash interface remains valid; mixing the two modes is rejected.

The calculation response also makes its layout contract explicit: `totals` are raw column sums for declared arithmetic checks, not necessarily business metrics. Rates and repeated source summaries may be non-additive. XLSX Data and CSV contain source/derived data rows only, with no automatic totals row; JSON retains arithmetic metadata. This prevents a planner from mistaking raw percentage sums for an Excel presentation defect and performing unnecessary repair work.

Planning guidance has a short common introduction; detailed execution guidance is returned once when a tracked plan starts. Current progress and recent receipt IDs remain accurate even when the runtime stabilizes its older plan prefix for provider-session reuse. No additional planner service or mandatory plan approval was introduced.

## Validation and limits

- Local focused suite: **242 passed, 3 skipped**, covering planning, runtime controls, continuation, errors, scope/model changes, budgets, table delivery, strict CLI schemas and exact reference preservation. The three skips were existing optional/platform coverage.
- Changed-file Ruff and diff check passed. Web TypeScript check passed; existing task overview **4 tests** and native host **5 tests** passed. The change adds optional frontend types and uses existing progress UI; it introduces no new layout or desktop install.
- Synthetic company report with real model inference and MCP transport: completed in **104.61 s**, 9 model calls, 4 business/local tool calls. All three plan steps finished. One calculation and one receipt-based export produced a correct **12%** weighted margin, eight passed checks and an independently inspected XLSX. Currency/source limitations were disclosed.
- An earlier development attempt produced a correct workbook but exhausted its token budget after repeated recipes and a rejected wording change. It is **not** counted as passed. The successful replay used the same 12-model-call, 250,000-token, 240-second test limits. These single runs do not establish a statistical speedup or latency SLA; planning still has an overhead.
- A repeat on `bf718442` also exhausted the 250,000-token budget after producing files: the model incorrectly inferred that raw column-sum metadata was rendered as Excel totals, revised the recipe and ran out of budget before its final answer. This failed repeat is excluded from passing acceptance and motivated the explicit layout contract and regression test above. A single successful replay must not be treated as consistent production performance.
- Personal project replay on committed `bf718442`: **59.72 s**, 6 model calls, all three runtime steps finished with receipts. It used short progress updates, read actual local synthetic notes, wrote and read back a three-step checklist, preserved budget/no-purchase constraints, left unknown dimensions/prices unresolved, and used no company tools. It correctly distinguished finishing the draft from performing the future physical actions. An ordinary version also completed directly without a plan. Private replay tooling and details are in `gshopper-ryn-node`.

These are bounded synthetic acceptances, not live company data or the original unavailable employee cases. Arbitrary financial optimization, business-rule correctness, lossless cross-conversation history and universal answer accuracy are not established. Business-specific solvers and employee workflow acceptance must use actual rules, source data and independent expected results. No production deployment, signed release or Windows installed-app acceptance was performed.

Follow-up: [declared schedule validation](AGENT_SCHEDULE_VALIDATION_20261112.md) adds shared deterministic balance/scope/calendar/capacity checks. Original captured employee inputs are now available privately for conditional audits; this does not change the limitations of the synthetic acceptances above.
