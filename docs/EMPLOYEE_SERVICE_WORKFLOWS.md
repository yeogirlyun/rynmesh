# Employee services on personal Ryn nodes

Owner direction, 2026-11-02. This is an implementation contract and next-batch
acceptance plan, not a claim that every company workflow is already available.

A person uses their own Ryn node for both work and personal projects, content,
entertainment and social activity. An organization supplies selected services;
joining it does not transfer ownership of the person's node or personal history.

## One set of extension mechanisms

- A **service** implements a business capability and its current caller checks.
- An **API** exposes that service; an **MCP server** adapts it for assistants.
- A **plugin** packages discovery, connection settings, tools, skills and optional
  views. The **extension framework** hosts these components; it is not another
  kind of business service. An **app** supplies a user interface to the same
  business services.

The public repository owns reusable discovery, connection/session handling,
MCP invocation, tasks, artifacts and history. Private company repositories own
business logic, data, accounting, employee mappings and deployment configuration.
No company repository or company login is needed for personal Ryn features.

## Employee journey to implement

1. Open a company invitation on an ordinary supported Ryn node, connect once,
   and discover only the services available to the current employee.
2. Ask a useful question. The assistant selects declared tools with accurate
   argument schemas and reports source, effective filters and freshness.
3. Produce a useful file, open it, and optionally share it to a reviewed target.
   A queued send is distinct from accepted, unknown, confirmed or failed.
4. Reopen the conversation after restart and ask a follow-up. Relevant prior
   answers, tool results and artifact references supply bounded context.
5. Changing employee identity or losing access updates the available services.
   Reopening history does not replay a write or reinstate an old approval.

Remote employees must not need the developer's source tree, local Dashboard
installation, or Windows desktop. Platform-specific native tools are provider
capabilities, not prerequisites for every Ryn client.

## Durable history and useful context

Persist user messages, visible assistant replies, tool inputs/results with
credential redaction, outcome/error state, artifact references, timestamps,
service/version identity and operation receipts. Do not request or store hidden
model reasoning. Show when an answer used a snapshot, unavailable source or
partial result. Recover interrupted work without duplicating external effects.

Personal conversations remain local/private by default. Company work may also
be retained in an employee-scoped company history under an explicit company
policy, visible to the employee; connecting a company service must not upload
unrelated personal conversations. Company references must identify their scope
so retrieval cannot mix employees, organizations or personal workspaces.

Search/reopen and bounded retrieval are separate from retaining the full
transcript: old evidence remains available even when the active prompt is
compacted. Recheck current access before fetching company records or artifacts;
historic context grants no current write authority. Retention/export behavior
must be visible rather than silently promised as unlimited storage.

## Current evidence and remaining work

`test_mcp_conversation_and_receipt_reopen_as_followup_context` in
`tests/test_agent_extensions_runtime.py` drives a temporary MCP subprocess,
persists an actual task/receipt in encrypted storage, reopens it, and verifies
the next model request receives the saved answer and tool result without a
second service invocation. The model boundary is scripted and all data synthetic.
This proves one local history contract, not company-wide remote access, search,
centralized retention, or a real employee workflow.

Next acceptance: two employee identities on separate clean client homes use one
company provider; one completes report -> file -> explicit sharing -> confirmed
result -> restart -> follow-up. Verify another employee's work and unrelated
personal chats do not enter that follow-up. Measure first useful result and
warm repeat latency. Start with a manual pilot of three providers and six to ten
invitees; do not add automatic monitoring or more vertical apps for this gate.
