# Two physical Windows hosts: development gate

This harness is an independent development check, not a formal acceptance trial or part of the 30-case count. The formal grader is not activated. It wraps the installed production peer HTTP application; FriendService, signed catalog verification, encrypted task protocol, admission, ModelDispatchAdapter, AgentRuntime and local encrypted stores remain production implementations. No transport is replaced with an in-memory mock.

Use the same built wheel on both hosts. Run from the isolated harness directory with its venv Python, without a checkout `PYTHONPATH`. Python 3.12 and the wheel's base dependencies suffice. Copy these non-secret scripts to both hosts. Never transfer the provider owner's identity file, API credential or source configuration to the consumer. Only invitations, public identities and test evidence are exchanged by the operator.

## Start and bootstrap (no model calls)

Local provider (the operator supplies its actual local owner home):

```powershell
<local-venv-python> node.py --role provider --home D:/code/rynmesh/.codex-tmp/two-host-20261010/state-provider --bind 172.16.8.191 --port 18781 --allowed-peer 172.16.8.82 --owner-home <local-owner-home>
```

Remote consumer, in a foreground SSH session retained by the operator:

```powershell
C:/Users/ziling/rynmesh-agent-test-20261010/venv/Scripts/python.exe node.py --role consumer --home C:/Users/ziling/rynmesh-agent-test-20261010/state-consumer --bind 172.16.8.82 --port 18782 --allowed-peer 172.16.8.191
```

Each process listens on the specified LAN interface and loopback on that port. `/api/local/` is blocked on LAN by this test wrapper and still requires the host-local control token. `--allowed-peer` is an extra test network boundary, not production authorization evidence. The remote firewall should restrict the listener to the local peer IP. On the current local host firewall profiles are disabled; its configured firewall rule must not be described as enforced. No SSH tunnel is used by these endpoints. LAN configuration alone does not prove physical transport: preserve both peers' observed source IP receipts and host/process evidence.

Each host writes `two-host-identity.json`, `two-host-audit.jsonl`, and a local token file in its isolated home. Evidence contains Python production-file hashes, launcher hash, PID, hostname, peer public keys, endpoint and store binding. The provider maps only the approved existing `src-7de9fb1d36c44bc8` / `glm-5.3-flash` adapter in memory. Its encrypted source-file hash is checked before and after every actual inference. The isolated source holds no API key; the private boundary canary has no working model adapter. Startup makes no inference calls and imports no earlier tool-support assertion.

Use `control.py --home <local-state-home> --port <local-port> invite`, transfer only its invite response JSON, then on the consumer use `join --invite-file <file>`. Every control operation is retained under `two-host-control/`; its token header is never recorded. Invite secrets are pairing capabilities, so do not publish those raw files publicly while valid.

## Explicit live preparation

For actual remote Agent execution, both nodes also require a shared production HTTP signaling registry. AgentModelBridge intentionally requests strict `p2p`; friend HTTP reachability alone is insufficient, and an environment `direct` override cannot weaken this choice. Add `--registry-url http://172.16.8.191:18780 --stun off` to both node commands. Leave `--p2p-bind-port` unset for dynamic UDP and persistent pooled sessions. Scope the consumer UDP firewall permission to its actual Python executable, local LAN interface and provider IP. The registry uses isolated storage and an IP boundary; no public registration or payload relay is required. Preserve nominated ICE local/remote addresses and `relay_used=false` from actual order receipts.

The first physical attempt lacked shared signaling and failed before provider inference. Those failures remain evidence; the corrected topology is a separate phase. Its node version writes an append-only process-start record and a historical identity file on each restart. Parent-operated registry launchers must be identified by their actual source hash; merely including `registry_node.py` here does not mean that file was executed.

On the provider, invoke the normal source tools probe with a UTF-8 JSON body `{"model_id":"glm-5.3-flash","probe":"tools"}`:

```powershell
<python> control.py --home <provider-home> --port 18781 api POST /api/local/llm/sources/src-two-host-glm/models/test --body-file <probe-body.json>
```

This explicitly runs the production two-turn tool probe using GLM. Its results update only the isolated source and are captured in the dispatch log. This local owner probe is distinct from the later peer admission tests. Enable sharing only for GLM using the normal source API, grant that service to the actual friend's relationship using `grant <service-id> <relationship-id> --revision 0`, then consumer `refresh <provider-peer-id>`. Preserve pre/post catalog snapshots; the private canary must remain absent. Read `/api/local/agent/models` for the real peer catalog model ID.

## Sessions and boundaries

Before tasks, seed a real provider-local encrypted *manual* history marker (no inference and no Agent task):

```powershell
<python> scenario.py --home <provider-home> --port 18781 --out <provider-canary.json> provider-history
<python> scenario.py --home <consumer-home> --port 18782 --out <sessions.json> consumer-two-sessions --model-id <actual-peer-model-id>
```

The consumer submits two actual Agent tasks with different random markers and read-only goals requesting two successive inspections. It does not script model tool calls. Two queued tasks are not by themselves proof of interleaving or concurrency. Inspect actual model/tool event order, require at least two separate tool-call rounds per session, and preserve failures if the model does not do this. After terminal status, obtain its current revision and run `scenario.py ... --out <followup.json> followup --task-id <id> --revision <revision>`. Verify each follow-up retains its own marker without the other session's marker. Provider task count must stay zero. Manual provider history is not provider Agent history; that T15 variant remains untested by this scenario.

`probe.py` runs only on the consumer using its isolated signing/messaging keys. Give it the provider's public identity JSON, actual service ID and a permission JSON. Run before granting, against the private sibling while copying the GLM grant, and after revoking with the saved old grant revision. Preserve signed requests and authenticated responses alongside provider events. An HTTP failure alone is not a model authorization verdict. Require the specific production denial and zero corresponding upstream dispatches. An unexpected admission may call the sole authorized GLM, so these probes belong to the explicit live stage.

Use the normal APIs to test Agent off/on while manual model calls/sharing still work. Test disconnect by stopping the isolated provider process, preserve failed consumer operations, then restart the same provider home, refresh and retry. Current launcher rebuilds cached production managers through the guarded factory on restart. Do not erase old audit logs. Per-model grants, revisions and source histories remain real persisted business state.

## Later provider Agent-history phase

`extra_scenarios.py` is a separate optional live-action script; it does not change the frozen node or original scenario file. First preserve the completed two-consumer phase snapshot proving zero provider Agent tasks. Then run the provider-side `provider-agent-history --model-id <actual-local-GLM-id>`. It reads the real production catalog, enables the local Agent, and creates one actual provider-owned read-only task with a fresh private marker. Its creation receipt is not completion proof: wait for actual `completed` status, persisted assistant answer and corresponding GLM receipts. Then run `agent-off` locally. This refuses to interrupt queued/running tasks.

After that phase, do not apply the old zero-provider-task assertion to the new snapshot. Check that the provider task set is exactly its prior tasks plus the independently created provider task, that its task ID is absent from consumer state, and that no consumer Agent task IDs are present in provider state. Inspect provider upstream requests corresponding to later consumer orders and ensure the provider Agent marker is absent. This provides a separate genuine provider Agent-history isolation check while preserving the earlier zero-task baseline.

On the consumer, `agent-off` followed by `manual-off --model-id <actual-peer-GLM-id>` sends one normal manual order while the consumer Agent stays off. The script obtains the authenticated current destination binding from the actual catalog and retains strict production P2P, then records before/after task sets and settings. It neither injects an Agent task nor mutates the provider grant. Provider sharing while its Agent is off is verified by the same actual successful consumer order and provider-off snapshot.

For externally performed stop/restart steps, `connectivity-check --provider-peer-id <id> --provider-endpoint <LAN-URL> --expected unreachable|reachable` captures actual health identity and catalog refresh. It does not kill processes or infer model success from health. Use a separate `manual-off` request to retain a real failed operation during disconnect and a successful operation after reconnect. All actions take `--home`, `--port`, and `--out` before the action name; files are UTF-8. No action runs automatically.

## Collect and judge

On each host use `control.py ... snapshot --out <snapshot.json>`. Copy only snapshots, audit logs, control receipts and public identity artifacts for analysis, excluding the token and private state files. `validate.py --provider-snapshot ... --consumer-snapshot ... --provider-audit ... --consumer-audit ... --out ...` performs narrow mechanical observations and lists remaining manual checks. It does not certify arbitrary final-answer semantics or count formal passes. Log captures exceeding 2 MiB are marked truncated; absence claims must respect captured scope. Provider upstream transcripts contain test history and should be handled as private evidence.

The owner source hash checks cover that encrypted source file, not every unrelated owner setting or filesystem file. Signed request receipts establish message authentication; hostnames, PIDs and operator-collected OS/wheel evidence are separately needed for the physical-host claim. A copied code manifest is not hardware attestation.

## No-API verification

```powershell
.venv/Scripts/python.exe -m pytest docs/acceptance/ryn-agent-delivery/two-host/test_harness.py -q -p no:cacheprovider --tb=short
.venv/Scripts/python.exe -m ruff check docs/acceptance/ryn-agent-delivery/two-host
```

Tests use fresh synthetic keys and no vendor API. The local launcher smoke test is a one-host mechanism test, not two-physical-host evidence.

## Cancellation follow-up, 2026-10-11

`phase6-cancellation-operator-review.json` freezes the latest same-wheel physical-host
check. Both Windows installations matched all 238 package files in wheel
`a034a1776369540f11c6e24238d9373197fb139d916200919dcfcb010d0d7607`.
After confirming a real GLM upstream dispatch, normal consumer cancellation
returned in 0.109 seconds. Both persisted order ledgers remained cancelled. Once
the provider's occupied inference slot became available, a subsequent manual
request succeeded through a new direct ICE connection in the same two processes.
Both Agents stayed disabled and no Agent task was added. The previous cancellation
also remained cancelled across a process restart.

The earlier attempted follow-up hit `ConnectError` because all three old test
sessions had exited. That result is retained separately; it is not reclassified
as a successful same-process test. The remote temporary firewall rules and both
node/registry listeners were removed after collection; installations and state
remain available for later tests. Owner source ciphertext hash is unchanged.

The production fix interrupts the consumer's pending P2P exchange when its own
ledger records cancellation. It evicts the cancelled session, avoids transport
fallback/replay, suppresses late output, and preserves authenticated usage if
already received. It does not establish that supplier computation/billing has
stopped, nor prove blocking HTTP/relay/signaling waits interruptible. Regression:
168 passed, 4 skipped; the four skips are existing POSIX-only fake llama-server
installation tests in `test_llm_package.py` on Windows, not cancellation tests.
Targeted Ruff passed. Independent Astra review remains
pending after usage limits. This is operator development evidence, not a formal
trial, Agent-budget real-model validation, or Windows desktop install/upgrade
acceptance. Full acceptance remains incomplete.
