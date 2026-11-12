# CLI plugin prefetch interrupts Agent report delivery

Date: 2026-11-12. Source base: `4eb8893713c1964ae32698ffe09f70997a041436` (PR #130).

## Reproduction and change

A local, real-model Agent replay retrieved a synthetic financial report but failed before delivering Excel. The underlying CLI error was `CLI temporary storage limit exceeded`. This reproduced twice; a third run through the normal `AgentModelBridge` failed at the same storage boundary.

The installed binary was Codex `0.167.0-alpha.4.1`. In an isolated Ryn workspace, its feature inventory showed `plugins=true` and `plugin_discovery_prefetch=true` despite `remote_plugin=false`. An inventory of that owned workspace showed a plugin Git clone and downloaded document/presentation template assets. The 64 MiB quota was correctly protecting private storage; available disk space was not the problem.

Ryn's inference-only configuration now sets both `plugins=false` and `plugin_discovery_prefetch=false`. This affects only ephemeral provider workspaces created by Ryn. The existing storage quota, origin-side tool execution and user's installed CLI configuration remain intact. Native image generation retains its existing explicit opt-in path.

[Official configuration reference](https://learn.chatgpt.com/docs/config-file/config-reference) distinguishes plugin availability from remote plugin availability. Exact flag acceptance and the behavior described here were verified against the locally installed binary; this is not a claim about every CLI version.

## Verification

- Focused proposal/protocol/table suite: **62 passed, 2 skipped**.
- Actual binary with a local fake model, no billed inference: **2 passed**. Tests verify that only origin tool proposals are exposed, no environment execution occurs, no privacy sentinel is persisted, no plugin cache/clone assets appear, and owned workspaces are removed.
- Real-model report replay through `AgentRuntime`, `AgentModelBridge`, the actual CLI adapter and MCP transport: **completed in 80.52 s**, 7 model calls and 4 tool calls. Generated a 4,497-byte XLSX, with matching source values, 12% weighted margin rather than the incorrect 20% mean, nine passed declared checks, verified readback and a current downloadable file receipt. No plugin assets appeared in its private workspace inventory. The model accurately disclosed synthetic data and unspecified currency.
- Additional real-model bank coverage and missing repayment input cases completed in 18.04 s and 16.71 s. These are synthetic contract checks; private replay details live in the company repository.
- Changed-file Ruff and `git diff --check`: passed.

No desktop install, production deployment, live financial query or original employee-case acceptance is implied. The original failed questions/data were not available. A complete end-to-end financial task still took about 80 seconds; these smoke runs establish correctness on these cases, not a latency or universal answer-quality guarantee.
