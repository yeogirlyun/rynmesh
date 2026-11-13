# Declared schedule validation in the shared Agent

Date: 2026-11-12. Integrates with the existing Ryn Agent, selected model and workspace permissions. This is generic personal/company-node infrastructure; company policies and employee evidence remain in the private node repository.

## What the host checks

`ryn_schedule_validate` reads an authorized workspace UTF-8 CSV or JSON file. The model supplies explicit assumptions and rules; the host computes exact decimal balances and checks declared constraints together. It never generates or optimizes a schedule, infers financial rules, contacts a bank or executes payments.

Each chronological row contains a unique `id`, ISO `date`, `subject`, `outflow`, `inflow`, completed `units`, explicit `counts_as_operation`, and optionally `reported_balance`. Amounts are exact decimal strings with at most two decimal places. Each row pays **outflow before receiving inflow**. Represent delayed receipts as separate later rows, with zero completed units and the appropriate operation-count flag. Same-date ordering follows the file; intraday timing is not inferred.

Rules require `opening_balance`, a `unit` label and explicit `assumptions`. Optional rules declare scoped subject quantities/deadlines, working weekdays (0=Monday), excluded dates, daily operation capacity and period start/end. Missing amounts, malformed values, nonfinite numbers, duplicate rows/fields and unsorted dates are rejected rather than silently repaired. No unrequested subjects or missing/extra quantities can pass when scope is supplied.

The result distinguishes liquidity shortfalls, incorrect claimed balances, scope/quantity mismatches, literal deadline violations, non-working dates, daily overload and period capacity. After an invalid payment, later balances remain counterfactual diagnostics, not an executable path. Counts are complete while detailed failures and row previews are bounded; input/rule hashes identify what was checked.

When period, calendar and daily capacity are supplied, the host also computes a **fixed-operation-count** slot bound and earliest completion date ignoring cash, deadlines and changes in the number of operations. This cannot prove that every alternative splitting strategy is infeasible. Moving dates may change other inputs such as interest, so a revised proposal must be recomputed and checked again.

## Planning and efficient use

Tracked execution guidance asks the model to clarify ambiguous outcomes, retain named scope and time windows, and validate joint constraints before claiming success. This adds no company keyword router or mandatory approval round. Essential unknown business facts use the existing structured input flow.

Use a compact input manifest containing file paths, source hashes and explicit rule profiles. The validation tool reads all rows directly; reading entire tables into model context first is unnecessary. Inspect bounded source excerpts when checking interpretation. A larger prompt or model alone is not a substitute for deterministic verification.

A `passed` result means only that the supplied rows satisfy supplied arithmetic/constraints. `business_rules_verified` is always false. Source truth, interest accrual, settlement timing, holidays/cutoffs, operational authority and optimality remain outside this checker; omitted categories are identified in `unchecked`.

## Acceptance boundaries

Synthetic unit/integration tests cover pay-before-credit liquidity, cumulative costs, scope and quantity conservation, deadlines, delayed inflows, operation counts, calendar exclusions, a fixed-count capacity bound, input rejection, bounded output, workspace read-only behavior and strict CLI tool schemas. The focused Agent reliability CI includes these checks.

Local focused Agent reliability suite: **188 passed, 3 existing optional/platform skips**; changed-file and CI-list Ruff checks passed.

Private tooling additionally audits captured employee artifacts with separately supplied original scope and version-specific assumptions. This is not live company MCP acceptance or an installed Windows-app acceptance. Original-route comparisons and any executable business plan require confirmed policies and their own acceptance. There is no production deployment or financial operation in this change.
