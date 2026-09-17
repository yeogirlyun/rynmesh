# Services UI architecture

Status: v0.7.0 service screens with the #26 shared lifecycle framework.

This document describes the user-facing Services catalog and its typed service
experiences. It records current behavior and the boundaries contributors must
preserve when extending the UI.

## Product contract

`/services` is a task-first catalog. Users choose what they want to do; provider
nodes, package IDs, networks, and transport details remain behind optional
details or the legacy management screen.

Each service type opens the interaction suited to its lifecycle:

| Route | Experience | Node-client boundary |
|---|---|---|
| `/services` | Searchable and filterable service catalog | `listLLMServices`, `listJobCapacities` |
| `/services/private-ai/chat` | Node-owned multi-conversation chat | `askHistory` / Ask runs; `NodeClient` orders in fixture mode |
| `/services/video-rendering` | Bounded render workflow | `submitWorkOrder`, `listWorkResults` |
| `/services/secure-web-access` | Connect, launch, and disconnect lifecycle | `egressStatus`, `egressConnect`, `egressLaunch`, `egressDisconnect` |
| `/services/manage` | Advanced provider/package administration | Existing Services APIs |
| `/chat` | Direct peer-to-peer messaging | Messaging APIs; intentionally unchanged |

The catalog currently maps discovered language-model and video capabilities to
three curated product experiences. Adding generic service-manifest rendering is
separate protocol work; contributors should not expose raw manifest fields as a
user workflow without an accepted design.

## Data flow

```text
Services experience
        |
        v
typed NodeClient method
        |
        v
local Ryn node control API
        |
        v
provider discovery / transport / settlement
```

The webapp does not contact providers or the registry directly. The local node
remains the enforcement point for discovery, transport, cancellation, balance,
and result retention.

## Local state and privacy

The catalog stores up to three recently opened service IDs and timestamps in
`localStorage` under `ryn.services.recent.v1`. It does not store prompts,
results, provider IDs, or routes there.

Live Private AI conversations and task state belong to the node's encrypted
Ask history repository. The browser polls that repository and submits/cancels
through Ask runs. It does not use IndexedDB as the primary live history store.
Legacy browser history retains its encrypted migration/fixture adapter; a failed
migration must remain visible. This change does not alter retention, settlement,
archive-before-cleanup, or deletion semantics.

Discovery, order snapshots and pending-operation flags in the shared hooks are
memory-only. Provider identity includes network, peer and service; the existing
conversation key format remains unchanged for storage compatibility. The selected
provider necessarily sees plaintext during inference.

## Shared lifecycle framework

`serviceDescriptors.ts` owns capability/operation names, pricing units, region
metadata and polling cadences. Prices still come from verified node advertisements.
The secure-web descriptor retains CN as the supported default; adding regions is
separate product work. Video discovery matches its exact capability.

`useProviderDiscovery` deduplicates by full provider identity. `useServiceOrder`
owns completion-scheduled reads, bounded exponential retry delay, terminal-state
stopping and explicit operations. Manual refresh joins an in-flight read. Switching
client or identity suppresses late results; unmount stops local timers and never
cancels a remote task. A pending explicit operation is never retried automatically.
A read started before a write cannot overwrite the write's result. Fixture chat
uses the hook's exclusive `awaitTerminal` adapter for its original task.

Catalog, Private AI, Video rendering, Secure web access and Services management
use these hooks. The management page's local model installation/recovery jobs retain
their dedicated lifecycle protocol. The #23 streaming subscription remains separate
from node-owned final history reconciliation.

Video result reads bind to the original network/provider/order and ignore unrelated
results. A submission with an uncertain response cannot silently submit again;
the user must check the original request before explicitly allowing a new render.
This is not a durable purchase-recovery ledger: refreshing the page still loses
that transient form, as before. Node-side consumer order lookup and durable recovery
are separate protocol work. No shared hook creates an order merely by mounting.

## Compatibility rules

- Keep `/chat` reserved for direct node messaging.
- Keep `/services/manage` available until its advanced provider and package
  controls have dedicated replacements.
- Route all service actions through `NodeClient`; do not call peers directly.
- Preserve the `client=fixture` query parameter in fixture navigation so UI
  tests do not fall through to a live node.
- Do not put conversation or result bodies in URLs, logs, `localStorage`, or
  catalog analytics.
- Infrastructure identifiers may appear in explicit Details sections, but not
  as required inputs for ordinary users.

## Contributor workflow

When adding or changing a service experience:

1. Add or reuse a typed `NodeClient` method and implement both live and fixture
   clients.
2. Keep the catalog card action-oriented and provide unavailable/error states.
3. Add component tests for the full action lifecycle, including failure or
   cancellation where applicable.
4. Run `npm test`, `npm run lint`, and `npm run build` from `webapp/`.
5. Browser-test the affected route and attach screenshots to the pull request.
6. Update this document when routes, persistence, privacy, or compatibility
   boundaries change.

Browser acceptance evidence for the current implementation is stored in
[`acceptance/services-ui-browser/`](acceptance/services-ui-browser/README.md).
