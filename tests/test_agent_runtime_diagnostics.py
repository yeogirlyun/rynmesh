"""T08 diagnostic facts and privacy boundaries; no model/network invocation."""
import json
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from test_agent_runtime import advance, approve, assistant, make_runtime

from rynmesh.agent_runtime.diagnostics import NAMES, DiagnosticTools
from rynmesh.agent_runtime.store import AgentToolRejected


def toolset(runtime=None, models=None, evidence=None):
    if runtime is None:
        runtime = SimpleNamespace(owner_id="local-owner", consumer_node_id="this-node", settings=Mock(return_value={"enabled": False, "model_id": "chosen-offline", "permission_mode": "confirm_changes", "max_steps": 16}),
            get=Mock(return_value={"status": "failed", "run_state": "ended", "error": "agent_model_connection_failed", "steps": 3, "outcome": {"status": "unverified"}}))
        runtime.configuration_snapshot = lambda: {"settings_revision": 4, "settings": runtime.settings()}
    catalog = SimpleNamespace(list_models=Mock(return_value=models if models is not None else [
        {"id": "available-other", "name": "Other", "available": True, "agent_supported": True, "tool_support": "supported"},
        {"id": "chosen-offline", "name": "Chosen", "available": False, "agent_supported": False, "tool_support": "unknown"}]))
    return DiagnosticTools(SimpleNamespace(state=SimpleNamespace(agent_runtime=runtime)), catalog, evidence), runtime, catalog


def execute(tools, name, **kwargs):
    return tools.execute(name, {}, context={"task_id": "trusted-current"}, cancelled=lambda: False, **kwargs)


def test_real_failed_task_uses_safe_runtime_facts_without_reading_other_tasks(tmp_path):
    runtime, _, _, _ = make_runtime(tmp_path)
    row = runtime.create({"message": "PRIVATE GOAL", "model_id": "local:test"})
    runtime._fail(row["id"], "agent_model_connection_failed")
    runtime.list_tasks = Mock(side_effect=AssertionError("must not read history"))
    tools, _, _ = toolset(runtime)
    result = tools.execute("ryn_diagnose_current_task", {}, context={"task_id": row["id"]}, cancelled=lambda: False)
    assert result["status"] == "failed" and result["error"] == "agent_model_connection_failed"
    assert result["suggestions"][0]["owner_path"] == "/services/sources"
    assert result["suggestions"][0]["executed"] is False
    assert result["elapsed_seconds"] is None and result["usage"]["total_tokens"] is None
    assert "PRIVATE GOAL" not in json.dumps(result)
    runtime.list_tasks.assert_not_called()


def test_internal_input_rejection_is_diagnosed_without_payload_disclosure():
    result = DiagnosticTools._operations({"events": [{"seq": 1, "type": "tool_result", "internal": True,
        "tool": "ryn_request_input", "operation_id": "input-1", "result": {
            "error": "agent_invalid_control_proposal", "not_executed": True,
            "secret": "PRIVATE_CREDENTIAL", "arguments": "PRIVATE_ENDPOINT"}}]})
    operation = result["operations"][0]
    assert operation["error"] == "agent_invalid_control_proposal"
    assert operation["suggestion"]["id"] == "correct_input_request"
    assert operation["not_executed"] is True
    assert not result["write_dispatched"]
    assert "PRIVATE" not in json.dumps(result)


def test_actual_persisted_default_is_not_replaced_by_available_model():
    tools, runtime, _ = toolset()
    result = execute(tools, "ryn_get_agent_configuration")
    assert result["enabled"] is False
    assert result["settings_revision"] == 4
    assert result["default_model_id"] == "chosen-offline"
    assert result["default_model"]["available"] is False
    assert result["evidence"]["default_model_selected_from"] == "settings.model_id"
    runtime.get.assert_not_called()


def test_missing_default_stays_missing_and_catalog_ready_is_not_probe():
    tools, runtime, _ = toolset()
    runtime.settings.return_value["model_id"] = "missing-model"
    result = execute(tools, "ryn_get_agent_configuration")
    assert result["default_model"] is None and result["default_model_in_catalog"] is False
    evidence = execute(tools, "ryn_get_system_diagnostics")["models"][0]["evidence"]
    assert evidence["read_operation"]["connection_probe_performed"] is False
    assert evidence["read_operation"]["inference_probe_performed"] is False
    assert evidence["recorded_probes"]["connection"]["status"] == "unknown"
    assert evidence["history_coverage"] == "not_established"


def test_diagnostic_read_retains_saved_probe_subject_time_and_result_without_claiming_new_probe():
    stamp = "2026-01-01T01:00:00Z"
    tools, runtime, catalog = toolset(models=[
        {"id": "chosen-offline", "available": False, "evidence": {
            "connection": {"status": "failed", "checked_at": stamp, "method": "fixed_chat"}}},
        {"id": "other", "available": True, "evidence": {
            "tools": {"status": "supported", "checked_at": stamp, "method": "two_turn_client_tool"}}}])
    result = execute(tools, "ryn_get_system_diagnostics")
    saved = result["models"][0]["evidence"]
    assert saved["recorded_probes"]["connection"] == {"status": "failed", "checked_at": stamp, "method": "fixed_chat"}
    assert saved["read_operation"] == {"connection_probe_performed": False, "inference_probe_performed": False}
    assert saved["history_coverage"] == "not_established"
    assert result["models"][1]["evidence"]["recorded_probes"]["connection"]["status"] == "unknown"
    assert result["models"][1]["evidence"]["recorded_probes"]["tools"]["status"] == "supported"
    assert execute(tools, "ryn_get_agent_configuration")["default_model"]["evidence"] == saved
    # No writeback, timestamp refresh or inference endpoint is part of this read.
    assert catalog.list_models.return_value[0]["evidence"]["connection"]["checked_at"] == stamp
    runtime.get.assert_not_called()


@pytest.mark.parametrize("value", [None, [], "Bearer probe-secret", {"connection": "Bearer probe-secret"},
    {"connection": {"status": ["passed"], "checked_at": "Bearer probe-secret", "method": "Bearer probe-secret", "api_key": "probe-secret"}}])
def test_probe_projection_rejects_malformed_records_and_drops_untrusted_fields(value):
    tools, _, _ = toolset(models=[{"id": "model", "evidence": value}])
    row = execute(tools, "ryn_get_system_diagnostics")["models"][0]
    assert row["evidence"]["recorded_probes"]["connection"] == {"status": "unknown", "checked_at": None, "method": "unknown"}
    assert "probe-secret" not in json.dumps(row)


def test_only_whitelisted_current_task_facts_and_safe_errors_leave_node():
    tools, runtime, _ = toolset()
    runtime.get.return_value.update(goal={"text": "private goal"}, messages=[{"content": "private chat"}], result="private result", summary="private summary", events=[{"secret": "private event"}], error="HTTP failed Bearer private-credential", usage={"input_tokens": 9, "secret": "private usage"}, outcome={"status": "unknown", "summary": "private outcome"})
    result = execute(tools, "ryn_diagnose_current_task")
    serialized = json.dumps(result)
    assert "private" not in serialized
    assert result["error"] == "agent_unknown_error" and result["outcome_status"] == "unknown"
    assert result["usage"]["input_tokens"] == 9
    runtime.get.assert_called_once_with("trusted-current")


def test_unknown_write_advice_does_not_repeat_or_claim_success():
    tools, runtime, _ = toolset()
    runtime.get.return_value.update(status="interrupted", error="agent_write_outcome_unknown", outcome={"status": "unknown"})
    result = execute(tools, "ryn_diagnose_current_task")
    assert result["suggestions"][0]["id"] == "reconcile_original_write"
    assert "do not repeat" in result["suggestions"][0]["instruction"]
    assert result["outcome_status"] == "unknown"


def test_device_evidence_is_reused_and_untested_reachability_stays_unknown():
    evidence = Mock(return_value={"devices": [
        {"peer_id": "self", "online": True, "evidence_source": "self", "http_probe_performed": False, "http_reachable": True, "lan_reachable": True, "endpoint": "http://user:password@example.test/private"},
        {"peer_id": "remote", "online": None, "evidence_source": "unknown", "http_probe_performed": True, "http_reachable": False, "lan_probe_performed": False, "lan_reachable": False} ], "friends": [{"private_key": "never"}]})
    tools, _, _ = toolset(evidence=evidence)
    result = execute(tools, "ryn_get_system_diagnostics")
    evidence.assert_called_once()
    assert result["devices"]["devices"][0]["http_reachable"] is None
    assert result["devices"]["devices"][0]["lan_reachable"] is None
    assert result["devices"]["devices"][1]["online"] is None
    assert result["devices"]["devices"][1]["http_reachable"] is False
    assert "password" not in json.dumps(result) and "private_key" not in json.dumps(result)


def test_model_projection_drops_secrets_and_redacts_untrusted_labels():
    tools, _, _ = toolset(models=[{"id": "safe", "name": "Bearer credential-leak", "api_key": "private credential", "endpoint": "https://secret-host/", "source_id": "source", "available": True, "agent_supported": True, "tool_support": "supported", "ready": True}])
    result = execute(tools, "ryn_get_system_diagnostics")
    assert result["models"][0]["name"] == "[redacted]"
    assert "credential" not in json.dumps(result) and "secret-host" not in json.dumps(result)
    assert "ready" not in result["models"][0]


def test_model_cannot_choose_task_id_and_missing_context_is_rejected():
    tools, runtime, _ = toolset()
    with pytest.raises(AgentToolRejected, match="agent_tool_arguments_invalid"):
        tools.execute("ryn_diagnose_current_task", {"task_id": "another"}, context={"task_id": "trusted-current"}, cancelled=lambda: False)
    with pytest.raises(AgentToolRejected, match="agent_task_context_required"):
        tools.execute("ryn_diagnose_current_task", {}, context={}, cancelled=lambda: False)
    runtime.get.assert_not_called()


def test_diagnostics_are_read_only_and_cancel_before_any_read():
    tools, runtime, catalog = toolset()
    for name in NAMES:
        assert tools.metadata(name)["effect"] == "read"
        assert tools.metadata(name)["scope_context_supported"] is True
        with pytest.raises(AgentToolRejected, match="agent_cancelled"):
            tools.execute(name, {"task_id": "existing"} if name == "ryn_diagnose_task" else {}, context={"task_id": "trusted-current"}, cancelled=lambda: True)
    runtime.get.assert_not_called()
    runtime.settings.assert_not_called()
    catalog.list_models.assert_not_called()
    assert all(spec["function"]["parameters"]["additionalProperties"] is False for spec in tools.specs())


def test_diagnostics_unavailable_and_provider_failures_return_no_raw_exception():
    tools, _, catalog = toolset()
    tools.app.state.agent_runtime = None
    with pytest.raises(AgentToolRejected, match="^agent_diagnostics_unavailable$"):
        execute(tools, "ryn_get_agent_configuration")
    catalog.list_models.side_effect = RuntimeError("Bearer secret-never-return")
    with pytest.raises(AgentToolRejected, match="^agent_diagnostics_catalog_unavailable$"):
        execute(tools, "ryn_get_system_diagnostics")


def test_owner_target_failure_projection_locates_timeout_and_proves_no_write_without_content():
    tools, runtime, _ = toolset()
    runtime.get.return_value.update(error="resource_read_timeout", title="private title", messages=[{"content": "private goal"}], events=[
        {"type": "tool_started", "tool": "read:docs-offline", "effect": "read", "operation_id": "read-op", "arguments": {"path": "private path"}},
        {"type": "tool_result", "tool": "read:docs-offline", "operation_id": "read-op", "result": {"error": "resource_read_timeout", "connection_id": "docs-offline", "connection_name": "Docs offline", "content": "private file", "path": "private path", "api_key": "private key"}},
        {"type": "tool_result", "tool": "internal", "internal": True, "operation_id": "internal-op", "result": {"content": "private internal"}},
    ])
    result = tools.execute("ryn_diagnose_task", {"task_id": "task-failed-read"}, context={"owner_id": "local-owner", "consumer_node_id": "this-node"}, cancelled=lambda: False)
    runtime.get.assert_called_once_with("task-failed-read")
    assert result["task_id"] == "task-failed-read"
    assert result["error"] == "resource_read_timeout"
    assert result["write_dispatched"] is False and result["write_dispatch_count"] == 0
    assert result["read_dispatch_count"] == 1 and len(result["operations"]) == 1
    operation = result["operations"][0]
    assert operation["tool"] == "read:docs-offline" and operation["error"] == "resource_read_timeout"
    assert operation["connection_id"] == "docs-offline"
    assert operation["suggestion"]["owner_path"] == "/drives"
    assert operation["suggestion"]["executed"] is False
    assert "private" not in json.dumps(result)


def test_actual_failed_read_operation_is_projected_without_rerunning_it(tmp_path):
    runtime, _, _, _ = make_runtime(tmp_path, assistant(name="read:docs-offline"))
    read = Mock(side_effect=AgentToolRejected("resource_read_timeout"))
    runtime.tools = SimpleNamespace(specs=lambda: [{"type": "function", "function": {"name": "read:docs-offline", "description": "Read docs", "parameters": {"type": "object", "properties": {}, "additionalProperties": False}}}], metadata=lambda name: {"effect": "read", "title": "Read docs"}, execute=read)
    row = runtime.create({"message": "PRIVATE task data", "model_id": "local:test"})
    advance(runtime, 2)
    # The ordinary runtime allows a model to react to a rejected read. Freeze
    # the evaluation's failed-task state after the actual single failed dispatch.
    runtime._fail(row["id"], "resource_read_timeout")
    failed = runtime.get(row["id"])
    assert failed["status"] == "failed"
    tools, _, _ = toolset(runtime)
    result = tools.execute("ryn_diagnose_task", {"task_id": row["id"]}, context={"owner_id": runtime.owner_id, "consumer_node_id": runtime.consumer_node_id}, cancelled=lambda: False)
    assert result["operations"][0]["error"] == "resource_read_timeout"
    assert result["operations"][0]["tool"] == "read:docs-offline"
    assert result["write_dispatched"] is False
    read.assert_called_once()
    assert "PRIVATE" not in json.dumps(result)


def test_current_task_reports_recoverable_permission_rejection_without_inventing_cause(tmp_path):
    runtime, _, business, _ = make_runtime(tmp_path,
        assistant(name="share_service", args={"service_id": "PRIVATE-service"}))
    row = runtime.create({"message": "PRIVATE goal"})
    advance(runtime, 2)
    assert runtime.get(row["id"])["status"] == "waiting_approval"
    approve(runtime, row["id"])
    business.execute = Mock(side_effect=AgentToolRejected("agent_permission_denied"))
    advance(runtime)
    persisted = runtime.get(row["id"])
    assert persisted["error"] is None  # A model may respond to a tool rejection.
    tools, _, _ = toolset(runtime)
    result = tools.execute("ryn_diagnose_current_task", {},
        context={"task_id": row["id"]}, cancelled=lambda: False)
    operation, = result["operations"]
    assert operation["error"] == "agent_permission_denied"
    assert operation["not_executed"] is True
    assert result["error"] is None and result["write_dispatch_count"] == 1
    assert operation["suggestion"]["id"] == "review_permissions"
    assert "code alone does not identify" in operation["suggestion"]["instruction"]
    assert "PRIVATE" not in json.dumps(result)
    business.execute.assert_called_once()
    assert business.shared is False


@pytest.mark.parametrize("context", [{}, {"owner_id": "another", "consumer_node_id": "this-node"}, {"owner_id": "local-owner", "consumer_node_id": "another"}])
def test_target_diagnostics_requires_trusted_owner_and_consumer_node(context):
    tools, runtime, _ = toolset()
    with pytest.raises(AgentToolRejected, match="^agent_diagnostics_context_denied$"):
        tools.execute("ryn_diagnose_task", {"task_id": "existing"}, context=context, cancelled=lambda: False)
    runtime.get.assert_not_called()
