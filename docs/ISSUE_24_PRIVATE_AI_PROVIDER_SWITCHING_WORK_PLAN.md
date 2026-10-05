# Issue #24 work plan: in-chat Private AI provider and model switching

Status: implementation-ready  
Issue: https://github.com/yeogirlyun/rynmesh/issues/24  
Recommended order: implement before #25; independent from #23 streaming

## 中文执行摘要

当前 Private AI 聊天只在页面打开时选择一次 Provider。虽然服务发现、价格、
上下文窗口、在线状态和 `(peer_id, package_id)` 复合键都已经存在，但聊天页没有
切换入口，历史列表也只加载首次选中的服务桶。

本任务只改 Webapp：在聊天页加入 Provider/模型选择器和对比信息。切换时保存
当前会话，加载目标服务自己的会话桶；不把一段对话自动复制给另一个 Provider，
避免历史归属、隐私和计费语义混乱。请求进行中禁止切换。服务身份始终使用
`peer_id + package_id`，不能用可能重名的模型别名。

## 1. Verified current state (2026-09-02)

- GitHub Issue is open with `status:available`.
- Neither `upstream` nor `origin` has a matching feature branch.
- No open or closed business PR references this work.
- No local branch or commit implements the feature.
- `PrivateAIChat.tsx` discovers all services but chooses one only during initial
  load from query parameters, the first online service, or the first record.
- `Services.tsx` already proves that a compound provider/package selector works
  and exposes the comparison data required by the Issue.
- `llmServiceRecordKey` already defines the stable service identity.
- `llmConversationStore` already stores each conversation under a service key
  and encrypts message bodies in IndexedDB.

This is therefore unimplemented, but its data and persistence foundations are
already present.

## 2. Goals

- Let the owner switch Provider/model without leaving Private AI.
- Show model alias, Provider name/peer identity, package ID, online/busy state,
  context window, maximum output, and input/output pricing before switching.
- Keep every conversation bound to the service key with which it was created.
- Restore the most recent conversation for a previously used service.
- Create a new empty conversation when the target service has no history.
- Keep the URL's `peer`, `service`, and `network` parameters synchronized so a
  refresh restores the same service when it is still discoverable.
- Handle a disappeared, offline, or full Provider without losing history.
- Keep all order submission through the local node.

## 3. Non-goals

- Do not move or clone conversation history between Providers automatically.
- Do not send the same prompt to multiple Providers as a comparison run.
- Do not implement token streaming; that belongs to #23.
- Do not change the LLM task, settlement, discovery, or encryption protocol.
- Do not change the IndexedDB encryption key or database schema version.
- Do not treat the model alias as a unique identifier.

## 4. Interaction contract

### 4.1 Selector

Add a selector to the chat header. The collapsed state shows the active model,
Provider, online state, and price currency. The expanded list shows:

- model alias and package ID;
- Provider display name plus a shortened peer ID;
- online/offline and available/busy state;
- context window and maximum output tokens;
- input/output price per 1k tokens and minimum price.

Offline entries remain visible so the owner understands why old history cannot
currently submit. The currently selected compound key is marked explicitly.

### 4.2 Switching rules

Switching from service A to service B performs this sequence:

1. refuse the switch while an order is submitting, running, or cancelling;
2. keep A's current conversation saved under A's existing service key;
3. load `listConversations(key(B))`;
4. select B's most recently updated conversation, or create one for B;
5. update `selectedService`, conversations, selected ID, details, and URL in one
   state transition;
6. retain an unsent composer draft in memory, because it has not yet been sent
   to either Provider.

The UI must not render A's messages under B's header during the asynchronous
load. Use an explicit switching/loading state or load before committing the
new active key.

### 4.3 Discovery refresh

Refresh service discovery periodically while the page is open (recommended:
10 seconds, paused when the document is hidden). Reconcile records by
`llmServiceRecordKey` and do not reorder the owner's current selection merely
because discovery order changed.

If the selected service disappears, retain its local history and show it as
unavailable. Submission stays disabled until it reappears or the owner chooses
another service.

### 4.4 Conversation ownership

`LLMConversation.serviceKey`, `serviceName`, and `providerPeerId` are immutable
identity fields after creation. A send operation must derive Provider and
package from the conversation's service key and the matching discovery record,
not merely from a mutable visual selection.

Before submitting, assert that the selected conversation and selected service
have the same service key. Fail locally with a user-readable error if they do
not; never risk sending a conversation to the wrong Provider.

## 5. Implementation steps

### Step 1: extract service comparison helpers

Files:

- `webapp/src/domain/llmOrders.ts`
- `webapp/src/domain/nodeClient.ts` only if a display type is useful

Add pure helpers for availability, display identity, and pricing labels. Keep
compound identity in the existing functions rather than introducing another
key format.

### Step 2: add the chat-header selector

Files:

- `webapp/src/screens/PrivateAIChat.tsx`
- `webapp/src/screens/PrivateAIChat.module.css`

Implement accessible keyboard navigation, visible focus, a selected marker,
and readable offline/busy states. Reuse existing service record fields; no new
backend response is required.

### Step 3: make conversation buckets switchable

File: `webapp/src/screens/PrivateAIChat.tsx`

Move initial service selection and bucket loading into reusable functions.
Guard against stale async loads with an incrementing request generation or
abort flag. Disable switching while a request is active.

### Step 4: synchronize navigation state

Use React Router search-parameter replacement so switching does not add a new
browser history entry for every Provider selection. Preserve `network` and any
future unrelated parameters.

### Step 5: refresh availability safely

Re-query `listLLMServices(networkId)` on a bounded interval. Preserve the
selected compound key and avoid clearing encrypted history when discovery is
temporarily unavailable.

## 6. Test plan

Extend `webapp/src/screens/PrivateAIChat.test.tsx` to cover:

- two services with the same model alias but different peer/package keys;
- visible Provider, context, price, and online comparison fields;
- switching creates or restores the correct service-specific history bucket;
- messages from Provider A never appear in Provider B's conversation;
- the exact selected peer and package are submitted after switching;
- URL parameters track the active service and restore it on remount;
- switching is disabled while an order is in flight;
- an offline/disappeared Provider keeps history but cannot submit;
- an unsent draft survives a switch;
- a stale async bucket load cannot overwrite a newer selection.

Keep existing conversation encryption and corruption-recovery tests passing.

## 7. Acceptance criteria

- [ ] A user can switch Provider/model entirely inside Private AI.
- [ ] Required comparison data is visible before selection: Provider identity,
  package, online/busy state, context window, output limit, and price.
- [ ] Two services with the same model alias remain distinguishable and submit
  the exact selected `peer_id + package_id`.
- [ ] History remains bound to the original compound service key.
- [ ] No request can be sent to a Provider different from the conversation
  header and stored conversation identity.
- [ ] URL parameters restore the same service after refresh when available.
- [ ] Switching is blocked while an order is submitting/running/cancelling.
- [ ] Offline, busy, disappeared, and stale-discovery states are understandable
  and do not delete history or drafts.
- [ ] Existing encrypted persistence, cancellation, settlement, and node-only
  gateway boundaries remain unchanged.
- [ ] Focused Vitest, full Webapp tests, TypeScript typecheck, and production
  build pass.

### Required acceptance evidence

- Attach an interaction recording or screenshots covering service comparison,
  switch, refresh restore, offline state, and return to the original history.
- Attach a test result showing two equal aliases submit to different exact
  peer/package identities.
- Inspect IndexedDB through the existing tests and show that message bodies
  remain encrypted and isolated by service key.
- Record the implementation commit and the focused/full Webapp commands used.

## 8. Readiness assessment

**Can development start now: yes.** This is a contained Webapp change with no
protocol or backend blocker. It should land before #25 so grounded article
conversations can use the same Provider selector. It may land before or after
#23, but keeping switching separate from streaming will make both reviews
smaller and safer.
