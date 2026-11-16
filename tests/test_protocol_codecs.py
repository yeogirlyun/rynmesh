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
