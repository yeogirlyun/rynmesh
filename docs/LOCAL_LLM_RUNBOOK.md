# Local LLM package runbook

## Safety and product boundaries

- The local runtime URL, API-key environment-variable name, GGUF path, and
  filename are private node configuration. Discovery exposes an alias and a
  stable SHA-256 fingerprint only.
- API URLs are loopback-only by default. `--allow-non-loopback` is an explicit
  advanced override for an owner-controlled network and displays the risk in
  the command/configuration surface.
- Ryn nodes expose the unified encrypted task protocol. They never proxy the
  raw local model port to a Consumer.
- A Provider and its model runtime see plaintext while computing. Isolation,
  no-body logs, in-memory processing, and cleanup reduce risk; this is not
  confidential computing or an absolute-privacy guarantee.
- The official registry/control plane must never be configured as
  `RYNMESH_LLM_RELAY_URL`. Use a separately operated data-plane relay. It stores
  end-to-end ciphertext only.
- Rynmesh Credits are contribution/reputation. Development Task Balance is a
  separate simulated spend/earn ledger. There is no deposit, withdrawal,
  production payment, chargeback, or dispute system in this MVP.

## Setup wizard

All modes start from `rynmesh-llm`; users do not write Python, CUDA, Docker, or
llama-server commands.

Inspect hardware and conservative recommendations:

```bash
rynmesh-llm detect
```

Managed install downloads the recommended official GGUF, obtains the source's
LFS SHA-256, verifies every downloaded byte, pulls the llama.cpp runtime,
starts it on loopback, and makes a real completion request:

```bash
rynmesh-llm setup --mode managed --package-id local-small --yes
```

Import an existing GGUF in place. It is mounted read-only and is never copied,
uploaded, renamed, or rewritten:

```bash
rynmesh-llm setup --mode import-gguf --package-id my-private-model \
  --alias private-coding-model --model-path /models/private.gguf --yes
```

Connect an existing OpenAI-compatible local API:

```bash
rynmesh-llm setup --mode openai-compatible --package-id existing-local \
  --alias local-model --base-url http://127.0.0.1:8080 --model my-model
```

If it needs a bearer token, put the value in the environment and provide only
the variable name; the value is not written to the manifest or normal logs:

```bash
export MY_LOCAL_LLM_KEY='...'
rynmesh-llm setup --mode openai-compatible --package-id existing-local \
  --alias local-model --base-url http://127.0.0.1:8080 \
  --api-key-env MY_LOCAL_LLM_KEY
```

Ollama uses native model discovery and its OpenAI-compatible inference API:

```bash
rynmesh-llm setup --mode ollama --package-id ollama-local \
  --alias local-ollama --base-url http://127.0.0.1:11434
```

## Lifecycle and removal

```bash
rynmesh-llm status --package-id local-small
rynmesh-llm stop --package-id local-small
rynmesh-llm start --package-id local-small
rynmesh-llm restart --package-id local-small
rynmesh-llm update --package-id local-small
rynmesh-llm self-test --package-id local-small
rynmesh-llm uninstall --package-id local-small
```

Default uninstall removes the managed runtime container but preserves both the
private manifest and model. Managed-model deletion is a second explicit action:

```bash
rynmesh-llm uninstall --package-id local-small \
  --delete-model --confirm-model-delete
```

Rynmesh refuses that deletion for imported/user-owned GGUF files.

## Publish, discover, and order

Start the Provider Ryn node with its private manifest path:

```bash
export RYNMESH_LLM_SERVICE_MANIFEST="$HOME/.rynmesh/llm/packages/local-small/manifest.json"
rynmesh-peer
```

The Provider publishes a sanitized service record on startup and refreshes the
short-lived discovery record every 30 seconds. The WebApp Services page also
shows Provider health/capacity and provides **Publish / refresh service** for an
immediate retry; that action publishes metadata only, never prompts or model
files. The webapp/CLI must supply the node-local token when one is configured. Consumers
discover `rynmesh.llm.private.v1`, freeze the maximum Task Balance amount, and
submit to their own local Ryn node. The local node resolves and contacts the
Provider Ryn endpoint; it does not return or use the model runtime URL.

Provider capacity policy is explicit rejection when all slots are occupied.
Task states are `created`, `accepted`, `running`, and one terminal state:
`succeeded`, `failed`, `timed_out`, `cancelled`, or `rejected`. Stable task IDs,
signed envelopes, terminal ciphertext caching, and idempotent settlement IDs
prevent duplicate inference and duplicate charging on retry/reconnect.

## Two isolated nodes

Automated deterministic protocol test (clearly labelled test adapter):

```bash
python scripts/llm_e2e.py run
```

The isolated demo WebApps use the fixed, test-only device token
`rynmesh-e2e-browser-token` on both ports. This token is intentionally public
inside the local demo configuration and must never be reused for a real node.

Force the dedicated ciphertext relay path:

```bash
python scripts/llm_e2e.py relay-run
```

Use an already-running real OpenAI-compatible host runtime (the supplied
example expects the test machine's `qwen35-4b-vlm` on port 8080; edit the private
manifest for another owner-managed runtime):

```bash
python scripts/llm_e2e.py host-real-run
```

Or mount an owner-supplied GGUF into the isolated real llama.cpp profile:

```bash
export RYNMESH_REAL_MODEL_PATH=/absolute/path/to/model.gguf
python scripts/llm_e2e.py real-run
```

Topology:

```text
Consumer Ryn ── signed E2EE task ──> Provider Ryn ── private API ──> LLM runtime
      │                                  │                              │
      └──── directory metadata ──> Registry         Provider-only network ┘
      └──── optional ciphertext ──> dedicated Relay ────────────────────┘
```

The Consumer and Provider have different identities, ports, configuration,
and Docker volumes. The test/real runtime is only on `provider-runtime`; the
Consumer is not attached to that network.

### Remote Windows Consumer through public STCP

`deploy/llm-e2e/windows-consumer/` contains the reproducible PyInstaller spec
and no-console bootstrap. Build it from the repository root, then place the
result beside `frpc.exe`, a private `frpc.toml`, and a private
`rynmesh-consumer.json`. Both private configuration files are gitignored.

The bootstrap starts the FRP visitor first, binds the tunneled Registry and
Relay endpoints to remote loopback, starts the Consumer on remote loopback, and
opens the Services page. Configure `RYNMESH_LLM_FORCE_RELAY=1`; expose only the
STCP provider names on the Provider-side FRP client. Do not create a public TCP
mapping for the Provider peer or model port. The public FRP server transports
the private tunnel but the LLM Relay receives only end-to-end ciphertext.

The Windows package can be built with:

```powershell
.venv\Scripts\pyinstaller.exe --noconfirm --clean `
  deploy\llm-e2e\windows-consumer\RynmeshPublicConsumer.spec
```

Start `RynmeshPublicConsumer.exe` from a local folder on the remote machine.
Identity, order state, logs, and PID files are stored under
`%LOCALAPPDATA%\RynmeshPublicConsumer`; model files and Provider secrets are
never copied there.

Stop without deleting volumes:

```bash
python scripts/llm_e2e.py down
```

Remove only the named E2E containers, networks, and E2E volumes:

```bash
python scripts/llm_e2e.py clean
```

Cleanup never deletes a host GGUF mounted read-only by the real profile.

## Troubleshooting

- `Docker ... engine is not running`: start Docker Desktop/Engine, then rerun.
- `checksum mismatch`: the partial file is deleted and installation stops.
  Do not bypass it; retry only after validating the source.
- `non-loopback local API blocked`: use loopback. Use the override only for an
  owner-controlled isolated network.
- `configured model not reported`: list the runtime's `/v1/models` result and
  select its exact private model ID; discovery still publishes only the alias.
- `capacity_exhausted`: wait and retry with the same task ID/idempotency key.
- `direct provider path failed`: configure a separate
  `RYNMESH_LLM_RELAY_URL` on both nodes. The control-plane registry is not a
  task relay.
- Debug-body logging requires `RYNMESH_LLM_DEBUG_BODIES=1` and emits a warning.
  Never enable it on production/private tasks.

## Deferred P1

Native streaming-token delivery, hard interruption of an in-flight backend
request, AMD/Apple-Silicon acceleration, additional model formats/engines,
dynamic pricing, package signatures, production payment, and dispute handling
remain P1 and do not alter the P0 privacy/payment claims above.
