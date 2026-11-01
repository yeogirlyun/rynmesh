import json
from types import SimpleNamespace

import pytest
from test_agent_integrations import node as integration_node

from rynmesh.agent_runtime.bridge import build_agent_integrations
from rynmesh.agent_runtime.diagnostics import NAMES
from rynmesh.agent_runtime.store import AgentToolRejected

node = integration_node


def test_dispatched_read_failure_survives_runtime_and_safe_diagnostic_projection(tmp_path):
    from test_agent_runtime import advance, assistant, make_runtime

    from rynmesh.agent_runtime.diagnostics import DiagnosticTools
    from rynmesh.agent_runtime.store import AgentReadFailure

    runtime, _, tools, _ = make_runtime(tmp_path,
        assistant(name="inspect_service", args={"service_id": "docs-offline"}))
    def timeout(*args, **kwargs):
        raise AgentReadFailure("resource_read_timeout", connection_id="docs-offline",
                               connection_name="docs-offline token=NEVER_EXPOSE_712")
    tools.execute = timeout
    task = runtime.create({"message": "Read authorized resource"})
    advance(runtime, 2)
    row = runtime.get(task["id"])
    receipt = next(e["result"] for e in row["events"] if e["type"] == "tool_result")
    assert receipt["error"] == "resource_read_timeout" and receipt["not_executed"] is False
    assert receipt["no_mutation"] is True and receipt["connection_id"] == "docs-offline"
    assert "NEVER_EXPOSE_712" not in json.dumps(row)
    operations = DiagnosticTools._operations(row)
    assert operations["read_dispatch_count"] == 1 and operations["write_dispatch_count"] == 0
    failure = operations["operations"][0]
    assert failure["connection_name"] == "docs-offline [redacted]"
    assert failure["suggestion"]["executed"] is False


def test_real_registry_dispatches_diagnostics_and_fresh_fact_verification(node):
    app, store = node
    runtime = SimpleNamespace(owner_id="local_owner", consumer_node_id=store.peer_id,
        configuration_snapshot=lambda: {"settings_revision": 3, "settings": {"enabled": False, "model_id": "chosen-default", "permission_mode": "read_only", "max_steps": 12}},
        get=lambda task_id: {"status": "failed", "error": "resource_read_timeout", "steps": 4,
                            "messages": [{"content": "private task body"}], "result": "secret result", "events": []})
    app.state.agent_runtime = runtime
    _, tools, _ = build_agent_integrations(app, store, store.home)
    context = {"task_id": "diagnostic-current", "owner_id": "local_owner", "consumer_node_id": store.peer_id}
    assert NAMES <= {s["function"]["name"] for s in tools.specs()}
    for name in NAMES:
        args = {"task_id": "failed-other"} if name == "ryn_diagnose_task" else {}
        result = tools.execute(name, args, operation_id="diagnostic" + name, context=context, cancelled=lambda: False)
        assert "private task body" not in json.dumps(result) and "secret result" not in json.dumps(result)
        assert tools.metadata(name)["effect"] == "read"
        receipt = {"tool": name, "arguments": args, "result": result, "status": "confirmed"}
        verified = tools.verify(receipt, [{"verifier": name + "_facts"}], context=context, cancelled=lambda: False)
        assert verified["status"] == "passed"
    config = tools.execute("ryn_get_agent_configuration", {}, operation_id="config", context=context, cancelled=lambda: False)
    assert config["default_model_id"] == "chosen-default" and config["enabled"] is False
    with pytest.raises(AgentToolRejected, match="agent_diagnostics_context_denied"):
        tools.execute("ryn_diagnose_task", {"task_id": "failed-other"}, operation_id="denied", context={**context, "owner_id": "other"}, cancelled=lambda: False)
