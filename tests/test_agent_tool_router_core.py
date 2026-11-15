"""Router boundaries without providers, handlers or durable state."""
import copy
import json

import pytest

from rynmesh.agent_runtime.output_policy import MAX_RAW_RESPONSE_BYTES, MAX_TOOL_CALL_ARGUMENT_BYTES
from rynmesh.agent_runtime.store import AgentError, AgentToolRejected
from rynmesh.agent_runtime.tool_router import (
    INVALID_TOOL_NAME,
    ToolCallProtocolError,
    assert_schema_unchanged,
    normalize_tool_calls,
)
from rynmesh.llm_package.chat import validate_chat


def spec(name="read", value_type="string"):
    return {"type": "function", "function": {"name": name, "parameters": {
        "type": "object", "properties": {"path": {"type": value_type}},
        "required": ["path"], "additionalProperties": False,
    }}}


def call(identifier="call_one", name="read", arguments='{"path":"fixture.txt"}'):
    return {"id": identifier, "type": "function", "function": {"name": name, "arguments": arguments}}


def test_mixed_calls_keep_order_and_pair_recoverable_errors_without_metadata():
    looked_up = []

    def metadata(name):
        looked_up.append(name)
        return {"effect": "read"}

    calls = [call(), call("unknown", "missing"), call("bad_json", arguments="{"),
             call("bad_schema", arguments='{"path":7}')]
    before = copy.deepcopy(calls)
    routed = normalize_tool_calls(calls, [spec()], metadata=metadata)
    assert [item["call_id"] for item in routed] == ["call_one", "unknown", "bad_json", "bad_schema"]
    assert looked_up == ["read"]
    assert routed[0]["arguments"] == {"path": "fixture.txt"}
    assert routed[0]["error_result"] is None
    assert [item["error_result"]["error"] for item in routed[1:]] == [
        "agent_unknown_tool", "agent_tool_arguments_invalid", "agent_tool_arguments_invalid"]
    assert all(item["error_result"]["not_executed"] and item["error_result"]["no_mutation"] for item in routed[1:])
    # The whole proposed batch can be recorded and answered without inventing
    # ids or leaking an unpaired call into the next model request.
    assistant = {"role": "assistant", "content": "", "tool_calls": [
        call(item["call_id"], item["name"], item["arguments_json"]) for item in routed]}
    replies = [{"role": "tool", "tool_call_id": item["call_id"], "content": json.dumps(item["error_result"] or {"ok": True})}
               for item in routed]
    validate_chat({"messages": [assistant, *replies]})
    assert routed[2]["arguments_json"] == "{"  # Raw provider proposal survives rejection.
    assert calls == before


@pytest.mark.parametrize("identifier", [None, "", "contains space", "x" * 129, 7])
def test_invalid_identity_rejects_entire_batch_before_metadata(identifier):
    looked_up = []
    with pytest.raises(ToolCallProtocolError) as error:
        normalize_tool_calls([call(), call(identifier)], [spec()], metadata=lambda name: looked_up.append(name))
    assert error.value.reason == "invalid_call_id"
    assert looked_up == []


@pytest.mark.parametrize("calls,existing", [([call(), call()], []), ([call()], ["call_one"])])
def test_reused_call_ids_cannot_steal_an_existing_result(calls, existing):
    with pytest.raises(ToolCallProtocolError) as error:
        normalize_tool_calls(calls, [spec()], existing_call_ids=existing)
    assert error.value.reason == "duplicate_call_id"


@pytest.mark.parametrize("count", [9, 17, 33, 100])
def test_large_batches_keep_every_call_in_order(count):
    calls = [call(f"call_{n}") for n in range(count)]
    routed = normalize_tool_calls(calls, [spec()])
    assert [item["call_id"] for item in routed] == [item["id"] for item in calls]
    assert all(item["error_result"] is None for item in routed)


def test_utf8_argument_and_total_byte_budgets_reject_atomically():
    calls = [call(f"call_{n}", arguments=json.dumps({"path": "x" * 60000})) for n in range(5)]
    assert len(json.dumps(calls).encode()) > MAX_RAW_RESPONSE_BYTES
    with pytest.raises(ToolCallProtocolError) as error:
        normalize_tool_calls(calls, [spec()])
    assert error.value.reason == "response_too_large"
    raw = '{"path":"中文"}'
    with pytest.raises(ToolCallProtocolError) as error:
        normalize_tool_calls([call(arguments="{}"), call("large", arguments=raw)], [spec()],
                             max_argument_bytes=len(raw.encode()) - 1)
    assert error.value.reason == "arguments_too_large"


@pytest.mark.parametrize("budget", [{"max_argument_bytes": MAX_TOOL_CALL_ARGUMENT_BYTES + 1}])
def test_callers_may_lower_but_never_raise_global_proposal_limits(budget):
    with pytest.raises(ValueError, match="invalid tool-call budget"):
        normalize_tool_calls([], [], **budget)


def test_provider_object_arguments_are_normalized_without_sharing_mutable_input():
    arguments = {"path": "fixture.txt"}
    routed = normalize_tool_calls([call(arguments=arguments)], [spec()])[0]
    assert routed["error_result"] is None
    assert json.loads(routed["arguments_json"]) == arguments
    arguments["path"] = "changed.txt"
    assert routed["arguments"] == {"path": "fixture.txt"}


def test_invalid_host_schema_cannot_resolve_external_references():
    offered = spec()
    offered["function"]["parameters"] = {"$ref": "https://invalid.example/schema.json"}
    with pytest.raises(AgentError, match="agent_tool_definition_changed"):
        normalize_tool_calls([call()], [offered])


def test_schema_is_frozen_at_request_and_checked_again_before_host_authorization():
    offered = spec()
    routed = normalize_tool_calls([call()], [offered], metadata=lambda _name: {"effect": "write"})[0]
    assert routed["effect"] == "write"  # Metadata classification is retained, never inferred from the name.
    offered["function"]["parameters"]["properties"]["path"]["type"] = "integer"
    assert routed["schema_snapshot"]["parameters"]["properties"]["path"]["type"] == "string"
    with pytest.raises(AgentToolRejected, match="agent_tool_definition_changed"):
        assert_schema_unchanged(routed, offered)
    assert_schema_unchanged(routed, spec())
    routed["schema_snapshot"]["parameters"]["required"] = []
    with pytest.raises(AgentToolRejected, match="agent_tool_definition_changed"):
        assert_schema_unchanged(routed, spec())


def test_response_item_uses_call_id_not_provider_item_id():
    item = {"type": "function_call", "id": "item_not_a_call", "call_id": "stable_call", "name": "read",
            "arguments": '{"path":"fixture.txt"}'}
    routed = normalize_tool_calls([item], [spec()])[0]
    assert routed["call_id"] == "stable_call"
    assert routed["error_result"] is None


@pytest.mark.parametrize("arguments", [None, "[]", "null", '{"path":NaN}', '[', 9])
def test_non_object_and_invalid_json_get_paired_nonexecution_errors(arguments):
    routed = normalize_tool_calls([call(arguments=arguments)], [spec()])[0]
    assert routed["call_id"] == "call_one"
    assert routed["error_result"]["error"] == "agent_tool_arguments_invalid"
    with pytest.raises(AgentToolRejected, match="agent_tool_not_executable"):
        assert_schema_unchanged(routed, spec())


@pytest.mark.parametrize("name", [None, "", {}, "bad name"])
def test_missing_or_bad_name_remains_a_pairable_error(name):
    routed = normalize_tool_calls([call(name=name)], [spec()])[0]
    assert routed["name"] == INVALID_TOOL_NAME
    assert routed["error_result"]["error"] == "agent_unknown_tool"
    assert routed["call_id"] == "call_one"


def test_namespace_cannot_accidentally_dispatch_same_named_flat_tool():
    item = {"type": "function_call", "call_id": "namespaced", "namespace": "unoffered", "name": "read",
            "arguments": '{"path":"fixture.txt"}'}
    routed = normalize_tool_calls([item], [spec()], metadata=lambda _name: pytest.fail("must not resolve"))[0]
    assert routed["error_result"]["error"] == "agent_unknown_tool"


def test_bad_host_metadata_is_not_misreported_as_a_model_argument_error():
    with pytest.raises(AgentError, match="agent_tool_definition_changed"):
        normalize_tool_calls([call()], [spec()], metadata=lambda _name: {"effect": "unknown"})


def test_internal_validation_stays_with_existing_internal_handler():
    routed = normalize_tool_calls([call("internal", "internal", '{}')], [spec("internal")],
                                  internal_names={"internal"}, metadata=lambda _name: {"effect": "read"})[0]
    assert routed["error_result"] is None
    assert routed["arguments"] == {}


def test_empty_or_whitespace_arguments_parse_as_empty_object():
    optional_spec = {"type": "function", "function": {"name": "list_items", "parameters": {
        "type": "object", "properties": {"cursor": {"type": "string"}}, "additionalProperties": False,
    }}}
    looked_up = []
    routed = normalize_tool_calls(
        [call("empty_str", "list_items", ""), call("ws_str", "list_items", "  \n\t ")],
        [optional_spec],
        metadata=lambda name: looked_up.append(name) or {"effect": "read"},
    )
    assert looked_up == ["list_items", "list_items"]
    assert routed[0]["error_result"] is None
    assert routed[0]["arguments"] == {}
    assert routed[0]["arguments_json"] == ""
    assert routed[1]["error_result"] is None
    assert routed[1]["arguments"] == {}
    assert routed[1]["arguments_json"] == "  \n\t "


def test_malformed_json_and_non_object_arguments_report_specific_parse_reason():
    bad_json = normalize_tool_calls([call("bad_json", arguments='{"a":')], [spec()])[0]
    err = bad_json["error_result"]
    assert err["error"] == "agent_tool_arguments_invalid"
    assert err["message"].startswith("failed to parse function arguments:")
    assert "line 1 column" in err["message"]

    for raw_arg, expected_type in [("[]", "array"), ("null", "null"), (9, "number"), (True, "boolean")]:
        routed = normalize_tool_calls([call("non_obj", arguments=raw_arg)], [spec()])[0]
        assert routed["error_result"]["error"] == "agent_tool_arguments_invalid"
        assert routed["error_result"]["message"] == f"arguments must be a JSON object, got {expected_type}"


def test_schema_validation_reports_field_path_required_and_truncates_to_300_chars():
    from rynmesh.llm_package.chat import validate_tool_arguments

    missing_req = normalize_tool_calls([call("missing_req", arguments="{}")], [spec()])[0]
    err = missing_req["error_result"]
    assert err["error"] == "agent_tool_arguments_invalid"
    assert "path" in err["message"] and "required" in err["message"]
    assert err["details"] == {"field_path": "<root>", "validator": "required"}

    nested_spec = {"type": "function", "function": {"name": "batch", "parameters": {
        "type": "object",
        "properties": {
            "items": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {"count": {"type": "integer"}},
                    "required": ["count"],
                },
            }
        },
        "required": ["items"],
    }}}
    long_bad = "Bearer secret_tok_999 " + ("x" * 500)
    with pytest.raises(ValueError) as exc_info:
        validate_tool_arguments(nested_spec["function"]["parameters"], {"items": [{"count": long_bad}]})
    assert exc_info.value.__cause__ is not None
    assert exc_info.value.field_path == "$.items[0].count"
    assert len(exc_info.value.detail) == 300

    routed = normalize_tool_calls(
        [call("nested_bad", "batch", json.dumps({"items": [{"count": long_bad}]}))],
        [nested_spec],
    )[0]
    nested_err = routed["error_result"]
    assert nested_err["error"] == "agent_tool_arguments_invalid"
    assert nested_err["details"] == {"field_path": "items/0/count", "validator": "type"}
    assert "items/0/count" in nested_err["message"]
    assert "secret_tok_999" not in nested_err["message"]
    assert "Bearer [redacted]" in nested_err["message"]
    prefix = "Invalid arguments for 'batch' at field 'items/0/count' (type): "
    assert nested_err["message"].startswith(prefix)
    assert len(nested_err["message"][len(prefix):]) <= 300


def test_namespace_resolves_prefixed_or_canonical_tool_and_rejects_unmatched_or_ambiguous():
    mcp_spec = spec("mcp__x__y")
    hit_prefixed = {
        "type": "function_call", "call_id": "ns_prefixed", "namespace": "mcp__x", "name": "y",
        "arguments": '{"path":"fixture.txt"}',
    }
    routed_prefixed = normalize_tool_calls([hit_prefixed], [mcp_spec], metadata=lambda name: {"effect": "read"})[0]
    assert routed_prefixed["error_result"] is None
    assert routed_prefixed["name"] == "mcp__x__y"

    hit_functions = {
        "type": "function_call", "call_id": "ns_functions", "namespace": "functions", "name": "read",
        "arguments": '{"path":"fixture.txt"}',
    }
    routed_functions = normalize_tool_calls([hit_functions], [spec("read")], metadata=lambda name: {"effect": "read"})[0]
    assert routed_functions["error_result"] is None
    assert routed_functions["name"] == "read"

    hit_direct = {
        "type": "function_call", "call_id": "ns_direct", "namespace": "mcp__x", "name": "mcp__x__y",
        "arguments": '{"path":"fixture.txt"}',
    }
    routed_direct = normalize_tool_calls([hit_direct], [mcp_spec], metadata=lambda name: {"effect": "read"})[0]
    assert routed_direct["error_result"] is None
    assert routed_direct["name"] == "mcp__x__y"

    unoffered_flat = {
        "type": "function_call", "call_id": "ns_unoffered", "namespace": "unoffered", "name": "read",
        "arguments": '{"path":"fixture.txt"}',
    }
    routed_unoffered = normalize_tool_calls(
        [unoffered_flat], [spec("read")], metadata=lambda _name: pytest.fail("must not resolve")
    )[0]
    assert routed_unoffered["error_result"]["error"] == "agent_unknown_tool"

    miss = {
        "type": "function_call", "call_id": "ns_miss", "namespace": "mcp__other", "name": "z",
        "arguments": '{"path":"fixture.txt"}',
    }
    routed_miss = normalize_tool_calls([miss], [mcp_spec], metadata=lambda _name: pytest.fail("must not resolve"))[0]
    assert routed_miss["error_result"]["error"] == "agent_unknown_tool"


def test_additional_properties_rejection_does_not_leak_unexpected_key_or_value():
    from rynmesh.llm_package.chat import validate_tool_arguments

    payload = {"path": "fixture.txt", "private-secret": "never-export"}
    with pytest.raises(ValueError) as exc_info:
        validate_tool_arguments(spec()["function"]["parameters"], payload)
    assert "unexpected properties are not allowed" in exc_info.value.detail
    assert "private-secret" not in str(exc_info.value)
    assert "never-export" not in str(exc_info.value)
    assert "private-secret" not in json.dumps(list(exc_info.value.args))

    routed = normalize_tool_calls([call("extra_key", "read", json.dumps(payload))], [spec()])[0]
    err = routed["error_result"]
    assert err["error"] == "agent_tool_arguments_invalid"
    assert "unexpected properties are not allowed" in err["message"]
    assert err["details"] == {"field_path": "<root>", "validator": "additionalProperties"}
    dumped = json.dumps(err)
    assert "private-secret" not in dumped
    assert "never-export" not in dumped


def test_loop_secondary_validation_rejects_legacy_queued_call_with_field_details():
    from types import SimpleNamespace

    from rynmesh.agent_runtime.loop import AgentLoop

    class DummyHost(AgentLoop):
        def __init__(self):
            self.tools = SimpleNamespace(
                metadata=lambda _name: {"effect": "read"},
            )

        def _reserve_output(self, _selected):
            pass

        def _business_specs(self, _selected):
            return [spec("read")]

    host = DummyHost()
    legacy_call = {
        "call_id": "legacy_bad",
        "operation_id": "op_legacy",
        "name": "read",
        "effect": "read",
        "arguments": {},
    }
    selected = {"id": "task_1", "queue": [legacy_call]}
    with pytest.raises(AgentToolRejected) as exc_info:
        host._tool(selected, lambda: False, 0.0)
    assert exc_info.value.args[0] == "agent_tool_arguments_invalid"
    assert "path" in (exc_info.value.correction or "") and "required" in (exc_info.value.correction or "")
    assert exc_info.value.details == {"tool": "read", "field_path": "<root>", "validator": "required"}

