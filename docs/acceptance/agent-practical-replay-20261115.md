# Practical Agent answer and artifact checks

Date: 2026-11-15 (America/New_York). Runs use the owner's existing local Codex CLI configuration via actual AgentRuntime, AgentModelBridge, CLI adapter, workspace tools and (for company reports) local MCP transport. They do not use production company APIs or establish actual upstream model weights. All records and artifacts remain outside Git. No automation, deployment or financial operation.

## What Ziling supplied

The current `feature/agent-overhaul` handoffs at `f77418ae` explicitly report no real-model/two-device end-to-end acceptance. The supplied research package contains mechanism comparisons and engineering tests, not a new original employee question/context/before-and-after-result bundle. The earlier employee loan cases remain available privately, but institution rules are still unconfirmed. Mechanism coverage does not prove task success.

## Controlled company-report comparison

Identical request: fetch the synthetic September 2026 team P/L, explain company operating margin versus averaging team percentages, and deliver a verifiable Excel. Same existing CLI configuration, same synthetic company contracts and skill instructions, same 12 model / 30 tool / 250,000 token / 240 second limits. Single-agent replay.

| Runtime | Status | Seconds | Model calls | Tool calls | Reported cumulative tokens |
|---|---|---:|---:|---:|---:|
| develop `87309899` | completed | 113.35 | 9 | 4 | 200,469 |
| overhaul `f77418ae` | completed | 109.01 | 9 | 4 | 201,049 |

Independent XLSX inspection with openpyxl confirmed both team rows (sales 100/900, operating profit 30/90), total sales 1,000, total operating profit 120 and correct **12%** company margin. Both answers reject the simple 20% mean, identify synthetic provenance/missing currency, perform one calculation and one verified export, and include Checks/Provenance sheets with eight declared checks. These are correct, useful controlled deliverables, not verified live financial reports or a fully accepted employee workflow.

One run per version cannot establish a speedup: call counts are equal, time differs by about four seconds, and token use is essentially unchanged. Cumulative token counts include repeated turn context and are not a count of unique source data. Actual monetary cost is unknown. Reduce avoidable planning-only turns, repeated schemas and data loading before claiming efficiency gains; preserve source coverage and deterministic checks.

## Original ambiguous request

On `f77418ae`, replay of the earlier original financial question/image reached `waiting_input` in **36.04 seconds**, two model calls, three tools and 33,421 reported tokens, without an error. It asked whether completion means one rollover cycle or full debt clearance, and requested interest, borrowing/settlement and calendar rules. This is correct handling of missing inputs, not a completed repayment solution. No live original shared-model-route comparison was performed.

## Personal-node smoke test

On integrated product source `8e03305d`, a real-model personal-project request read synthetic balcony notes, saved `checklist.md` and read it back in **34.41 seconds**. This isolated personal workspace exposed file tools and no company MCP connection. The artifact and remaining unknowns were independently reviewed; no purchase or real-world task was performed.

## Evidence limits and next acceptance

Keep complex recovery and employee workflow changes with Ziling. Before broader integration, use actual original queries, attachments/context, expected answers and confirmed business rules. Compare current develop and the revised branch on the same configured model, budgets and data, including multi-page/zero/missing records, useful file delivery, error recovery and restart continuation. Test two ordinary employees from their own nodes, with correct access, retained conversation/source/action records and no duplicate writes.
