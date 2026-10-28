# Agent loop v4 — 2026-10-15

The model-driven loop and permission decision are implemented in
`rynmesh/agent_runtime/loop.py` and `execution_policy.py`. Existing transports,
encrypted state, budgets and tool implementations remain in use. See
[the design](../../design/ryn-agent-loop-v4.md) for the behavior and boundaries.

## Passing checks

| Check | Result |
| --- | --- |
| Runtime, lifecycle, approvals, cancellation, budgets, context, projects, model integration, resource writes and new loop acceptance | 359 passed |
| Frontend Agent UI, project selector, domain and configuration cache | 72 passed |
| Codex tool proposals, CLI agent adapter and peer CLI integration | 49 passed |
| Stream-control controller, including observed stream after resume before cancellation | 5 passed |
| Frozen backend contents | v4 loop, policy, protocol, process tools and native transport present |
| Frozen backend startup in a new isolated directory | Passed; default `workspace`, Agent disabled, 47 tool entries |
| Windows desktop installer | Built successfully |

New acceptance exercises real local file IO, real ripgrep and a real HTTP request
to a synthetic balance server through the production shell executor. The model
is scripted. No real balance was queried and these tests do not establish live
model decision equivalence to Codex.

Historical scenarios that specifically test independent assessment now request
`verify_text_responses` explicitly. They do not change the normal v4 completion
path. The R05 controller now waits for an actual resumed text delta rather than
cancelling between a durable inference intent and provider admission.

## Limits of validation

One native Windows accessibility test failed at keyboard input because Windows
refused foreground focus (`agent_computer_focus_blocked`). The desktop adapter
was not modified to bypass that restriction. This is not a passing full-suite
claim. A broad historical acceptance run was stopped during its lengthy release
fixture coverage; its intermediate failures are not presented as passing results.
Relevant runtime tests were rerun after the changes above.

There is no OS sandbox for arbitrary Ryn shell commands. The implementation aligns
with published Codex loop and approval concepts, while using Ryn's existing model
providers and tools. It is not a copy of the Codex runtime and cannot guarantee
identical decisions from different models.

## Delivery

Installer: `D:/code/rynmesh/output/Ryn-0.7.0-agent-loop-v4-20261015-windows-x64-setup.exe`.
The installer was built and hashed, then installed over the current user's
`AppData/Local/Ryn` installation at the user's explicit request. The desktop
shortcut now points to that installation. `installer.json` records the installer
hash; `installed-upgrade.json` verifies the installed files (the UI executable
differs from the build output only in Tauri's expected NSIS bundle marker).

Fresh configurations default to **Work autonomously / 自主执行**. Saved strict
permissions are preserved by software upgrades; for this installation the user
explicitly requested the new default, so `workspace` has been saved and verified
independently in the encrypted settings store. No manual selection is required.
Recognized literal searches no longer require a change approval even
in saved strict sessions. Existing waiting read dialogs can return to the queue;
pending mutations are not silently approved.

The isolated frozen-node smoke test generated its own identity and data directory,
listened on loopback, and stopped its process tree after validation.

## Installed live balance query

Task `agent_66d97f94638c4c34a28e214ece127f5d` ran on the installed backend with the
user's existing default Codex model. Its initial request encountered the project's
unavailable localhost development endpoint. Read-only inspection confirmed the
existing production service; its address was supplied as task information, and
Ryn performed the actual balance request itself. No financial service was started,
reconfigured or deployed.

The task completed successfully. Approval requests: **0**; owner approval events:
**0**; input requests: **0**. Its final amount matches the real production tool
response, dated 2026-10-15, covering 16 matched account rows. Monetary details and
bank identifiers are retained only in private ignored evidence, not this report.
`live-balance-result.json` records the non-financial verification and result hash.
`installed-default-settings.json` confirms that autonomous execution is persisted
and implicit independent assessment is disabled.
