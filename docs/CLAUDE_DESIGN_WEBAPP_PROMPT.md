# Claude Design Prompt: Ryn Webapp

Use this prompt in Claude Design to review the existing Rynmesh repository, design the first Ryn webapp, and generate the corresponding frontend codebase.

## Prompt

You are Claude Design working inside the Rynmesh repository. Review the existing codebase and docs first, then design and implement the first Ryn webapp.

Read these files before designing:

- `README.md`
- `docs/ARCHITECTURE.md`
- `docs/RYN_NODE_WEBAPP_SPEC.md`
- `rynmesh/peer_http.py`
- `rynmesh/store.py`
- `rynmesh/types.py`
- `rynmesh/manifest.py`
- `rynmesh/provenance.py`
- `rynmesh/identity.py`
- `rynmesh/credits.py`
- `rynmesh/mcp_server.py`

## Product Truth

Rynmesh is a verifiable AI node network for AI-generated and AI-curated content. Users run a local Ryn node daemon and a Ryn webapp. The webapp lets users review what is available through the network, ask an AI curator for recommendations, inspect receipts, fetch previews or full content, and publish approved local material.

The webapp is not the network. The AI model is not the network. The registry is not the network. The Ryn node is the user's trusted gateway into Rynmesh.

Core rule: every operation goes through the local Ryn node. The webapp must not contact remote peers or registries directly. It should call a local node control API. If that API does not exist yet, design the API contract, implement the frontend against a typed live-client boundary, and include development fixtures only for previewing the design while the local control API is being implemented. Do not ship mock-only screens as the production path.

## Vocabulary To Use Consistently

- **Rynmesh**: the protocol and peer network.
- **Ryn node**: the local node controlled by the user.
- **Ryn node daemon**: the local server process the user runs.
- **Ryn webapp**: the browser GUI for the local node.
- **Rynmesh MCP server**: the agent adapter for the same node.
- **Peer**: another Ryn node.
- **Registry**: discovery coordination, not a trust authority.
- **Content item**: any publishable artifact: video, image, audio, document, slide deck, dataset, code artifact, report, or multimodal package.
- **Manifest**: signed metadata binding content hash, preview hash, publisher, safety receipts, provenance references, title, description, and tags.
- **Provenance chain**: signed, tamper-evident chain of creation, scan, and attestation events.
- **Safety receipt**: signed verifier result for a content item.
- **Rynmesh Credits**: non-transferable signed reputation events for useful work and penalties.
- **Distribution weight**: ranking signal derived from credits and identity tier caps.
- **Identity tier**: `unverified`, `attested`, `staked`, or `proven`.
- **AI curator**: the user-selected local or cloud model that reviews node-visible evidence and returns recommendations.
- **Recommendation**: a user-reviewable AI suggestion with cited evidence.

## Target User Experience

Design Ryn webapp as a calm, high-trust local operations console for AI-curated discovery. It should feel like a powerful review desk, not a marketing site, social feed clone, crypto dashboard, or generic file browser.

The user should feel:

- "My node is running and I understand what it sees."
- "I can ask AI to help review materials, but I stay in control."
- "Every item has receipts, provenance, safety status, and source identity."
- "Network operations are mediated by my node."
- "Publishing is deliberate and reviewable."

Do not build a landing page as the first screen. The first screen should be the actual usable app.

## Visual Direction

Use a restrained, professional interface suited to repeated review work:

- dense but readable information layout
- strong hierarchy for status, source, trust, and actions
- clear badges for safety, provenance, identity tier, and fetch state
- durable table/list patterns for scan-heavy workflows
- item detail views that make receipts inspectable without overwhelming the user
- conversational AI panel that feels embedded in the workflow, not like a separate chatbot product

Avoid:

- crypto/cyberpunk visual cliches
- oversized marketing hero sections
- decorative blob/orb backgrounds
- one-note purple/blue gradient palettes
- vague trust language without concrete receipt fields
- UI that implies the AI can publish or trust roots autonomously

## Required Screens

Build a working first version with these screens. Use realistic development fixture data only behind the local node client boundary so the UI can be reviewed before the local control API is complete.

### Home

Purpose: compact node status and entry point.

Include:

- node name and peer ID
- daemon status
- registry status
- peer count
- local content count
- fetched content count
- pending recommendations
- recent verification, fetch, publish, and credit events
- primary actions: Explore, Ask AI, Publish, Settings

### Explore

Purpose: browse local, fetched, and discovered content.

Include:

- source filter: local, fetched, discovered peers, specific peer
- content kind filter
- safety outcome filter
- identity tier filter
- provenance status filter
- rank mode: distribution weight, newest, trusted, AI-curated, novelty
- search query
- content list/table/cards with title, preview snippet, kind, type, source peer, provider peer, distribution weight, safety, provenance, identity tier, fetch status

### Recommendations

Purpose: review AI-curated suggestions.

Each recommendation must show:

- title and preview
- why AI recommends it
- evidence used
- confidence or priority
- safety and provenance badges
- source peer and identity tier
- whether the AI reviewed metadata only, preview, or full content
- actions: Inspect, Fetch Preview, Fetch Full, Hide, More Like This

### Search And Ask

Purpose: conversational steering for node-mediated search and AI review.

Support example user requests:

- "Find more like this."
- "Show only proven peers."
- "Search design references about urban gardens."
- "Ignore low-trust peers."
- "Fetch previews for the top 10."
- "Explain why item 4 outranks item 7."

The UI must make clear that the webapp sends the request to the local node. The node decides which discovery, listing, fetch, ranking, or AI-curation operations are allowed.

### Publish

Purpose: user-approved publishing flow.

Design a stepper:

1. Select file or folder.
2. Add title, description, tags, content kind, and optional model metadata.
3. Node builds preview and content hash.
4. Node runs safety scan.
5. Node builds provenance chain and manifest.
6. Webapp shows pre-publish review.
7. User confirms publish.

Publishing must require explicit user confirmation.

### Item Detail

Purpose: inspect one content item deeply.

Include:

- preview area
- metadata
- content hash and manifest hash
- publisher identity
- provider identity
- provenance chain timeline
- safety receipts
- credit/reputation signals
- fetch history
- actions: Fetch Preview, Fetch Full, More Like This, Hide/Downrank, Report/Quarantine, Copy Citation/Receipt Data

### Peers

Purpose: show discovered nodes and trust signals.

Include:

- peer ID and short slug
- node name
- endpoint
- network ID
- identity tier
- credit score
- distribution weight
- last seen
- served/fetched counts when available
- local trust/quarantine status

### Settings

Purpose: configure local node behavior.

Include:

- node identity and storage paths
- registry URL
- peer HTTP host, port, and public endpoint
- trusted root peer IDs
- safety policy
- model provider for AI curator
- local-only/cloud model toggle
- ranking preferences
- publish defaults
- fetch limits and timeouts

## Critical Interaction Rules

Low-risk actions can run directly:

- list local content
- list cached peers
- inspect manifest
- inspect provenance
- run local ranking

Medium-risk actions should make network/model effects visible:

- query registry
- list peer content
- fetch preview
- send bounded metadata to AI model

High-risk actions require explicit confirmation:

- fetch full content
- publish content
- trust a new root peer
- change safety policy
- enable cloud model access
- quarantine or globally report a peer

The AI curator may recommend or suggest. It may not publish, trust roots, change safety policy, or send full local files to a cloud model without explicit confirmation.

## Implementation Requirements

Create a new frontend codebase under `webapp/` unless the repository already has a webapp directory.

Preferred stack if no frontend exists:

- React
- TypeScript
- Vite
- CSS modules or a small local styling system
- lucide-react for icons

Implement:

- route-level screens for Home, Explore, Recommendations, Search And Ask, Publish, Item Detail, Peers, Settings
- shared app shell with sidebar/topbar navigation
- typed domain models matching Rynmesh vocabulary
- typed local node API client
- live local node API adapter
- development fixture adapter with realistic data for design preview
- a single switchable boundary where live local node API calls and development fixtures share the same interface
- reusable components for status badges, identity tier badges, safety badges, provenance timeline, content item rows/cards, recommendation cards, receipt panels, confirmation dialogs, and empty/error/loading states
- responsive layout for desktop and tablet; mobile should remain usable for review, but desktop is primary

Do not implement direct remote peer or registry calls in the webapp. All network-looking actions must go through the local node client abstraction. Fixture data is allowed for design preview, but it must not be presented as a substitute for the local Ryn node.

## Local Node API Contract To Design Against

Define the frontend-facing local control API contract in code with these operations:

- `getNodeStatus()`
- `getRegistryStatus()`
- `discoverPeers(request)`
- `listPeers(filters)`
- `listContent(filters)`
- `getContentItem(contentId)`
- `fetchPreview(contentId, providerPeerId)`
- `fetchFullContent(contentId, providerPeerId)`
- `requestRecommendations(request)`
- `submitSearchAsk(request)`
- `preparePublish(request)`
- `confirmPublish(publishDraftId)`
- `getCreditScoreboard(filters)`
- `getSettings()`
- `updateSettings(patch)`

Development fixture responses should include enough data to exercise safety states, provenance states, identity tiers, content kinds, fetched/unfetched items, recommendation evidence types, and confirmation flows.

## Data And Trust Details To Surface

Every content item should be able to show:

- `content_id`
- `manifest_hash`
- `title`
- `description`
- `tags`
- `content_kind`
- `content_type`
- `publisher_peer_id`
- `provider_peer_id`
- `source_peer_name`
- `identity_tier`
- `credit_score`
- `distribution_weight`
- `safety_outcome`
- `safety_scanner_id`
- `provenance_status`
- `provenance_head_hash`
- `fetch_status`
- `review_basis`: metadata, preview, full content

Recommendations should cite:

- content IDs used
- evidence basis
- query match
- safety status
- provenance status
- peer reputation
- novelty or diversity reason
- uncertainty or missing evidence

## Design Quality Bar

The app should feel complete enough for a private alpha user to understand and use the system without reading protocol docs.

Before finishing:

- verify text does not overflow in cards, badges, nav, tables, or buttons
- verify controls have stable dimensions
- verify the app has empty, loading, error, and offline-node states
- verify high-risk actions show confirmation UI
- verify mock data demonstrates the core Rynmesh concepts
- verify terminology matches the vocabulary above
- verify no UI implies the webapp or AI bypasses the Ryn node
- run the frontend build/test/lint commands you add

## Expected Output

Return:

- a concise explanation of the design direction
- a list of files created or changed
- the run commands for the webapp
- notes on the local node API contract that the backend should implement next
- any known gaps that remain after the first implementation

Then implement the codebase.
