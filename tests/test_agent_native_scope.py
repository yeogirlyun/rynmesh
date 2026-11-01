"""Native consumer scope uses the real local catalog, HTTP read and final gate."""
import copy
import json

import pytest
from test_agent_integrations import node  # noqa: F401
from test_agent_resources_http import http_resources, reference  # noqa: F401
from test_native_model_policy import native  # noqa: F401

from rynmesh.agent_runtime.bridge import build_agent_integrations
from rynmesh.agent_runtime.runtime import AgentRuntime
from rynmesh.agent_runtime.store import DEFAULT_SETTINGS, AgentError, AgentStore
from rynmesh.services import peer_box


@pytest.fixture
def native_scope(native, http_resources):  # noqa: F811
    app, store, model, traces = native
    # The egress fixture needs room for the authorized document and schemas.
    # The separate 4K case below checks automatic small-window projection.
    model.metadata["context_window"] = 32768
    model._update_manifest()
    store.llm_tool_capabilities.save(model.manifest, "supported")
    _, nas, source, dav, _, text = http_resources
    app.state.nas_plugin = nas
    bridge, tools, catalog = build_agent_integrations(app, store, store.home)
    key = peer_box.load_or_create_messaging_key(store.home / "messaging.x25519")
    runtime = AgentRuntime(AgentStore(store.home / "native-agent", key), bridge, tools, catalog, consumer_node_id=store.peer_id)
    app.state.agent_runtime = runtime
    selected = next(row for row in catalog.list_models() if row["runtime"] == "native_llama_cpp")
    runtime.update_settings({"enabled": True, "model_id": selected["id"]})
    selection = {"resources": [{"connection_id": source["id"], "root_path": "allowed", "actions": ["list", "read_ai"]}],
                 "allow_model_data": True}
    return runtime, tools, selected, model, traces, selection, dav, text


def test_native_authorized_http_resource_reaches_only_bound_adapter(native_scope):
    runtime, tools, selected, model, traces, selection, dav, expected = native_scope
    task = runtime.create({"message": "Read only my authorized fixture.", "scope": selection,
                           "expected_destination_revision": selected["destination_revision"]})
    row = runtime.store.read()["tasks"][task["id"]]
    context = runtime._execution_context(row)
    ref = reference(tools, context)
    result = tools.execute("ryn_read_resource", {"resource_ref": ref}, operation_id="fixture-read",
                           context=context, cancelled=lambda: False)
    assert result["text"] == expected[:2000]
    def record(data):
        data["tasks"][task["id"]]["messages"].extend([
            {"role": "assistant", "content": "", "context_epoch": 0, "tool_calls": [{"id": "read-fixture", "type": "function",
                "function": {"name": "ryn_read_resource", "arguments": json.dumps({"resource_ref": ref})}}]},
            {"role": "tool", "tool_call_id": "read-fixture", "content": json.dumps(result), "context_epoch": 0,
             "resource_origin": result["origin"]}])
    runtime.store.transaction(record)
    runtime.run_once()
    status = runtime.get(task["id"])
    assert len(traces) == 1, (status["status"], status.get("error"))
    assert expected[:2000] in json.dumps(traces[0])
    assert "Must never enter model data" not in json.dumps(traces)
    assert not any(event["mutation"] for event in dav.audit)
    old = runtime.store.read()["tasks"][task["id"]]
    model.rotate_model_binding()
    with pytest.raises(AgentError, match="agent_model_destination_changed"):
        runtime._validate_context(old)
    assert len(traces) == 1


def test_small_native_window_projects_tools_before_actual_dispatch(native):  # noqa: F811
    app, store, model, traces = native
    bridge, tools, catalog = build_agent_integrations(app, store, store.home)
    key = peer_box.load_or_create_messaging_key(store.home / "messaging.x25519")
    runtime = AgentRuntime(AgentStore(store.home / "small-agent", key), bridge, tools, catalog, consumer_node_id=store.peer_id)
    selected = next(row for row in catalog.list_models() if row["runtime"] == "native_llama_cpp")
    assert selected["context_window"] == 4096
    runtime.update_settings({"enabled": True, "model_id": selected["id"]})
    task = runtime.create({"message": "Check my model configuration."})
    runtime.run_once()
    assert len(traces) == 1, runtime.get(task["id"]).get("error")
    assert "ryn_discover_tools" in [tool["function"]["name"] for tool in traces[0]["tools"]]
    assert len(json.dumps([traces[0]["messages"], traces[0]["tools"]], ensure_ascii=False)) // 3 + traces[0]["max_tokens"] <= 4096
    # The saved owner limit is the unchanged default; the 4K window is clamped only in the working projection.
    assert runtime.get(task["id"])["limits"]["max_context_chars"] == DEFAULT_SETTINGS["max_context_chars"]


@pytest.mark.parametrize("change", [
    {"runtime": "codex_cli"}, {"peer_id": "another-node"}, {"destination_binding_supported": False},
    {"destination_revision": "unrelated"}, {"local_model_binding": None}, {"service_id": "different-model"},
])
def test_unverifiable_local_service_never_inherits_native_data_access(native_scope, change):
    runtime, _, selected, _, traces, _, _, _ = native_scope
    invalid = {**copy.deepcopy(selected), **change}
    with pytest.raises(AgentError, match="agent_model_destination_unverifiable"):
        runtime._bound_destination(invalid)
    assert not traces
