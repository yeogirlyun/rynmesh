# Streaming harness repair — 2026-11-10

Reviewed base: develop `de74e816548a3a3c44af1feafd41b03d6e641ced`.

## Reproduced cause

The [PR #124 LLM job](https://github.com/yeogirlyun/rynmesh/actions/runs/42823539592/job/129854847364) failed in `local-run` with no discovered service, a failed task, no deltas and no settlement. The same failure was reproduced locally with four isolated processes and fresh temporary node directories.

The streaming verifier had not been updated for three existing contracts:

1. Shared model discovery and orders require a friend relationship and an explicit per-model grant. The normal Docker LLM verifier already established them; the streaming verifier did neither and omitted `ai_permission` from its orders.
2. Provider discovery advertises streaming from the manifest's declared capabilities. The old deterministic fixture only declared text generation, so successful authorization still selected complete-response delivery.
3. SSE completion is body-free. The private final answer must be retrieved from the authenticated order-result endpoint. There can be a short interval between the terminal event and publication of that one-shot answer.

## Scoped repair

- Reuse the existing pairing/grant helper with explicit provider/consumer URLs, supporting both Docker's fixed endpoints and local ephemeral ports.
- Supply the returned permission on initial and duplicate submissions. Stop before submitting any order if authorization or discovery fails.
- Give the streaming profile a streaming-capable deterministic manifest. Preserve the complete-only fixture for fallback and the existing complete-response profiles.
- Read the result through the existing private endpoint, with a bounded wait for worker publication. This does not resubmit inference or persist the answer. Preserve SSE timing evidence separately.
- Set test-only loopback friend endpoints and the matching local-control fixture credential explicitly for child processes. Production permissions and transport/runtime code are unchanged.

## Local verification

macOS arm64, Python 3.13.2; all execution used the local repository/workstation.

| Check | Result |
| --- | --- |
| Unmodified base, four-process local streaming | Reproduced zero services and failed task |
| Repaired local streaming | One authorized service; stream-v1; three deltas before terminal; successful private result |
| Repaired local fallback | One authorized service; complete-v1 only; no deltas; successful private result |
| Duplicate submission | Same terminal task, one consumer/provider task record |
| Accounting evidence, both modes | One hold, one settlement and one provider earning per task |
| Privacy and shutdown, both modes | Prompt/output absent from checked logs and persistent files; all four owned processes stopped |
| Focused Python regression suite | 44 passed; two existing websockets deprecation warnings |
| Changed Python files | Ruff and `git diff --check` passed |

Focused suite:

```sh
python -m pytest -q tests/test_llm_stream_e2e_script.py tests/test_llm_e2e_script.py \
  tests/test_llm_streaming.py tests/test_llm_streaming_http.py tests/test_ai_access_peer_routes.py
python scripts/llm_stream_e2e.py local-run
```

Regression coverage includes default/ephemeral endpoints, permission propagation, metadata-only SSE completion, delayed one-shot result availability, and refusal to submit after authorization denial or discovery timeout. Existing streaming, route, privacy and exactly-once assertions remain active.

The model is deterministic: this proves protocol integration, not model quality, GPU performance or cross-NAT connectivity. The local Docker daemon was unavailable and was not started; Docker-only profiles require their separate CI result. This repair does not establish that the full develop suite or Linux packaging is green, publish an update, change installed nodes, or resolve the larger continuation/employee-workflow assignments.
