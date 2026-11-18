"""S06-I22 batch-local rejection and one canonical, first-wins tool catalog."""
import copy
import hashlib
import json

import pytest
from agent_tool_fixtures import expose_on_create
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey
from test_agent_runtime import advance, assistant, make_runtime
from test_agent_tool_router_core import call, spec
from test_agent_js_integration import _seed_task_row, HOST_METHOD_MAPPING

from rynmesh.agent_runtime import tool_menu
from rynmesh.agent_runtime.store import AgentError, AgentStore
from rynmesh.agent_runtime.tool_delivery import stage_child_completion
from rynmesh.agent_runtime.js_integration import plan_child_call
from rynmesh.agent_runtime.js_runtime.protocol import PendingHostCallEnvelope, canonical_arguments_sha256
from rynmesh.agent_runtime.output_policy import MAX_RAW_RESPONSE_BYTES, MAX_TOOL_CALL_ARGUMENT_BYTES
from rynmesh.agent_runtime.store import encoded
from rynmesh.agent_runtime.tool_router import normalize_tool_calls, ToolCallProtocolError
from rynmesh.llm_package.chat import validate_chat


def test_T1_oversize_call_is_paired_while_other_calls_keep_order_and_effect():
    raw = json.dumps({"path": "中文" * 30}, ensure_ascii=False)
    seen = []
    routed = normalize_tool_calls([call("one"), call("large", arguments=raw), call("three")], [spec()],
        max_argument_bytes=64, metadata=lambda name: seen.append(name) or {"effect": "read"})
    assert [c["call_id"] for c in routed] == ["one", "large", "three"]
    assert [c["error_result"] is None for c in routed] == [True, False, True]
    bad = routed[1]
    assert bad["error_result"]["error"] == "agent_model_output_limit" and bad["arguments"] == {}
    assert bad["arguments_json"] == f"<arguments omitted: {len(raw.encode())} bytes>"
    assert bad["raw_arguments_json"] == bad["arguments_json"]
    assert bad["arguments_sha256"] == hashlib.sha256(raw.encode()).hexdigest()
    assert seen == ["read", "read"] and all(c["effect"] == "read" for c in routed)
    messages = [{"role": "assistant", "content": "", "tool_calls": [call(c["call_id"], c["name"], c["arguments_json"]) for c in routed]},
                *[{"role": "tool", "tool_call_id": c["call_id"], "content": json.dumps(c["error_result"] or {"ok": True})} for c in routed]]
    validate_chat({"messages": messages})
    with pytest.raises(ToolCallProtocolError, match="agent_model_output_limit") as error:
        normalize_tool_calls([call("full", arguments="a" * MAX_RAW_RESPONSE_BYTES)], [spec()])
    assert error.value.reason == "response_too_large"


def test_T2_router_collision_keeps_first_schema_and_collects_name():
    collisions = []
    routed = normalize_tool_calls([call()], [spec(), spec(value_type="integer")], collisions=collisions)
    assert collisions == ["read"]
    assert routed[0]["error_result"] is None
    assert routed[0]["schema_snapshot"]["parameters"]["properties"]["path"]["type"] == "string"


@pytest.mark.parametrize("bad", [None, {}, {"effect": "unknown"}, {"effect": []}, {"effect": {}}, "raises"])
def test_T3_metadata_problem_only_rejects_that_call(bad):
    def metadata(name):
        if name == "broken":
            if bad == "raises":
                raise KeyError("private configuration detail")
            return bad
        return {"effect": "read"}
    routed = normalize_tool_calls([call("one"), call("bad", "broken"), call("three")], [spec(), spec("broken")], metadata=metadata)
    assert [c["error_result"] is None for c in routed] == [True, False, True]
    assert routed[1]["error_result"]["error"] == "agent_tool_unavailable"
    assert "private configuration" not in json.dumps(routed)
    assert routed[0]["effect"] == routed[2]["effect"] == "read"


def test_T4_project_and_discover_share_first_wins_and_reserved_names():
    first = spec()
    first["function"]["description"] = "description " * 1500
    catalog = [first, spec(value_type="integer"), copy.deepcopy(tool_menu.SPEC)]
    unique, collisions = tool_menu.dedupe_specs(catalog, {tool_menu.NAME})
    assert unique == [first] and collisions == ["read", tool_menu.NAME]
    exposed, state = tool_menu.project(catalog, [], None, max_context_bytes=16000, authority_bytes=0, epoch=0, goal_revision=1)
    assert state["mode"] == "directory"
    assert [s["function"]["name"] for s in exposed] == [tool_menu.NAME]
    result, _ = tool_menu.discover(catalog, [], lambda _: {"effect": "read"}, state, {"cursor": 0}, capacity_bytes=8000)
    assert result["total"] == 1 and result["tools"][0]["name"] == "read"


def test_T4_real_runtime_records_one_bounded_collision_event_and_notifies_model(tmp_path):
    runtime, model, tools, _ = make_runtime(tmp_path,
        assistant(name="inspect_service", args={"service_id": "A"}), assistant("Inspected A"))
    original_specs, original_meta = tools.specs, tools.metadata
    extra = [spec(f"duplicate_{n}") for n in range(20)]
    tools.specs = lambda: [*original_specs(), *extra, *copy.deepcopy(extra), copy.deepcopy(tool_menu.SPEC)]
    tools.metadata = lambda name: original_meta(name) if name in {"inspect_service", "share_service"} else {"effect": "read"}
    expose_on_create(runtime, ["inspect_service"])
    task = runtime.create({"message": "Inspect service A"})
    advance(runtime, 3)
    row = runtime.store.read()["tasks"][task["id"]]
    events = [e for e in row["events"] if e["type"] == "tool_name_collision"]
    assert len(events) == 1 and 0 < len(events[0]["names"]) <= 16
    assert "tool_menu_notice" in json.dumps(model.calls[0][1]) and "duplicate_0" in json.dumps(model.calls[0][1])
    assert [name for name, _, _ in tools.calls] == ["inspect_service"]
    assert row["status"] == "completed"
    assert tool_menu.NAME not in [s["function"]["name"] for s in runtime._business_specs(row)]


def test_T1_real_runtime_runs_good_siblings_and_pairs_oversize_without_retry(tmp_path):
    proposal = assistant()["message"]
    arguments = json.dumps({"service_id": "x" * (MAX_TOOL_CALL_ARGUMENT_BYTES + 10)})
    proposal["tool_calls"] = [call("one", "inspect_service", '{"service_id":"A"}'), call("big", "inspect_service", arguments),
                              call("three", "inspect_service", '{"service_id":"B"}')]
    runtime, model, tools, _ = make_runtime(tmp_path, {"message": proposal})
    task = runtime.create({"message": "Inspect A and B"})
    advance(runtime, 4)
    row = runtime.store.read()["tasks"][task["id"]]
    assert [args["service_id"] for _, args, _ in tools.calls] == ["A", "B"]
    paired = [m for m in row["messages"] if m.get("role") == "tool" and m.get("tool_call_id") == "big"]
    assert len(paired) == 1 and json.loads(paired[0]["content"])["error"] == "agent_model_output_limit"
    assert len(model.calls) == 1 and row.get("model_retry_count", 0) == 0


@pytest.mark.parametrize("tamper", [None, "digest", "placeholder", "arguments", "error"])
def test_T1_phase_a_js_oversize_callback_has_deliverable_paired_rejection(tmp_path, tamper):
    store = AgentStore(tmp_path, X25519PrivateKey.generate())
    _, catalog = _seed_task_row(store)
    arguments = {"window": {"app": "Notepad", "id": 1}, "text": "short"}
    env = PendingHostCallEnvelope(task_id="task_js_phase_a", session_id="sess_001", generation=1, execution_id="exec_001",
        callback_id="cb_large", ordinal=0, method="type_text", effect="write", target_tool="computer_act_window",
        arguments=arguments, arguments_sha256=canonical_arguments_sha256(arguments))
    # The guest frame must fit even when host translation makes the tool call too large.
    def translate(_method, original):
        return {**original, "text": "x" * (MAX_TOOL_CALL_ARGUMENT_BYTES + 10)}

    routed = store.transaction(lambda data: plan_child_call(data["tasks"][env.task_id], env, catalog,
                               HOST_METHOD_MAPPING, argument_translator=translate))
    assert routed["error_result"]["error"] == "agent_model_output_limit"
    routed = copy.deepcopy(routed)
    if tamper:
        if tamper == "digest":
            routed["arguments_sha256"] = "0" * 64
        elif tamper == "placeholder":
            routed["arguments_json"] = routed["raw_arguments_json"] = f"<arguments omitted: {MAX_TOOL_CALL_ARGUMENT_BYTES} bytes>"
        elif tamper == "arguments":
            routed["arguments"] = {"text": "replacement"}
        else:
            routed["error_result"]["error"] = "agent_tool_unavailable"
        before = store.read()
        with pytest.raises(AgentError, match="agent_js_callback_conflict"):
            store.transaction(lambda data: stage_child_completion(data["tasks"][env.task_id], routed,
                receipt_status="not_executed", result=routed["error_result"], record_receipt=False))
        assert store.read() == before
        return
    token = store.transaction(lambda data: stage_child_completion(data["tasks"][env.task_id], routed,
        receipt_status="not_executed", result=routed["error_result"], record_receipt=False))
    assert token.receipt_id is None and token.not_executed
    assert token.payload["error"] == "agent_model_output_limit"
