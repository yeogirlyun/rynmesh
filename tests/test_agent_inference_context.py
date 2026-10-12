"""Inference facts in actual offline provider requests, never a model-behavior pass."""
import copy
import json

import pytest
from test_agent_integrations import add_source, fake_upstream, node  # noqa: F401
from test_agent_native_scope import native_scope  # noqa: F401
from test_agent_peer_binding import paired  # noqa: F401
from test_agent_peer_scope import consumer, create_scoped  # noqa: F401
from test_agent_resources import file_ref, invoke, resources  # noqa: F401
from test_agent_resources_http import http_resources  # noqa: F401
from test_agent_runtime import assistant, make_runtime
from test_agent_runtime_model_switch import body
from test_native_model_policy import native  # noqa: F401

from rynmesh.agent_runtime.bridge import build_agent_integrations
from rynmesh.agent_runtime.checker_references import expand
from rynmesh.agent_runtime.inference_context import GUIDANCE, projection
from rynmesh.agent_runtime.runtime import AgentRuntime
from rynmesh.agent_runtime.store import AgentError, AgentStore, encoded
from rynmesh.agent_runtime.verification import checker_messages, record
from rynmesh.services import peer_box


def control(messages):
    system = next(m["content"] for m in messages if m["role"] == "system")
    state = json.loads(system.split("\nServer control state: ", 1)[1].split("\n", 1)[0])
    assert GUIDANCE in system
    assert len(encoded(state["inference_context"])) + len(GUIDANCE.encode()) < 1000
    return state


def record_read(runtime, task_id, result):
    def save(data):
        row = data["tasks"][task_id]
        row["messages"].extend([
            {"role": "assistant", "content": "", "context_epoch": row.get("context_epoch", 0),
             "tool_calls": [{"id": "read", "type": "function", "function": {
                 "name": "ryn_read_resource", "arguments": "{}"}}]},
            {"role": "tool", "tool_call_id": "read", "content": json.dumps(result),
             "context_epoch": row.get("context_epoch", 0), "resource_origin": result["origin"]}])
        record(row, {"operation_id": "fixture-read", "name": "ryn_read_resource", "effect": "read",
                     "arguments": {"resource_ref": result["origin"]["resource_ref"]}},
               "confirmed", "2026-10-11T00:00:00Z", result=result)
    runtime.store.transaction(save)


def test_peer_request_carries_read_body_and_facts_then_switch_removes_current_grants(consumer):  # noqa: F811
    p, runtime, model, selection, envelopes, resource_tools = consumer
    runtime.update_settings({"permission_mode": "read_only"})
    task_id = create_scoped(runtime, model, selection)
    row = runtime.store.read()["tasks"][task_id]
    context = runtime._execution_context(row)
    result = invoke(resource_tools, context, "ryn_read_resource", file_ref(resource_tools, context))
    record_read(runtime, task_id, result)
    runtime.run_once()
    assert len(p.traces) == len(envelopes) == 1, runtime.get(task_id).get("error")
    messages = p.traces[0]["messages"]
    facts = control(messages)["inference_context"]
    assert facts == {"destination_kind": "peer", "destination_binding_recorded": True,
                     "resource_grants_recorded": 1, "resources_with_matching_egress": 1,
                     "historical_egress": "not_established"}
    assert json.loads(next(m["content"] for m in messages if m["role"] == "tool"))["text"] == result["text"]
    assert "read-only" in messages[0]["content"]
    assert result["text"] not in messages[0]["content"]
    before = runtime.store.read()["tasks"][task_id]
    checker_before = checker_messages(before, context_limit=12000)
    before_payload = expand(json.loads(checker_before[1]["content"]))
    assert before_payload["inference_context"] == facts
    assert before_payload["candidate_answer"] == before["result"]
    assert before_payload["receipts"] == before["receipts"]
    assert before_payload["receipts"][0]["result"] == result
    assert GUIDANCE in checker_before[0]["content"]
    assert len(encoded(checker_before)) < 12000
    runtime.action(task_id, {"action": "pause"})
    runtime.switch_model(task_id, body(runtime, task_id, model["id"]))
    row = runtime.store.read()["tasks"][task_id]
    assert row["scope"]["selection"]["resources"] == selection["resources"]
    state = control(runtime._context(row))
    assert state["scope_revision"] == row["scope_revision"]
    assert state["inference_context"]["resources_with_matching_egress"] == 0
    assert state["inference_context"]["historical_egress"] == "not_established"
    checker_after = checker_messages(row, context_limit=12000)
    after_payload = json.loads(checker_after[1]["content"])
    assert after_payload["inference_context"] == state["inference_context"]
    assert after_payload["inference_context"]["resources_with_matching_egress"] == 0
    assert GUIDANCE in checker_after[0]["content"]
    assert len(encoded(checker_after)) < 12000
    assert result["text"] not in str(runtime._context(row))
    assert len(p.traces) == 1  # Projection and switching do not dispatch.


def test_revoked_resource_permission_blocks_dispatch_despite_recorded_scope(consumer, resources):  # noqa: F811
    p, runtime, model, selection, envelopes, _ = consumer
    task_id = create_scoped(runtime, model, selection)
    row = runtime.store.read()["tasks"][task_id]
    resources[1].update_source("docs", {"allow_ai": False})
    # Facts describe recorded consent only, never bypass live generation checks.
    assert projection(row)["resources_with_matching_egress"] == 1
    with pytest.raises(AgentError, match="agent_resource_ai_not_allowed"):
        runtime._validate_context(row)
    runtime.run_once()
    assert not p.traces and not envelopes


def test_local_api_request_does_not_claim_local_inference_or_export_source_secrets(node, monkeypatch):  # noqa: F811
    app, store = node
    _, model_id = add_source(app)
    bridge, tools, catalog = build_agent_integrations(app, store, store.home)
    key = peer_box.load_or_create_messaging_key(store.home / "messaging.x25519")
    runtime = AgentRuntime(AgentStore(store.home / "facts-agent", key), bridge, tools, catalog)
    runtime.update_settings({"enabled": True, "model_id": model_id, "permission_mode": "read_only"})
    requests = fake_upstream(monkeypatch, [{"role": "assistant", "content": "Synthetic answer"}], json_only=True)
    runtime.create({"message": "Inspect the current destination."})
    runtime.run_once()
    assert len(requests) == 1
    facts = control(requests[0]["messages"])["inference_context"]
    assert facts["destination_kind"] == "local_api"
    assert facts["resources_with_matching_egress"] == 0
    assert "secret-do-not-export" not in json.dumps(requests)
    assert "https://vendor.test" not in json.dumps(requests)


def test_local_native_request_reports_local_service_route_conservatively(native_scope):  # noqa: F811
    runtime, _, selected, _, traces, _, _, _ = native_scope
    runtime.create({"message": "Inspect the current destination."})
    runtime.run_once()
    assert len(traces) == 1
    facts = control(traces[0]["messages"])["inference_context"]
    assert facts["destination_kind"] == "local_service"
    assert facts["destination_binding_recorded"] is True
    assert facts["historical_egress"] == "not_established"
    assert selected["runtime"] == "native_llama_cpp"


def test_legacy_unknown_destination_stays_unknown_in_actual_request(tmp_path):
    runtime, model, _, _ = make_runtime(tmp_path, assistant("Synthetic answer"))
    runtime.create({"message": "Inspect only."})
    runtime.run_once()
    facts = control(model.calls[0][1])["inference_context"]
    assert facts["destination_kind"] == "unknown"
    assert facts["destination_binding_recorded"] is False


@pytest.mark.parametrize("change", ["destination", "grant_id", "no_binding", "selection_only"])
def test_projection_never_promotes_stale_selection_or_nonmatching_egress(change):
    destination = {"catalog_model_id": "peer/opaque/service", "provider_peer_id": "peer",
                   "source_id": "", "destination_revision": "revision"}
    row = {"model_id": destination["catalog_model_id"], "model_destination": destination,
           "scope": {"resource_grants": [{"grant_id": "current"}],
                     "egress_grants": [{"destination": copy.deepcopy(destination), "origin_grant_ids": ["current"]}],
                     "selection": {"allow_model_data": True, "resources": [{"root_path": "PRIVATE-PATH"}]}}}
    if change == "destination":
        row["scope"]["egress_grants"][0]["destination"]["destination_revision"] = "old"
    elif change == "grant_id":
        row["scope"]["egress_grants"][0]["origin_grant_ids"] = ["old"]
    elif change == "no_binding":
        destination["destination_revision"] = ""
    else:
        row["scope"]["resource_grants"] = []
    before = copy.deepcopy(row)
    facts = projection(row)
    assert facts["resources_with_matching_egress"] == 0
    assert facts["historical_egress"] == "not_established"
    assert "PRIVATE-PATH" not in json.dumps(facts)
    assert row == before


def test_projection_copies_no_arbitrary_destination_or_resource_payload():
    destination = {"catalog_model_id": "local/service", "provider_peer_id": "local-node",
                   "source_id": "source", "destination_revision": "revision",
                   "base_url": "https://private-endpoint.test", "api_key": "PRIVATE-CREDENTIAL"}
    grant = {"grant_id": "grant", "root_resource_ref": "PRIVATE-ENCRYPTED-REF", "text": "PRIVATE-BODY" * 10000}
    row = {"model_id": destination["catalog_model_id"], "model_destination": destination,
           "scope": {"resource_grants": [grant], "egress_grants": [{
               "destination": copy.deepcopy(destination), "origin_grant_ids": ["grant"]}]}}
    facts = projection(row)
    assert facts["resources_with_matching_egress"] == 1
    assert "PRIVATE" not in json.dumps(facts) and "private-endpoint" not in json.dumps(facts)
    assert len(encoded(facts)) + len(GUIDANCE.encode()) < 1000
