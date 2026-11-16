"""Tests for S05-I4: Optional freeform apply_patch over OpenAI Responses custom tools."""
from __future__ import annotations

import copy
import json

import pytest

from rynmesh.agent_runtime import local_patch, local_specs, model_profiles
from rynmesh.agent_runtime.local_specs import SPECS
from rynmesh.agent_runtime.model_events import ModelEventStream
from rynmesh.agent_runtime.tool_router import normalize_tool_calls
from rynmesh.llm_package.chat import ChatAccumulator, validate_chat
from rynmesh.llm_package.protocol_codecs import (
    MESSAGES,
    RESPONSES,
    StreamDecoder,
    make_request,
    parse_response,
)

SCOPE = "a" * 64
OTHER_SCOPE = "b" * 64
MODEL = "gpt-5-codex"


def _tool_spec(name: str, **extra_fn) -> dict:
    desc, _effect, _risk, params = SPECS[name]
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": desc,
            "parameters": copy.deepcopy(params),
            **extra_fn,
        },
    }


def test_t1_custom_tool_definition_encoding_and_model_profile_gate():
    """T1: custom 工具定义编码、Lark 语法常量、路由档案 custom_tools 能力位门控与 chat 路由保持 JSON（覆盖改动 1、4、5）。"""
    grammar = local_specs.APPLY_PATCH_LARK_GRAMMAR
    assert isinstance(grammar, str) and "*** Begin Patch" in grammar and "*** End Patch" in grammar
    assert "*** Add File: " in grammar and "*** Delete File: " in grammar and "*** Update File: " in grammar
    assert local_specs.FREEFORM_GRAMMARS["ryn_apply_patch"] == grammar
    # SPECS 仍保持 4 元组且 JSON Schema 不变，保证 chat 路由保持 JSON
    assert len(SPECS["ryn_apply_patch"]) == 4
    validated_chat = validate_chat(
        {
            "messages": [{"role": "user", "content": "patch it"}],
            "tools": [_tool_spec("ryn_apply_patch")],
        }
    )
    assert validated_chat["tools"][0]["type"] == "function"
    assert "freeform_grammar" not in validated_chat["tools"][0]["function"]

    # model_profiles.Profile 默认 custom_tools=False；仅已验证的 OpenAI Responses 源打开
    assert model_profiles.PROFILES["generic"].custom_tools is False
    assert model_profiles.PROFILES["codex-cli"].custom_tools is False
    assert model_profiles.resolve(None)["custom_tools"] is False
    assert model_profiles.resolve({"adapter": "openai_compatible", "provider": "openai", "tool_support": "supported"})["custom_tools"] is False
    assert model_profiles.resolve({"adapter": "anthropic_messages", "provider": "anthropic", "tool_support": "supported"})["custom_tools"] is False
    assert model_profiles.resolve({"adapter": "codex_cli", "tool_support": "supported"})["custom_tools"] is False
    # 第三方 Responses 兼容端点或未验证工具能力的 OpenAI 源保持关闭
    assert model_profiles.resolve(
        {"source": "local_api", "adapter": "openai_responses", "provider": "custom", "base_url": "https://proxy.example/v1", "tool_support": "supported"}
    )["custom_tools"] is False
    assert model_profiles.resolve(
        {"source": "local_api", "adapter": "openai_responses", "provider": "openai", "tool_support": "unknown"}
    )["custom_tools"] is False
    # 已验证的 OpenAI Responses 源打开，且显式 override="generic" 可回滚关闭
    verified_openai = {
        "id": "local/openai-gpt5",
        "source": "local_api",
        "adapter": "openai_responses",
        "provider": "openai",
        "base_url": "https://api.openai.com/v1",
        "tool_support": "supported",
    }
    verified_profile = model_profiles.resolve(verified_openai)
    assert verified_profile["custom_tools"] is True
    assert model_profiles.resolve(verified_openai, override="generic")["custom_tools"] is False

    req_body = {
        "messages": [{"role": "user", "content": "Update hello.py"}],
        "tools": [_tool_spec("ryn_apply_patch"), _tool_spec("ryn_file_read")],
        "max_tokens": 512,
    }
    wire = make_request(req_body, RESPONSES, MODEL, SCOPE, profile=verified_profile)
    patch_def, read_def = wire["tools"]
    assert patch_def == {
        "type": "custom",
        "name": "ryn_apply_patch",
        "description": SPECS["ryn_apply_patch"][0],
        "format": {
            "type": "grammar",
            "syntax": "lark",
            "definition": grammar,
        },
    }
    assert read_def["type"] == "function"
    assert read_def["name"] == "ryn_file_read"
    assert "parameters" in read_def

    # 显式传入 freeform_grammar 的工具在 custom_tools=True 时同样编码为 custom，在 MESSAGES 上仍保持常规 schema
    explicit_tool = {
        "type": "function",
        "function": {
            "name": "custom_patch",
            "description": "Custom patch tool",
            "parameters": {"type": "object", "properties": {"patch": {"type": "string"}}, "required": ["patch"]},
            "freeform_grammar": grammar,
        },
    }
    wire_explicit = make_request(
        {"messages": [{"role": "user", "content": "hi"}], "tools": [explicit_tool]},
        RESPONSES,
        MODEL,
        SCOPE,
        custom_tools=True,
    )
    assert wire_explicit["tools"][0] == {
        "type": "custom",
        "name": "custom_patch",
        "description": "Custom patch tool",
        "format": {"type": "grammar", "syntax": "lark", "definition": grammar},
    }
    wire_messages = make_request(
        {"messages": [{"role": "user", "content": "hi"}], "tools": [explicit_tool]},
        MESSAGES,
        MODEL,
        SCOPE,
        custom_tools=True,
    )
    assert "input_schema" in wire_messages["tools"][0]
    assert "format" not in wire_messages["tools"][0]


def test_t2_custom_tool_call_non_streaming_and_streaming_decoding():
    """T2: custom_tool_call 流式/非流式解码成 {'patch': ...} 并带 wire_kind='custom'（覆盖改动 2）。"""
    raw_patch = (
        "*** Begin Patch\n"
        "*** Add File: src/hello.py\n"
        "+def greet() -> str:\n"
        '+    return "你好 \\"Ryn\\""\n'
        "*** End Patch\n"
    )
    native_resp = {
        "id": "resp_custom_1",
        "status": "completed",
        "output": [
            {"type": "reasoning", "id": "rs_1", "summary": [], "encrypted_content": "ENC_DATA"},
            {
                "type": "custom_tool_call",
                "id": "ctc_1",
                "call_id": "call_patch_1",
                "name": "ryn_apply_patch",
                "input": raw_patch,
                "status": "completed",
            },
        ],
        "usage": {"input_tokens": 25, "output_tokens": 18},
    }

    # 1) 非流式解码
    parsed = parse_response(native_resp, RESPONSES, MODEL, SCOPE)
    assert parsed["finish_reason"] == "tool_calls"
    calls = parsed["message"]["tool_calls"]
    assert len(calls) == 1
    assert calls[0]["id"] == "call_patch_1"
    assert calls[0]["type"] == "function"
    assert calls[0]["wire_kind"] == "custom"
    assert calls[0]["function"]["name"] == "ryn_apply_patch"
    assert json.loads(calls[0]["function"]["arguments"]) == {"patch": raw_patch}

    # 校验内部工具路由 normalize_tool_calls 与 local_patch.parse 可无缝接收该调用
    routed = normalize_tool_calls(calls, [_tool_spec("ryn_apply_patch")])
    assert len(routed) == 1 and routed[0].get("error_result") is None
    assert routed[0]["arguments"] == {"patch": raw_patch}
    ops = local_patch.parse(routed[0]["arguments"]["patch"])
    assert len(ops) == 1 and ops[0]["kind"] == "add" and ops[0]["path"] == "src/hello.py"

    # 2) 流式解码（含 custom_tool_call_input.delta / .done，并验证与 ModelEventStream、ChatAccumulator 兼容）
    mid = len(raw_patch) // 2
    events = [
        {"type": "response.created", "response": {"id": "resp_custom_1", "status": "in_progress"}},
        {"type": "response.output_item.added", "output_index": 0, "item": native_resp["output"][0]},
        {"type": "response.output_item.done", "output_index": 0, "item": native_resp["output"][0]},
        {
            "type": "response.output_item.added",
            "output_index": 1,
            "item": {**native_resp["output"][1], "input": "", "status": "in_progress"},
        },
        {
            "type": "response.custom_tool_call_input.delta",
            "output_index": 1,
            "item_id": "ctc_1",
            "delta": raw_patch[:mid],
        },
        {
            "type": "response.custom_tool_call_input.delta",
            "output_index": 1,
            "item_id": "ctc_1",
            "delta": raw_patch[mid:],
        },
        {
            "type": "response.custom_tool_call_input.done",
            "output_index": 1,
            "item_id": "ctc_1",
            "input": raw_patch,
        },
        {"type": "response.output_item.done", "output_index": 1, "item": native_resp["output"][1]},
        {"type": "response.completed", "response": native_resp},
    ]

    decoder = StreamDecoder(RESPONSES, MODEL, SCOPE)
    observed_events = []
    event_stream = ModelEventStream(observed_events.append)
    accumulator = ChatAccumulator()
    for ev in events[:-1]:
        chunks = decoder.feed(ev)
        assert not any(c.get("choices", [{}])[0].get("delta", {}).get("tool_calls") for c in chunks if c.get("choices"))
        for chunk in chunks:
            event_stream.feed(chunk)
            accumulator.add(chunk)
    for chunk in decoder.feed(events[-1]):
        event_stream.feed(chunk)
        accumulator.add(chunk)

    stream_final = decoder.result()
    assert stream_final == parsed
    event_stream.finish(stream_final)
    completed_calls = [e["call"] for e in observed_events if e["type"] == "tool_call_complete"]
    assert completed_calls == calls
    assert accumulator.calls[0]["wire_kind"] == "custom"
    assert json.loads(accumulator.calls[0]["function"]["arguments"]) == {"patch": raw_patch}

    # 终态篡改流式已发出的 input 必须报错
    tampered_decoder = StreamDecoder(RESPONSES, MODEL, SCOPE)
    for ev in events[:-1]:
        tampered_decoder.feed(ev)
    bad_terminal = copy.deepcopy(events[-1])
    bad_terminal["response"]["output"][1]["input"] = "*** Begin Patch\n*** Delete File: wrong.py\n*** End Patch\n"
    with pytest.raises(ValueError, match="arguments changed"):
        tampered_decoder.feed(bad_terminal)


def test_t3_history_replay_emits_custom_tool_call_and_custom_tool_call_output():
    """T3: 含 custom 调用的历史回放类型正确（custom_tool_call + custom_tool_call_output，旧调用保持 function_call）（覆盖改动 3）。"""
    raw_patch = "*** Begin Patch\n*** Delete File: obsolete.txt\n*** End Patch\n"
    native_resp = {
        "id": "resp_hist_1",
        "status": "completed",
        "output": [
            {
                "type": "custom_tool_call",
                "id": "ctc_hist_1",
                "call_id": "call_custom_1",
                "name": "ryn_apply_patch",
                "input": raw_patch,
                "status": "completed",
            },
            {
                "type": "function_call",
                "id": "fc_hist_2",
                "call_id": "call_fn_2",
                "name": "ryn_file_read",
                "arguments": '{"path":"README.md"}',
                "status": "completed",
            },
        ],
        "usage": {"input_tokens": 12, "output_tokens": 9},
    }
    parsed = parse_response(native_resp, RESPONSES, MODEL, SCOPE)
    assistant_msg = parsed["message"]
    assert assistant_msg["tool_calls"][0]["wire_kind"] == "custom"
    assert "wire_kind" not in assistant_msg["tool_calls"][1]

    history = [
        {"role": "user", "content": "Clean obsolete.txt and read README.md"},
        assistant_msg,
        {"role": "tool", "tool_call_id": "call_custom_1", "content": '{"ok":true}'},
        {"role": "tool", "tool_call_id": "call_fn_2", "content": '{"content":"hello"}'},
        {"role": "user", "content": "Summarize"},
    ]

    # 1) 同 scope 下带 provider_state 续接回放
    wire_same_scope = make_request(
        {"messages": history, "tools": [_tool_spec("ryn_apply_patch"), _tool_spec("ryn_file_read")]},
        RESPONSES,
        MODEL,
        SCOPE,
        custom_tools=True,
    )
    items_same = wire_same_scope["input"]
    assert items_same[1]["type"] == "custom_tool_call"
    assert items_same[1]["call_id"] == "call_custom_1"
    assert items_same[1]["input"] == raw_patch
    assert items_same[2]["type"] == "function_call"
    assert items_same[2]["call_id"] == "call_fn_2"
    assert items_same[3] == {"type": "custom_tool_call_output", "call_id": "call_custom_1", "output": '{"ok":true}'}
    assert items_same[4] == {"type": "function_call_output", "call_id": "call_fn_2", "output": '{"content":"hello"}'}

    # 2) 无 provider_state（或跨 scope）按公开历史 wire_kind 回放
    stripped_assistant = {k: copy.deepcopy(v) for k, v in assistant_msg.items() if k != "provider_state"}
    public_history = [
        history[0],
        stripped_assistant,
        history[2],
        history[3],
        history[4],
    ]
    wire_public = make_request(
        {"messages": public_history, "tools": [_tool_spec("ryn_apply_patch"), _tool_spec("ryn_file_read")]},
        RESPONSES,
        MODEL,
        OTHER_SCOPE,
        custom_tools=True,
    )
    items_pub = wire_public["input"]
    assert items_pub[1] == {
        "type": "custom_tool_call",
        "call_id": "call_custom_1",
        "name": "ryn_apply_patch",
        "input": raw_patch,
    }
    assert items_pub[2] == {
        "type": "function_call",
        "call_id": "call_fn_2",
        "name": "ryn_file_read",
        "arguments": '{"path":"README.md"}',
    }
    assert items_pub[3] == {"type": "custom_tool_call_output", "call_id": "call_custom_1", "output": '{"ok":true}'}
    assert items_pub[4] == {"type": "function_call_output", "call_id": "call_fn_2", "output": '{"content":"hello"}'}


def test_t4_disabled_capability_is_byte_identical_to_baseline():
    """T4: 能力位关闭时与现在字节完全一致（覆盖改动 1、4、5）。"""
    history = [
        {"role": "developer", "content": "System rules"},
        {"role": "user", "content": "Apply patch"},
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {
                    "id": "call_legacy_1",
                    "type": "function",
                    "function": {
                        "name": "ryn_apply_patch",
                        "arguments": '{"patch":"*** Begin Patch\\n*** Delete File: a.txt\\n*** End Patch\\n"}',
                    },
                }
            ],
        },
        {"role": "tool", "tool_call_id": "call_legacy_1", "content": '{"ok":true}'},
        {"role": "user", "content": "Next"},
    ]
    req = {
        "messages": history,
        "tools": [_tool_spec("ryn_apply_patch"), _tool_spec("ryn_file_read")],
        "tool_choice": "auto",
        "max_tokens": 1024,
        "reasoning_effort": "medium",
        "prompt_cache_key": "task:abc123",
    }

    expected_baseline = {
        "model": MODEL,
        "stream": False,
        "max_output_tokens": 1024,
        "tools": [
            {
                "name": "ryn_apply_patch",
                "parameters": copy.deepcopy(SPECS["ryn_apply_patch"][3]),
                "description": SPECS["ryn_apply_patch"][0],
                "type": "function",
                "strict": False,
            },
            {
                "name": "ryn_file_read",
                "parameters": copy.deepcopy(SPECS["ryn_file_read"][3]),
                "description": SPECS["ryn_file_read"][0],
                "type": "function",
                "strict": False,
            },
        ],
        "tool_choice": "auto",
        "input": [
            {"role": "developer", "content": [{"type": "input_text", "text": "System rules"}]},
            {"role": "user", "content": [{"type": "input_text", "text": "Apply patch"}]},
            {
                "type": "function_call",
                "call_id": "call_legacy_1",
                "name": "ryn_apply_patch",
                "arguments": '{"patch":"*** Begin Patch\\n*** Delete File: a.txt\\n*** End Patch\\n"}',
            },
            {"type": "function_call_output", "call_id": "call_legacy_1", "output": '{"ok":true}'},
            {"role": "user", "content": [{"type": "input_text", "text": "Next"}]},
        ],
        "store": False,
        "include": ["reasoning.encrypted_content"],
        "reasoning": {"effort": "medium"},
        "prompt_cache_key": "task:abc123",
    }

    def dump_bytes(obj: dict) -> bytes:
        return json.dumps(obj, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")

    default_wire = make_request(copy.deepcopy(req), RESPONSES, MODEL, SCOPE)
    explicit_false_wire = make_request(copy.deepcopy(req), RESPONSES, MODEL, SCOPE, custom_tools=False)
    unverified_profile = model_profiles.resolve({"source": "local_api", "adapter": "openai_responses", "model_id": MODEL})
    assert unverified_profile["custom_tools"] is False
    profile_false_wire = make_request(copy.deepcopy(req), RESPONSES, MODEL, SCOPE, profile=unverified_profile)

    baseline_bytes = dump_bytes(expected_baseline)
    assert dump_bytes(default_wire) == baseline_bytes
    assert dump_bytes(explicit_false_wire) == baseline_bytes
    assert dump_bytes(profile_false_wire) == baseline_bytes


def test_t5_real_protocol_adapter_and_agent_runtime_custom_patch_round_trip(tmp_path, monkeypatch):
    """返工补充：经真实 SourceStore.adapter -> NativeProtocolAdapter -> AgentRuntime 假 HTTP 层闭环，验证能力位开关、agent 历史保留 wire_kind='custom'、下一轮回放及关闭时字节不变。"""
    import threading
    from types import SimpleNamespace

    from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey

    from rynmesh.agent_runtime.bridge import AgentModelBridge
    from rynmesh.agent_runtime.local_tools import LocalTools
    from rynmesh.agent_runtime.models import ModelCatalog
    from rynmesh.agent_runtime.operation_ledger import OperationLedger
    from rynmesh.agent_runtime.runtime import AgentRuntime
    from rynmesh.agent_runtime.store import AgentStore
    from rynmesh.llm_package import model_settings, protocol_adapter
    from rynmesh.llm_package.protocol_adapter import NativeProtocolAdapter
    from rynmesh.llm_package.sources import SourceStore

    class _FakeHttpResponse:
        def __init__(self, body_dict: dict):
            self.body = json.dumps(body_dict).encode("utf-8")
            self.headers = {"content-type": "application/json"}

        def read(self, _limit=None):
            return self.body

        def __enter__(self):
            return self

        def __exit__(self, *_):
            return False

    raw_patch = (
        "*** Begin Patch\n"
        "*** Update File: notes.txt\n"
        "@@\n"
        "-alpha\n"
        "+beta\n"
        "*** End Patch\n"
    )
    captured_requests: list[bytes] = []
    turn_replies = [
        {
            "id": "resp_e2e_1",
            "status": "completed",
            "output": [
                {
                    "type": "custom_tool_call",
                    "id": "ctc_e2e_1",
                    "call_id": "call_e2e_patch_1",
                    "name": "ryn_apply_patch",
                    "input": raw_patch,
                    "status": "completed",
                }
            ],
            "usage": {"input_tokens": 30, "output_tokens": 20},
        },
        {
            "id": "resp_e2e_2",
            "status": "completed",
            "output": [
                {
                    "type": "message",
                    "id": "msg_e2e_2",
                    "role": "assistant",
                    "status": "completed",
                    "content": [{"type": "output_text", "text": "Updated notes.txt to beta."}],
                }
            ],
            "usage": {"input_tokens": 45, "output_tokens": 10},
        },
    ]

    def _fake_urlopen(request, timeout=None):
        captured_requests.append(bytes(request.data))
        return _FakeHttpResponse(turn_replies.pop(0))

    monkeypatch.setattr(protocol_adapter, "model_urlopen", _fake_urlopen)

    # 1) 能力位关闭（第三方 Responses 源 / 未验证源）经 NativeProtocolAdapter 请求字节完全不变
    off_req = {
        "messages": [{"role": "user", "content": "Edit notes.txt"}],
        "tools": [_tool_spec("ryn_apply_patch"), _tool_spec("ryn_file_read")],
        "max_tokens": 256,
    }
    off_adapter = NativeProtocolAdapter(
        protocol="openai_responses",
        base_url="https://proxy.example/v1",
        provider="custom",
        model="gpt-5-codex",
        api_key="sk-test",
        allow_non_loopback=True,
    )
    turn_replies.insert(
        0,
        {
            "id": "resp_off",
            "status": "completed",
            "output": [
                {
                    "type": "message",
                    "id": "msg_off",
                    "role": "assistant",
                    "status": "completed",
                    "content": [{"type": "output_text", "text": "ok"}],
                }
            ],
            "usage": {"input_tokens": 10, "output_tokens": 2},
        },
    )
    off_adapter.chat(copy.deepcopy(off_req), task_id="task_off", timeout_s=10)
    off_sent_bytes = captured_requests.pop(0)
    expected_off_payload = make_request(
        copy.deepcopy(off_req),
        RESPONSES,
        "gpt-5-codex",
        off_adapter._scope(),
        prompt_cache="auto",
        custom_tools=False,
    )
    assert off_sent_bytes == json.dumps(expected_off_payload).encode("utf-8")

    # 2) 已验证的 OpenAI Responses 源：经 SourceStore.adapter -> NativeProtocolAdapter -> AgentRuntime 全链路运行
    workspace = tmp_path / "ws"
    workspace.mkdir()
    (workspace / "notes.txt").write_text("alpha\n", encoding="utf-8")

    source_record = {
        "id": "src-openai",
        "name": "OpenAI Official",
        "kind": "api",
        "provider": "openai",
        "protocol": "openai_responses",
        "base_url": "https://api.openai.com/v1",
        "api_key": "sk-fixture",
        "enabled": True,
        "status": "ready",
        "destination_revision": "rev-openai-1",
        "models": ["gpt-5-codex"],
        "tool_support": {"gpt-5-codex": "supported"},
        "model_metadata": {},
        "model_settings": {},
    }
    real_adapter = SourceStore.adapter(source_record, "gpt-5-codex")
    svc_id = "api-src-openai-gpt-5-codex"
    model_id = "local/" + svc_id

    source_store = SimpleNamespace(
        read=lambda: {"src-openai": copy.deepcopy(source_record)},
        lock=threading.Lock(),
        model_service_id=lambda record, model: svc_id,
        shared_models=lambda record: [],
        path=tmp_path / "sources.json",
    )
    managers = {
        svc_id: SimpleNamespace(
            manifest=SimpleNamespace(timeout_seconds=60),
            adapter=real_adapter,
            _slots=threading.BoundedSemaphore(1),
            _lock=threading.Lock(),
            _running=0,
            _source_revision=model_settings.fingerprint(source_record, "gpt-5-codex"),
        )
    }
    discovered = [
        {
            "peer_id": "node-local",
            "node_name": "Local",
            "online": True,
            "ready": True,
            "chat_protocol": "rynmesh.chat.v1",
            "service": {
                "package_id": svc_id,
                "adapter": "openai_compatible",
                "runtime": "external",
                "model_alias": "gpt-5-codex",
                "capabilities": ["client-tools"],
            },
        }
    ]
    services = {
        "source_store": source_store,
        "discover": lambda _net: copy.deepcopy(discovered),
        "provider_for": lambda sid: managers.get(sid),
    }
    node_store = SimpleNamespace(
        peer_id="node-local",
        personal_space=SimpleNamespace(allows_ai=lambda _peer: False),
        llm_tool_capabilities=SimpleNamespace(support=lambda _manifest, _model: "supported"),
    )
    catalog = ModelCatalog(services, node_store)
    assert model_profiles.resolve(catalog.get_model(model_id))["custom_tools"] is True

    agent_store = AgentStore(tmp_path / "agent", X25519PrivateKey.generate())

    class _LocalToolBridge:
        def __init__(self, st, home_dir):
            ledger = OperationLedger(home_dir)
            self.local = LocalTools(SimpleNamespace(home=home_dir), lambda: ledger)
            self.local.store = st

        def specs(self):
            return [_tool_spec("ryn_apply_patch"), _tool_spec("ryn_file_read")]

        def metadata(self, name):
            desc, eff, risk, _ = SPECS[name]
            return {
                "effect": eff,
                "risk": risk,
                "title": desc.split(".")[0],
                "scope_context_supported": True,
                "supports_preview": eff == "write",
                "preview_precondition": eff == "write",
            }

        def preview(self, name, arguments, *, context=None):
            return self.local.preview(name, arguments, context or {})

        def prepare_scope(self, selection, *, owner_id, consumer_node_id, model_destination):
            return {"resource_grants": [], "egress_grants": []}

        def validate_context(self, context, *, cheap_only=False):
            return None

        def execute(self, name, arguments, *, operation_id, cancelled, context=None, expected_preview=None):
            return self.local.execute(
                name,
                arguments,
                operation_id=operation_id,
                context=dict(context or {}),
                expected_preview=expected_preview,
                cancelled=cancelled,
            )

        def verification_specs(self):
            return []

        def verify(self, receipt, criteria, *, context, cancelled):
            return {"status": "unknown", "evidence": {}, "checked_at": "fixed"}

    runtime = AgentRuntime(
        agent_store,
        AgentModelBridge(services, catalog),
        _LocalToolBridge(agent_store, tmp_path / "agent"),
        catalog,
        owner_id="owner-local",
        consumer_node_id="node-local",
    )
    runtime.update_settings(
        {
            "enabled": True,
            "workspace_root": str(workspace),
            "permission_mode": "automatic",
            "model_id": model_id,
        }
    )
    created = runtime.create({"message": "Change alpha to beta in notes.txt", "model_id": model_id})

    # 第 1 步模型请求：发出 custom 工具，接收 custom_tool_call，agent 历史写入 wire_kind="custom"
    runtime.run_once()
    assert len(captured_requests) == 1
    step1_payload = json.loads(captured_requests[0].decode("utf-8"))
    patch_tool_step1 = next(t for t in step1_payload["tools"] if t["name"] == "ryn_apply_patch")
    assert patch_tool_step1 == {
        "type": "custom",
        "name": "ryn_apply_patch",
        "description": SPECS["ryn_apply_patch"][0],
        "format": {
            "type": "grammar",
            "syntax": "lark",
            "definition": local_specs.APPLY_PATCH_LARK_GRAMMAR,
        },
    }

    stored_after_step1 = runtime.store.read()["tasks"][created["id"]]
    assistant_in_history = next(
        m for m in stored_after_step1["messages"] if m.get("role") == "assistant" and m.get("tool_calls")
    )
    assert assistant_in_history["tool_calls"][0]["wire_kind"] == "custom"

    # 工具执行轮次 + 后续模型轮次：模拟去掉 provider_state 以同时检验仅靠公开历史 wire_kind="custom" 的回放
    def _drop_provider_state(data):
        for msg in data["tasks"][created["id"]]["messages"]:
            msg.pop("provider_state", None)

    runtime.store.transaction(_drop_provider_state)

    for _ in range(4):
        runtime.run_once()
        if runtime.get(created["id"])["status"] in {"completed", "failed"}:
            break

    assert runtime.get(created["id"])["status"] == "completed"
    assert (workspace / "notes.txt").read_text(encoding="utf-8") == "beta\n"
    assert len(captured_requests) == 2
    step2_payload = json.loads(captured_requests[1].decode("utf-8"))
    replay_call = next(item for item in step2_payload["input"] if item.get("type") == "custom_tool_call")
    replay_out = next(item for item in step2_payload["input"] if item.get("type") == "custom_tool_call_output")
    assert replay_call == {
        "type": "custom_tool_call",
        "call_id": "call_e2e_patch_1",
        "name": "ryn_apply_patch",
        "input": raw_patch,
    }
    assert replay_out["call_id"] == "call_e2e_patch_1"
    assert json.loads(replay_out["output"])["applied"] is True
