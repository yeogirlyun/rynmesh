# Native-model direct streaming follow-up

Windows / Python 3.12.10; the installed wheel from the PR #60 implementation
served its bundled web UI. Two fresh loopback nodes used real invitation/join,
explicit friend AI permission and the authenticated friend service catalog.
The provider used the existing local GGUF model through native llama.cpp b10774,
four CPU threads, 4096 context, no GPU offload. This is actual model inference,
not the deterministic HTTP model in the original protocol test.

## Observed

- The first valid request showed Stop generating, then “Generating · partial
  answer” while inference was active. The partial label was observed 5.65 seconds
  after the browser's confirmation click; this is an observation upper bound,
  not an instrumented first-token latency measurement or benchmark.
- Two requests reached succeeded, with saved assistant answers of 1088 and 1158
  characters. Each displayed 0.001 final credits in its stored message. A page
  reload retained the saved answers and did not create a replacement order.
- A further request visibly reached partial generation. Pressing Stop generating
  led to the node's cancelled state with cancel_requested=true and no final cost
  attached to that assistant message. This proves the application's cancellation
  flow with a real model, not immediate termination of remote CPU work.
- Reload was initiated after a partial answer was observed, but generation finished
  before a recovered partial state could be observed. Therefore this run confirms
  same-task final history after reload; deterministic tests remain the evidence for
  recovery of a still-running preview.
- Scanned 27 JSON files under the two disposable node homes: none contained any
  of the three planted prompt markers. No prompts, answers, invite secrets or
  content screenshots are included in this acceptance record.

## Failures retained in the record

An initial fixture used a model basename instead of the native server's reported
model identifier and failed with service_unhealthy. The fixture was corrected to
discover the reported model; no product code changed for that failure.

During the subsequent session, one request failed with provider_unavailable after
the manually served fixture had been idle. Refreshing the friend service catalog
restored availability for the next explicit request. This fixture starts servers
without their ordinary lifespan workers. The stale-catalog cause has not been
isolated beyond that observation; it is not counted as a successful request or as
proof that normal background refresh always recovers it. No failed order was
automatically resubmitted.

## Remaining limits

This adds real local CPU-model generation and cancellation evidence. macOS
desktop interaction, different physical devices/public egress, GPU performance,
and a complete real-model restart/in-flight-revocation matrix remain unverified.
Final metering here is the app's test-node record, not an external payment.
