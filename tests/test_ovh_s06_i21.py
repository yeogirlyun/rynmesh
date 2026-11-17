"""S06-I21: generic inline previews through the actual result publication paths."""
import copy
import json

import pytest
from test_agent_runtime import advance, assistant, make_runtime

from rynmesh.agent_runtime import results
from rynmesh.agent_runtime.protocol import hydrated_events
from rynmesh.agent_runtime.store import encoded


HEAD, TAIL = "BEGIN_INLINE_RESULT", "END_INLINE_RESULT"


def payload(body="x" * 30000):
    return {"status": "ok", "count": 42, "ready": True, "next": None,
            "items": [{"text": HEAD + body + TAIL}]}


def reference(value):
    return {"id": "result_" + "a" * 32, "sha256": "b" * 64, "bytes": len(encoded(value))}


@pytest.mark.parametrize("tool", ["ryn_browser_read", "ryn_web_search", "ryn_list_plugins",
                                  "ryn_craft_read", "plugin_fixture_read", "ryn_get_agent_result"])
def test_t1_t2_all_generic_dict_tools_get_bounded_head_tail(tool):
    value = payload()
    original = copy.deepcopy(value)
    ref = reference(value)
    out = results.format_reference_result(tool, value, ref, max_bytes=2000)
    assert out["result_ref"] == ref["id"] and out["output_sha256"] == ref["sha256"]
    assert out["bytes"] == ref["bytes"] and out["truncated_inline"] is True
    assert {key: out[key] for key in ("status", "count", "ready", "next")} == {
        "status": "ok", "count": 42, "ready": True, "next": None}
    assert HEAD in out["structured_preview"] and TAIL in out["structured_preview"]
    assert "chars truncated" in out["structured_preview"]
    assert len(encoded(out)) <= 2000 and value == original


@pytest.mark.parametrize("body", ["中文😀" * 10000, '"\\\n\t' * 10000], ids=["utf8", "json-escaping"])
def test_generic_preview_accounts_for_utf8_and_json_escaping(body):
    value = payload(body)
    out = results.format_reference_result("ryn_browser_read", value, reference(value), max_bytes=1800)
    assert HEAD in out["structured_preview"] and TAIL in out["structured_preview"]
    assert len(encoded(out)) <= 1800


@pytest.mark.parametrize("value", [[HEAD, "x" * 30000, TAIL], HEAD + "x" * 30000 + TAIL], ids=["list", "string"])
def test_generic_list_and_string_use_preview_field(value):
    out = results.format_reference_result("plugin_fixture_read", value, reference(value), max_bytes=2000)
    assert HEAD in out["preview"] and TAIL in out["preview"]
    assert "structured_preview" not in out and out["truncated_inline"] is True
    assert len(encoded(out)) <= 2000


@pytest.mark.parametrize("error", ["agent_tool_unavailable", "error-detail-" * 20])
def test_t3_error_is_kept_even_when_longer_than_normal_scalar_limit(error):
    value = {**payload(), "error": error, "detail": "D" * 201, "short": "S" * 200}
    out = results.format_reference_result("ryn_browser_read", value, reference(value), max_bytes=2400)
    assert out["error"] == error and out["short"] == value["short"]
    assert "detail" not in out and '"detail"' in out["structured_preview"]
    assert HEAD in out["structured_preview"] and TAIL in out["structured_preview"]
    assert len(encoded(out)) <= 2400


def test_t4_read_result_keeps_reference_only():
    value = payload()
    ref = reference(value)
    out = results.format_reference_result("ryn_read_result", value, ref, max_bytes=2000)
    assert set(out) == {"result_ref", "output_sha256", "bytes", "note"}
    assert out["result_ref"] == ref["id"]


@pytest.mark.parametrize("tool", ["ryn_file_read", "mcp__fixture__read"])
def test_t5_existing_file_and_mcp_shapes_are_preserved(tool):
    value = {"status": "ok", "text": HEAD + "x" * 30000 + TAIL}
    if tool.startswith("mcp__"):
        value["structuredContent"] = {"count": 7, "items": ["y" * 12000]}
    ref = reference(value)
    out = results.format_reference_result(tool, value, ref, max_bytes=4000)
    expected = {"status", "text", "result_ref", "output_sha256", "bytes", "note", "truncated_inline"}
    if tool.startswith("mcp__"):
        expected |= {"structuredContent", "structured_preview"}
        assert out["structuredContent"] == {"count": 7}
        assert out["structured_preview"].startswith('{"count":7,"items":[')
    assert set(out) == expected and out["result_ref"] == ref["id"]
    assert HEAD in out["text"] and TAIL in out["text"]
    assert len(encoded(out)) <= 4000


@pytest.mark.parametrize("tool", ["mcp__fixture__read", "ryn_computer_read", "ryn_file_read"])
def test_t5_existing_specialized_budget_fallback_is_preserved(tool):
    value = {"text": "x" * 30000, "padding": "y" * 30000}
    out = results.format_reference_result(tool, value, reference(value), max_bytes=2000)
    assert set(out) == {"result_ref", "output_sha256", "bytes", "note"}


class PreviewTools:
    def __init__(self):
        self.calls = []

    def specs(self):
        return [{"type": "function", "function": {"name": name, "description": name,
                 "parameters": {"type": "object", "properties": {}, "additionalProperties": False}}}
                for name in ("ryn_list_plugins", "ryn_web_search")]

    def metadata(self, name):
        return {"effect": "read", "supports_parallel": True}

    def execute(self, name, arguments, **kwargs):
        self.calls.append(name)
        return {**payload(), "error": "agent_fixture_error"}


@pytest.mark.parametrize("mode", ["single", "batch", "stream"])
def test_real_publication_paths_preview_and_keep_full_encrypted_result(tmp_path, mode):
    runtime, model, _, _ = make_runtime(tmp_path)
    runtime.tools = PreviewTools()
    names = ["ryn_list_plugins", "ryn_web_search"] if mode == "batch" else ["ryn_web_search"]
    calls = [assistant(name=name, call_id=f"call_{index}")["message"]["tool_calls"][0]
             for index, name in enumerate(names)]
    response = {"message": {"role": "assistant", "content": "", "tool_calls": calls}}
    if mode == "stream":
        def complete(model_id, messages, tool_specs, cancelled, on_event):
            on_event({"type": "tool_call_complete", "index": 0, "call": calls[0]})
            return response
        model.complete = complete
    else:
        model.responses = [response]
    task_id = runtime.create({"message": "Read the fixture result"})["id"]
    runtime.store.transaction(lambda data: data["tasks"][task_id]["limits"].update(max_output_chars=2000))
    advance(runtime, 1 if mode == "stream" else 2)
    row = runtime.store.read()["tasks"][task_id]
    events = [item for item in hydrated_events(row, row["events"]) if item["type"] == "tool_result"]
    assert len(events) == len(names) and sorted(runtime.tools.calls) == sorted(names), events
    if mode == "stream":
        assert events[0]["streamed"] is True
    elif mode == "batch":
        assert events[1]["timing"]["batch_shared"] is True
    for item in events:
        out = item["result"]
        assert HEAD in out["structured_preview"] and TAIL in out["structured_preview"]
        assert out["error"] == "agent_fixture_error" and len(encoded(out)) <= 2000
        full, ref = results.read(runtime.store, row, out["result_ref"])
        assert full == {**payload(), "error": "agent_fixture_error"}
        assert ref["tool"] == item["tool"] and ref["bytes"] > 30000
    messages = [json.loads(item["content"]) for item in row["messages"] if item["role"] == "tool"]
    assert len(messages) == len(names)
    assert all(HEAD in item["structured_preview"] and TAIL in item["structured_preview"] for item in messages)
