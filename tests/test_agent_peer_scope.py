"""Consumer scope transactions with real authenticated local peer envelopes."""
import copy
import json

import pytest
from fastapi import FastAPI
from test_agent_peer_binding import paired, response  # noqa: F401
from test_agent_resources import file_ref, invoke, resources  # noqa: F401
from test_agent_runtime_model_switch import body
from test_agent_space_binding import space_pair  # noqa: F401
from test_personal_space import nodes  # noqa: F401

from rynmesh.agent_runtime.bridge import build_agent_integrations
from rynmesh.agent_runtime.runtime import AgentRuntime
from rynmesh.agent_runtime.store import AgentError, AgentStore
from rynmesh.ai_access.routes import install_ai_access
from rynmesh.llm_package.routes import install_llm_routes
from rynmesh.llm_package.task_protocol import open_task
from rynmesh.store import RynmeshStore


@pytest.fixture
def consumer(paired, resources, tmp_path, monkeypatch):  # noqa: F811
    p = paired
    p.consumer.refresh(p.alice.peer_id)
    store = RynmeshStore(home=p.bob.store.root.parent, network_dir=tmp_path / "network")
    app = FastAPI()
    install_ai_access(app, home=store.home, local_control=lambda _: None, relationship=p.bob.store.relationship)
    app.state.ai_access.catalog = p.consumer
    app.state.nas_plugin = resources[1]
    install_llm_routes(app, store=store, home=store.home, messaging_key=p.bob.messaging_private,
                       resolve_endpoint=lambda _: "", resolve_pubkey=lambda _: "")
    envelopes = []
    async def exchange(*, signed_request, **kwargs):
        _, value = open_task(signed_request, recipient_peer_id=p.alice.peer_id,
                             recipient_messaging_key=p.alice.messaging_private, expected_kind="llm_request")
        envelopes.append(copy.deepcopy(value))
        reply = p.providers["a"].handle(signed_request, on_event=lambda _: None)
        p.last_response = response(p, reply)
        return reply, {"transport": "local_signed_fixture", "relay_used": False}
    monkeypatch.setattr("rynmesh.llm_package.routes.consumer_exchange", exchange)
    bridge, tools, catalog = build_agent_integrations(app, store, store.home)
    runtime = AgentRuntime(AgentStore(tmp_path / "consumer-agent", p.bob.messaging_private), bridge, tools, catalog,
                           consumer_node_id=p.bob.peer_id)
    model = next(row for row in catalog.list_models() if row["source"] == "peer")
    runtime.update_settings({"enabled": True, "model_id": model["id"]})
    selection = {"resources": [{"connection_id": "docs", "root_path": "allowed", "actions": ["list", "read_ai"]}],
                 "allow_model_data": True}
    try:
        yield p, runtime, model, selection, envelopes, resources[0]
    finally:
        app.state.llm_shutdown()


def create_scoped(runtime, model, selection):
    return runtime.create({"message": "Inspect only the explicitly authorized fixture.", "scope": selection,
                           "expected_destination_revision": model["destination_revision"]})["id"]


def test_peer_scope_real_dispatch_regrant_blocks_old_history_and_switch_clears_it(consumer):
    p, runtime, model, selection, envelopes, resource_tools = consumer
    task_id = create_scoped(runtime, model, selection)
    row = runtime.store.read()["tasks"][task_id]
    context = runtime._execution_context(row)
    result = invoke(resource_tools, context, "ryn_read_resource", file_ref(resource_tools, context))
    def record(data):
        messages = data["tasks"][task_id]["messages"]
        messages.extend([{"role": "assistant", "content": "", "tool_calls": [{"id": "read", "type": "function",
            "function": {"name": "ryn_read_resource", "arguments": "{}"}}], "context_epoch": 0},
            {"role": "tool", "tool_call_id": "read", "content": json.dumps(result),
             "resource_origin": result["origin"], "context_epoch": 0}])
    runtime.store.transaction(record)
    runtime.run_once()
    assert len(envelopes) == 1 and len(p.traces) == 1, getattr(p, "last_response", runtime.get(task_id).get("error"))
    received = [message for message in p.traces[0]["messages"] if message["role"] == "tool"]
    assert json.loads(received[0]["content"])["text"] == result["text"]
    assert envelopes[0]["expected_model_binding"]["consumer_peer_id"] == p.bob.peer_id
    runtime.action(task_id, {"action": "pause"})
    p.grants.set(p.providers["a"].manifest.package_id, p.rid, allowed=False, expected_revision=1)
    p.grants.set(p.providers["a"].manifest.package_id, p.rid, allowed=True, expected_revision=2)
    p.consumer.refresh(p.alice.peer_id)
    with pytest.raises(AgentError, match="agent_model_destination_changed"):
        runtime._validate_context(runtime.store.read()["tasks"][task_id])
    with pytest.raises(AgentError, match="agent_model_destination_changed"):
        runtime.switch_model(task_id, body(runtime, task_id, model["id"], transfer_resource_scope=True))
    changed = runtime.switch_model(task_id, body(runtime, task_id, model["id"]))
    stored = runtime.store.read()["tasks"][task_id]
    assert changed["status"] == "paused" and not stored["scope"]["resource_grants"]
    assert result["text"] not in str(runtime._context(stored))
    assert stored["scope"]["selection"]["resources"] == selection["resources"]
    runtime._validate_context(stored)
    assert len(envelopes) == 1  # Switching never sends or silently transfers data.


@pytest.mark.parametrize("tamper", ["flag", "recipient", "digest", "permission", "native"])
def test_unverified_or_other_recipient_catalog_cannot_authorize_scope_or_switch(consumer, tamper):
    _, runtime, model, selection, envelopes, _ = consumer
    task_id = runtime.create({"message": "No documents yet"})["id"]
    runtime.action(task_id, {"action": "pause"})
    altered = copy.deepcopy(model)
    if tamper == "flag":
        altered["destination_binding_supported"] = False
    elif tamper == "recipient":
        altered["expected_model_binding"]["consumer_peer_id"] = "someone-else"
    elif tamper == "digest":
        altered["destination_revision"] = "0" * 64
    elif tamper == "permission":
        altered["expected_model_binding"]["ai_permission"]["revision"] += 1
    else:
        altered["source"] = "local_service"
    runtime.catalog.list_models = lambda: [altered]
    with pytest.raises(AgentError, match="agent_model_destination_unverifiable"):
        runtime.preview_model_switch(task_id, model["id"])
    with pytest.raises(AgentError, match="agent_model_destination_unverifiable"):
        create_scoped(runtime, altered, selection)
    assert not envelopes


def test_authorized_peer_scope_transfer_requires_explicit_choice_and_fresh_grants(consumer):
    _, runtime, model, selection, envelopes, _ = consumer
    task_id = create_scoped(runtime, model, selection)
    runtime.action(task_id, {"action": "pause"})
    original = runtime.store.read()["tasks"][task_id]
    request = body(runtime, task_id, model["id"], transfer_resource_scope=True)
    runtime.switch_model(task_id, request)
    stored = runtime.store.read()["tasks"][task_id]
    assert stored["scope"]["selection"] == selection
    assert stored["scope"]["resource_grants"][0]["grant_id"] != original["scope"]["resource_grants"][0]["grant_id"]
    assert stored["scope"]["egress_grants"][0]["destination"] == request["expected_destination"]
    assert stored["status"] == "paused" and not envelopes


def test_space_consumer_policy_aba_invalidates_scoped_task_and_mixed_permissions(space_pair, resources, tmp_path):  # noqa: F811
    p = space_pair
    p.consumer.refresh(p.locator)
    app = p.apps[1]
    app.state.nas_plugin = resources[1]
    app.state.llm_agent_services["discover"] = lambda _: p.consumer.records()
    bridge, tools, catalog = build_agent_integrations(app, p.child.store, p.child.store.home)
    runtime = AgentRuntime(AgentStore(tmp_path / "space-agent", p.keys[1]), bridge, tools, catalog,
                           consumer_node_id=p.child.store.peer_id)
    model = next(row for row in catalog.list_models() if row["source"] == "peer")
    runtime.update_settings({"enabled": True, "model_id": model["id"]})
    selection = {"resources": [{"connection_id": "docs", "root_path": "allowed", "actions": ["list", "read_ai"]}], "allow_model_data": True}
    task_id = create_scoped(runtime, model, selection)
    runtime.action(task_id, {"action": "pause"})
    runtime.preview_model_switch(task_id, model["id"])
    mixed = copy.deepcopy(model)
    mixed["expected_model_binding"]["ai_permission"] = {"relationship_id": "a" * 32, "revision": 1}
    with pytest.raises(AgentError, match="agent_model_destination_unverifiable"):
        runtime._bound_destination(mixed)
    p.child.set_policy("local")
    p.child.set_policy("space")
    p.consumer.refresh(p.locator)
    with pytest.raises(AgentError, match="agent_model_destination_changed"):
        runtime._validate_context(runtime.store.read()["tasks"][task_id])
    runtime.switch_model(task_id, body(runtime, task_id, model["id"]))
    stored = runtime.store.read()["tasks"][task_id]
    assert stored["status"] == "paused" and not stored["scope"]["egress_grants"]
