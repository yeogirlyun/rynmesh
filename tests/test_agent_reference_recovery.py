"""Actual native process loss and SQLite resume; limitations stay failures."""
import copy
import json
import os
import shutil
from pathlib import Path
from types import SimpleNamespace

import pytest
from test_agent_integrations import node  # noqa: F401
from test_agent_resource_writes import writable  # noqa: F401

from rynmesh.agent_runtime.runtime import AgentRuntime
from rynmesh.agent_runtime.store import AgentStore
from rynmesh.services import peer_box

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def native(monkeypatch):
    upstream = Path(os.environ.get("RYN_REFERENCE_UPSTREAM", ROOT / ".codex-tmp/research/zcode-runtime-reference"))
    executable = os.environ.get("RYN_REFERENCE_NODE") or shutil.which("node")
    if not executable or not upstream.is_dir():
        pytest.skip("Pinned native dependencies absent")
    monkeypatch.syspath_prepend(str(ROOT / "docs/acceptance/ryn-agent-delivery/reference"))
    monkeypatch.syspath_prepend(str(ROOT / "docs/acceptance/ryn-agent-delivery/real-model"))
    monkeypatch.setattr(AgentRuntime, "run_once", lambda *_: pytest.fail("Candidate planner is forbidden"))
    return Path(executable), upstream


def interrupt_resume(runtime, task, tools, catalog, bridge, home, native, phase, policy, record_property):
    from documents import execute
    from recovery import ReferenceRecovery

    before = {"prompt": runtime.store.read()["tasks"][task["id"]]["messages"][0]["content"], "approvals": []}
    options = dict(runtime=runtime, tools=tools, catalog=catalog, selected=task["model_id"], task=task,
        bridge=bridge, home=home, node=native[0], upstream=native[1], allow_write=policy,
        real_model=False, native_business_operations=True)
    execute(**options, record=before, recovery=ReferenceRecovery(runtime, task["id"], phase))
    assert before["reference"]["interrupted"]
    original = runtime.store.read()["tasks"][task["id"]]
    operation_id = original["reference_session"]["interrupted_operation_id"]
    operation = copy.deepcopy(original["reference_operations"][operation_id])
    assert operation["state"] == ("waiting_approval" if phase == "approval" else "dispatched")
    assert "result" not in operation
    ledger = copy.deepcopy(original["budget"])
    # Reconstruct the host runtime, admission and meter from the same encrypted
    # task store. No candidate reconcile or saved executor closure is used.
    reopened = AgentRuntime(runtime.store, bridge, tools, catalog, consumer_node_id=runtime.consumer_node_id)
    after = {"prompt": before["prompt"], "approvals": []}
    prior_audit = home / "prior-native-dispatch.jsonl"
    prior_audit.write_text("retained original dispatch audit\n", encoding="utf-8")
    execute(**{**options, "runtime": reopened}, record=after,
            dispatch_provenance={"audit_path": str(prior_audit)},
            dispatch_observer=lambda *_: pytest.fail("Readback cannot register a fake dispatch"),
            recovery=ReferenceRecovery(reopened, task["id"], "resume"))
    assert prior_audit.read_text(encoding="utf-8") == "retained original dispatch audit\n"
    result = after["reference"]["result"]
    assert result["resumed"] and result["persisted_history_unchanged"]
    assert result["session_id"] == original["reference_session"]["session_id"]
    assert result["hydration"]["interruptedToolCount"] == 1
    assert result["native_pending_approvals"] == 0
    pending = next(p for p in result["persisted_tools"] if p["call_id"] == operation["tool_call_id"])
    assert pending["input"] == operation["arguments"] and pending["tool"] == operation["name"]
    assert pending["status"] in {"pending", "running"}
    assert after["reference"]["dispatch_counts"] == {"model": 0, "tool": 0, "approval": 0}
    assert after["reference_recovery"]["worker_pid"] != before["reference"]["worker_pid"]
    final = runtime.store.read()["tasks"][task["id"]]
    assert final["reference_operations"][operation_id] == operation
    assert final["budget"] == ledger and final["model_id"] == original["model_id"]
    assert final["control_revision"] == original["control_revision"]
    record_property("native_recovery", json.dumps({"phase": phase, "before": before, "after": after,
        "case_pass": False, "capability_limit": result["limitation"], "operation_id": operation_id,
        "budget_preserved": True, "planner_replaced": False}))
    return original, final


def test_r02_native_approval_wait_restart_preserves_identity_but_cannot_reattach(node, tmp_path, native, record_property):  # noqa: F811
    from sharing_cases import SharingWorld
    from sharing_recovery_worker import RecoveryFixturePlanner

    from rynmesh.agent_runtime.bridge import build_agent_integrations

    app, store = node
    with SharingWorld(tmp_path / "world", "R02") as world:
        selected = world.configure(app, store)
        _, tools, catalog = build_agent_integrations(app, store, store.home)
        world.attach(tools)
        bridge = RecoveryFixturePlanner()
        runtime = AgentRuntime(AgentStore(store.home / "agent", peer_box.load_or_create_messaging_key(store.home / "messaging.x25519")),
                               bridge, tools, catalog, consumer_node_id=store.peer_id)
        app.state.agent_runtime = runtime
        runtime.update_settings({"enabled": True, "model_id": selected, "max_steps": 24,
                                 "max_seconds": 600, "max_context_chars": 100000})
        task = runtime.create({"message": world.case["prompt"]})
        before = world.sources.path.read_bytes()
        calls = []
        def approve(request):
            calls.append(request)
            return world.permitted({**request, "tool": request["name"]})
        original, final = interrupt_resume(runtime, task, tools, catalog, bridge, store.home, native,
                                            "approval", approve, record_property)
        assert not calls  # Original case's same-native-approval-reloaded trigger was not reached.
        assert world.sources.path.read_bytes() == before and not world.grants.list()
        assert final["in_flight"] is None and original["reference_session"] == final["reference_session"]


def test_r03_native_commit_before_receipt_restart_keeps_unknown_without_second_put(writable, native, record_property):  # noqa: F811
    tools, _, _, _, source, server, root, _, store = writable
    target = root / "allowed/reports/acceptance"
    target.mkdir(parents=True)
    case = next(c for c in json.loads((ROOT / "docs/acceptance/ryn-agent-delivery/cases-v1.json").read_text(encoding="utf-8"))["cases"] if c["id"] == "R03")
    model = {"id": "fixture", "model_id": "fixture", "source": "local_api", "available": True,
             "agent_supported": True, "peer_id": "local", "source_id": "fixture", "destination_revision": "rev1",
             "context_window": 100000, "max_output_tokens": 4096}
    catalog = SimpleNamespace(list_models=lambda: [model], resolve=lambda _: model)

    class Planner:
        supports_destination_binding = True

        def complete(self, model_id, messages, specs, cancelled, **options):
            assert specs
            receipts = [json.loads(m["content"]) for m in messages if m["role"] == "tool"]
            if not receipts:
                name, args = "ryn_list_resource_roots", {}
            else:
                name = "ryn_create_text_file"
                args = {"directory_ref": receipts[0]["roots"][0]["resource_ref"],
                        "name": "recovery.md", "content": "RECOVERY_ONCE_731"}
            return {"message": {"role": "assistant", "content": "", "tool_calls": [{"id": "call-" + name,
                "type": "function", "function": {"name": name, "arguments": json.dumps(args)}}]},
                "usage": {"input_tokens": 10, "output_tokens": 10}}

    bridge = Planner()
    key = peer_box.load_or_create_messaging_key(store.home / "messaging.x25519")
    runtime = AgentRuntime(AgentStore(store.home / "agent", key), bridge, tools, catalog, consumer_node_id="local")
    runtime.update_settings({"enabled": True, "model_id": "fixture", "max_context_chars": 100000,
                             "max_steps": 24, "max_seconds": 600})
    task = runtime.create({"message": case["prompt"], "scope": {"resources": [{"connection_id": source["id"],
        "root_path": "allowed/reports/acceptance", "actions": ["list", "read_ai", "create_file", "create_directory"]}],
        "allow_model_data": True}, "expected_destination_revision": "rev1"})
    original, final = interrupt_resume(runtime, task, tools, catalog, bridge, store.home, native, "receipt",
        lambda request: request["arguments"].get("name") == "recovery.md" and request["arguments"].get("content") == "RECOVERY_ONCE_731",
        record_property)
    assert (target / "recovery.md").read_text() == "RECOVERY_ONCE_731"
    assert len(list(target.iterdir())) == 1
    assert len([e for e in server.audit if e["method"] == "PUT"]) == 1
    assert final["in_flight"] == original["in_flight"] and final["in_flight"]["dispatched"]
