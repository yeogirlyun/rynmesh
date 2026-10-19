"""Single-layer Codex arguments, strict schemas and the real app-server wire."""
from __future__ import annotations

import copy
import json
import os
import shutil
import threading
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
from jsonschema import Draft202012Validator

from rynmesh.agent_runtime.local_specs import SPECS
from rynmesh.agent_runtime.tool_router import normalize_tool_calls
from rynmesh.llm_package import codex_session
from rynmesh.llm_package.cli_adapter import CLIAgentAdapter
from rynmesh.llm_package.codex_tools import _response_schema, _validated


def assert_strict_subset(schema):
    """Check the documented strict restrictions, in addition to JSON Schema.

    https://developers.openai.com/api/docs/guides/structured-outputs#supported-schemas
    This runs against the schema emitted by the installed binary, not only the
    Python source's schema. A local responder cannot prove cloud acceptance;
    the separately requested real-model acceptance run supplies that evidence.
    """
    Draft202012Validator.check_schema(schema)
    assert schema["type"] == "object" and "anyOf" not in schema
    pending = [(schema, 1)]
    properties_count = enum_count = string_count = 0
    while pending:
        value, depth = pending.pop()
        assert depth <= 10
        assert not set(value) & {"allOf", "oneOf", "not", "dependentRequired", "dependentSchemas", "if", "then", "else", "default"}
        types = value.get("type", [])
        types = [types] if isinstance(types, str) else types
        if "object" in types or "properties" in value:
            properties = value.get("properties", {})
            assert value.get("additionalProperties") is False
            assert set(value.get("required", [])) == set(properties)
            properties_count += len(properties)
            string_count += sum(map(len, properties))
            pending.extend((item, depth + 1) for item in properties.values())
        pending.extend((item, depth) for item in value.get("anyOf", []))
        if "items" in value:
            pending.append((value["items"], depth + 1))
        if "enum" in value:
            enum_count += len(value["enum"])
            string_count += sum(len(item) for item in value["enum"] if isinstance(item, str))
    assert properties_count <= 5000 and enum_count <= 1000 and string_count <= 120000


def test_every_workstation_tool_has_an_object_protocol_in_the_strict_subset():
    from rynmesh.agent_runtime.tool_menu import SPEC as DISCOVERY
    tools = {name: {"name": name, "parameters": spec[3]} for name, spec in SPECS.items()}
    tools[DISCOVERY["function"]["name"]] = DISCOVERY["function"]
    before = copy.deepcopy(tools)
    schema = _response_schema(tools, strict=True)
    assert_strict_subset(schema)
    for variant in schema["properties"]["tool_calls"]["items"]["anyOf"]:
        assert variant["properties"]["arguments"]["type"] == "object"
    assert tools == before


def test_strict_optional_arguments_restore_omission_without_losing_false_or_nullable():
    parameters = {"type": "object", "additionalProperties": False, "properties": {
        "script": {"type": "string"}, "background": {"type": "boolean", "default": True},
        "timeout": {"type": "integer"}, "nullable": {"type": ["string", "null"]}}, "required": ["script"]}
    tools = {"run": {"name": "run", "parameters": parameters}}
    script = 'print("中文\\path")\nprint(\'quarter "two"\')\n'
    value = {"content": "Preparing", "tool_calls": [{"name": "run", "arguments": {
        "script": script, "background": False, "timeout": None, "nullable": None}}]}
    Draft202012Validator(_response_schema(tools, strict=True)).validate(value)
    result = {"text": json.dumps(value, ensure_ascii=False)}
    message = _validated(result, tools, False, restore_omitted=True)
    assert json.loads(message["tool_calls"][0]["function"]["arguments"]) == {
        "script": script, "background": False, "nullable": None}


def test_fallback_first_turn_and_continuation_instruct_omission_for_real_read_tools(codex):
    from test_codex_thread_reuse import conversation

    from rynmesh.agent_runtime.prompt import INTERNAL_SPECS

    tools = [{"type": "function", "function": {"name": name, "parameters": SPECS[name][3]}}
             for name in ("ryn_file_read", "ryn_file_search")]
    tools.append(next(tool for tool in INTERNAL_SPECS if tool["function"]["name"] == "ryn_request_input"))
    with pytest.raises(ValueError):
        _response_schema({tool["function"]["name"]: tool["function"] for tool in tools}, strict=True)
    # Replay the observed call shapes with the unused offsets omitted as the
    # fallback protocol now requests, retaining both calls and their pairing.
    codex.replies.extend([{"content": "Read first", "tool_calls": [
        {"name": "ryn_file_read", "arguments": {"path": "build.py", "start_line": 1, "max_lines": 20}},
        {"name": "ryn_file_search", "arguments": {"path": ".", "query": "target", "regex": False,
                                                    "case_sensitive": False, "file_pattern": "*.py"}}]},
        {"content": "Read complete", "tool_calls": []}])
    adapter, messages, streamed = CLIAgentAdapter("codex_cli"), conversation(), []
    try:
        first = adapter.chat({"messages": copy.deepcopy(messages), "tools": tools, "max_tokens": 512,
                              "stream": True}, task_id="fallback-read-one", timeout_s=5, on_event=streamed.append)
        calls = normalize_tool_calls(first["message"]["tool_calls"], tools)
        assert len(calls) == 2 and all(call["error_result"] is None for call in calls)
        assert all("offset" not in call["arguments"] for call in calls)
        messages.append(first["message"])
        messages.extend({"role": "tool", "tool_call_id": call["call_id"], "content": "Origin read complete"} for call in calls)
        adapter.chat({"messages": messages, "tools": tools, "max_tokens": 512}, task_id="fallback-read-two", timeout_s=5)
        turns = codex.turns(codex.instances[0])
        assert len(turns) == 2 and adapter._codex_threads.stats["resumed"] == 1
        for turn in turns:
            prompt = turn["input"][0]["text"].split("\n", 1)[0]
            assert "outputSchema" not in turn
            assert "Omit unused optional properties" in prompt
            assert "null only where the function's original parameter schema allows null" in prompt
            assert "Use null for unused optional arguments" not in prompt
        assert "Read first" in "".join(event["choices"][0]["delta"].get("content", "") for event in streamed)
    finally:
        adapter.shutdown()


def test_continuation_switches_strict_fallback_strict_rules_with_the_current_menu(codex):
    from test_codex_thread_reuse import conversation, step

    from rynmesh.agent_runtime.prompt import INTERNAL_SPECS

    read = {"type": "function", "function": {"name": "read_file", "parameters": {
        "type": "object", "properties": {"path": {"type": "string"}, "offset": {"type": "integer"}},
        "required": ["path"], "additionalProperties": False}}}
    unsupported = next(tool for tool in INTERNAL_SPECS if tool["function"]["name"] == "ryn_request_input")
    codex.replies.extend([
        {"content": "", "tool_calls": [{"name": "read_file", "arguments": {"path": "a", "offset": None}}]},
        {"content": "", "tool_calls": [{"name": "read_file", "arguments": {"path": "b"}}]},
        {"content": "", "tool_calls": [{"name": "read_file", "arguments": {"path": "c", "offset": None}}]},
    ])
    adapter, messages = CLIAgentAdapter("codex_cli"), conversation()
    try:
        for index, menu in enumerate(([read], [read, unsupported], [read])):
            result = step(adapter, messages, f"mode-{index}", tools=menu)
            call = normalize_tool_calls(result["message"]["tool_calls"], menu)[0]
            assert call["error_result"] is None and "offset" not in call["arguments"]
        assert len(codex.instances) == 1 and adapter._codex_threads.stats["resumed"] == 2
        turns = codex.turns(codex.instances[0])
        assert ["outputSchema" in turn for turn in turns] == [True, False, True]
        for turn, strict in zip(turns, (True, False, True), strict=True):
            prompt = turn["input"][0]["text"].split("\n", 1)[0]
            assert ("Use null for unused optional arguments" in prompt) is strict
            assert ("Omit unused optional properties" in prompt) is not strict
    finally:
        adapter.shutdown()


@pytest.mark.parametrize("restore_omitted", [False, True])
def test_optional_null_rules_do_not_hide_required_null_wrong_types_or_valid_siblings(restore_omitted):
    parameters = {"type": "object", "additionalProperties": False, "properties": {
        "path": {"type": "string"}, "offset": {"type": "integer", "minimum": 0},
        "nullable": {"type": ["string", "null"]}, "required_nullable": {"type": ["string", "null"]},
        "flag": {"type": "boolean"}, "zero": {"type": "integer"}, "text": {"type": "string"}},
        "required": ["path", "required_nullable"]}
    tool = {"type": "function", "function": {"name": "read_file", "parameters": parameters}}
    valid = {"path": "a", "nullable": None, "required_nullable": None, "flag": False, "zero": 0, "text": ""}
    arguments = [valid, {**valid, "offset": None}, {**valid, "path": None}, {**valid, "offset": "0"}]
    before = copy.deepcopy(arguments)
    message = _validated({"text": json.dumps({"content": "", "tool_calls": [
        {"name": "read_file", "arguments": value} for value in arguments]})},
        {"read_file": tool["function"]}, False, restore_omitted=restore_omitted)
    calls = normalize_tool_calls(message["tool_calls"], [tool])
    assert calls[0]["error_result"] is None and calls[0]["arguments"] == valid
    assert (calls[1]["error_result"] is None) is restore_omitted
    assert all(call["error_result"]["error"] == "agent_tool_arguments_invalid" for call in calls[2:])
    assert len({call["call_id"] for call in calls}) == 4 and arguments == before
    if not restore_omitted:
        assert json.loads(message["tool_calls"][1]["function"]["arguments"])["offset"] is None


def test_bad_arguments_do_not_discard_valid_siblings_or_tool_pairing():
    tool = {"type": "function", "function": {"name": "probe", "parameters": {
        "type": "object", "properties": {"value": {"type": "string"}},
        "required": ["value"], "additionalProperties": False}}}
    value = {"content": "", "tool_calls": [{"name": "probe", "arguments": {"value": 3}},
                                             {"name": "probe", "arguments": {"value": "ok"}}]}
    message = _validated({"text": json.dumps(value)}, {"probe": tool["function"]}, False)
    calls = normalize_tool_calls(message["tool_calls"], [tool])
    assert calls[0]["error_result"]["error"] == "agent_tool_arguments_invalid"
    assert calls[1]["error_result"] is None and calls[1]["arguments"] == {"value": "ok"}
    assert len({call["call_id"] for call in calls}) == 2


def test_optional_object_union_uses_plain_objects_instead_of_ambiguous_null_sentinels(codex):
    from test_codex_thread_reuse import conversation, step

    optional = {"type": "object", "additionalProperties": False,
                "properties": {"field": {"type": "string"}}, "required": []}
    nullable = {"type": "object", "additionalProperties": False,
                "properties": {"field": {"type": ["string", "null"]}}, "required": ["field"]}
    parameters = {"type": "object", "additionalProperties": False, "properties": {
        "path": {"type": "string"}, "payload": {"anyOf": [optional, nullable]}}, "required": ["path", "payload"]}
    tools = [{"type": "function", "function": {"name": "read_file", "parameters": parameters}}]
    with pytest.raises(ValueError):
        _response_schema({"read_file": tools[0]["function"]}, strict=True)
    codex.replies.extend([{"content": "", "tool_calls": [{"name": "read_file", "arguments": {"path": "a", "payload": {}}}]},
                         {"content": "done", "tool_calls": []}])
    adapter, messages = CLIAgentAdapter("codex_cli"), conversation()
    result = step(adapter, messages, "one", tools=tools)
    step(adapter, messages, "two", tools=tools)
    assert json.loads(result["message"]["tool_calls"][0]["function"]["arguments"])["payload"] == {}
    assert len(codex.instances) == 1 and adapter._codex_threads.stats["resumed"] == 1
    assert all("outputSchema" not in turn for turn in codex.turns(codex.instances[0]))


def test_scalar_and_fully_required_object_unions_keep_strict_output():
    fully_required = {"type": "object", "additionalProperties": False,
                      "properties": {"value": {"type": "string"}}, "required": ["value"]}
    tool = {"name": "probe", "parameters": {"type": "object", "additionalProperties": False,
            "properties": {"payload": {"anyOf": [fully_required, {"type": "string"}]},
                           "scalar": {"anyOf": [{"type": "string"}, {"type": "integer"}]}},
            "required": ["payload", "scalar"]}}
    schema = _response_schema({"probe": tool}, strict=True)
    assert_strict_subset(schema)
    Draft202012Validator(schema).validate({"content": "", "tool_calls": [{"name": "probe", "arguments": {
        "payload": {"value": "ok"}, "scalar": 3}}]})


def test_open_dictionary_keeps_object_protocol_and_thread_reuse(codex):
    from test_codex_thread_reuse import conversation, step

    tool = {"type": "function", "function": {"name": "read_file", "parameters": {
        "type": "object", "properties": {"path": {"type": "string"}, "extra": {"type": "object"}},
        "required": ["path"], "additionalProperties": False}}}
    codex.replies.extend([{"content": "", "tool_calls": [{"name": "read_file", "arguments": {"path": "a"}}]},
                         {"content": "done", "tool_calls": []}])
    adapter, messages = CLIAgentAdapter("codex_cli"), conversation()
    step(adapter, messages, "one", tools=[tool])
    step(adapter, messages, "two", tools=[tool])
    assert len(codex.instances) == 1
    turns = codex.turns(codex.instances[0])
    assert all("outputSchema" not in turn for turn in turns)
    assert codex.payload(turns[1])["messages"] == [messages[-2]]


@pytest.fixture
def codex(monkeypatch, tmp_path):
    from test_codex_thread_reuse import Codex
    fake = Codex(tmp_path)
    monkeypatch.setattr(codex_session, "CodexRPC", fake.factory())
    monkeypatch.setattr(CLIAgentAdapter, "_command_path", lambda _: "fixture-codex")
    return fake


@pytest.mark.skipif(os.environ.get("RYNMESH_TEST_CODEX_BINARY") != "1", reason="explicit local Codex binary test")
@pytest.mark.parametrize("fallback", [False, True])
def test_real_binary_emits_strict_object_schema_and_continues_after_origin_execution(monkeypatch, tmp_path, fallback):
    executable = shutil.which("codex")
    if not executable:
        pytest.skip("Codex CLI not installed")
    captured, server_errors = [], []
    script = 'console.log("第二季度", \'third "quarter"\');\nconst path = "C:\\\\sales";\n'
    tool = {"type": "function", "function": {"name": "write_local", "parameters": {
        "type": "object", "properties": {"path": {"type": "string"}, "content": {"type": "string"},
                                           "expected_sha256": {"type": "string"}},
        "required": ["path", "content"], "additionalProperties": False}}}
    tools = [tool]
    if fallback:
        from rynmesh.agent_runtime.prompt import INTERNAL_SPECS
        tools.append(next(spec for spec in INTERNAL_SPECS if spec["function"]["name"] == "ryn_request_input"))

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            captured.append(body)
            try:
                wire_format = body.get("text", {}).get("format", {})
                if fallback:
                    assert wire_format.get("type") != "json_schema"
                    assert "Omit unused optional properties" in json.dumps(body)
                    assert "Use null for unused optional arguments" not in json.dumps(body)
                else:
                    assert wire_format["type"] == "json_schema" and wire_format["strict"] is True
                    assert_strict_subset(wire_format["schema"])
                arguments = {"path": "build.mjs", "content": script}
                if not fallback:
                    arguments["expected_sha256"] = None
                value = ({"content": "Writing the script", "tool_calls": [{"name": "write_local", "arguments": {
                    **arguments}}]} if len(captured) == 1 else
                    {"content": "Script written and checked.", "tool_calls": []})
                if not fallback:
                    Draft202012Validator(wire_format["schema"]).validate(value)
            except Exception as exc:
                server_errors.append(repr(exc))
                self.send_error(400, "schema rejected by local strict-subset contract")
                return
            text = json.dumps(value, ensure_ascii=False)
            item = {"type": "message", "id": "msg_" + str(len(captured)), "role": "assistant", "status": "completed",
                    "phase": "final_answer", "content": [{"type": "output_text", "text": text, "annotations": []}]}
            response = {"id": "resp_" + str(len(captured)), "object": "response", "status": "completed", "output": [item],
                        "usage": {"input_tokens": 5, "output_tokens": 5, "total_tokens": 10}}
            values = [
                {"type": "response.created", "response": {**response, "status": "in_progress", "output": []}},
                {"type": "response.output_item.added", "output_index": 0, "item": {**item, "content": []}},
                {"type": "response.content_part.added", "output_index": 0, "item_id": item["id"], "content_index": 0,
                 "part": {"type": "output_text", "text": "", "annotations": []}},
                {"type": "response.output_text.delta", "output_index": 0, "item_id": item["id"], "content_index": 0, "delta": text},
                {"type": "response.output_text.done", "output_index": 0, "item_id": item["id"], "content_index": 0, "text": text},
                {"type": "response.output_item.done", "output_index": 0, "item": item},
                {"type": "response.completed", "response": response},
            ]
            payload = "".join("event: " + value["type"] + "\ndata: " + json.dumps(value) + "\n\n" for value in values).encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    original, roots = codex_session.private_workspace, []

    @contextmanager
    def workspace():
        with original(copy_auth=False) as (root, env):
            roots.append(root)
            env.pop("OPENAI_API_KEY", None)
            config = root / "codex/config.toml"
            config.write_text('model = "mock"\nmodel_provider = "local_test"\n' + config.read_text() +
                f'\n[model_providers.local_test]\nname = "local fake model"\nbase_url = "http://127.0.0.1:{server.server_port}/v1"\nwire_api = "responses"\nrequires_openai_auth = false\nsupports_websockets = false\n', encoding="utf-8")
            yield root, env

    monkeypatch.setattr(codex_session, "private_workspace", workspace)
    adapter, events = CLIAgentAdapter("codex_cli", executable=executable), []
    messages = [{"role": "user", "content": "Write build.mjs at the origin client"}]
    try:
        first = adapter.chat({"messages": messages, "tools": tools, "max_tokens": 1024, "stream": True},
                             task_id="local-object-one", timeout_s=30, on_event=events.append)
        calls = normalize_tool_calls(first["message"]["tool_calls"], tools)
        assert len(calls) == 1 and calls[0]["error_result"] is None, server_errors
        assert calls[0]["arguments"] == {"path": "build.mjs", "content": script}
        (tmp_path / "build.mjs").write_text(calls[0]["arguments"]["content"], encoding="utf-8")
        assert (tmp_path / "build.mjs").read_text(encoding="utf-8") == script
        messages.extend([first["message"], {"role": "tool", "tool_call_id": calls[0]["call_id"], "content": "Written and read back exactly"}])
        second = adapter.chat({"messages": messages, "tools": tools, "max_tokens": 1024},
                              task_id="local-object-two", timeout_s=30)
        assert second["text"] == "Script written and checked."
        assert len(captured) == 2 and not server_errors
        assert adapter._codex_threads.stats["resumed"] == 1
        assert "Writing the script" in "".join(event["choices"][0]["delta"].get("content", "") for event in events)
    finally:
        adapter.shutdown()
        server.shutdown()
        server.server_close()
    assert all(not root.exists() for root in roots)
