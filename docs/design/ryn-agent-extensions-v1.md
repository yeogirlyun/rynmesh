# Ryn Agent extensions: MCP, Skills, and portable plugins

Design owner: Codex. Implementation owner: local Antigravity, Gemini 4 Argon Medium.
Date: 2026-10-19. Status: implementation specification; acceptance is recorded separately.

## Goal

Give Ryn Agent the familiar Codex extension-management experience: a plugin directory,
installed extensions, Skills, MCP server configuration, individual enable switches,
details, import previews and real connection tests. A configured external tool must
actually enter Ryn's existing model/tool loop. The first complete use case is computer
control. Keep execution on the Ryn consumer device even when a remote CLI supplies
the reasoning model.

This is a working feature, not an illustrative catalog. Never represent an installed
package as connected, a listed tool as executed, or an input event as verified success.

## Reference behavior

- Codex MCP: https://learn.chatgpt.com/docs/extend/mcp?surface=cli
- Codex Skills: https://learn.chatgpt.com/docs/build-skills
- Portable/Codex manifests: https://developers.openai.com/plugins/build/plugins
- Claude package conversion: https://developers.openai.com/plugins/guides/submit-claude-plugin
- MCP Python SDK: https://py.sdk.modelcontextprotocol.io/v1/

Match the Codex information architecture and interactions while using Ryn's existing
typography, neutral surfaces, theme tokens, buttons, and responsive shell. Do not add
marketing banners, metrics dashboards, fake marketplace entries, or new visual styles.

## Navigation and page design

Add an Agent extensions management entry reachable from both Ryn Agent and Settings.
Keep the current Craft application plugins page working. Use one stable extension
route with three URL-addressable tabs/sections: Plugins, Skills, MCP servers.

Desktop: narrow section navigation, main list, a details drawer/dialog when selected.
Mobile: horizontal section navigation and full-width details/dialogs. Support light and
dark themes, keyboard focus, Escape, correctly labeled forms, loading and error states.
All user-facing text must use the existing Chinese/English localization mechanism.

### Plugins

Header: Plugins / a short explanatory subtitle / Import plugin button.
Under the header: searchable list with All and Installed filters.
Cards show icon, name, short purpose, source (Ryn/Codex/Claude/local), installed state,
and enable switch. Details show description, included Skills and MCP servers, actual
compatibility notes, import origin, version, and uninstall. Details must expose separate
server and skill switches as well as the package switch; a disabled parent disables
all contained capabilities immediately.

Provide one honest first-party Computer Use entry. Its details state Windows support,
what can be observed and controlled, and current desktop permission. Enabling computer
control is an explicit owner action and uses the existing desktop_access setting.
It must be usable through the same extension/tool integration, not just a decorative card.
No assumption that a proprietary Codex Computer Use server can be reused outside its app.

### Skills

Header: Skills / Import skill button / search.
List rows: title, purpose, enabled switch, source, version; distinguish existing built-in
skills from imported skills without duplicating either. Selecting a row opens the full
SKILL.md, imported file list, dependency/compatibility notes, and remove action.
Built-ins cannot be deleted. Imported Skills can be enabled, disabled, and removed.
The existing new-task Skill selector must include enabled imported skills and retain
its behavior for built-ins. Explicit selection snapshots the skill version for that task.

Imported skill content is lower-priority local guidance, never permission or evidence.
Expose enabled skill names/descriptions for discovery, and load full content when chosen
or explicitly invoked with $skill-name. Referenced files must be accessible through a
bounded skill-file read tool; scripts remain subject to existing shell execution policy.
Do not execute scripts at import time or silently install dependencies.

### MCP servers

Header: MCP servers / Add server / Import configuration.
Rows: name, transport (STDIO / HTTP), observed state, tool count when known, enable
switch, overflow menu. States: disabled, not tested, connecting, connected, error.
Never show connected solely because enabled=true. Details show last test time, tool list
with descriptions, per-tool enabled controls, and edit/test/restart/remove actions.
Retain errors on screen with retry; never swallow an error into an empty list.

Add/edit dialog:
- Name and transport.
- STDIO: command, one-argument-per-line args, optional absolute working directory,
  environment key/value rows (values masked by default).
- HTTP: URL, optional headers (values masked), optional bearer-token environment name.
- Advanced: startup timeout and tool timeout, enabled tool selection, computer-access flag.
- Save and Test connection. Save records configuration; test actually initializes the
  server, lists tools, and reports the result. Test may start the configured process,
  so the user must explicitly click it or enable the configured server.

Secrets may be entered only in owner configuration forms. Public lists, errors, model
context, and logs must omit credentials. Editing leaves secret values unchanged unless
explicitly replaced/removed. Follow the project's encrypted-storage conventions.

### Import

Support local absolute folder/file paths and ZIP packages in a usable owner UI. A plain
SKILL.md import is allowed; a folder/ZIP preserves its scripts, references and assets.
Configuration import accepts JSON text (Claude .mcp.json / mcpServers and compatible
Codex JSON) and Codex config.toml mcp_servers tables. Plugin import detects:
- portable root plugin.json + mcp.json + skills/;
- .codex-plugin/plugin.json + .mcp.json + skills/;
- .claude-plugin/plugin.json + .mcp.json + skills/.

Preview before commit: detected name/version/source, skills, server names/transports,
duplicates, missing dependencies, and unsupported features. Do not activate or start
anything during preview. Commit creates disabled imported entries; enable is a separate
explicit owner action. Reject invalid packages atomically with actionable errors.
Preserve relative references; resolve package-root placeholders within the imported root.
Recognize ${CLAUDE_PLUGIN_ROOT} and ${CODEX_PLUGIN_ROOT} in package paths. Unsupported
hooks, agents, commands, app-private connectors, and OAuth login flows must be listed
honestly and never silently declared compatible. Essential unsupported dependencies
make the relevant capability unavailable, rather than executing an altered workflow.

## Backend integration contract

Create a small, cohesive extensions package under agent_runtime. Separate persisted
configuration, import validation, MCP transport, tool adaptation and skill handling.
Use encrypted transactional owner storage under the existing Agent home; avoid changing
existing task-state formats unnecessarily. Configuration mutations need revision/conflict
checks. Imported files have immutable version paths and manifest hashes.

Expose local-owner authenticated API routes under /api/local/agent/extensions. They
must reuse local_control, no-store and bounded request-body conventions. Provide list,
detail, save/update/delete, enable/disable, test/restart, import-preview and import-commit
operations. The frontend uses a typed client. Avoid overloading the fixed Craft catalog.

Use an established MCP SDK where practical. Ryn currently supports Python 3.10 and uses
httpx, so use the maintained v1 SDK with a compatible bounded dependency, or document
another tested choice. The current v2 SDK requires Python 3.11 and different HTTP APIs;
do not raise Ryn's Python minimum accidentally. Handle initialization/initialized,
paginated tools/list, tools/call, JSON and SSE Streamable HTTP responses, session headers,
bounded timeouts and subprocess cleanup. Never use shell=True for STDIO commands.

Transport operations must not block the node event loop. Use a correctly owned async
lifecycle/thread or bounded isolated connections. Cancellation and disable/delete/close
must close relevant sessions/processes, with no writes retried after an unknown outcome.
All SDK imports should fail gracefully if dependency is unavailable in an old installation.

Adapt discovered tools into RynTools.specs/metadata/validation/preview/execute and existing
discovery, schema-snapshot, permission, journal, budget, output-size and cancellation paths.
Expose stable collision-resistant model tool names no longer than 64 characters. Keep
the original server/tool identity and configuration revision bound to a discovered schema;
configuration or schema changes invalidate a pending invocation before dispatch.

External tool annotations are hints. Imported tools default to write/high risk and must
use existing approval policy; an MCP server cannot grant itself read privileges. Owner
configuration may explicitly designate a tool read-only. All computer-capable servers
also require current task and current global desktop_access. Removing or disabling a
server/tool must revoke it immediately, including already prepared invocations.

Do not loosen the Codex or Claude model-provider isolation. Codex supplies proposals,
Ryn executes local tools. Claude CLI remains chat-only until its separate bridge exists.

MCP text/structured/error content must be bounded and represented accurately. Image
content should be saved as a bounded workspace artifact, then exposed through the
existing ryn_image_inspect/_vision_inference path for supported API models. Do not put
base64 image data into ordinary text context or claim CLI screenshot vision support.
Never follow an external resource link to read arbitrary local files without permission.

## Computer Use vertical slice

Ship a portable first-party Computer Use package/provider or equivalent honest built-in
extension wired through this generic manager. It can reuse local_desktop and local_browser
and should expose window observation and bounded click/type/key/invoke/set-value actions.
An image may be saved for the supported image-inspection path. Each action needs a fresh
window observation and verification remains a subsequent read.

Prove the generic STDIO path with real JSON-RPC exchange, not mocked manager methods.
Prove HTTP with a local fixture server. On Windows, use an isolated synthetic acceptance
window owned by the test process: read its controls, enter a known marker, read it back,
and close only that fixture. Do not manipulate unrelated user windows. If the environment
cannot run real desktop acceptance, state that exact limit and retain transport evidence.

## Required acceptance

1. Existing Agent, Craft plugin and skill-selection tests keep passing.
2. A real stdio MCP fixture initializes, paginates its tool list, executes a tool through
   Ryn's runtime and returns an observable result. Invalid arguments never execute.
3. Streamable HTTP JSON/SSE, error response, timeout and disconnect are covered with
   a real local fixture or meaningful transport tests.
4. A skill with references and a script imports, appears in the task selector and can
   have its selected full content/reference read; disabled/deleted skills aren't offered.
5. Representative Codex and Claude packages import with preserved assets, truthful
   compatibility notes, duplicate detection and atomic failure behavior.
6. Reject archive traversal, symlink escape, oversize/deep imports and malformed schemas.
   Credentials never appear in public responses or execution context.
7. Disable/delete, pending schema/config changes, permission modes, cancellation and
   uncertain writes honor the existing runtime contract.
8. UI: add/edit/test/enable/delete MCP; preview/import/enable/inspect/remove Skill/plugin;
   search, keyboard flow, error recovery; desktop/mobile; Chinese/English; light/dark.
9. Complete the controlled computer-use read -> action -> read flow. Separate this
   evidence from generic transport and visual-model evidence.
10. Run focused pytest, frontend tests, TypeScript/production build and lint for changes.
    Do not weaken existing assertions or unrelated code to make checks pass.

## Collaboration and delivery

Gemini owns application code and tests. Codex owns this design, review, acceptance and
review feedback; fixes are sent back to the same verified Gemini conversation. Do not
commit, push, deploy, change live node settings, or change provider login/configuration.
Keep local process logs, prompts, session id and results in work/agent-extensions-20261019/.
Use isolated synthetic node homes for tests and browser acceptance; preserve live data.
Implementation must report modified files, actual commands/results, known unsupported
features and any tool permission denials. A SUCCESS label alone is not acceptance.

## Frozen frontend/backend API contract

The implementation can run in parallel with exclusive ownership: backend worker owns
Python/dependencies/tests; frontend worker owns webapp/. Both use Gemini 4 Argon Medium.
Codex owns design and independent acceptance. Do not edit the other worker's files.

Base path: `/api/local/agent/extensions`. GET base returns ExtensionStore.overview:
`{revision, plugins: Plugin[], skills: Skill[], mcp_servers: Server[], desktop_access}`.
Use the actual public DTO fields in extensions/store.py; do not invent alternate names.
They include `id`, `title/name`, `description`, `version`, `source_format`, `builtin`,
`deletable`, `enabled` (effective), `component_enabled`, `parent_enabled`, `revision`.
Plugins also include `skills`, `mcp_servers`, `warnings`, `unsupported_components`.
Skills include `content`, `files`, `warnings`, `plugin_id`.
Servers include `transport` (`stdio` or `streamable_http`), `command`, `args`, `cwd`,
`url`, `masked_env`, `masked_headers`, `bearer_token_env`, `startup_timeout_ms`,
`tool_timeout_ms`, `computer_access`, `tools`, `tools_count`, `status`, `status_message`,
`last_tested_at`, `enabled_tools`, `disabled_tools`, `read_only_tools`, `config_generation`.

- POST `/mcp/servers`: server form payload + expected_revision=overview.revision;
  returns public Server.
- PATCH `/mcp/servers/{id}`: partial payload + expected_revision=server.revision;
  returns public Server. Enable/disable and per-tool changes use this operation.
- DELETE `/mcp/servers/{id}?expected_revision=N`: returns `{deleted:true}`.
- POST `/mcp/servers/{id}/test` and `/restart`, body `{expected_revision}`:
  returns `{server:Server,test_result:{status:"connected"|"error",tools_count,error?}}`.
  Explicit testing while disabled is allowed but cannot enable the entry or expose its
  tools for Agent execution. Bind the probe to the initial identity/generation/enabled
  state; a disable/delete/recreate during a probe invalidates its result.
- PATCH `/plugins/{id}` or `/skills/{id}`, body `{enabled,expected_revision}`:
  returns the corresponding public entry.
- DELETE `/plugins/{id}` or `/skills/{id}` with expected_revision query:
  returns `{deleted:true}`. Built-ins cannot be deleted.
- POST `/import/preview`, JSON `{kind:"plugin"|"skill"|"mcp",path?,config_text?,
  skill_text?,filename?,zip_base64?}`. Exactly one source (path, config_text, skill_text,
  zip_base64). Returns `{preview_id,name,version,source_format,plugins:[],skills:[],
  mcp_servers:[],warnings:string[],unsupported_components:string[],can_import:boolean,
  expected_revision}`. Lists contain the detected public ID/name/title/description as
  applicable and no secret values. Failed parsing returns a bounded actionable error.
- POST `/import/commit`, body `{preview_id,expected_revision}`: returns fresh overview.
  Store preview content, not an unchecked reference to the original mutable source.

All configuration mutation routes require expected_revision. Error shape follows existing
Ryn `{detail:stable_error_code}` with 409 for conflicts. UI refetches after mutations,
retains form data after failure, and distinguishes successful save from connection test.
Editing secrets: omit env/header keys to preserve, send replacement key/value pairs to
replace, send null for a key to remove. Never submit masked values as replacements.

Computer permission UI uses the existing `PUT /api/local/agent/settings` with
`{desktop_access:boolean}` only; plugin enabling must not silently grant it. Existing
`GET /api/local/agent/skills` must expose enabled imported skills for task selection.
