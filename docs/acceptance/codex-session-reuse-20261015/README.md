# Codex CLI thread reuse — 2026-10-15

Before this change every Ryn agent step on a Codex CLI model created a private
temp directory, copied `auth.json`, started `codex app-server --stdio`, ran
`initialize`, created an ephemeral thread, sent the whole conversation and then
closed the process. The measured median was about 17 s per step, with a maximum
of 97 s. Each step got a new thread id, so the provider prompt cache keyed on that
id rarely matched, and reasoning never carried over between steps.

## What changed

| Area | Behavior |
| --- | --- |
| `llm_package/codex_threads.py` (new) | A per-adapter pool of in-memory ephemeral threads, each in its own app-server process and private `CODEX_HOME`. Threads are grouped by `(command, model, effort, requester, window)`. At most 4 idle threads (`RYN_CODEX_SESSION_LIMIT`, 1–16), LRU-evicted, and closed after 30 min idle. There is no turn or age cap. |
| Requester | Derived by the provider from the authenticated caller, never from request fields. The local agent bridge and self-orders use `local`, each local OpenAI-compatible API key uses `local-api:<key id>`, and a P2P order uses `peer:<signed from_peer_id>` (`local` for the node's own requests). |
| Window | The bridge accepts `conversation=` (the agent task id) and sends only `sha256("ryn-codex-window-v1\0" + id)[:32]`. Local adapters get it in `conversation_scope`. P2P orders carry it as `conversation_key` in the sealed request body and in the request fingerprint. `execute_order` rejects a malformed key; the provider ignores one. The existing rejection of `conversation_id` is unchanged. |
| Retention | With a window, a thread survives final answers: the final answer is recorded in the thread's transcript, and the window's next owner message continues it. Without a window (older clients, API keys) a thread only spans one tool loop, as before. |
| Resume rule | Inside a group, a thread is resumed only when the request's client transcript exactly extends what that thread received plus the response it returned, including the random `call_…` ids. This content match remains the second check. Supersession (closing a thread that a new request shares a root with but does not extend) is judged only inside the same group. A model or effort switch in a window closes that window's old thread. |
| Turn content | The seed turn sends the full framed transcript. Each later turn sends only the new `tool`/`user` messages. Changed functions go in `functions_update`. Changed labelled JSON state lines of the system message (for example `Server control state: {...}`, where write receipts grow) go in `system_state_updates`. Changed pre-conversation context (plan, guidance) goes in `context_messages`. |
| Prompt prefix | Both modes now send `functions`, `tool_choice` and `output_schema` before `messages`. |
| Streaming | `item/agentMessage/delta` is buffered beside the RPC queue, with at most one queued wake-up. The caller thread decodes the protocol object's leading `content` string as it arrives and emits OpenAI chunks; the bridge turns them into `text_delta`. Text is coalesced to about 100 ms or 256 characters. Tool proposals stay buffered until validated and are sent at the end. |
| `CodexRPC` | Per-turn deadline and 8 MiB output budget. Disk and private-storage checks are throttled to every 250 ms instead of every event. `alive()` and `settle()` check an idle thread before reuse. |
| CPU budget | POSIX workers keep a lifetime `RLIMIT_CPU`, now 7200 s for kept processes. Before resuming, the pool reads the process CPU (`/proc` on Linux, `ps` on macOS, otherwise the wall time spent in turns, an upper bound for this I/O-bound server). If the next turn could cross the limit, the thread is closed and the request is reseeded with full history (`stats["rebuilt"]`), never failed. Windows job objects cap the CPU rate only, so no lifetime check is needed there. |
| `CLIAgentAdapter` | `supports_conversation_scope` and `chat(..., conversation_scope=)`. Passes `on_event` to Codex for live streaming. `close_sessions()` is called by `shutdown()`. Capabilities add `conversation_memory: tool_loop_idle_bounded`; `conversation_storage` stays `none` because nothing is stored on disk. |
| `agent_runtime/bridge.py` | `supports_conversation_key = True` and `complete(..., conversation=)`. |

## Safety constraints kept

- Every thread uses `ephemeral: true`, `environments: []` on thread and turn, `sandbox: read-only`, `approvalPolicy: never`, `dynamicTools: []`, empty workspace and capability roots, and a private `CODEX_HOME` with the restrictive config. Any provider execution item or server request is rejected and ends the turn. A server request that arrives while a thread is idle voids that thread (`settle`).
- A thread is closed, and the next request uses full history, on:
  - a final answer, but only for requests without a window key
  - a rejected or invalid proposal
  - any turn error, timeout or cancellation (`adapter.cancel` kills the in-flight process)
  - a process that exited while idle
  - a transcript that no longer extends the thread (compaction, a model switch or a resource-scope change, which moves the context epoch)
  - a change in system-message prose, which is where the client permission policy (for example read-only) is written
  - a model or effort switch in the same window
  - LRU eviction, 30 min idle, adapter shutdown or application exit (`atexit`)
- Isolation: a different requester never gets another requester's thread, even with an identical window key and transcript. A request can only supersede threads in its own requester group.
- Fallback: when a resumed thread is lost mid-turn (`runtime_connection_failed`), the request is retried once in the previous stateless full-history mode. It is not retried if the request was cancelled, was out of time, or had already streamed text. The provider never executes tools, so the fallback only resends the client's own record, including tool results it already executed. A proposal from the lost turn was never returned, so nothing is replayed.
- Kill switch: `RYN_CODEX_SESSION_REUSE=0` restores the stateless mode exactly.
- Native `dynamicTools` / `item/tool/call` is still not used. The recorded probe (`.codex-tmp/research/codex-dynamic-probe.json`, see `reference-runtime.md`) shows the real model never emits `item/tool/call` without a provider environment. Enabling the code-mode host to force it would grant the provider execution, so outputSchema proposals remain. The protocol's `TurnToolOutput` also carries no call id to answer a specific call.

## Tests (this machine, without Codex installed)

New file `tests/test_codex_thread_reuse.py` (35 tests). The first 23 cover the tool-loop design:
- a multi-step loop on one process and thread, with seed and delta payloads checked
- in-place state, context and function updates
- a policy-prose change that reseeds
- cancel closing the thread without fallback
- a crash mid-turn falling back once with full history
- a process that exited while idle being replaced
- no fallback after streamed text
- streamed content before proposals, and escape and surrogate decoding split across deltas
- a rejected proposal closing the thread
- LRU bound, idle reaper and shutdown
- the kill switch
- idle server requests
- one queued delta wake-up
- the real agent runtime and bridge continuing one thread across three steps

The window-scoped tests cover:
- a final answer kept for the window, with the next owner message resumed
- two windows that both start with “你好” not superseding each other
- another requester replaying the identical transcript and window key getting a new thread
- 30-minute idle expiry
- no window key, or an invalid one, falling back to tool-loop reuse
- a model switch closing the window's old thread
- the CPU-budget rebuild
- the bridge sending only the hashed window and the `local` requester (and nothing for peers without a window)
- the provider taking the requester from the signed sender even when the body claims `requester: local`
- each local API key being its own requester

`tests/test_inference_privacy.py`: the two-step origin-tool test now asserts that the second turn continues the same thread and sends only the tool result. The shared fake gained `alive()`, `settle()` and `cpu_seconds()`.

| Check | Result |
| --- | --- |
| `test_codex_thread_reuse`, `test_codex_tool_proposals`, `test_inference_privacy`, `test_cli_agent`, `test_agent_peer_cli`, `test_agent_codex_loop`, `test_agent_reasoning_effort` (window version, against the current runtime and store) | 161 passed, 2 skipped (with a dummy `codex` on PATH; without one, the two `test_native_prompt_*` cases fail at `_command_path`, as they did before this change). `test_codex_thread_reuse` alone also passed 4 consecutive full-file runs. |
| Runtime, bridge, CLI, inference API, LLM and peer selection (window version) | 952 passed, 23 skipped, 9 failed: the 2 `_command_path` cases, the 5 `test_agent_run_timing.py` cases that predate this change, and `test_llm_streaming_http.py::test_real_http_friend_stream_archives_once_and_reconnects_without_new_inference[0, 3600]`. The last one uses an OpenAI-compatible HTTP runtime, not Codex. It also fails on an overlay of the current package with this change's `routes.py`/`api.py` additions removed. Both runs time out on the test's fixed 10 s waits on this share. |
| Earlier tool-loop version, 22 module groups | 719 passed, 10 skipped, 7 failed: the 2 `_command_path` cases above and 5 in `test_agent_run_timing.py`. The latter fail identically against a baseline copy with the original Codex files, so they predate this change. |

The wide `-k agent` selection was stopped: on this VirtualBox share it was at 3 % after
10 minutes, and a headless-Edge browser test (`test_page_controls_and_text_*`) sat idle.
Browser and desktop tests do not touch this adapter.

## Before/after per-step latency

Codex is not installed on the machine where this was implemented, so no live
timing was recorded here. On a machine with a logged-in Codex CLI, run from the
repository with its virtual environment:

```bash
python docs/acceptance/codex-session-reuse-20261015/measure_steps.py --live --rounds 3 --stream
```

It runs the same scripted agent window in alternating order with
`RYN_CODEX_SESSION_REUSE=0` and `=1`: a first owner turn that lists a directory,
reads five files and answers, then a follow-up owner turn in the same window. It
writes `results/steps-<UTC>.json` with per-step wall time, `duration_ms`, tokens,
whether the step started, resumed, fell back or was rebuilt, and time to first
streamed text. Medians and p90/max are reported for the first step, later steps
and the first step of the follow-up turn. It never records model text or fixture
content. It spends real Codex inference, so it requires `--live`.

### Memory and `SESSION_LIMIT`

Each kept thread is one idle `codex app-server` process. The script records the
resident memory of every kept process after each step (`kept_rss_kb`,
`kept_rss_kb_max`). It could not be measured here, because Codex is not installed.
The default stays 4, so idle memory is bounded at 4 × that RSS. The Windows job
object also caps each process at 2 GiB. If the measured RSS is small, for example
under about 100 MB, raising the limit to 6–8 with `RYN_CODEX_SESSION_LIMIT` keeps
more windows warm. Each eviction costs one cold start on that window's next turn,
not a failure.

## Not yet verified

- Agent wiring: `AgentRuntime._complete` (in `runtime.py`, outside this change)
  must pass `conversation=row["id"]` for non-nested calls when
  `self.model.supports_conversation_key` is true. Until it does, the agent sends
  no window key and gets tool-loop reuse only; cross-turn retention is covered
  by the adapter, bridge and provider tests above.
- Live per-step latency, follow-up latency and kept-process RSS on a machine
  with a logged-in Codex CLI (`measure_steps.py --live`).
- Mixed versions: an older provider ignores `conversation_key` and an older
  consumer sends none; both fall back to tool-loop reuse. This follows from the
  code paths but was not run against an older build.
