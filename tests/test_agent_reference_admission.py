"""Exact native admission to production default-model authority, offline only."""
import os
import shutil
from pathlib import Path

import pytest
from test_agent_default_model import setup
from test_agent_integrations import node  # noqa: F401

from rynmesh.agent_runtime.default_model import NAME
from rynmesh.agent_runtime.store import AgentToolRejected

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def ports(monkeypatch):
    folder = ROOT / "docs/acceptance/ryn-agent-delivery/reference"
    monkeypatch.syspath_prepend(str(folder))
    from budget_host import ReferenceBudget
    from operation_host import ReferenceOperationAdmission
    from tool_host import ReferenceToolHost
    return ReferenceOperationAdmission, ReferenceToolHost, ReferenceBudget


def host(tmp_path, ports):
    runtime, model, tools, _, _ = setup(tmp_path)
    task = runtime.create({"message": "Set default for future tasks only"})
    context = runtime._execution_context(runtime.store.read()["tasks"][task["id"]])
    admission = ports[0](runtime, task["id"], context)
    gate = ports[1](tools, policy=lambda _: True, cancelled=lambda: False, context=context, admission=admission)
    meter = ports[2](runtime, task["id"], guard=gate.check_current_permissions, admission=admission)
    gate.cancelled = meter.cancelled
    params = {"name": NAME, "arguments": {"model_id": "local:other",
        "expected_settings_revision": runtime.configuration_snapshot()["settings_revision"]}, "tool_call_id": "native-call-1"}
    return runtime, task, gate, meter, params


def test_native_approved_flight_is_real_authority_and_settles_once(tmp_path, ports):
    runtime, task, gate, meter, params = host(tmp_path, ports)
    before = runtime.store.read()["tasks"][task["id"]]
    execute = gate.tools.execute
    def inspect(name, args, **options):
        row = runtime.store.read()["tasks"][task["id"]]
        assert row["status"] == "running" and row["in_flight"]["authorization"] == "approved"
        assert row["in_flight"]["tool_call_id"] == params["tool_call_id"]
        assert row["in_flight"]["operation_id"] == options["operation_id"]
        meter.guard()
        return execute(name, args, **options)
    gate.tools.execute = inspect
    gate.approval(params)
    result = meter.tool(lambda: gate.execute(params))
    after = runtime.store.read()["tasks"][task["id"]]
    assert result["ok"] and runtime.settings()["model_id"] == "local:other"
    assert after["status"] == "queued" and after["in_flight"] is None
    assert after["control_revision"] == before["control_revision"]
    assert after["model_id"] == before["model_id"] == "local:test"
    assert runtime.create({"message": "Future task"})["model_id"] == "local:other"
    saved = after["reference_operations"][result["operation_id"]]
    assert saved["result"] == result and saved["state"] == "returned"
    assert saved["tool_call_id"] == params["tool_call_id"] and saved["approved"] is True
    assert len(after["configuration_operations"]) == 1
    with pytest.raises(ValueError, match="duplicate_dispatch"):
        gate.execute(params)
    assert meter.snapshot()["lifetime"]["tool_calls"] == 1


@pytest.mark.parametrize("change", ["id", "arguments", "preview", "context", "revision", "task_permission", "global_permission", "foreign_flight"])
def test_native_stale_or_unbound_calls_dispatch_zero_writes(tmp_path, ports, change):
    runtime, task, gate, meter, params = host(tmp_path, ports)
    original = runtime.settings()
    gate.approval(params)
    if change == "id":
        params["tool_call_id"] = "different-call"
    elif change == "arguments":
        params["arguments"]["model_id"] = "local:test"
    elif change == "preview":
        gate.approvals[params["tool_call_id"]]["preview"]["before"]["model_id"] = "wrong"
    elif change == "context":
        gate.context["owner_id"] = "different-owner"
    else:
        def mutate(data):
            row = data["tasks"][task["id"]]
            if change == "revision":
                row["control_revision"] += 1
            elif change == "task_permission":
                row["permission_mode"] = "read_only"
            elif change == "global_permission":
                data["settings"]["permission_mode"] = "read_only"
            else:
                row.update(status="running", in_flight={"operation_id": "foreign"})
        runtime.store.transaction(mutate)
    calls = []
    gate.tools.execute = lambda *a, **k: calls.append(a)
    with pytest.raises(AgentToolRejected):
        meter.tool(lambda: gate.execute(params))
    row = runtime.store.read()["tasks"][task["id"]]
    assert not calls and not row.get("configuration_operations")
    assert runtime.settings()["model_id"] == original["model_id"]


def test_unknown_native_write_remains_unresolved_and_cannot_resume(tmp_path, ports):
    runtime, task, gate, meter, params = host(tmp_path, ports)
    execute = gate.tools.execute
    def lost_receipt(*args, **options):
        execute(*args, **options)
        raise OSError("lost acknowledgement")
    gate.tools.execute = lost_receipt
    gate.approval(params)
    with pytest.raises(AgentToolRejected, match="cancelled"):
        meter.tool(lambda: gate.execute(params))
    row = runtime.store.read()["tasks"][task["id"]]
    assert row["status"] == "running" and row["in_flight"]["dispatched"] is True
    assert list(row["reference_operations"].values())[0]["state"] == "unknown"
    assert len(row["configuration_operations"]) == 1
    with pytest.raises(AgentToolRejected):
        gate.execute(params)
    assert meter.snapshot()["lifetime"]["unsettled_operations"] == 0


def test_owner_stop_during_native_write_is_not_resurrected(tmp_path, ports):
    runtime, task, gate, meter, params = host(tmp_path, ports)
    execute = gate.tools.execute
    def stop_after_commit(*args, **options):
        result = execute(*args, **options)
        runtime.action(task["id"], {"action": "cancel", "expected_control_revision": task["control_revision"]})
        return result
    gate.tools.execute = stop_after_commit
    gate.approval(params)
    with pytest.raises(AgentToolRejected):
        meter.tool(lambda: gate.execute(params))
    row = runtime.store.read()["tasks"][task["id"]]
    assert row["status"] == "cancelled" and row["control_revision"] > task["control_revision"]
    assert list(row["reference_operations"].values())[0]["state"] == "returned"
    assert len(row["configuration_operations"]) == 1


def test_c03_actual_pinned_native_worker(node, tmp_path, monkeypatch, ports):  # noqa: F811
    upstream = Path(os.environ.get("RYN_REFERENCE_UPSTREAM", ROOT / ".codex-tmp/research/zcode-runtime-reference"))
    executable = os.environ.get("RYN_REFERENCE_NODE") or shutil.which("node")
    if not executable or not upstream.is_dir():
        pytest.skip("Pinned native dependencies absent; set RYN_REFERENCE_NODE and RYN_REFERENCE_UPSTREAM")
    folder = ROOT / "docs/acceptance/ryn-agent-delivery/real-model"
    monkeypatch.syspath_prepend(str(folder))
    from documents import execute
    from sharing_cases import SharingFixturePlanner, SharingWorld

    from rynmesh.agent_runtime.bridge import build_agent_integrations
    from rynmesh.agent_runtime.runtime import AgentRuntime
    from rynmesh.agent_runtime.store import AgentStore
    from rynmesh.services import peer_box
    app, store = node
    with SharingWorld(tmp_path / "world", "C03") as world:
        selected = world.configure(app, store)
        _, tools, catalog = build_agent_integrations(app, store, store.home)
        bridge = SharingFixturePlanner("reference")
        model_ids = []
        complete = bridge.complete
        def trace(model_id, *args, **kwargs):
            model_ids.append(model_id)
            return complete(model_id, *args, **kwargs)
        bridge.complete = trace
        runtime = AgentRuntime(AgentStore(store.home / "agent", peer_box.load_or_create_messaging_key(store.home / "messaging.x25519")),
                               bridge, tools, catalog, consumer_node_id=store.peer_id)
        app.state.agent_runtime = runtime
        runtime.update_settings({"enabled": True, "model_id": world.bindings["model-c"], "max_steps": 24,
                                 "max_seconds": 600, "max_context_chars": 100000})
        monkeypatch.setattr(runtime, "run_once", lambda: pytest.fail("Reference must never invoke candidate planner"))
        world.attach(tools)
        before = runtime.settings()
        task = runtime.create({"message": world.case["prompt"], "model_id": selected})
        record = {"prompt": world.case["prompt"], "approvals": []}
        execute(runtime=runtime, tools=tools, catalog=catalog, selected=selected, task=task, bridge=bridge,
                record=record, home=store.home, node=Path(executable), upstream=upstream,
                allow_write=lambda request: world.permitted({**request, "tool": request["name"]}),
                real_model=False, native_business_operations=True)
        result = world.collect_postconditions(runtime, task["id"], before)
        assert record["reference"]["upstream_commit"] == "29628c9acdb81b703bbd4080c207a0e7ce5e276e"
        assert record["reference"]["result"]["persistent_session"] is True
        assert (store.home / "reference-session.sqlite").stat().st_size > 0
        counts = record["reference"]["dispatch_counts"]
        # One schema-discovery exchange is now part of the native protocol.
        assert counts == {"model": 12, "tool": 10, "approval": 4}
        assert set(model_ids) == {selected} and len(model_ids) == counts["model"]
        assert all(item["approved"] for item in record["approvals"])
        assert result["settings_after"]["model_id"] == selected != before["model_id"]
        assert result["settings_except_default_unchanged"] and result["sibling_and_source_configuration_unchanged"]
        assert result["receiver_visible_models"] == ["model-b"] and not result["ungranted_receiver_visible_models"]
        assert [p["result"] for p in result["receiver_probes"]] == ["allowed", "denied", "denied"]
        assert [p["upstream_requests"] for p in result["receiver_probes"]] == [1, 0, 0]
        raw = runtime.store.read()["tasks"][task["id"]]
        writes = raw["reference_operations"]
        assert len(writes) == 4 and all(op["state"] == "returned" for op in writes.values())
        events = [e for e in record["tool_events"] if e["type"] == "tool_result" and e["operation_id"] in writes]
        assert len(events) == 4
        assert all(writes[e["operation_id"]]["result"] == e["result"] and
                   writes[e["operation_id"]]["tool_call_id"] == e["tool_call_id"] for e in events)
        assert raw["status"] == "queued" and raw["model_id"] == selected and raw["in_flight"] is None
        assert runtime.create({"message": "Future task"})["model_id"] == selected
        lifetime = record["reference_budget"]["lifetime"]
        assert lifetime["model_calls"] == counts["model"] and lifetime["tool_calls"] == counts["tool"]
        assert lifetime["unsettled_operations"] == 0 and lifetime["cost"] is None
