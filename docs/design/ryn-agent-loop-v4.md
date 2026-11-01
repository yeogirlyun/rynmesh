# Ryn model-driven agent loop v4

The reported balance query stopped before running a literal `rg` search because
shell was recorded as a write and every write required a separate confirmation.
The execution effect, crash-recovery requirements and approval decision had been
treated as one concept. They are now separate.

## Execution

`AgentLoop` owns conversation projection, schema discovery, model responses,
tool queues, result continuation, context compaction and internal controls. The
host controller retains encrypted persistence, budget accounting, provider and
destination bindings, lifecycle controls and recovery of uncertain operations.
Existing model adapters and workstation/business/plugin tools are retained.

Each model response either requests tools or answers the user. Tool results
return to that same conversation, including errors the model can correct. Plans
are optional. A normal final answer ends the turn; no independent reviewer,
mandatory diff-review conversation or semantic repair cycle starts implicitly.
The pre-existing independent verifier remains available through the explicit
`verify_text_responses` setting. Output truncation continues under the original
budget and retains the answer fragments. Cancellation and owner steering still
win over late responses. Uncertain mutations are never replayed automatically.

## Permissions

| Mode | Behavior |
| --- | --- |
| `workspace` (new default) | Queries and local workstation actions execute continuously. External configuration/access changes retain approval boundaries. |
| `read_only` | Typed reads and recognized literal shell reads execute; mutations are denied. |
| `confirm_changes` | Reads execute; other effects require an exact preview and approval. |
| `automatic` | Reads execute; other effects use the owner's selected tool grants. |

Both task and current host permissions apply. Changing host settings cannot
elevate an existing task's permission snapshot. Existing saved modes are retained;
select **Work autonomously / 自主执行** for future tasks to use the new default
behavior in an already-configured installation. No existing pending mutation is
silently approved. Obsolete dialogs for literal reads are re-evaluated and can
return to the queue without an owner click.

Host metadata determines tool effects. A narrow literal-command recognizer
allows standard searches and file inspections in strict legacy sessions. It
rejects command chaining, pipes, redirection, substitution and executable ripgrep
options. Recognized native reads are invoked with argument arrays; ripgrep
configuration is disabled. PowerShell read cmdlets are module-qualified and
their values quoted. Their original write-effect journalling remains in place
for conservative crash recovery. Process monitoring respects their host-recorded
read authorization and still responds to permission revocation and cancellation.

This implementation is **not an OS sandbox**: arbitrary shell tools run under
the local user's OS account. It does not claim byte-for-byte equivalence to the
Codex implementation, nor identical decisions across different models. Codex's
published architecture separates its agent loop from application context/tools
and separates sandbox enforcement from approval policy:

- [Codex platform and open harness](https://developers.openai.com/blog/codex-as-a-platform)
- [Agent approvals and security](https://learn.chatgpt.com/docs/agent-approvals-security)

## Acceptance

`tests/test_agent_codex_loop.py` exercises actual file IO, actual ripgrep, and an
actual HTTP balance query through the production process tools. Model replies
are deterministic test doubles; the HTTP test server contains synthetic data.
These tests do not query a real financial account or demonstrate live-model
reasoning equivalence. Existing tests cover exact approval, revocation,
destination binding, cancellation, compaction and crash recovery. The UI exposes
the new mode and accepts it in its configuration cache. Ordinary completions do
not display an independent-assessment warning card.
