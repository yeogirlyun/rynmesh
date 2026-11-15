# Recovering a stream that ends before completion

Date: 2026-11-15 (America/New_York). Isolates Ziling source commit `ef57371b` from the larger Agent overhaul.

Chat, Responses and Anthropic SSE streams that reach EOF without a valid completion are classified as `upstream_stream_truncated`, mapped to `agent_model_stream_interrupted`, and handled by the existing bounded retry policy. Failed usage remains accounted; completed streamed reads keep their receipts. Partial/malformed proposals do not authorize writes.

This does not take the overhaul's new idle-timeout implementation, unbounded reconnection policy, automatic acknowledgment of uncertain writes, or other lifecycle changes. The current task/model budgets and unknown-write reconciliation remain in force. The false idle timeout and logical-write deduplication remain assigned to Ziling.

Local verification: **122 passed** across dedicated truncated-stream tests, native protocol transport, adapter failure stages, rejected usage, connection recovery and request deadlines. These use controlled local services, not paid models or production APIs. Ruff/diff checks passed. Focused CI includes the stream cases. No application install or deployment.
