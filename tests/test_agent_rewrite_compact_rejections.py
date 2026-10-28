"""Rejected call history remains usable through actual run_once compaction."""
import copy
import json

import pytest
from test_agent_runtime import assistant, make_runtime

from rynmesh.agent_runtime.store import encoded

CONTEXT_LIMIT = 12000
LITERAL_JSON = '{"keep":"\\n and \\\"quotes\\\" remain business text"}'
BAD_ARGUMENTS = '{"page": 0, "literal_json": "unterminated'
DEEP_ARGUMENTS = '{"page":' + "[" * 1100 + "0" + "]" * 1099 + ',"literal_json":""}'


def request_size(messages, specs):
    return len(encoded(messages)) + len(encoded(specs))


class Pages:
    def __init__(self):
        self.calls = []
        self.body = ""

    def specs(self):
        return [{"type": "function", "function": {"name": "ryn_read_resource", "description": "Read an authored page.",
            "parameters": {"type": "object", "properties": {"page": {"type": "integer"},
                "literal_json": {"type": "string"}}, "required": ["page", "literal_json"],
                "additionalProperties": False}}}]

    def metadata(self, name):
        assert name == "ryn_read_resource"
        return {"effect": "read", "title": "Read page"}

    def execute(self, name, arguments, *, operation_id, cancelled):
        assert not cancelled()
        self.calls.append(copy.deepcopy(arguments))
        return {"page": arguments["page"], "body": self.body, "literal_json": arguments["literal_json"]}


def prepare(tmp_path):
    runtime, model, _, _ = make_runtime(tmp_path)
    tools = runtime.tools = Pages()
    runtime.update_settings({"max_context_chars": CONTEXT_LIMIT, "max_output_chars": CONTEXT_LIMIT,
                             "max_steps": 20, "max_seconds": 120})
    created = runtime.create({"message": "Read the authored page. Never change it or grant access."})
    row = runtime.store.read()["tasks"][created["id"]]
    initial = request_size(runtime._context(row), runtime._specs(row))
    assert initial < CONTEXT_LIMIT - 1000
    # The normal request must compact, while a summary of the same observation
    # fits without the pinned developer layers and full tool catalog.
    tools.body = "Observed fixture fact. " * ((CONTEXT_LIMIT - initial + 500) // 23 + 1)
    return runtime, model, tools, created["id"]


@pytest.mark.parametrize("bad_arguments", [
    BAD_ARGUMENTS,
    '{"page":NaN,"literal_json":""}',
], ids=["broken-json", "non-finite-json"])
def test_bad_json_rejection_is_corrected_then_compacted_without_losing_history(tmp_path, bad_arguments):
    runtime, model, tools, task_id = prepare(tmp_path)
    sources, requests = [], []
    main_step = 0

    def complete(model_id, messages, specs, cancelled, on_event):
        nonlocal main_step
        requests.append((copy.deepcopy(messages), copy.deepcopy(specs)))
        assert request_size(messages, specs) <= CONTEXT_LIMIT
        if not specs:
            source = json.loads(messages[-1]["content"])
            sources.append(source)
            calls = {call["id"]: call for message in source["history"] for call in message.get("tool_calls", [])}
            results = {message["tool_call_id"]: message["content"] for message in source["history"] if message["role"] == "tool"}
            assert set(calls) == set(results)
            assert calls["bad-json"]["function"]["arguments"] == bad_arguments
            assert calls["bad-json"]["function"]["arguments_parse_error"] is True
            assert results["bad-json"]["not_executed"] is True
            assert results["bad-json"]["error"] == "agent_tool_arguments_invalid"
            assert calls["corrected"]["function"]["arguments"] == {"page": 0, "literal_json": LITERAL_JSON}
            assert "arguments_parse_error" not in calls["corrected"]["function"]
            assert results["corrected"]["literal_json"] == LITERAL_JSON
            return assistant("The malformed proposal was not executed. Corrected page zero was read; the source was unchanged.")
        main_step += 1
        if main_step == 1:
            value = assistant(name="ryn_read_resource", args={}, call_id="bad-json")
            value["message"]["tool_calls"][0]["function"]["arguments"] = bad_arguments
            return value
        if main_step == 2:
            rejected = json.loads(next(message["content"] for message in messages if message.get("tool_call_id") == "bad-json"))
            assert rejected["not_executed"] is True
            return assistant(name="ryn_read_resource", args={"page": 0, "literal_json": LITERAL_JSON}, call_id="corrected")
        assert sources and any(message["content"].startswith("Historical checkpoint") for message in messages)
        return assistant("Read completed after correction and compaction; no source changes.")

    model.complete = complete
    for _ in range(4):
        runtime.run_once()
    original = copy.deepcopy(runtime.store.read()["tasks"][task_id]["messages"])
    assert tools.calls == [{"page": 0, "literal_json": LITERAL_JSON}]
    for _ in range(8):
        runtime.run_once()
        if runtime.get(task_id)["status"] == "completed":
            break
    done = runtime.get(task_id)
    assert done["status"] == "completed", done.get("error")
    saved = runtime.store.read()["tasks"][task_id]
    assert saved["context_checkpoint"] and sources and main_step == 3
    assert saved["messages"][:len(original)] == original
    bad = next(call for message in saved["messages"] for call in message.get("tool_calls", []) if call["id"] == "bad-json")
    assert bad["function"]["arguments"] == bad_arguments
    assert "arguments_parse_error" not in bad["function"]
    assert len(tools.calls) == 1
    assert all(request_size(messages, specs) <= CONTEXT_LIMIT for messages, specs in requests)


@pytest.mark.parametrize("stored_as_dict,rejected_depth", [(False, False), (True, False), (False, True)],
                         ids=["valid-json", "valid-dict", "saved-deep-rejection"])
def test_saved_arguments_project_once_and_preserve_nested_business_json(tmp_path, stored_as_dict, rejected_depth):
    runtime, model, tools, task_id = prepare(tmp_path)
    arguments = {"page": 0, "literal_json": LITERAL_JSON}
    exchange = [assistant(name="ryn_read_resource", args=arguments, call_id="legacy-page")["message"],
                {"role": "tool", "tool_call_id": "legacy-page", "content": encoded({"body": tools.body,
                    "literal_json": LITERAL_JSON}).decode()}]
    if stored_as_dict:
        exchange[0]["tool_calls"][0]["function"]["arguments"] = copy.deepcopy(arguments)
    if rejected_depth:
        # Isolate compatibility with already-saved rejection history; admission
        # of an excessively nested new proposal is a separate router boundary.
        exchange[0]["tool_calls"][0]["function"]["arguments"] = DEEP_ARGUMENTS
        exchange[1]["content"] = encoded({"error": "agent_tool_arguments_invalid", "not_executed": True,
                                         "body": tools.body, "literal_json": LITERAL_JSON}).decode()
    runtime.store.transaction(lambda data: data["tasks"][task_id]["messages"].extend(copy.deepcopy(exchange)))
    original = copy.deepcopy(runtime.store.read()["tasks"][task_id]["messages"])
    sources = []

    def complete(model_id, messages, specs, cancelled, on_event):
        assert request_size(messages, specs) <= CONTEXT_LIMIT
        if not specs:
            source = json.loads(messages[-1]["content"])
            sources.append(source)
            function = next(call["function"] for message in source["history"] for call in message.get("tool_calls", []))
            result = next(message["content"] for message in source["history"] if message["role"] == "tool")
            if rejected_depth:
                assert function["arguments"] == DEEP_ARGUMENTS and function["arguments_parse_error"] is True
                assert result["not_executed"] is True
            else:
                assert function["arguments"] == arguments
                assert "arguments_parse_error" not in function
            assert result["literal_json"] == LITERAL_JSON
            return assistant("Authored page was observed. Preserve its literal JSON text.")
        assert sources
        return assistant("Continued after a bounded checkpoint.")

    model.complete = complete
    for _ in range(4):
        runtime.run_once()
        if runtime.get(task_id)["status"] == "completed":
            break
    done = runtime.get(task_id)
    assert done["status"] == "completed", done.get("error")
    assert sources and runtime.store.read()["tasks"][task_id]["messages"][:len(original)] == original
    assert tools.calls == []  # Projection/restoration never re-executes history.
