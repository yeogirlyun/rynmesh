"""Tests for S01-I8: Prompt caching (Responses prompt_cache_key, Anthropic cache_control, Chat provider/probe gating)."""
from __future__ import annotations

import copy
import json
import threading
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import pytest
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey

from rynmesh.agent_runtime import subagents, task_forks
from rynmesh.agent_runtime.bridge import AgentModelBridge
from rynmesh.agent_runtime.models import ModelCatalog
from rynmesh.agent_runtime.runtime import AgentRuntime
from rynmesh.agent_runtime.store import AgentStore
from rynmesh.llm_package import model_settings
from rynmesh.llm_package.adapters import OpenAICompatibleAdapter
from rynmesh.llm_package.api_formats import normalize
from rynmesh.llm_package.chat import validate_chat
from rynmesh.llm_package.protocol_adapter import NativeProtocolAdapter
from rynmesh.llm_package.protocol_codecs import (
    MESSAGES,
    RESPONSES,
    StreamDecoder,
    make_request,
)
from rynmesh.llm_package.sources import SourceStore

SCOPE = "a" * 64
MODEL = "cache-test-model"
TOOL_A = {
    "type": "function",
    "function": {
        "name": "tool_alpha",
        "description": "Alpha tool",
        "parameters": {
            "type": "object",
            "properties": {"q": {"type": "string"}},
            "required": ["q"],
            "additionalProperties": False,
        },
    },
}
TOOL_B = {
    "type": "function",
    "function": {
        "name": "tool_beta",
        "description": "Beta tool",
        "parameters": {
            "type": "object",
            "properties": {"id": {"type": "string"}},
            "required": ["id"],
            "additionalProperties": False,
        },
    },
}


class _QuietHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *_args: Any) -> None:
        return


def _start_http_server(responder) -> tuple[ThreadingHTTPServer, str, list[tuple[str, dict[str, Any]]]]:
    recorded: list[tuple[str, dict[str, Any]]] = []

    class Handler(_QuietHandler):
        def do_POST(self) -> None:
            size = int(self.headers.get("Content-Length", "0"))
            raw = self.rfile.read(size) if size else b"{}"
            parsed = json.loads(raw.decode("utf-8"))
            recorded.append((self.path, parsed))
            status, headers, payload_obj = responder(self.path, parsed, len(recorded))
            body_bytes = (
                payload_obj
                if isinstance(payload_obj, bytes)
                else json.dumps(payload_obj).encode("utf-8")
            )
            self.send_response(status)
            merged = {
                "Content-Type": "application/json",
                "Content-Length": str(len(body_bytes)),
                **headers,
            }
            for k, v in merged.items():
                self.send_header(k, v)
            self.end_headers()
            self.wfile.write(body_bytes)

    srv = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    srv.daemon_threads = True
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    return srv, f"http://127.0.0.1:{srv.server_port}", recorded


def _count_cache_controls(obj: Any) -> int:
    if isinstance(obj, dict):
        own = 1 if "cache_control" in obj else 0
        return own + sum(_count_cache_controls(v) for v in obj.values())
    if isinstance(obj, list):
        return sum(_count_cache_controls(x) for x in obj)
    return 0


# T1: Responses request includes validated prompt_cache_key; allowlists & model_settings defaults
def test_t1_responses_request_carries_validated_prompt_cache_key() -> None:
    assert "prompt_cache" in model_settings.FIELDS
    assert model_settings.defaults({"models": ["m"], "protocol": "openai_responses"})["prompt_cache"] == "auto"
    assert model_settings.defaults({"models": ["m"], "protocol": "anthropic_messages"})["prompt_cache"] == "auto"
    assert model_settings.defaults({"models": ["m"], "protocol": "openai_chat"})["prompt_cache"] == "off"
    assert model_settings.defaults({"models": ["m"]})["prompt_cache"] == "off"
    assert model_settings.validate({"prompt_cache": "auto"})["prompt_cache"] == "auto"
    assert model_settings.validate({"prompt_cache": "off"})["prompt_cache"] == "off"
    for invalid_mode in ("on", "", True, 1, None):
        with pytest.raises(ValueError, match="model_prompt_cache_invalid"):
            model_settings.validate({"prompt_cache": invalid_mode})

    valid_key = "agent_0123456789abcdef0123456789abcdef:subagent"
    chat_norm = normalize(
        {"messages": [{"role": "user", "content": "hi"}], "max_tokens": 64, "prompt_cache_key": valid_key},
        "chat",
    )
    assert chat_norm["prompt_cache_key"] == valid_key

    resp_norm = normalize(
        {"input": "hi", "max_output_tokens": 64, "prompt_cache_key": valid_key},
        "responses",
    )
    assert resp_norm["prompt_cache_key"] == valid_key

    validated = validate_chat(
        {"messages": [{"role": "user", "content": "hi"}], "max_tokens": 64, "prompt_cache_key": valid_key}
    )
    assert validated["prompt_cache_key"] == valid_key

    wire = make_request(
        {
            "messages": [{"role": "user", "content": "hello"}],
            "tools": [copy.deepcopy(TOOL_A)],
            "max_tokens": 128,
            "prompt_cache_key": valid_key,
        },
        RESPONSES,
        MODEL,
        SCOPE,
    )
    assert wire["prompt_cache_key"] == valid_key
    assert _count_cache_controls(wire) == 0

    wire_no_key = make_request(
        {"messages": [{"role": "user", "content": "hello"}], "max_tokens": 128},
        RESPONSES,
        MODEL,
        SCOPE,
    )
    assert "prompt_cache_key" not in wire_no_key

    # Invalid prompt_cache_key values (>64 bytes, forbidden chars, empty, non-str) must be rejected
    for bad_key in ("", "a" * 65, "bad space", "bad/slash", "中文键", 123, True):
        with pytest.raises(ValueError):
            validate_chat({"messages": [{"role": "user", "content": "hi"}], "prompt_cache_key": bad_key})
        with pytest.raises(ValueError):
            make_request(
                {"messages": [{"role": "user", "content": "hi"}], "prompt_cache_key": bad_key},
                RESPONSES,
                MODEL,
                SCOPE,
            )


# T2: Messages request places 3 cache_control breakpoints after step 2, omits when off, records cache_creation_input_tokens, and retries once on 400 cache_control rejection
def test_t2_messages_cache_control_breakpoints_step_threshold_off_and_400_fallback() -> None:
    base_prefix = [
        {"role": "system", "content": "System instructions"},
        {"role": "developer", "content": "Developer policy"},
    ]
    tools = [copy.deepcopy(TOOL_A), copy.deepcopy(TOOL_B)]

    # Step 1 (1 user message) -> no cache_control breakpoints
    step1_wire = make_request(
        {
            "messages": [*base_prefix, {"role": "user", "content": "Step 1 prompt"}],
            "tools": copy.deepcopy(tools),
            "max_tokens": 256,
            "prompt_cache_key": "agent_step1_key",
        },
        MESSAGES,
        MODEL,
        SCOPE,
    )
    assert "prompt_cache_key" not in step1_wire
    assert _count_cache_controls(step1_wire) == 0

    # Step 2 (2 user messages, 1 assistant turn) -> still no cache_control ("默认第 2 步之后才加断点")
    step2_history = [
        *base_prefix,
        {"role": "user", "content": "Step 1 prompt"},
        {
            "role": "assistant",
            "content": "Calling alpha",
            "tool_calls": [
                {"id": "call_1", "type": "function", "function": {"name": "tool_alpha", "arguments": '{"q":"a"}'}}
            ],
        },
        {"role": "tool", "tool_call_id": "call_1", "content": "alpha_ok"},
    ]
    step2_wire = make_request(
        {
            "messages": step2_history,
            "tools": copy.deepcopy(tools),
            "max_tokens": 256,
            "prompt_cache_key": "agent_step2_key",
        },
        MESSAGES,
        MODEL,
        SCOPE,
    )
    assert "prompt_cache_key" not in step2_wire
    assert _count_cache_controls(step2_wire) == 0

    # Step 3 (after step 2: 3 user messages, 2 assistant turns) -> 3 cache_control breakpoints at exact positions
    step3_history = [
        *step2_history,
        {
            "role": "assistant",
            "content": "Calling beta",
            "tool_calls": [
                {"id": "call_2", "type": "function", "function": {"name": "tool_beta", "arguments": '{"id":"b"}'}}
            ],
        },
        {"role": "tool", "tool_call_id": "call_2", "content": "beta_ok"},
        {"role": "user", "content": "Now summarize"},
    ]
    step3_wire = make_request(
        {
            "messages": copy.deepcopy(step3_history),
            "tools": copy.deepcopy(tools),
            "max_tokens": 256,
            "prompt_cache_key": "agent_step3_key",
        },
        MESSAGES,
        MODEL,
        SCOPE,
    )
    assert "prompt_cache_key" not in step3_wire
    assert _count_cache_controls(step3_wire) == 3
    # 1) Last block of system
    assert "cache_control" not in step3_wire["system"][0]
    assert step3_wire["system"][-1]["cache_control"] == {"type": "ephemeral"}
    # 2) Last item of tools
    assert "cache_control" not in step3_wire["tools"][0]
    assert step3_wire["tools"][-1]["cache_control"] == {"type": "ephemeral"}
    # 3) Last block of second-to-last user message
    user_msgs = [m for m in step3_wire["messages"] if m["role"] == "user"]
    assert len(user_msgs) == 3
    assert _count_cache_controls(user_msgs[0]) == 0
    assert user_msgs[-2]["content"][-1]["cache_control"] == {"type": "ephemeral"}
    assert _count_cache_controls(user_msgs[-1]) == 0

    # prompt_cache="off" -> 0 cache_control breakpoints even on Step 3+
    step3_off_wire = make_request(
        {
            "messages": copy.deepcopy(step3_history),
            "tools": copy.deepcopy(tools),
            "max_tokens": 256,
            "prompt_cache_key": "agent_step3_key",
        },
        MESSAGES,
        MODEL,
        SCOPE,
        prompt_cache="off",
    )
    assert "prompt_cache_key" not in step3_off_wire
    assert _count_cache_controls(step3_off_wire) == 0

    # StreamDecoder._update_usage records cache_creation_input_tokens for diagnostics
    decoder = StreamDecoder(MESSAGES, MODEL, SCOPE)
    decoder.feed(
        {
            "type": "message_start",
            "message": {
                "id": "msg_cache_diag",
                "type": "message",
                "role": "assistant",
                "content": [],
                "usage": {
                    "input_tokens": 20,
                    "output_tokens": 1,
                    "cache_creation_input_tokens": 150,
                    "cache_read_input_tokens": 400,
                },
            },
        }
    )
    assert decoder.usage["prompt_tokens"] == 570
    assert decoder.cached_tokens == 400
    assert decoder.cache_creation_input_tokens == 150

    # Domestic relay returning 400 mentioning cache_control: strip cache_control, resend once, remember capability
    def _relay_responder(_path: str, payload: dict[str, Any], _idx: int):
        if _count_cache_controls(payload) > 0:
            return (
                400,
                {},
                {
                    "type": "error",
                    "error": {
                        "type": "invalid_request_error",
                        "message": "Extra inputs are not permitted: system.1.cache_control",
                    },
                },
            )
        return (
            200,
            {},
            {
                "id": "msg_relay_ok",
                "type": "message",
                "role": "assistant",
                "content": [{"type": "text", "text": "Relay succeeded without cache_control"}],
                "stop_reason": "end_turn",
                "usage": {
                    "input_tokens": 30,
                    "output_tokens": 6,
                    "cache_creation_input_tokens": 0,
                    "cache_read_input_tokens": 0,
                },
            },
        )

    srv, base_url, recorded = _start_http_server(_relay_responder)
    try:
        adapter = NativeProtocolAdapter(
            protocol="anthropic_messages",
            base_url=base_url,
            api_prefix="",
            api_key="relay-test-key",
            model="claude-relay",
        )
        out1 = adapter.chat(
            {"messages": copy.deepcopy(step3_history), "tools": copy.deepcopy(tools), "max_tokens": 128},
            task_id="relay-1",
            timeout_s=5.0,
        )
        assert out1["text"] == "Relay succeeded without cache_control"
        assert len(recorded) == 2
        assert _count_cache_controls(recorded[0][1]) == 3
        assert _count_cache_controls(recorded[1][1]) == 0

        # Subsequent call on another NativeProtocolAdapter for the same source/model skips cache_control immediately
        adapter2 = NativeProtocolAdapter(
            protocol="anthropic_messages",
            base_url=base_url,
            api_prefix="",
            api_key="relay-test-key",
            model="claude-relay",
        )
        out2 = adapter2.chat(
            {"messages": copy.deepcopy(step3_history), "tools": copy.deepcopy(tools), "max_tokens": 128},
            task_id="relay-2",
            timeout_s=5.0,
        )
        assert out2["text"] == "Relay succeeded without cache_control"
        assert len(recorded) == 3
        assert _count_cache_controls(recorded[2][1]) == 0
    finally:
        srv.shutdown()
        srv.server_close()


# T3: Root task, subagent, forked task, and summary/nested requests get expected cache_key; peer omits prompt_cache_key
def test_t3_runtime_and_bridge_cache_keys_for_root_subagent_fork_summary_and_peer(tmp_path: Path) -> None:
    assert getattr(AgentModelBridge, "supports_cache_key", False) is True

    class _RecordingModel:
        supports_cache_key = True
        supports_conversation_key = True

        def __init__(self) -> None:
            self.calls: list[dict[str, Any]] = []
            self.responses: list[dict[str, Any]] = []

        def complete(
            self,
            model_id: str,
            messages: list[dict[str, Any]],
            tool_specs: list[dict[str, Any]],
            cancelled: Any,
            on_event: Any,
            **options: Any,
        ) -> dict[str, Any]:
            self.calls.append(
                {
                    "model_id": model_id,
                    "messages": messages,
                    "specs_count": len(tool_specs),
                    "options": dict(options),
                }
            )
            if self.responses:
                return self.responses.pop(0)
            return {"message": {"role": "assistant", "content": "ok"}, "finish_reason": "stop"}

    class _Catalog:
        def list_models(self) -> list[dict[str, Any]]:
            return [
                {
                    "id": "local:test",
                    "name": "Test",
                    "source": "local_api",
                    "available": True,
                    "agent_supported": True,
                }
            ]

    class _Tools:
        def specs(self) -> list[dict[str, Any]]:
            return [copy.deepcopy(TOOL_A)]

        def metadata(self, name: str) -> dict[str, Any]:
            return {"effect": "read", "title": name}

        def preview(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
            return {}

        def execute(self, name: str, arguments: dict[str, Any], *, operation_id: str, cancelled: Any) -> dict[str, Any]:
            return {"ok": True}

    key = X25519PrivateKey.generate()
    ws = tmp_path / "ws"
    ws.mkdir(parents=True, exist_ok=True)
    model = _RecordingModel()
    runtime = AgentRuntime(AgentStore(tmp_path / "agent", key), model, _Tools(), _Catalog())
    runtime.update_settings({"enabled": True, "model_id": "local:test", "workspace_root": str(ws)})

    # 1) Root task main step -> cache_key == root_id
    model.responses.append({"message": {"role": "assistant", "content": "Root answer"}})
    root_task = runtime.create({"message": "Root task question"})
    root_id = root_task["id"]
    runtime.run_once()
    assert runtime.get(root_id)["status"] == "completed"
    assert model.calls[-1]["options"].get("cache_key") == root_id

    # 2) Compaction/summary request (specs=[]) and nested checker request (nested=True) -> same root_id cache_key
    def _prep_run_fields(tid: str) -> dict[str, Any]:
        def _mark_running(data: dict[str, Any]) -> dict[str, Any]:
            r = data["tasks"][tid]
            r["status"] = "running"
            return copy.deepcopy(r)

        cp = runtime.store.transaction(_mark_running)
        cp["_run_started"] = 0.0
        cp["_run_lifetime_remaining"] = 60.0
        cp["_run_control_revision"] = cp["control_revision"]
        return cp

    saved_root = _prep_run_fields(root_id)
    model.responses.append({"message": {"role": "assistant", "content": "Summary checkpoint text"}})
    runtime._complete(saved_root, [{"role": "user", "content": "Summarize history"}], [], lambda: False, lambda _e: None)
    assert model.calls[-1]["specs_count"] == 0
    assert model.calls[-1]["options"].get("cache_key") == root_id
    assert "conversation" not in model.calls[-1]["options"]

    model.responses.append({"message": {"role": "assistant", "content": "Nested inspector check"}})
    runtime._complete(
        saved_root,
        [{"role": "user", "content": "Nested check"}],
        [],
        lambda: False,
        lambda _e: None,
        nested=True,
    )
    assert model.calls[-1]["options"].get("cache_key") == root_id

    # Restore root task to completed so fork boundary inspection can read settled state cleanly
    runtime.store.transaction(lambda d: d["tasks"][root_id].update(status="completed"))

    # 3) Subagent task (and nested child subagent) -> f"{root_id}:subagent"
    def _spawn_children(data: dict[str, Any]) -> tuple[str, str]:
        parent = data["tasks"][root_id]
        res1 = subagents.spawn_agent_in_tx(data, parent, {"prompt": "Child 1 task", "role": "explorer"})
        child1 = data["tasks"][res1["agent_id"]]
        res2 = subagents.spawn_agent_in_tx(data, child1, {"prompt": "Grandchild task", "role": "explorer"})
        return res1["agent_id"], res2["agent_id"]

    child1_id, grandchild_id = runtime.store.transaction(_spawn_children)
    for sub_id in (child1_id, grandchild_id):
        sub_row = _prep_run_fields(sub_id)
        model.responses.append({"message": {"role": "assistant", "content": "Subagent done"}})
        runtime._complete(
            sub_row,
            [{"role": "user", "content": "Subagent work"}],
            [copy.deepcopy(TOOL_A)],
            lambda: False,
            lambda _e: None,
        )
        assert model.calls[-1]["options"].get("cache_key") == f"{root_id}:subagent"
        runtime.store.transaction(lambda d, sid=sub_id: d["tasks"][sid].update(status="completed"))

    # 4) Forked task -> inherits source_task_id (root_id) as cache_key
    root_public = runtime.get(root_id)
    boundaries = task_forks.boundaries(runtime.store.read()["tasks"][root_id])
    assert boundaries
    forked = task_forks.create(
        runtime,
        root_id,
        {
            "client_request_id": str(uuid.uuid4()),
            "title": "Forked branch",
            "message_count": boundaries[-1]["message_count"],
            "token": boundaries[-1]["token"],
            "shared_workspace_confirmed": True,
        },
    )
    fork_row = _prep_run_fields(forked["id"])
    assert fork_row["id"] != root_public["id"]
    model.responses.append({"message": {"role": "assistant", "content": "Fork reply"}})
    runtime._complete(
        fork_row,
        [{"role": "user", "content": "Continue from fork"}],
        [copy.deepcopy(TOOL_A)],
        lambda: False,
        lambda _e: None,
    )
    assert model.calls[-1]["options"].get("cache_key") == root_id

    # 5) Bridge peer route NEVER sends prompt_cache_key; local Responses auto sends it and off omits it
    peer_orders: list[dict[str, Any]] = []

    async def _fake_execute_order(req: dict[str, Any], *_args: Any) -> dict[str, Any]:
        peer_orders.append(copy.deepcopy(req))
        return {
            "state": "succeeded",
            "message": {"role": "assistant", "content": "peer ok"},
            "finish_reason": "stop",
            "reported_usage": {"input_tokens": 5, "output_tokens": 2},
        }

    local_chats: list[dict[str, Any]] = []

    class _StubAdapter:
        timeout_s = 30.0

        def chat(self, body: dict[str, Any], **_kw: Any) -> dict[str, Any]:
            local_chats.append(copy.deepcopy(body))
            return {
                "text": "local ok",
                "message": {"role": "assistant", "content": "local ok"},
                "finish_reason": "stop",
                "reported_usage": {"input_tokens": 5, "output_tokens": 2},
                "cache_creation_input_tokens": 42,
            }

    class _StubManager:
        def __init__(self) -> None:
            self.adapter = _StubAdapter()
            self.manifest = type("M", (), {"timeout_seconds": 30.0})()
            self._slots = threading.Semaphore(2)
            self._lock = threading.Lock()
            self._running = 0

    stub_mgr = _StubManager()

    class _BridgeCatalog:
        network = "rynmesh-test"

        def __init__(self) -> None:
            self.mode = "auto"

        def destination(self, row: dict[str, Any]) -> dict[str, Any]:
            return {
                "catalog_model_id": row["id"],
                "provider_peer_id": row.get("peer_id", ""),
                "source_id": row.get("source_id", ""),
                "destination_revision": row.get("destination_revision", ""),
            }

        def resolve(self, model_id: str) -> dict[str, Any]:
            if model_id == "peer/remote-1":
                return {
                    "id": model_id,
                    "source": "peer",
                    "peer_id": "peer-xyz",
                    "service_id": "svc-1",
                    "adapter": "openai_compatible",
                    "available": True,
                    "agent_supported": True,
                    "context_window": 32768,
                    "max_output_tokens": 1024,
                    "destination_revision": "rev-peer",
                    "destination_binding_supported": True,
                    "expected_model_binding": {"binding": "ok"},
                }
            return {
                "id": model_id,
                "source": "local_api",
                "source_id": "src-1",
                "service_id": "svc-local",
                "model_id": "gpt-resp",
                "peer_id": "self",
                "adapter": "openai_responses",
                "available": True,
                "agent_supported": True,
                "context_window": 32768,
                "max_output_tokens": 1024,
                "destination_revision": "rev-local",
                "destination_binding_supported": True,
                "model_settings": {"effective": {"prompt_cache": self.mode}},
            }

    b_cat = _BridgeCatalog()
    bridge = AgentModelBridge(
        {"provider_for": lambda _sid: stub_mgr, "execute_order": _fake_execute_order},
        b_cat,
    )

    bridge.complete("peer/remote-1", [{"role": "user", "content": "hi"}], [], lambda: False, cache_key=root_id)
    assert len(peer_orders) == 1
    assert "prompt_cache_key" not in peer_orders[0]["chat"]

    res_local = bridge.complete("local/svc-local", [{"role": "user", "content": "hi"}], [], lambda: False, cache_key=root_id)
    assert local_chats[-1].get("prompt_cache_key") == root_id
    assert res_local.get("cache_creation_input_tokens") == 42

    b_cat.mode = "off"
    bridge.complete("local/svc-local", [{"role": "user", "content": "hi"}], [], lambda: False, cache_key=root_id)
    assert "prompt_cache_key" not in local_chats[-1]


# T4: Chat third-party source omits prompt_cache_key by default and under auto unless provider is openai/azure or probe confirmed
def test_t4_chat_route_third_party_omits_key_by_default_and_only_sends_for_openai_azure_or_probed(
    tmp_path: Path,
) -> None:
    reject_cache_key = {"enabled": False}

    def _chat_responder(_path: str, payload: dict[str, Any], _idx: int):
        if reject_cache_key["enabled"] and "prompt_cache_key" in payload:
            return (
                400,
                {},
                {"error": {"type": "invalid_request_error", "param": "prompt_cache_key", "message": "Unknown field prompt_cache_key"}},
            )
        return (
            200,
            {},
            {
                "id": "chatcmpl-1",
                "choices": [{"index": 0, "message": {"role": "assistant", "content": "OK"}, "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 8, "completion_tokens": 2},
            },
        )

    srv, base_url, recorded = _start_http_server(_chat_responder)
    try:
        store = SourceStore(tmp_path / "node", b"test-private-key-s01-i8")

        # 1) Third-party Chat source default: prompt_cache == "off", never sends prompt_cache_key
        rec_third = store.prepare(
            {
                "name": "ThirdParty",
                "kind": "local",
                "provider": "deepseek",
                "protocol": "openai_chat",
                "base_url": base_url,
                "models": ["deepseek-chat"],
            }
        )
        store.save(rec_third)
        assert model_settings.effective(rec_third, "deepseek-chat")["prompt_cache"] == "off"
        ad_default = store.adapter(store.read()[rec_third["id"]], "deepseek-chat")
        assert isinstance(ad_default, OpenAICompatibleAdapter)
        ad_default.chat(
            {"messages": [{"role": "user", "content": "hi"}], "max_tokens": 32, "prompt_cache_key": "agent_key_1"},
            task_id="t4-1",
            timeout_s=5.0,
        )
        assert "prompt_cache_key" not in recorded[-1][1]

        # 2) Third-party Chat source with prompt_cache="auto" (unprobed): still strips prompt_cache_key before sending!
        rule_rev = store.read()[rec_third["id"]]["model_settings"]["deepseek-chat"]["rule_revision"]
        store.configure_model(
            rec_third["id"],
            {
                "model_id": "deepseek-chat",
                "expected_rule_revision": rule_rev,
                "configuration": {"prompt_cache": "auto"},
            },
        )
        ad_auto_unprobed = store.adapter(store.read()[rec_third["id"]], "deepseek-chat")
        ad_auto_unprobed.chat(
            {"messages": [{"role": "user", "content": "hi"}], "max_tokens": 32, "prompt_cache_key": "agent_key_2"},
            task_id="t4-2",
            timeout_s=5.0,
        )
        assert "prompt_cache_key" not in recorded[-1][1]

        # 3) OpenAI / Azure Chat source with prompt_cache="auto": DOES send prompt_cache_key
        for trusted_provider in ("openai", "azure"):
            ad_trusted = OpenAICompatibleAdapter(
                base_url=base_url,
                api_prefix="",
                model="gpt-4.1",
                provider=trusted_provider,
                prompt_cache="auto",
            )
            ad_trusted.chat(
                {"messages": [{"role": "user", "content": "hi"}], "max_tokens": 32, "prompt_cache_key": "agent_key_oa"},
                task_id=f"t4-{trusted_provider}",
                timeout_s=5.0,
            )
            assert recorded[-1][1].get("prompt_cache_key") == "agent_key_oa"

        # 4) Probing third-party Chat source with prompt_cache="auto":
        #    (a) When upstream accepts prompt_cache_key -> probe records supported -> subsequent chat sends it
        rec_probed_ok = store.read()[rec_third["id"]]
        probe_res = store.probe(rec_probed_ok)
        assert probe_res["ok"] is True
        store.save(rec_probed_ok)
        ad_probed_ok = store.adapter(store.read()[rec_third["id"]], "deepseek-chat")
        ad_probed_ok.chat(
            {"messages": [{"role": "user", "content": "hi"}], "max_tokens": 32, "prompt_cache_key": "agent_key_probed"},
            task_id="t4-probed-ok",
            timeout_s=5.0,
        )
        assert recorded[-1][1].get("prompt_cache_key") == "agent_key_probed"

        #    (b) When upstream rejects prompt_cache_key with 400 -> probe retries without it & marks unsupported -> chat strips it
        reject_cache_key["enabled"] = True
        rec_strict = store.prepare(
            {
                "name": "StrictRelay",
                "kind": "local",
                "provider": "custom",
                "protocol": "openai_chat",
                "base_url": base_url,
                "models": ["strict-model"],
                "prompt_cache": "auto",
            }
        )
        store.save(rec_strict)
        strict_saved = store.read()[rec_strict["id"]]
        probe_strict = store.probe(strict_saved)
        assert probe_strict["ok"] is True
        store.save(strict_saved)
        ad_strict = store.adapter(store.read()[rec_strict["id"]], "strict-model")
        ad_strict.chat(
            {"messages": [{"role": "user", "content": "hi"}], "max_tokens": 32, "prompt_cache_key": "agent_key_strict"},
            task_id="t4-probed-strict",
            timeout_s=5.0,
        )
        assert "prompt_cache_key" not in recorded[-1][1]

        # Full AgentModelBridge -> third-party Chat source default omits prompt_cache_key
        class _MiniCatalog:
            network = "rynmesh-test"

            def destination(self, r: dict[str, Any]) -> dict[str, Any]:
                return {
                    "catalog_model_id": r["id"],
                    "provider_peer_id": r["peer_id"],
                    "source_id": r["source_id"],
                    "destination_revision": r["destination_revision"],
                }

            def resolve(self, model_id: str) -> dict[str, Any]:
                src = store.read()[rec_strict["id"]]
                # Check default chat source (create a fresh default chat source)
                return {
                    "id": model_id,
                    "source": "local_api",
                    "source_id": src["id"],
                    "service_id": "svc-chat",
                    "model_id": "strict-model",
                    "peer_id": "self",
                    "adapter": "openai_compatible",
                    "available": True,
                    "agent_supported": True,
                    "context_window": 32768,
                    "max_output_tokens": 1024,
                    "destination_revision": src["destination_revision"],
                    "destination_binding_supported": True,
                    "model_settings": model_settings.public(src, "strict-model"),
                }

        class _MgrWrap:
            def __init__(self, adapter_inst: Any) -> None:
                self.adapter = adapter_inst
                self.manifest = type("M", (), {"timeout_seconds": 30.0})()
                self._slots = threading.Semaphore(2)
                self._lock = threading.Lock()
                self._running = 0

        mgr_wrap = _MgrWrap(store.adapter(store.read()[rec_strict["id"]], "strict-model"))
        bridge = AgentModelBridge(
            {"provider_for": lambda _sid: mgr_wrap, "source_store": store},
            _MiniCatalog(),
        )
        bridge_res = bridge.complete(
            "local/svc-chat",
            [{"role": "user", "content": "hello"}],
            [],
            lambda: False,
            cache_key="agent_bridge_chat_key",
        )
        assert bridge_res["message"]["content"] == "OK"
        assert "prompt_cache_key" not in recorded[-1][1]
    finally:
        srv.shutdown()
        srv.server_close()
