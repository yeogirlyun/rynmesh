# Issue #23 work plan: stream Private AI responses over the direct transport

Status: design-complete draft; implementation depends on #28  
Issue: https://github.com/yeogirlyun/rynmesh/issues/23  
Recommended order: merge #28; land #24 and #25 input flows; then implement #23

## 中文执行摘要

当前适配器只探测模型是否支持 streaming，真正推理仍发送 `stream: false`；
Provider 等完整结果后返回一个加密响应，Consumer 本地 API 也只提供轮询，所以
聊天生成几十秒时页面一直显示等待。

本任务只让 `peer_http_direct` 路径流式返回。Provider 将每个增量封装为带序号的
端到端加密事件，Consumer node 解密后通过本地 SSE 转发给 Webapp；最终结果仍
使用现有 `llm_response` 终态信封，并且只按最终 usage 结算一次。Relay 和严格
P2P 路径继续整段返回，但使用同一个浏览器事件接口平滑退化。

#28 必须先合入，因为当前 upstream 的 LLM POST 仍绕过 Transport。本任务还要在
Transport 上增加有界的增量响应能力，不能重新引入一条裸 `urllib` 旁路。

## 1. Verified current state (2026-09-02)

- GitHub Issue is open with `status:available` and has no matching online or
  local feature branch, PR, or implementation commit.
- `OpenAICompatibleAdapter.capabilities()` already probes `stream: true`.
- `OpenAICompatibleAdapter.infer()` always sends `stream: false` and returns a
  complete text result.
- `ProviderService.handle()` performs full inference before returning one
  sealed `llm_response` envelope.
- Direct Consumer delivery blocks on `/api/peer/llm/tasks` until inference is
  complete.
- `/api/local/llm/orders/async` plus status polling exposes only queued/running/
  terminal snapshots.
- `PrivateAIChat.tsx` polls every 650 ms and renders a thinking indicator until
  terminal output arrives.
- Relay and strict P2P paths already have complete-message semantics.
- Local branch `codex/issue-28-transport-post` contains one implementation
  commit (`933c312`) but it is not merged or pushed as a PR.

Streaming is therefore not implemented. Capability probing alone must not be
reported as end-to-end streaming support.

## 2. Goals

- Stream text deltas from a compatible local inference adapter.
- Carry deltas over the node-to-node direct path without exposing plaintext to
  Registry, Relay, logs, or a browser-to-Provider connection.
- Authenticate, encrypt, order, and size-bound every stream event.
- Expose one local browser event interface for streaming and non-streaming
  transports.
- Render partial assistant text promptly and accessibly.
- Preserve cancellation, idempotency, final encrypted result retention, and
  exactly-once settlement from final metering.
- Fall back cleanly when the model, selected Transport, Relay, or P2P path does
  not support streaming.

## 3. Non-goals

- Do not stream through Registry blobs, the dedicated Relay, or strict ICE/P2P
  in this issue.
- Do not let the browser contact the Provider node.
- Do not persist partial plaintext deltas to disk.
- Do not settle per token or per chunk.
- Do not change credit currency or pricing.
- Do not promise resumability after a Consumer node restart.
- Do not add tool calls, multimodal chunks, or reasoning-token display.

## 4. Protocol decisions

### 4.1 Capability negotiation

Keep adapter capability (`streaming`) separate from delivery capability.
Streaming is selected only when all are true:

- the service manifest advertises streaming adapter support;
- the requested transport resolves to `peer_http_direct`;
- the active Transport implementation supports incremental POST responses;
- the Consumer requests stream protocol version 1.

Otherwise execute the existing complete-message path. Capability fallback is
normal behavior, not an error.

### 4.2 Request

Add optional fields inside the existing encrypted request body:

```json
{
  "response_mode": "stream-v1",
  "stream_event_max_bytes": 262144
}
```

Old Providers ignore or reject the new mode; the Consumer retries the same
task ID using the complete-message path only when it can do so without running
inference twice. A version/capability check should normally avoid that retry.

### 4.3 Provider stream endpoint

Add `POST /api/peer/llm/tasks/stream` returning bounded NDJSON or SSE. NDJSON is
recommended for node-to-node transport because it has simple binary-safe line
framing; the local browser boundary may still use SSE.

Every line contains one signed and sealed envelope. Event bodies use:

- `kind: llm_stream_delta`, `task_id`, `sequence`, `delta`;
- optional safe progress metadata such as elapsed milliseconds;
- a final existing `llm_response` envelope containing complete output, usage,
  duration, amount, state, and service identity;
- or a sealed terminal failure/cancellation response.

Use a monotonically increasing zero-based sequence. Reject duplicate, missing,
out-of-order, wrong-task, wrong-signer, wrong-service, expired, or oversized
events. Registry and task history store metadata only; they never store delta
plaintext.

The final envelope remains the source of truth. Deltas are presentation data,
not billable records.

### 4.4 Encryption and retention

Reuse the existing X25519/Ed25519 task primitives with a distinct
`llm_stream_delta` kind. Each delta is authenticated for the selected Consumer
and signed by the selected Provider. The final full response continues to use
`llm_response` and the existing encrypted-response retention policy.

Partial deltas may exist only:

- in the Provider adapter/process memory;
- on the direct encrypted wire;
- in a bounded Consumer in-memory broker;
- in current Webapp React state.

Do not write partial deltas to `TaskOrderStore`, IndexedDB, logs, crash reports,
Registry, or Relay. Save the assistant message to encrypted conversation
storage once the terminal result is verified.

## 5. Transport extension

#28 adds bounded `post_bytes`, which still buffers the whole response. Extend
the seam narrowly with an incremental operation, for example:

```python
def iter_post_bytes(
    self,
    url: str,
    body: bytes,
    *,
    timeout_s: float,
    max_chunk_bytes: int,
    max_total_bytes: int,
    headers: dict[str, str] | None = None,
) -> Iterator[bytes]: ...
```

Rules:

- the iterator closes sockets/responses in `finally`;
- response status and redirects are validated before yielding;
- a single framed event and the total stream are independently bounded;
- timeouts and TLS/network failures use the existing `TransportError` taxonomy;
- network-key/profile headers from #28 remain active;
- request and response bodies never enter exception messages.

Implement first for `StdlibHttpsTransport` and `FrontedHttpsTransport`.
Transports that inherently buffer a whole tunneled response, including the
current CDN-WebSocket framing and old plugins, declare streaming unsupported
and fall back to `post_bytes`; they must not silently call raw urllib.

Add an `HttpPeerClient` NDJSON helper that owns framing, UTF-8 decoding, line
limits, and JSON-object validation.

## 6. Adapter changes

Add an optional streaming adapter method whose callback/generator yields text
deltas and returns final usage metadata. For OpenAI-compatible servers:

- send `stream: true`;
- parse `data:` events and `[DONE]` incrementally;
- accept content deltas only from the selected choice;
- bound line size and cumulative output size;
- collect final usage when supplied;
- fall back to conservative token estimates only at finalization;
- close the active HTTP response on cancellation;
- never log raw frames or deltas.

If the runtime claims streaming but returns ordinary JSON, treat it as one
complete delta plus final result when safely parseable; otherwise fall back on
a future request, never reinterpret an unbounded body.

## 7. Consumer node event broker

Create a bounded process-memory broker keyed by task ID. It tracks:

- public state (`queued`, `running`, terminal);
- next expected sequence;
- current cumulative text up to the provider output limit;
- a small event ring for reconnect within the same process;
- subscribers and a terminal marker.

Expose `GET /api/local/llm/orders/{task_id}/events` as authenticated local SSE.
Suggested event names:

- `state`: body-free state change;
- `delta`: `{sequence, delta}`;
- `complete`: the verified terminal `LLMOrderResult`;
- `error`: stable local error code without prompt/output leakage.

Slow or disconnected subscribers must not block inference or settlement. Bound
subscriber queues and let a reconnect receive the in-memory cumulative
snapshot plus later deltas. After a node restart, the existing status endpoint
remains the recovery path for a retained terminal result.

Relay and P2P orders publish only state plus one terminal `complete` event, so
the Webapp consumes one interface regardless of delivery mode.

## 8. Settlement and failure recovery

- Hold balance exactly once before delivery, as today.
- Provider computes final metering once.
- Consumer settles only after verifying the final envelope.
- Provider earns only from the existing idempotent settlement message.
- Never calculate amount from received delta count or text length.

If the direct connection drops after partial output, retry retrieval using the
same task ID. An already completed Provider returns its stored final encrypted
response without rerunning inference. If no final result can be recovered,
mark the task failed, release the hold, and mark the partial UI output as
incomplete rather than presenting it as a successful answer.

## 9. Webapp changes

Files:

- `webapp/src/domain/nodeClient.ts`
- `webapp/src/domain/liveNodeClient.ts`
- `webapp/src/domain/fixtureNodeClient.ts`
- `webapp/src/screens/PrivateAIChat.tsx`
- `webapp/src/screens/PrivateAIChat.module.css`

After async submission returns a task ID, subscribe to the local event stream.
Create an in-memory assistant message with `status: streaming`, append deltas
by sequence, and announce progress without making screen readers repeat the
entire answer. On final verification, replace it with `complete` and persist
the conversation once. On fallback, retain the current thinking state until
the one complete event arrives.

Stop closes the UI subscription and calls the existing cancellation endpoint;
Provider cancellation remains best effort.

### 9.1 Implementation sequence

1. Approve `stream-v1` event and fallback protocol.
2. Add adapter incremental parsing and cancellation tests.
3. Extend the #28 Transport seam with bounded incremental POST responses.
4. Add Provider stream endpoint and authenticated encrypted delta envelopes.
5. Add the Consumer in-memory event broker and local SSE endpoint.
6. Integrate Webapp incremental rendering and terminal-only persistence.
7. Verify settlement, recovery, fallback, privacy, and full regression paths.

## 10. Test plan

### Adapter and protocol

- fragmented SSE lines, comments, `[DONE]`, ordinary JSON fallback;
- Unicode split across network chunks;
- oversized event and cumulative-output rejection;
- cancellation closes the active response;
- event signature, recipient, task, service, sequence, and expiry rejection;
- no prompt/delta text in errors, logs, or task metadata.

### Transport and routes

- exact network-key/profile headers on incremental POST;
- redirect, timeout, TLS, status, per-event, and total-size failures;
- unsupported Transport cleanly uses complete-message delivery;
- direct stream success plus Relay/P2P complete-message fallback;
- dropped connection recovers the final result idempotently;
- final settlement and Provider earning occur exactly once.

### Webapp

- first delta replaces the thinking indicator promptly;
- ordered deltas render one assistant answer;
- duplicate/out-of-order events do not corrupt the answer;
- partial text is not saved to IndexedDB before terminal completion;
- cancellation, incomplete-stream failure, reconnect, and whole-message
  fallback states are visible;
- existing non-streaming chat tests continue to pass.

## 11. Acceptance criteria

- [ ] A compatible direct Provider visibly delivers the first authenticated
  delta before the full generation completes.
- [ ] Browser traffic terminates at the Consumer node and only the authenticated
  local SSE route reaches the Webapp.
- [ ] Every delta is bounded, signed, encrypted, tied to task/service/recipient,
  and accepted only in monotonically increasing sequence.
- [ ] Relay, strict P2P, unsupported Transport, and non-streaming models
  complete normally through one whole-message terminal event.
- [ ] Partial plaintext exists only in bounded process/UI memory and is not
  persisted before a verified terminal response.
- [ ] No prompt, delta, or output body enters Registry, Relay metadata, task
  metadata, logs, crash text, or exception messages.
- [ ] Cancellation closes active responses where possible and never presents
  partial text as success.
- [ ] Dropped, duplicate, out-of-order, malformed, oversized, expired, or
  wrong-signer streams fail closed.
- [ ] Recovery with the same task ID does not rerun completed inference or
  create a second debit/earning.
- [ ] Only verified final metering settles once and Provider earning is applied
  exactly once.
- [ ] Direct streaming E2E, direct non-streaming, strict P2P, encrypted Relay,
  focused tests, full tests, ruff, Webapp tests, typecheck, and build pass.

### Required acceptance evidence

- Record timestamps for submission, first delta, terminal response, and total
  generation to prove the UI did not merely animate a buffered final body.
- Attach sanitized direct streaming and whole-message fallback E2E reports.
- Attach fault-injection results for duplicate, gap, reorder, disconnect,
  cancellation, event limit, and total limit.
- Attach ledger evidence showing one hold, one settlement/release outcome, and
  one Provider earning for the accepted task ID.
- Use unique secret markers to prove prompts/deltas are absent from Registry,
  task files, logs, errors, and retained metadata.
- Record protocol version approval and the implementation commit.

## 12. Readiness assessment

**Can development start on upstream `main`: not cleanly yet.** #28 is a hard
dependency because upstream still bypasses the Transport seam for LLM POSTs.
Development may start immediately on top of local commit `933c312`, but the
preferred sequence is to review and merge #28 first. The streaming protocol
above also needs maintainer approval because it extends the peer wire contract.
The remaining adapter, broker, and Webapp foundations are sufficient once those
two gates are cleared.
