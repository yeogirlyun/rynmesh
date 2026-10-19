"""Router boundaries without providers, handlers or durable state."""
import copy
import json

import pytest

from rynmesh.agent_runtime.output_policy import MAX_TOOL_CALL_ARGUMENT_BYTES, MAX_TOOL_CALLS
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


def test_call_count_and_utf8_argument_budgets_reject_atomically():
    with pytest.raises(ToolCallProtocolError) as error:
        normalize_tool_calls([call(f"call_{n}") for n in range(MAX_TOOL_CALLS + 1)], [spec()])
    assert error.value.reason == "too_many_calls"
    raw = '{"path":"中文"}'
    with pytest.raises(ToolCallProtocolError) as error:
        normalize_tool_calls([call(arguments="{}"), call("large", arguments=raw)], [spec()],
                             max_argument_bytes=len(raw.encode()) - 1)
    assert error.value.reason == "arguments_too_large"


@pytest.mark.parametrize("budget", [{"max_calls": MAX_TOOL_CALLS + 1},
                                    {"max_argument_bytes": MAX_TOOL_CALL_ARGUMENT_BYTES + 1}])
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
