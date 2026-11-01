"""Real runtime/adapter/store policy gates with synthetic model and MCP transport."""
import copy
import json
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
from test_agent_extensions_oauth import no_network  # noqa: F401
from test_agent_extensions_runtime import _assistant
from test_agent_extensions_runtime import ext_env as ext_env
from test_agent_runtime import advance, compaction_history

from rynmesh.agent_runtime import budget, data_policy, results
from rynmesh.agent_runtime.code_mode import CodeModeRunner
from rynmesh.agent_runtime.extensions import compute_model_tool_name
from rynmesh.agent_runtime.extensions.routes import install_extension_routes
from rynmesh.agent_runtime.store import AgentError, AgentToolRejected

DENY_MEMORY = {"models": {"mode": "any"}, "memory": "deny"}
ALLOW = {"models": {"mode": "allowlist", "allow": [{"catalog_model_id": "local:test"}]}, "memory": "allow"}


@pytest.fixture
def env(ext_env, monkeypatch):  # noqa: F811 - imported pytest fixture parameter
    runtime, model, tools, workspace, home = ext_env
    rows = [{"id": name, "name": name, "source": "local_api", "source_id": name,
             "peer_id": "self", "destination_revision": name + "-v1", "available": True,
             "agent_supported": True} for name in ("local:test", "local:other")]
    runtime.catalog.list_models = lambda: copy.deepcopy(rows)
    model.supports_destination_binding = model.supports_data_policy = model.supports_conversation_key = True
    def context(model_id, expected):
        row = next(row for row in rows if row["id"] == model_id)
        if expected != runtime._destination(row):
            raise AgentError("agent_model_destination_changed")
        return {"model": copy.deepcopy(row), "model_destination": copy.deepcopy(expected),
                "model_destination_verified": True, "consumer_node_id": "self"}
    model.data_policy_context = context
    # This lightweight host predates the generic model-context interface.
    tools.supports_model_context = True
    tools.specs = lambda context=None: tools.extensions.specs(context)
    monkeypatch.setenv("RYNMESH_P2P_STUN", "off")
    yield runtime, model, tools, rows


def install(env, policy=DENY_MEMORY, *, trust=True, identifier="restricted"):
    runtime, _, tools, _ = env
    adapter = tools.extensions
    preview = adapter.imports.preview({"kind": "mcp", "config_text": json.dumps({"mcpServers": {identifier: {
        "url": "https://mcp.example.test/tools", "auth": {"mode": "none"}, "ryn": {"dataPolicy": policy}}}})})
    adapter.imports.commit(preview["preview_id"], expected_revision=preview["expected_revision"])
    row = adapter.store.get_mcp_server_internal(identifier)
    row = adapter.store.upsert_mcp_server({"enabled": True}, server_id=identifier, expected_revision=row["revision"])
    row = adapter.store.record_mcp_discovery(identifier, expected_revision=row["revision"], status="ready", discovered_tools=[
        {"name": "read", "inputSchema": {"type": "object", "properties": {}}, "annotations": {"readOnlyHint": True}}])
    if trust:
        row = adapter.store.apply_server_hints(identifier, confirmation(row))
    return row, compute_model_tool_name(identifier, "read")


def confirmation(row):
    return {"expected_revision": row["revision"], "instance_id": row["instance_id"],
            "discovery_revision": row["discovery_revision"], "tools": [
                {"name": tool["name"], "schema_sha256": tool["schema_sha256"]} for tool in row["server_hint_candidates"]]}


def task_row(runtime):
    created = runtime.create({"message": "Inspect the synthetic record"})
    return runtime.store.read()["tasks"][created["id"]]


def active_tick(runtime, selected):
    """Claim the same durable authority tuple used by the scheduler, without bypassing metering."""
    def claim(data):
        row = data["tasks"][selected["id"]]
        assert data["settings"]["enabled"] and row["status"] == "queued"
        row.update(status="running", in_flight={"kind": "model", "dispatched": False,
            "goal_revision": row["goal"]["revision"], "control_revision": row["control_revision"], "scope_revision": row["scope_revision"]})
        return {"_run_control_revision": row["control_revision"], "_run_lifetime_remaining": budget.time_remaining(row)}
    return {**selected, **runtime.store.transaction(claim), "_run_started": time.monotonic(), "_request_id": "req_" + uuid.uuid4().hex}


def execute(runtime, tools, row, name):
    return tools.extensions.execute(name, {}, operation_id="op_" + uuid.uuid4().hex,
                                    context=runtime._execution_context(row))


@pytest.mark.parametrize("fail", [False, True])
def test_memory_deny_is_durable_before_dispatch_even_on_failure(env, monkeypatch, fail):
    runtime, _, tools, _ = env
    install(env)
    row = task_row(runtime)
    observed = []
    def call(*args, **kwargs):
        kwargs["before_dispatch"]()
        stored = runtime.store.read()["tasks"][row["id"]]
        observed.append(data_policy.memory_denied(stored))
        if fail:
            raise AgentError("synthetic_failure")
        return {"content": [{"type": "text", "text": "synthetic private record"}]}
    monkeypatch.setattr(tools.extensions.manager, "call_tool", call)
    if fail:
        with pytest.raises(AgentError):
            execute(runtime, tools, row, compute_model_tool_name("restricted", "read"))
    else:
        execute(runtime, tools, row, compute_model_tool_name("restricted", "read"))
    assert observed == [True]
    assert data_policy.memory_denied(runtime.store.read()["tasks"][row["id"]])


def test_allowlist_filters_specs_and_preview_and_rechecks_at_dispatch(env, monkeypatch):
    runtime, _, tools, rows = env
    _, name = install(env, ALLOW)
    row = task_row(runtime)
    assert name not in {s["function"]["name"] for s in tools.extensions.specs()}
    assert name in {s["function"]["name"] for s in runtime._business_specs(row)}
    other = {**row, "model_id": "local:other", "model_destination": runtime._destination(rows[1])}
    assert name not in {s["function"]["name"] for s in runtime._business_specs(other)}
    with pytest.raises(AgentToolRejected, match="agent_extension_model_not_allowed"):
        tools.extensions.preview(name, {}, context=runtime._execution_context(other))
    sent = []
    def changed(*args, **kwargs):
        rows[0]["destination_revision"] = "changed-v2"
        kwargs["before_dispatch"]()
        sent.append(True)
    monkeypatch.setattr(tools.extensions.manager, "call_tool", changed)
    with pytest.raises(AgentToolRejected, match="agent_extension_model_not_allowed"):
        execute(runtime, tools, row, name)
    assert sent == []


def test_parallel_reads_merge_restrictions_and_large_reference_keeps_snapshot(env, monkeypatch):
    runtime, _, tools, _ = env
    _, first = install(env, DENY_MEMORY)
    _, second = install(env, ALLOW, identifier="model-rule")
    row = task_row(runtime)
    def call(*args, **kwargs):
        kwargs["before_dispatch"]()
        return {"content": [{"type": "text", "text": "synthetic"}]}
    monkeypatch.setattr(tools.extensions.manager, "call_tool", call)
    with ThreadPoolExecutor(max_workers=2) as pool:
        list(pool.map(lambda name: execute(runtime, tools, row, name), [first, second]))
    state = runtime.store.read()["tasks"][row["id"]]["data_policy_state"]
    assert len(state["sources"]) == 2 and state["memory_deny"]
    reference = results.save(runtime.store, row, {"name": first, "operation_id": "op"}, {"text": "large" * 2000})
    assert reference["data_policy_state"] == state


@pytest.mark.parametrize("nested,stage", [(False, None), (True, None), (True, "summary")])
def test_every_completion_reads_live_policy_including_stale_row(env, nested, stage):
    runtime, model, tools, _ = env
    server, _ = install(env, ALLOW)
    row = task_row(runtime)
    runtime._admit_extension_policy(row, data_policy.snapshot_source(server))
    assert "data_policy_state" not in row
    row = active_tick(runtime, row)
    other = {**row, "model_id": "local:other", "model_destination": runtime._current_destination("local:other")}
    with pytest.raises(AgentToolRejected, match="agent_extension_model_not_allowed"):
        runtime._complete(other, [{"role": "user", "content": "synthetic"}], [], lambda: False, lambda value: None, nested=nested, stage=stage)
    assert model.calls == []
    runtime._complete(row, [{"role": "user", "content": "synthetic"}], [], lambda: False, lambda value: None, nested=nested, stage=stage)
    assert len(model.calls) == 1
    assert model.calls[0][3]["data_policy_state"]["sources"][0]["policy"] == ALLOW


def test_real_model_switch_does_not_drop_restrictions(env):
    runtime, model, _, _ = env
    server, _ = install(env, ALLOW)
    row = task_row(runtime)
    runtime._admit_extension_policy(row, data_policy.snapshot_source(server))
    runtime.action(row["id"], {"action": "pause"})
    preview = runtime.preview_model_switch(row["id"], "local:other")
    runtime.switch_model(row["id"], {"model_id": "local:other", "expected_control_revision": preview["control_revision"],
        "expected_scope_revision": preview["scope_revision"], "expected_destination": preview["destination"],
        "confirm_data_destination": True, "transfer_resource_scope": False, "client_request_id": str(uuid.uuid4())})
    stored = runtime.store.read()["tasks"][row["id"]]
    assert stored["data_policy_state"]["sources"][0]["policy"] == ALLOW
    runtime.action(row["id"], {"action": "resume", "expected_control_revision": stored["control_revision"]})
    runtime.run_once()
    blocked = runtime.get(row["id"])
    assert blocked["status"] == "failed" and blocked["error"] == "agent_extension_model_not_allowed"
    assert model.calls == []


def test_real_code_mode_parallel_children_filter_catalog_and_persist_before_results(env, monkeypatch):
    runtime, _, tools, _ = env
    _, first = install(env, DENY_MEMORY)
    _, second = install(env, ALLOW, identifier="model-rule")
    row = task_row(runtime)
    runner = CodeModeRunner(tools)
    both_dispatched = threading.Barrier(2)
    observed = []
    def call(server_id, *args, **kwargs):
        kwargs["before_dispatch"]()
        state = runtime.store.read()["tasks"][row["id"]]["data_policy_state"]
        assert server_id in {item["server_id"] for item in state["sources"]}
        both_dispatched.wait(timeout=5)
        state = runtime.store.read()["tasks"][row["id"]]["data_policy_state"]
        observed.append((server_id, len(state["sources"]), state["memory_deny"]))
        return {"content": [{"type": "text", "text": "synthetic " + server_id}]}
    monkeypatch.setattr(tools.extensions.manager, "call_tool", call)
    try:
        default_catalog = runner._read_only_catalog(None)[0]
        assert first in default_catalog and second not in default_catalog
        allowed_catalog = runner._read_only_catalog(runtime._execution_context(row))[0]
        assert first in allowed_catalog and second in allowed_catalog
        result = runner.execute({"code": "const values = await Promise.all([tools[" + json.dumps(first) + "]({}), tools[" + json.dumps(second) + "]({})]); return {count: values.length};",
                                 "yield_time_ms": 8000}, operation_id="code-policy", cancelled=lambda: False,
                                context=runtime._execution_context(row))
        assert result["status"] == "completed", result
        assert result["return_value"] == {"count": 2}
        assert sorted((call["tool"], call["status"]) for call in result["tool_calls"]) == sorted([(first, "completed"), (second, "completed")])
        assert sorted(observed) == [("model-rule", 2, True), ("restricted", 2, True)]
    finally:
        runner.close()


def test_deleted_and_reimported_looser_source_cannot_widen_task(env):
    runtime, _, tools, _ = env
    server, _ = install(env, ALLOW)
    row = task_row(runtime)
    runtime._admit_extension_policy(row, data_policy.snapshot_source(server))
    tools.extensions.store.delete_mcp_server(server["id"], expected_revision=server["revision"])
    replacement, _ = install(env, DENY_MEMORY)
    assert replacement["instance_id"] != server["instance_id"]
    runtime._admit_extension_policy(row, data_policy.snapshot_source(replacement))
    state = runtime.store.read()["tasks"][row["id"]]["data_policy_state"]
    assert len(state["sources"]) == 2 and state["memory_deny"]
    other = {**row, "model_id": "local:other", "model_destination": runtime._current_destination("local:other")}
    assert not data_policy.allows_task(state, runtime._policy_context(other))


def test_owner_hint_route_and_availability_use_real_state_and_exact_revision(env, monkeypatch):
    runtime, model, tools, _ = env
    server, name = install(env, ALLOW, trust=False)
    app = FastAPI()
    def owner(request):
        if request.headers.get("origin") == "https://untrusted.example.test":
            raise HTTPException(401, "unauthorized")
    install_extension_routes(app, runtime=runtime, extensions=tools.extensions, local_control=owner)
    with TestClient(app) as client:
        path = "/api/local/agent/extensions/mcp/servers/restricted/apply-server-hints"
        payload = confirmation(server)
        assert tools.metadata(name)["effect"] == "write"
        assert client.post(path, json=payload, headers={"origin": "https://untrusted.example.test"}).status_code == 401
        assert tools.metadata(name)["effect"] == "write"
        assert client.post(path, json=payload).status_code == 200
        assert tools.metadata(name)["effect"] == "read"
        assert client.post(path, json=payload).status_code == 409
        available = client.get("/api/local/agent/extensions/model-availability", params={"model_id": "local:other"}).json()
        assert {"id": "restricted", "name": "restricted", "allowed": False} in available["servers"]
        assert client.get("/api/local/agent/extensions/model-availability", params={"model_id": "local:test"}).json()["servers"][0]["allowed"]
    current = tools.extensions.store.get_mcp_server_internal("restricted")
    tools.extensions.store.record_mcp_discovery("restricted", expected_revision=current["revision"], status="ready", discovered_tools=[
        {"name": "read", "inputSchema": {"type": "object"}, "annotations": {"readOnlyHint": False}}])
    assert tools.metadata(name)["effect"] == "write"
    dispatched = []
    monkeypatch.setattr(tools.extensions.manager, "call_tool", lambda *args, **kwargs: dispatched.append(True))
    model.responses = [
        _assistant(name="ryn_discover_tools", args={"load": [name]}, call_id="load"),
        _assistant(name=name, args={}, call_id="write-after-rediscovery"),
    ]
    row = task_row(runtime)
    for _ in range(4):
        runtime.run_once()
    waiting = runtime.get(row["id"])
    assert waiting["status"] == "waiting_approval", waiting
    assert waiting["pending_approval"]["tool"] == name
    assert waiting["pending_approval"]["preview"]["server_id"] == "restricted"
    assert dispatched == []


def switch_to_other(runtime, task_id):
    runtime.action(task_id, {"action": "pause"})
    preview = runtime.preview_model_switch(task_id, "local:other")
    return runtime.switch_model(task_id, {"model_id": "local:other", "expected_control_revision": preview["control_revision"],
        "expected_scope_revision": preview["scope_revision"], "expected_destination": preview["destination"],
        "confirm_data_destination": True, "transfer_resource_scope": False, "client_request_id": str(uuid.uuid4())})


def test_large_result_pages_follow_real_tool_workflow_and_switch_cannot_disclose(env, monkeypatch):
    runtime, model, tools, _ = env
    _, name = install(env, ALLOW)
    runtime.update_settings({"max_output_chars": 2000})
    marker = "SYNTHETIC_RESTRICTED_PAGE_MIDDLE"
    body = "p" * 3500 + marker + "q" * 3500
    def call(*args, **kwargs):
        kwargs["before_dispatch"]()
        return {"content": [{"type": "text", "text": body}]}
    monkeypatch.setattr(tools.extensions.manager, "call_tool", call)
    model.responses = [_assistant(name="ryn_discover_tools", args={"load": [name]}, call_id="load"),
                       _assistant(name=name, call_id="large")]
    row = task_row(runtime)
    advance(runtime, 4)
    current = runtime.get(row["id"])
    assert len(current["result_refs"]) == 1
    reference = current["result_refs"][0]
    state = runtime.store.read()["tasks"][row["id"]]["data_policy_state"]
    assert reference["data_policy_state"] == state
    assert marker not in json.dumps(current["messages"])
    assert marker not in results.path(runtime.store, row["id"], reference["id"]).read_text()
    original = runtime.get_result(row["id"], reference["id"], 0, 16384)["content"]
    offset = original.index(marker) - 16
    model.responses = [_assistant(name="ryn_read_result", args={"result_id": reference["id"], "offset": offset}, call_id="page"),
                       _assistant(content="Read the bounded synthetic page.")]
    advance(runtime, 3)
    assert runtime.get(row["id"])["status"] == "completed"
    assert marker in json.dumps(model.calls[-1][1])
    assert model.calls[-1][3]["data_policy_state"] == state
    calls_before_switch = len(model.calls)
    switched = switch_to_other(runtime, row["id"])
    assert runtime.store.read()["tasks"][row["id"]]["data_policy_state"] == state
    runtime.action(row["id"], {"action": "resume", "expected_control_revision": switched["control_revision"]})
    advance(runtime)
    assert runtime.get(row["id"])["error"] == "agent_extension_model_not_allowed"
    assert len(model.calls) == calls_before_switch


@pytest.mark.parametrize("allowed_destination", [True, False])
def test_automatic_compaction_retains_policy_and_rejects_disallowed_summary_destination(env, allowed_destination):
    runtime, model, _, _ = env
    server, _ = install(env, ALLOW)
    runtime.update_settings({"max_context_chars": 8000})
    row = task_row(runtime)
    runtime._admit_extension_policy(row, data_policy.snapshot_source(server))
    state = runtime.store.read()["tasks"][row["id"]]["data_policy_state"]
    if not allowed_destination:
        switch_to_other(runtime, row["id"])
    compaction_history(runtime, row["id"])
    # Keep this existing long-history fixture in the task's current projection.
    def current_epoch(data):
        stored = data["tasks"][row["id"]]
        for message in stored["messages"]:
            message["context_epoch"] = stored.get("context_epoch", 0)
    runtime.store.transaction(current_epoch)
    if not allowed_destination:
        stopped = runtime.get(row["id"])
        runtime.action(row["id"], {"action": "resume", "expected_control_revision": stopped["control_revision"]})
    model.responses = [_assistant(content="Inspected A; no changes. Keep B and C unchanged."),
                       _assistant(content="Inspection finished.")]
    advance(runtime)
    done = runtime.get(row["id"])
    assert any(item["type"] == "context_compaction_started" for item in done["events"]), done
    assert runtime.store.read()["tasks"][row["id"]]["data_policy_state"] == state
    if allowed_destination:
        assert done["status"] == "completed", done
        assert any(item["type"] == "context_compacted" for item in done["events"])
        assert len(model.calls) == 2 and model.calls[0][2] == []
        assert "CONTEXT CHECKPOINT COMPACTION" in model.calls[0][1][0]["content"]
        assert "untrusted" in model.calls[0][1][0]["content"]
        assert all(call[3]["data_policy_state"] == state for call in model.calls)
    else:
        assert done["error"] == "agent_extension_model_not_allowed", done
        assert model.calls == []


def test_restricted_tool_result_rotates_model_conversation_before_next_completion(env, monkeypatch):
    runtime, model, tools, _ = env
    _, name = install(env, ALLOW)
    def call(*args, **kwargs):
        kwargs["before_dispatch"]()
        return {"content": [{"type": "text", "text": "Synthetic restricted observation"}]}
    monkeypatch.setattr(tools.extensions.manager, "call_tool", call)
    model.responses = [_assistant(name="ryn_discover_tools", args={"load": [name]}, call_id="load"),
                       _assistant(name=name, call_id="read"), _assistant(content="Read complete.")]
    row = task_row(runtime)
    advance(runtime, 5)
    assert runtime.get(row["id"])["status"] == "completed"
    assert len(model.calls) == 3
    before = model.calls[0][3]["conversation"]
    assert model.calls[1][3]["conversation"] == before
    state = runtime.store.read()["tasks"][row["id"]]["data_policy_state"]
    after = model.calls[2][3]["conversation"]
    assert after == before + ":policy:" + str(state["revision"])
    assert after != before and model.calls[2][3]["data_policy_state"] == state
