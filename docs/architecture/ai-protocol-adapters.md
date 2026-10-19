# AI source protocol adapters

Ryn uses one internal chat and client-tool contract. A source chooses its upstream
wire protocol independently from its vendor or model name. Existing sources that
omit `protocol` retain `openai_chat`.

| Source protocol | Appended inference path | Authentication | Tool arguments on the wire |
| --- | --- | --- | --- |
| `openai_chat` | `/chat/completions` | Bearer | JSON string |
| `openai_responses` | `/responses` | Bearer | JSON string |
| `anthropic_messages` | `/messages` | `x-api-key`, `anthropic-version` | JSON object |

The configured root includes its API version, for example
`https://api.anthropic.com/v1`. Vendor-compatible roots may have other prefixes.
Changing protocol invalidates the same model bindings and probe evidence as
changing the source endpoint. Ryn never guesses another endpoint or repeats a
failed request through another protocol.

## Execution and continuation

`protocol_codecs.py` converts requests, responses and native SSE events.
`protocol_adapter.py` owns HTTP, cancellation, bounds, deadlines and usage.
The original Chat adapter and CLI adapters remain separate transports.
`api_formats.py` serves external clients of Ryn's local API; it is not the
upstream protocol adapter.

All upstream calls become the same internal assistant `tool_calls`; results
reference the original call ID. Only Ryn's router and execution policy authorize
and run tools. Multiple Messages results are grouped into the next user message.
Host-authored tool errors can carry `is_error`; ordinary tool text is never parsed
to guess this flag. Chat and CLI transports remove that internal flag.

Native private continuation is stored as bounded `provider_state` in encrypted
task history. It is excluded from public task responses, UI text, completion
events, summaries and checkpoints. A scope binds it to the protocol, endpoint,
credentials and model. Replaying it also requires the original public text and
tool identities/arguments to agree with the current canonical message.
Responses uses `store: false` with encrypted reasoning continuation. Optional
tool parameters retain their original schema; Responses tools default to
`strict: false` unless the definition explicitly requests strict validation.

The native codecs support text, validated inline user images and client tools.
Unsupported request features and server-side tools are rejected explicitly.
Configuring Claude thinking budgets/adaptive modes is not implemented; received
native thinking/signature blocks are kept privately for lossless continuation.
Responses stop sequences and Claude reasoning-effort settings are rejected.

## Terminal states and failures

Native tools are released only after a complete successful response. A block
stop, balanced argument JSON or socket EOF does not complete a tool proposal.
Truncated tools, unsupported stop states, broken streams and malformed native
envelopes produce `source_protocol_response_invalid`; unsupported options produce
`source_protocol_parameters_unsupported`. The Agent does not retry these errors
as model-format correction requests. Reported usage survives failures and
cancellation after a complete response body is received.

When a server buffers a requested stream, the same JSON response is consumed
once. Local API stream projections emit its actual final content. Tool names
and late IDs are assembled before emitting a target protocol's tool start event.

## Verification

The focused suites cover codecs, real loopback HTTP transport, source identity,
private state, public stream projections, and the HTTP-to-AgentRuntime failure
path. `test_native_protocol_retry.py` verifies one request and zero executed tools
after truncation or unsupported stop states, with measured usage retained.

The 2026-10-18 live acceptance used the configured GLM source through Z.ai's
Chat and Anthropic-compatible endpoints, with real local file writes and reads,
two tool-result continuations and an initially unknown host-generated nonce.
This verifies Anthropic wire compatibility; it is not a Claude-model acceptance.
Native OpenAI Responses requires a configured compatible API source for a live
provider acceptance run.
