"""Synthetic runtime acceptance, not a real-model or real-message E2E."""
import copy

import pytest
from test_agent_extensions_runtime import _assistant

from rynmesh.agent_runtime.extensions.store import compute_model_tool_name

pytest_plugins = ["test_agent_extensions_runtime"]


@pytest.mark.parametrize("change_after_approval", [False, True])
@pytest.mark.parametrize("remote", [False, True])
def test_managed_preview_and_durable_dispatch(ext_env, monkeypatch, change_after_approval, remote):
    runtime, model, tools, _, _ = ext_env
    store = tools.extensions.store
    srv = store.upsert_mcp_server({"id": "managed", "name": "Synthetic Managed Service",
        "transport": "streamable_http", "url": "https://synthetic.example.test/mcp" if remote else "http://127.0.0.1:19294/mcp", "enabled": True,
        "headers": {} if remote else {"X-Ryn-Service-Token": "synthetic-instance-token"}})
    store.record_mcp_discovery("managed", expected_revision=srv["revision"], status="ready",
        discovered_tools=[{"name": "send", "inputSchema": {"type": "object",
            "properties": {"text": {"type": "string"}}, "required": ["text"], "additionalProperties": False,
            **({"x-ryn-operation": "1"} if remote else {})}}])
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


def test_inline_export_is_saved_as_task_artifact_without_binary_model_content(ext_env):
    import base64
    import hashlib
    import json

    from rynmesh.agent_runtime import workspace_artifacts
    runtime, _, tools, workspace, _ = ext_env
    task_id = runtime.create({"message": "Synthetic export"})["id"]
    row = runtime.store.read()['tasks'][task_id]
    raw = b'synthetic,csv\namount,123.45\n'
    blob = base64.b64encode(raw).decode()
    result = tools.extensions._normalize_mcp_result({'content': [{'type': 'resource', 'resource': {
        'uri': 'ryn-artifact:synthetic', 'mimeType': 'text/csv', 'blob': blob}}]},
        binding={'server_id': 'synthetic', 'original_tool_name': 'export_file', 'model_tool_name': 'mcp__synthetic__export_file'},
        context=runtime._execution_context(row), operation_id='export-operation')
    assert blob not in json.dumps(result) and 'images' not in result
    artifact = result['mcp_artifacts'][0]
    assert (workspace / artifact['path']).read_bytes() == raw
    assert artifact['sha256'] == hashlib.sha256(raw).hexdigest()
    row['receipts'] = [{'tool': 'mcp__synthetic__export_file', 'status': 'confirmed', 'operation_id': 'export-operation', 'result': result}]
    assert len(workspace_artifacts.history(row)) == 1


def test_lost_write_response_queries_declared_read_result_once(ext_env, monkeypatch):
    runtime, model, tools, _, _ = ext_env
    store = tools.extensions.store
    server = store.upsert_mcp_server({"id": "recovery", "name": "Recovery fixture",
        "transport": "streamable_http", "url": "https://synthetic.example.test/mcp", "enabled": True,
        "read_only_tools": ["status"]})
    schema = {"type": "object", "properties": {}, "additionalProperties": False}
    store.record_mcp_discovery("recovery", expected_revision=server["revision"], status="ready", discovered_tools=[
        {"name": "send", "inputSchema": {**schema, "x-ryn-operation": "1",
            "x-ryn-result": {"tool": "status", "argument": "request_id"}}},
        {"name": "status", "inputSchema": {**schema, "properties": {"request_id": {"type": "string"}}, "required": ["request_id"]}}])
    calls = []
    def call(server, tool, arguments, **kw):
        if kw.get("prepare"):
            return {"structuredContent": {"preview": {"digest": "a" * 64, "fields": [{"label": "Target", "value": "Synthetic"}]}}}
        kw["before_dispatch"]()
        calls.append((tool, arguments, kw.get("trusted_operation")))
        if tool == "send":
            raise TimeoutError("synthetic response lost")
        return {"structuredContent": {"status": "unknown", "request_id": arguments["request_id"]}}
    monkeypatch.setattr(tools.extensions.manager, "call_tool", call)
    send = compute_model_tool_name("recovery", "send")
    model.responses = [_assistant(name="ryn_discover_tools", args={"load": [send]}, call_id="discover"), _assistant(name=send, call_id="send"), _assistant(content="Outcome unknown; do not resend.")]
    task_id = runtime.create({"message": "Synthetic send"})["id"]
    for _ in range(4):
        runtime.run_once()
    state = runtime.get(task_id)
    runtime.action(task_id, {"action": "approve", "approval_id": state["pending_approval"]["id"],
                            "expected_control_revision": state["control_revision"]})
    runtime.run_once()
    assert [row[0] for row in calls] == ["send", "status"]
    assert calls[1][1]["request_id"] == calls[0][2]["operation_id"]
    receipt = runtime.store.read()["tasks"][task_id]["receipts"][-1]
    assert receipt["result"]["recovery"]["write_replayed"] is False
