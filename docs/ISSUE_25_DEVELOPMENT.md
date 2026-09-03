# Issue #25 development document — grounded Private AI

Status: implemented

Baseline: `ef817bc` (`feat: switch Private AI providers in chat`)

## Architecture

```text
DigestViewer (reader blocks)
  -> random one-time in-memory handoff
  -> /services/private-ai/chat?grounding=<opaque-id>
  -> consume after Provider bucket is ready
  -> encrypted LLMConversation.grounding
  -> deterministic bounded prompt
  -> local Consumer node
  -> selected Provider
```

Only the opaque handoff ID crosses the route boundary. The chat atomically
replaces it with `peer`, `service`, and `network` after consumption, preserving
Issue #24 deep-link restoration without leaving a reusable grounding ID.

## Main implementation areas

- `webapp/src/domain/groundedContextHandoff.ts`
  - 24 cryptographically random bytes, hex encoded;
  - cloned value, five-minute TTL, opportunistic expiry purge;
  - consume exactly once; no enumeration surface.
- `webapp/src/domain/groundedContext.ts`
  - shared Webapp safety estimator;
  - prompt boundary/marker neutralization;
  - newest successful transcript retention;
  - original-order article inclusion and Unicode-scalar prefix truncation;
  - exact included/original metadata.
- `webapp/src/domain/llmConversationStore.ts`
  - optional `grounding` field inside the already encrypted JSON payload;
  - no IndexedDB schema migration or plaintext secondary store.
- `webapp/src/components/DigestViewer.tsx`
  - reader-state action, one-click navigation, safe reader-history rendering.
- `webapp/src/screens/PrivateAIChat.tsx`
  - grounded conversation creation/card/removal;
  - live pre-send budget preview;
  - bounded prompt submission and failure draft restoration;
  - generation-safe Provider storage error recovery.
- `rynmesh/llm_package/context_safety.py` and `routes.py`
  - matching Consumer-node admission policy.

## Context safety contract

Pricing and safety are intentionally different:

- Pricing preview remains `ceil(characters / 4)` because it is only an
  approximate hold/price display.
- Context safety uses `max(1, UTF-8 byte length)` plus the output cap and a
  fixed 128-token safety margin.

The UTF-8-byte estimate deliberately over-reserves ASCII and does not
under-count CJK, combining marks, or emoji. Both Webapp prompt construction and
Consumer admission apply:

```text
safety_input + output_cap + 128 <= provider.context_window
```

Until a Provider supplies a trusted tokenizer/count endpoint, this is the
portable v1 upper bound.

## Prompt construction

The final prompt contains fixed instructions outside a single
`ARTICLE_CONTEXT` block. Occurrences of `ARTICLE_CONTEXT` in title, byline, or
article text receive a zero-width separator so source text cannot close or
open the trusted delimiter. The source URL remains local and is not sent.

Budget allocation is deterministic:

1. fixed policy plus latest question;
2. newest successful transcript turns that fit, rendered chronologically;
3. complete reader blocks in original order;
4. if the next block is too large, the largest fitting Unicode-scalar prefix.

No network fetch, summarizer, embedding service, or unapproved full-content
read is invoked during budgeting.

## Provider bucket and error invariants

- `serviceKey = provider_peer_id + package_id`; aliases are display-only.
- A grounded conversation stays in its originating bucket.
- During switching, the old service and history remain paired until the target
  bucket is completely loaded.
- `loadServiceBucket` uses `try/catch/finally` plus a latest-generation gate.
  A stale request cannot clear a newer switch, and storage failure cannot leave
  switching stuck or replace the current bucket/draft.
- Removal writes the conversation again through the encrypted store before the
  next send.

## Runtime hardening discovered during browser acceptance

Real-browser testing found and fixed two gaps that component-only testing did
not initially expose:

1. grounding removal and Issue #24 URL synchronization raced, briefly dropping
   `peer/service/network`; the operation is now atomic;
2. compact consumption-history records can omit `evidence_packet`; Digest card
   and viewer rendering now treat it as optional instead of crashing when the
   user returns from Private AI.

The 2026-09-03 completion audit added two further deterministic safeguards:

3. an initial empty conversation and its grounded handoff conversation can
   share a millisecond timestamp; the grounded conversation now receives a
   strictly newer sort key so Provider round-trips restore it reliably;
4. a Provider whose context cannot fit a useful article excerpt now shows an
   actionable larger-context/remove message instead of only disabling Send.

## Repeatable browser harness

`scripts/issue25_browser_fixture.py` is a deliberately narrow acceptance-only
HTTP Consumer. It exposes the local Reader, discovery, settings, fixture
Provider, and async LLM-order endpoints needed by this flow. It never contacts
a Registry, publisher, or Provider and stores temporary state outside the
repository.

The harness request ledger is privacy-preserving: it records endpoint paths,
query-key names, body sizes/hashes, and synthetic-marker booleans, but never
request or response bodies. `VITE_RYN_NODE_BASE_URL` points both the live
`NodeClient` and `digestApi` at this isolated Consumer, so the browser exercises
the production client boundary instead of the in-browser `client=fixture`
shortcut. Reproduction commands and captured evidence live in
`docs/evidence/issue-25/README.md`.
