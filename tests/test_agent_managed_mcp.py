"""Synthetic runtime acceptance, not a real-model or real-message E2E."""
import copy

import pytest
from test_agent_extensions_runtime import _assistant

from rynmesh.agent_runtime.extensions.store import compute_model_tool_name

pytest_plugins = ["test_agent_extensions_runtime"]


@pytest.mark.parametrize("change_after_approval", [False, True])
def test_managed_preview_and_durable_dispatch(ext_env, monkeypatch, change_after_approval):
    runtime, model, tools, _, _ = ext_env
    store = tools.extensions.store
    srv = store.upsert_mcp_server({"id": "managed", "name": "Synthetic Managed Service",
        "transport": "streamable_http", "url": "http://127.0.0.1:19294/mcp", "enabled": True,
        "headers": {"X-Ryn-Service-Token": "synthetic-instance-token"}})
    store.record_mcp_discovery("managed", expected_revision=srv["revision"], status="ready",
        discovered_tools=[{"name": "send", "inputSchema": {"type": "object",
            "properties": {"text": {"type": "string"}}, "required": ["text"], "additionalProperties": False}}])
    name = compute_model_tool_name("managed", "send")
    preview = {"digest": "a"*64, "fields": [{"label": "Recipient", "value": "Synthetic Recipient"},
                                              {"label": "Body", "value": "Synthetic text"}]}
    dispatches = []
    def call(server, tool, arguments, **kwargs):
        assert server == "managed" and tool == "send"
        if kwargs.get("prepare"):
            return {"structuredContent": {"preview": copy.deepcopy(preview)}}
        operation = kwargs["trusted_operation"]
        prior = tools.extensions.ledger_factory().get(operation["operation_id"])
        assert prior["status"] == "intent"
        kwargs["before_dispatch"]()
        persisted = tools.extensions.ledger_factory().get(operation["operation_id"])
        assert persisted["status"] == "dispatched"
        assert persisted["trusted_operation"] == operation
        assert persisted["preview"]["service_preview"]["digest"] == operation["preview_digest"]
        assert operation["authority_revision"]["settings_revision"] is not None
        dispatches.append(operation)
        return {"content": [{"type": "text", "text": "synthetic queued"}]}
    monkeypatch.setattr(tools.extensions.manager, "call_tool", call)
    model.responses = [_assistant(name="ryn_discover_tools", args={"load": [name]}, call_id="discover"),
                       _assistant(name=name, args={"text": "Synthetic text"}, call_id="send"),
                       _assistant(content="Done")]
    task_id = runtime.create({"message": "Synthetic managed send"})["id"]
    for _ in range(4):
        runtime.run_once()
    state = runtime.get(task_id)
    assert state["status"] == "waiting_approval"
    approval = state["pending_approval"]
    assert approval["preview"]["service_preview"] == preview
    runtime.action(task_id, {"action": "approve", "approval_id": approval["id"],
                            "expected_control_revision": state["control_revision"]})
    if change_after_approval:
        preview["digest"] = "b"*64
        preview["fields"][0]["value"] = "Changed recipient"
    runtime.run_once()
    assert len(dispatches) == (0 if change_after_approval else 1)
