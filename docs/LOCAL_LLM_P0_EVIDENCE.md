# Local LLM P0 acceptance evidence

Validated on 2026-08-24 in branch `codex/local-llm-dual-node`. “Pass” below
means an implementation plus automated or recorded runtime evidence exists.
The deterministic adapter is used only for repeatable protocol automation;
non-mock evidence is recorded separately in `REAL_LLM_VALIDATION.md`.

> **Checkpoint status — 2026-08-25:** this branch is reviewable work in
> progress, not a final P0 sign-off.  The original isolated-Compose and local
> real-model scenarios below pass, but the subsequently requested strict
> public-internet NAT-hole-punch acceptance between the physical Windows
> Consumer and Provider has not succeeded.  Strict mode rejected host
> candidates, disabled relay fallback, and failed closed after the two STUN
> mappings proved unable to form a nominated UDP pair.  A different public
> egress (for example a phone hotspot on one machine) is required for the next
> acceptance run.  The older encrypted-relay run is retained as security
> evidence only and is **not** counted as P2P-direct success.

| # | Result | Evidence |
|---:|:---:|---|
| 1 | Pass | `rynmesh-llm setup` is the single wizard entry; managed/import users write no Python, CUDA, Docker, or server command. |
| 2 | Pass | `hardware.detect_hardware` reports OS/CPU/RAM/disk/NVIDIA memory/driver/Docker. Actual report captured Windows, 8 CPUs, 32,653 MiB RAM, Quadro P2200 5,120 MiB and Docker; missing capabilities produce readable warnings. |
| 3 | Pass | `hardware.recommend` uses 75% available RAM, 85% free VRAM, actual disk bounds, conservative context/concurrency, and returns no unsafe default. |
| 4 | Pass | Managed Qwen2.5 0.5B Q4_K_M download verified official LFS SHA-256, started llama.cpp, and completed a real self-test in 171 ms. |
| 5 | Pass | GGUF magic/readability/size/hardware/fingerprint validation plus actual owner model mounted read-only; no copy/upload/rewrite, and post-test existence verified. |
| 6 | Pass | OpenAI-compatible `/v1/models` health, `/v1/chat/completions` inference, and streaming probe passed in unit integration and against a real local 4B GGUF runtime (`streaming=true`). |
| 7 | Pass | Actual `status`, `stop`, `start`, `restart` (through update), `update`, self-test, and safe uninstall passed. |
| 8 | Pass | Default environment uninstall reported `MODEL_AFTER_ENV_UNINSTALL=True`; separate confirmed delete reported `False`; imported-model delete was refused and the user file remained. |
| 9 | Pass | `ProviderService.publish` advertises capability, context, benchmark, price/bounds, online health, capacity/concurrency/queue, alias/license/risk/content/privacy policy; configured Providers publish on startup and refresh every 30 seconds, with a WebApp manual retry. |
| 10 | Pass | `test_manifest_public_view_has_no_paths_urls_or_key_names` proves the public record excludes URL, API-key reference, runtime command, absolute path, and filename. |
| 11 | Pass | Publish refuses unhealthy services; every order rechecks adapter health; full slots return explicit `capacity_exhausted`. |
| 12 | Pass | Imported/private models publish only owner alias plus full local SHA-256 fingerprint; documentation explicitly limits what that fingerprint proves. |
| 13 | Pass | Compose Provider and Consumer use different identities, ports, config and Docker volumes and establish Ryn discovery/connectivity. |
| 14 | Pass | Consumer discovered one Provider service through the registry in deterministic, relay, and real runs. |
| 15 | Pass | Consumer local Ryn API created a stable task, froze balance, signed/encrypted the prompt, and submitted it. |
| 16 | Pass | Provider invoked an owner-managed real 4B GGUF llama.cpp backend; Chrome WebApp acceptance succeeded (26 input, 10 output tokens, 5,920 ms, output `RYNMESH_TWO_NODE_E2E_OK`). |
| 17 | Pass | Consumer calls only Provider Ryn port. In Compose it is not attached to `provider-runtime`; the model service has no Consumer/host port in isolated profiles. |
| 18 | Pass | Provider evidence records `created -> accepted -> running -> succeeded`; failed, timed-out, cancelled and rejected are terminal states. |
| 19 | Pass | Bounded semaphore plus `reject_when_full`; automated capacity test proves no inference call is dropped or silently accepted. |
| 20 | Pass | Terminal encrypted response cache executes duplicate task once; hold/settlement/earning IDs are idempotent; released holds can be safely re-held for reconnect recovery. |
| 21 | Pass | Runtime scan found zero validation-prompt plaintext matches in Registry, Relay, Provider and Consumer persisted data. Registry sees sanitized capacity, encrypted blob hashes and body-free settlements only. |
| 22 | Pass | Forced-relay Compose run succeeded through a separately operated relay. Relay persisted ciphertext; plaintext scan count was zero and signature/ciphertext tamper test fails authentication. |
| 23 | Pass | Body logging defaults false; Provider stores ciphertext/metadata only. `RYNMESH_LLM_DEBUG_BODIES=1` is the sole opt-in and emits a warning. |
| 24 | Pass | Request/response temp ciphertext is deleted in `finally`; container scan found zero `*.ciphertext` temp files. Unit test also proves persisted JSON contains neither prompt nor output. |
| 25 | Pass | Design/runbook state that Provider/runtime sees plaintext, Python memory cannot guarantee erasure, and Rynmesh is not confidential computing or “absolute privacy.” |
| 26 | Pass | `credits.py` is unchanged reputation Credits; `task_balance.py` uses explicit `rynmesh-dev-task-balance-v1` / `DEV_TASK_BALANCE`. README/runbook keep names and claims separate. |
| 27 | Pass | Consumer freezes before send, settles successful actual amount, refunds excess, and releases on failure/cancel. Provider earns only after signed acknowledgement. |
| 28 | Pass | Automated repeated settle/earn returns the same record; duplicate task reuses encrypted result and does not rerun inference or debit again. |
| 29 | Pass | Billing has task/service/node IDs, input/output tokens, duration, currency and amount; forbidden body/secret fields are rejected and tests scan events for prompt absence. |
| 30 | Pass | Ledger state and every user-facing document say development-only/simulated; no real payment backend is claimed. |
| 31 | Pass | `python scripts/llm_e2e.py run` starts Registry, separate Relay, Provider, Consumer and test runtime; real variants are one command after model/runtime selection. |
| 32 | Pass | Automated direct and forced-relay E2E both completed publish, discover, order, encrypted inference, result and settlement. |
| 33 | Pass | `REAL_LLM_VALIDATION.md` records reproducible, desensitized non-mock connection, managed, import and real two-node inference hashes/usage. |
| 34 | Pass | `down` stops; `clean` removes only the named E2E containers/networks/volumes and never deletes a host model. Managed/import cleanup behavior was separately verified. |
| 35 | Pass | Current focused LLM/services/transport suite: `31 passed`; Webapp: `17 passed`. Current Windows full suite: `477 passed / 13 failed / 3 skipped`; remaining failures are platform-only categories outside this task. |
| 36 | Pass | `ruff` passes; TypeScript/Vite production build passes; Vitest 17/17 passes. Linux full pytest from the Windows checkout: 484 pass, 2 pre-existing CRLF shell-syntax failures, 3 skip. |
| 37 | Pass | README plus design, runbook, real-validation and this evidence file cover all three modes, topology/demo, privacy/payment boundaries and troubleshooting. |
| 38 | Pass | Work began from clean committed `f631682` in a separate worktree/branch. Diff is limited to the package, node route integration, E2E, docs, entry point, ignore and focused tests; no original-worktree files were read/overwritten. |

## Verification commands

```bash
python -m pytest -q tests/test_llm_package.py tests/test_services.py \
  tests/test_peer_messaging_http.py tests/test_credit_policy.py tests/test_consumption.py
python -m ruff check rynmesh tests qa scripts
cd webapp && npm run build && npm test -- --run
python scripts/llm_e2e.py run
python scripts/llm_e2e.py relay-run
python scripts/llm_e2e.py host-real-run
python scripts/llm_e2e.py clean
```

## Known baseline/platform failures

The 13 Windows full-suite failures are unchanged baseline categories outside
this task: locale-default GBK decoding of UTF-8 examples/docs, POSIX executable
and `0600` mode assertions on NTFS, missing WSL bash, Windows `select()` on a
subprocess pipe. A
Linux container run removes those Windows-only categories but retains two
pre-existing CRLF shell syntax failures because it mounts the Windows checkout.
No LLM package, task, settlement, relay, Compose, ruff, TypeScript, or Vitest
check fails.

## Current review checkpoint

- Branch: `codex/local-llm-dual-node`.
- Strict public transport is fail-closed: public ICE candidates only; no host
  candidate and no task-relay fallback.
- The physical remote Consumer discovered the Provider and created orders, but
  strict UDP nomination timed out. Relay persisted-file count remained
  unchanged, so no prompt/output payload was silently rerouted through it.
- A UX fix now immediately shows `Connecting P2P...`, preserves
  `created -> accepted -> running -> terminal` Consumer history, exposes a
  sanitized failure reason, and refreshes released Task Balance. It is built
  and tested but still needs deployment to the remote Windows node.
- This checkpoint must not be described as completed public P2P acceptance.

## P1 intentionally deferred

Ollama-specific performance tuning (basic Ollama support is present), streaming
token delivery, hard mid-request backend interruption, AMD/Apple-Silicon
acceleration, more formats/engines, dynamic pricing, signed release packages,
production payment, disputes and chargebacks.
