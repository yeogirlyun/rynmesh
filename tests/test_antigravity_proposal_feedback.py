"""Strict AG proposal failures through real adapter, bridge and runtime paths."""
from __future__ import annotations

import json
import threading
import traceback
from types import SimpleNamespace

import pytest
from test_agent_runtime import make_runtime

from rynmesh.agent_runtime.bridge import AgentModelBridge
from rynmesh.agent_runtime.store import AgentModelResponseError
from rynmesh.llm_package import antigravity_session as session
from rynmesh.llm_package.adapters import AdapterError
from rynmesh.llm_package.cli_adapter import CLIAgentAdapter
from rynmesh.llm_package.codex_tools import FEEDBACK

PRIVATE = "AUTHORED_PRIVATE_PROPOSAL_SENTINEL_DO_NOT_COPY"


def invalid_text(kind):
    if kind == "bad_json":
        return "{" + PRIVATE
    proposal = ({"name": PRIVATE, "arguments": {"service_id": "svc1"}} if kind == "unknown_tool"
                else {"name": "inspect_service", "arguments": {"service_id": 123, PRIVATE: True}})
    return json.dumps({"content": "", "tool_calls": [proposal]})


def infer_result(text, *, input_tokens=11, output_tokens=3, finish_reason="stop"):
    return {"text": text, "message": {"role": "assistant", "content": text},
            "input_tokens": input_tokens, "output_tokens": output_tokens, "finish_reason": finish_reason}


def catalog_and_bridge():
    target = {"source": "local_service", "service_id": "ag-proposal-fixture", "adapter": "antigravity_cli",
              "streaming": True, "max_output_tokens": 1024, "context_window": 65536}
    adapter = CLIAgentAdapter("antigravity_cli", executable="must-not-launch")
    manager = SimpleNamespace(adapter=adapter, manifest=SimpleNamespace(timeout_seconds=10),
        _slots=threading.BoundedSemaphore(1), _lock=threading.Lock(), _running=0)
    catalog = SimpleNamespace(resolve=lambda model_id: target)
    return adapter, AgentModelBridge({"provider_for": lambda service_id: manager}, catalog), manager


def specs():
    return [{"type": "function", "function": {"name": "inspect_service", "parameters": {
        "type": "object", "properties": {"service_id": {"type": "string"}},
        "required": ["service_id"], "additionalProperties": False}}}]


@pytest.mark.parametrize("kind", ["bad_json", "unknown_tool", "schema"])
def test_invalid_proposal_real_adapter_is_strict_safe_typed_and_not_streamed(monkeypatch, kind):
    monkeypatch.setattr(session, "infer", lambda *args, **kwargs: infer_result(invalid_text(kind)))
    adapter, _, _ = catalog_and_bridge()
    events = []
    with pytest.raises(AdapterError) as info:
        adapter.chat({"messages": [{"role": "user", "content": "Inspect svc1"}], "tools": specs(),
                      "stream": True}, task_id="rejected-proposal", timeout_s=1, on_event=events.append)
    error = info.value
    assert error.code == "upstream_response_invalid"
    assert error.proposal_feedback_code == {"bad_json": "response_json", "unknown_tool": "tool_name", "schema": "arguments_schema_type"}[kind]
    assert error.reported_usage == {"input_tokens": 11, "output_tokens": 3} and error.finish_reason == "stop"
    assert error.__cause__ is None and error.__suppress_context__
    assert PRIVATE not in "".join(traceback.format_exception(error))
    assert events == [] and adapter.capabilities()["tool_execution"] == "origin_client"


@pytest.mark.parametrize("usage,finish,expected", [
    ({"input_tokens": 0, "output_tokens": 0}, "length", {"input_tokens": 0, "output_tokens": 0}),
    ({"input_tokens": True, "output_tokens": -1}, PRIVATE, {"input_tokens": None, "output_tokens": None}),
    ({"input_tokens": 7, "output_tokens": None}, None, {"input_tokens": 7, "output_tokens": None}),
])
def test_invalid_proposal_keeps_only_safe_real_usage_and_finish_metadata(monkeypatch, usage, finish, expected):
    monkeypatch.setattr(session, "infer", lambda *args, **kwargs: infer_result(invalid_text("bad_json"),
        **usage, finish_reason=finish))
    adapter, _, _ = catalog_and_bridge()
    with pytest.raises(AdapterError) as info:
        adapter.chat({"messages": [{"role": "user", "content": "Inspect"}], "tools": specs()}, task_id="usage", timeout_s=1)
    assert info.value.reported_usage == expected
    assert info.value.finish_reason == ("length" if finish == "length" else "unknown")


@pytest.mark.parametrize("value,choice,reason", [
    ({"tool_calls": []}, "auto", "response_shape"),
    ({"content": "", "tool_calls": {}, PRIVATE: True}, "auto", "response_shape"),
    ({"content": "", "tool_calls": {}}, "auto", "tool_calls"),
    ({"content": "", "tool_calls": []}, "required", "tool_calls"),
    ({"content": "", "tool_calls": [{"name": "inspect_service", "arguments": {}}] * 9}, "auto", "tool_calls"),
    ({"content": "", "tool_calls": [{"name": {PRIVATE: True}, "arguments": {}}]}, "auto", "tool_name"),
    ({"content": "", "tool_calls": [{"name": "inspect_service", "arguments": "{}"}]}, "auto", "arguments_object"),
    ({"content": "", "tool_calls": [{"name": "inspect_service", "arguments": []}]}, "auto", "arguments_object"),
    ({"content": "", "tool_calls": [{"name": "inspect_service", "arguments": {}}]}, "auto", "arguments_schema_required"),
    ({"content": "", "tool_calls": [{"name": "inspect_service", "arguments": {"service_id": "svc1", PRIVATE: True}}]}, "auto", "arguments_schema_additionalProperties"),
])
def test_rejection_category_is_exact_authored_and_contains_no_invalid_payload(monkeypatch, value, choice, reason):
    monkeypatch.setattr(session, "infer", lambda *args, **kwargs: infer_result(json.dumps(value)))
    adapter, _, _ = catalog_and_bridge()
    with pytest.raises(AdapterError) as info:
        adapter.chat({"messages": [{"role": "user", "content": "Inspect"}], "tools": specs(),
                      "tool_choice": choice}, task_id="category", timeout_s=1)
    error = info.value
    assert error.proposal_feedback_code == reason and reason in FEEDBACK
    assert PRIVATE not in "".join(traceback.format_exception(error))
    assert error.__cause__ is None and error.__suppress_context__


@pytest.mark.parametrize("choice,maximum,minimum,allowed", [
    ("auto", 8, 0, ["inspect_service"]),
    ("none", 0, 0, ["disabled"]),
    ("required", 8, 1, ["inspect_service"]),
    ({"type": "function", "function": {"name": "inspect_service"}}, 8, 1, ["inspect_service"]),
])
def test_provider_output_schema_is_enforced_without_registering_executable_tools(monkeypatch, choice, maximum, minimum, allowed):
    seen = []
    def fake_infer(adapter, prompt, **options):
        seen.append((json.loads(prompt.split("\n", 1)[1]), options))
        proposals = [] if choice in ("none", "auto") else [{"name": "inspect_service", "arguments": {"service_id": "svc1"}}]
        return infer_result(json.dumps({"content": "", "tool_calls": proposals}))
    monkeypatch.setattr(session, "infer", fake_infer)
    adapter, _, _ = catalog_and_bridge()
    adapter.chat({"messages": [{"role": "user", "content": "Inspect"}], "tools": specs(), "tool_choice": choice},
                 task_id="schema", timeout_s=1, model="gemini-4-argon-low")
    payload, options = seen[0]
    assert options["model"] == "gemini-4-argon-low"
    assert options["schema"] == payload["output_schema"]
    assert options["schema"]["additionalProperties"] is False
    proposals = options["schema"]["properties"]["tool_calls"]
    assert (proposals["maxItems"], proposals["minItems"]) == (maximum, minimum)
    assert proposals["items"]["properties"]["name"]["enum"] == allowed
    assert proposals["items"]["properties"]["arguments"] == {"type": "object"}
    assert set(options) == {"task_id", "timeout_s", "max_tokens", "schema", "model"}
    assert "tools" not in options


def test_later_invalid_proposal_cannot_emit_an_earlier_valid_call(monkeypatch):
    value = {"content": "", "tool_calls": [
        {"name": "inspect_service", "arguments": {"service_id": "svc1"}},
        {"name": PRIVATE, "arguments": {"service_id": "svc1"}},
    ]}
    monkeypatch.setattr(session, "infer", lambda *args, **kwargs: infer_result(json.dumps(value)))
    adapter, _, _ = catalog_and_bridge()
    events = []
    with pytest.raises(AdapterError) as info:
        adapter.chat({"messages": [{"role": "user", "content": "Inspect"}], "tools": specs(), "stream": True},
                     task_id="atomic", timeout_s=1, on_event=events.append)
    assert info.value.proposal_feedback_code == "tool_name" and events == []


@pytest.mark.parametrize("kind", ["bad_json", "unknown_tool", "schema"])
def test_bridge_exposes_bounded_safe_correction_instead_of_generic_inference_failure(monkeypatch, kind):
    monkeypatch.setattr(session, "infer", lambda *args, **kwargs: infer_result(invalid_text(kind)))
    _, bridge, manager = catalog_and_bridge()
    events = []
    with pytest.raises(AgentModelResponseError) as info:
        bridge.complete("local:test", [{"role": "user", "content": "Inspect"}], specs(), lambda: False, events.append)
    error = info.value
    assert str(error) == "agent_invalid_model_response"
    assert error.usage == {"input_tokens": 11, "output_tokens": 3} and error.finish_reason == "stop"
    assert "No proposed tool was executed" in error.detail
    assert PRIVATE not in error.detail and len(error.detail) < 300
    assert not any(event["type"] in {"tool_call_complete", "response_complete"} for event in events)
    assert manager._running == 0


@pytest.mark.parametrize("kind", ["bad_json", "unknown_tool", "schema"])
def test_runtime_safe_feedback_then_corrected_proposal_continues_without_replaying_side_effects(tmp_path, monkeypatch, kind):
    runtime, _, tools, _ = make_runtime(tmp_path)
    _, bridge, manager = catalog_and_bridge()
    runtime.model = bridge
    requests = []
    def fake_infer(adapter, prompt, **options):
        payload = json.loads(prompt.split("\n", 1)[1])
        requests.append(payload)
        assert "never execute tools here" in prompt
        assert options["schema"] == payload["output_schema"]
        assert options["schema"]["properties"]["tool_calls"]["items"]["properties"]["arguments"] == {"type": "object"}
        if len(requests) == 1:
            return infer_result(invalid_text(kind))
        if len(requests) == 2:
            wire = json.dumps(payload["messages"])
            assert "No proposed tool was executed" in wire and PRIVATE not in wire
            return infer_result(json.dumps({"content": "", "tool_calls": [
                {"name": "inspect_service", "arguments": {"service_id": "svc1"}}]}), input_tokens=13, output_tokens=4)
        assert payload["messages"][-1]["role"] == "tool"
        return infer_result(json.dumps({"content": "Inspection completed.", "tool_calls": []}), input_tokens=17, output_tokens=5)
    monkeypatch.setattr(session, "infer", fake_infer)
    row = runtime.create({"message": "Inspect svc1; do not change it."})
    runtime.run_once()
    rejected = runtime.store.read()["tasks"][row["id"]]
    assert rejected["status"] == "queued" and rejected["model_retry_count"] == 1
    assert not tools.calls and not tools.shared and not rejected.get("receipts") and rejected["queue"] == []
    assert rejected["budget"]["records"][0]["input_tokens"] == 11
    assert rejected["budget"]["records"][0]["output_tokens"] == 3
    assert any(event["type"] == "model_response_rejected" and event["error"] == "agent_invalid_model_response" for event in rejected["events"])
    for _ in range(3):
        runtime.run_once()
    saved = runtime.store.read()["tasks"][row["id"]]
    assert saved["status"] == "completed" and saved["error"] is None
    assert len(tools.calls) == 1 and tools.calls[0][0] == "inspect_service" and not tools.shared
    assert len(saved["receipts"]) == 1 and saved["receipts"][0]["status"] == "confirmed"
    assert len([message for message in saved["messages"] if message["role"] == "tool"]) == 1
    assert PRIVATE not in json.dumps(saved) and len(requests) == 3 and manager._running == 0
    models = [record for record in saved["budget"]["records"] if record["kind"] == "model"]
    assert [(record["input_tokens"], record["output_tokens"]) for record in models] == [(11, 3), (13, 4), (17, 5)]
