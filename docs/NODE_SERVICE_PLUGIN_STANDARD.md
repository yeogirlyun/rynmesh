# Rynmesh nodes, services, plugins and apps

Status: canonical terminology and implementation direction, owner-requested
2026-10-29. Existing APIs and saved manifests remain compatible. Items marked
next are design/implementation work, not newly delivered protocol behavior.

The same infrastructure serves a person, household, community or company.
Rynmesh supplies the open rails; node operators supply useful services and
choose their audiences. Corporate participation introduces no mandatory network
account, platform commission or company-specific protocol.

## Definitions

| Term | Meaning | Existing implementation / boundary |
| --- | --- | --- |
| Participant/operator | Person or organization responsible for nodes and offered value | Different from a device, key, model or wallet; richer cross-node organization identity is future work |
| Node | Running infrastructure with identity, storage, connectivity and advertised capabilities | Personal/corporate deployments use common mechanisms; organization membership belongs to its operator |
| Service | Versioned capability for a stated audience with availability and optional price/terms | AI, document work, content, directories and business APIs; discovery is not execution or payment |
| API | Interface through which an authorized caller uses a service | HTTP/streaming/native adapters coexist; not an installer or identity authority |
| MCP server | Adapter exposing tools, resources or prompts through Model Context Protocol | Local process or remote endpoint; not the whole mesh, a node, wallet or deployment package |
| Plugin | Versioned installed/enabled capability package or connector with declared components | May expose MCP configuration, skills, views, a remote app or local editor; no implicit broad authority |
| Extension framework | Host infrastructure for discovering/configuring/invoking extra capabilities | Existing agent/extensions routes remain; not a third installer or another service/node type |
| App | User-facing experience | Editor, workspace or remote application; services can also work without an app |
| Skill | Instructions/references that help an assistant use a capability | Guidance grants no data access, execution, identity or spending authority |

MCP has a host/client/server model and tools/resources/prompts; see the
[official architecture](https://modelcontextprotocol.io/specification/2026-07-28/architecture).
This terminology does **not** upgrade Ryn's wire protocol to that specification.
Current versions remain negotiated/tested by each adapter; later protocol support
needs explicit compatibility work.

## Personal and corporate interaction

```text
Personal Ryn client / Agent
  -> plugin: discovery + declared UI/tools
  -> service API, using that service's current authorization
  -> personal/community/corporate provider
  -> result or durable task + artifact/receipt
```

An operator can expose employee-only reports, partner tools, customer services
and public information. Public discovery never implies employee status. One
personal client can consume unrelated corporate and personal services; a corporate
login is not required for local/personal features.

Organizations can publish private plugin invitations and operate membership/SSO.
That is operator policy, not network-wide membership. Deployment can comprise a
Ryn node and independent API/MCP/web services. A remote API alone does not prove
signed mesh participation or Exchange validator status.

## Repository ownership

Public `yeogirlyun/rynmesh` owns transport, generic discovery, model pool, tasks,
permission/grant mechanisms, plugin lifecycle, MCP clients, app bridge, generic
peer directories and conformance fixtures.

Company repositories own business schemas/dashboards, finance/affiliate rules,
organization mappings, company SSO adapters, upstream configuration, migrations
and deployment. Credentials, databases and operational snapshots stay outside
Git and plugin ZIPs even in private repositories.

The canonical private Gshopper repository is `yeogirlyun/gshopper-ryn-node`.
It imports the reviewed backend foundation and preserves the latest application
from `yyeogirl/chat-backend`, `codex/company-app-migration-20261028`, under
`integrations/company-app`. The source repository remains a preserved legacy
reference; new Gshopper node work belongs in the new repository. Public fixtures use
synthetic organizations/domains. Personal nodes remain useful/buildable without
the private repository.

## One plugin concept with compatible backends

Two current implementations have different responsibilities:

- Node Apps/Craft manager (`/api/local/plugins`, compatible Craft routes):
  explicit download/install, hashes, enable/disable, uninstall, editor launch
  and adapter lifecycle.
- Agent extension framework (`/api/local/agent/extensions`): declarative imports,
  MCP/OAuth, skills, views, remote app declarations and data policy. Remote apps
  open at `/apps/extensions/:pluginId`.

Both are plugins in product vocabulary. Preserve their IDs, state, routes and
receipts; do not merge stores or rename APIs merely to align words. Next: propose
one catalogue/status projection over both backends so users see one Apps/Plugins
experience with version, source, actual capabilities and useful recovery guidance.

Users should see Connect service, Open app and Ask Ryn; protocol details belong
in advanced settings. Enabling an app never fabricates editing/tool support.

## Interaction contracts

1. **Declare actual capabilities.** Publisher/provider, version, platform,
   transport, schemas, app origin and requested capabilities agree. Native
   inference need not be MCP; add an optional adapter where useful.
2. **Connect once.** Remember valid sessions and scoped grants. Routine reads
   within their audience/context need no repeated prompt. Ask when recipient,
   write scope, paid terms or materially broader access changes. No complex
   role/KYC framework is required at this stage.
3. **Preserve identity correctness.** Providers map the verified caller to an
   active member/customer/partner. UI caches accelerate display, never choose an
   arbitrary administrator. Expired sign-in preserves the user's pending work.
4. **Keep credentials in the host/service.** Current remote apps use isolated
   origins and ryn.plugin.request/ryn.plugin.response. The bridge checks sender,
   enabled state and declared permission. tools.query uses that plugin's tools;
   agent.draft opens an editable draft. Current groups are tools, agent, models,
   messages and peer_provider; see [remote apps](REMOTE_PLUGIN_APPS.md). Finer
   scopes require a separate compatible design, not implicit new permissions.
5. **Recover original work.** Keep task, provider and terms; show progress,
   reconnect within budget, cancel promptly, and reconcile uncertain writes.
   Do not silently switch providers or repeat purchases.
6. **Deliver useful outputs.** Preserve artifact identity/format/hashes and reopen
   evidence where appropriate. Headless Agent documents and open editors' unsaved
   documents remain separate until an explicit save/reopen handoff exists.
7. **Separate discovery from access.** Directories publish approved projections;
   membership does not become personal-device ownership or reveal private files,
   models and messages.
8. **Separate accounting domains.** Company money/commissions/withdrawals are
   business data, not Ryncoin, reputation or simulated Task Balance. Network
   purchases reuse Exchange when an adapter is ready; free access needs no wallet.

## Efficiency, adoption and next deliverables

Bound and cache capability/catalogue metadata with revisions/expiry. Reuse
connections and stream useful results. Busy/offline states retain drafts and
explain recovery. Do not send full histories/document bodies to every service.

The next journey is invitation -> real answer -> useful artifact -> save/share
-> return. Measure cold start, connection, first useful result, artifact reopen
and successful invitation separately through optional local records/user feedback.
This standard adds no telemetry or recurring monitoring.

Next contributor proposals: unified catalogue projection; an operator-neutral
example with a tool/app/recoverable task/artifact; personal-to-personal and
personal-to-corporate acceptance; lightweight persistent company sessions;
one private weekly reporting workflow; a self-contained browser adapter; and
clear editor/document handoff. Keep those proposals independently reviewable
and preserve ordinary model, task, document and sharing functionality.
