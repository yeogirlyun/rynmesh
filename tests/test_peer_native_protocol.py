"""Authenticated peers retain native continuations only for a bound source."""
import copy
import json
from types import SimpleNamespace

import pytest
from test_agent_integrations import node  # noqa: F401
from test_agent_runtime_model_switch import body as switch_body
from test_agent_runtime_model_switch import setup as switch_setup

from rynmesh.agent_runtime.bridge import AgentModelBridge
from rynmesh.agent_runtime.models import ModelCatalog
from rynmesh.agent_runtime.store import AgentError
from rynmesh.llm_package import peer_binding
from rynmesh.llm_package.sources import SourceStore


def peer_catalog(tmp_path, protocol, *, bound=True, ambiguous=False):
    consumer = SimpleNamespace(peer_id="consumer")
    record = {"peer_id": "provider", "node_name": "Native source", "online": True, "ready": True,
              "chat_protocol": "rynmesh.chat.v1", "binding_verified": bound, "consumer_peer_id": consumer.peer_id,
              "model_binding": {"version": peer_binding.VERSION, "service_id": "source-model",
                                "model_generation": "a" * 32, "rule_revision": "b" * 32},
              "ai_permission": {"relationship_id": "c" * 32, "revision": 1},
              "service": {"package_id": "source-model", "model_alias": "Native model", "adapter": "openai_compatible",
                          "context_window": 32768, "max_output_tokens": 4096,
                          "capabilities": ["client-tools", "protocol:" + protocol]}}
    if ambiguous:
        other = "openai_responses" if protocol == "anthropic_messages" else "anthropic_messages"
        record["service"]["capabilities"].append("protocol:" + other)
    sources = SourceStore(tmp_path / "consumer", b"key")
    services = {"source_store": sources, "discover": lambda _: [copy.deepcopy(record)]}
    return ModelCatalog(services, consumer), services


@pytest.mark.parametrize("protocol", ["openai_responses", "anthropic_messages"])
@pytest.mark.parametrize("bound,ambiguous", [(True, False), (False, False), (True, True)])
def test_only_unique_authenticated_protocol_marker_enables_native_peer_label(tmp_path, protocol, bound, ambiguous):
    catalog, _ = peer_catalog(tmp_path, protocol, bound=bound, ambiguous=ambiguous)
    row = catalog.list_models()[0]
    assert row["adapter"] == (protocol if bound and not ambiguous else "openai_compatible")
    assert row["destination_binding_supported"] is bound
    assert bool(row["expected_model_binding"]) is bound
    assert row["agent_supported"]  # all paths still expose Ryn's canonical client tools


@pytest.mark.parametrize("protocol", ["openai_chat", "openai_responses", "anthropic_messages"])
def test_source_public_manifest_declares_native_protocol_without_changing_transport(node, protocol):  # noqa: F811
    app, _ = node
    services = app.state.llm_agent_services
    sources = services["source_store"]
    record = sources.prepare({"name": "Native API", "kind": "api", "base_url": "https://vendor.test/v1",
                              "api_key": "fixture-key", "models": ["m"], "protocol": protocol})
    record["status"] = "ready"
    sources.save(record)
    manager = services["active_source_managers"]()[sources.model_service_id(record, "m")]
    public = manager.manifest.public_dict()
    assert public["adapter"] == "openai_compatible"
    markers = [cap for cap in public["capabilities"] if cap.startswith("protocol:")]
    assert markers == ([] if protocol == "openai_chat" else ["protocol:" + protocol])
    assert "fixture-key" not in json.dumps(public)


@pytest.mark.parametrize("protocol", ["openai_responses", "anthropic_messages"])
def test_bound_native_peer_tool_envelope_retains_private_state_on_continuation(tmp_path, protocol):
    catalog, services = peer_catalog(tmp_path, protocol)
    row = catalog.list_models()[0]
    call = {"id": "native_call", "type": "function", "function": {"name": "ryn_file_read", "arguments": '{"path":"a.txt"}'}}
    blocks = ([{"type": "thinking", "thinking": "PRIVATE_CONTINUATION", "signature": "PRIVATE_SIGNATURE"},
               {"type": "text", "text": "Reading."},
               {"type": "tool_use", "id": "native_call", "name": "ryn_file_read", "input": {"path": "a.txt"}}]
              if protocol == "anthropic_messages" else [
                  {"type": "reasoning", "id": "rs_native", "summary": [], "encrypted_content": "PRIVATE_CONTINUATION"},
                  {"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": "Reading."}]},
                  {"type": "function_call", "id": "fc_native", "call_id": "native_call", "name": "ryn_file_read", "arguments": '{"path":"a.txt"}'}])
    private = {"protocol": protocol, "scope": "d" * 64,
               "data": {"content" if protocol == "anthropic_messages" else "output": blocks}}
    requested = []

    async def execute(request):
        requested.append(copy.deepcopy(request))
        message = ({"role": "assistant", "content": "Reading.", "tool_calls": [call], "provider_state": copy.deepcopy(private)}
                   if len(requested) == 1 else {"role": "assistant", "content": "Confirmed."})
        return {"state": "succeeded", "message": message,
                "finish_reason": "tool_calls" if len(requested) == 1 else "stop",
                "reported_usage": {"input_tokens": 1, "output_tokens": 1}}

    services.update(execute_order=execute, cancel_order=lambda _: None)
    bridge = AgentModelBridge(services, catalog)
    destination = catalog.destination(row)
    messages = [{"role": "user", "content": "Read a.txt."}]
    first = bridge.complete(row["id"], messages, [], lambda: False, expected_destination=destination)
    assert first["message"]["provider_state"]["data"] == private["data"]
    messages.extend([first["message"], {"role": "tool", "tool_call_id": "native_call", "content": "actual file data"}])
    second = bridge.complete(row["id"], messages, [], lambda: False, expected_destination=destination)
    continued = next(m for m in requested[1]["chat"]["messages"] if m.get("provider_state"))
    assert continued["provider_state"]["data"] == private["data"]
    assert requested[1]["expected_model_binding"] == row["expected_model_binding"]
    assert second["message"]["content"] == "Confirmed."


@pytest.mark.parametrize("protocol", ["openai_responses", "anthropic_messages"])
@pytest.mark.parametrize("missing_binding", [True, False])
def test_native_peer_missing_or_changed_destination_never_receives_private_state(tmp_path, protocol, missing_binding):
    catalog, services = peer_catalog(tmp_path, protocol)
    row = catalog.list_models()[0]
    destination = catalog.destination(row)
    dispatched = []
    async def execute(request):
        dispatched.append(request)
        raise AssertionError("private state must never dispatch to this destination")
    services.update(execute_order=execute, cancel_order=lambda _: None)
    if not missing_binding:
        changed = services["discover"](catalog.network)[0]
        changed["model_binding"]["model_generation"] = "f" * 32
        services["discover"] = lambda _: [copy.deepcopy(changed)]
    bridge = AgentModelBridge(services, catalog)
    messages = [{"role": "assistant", "content": "Prior.", "provider_state": {
        "protocol": protocol, "scope": "e" * 64, "data": {"content": "PRIVATE_OLD_PROVIDER"}}},
                {"role": "user", "content": "Continue."}]
    with pytest.raises(AgentError, match="agent_model_destination_unverifiable" if missing_binding else "agent_model_destination_changed"):
        bridge.complete(row["id"], messages, [], lambda: False,
                        expected_destination=None if missing_binding else destination)
    assert dispatched == []


def test_explicit_model_switch_retires_private_state_with_existing_context_epoch(tmp_path):
    runtime, _, _, _, task_id, _ = switch_setup(tmp_path)
    def append(data):
        row = data["tasks"][task_id]
        row["messages"].append({"role": "assistant", "content": "Old model reply.", "context_epoch": 0,
                                "provider_state": {"protocol": "anthropic_messages", "scope": "e" * 64,
                                                   "data": {"content": [{"type": "thinking", "thinking": "PRIVATE_OLD_PROVIDER"}]}}})
    runtime.store.transaction(append)
    runtime.switch_model(task_id, switch_body(runtime, task_id))
    stored = runtime.store.read()["tasks"][task_id]
    assert "PRIVATE_OLD_PROVIDER" in json.dumps(stored)  # encrypted audit remains
    assert "PRIVATE_OLD_PROVIDER" not in json.dumps(runtime._context(stored))
    assert not any(message.get("provider_state") for message in runtime._history(stored))
