# Private AI direct streaming (#23)

Baseline: upstream `f61c075` (after v0.7.0). This change is in development;
it is not a release or an issue-completion claim.

## User behavior

Ask Ryn shows a partial answer while a compatible friend's model is generating
over direct HTTP. The preview is transient. The node's existing Ask run archives
the verified final result, and that saved answer replaces the preview. Stop
continues to use the existing node-owned cancellation flow. Closing the page or
changing conversations only closes the subscription; it does not cancel or
resubmit the order. A failed subscription retries the same task up to three
times, then leaves history polling active and labels the preview incomplete.

Local runtime calls, relay, strict P2P, older providers, and transports without
incremental POST retain whole-response delivery. A runtime answering a streaming
request with ordinary JSON produces one preview without a second inference.

## Protocol and implementation

- Ask runs request `response_mode: stream-v1` through the existing consumer
  commands. Generic callers omitting it retain `complete-v1`.
- Provider status and the authenticated, encrypted friend service catalog carry
  `delivery_protocols`. Streaming requires adapter support, provider support,
  an incremental Transport and an effective direct/auto route.
- `POST /api/peer/llm/tasks/stream` returns NDJSON. Each delta is signed and
  sealed using the existing task envelope and binds task, service, sender,
  recipient, expiry, stream version and a contiguous sequence starting at zero.
- `ProviderService.handle_stream` delegates admission and inference to the
  ordinary `handle` lifecycle. Friend ACL/version checks, active permission
  revocation, duplicate claims, capacity and final result retention are shared.
  Structured Ask Ryn system/user roles also reach the streaming adapter.
- The consumer verifies every delta before publishing a local preview. Final
  `llm_response` validation and the existing hold/settle/release lifecycle remain
  authoritative. No per-chunk charging is introduced.
- `GET /api/local/llm/orders/{task_id}/events?after_sequence=N` uses the existing
  owner authentication and sends `state`, `delta`, `complete`, `error` and
  `heartbeat` SSE events. `snapshot: true` replaces a preview after an event-ring
  gap. Subscriptions expire after 60 seconds and also reconcile durable order
  state, so a node restart or evicted buffer cannot cause an endless wait.
- The browser connects only to its local node using the authenticated session
  cookie. Partial text is never written into conversation storage. Completion
  events contain state metadata; final answers use the established archive.
- Stream buffers are forgotten on archive acknowledgement and explicit result
  erasure/clearing. No partial output is persisted in task records, registry,
  relay or logs.

## Bounds and recovery

Peer request: 2 MiB. Wire event: 256 KiB. Total direct response: 16 MiB.
Partial output: 128 KiB UTF-8; final sealed envelope is separately size checked.
Provider queue: 32 sealed events. Provider stream workers: inference capacity
plus one. Local subscribers: 16. Consumer previews: 64 tasks, 256 events each,
plus bounded snapshots. Terminal previews expire logically after 60 seconds;
expiry is pruned on broker access. Active preview expiry is one hour.

EOF without a runtime completion marker is a failure, not a successful partial
answer. Runtime exceptions expose stable codes. Connection/stream errors never
cause the browser to place a new order. An auto route may use the existing relay
fallback with the same signed request and task identity; explicit direct and
strict P2P retain their existing failure boundaries. Consumer process restart
does not resume token generation and never creates a replacement task.

## Automated evidence

- `tests/test_llm_streaming.py`: fragmented UTF-8/NDJSON, transport auth and
  byte bounds, signatures/order/expiry, bounded replay, actual runtime SSE,
  first-delta-before-completion, cancellation, duplicate task and settlement.
- `tests/test_llm_streaming_http.py`: two real TCP nodes and a gated deterministic
  HTTP model. Performs real invitation/join, friend AI grant/catalog, Ask run,
  stream read, disconnect/reconnect and archive. Tests retention 0 and 3600,
  owner auth, preserved structured prompt roles, one model call, one settlement,
  and no prompt marker in persisted JSON or logs. This is protocol evidence,
  not real-model/GPU or cross-NAT acceptance.
- `tests/test_ai_access_provider.py`: existing permission/replay and in-flight
  revocation scenarios run with both whole-response and streaming delivery.
- `tests/test_ai_catalog.py`: stream negotiation is confined to an authorized
  friendship and older catalogs safely omit the capability.
- `webapp/src/domain/useLLMStream.test.tsx`: contiguous deltas, same-task replay,
  snapshot replacement, retry/output bounds, gaps and conversation isolation.
- `webapp/src/screens/PrivateAIChat.test.tsx`: a reopened live task displays a
  transient partial answer without saving it or submitting new work; the node
  archive then replaces it. Existing cancel and timeout recovery tests remain.

Local verification on 2026-09-17, Windows, Python 3.12.10 and Node 22.22.1:

- `python -m pytest tests/ -q`: 1581 passed, 29 skipped (361.43 seconds).
- `python -m ruff check rynmesh/ tests/`: passed.
- `npm test`: 327 passed across 53 files. After the final subscription timer
  changes, the 18 affected hook/chat tests passed again.
- `npm run build` (including `tsc -b`): passed after the final changes.
- Built a wheel, installed it into an isolated package directory, and confirmed
  both nodes imported the installed package. The browser loaded the node-served
  bundled UI without Vite. No additional runtime dependency was added.
- Two fresh loopback nodes and a deterministic HTTP runtime: the browser showed
  a partial-answer label before completion, recovered the preview after reload,
  displayed the archived final answer with final metering, and completed the
  existing Stop flow. A separate timeout run restored input without resubmitting.
  The successful fixture delayed its final event by 10 seconds; this is an
  intentional test delay, not a real-model latency measurement.

The screenshot below captures only status controls, excluding the question,
answer, node identifiers and conversation title as required by the testing
strategy. CI, maintainer review and real-machine acceptance remain separate gates.

![Final metering and completed-answer controls](completed-status.png)

## Manual acceptance

1. Use two fresh nodes running this branch. Pair them and explicitly grant the
   consumer access to a streaming OpenAI-compatible model on the provider.
2. Open Ask Ryn on the consumer and select that friend model. Ask for a long
   answer. Confirm visible text arrives while the provider task is running,
   with a partial-answer label and Stop available.
3. Reload the page while generating. It must observe the same task and recover
   the preview or continue checking history, without creating another order.
4. Cancel another long task. Confirm the existing cancellation message and
   terminal state; do not infer that remote compute stopped immediately.
5. Revoke the friend's AI permission during generation. Check that admission
   of a new request fails and the existing task drains through cancellation.
6. Complete a request, restart the consumer, and verify the saved answer and
   one settlement. Confirm partial text is absent from plaintext files/logs.
7. Repeat with a non-streaming provider or a relay/P2P route and verify normal
   whole-response completion. Record only timings, counts and safe error codes.

Not covered by the deterministic test: real model speed, macOS desktop behavior,
cross-NAT reachability, relay/P2P token streaming, tool calls or reasoning output.
