# Local LLM + dual-node development handoff

Last updated: 2026-08-25 (Asia/Hong_Kong)

## Read this first

This document is the handoff entry point for developers and AI agents working
on the local-LLM service package and dual-node order flow.

- Active branch: `feature/local-llm-dual-node`.
- Base commit: `f631682` from `test/issue-15-for-you`.
- Main implementation checkpoint: `74ca0ca` (with build/protocol hardening in
  `a6e1567`).
- Status: work in progress. Do **not** claim final P0 completion.
- Isolated Compose direct/relay, local real-model, async-order and cancellation
  paths pass. The additional strict public-internet NAT-hole-punch test between
  the physical Windows Consumer and Provider is still pending its remote run.
- No private network key, model path, API key, prompt, or model response belongs
  in Git, Registry records, control-plane logs, or ordinary node logs.

## Current topology

```text
Remote Windows Consumer
  Webapp http://127.0.0.1:8792/services
  packaged Ryn node
       |
       | public Registry/rendezvous: discovery + signed ICE metadata only
       | task payload relay disabled in strict acceptance mode
       v
Local Windows Provider Ryn :18894
       |
       | loopback OpenAI-compatible API
       v
llama.cpp :8080 + real owner-managed GGUF model
```

The official/control server may provide discovery and rendezvous signaling,
but must never receive prompt or response bodies. In strict public acceptance,
task relay fallback is removed and only server-reflexive ICE candidates are
accepted. The Provider and its model runtime necessarily see plaintext during
inference; this is not confidential computing.

## Implemented scope

### Local LLM package

- Manifest/config with package/protocol versions, adapter/runtime, public alias,
  capabilities, context/output/concurrency/timeouts, hardware, pricing,
  privacy/logging, health, lifecycle, checksum/source and license notice.
- Common adapter behavior for health, models/capabilities, inference, cancel,
  metrics and shutdown.
- Existing OpenAI-compatible API support, including loopback-only defaults,
  model discovery, health, streaming probe and real inference self-test.
- Basic Ollama compatibility through its OpenAI-compatible surface.
- Managed llama.cpp + GGUF installation with download checksum, startup,
  self-test and separated environment/model removal.
- Read-only GGUF import with validation, fingerprinting and no copy/upload.
- OS/CPU/RAM/disk/NVIDIA/driver/Docker detection and conservative
  recommendations.

### Publication, ordering and settlement

- Provider publishes sanitized capability, capacity, price, online state,
  benchmark, license/risk, content and privacy metadata.
- Model paths, filenames, local API URLs and key names are excluded from public
  records.
- Consumer discovery, signed/encrypted task creation, Provider execution,
  encrypted response and body-free measurement/settlement metadata.
- Stable task IDs, idempotent task execution, single settlement, bounded
  capacity, terminal failure/cancel states and temporary ciphertext cleanup.
- Development-only `DEV_TASK_BALANCE`, separate from reputation Credits; hold,
  settle or release is idempotent and billing records contain no bodies.
- Startup reconciliation releases stranded Consumer holds after an interrupted
  process.
- Settlement checkpoints are crash-safe and idempotent; the Provider retries
  body-free settlement acknowledgement through the Registry when necessary.

### P2P and Webapp

- ICE/UDP node-to-node exchange with encrypted task envelopes.
- `RYNMESH_P2P_REQUIRE_PUBLIC=1` filters out host/private candidates.
- Packaged public Consumer forces strict P2P and removes task-relay fallback.
- Webapp Services page supports all four Provider setup profiles, explicit
  self-test, manual publish/pause, discovery, service selection, prompt and
  max-token input, asynchronous ordering, progress, cancellation, result and
  Task Balance display.
- Consumer history persists body-free metadata only. Prompts are never stored;
  returned results use encrypted retention choices of 0, 1 hour, 24 hours or 7
  days and terminal history can be cleared.
- Latest source immediately returns a task ID, shows queued/connecting/running
  progress, records `created -> accepted -> running -> terminal`, exposes a
  sanitized failure reason and refreshes released balance. The rebuilt
  executable and adjacent strict configuration are packaged locally but have
  not yet run on the remote Windows machine.

## Key files

| Area | Files |
|---|---|
| Package interfaces and config | `rynmesh/llm_package/manifest.py`, `adapters.py` |
| Hardware and lifecycle | `rynmesh/llm_package/hardware.py`, `lifecycle.py`, `cli.py` |
| Encryption, P2P and settlement | `task_protocol.py`, `p2p.py`, `task_balance.py` |
| Node API and order orchestration | `rynmesh/llm_package/routes.py`, `rynmesh/peer_http.py` |
| Provider publication | `rynmesh/services/llm.py` and related service/store/registry changes |
| Consumer Webapp | `webapp/src/screens/Services.tsx`, domain client files |
| Isolated E2E | `deploy/llm-e2e/`, `scripts/llm_e2e.py` |
| Automated tests | `tests/test_llm_package.py`, `tests/test_services.py`, `tests/test_transport.py` |
| Detailed design/runbook | `LOCAL_LLM_SERVICE_MVP.md`, `LOCAL_LLM_RUNBOOK.md` |
| Acceptance evidence | `LOCAL_LLM_P0_EVIDENCE.md`, `REAL_LLM_VALIDATION.md` |

## Current verification

- Focused Python suite:
  `python -m pytest tests/test_llm_package.py tests/test_services.py tests/test_transport.py -q`
  -> 39 passed.
- Webapp: `npm test` -> 21 passed.
- Webapp: `npm run lint` and `npm run build` -> passed.
- `git diff --check` -> no content errors; Windows reports expected LF/CRLF
  conversion warnings.
- Full Windows Python suite -> 485 passed, 13 failed, 3 skipped. The remaining
  failures are Windows locale/POSIX-mode/WSL/subprocess-select categories in
  areas not changed for this feature; see `LOCAL_LLM_P0_EVIDENCE.md`.
- Real llama.cpp/OpenAI-compatible health remains available on Provider
  loopback and previous real inference evidence is recorded in
  `REAL_LLM_VALIDATION.md`.

## Strict public P2P result and remaining gate

The remote Consumer discovered the local Provider and created a strict order.
Both sides published only server-reflexive STUN candidates and explicitly
forbade relay. In that attempt the two machines reported the same public egress
with different UDP mappings; no nominated UDP pair formed before timeout. The order
failed closed, Provider inference did not run, the Consumer hold was released,
and the relay persisted-file count remained unchanged.

That historical attempt is not a successful public P2P acceptance. The current
machines now report different public egress addresses, the real-model Provider
is published in fail-closed strict mode, and the rebuilt Consumer package is
ready. The remaining blocker is operational: the existing RDP window belongs
to a disconnected local Windows session, so it cannot currently be captured or
controlled for deployment. Do not enable payload relay to make this acceptance
appear successful.

## Next actions

1. Reconnect the local Windows session that owns the RDP window and keep the
   remote desktop unlocked.
2. Copy the prepared Consumer package, confirm execution at action time, start
   it and verify the new progress/error UI plus `available=100.0`, `held=0.0`.
3. Submit a real prompt from the remote Consumer.
4. Record the nominated public ICE pair, `relay_used=false`, Provider real-model
   invocation, returned output, terminal order history, single settlement and
   unchanged relay storage count.
5. Update both evidence documents. Only then reconsider strict public P2P and
   overall P0 completion status.

## Common commands

```bash
python -m pytest tests/test_llm_package.py tests/test_services.py tests/test_transport.py -q
cd webapp && npm test && npm run lint && npm run build
python scripts/llm_e2e.py run
python scripts/llm_e2e.py down
pyinstaller --noconfirm deploy/llm-e2e/windows-consumer/RynmeshPublicConsumer.spec
```

## Rules for subsequent agents

- Preserve the independent worktree and this feature branch.
- Never log or commit prompt/response bodies, secrets, private model paths or
  owner filenames.
- Do not describe encrypted relay success as P2P-direct success.
- Do not weaken strict public-candidate or no-relay settings for the pending
  acceptance test.
- Do not mark the goal/P0 complete while the strict physical public test and
  remote deployment verification remain outstanding.
- Keep environment removal and model deletion separate; never delete imported
  user models.
