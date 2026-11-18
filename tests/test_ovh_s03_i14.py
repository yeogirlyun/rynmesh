"""S03-I14: owner-requested compaction is one summary, never a main turn."""
import copy
import uuid

import pytest
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey
from fastapi import FastAPI
from fastapi.testclient import TestClient
from test_agent_runtime import Catalog, Model, Tools, assistant, make_runtime
from test_agent_context_compaction_policy import exchange, is_compaction_call

from rynmesh.agent_runtime.control import allowed_actions
from rynmesh.agent_runtime.routes import install_agent
from rynmesh.agent_runtime.store import AgentError
from rynmesh.background_workers import BackgroundWorkerRegistry


def seed(runtime, status="paused"):
    tid = runtime.create({"message": "Inspect services; never change them."})["id"]
    def fill(data):
        row = data["tasks"][tid]
        row.update(status=status, result="Original answer", in_flight=None)
        for i in range(4):
            row["messages"].extend(exchange(str(i), "observed evidence " * 250))
        if status == "waiting_input":
            row["pending_input"] = {"id": "question", "kind": "information", "prompt": "Which service?", "fields": [],
                "control_revision": row["control_revision"], "goal_revision": row["goal"]["revision"], "scope_revision": row["scope_revision"]}
    runtime.store.transaction(fill)
    return runtime.get(tid)


def compact(runtime, row, request_id=None):
    return runtime.action(row["id"], {"action": "compact", "expected_control_revision": row["control_revision"],
                                     "client_request_id": request_id or str(uuid.uuid4())})


@pytest.mark.parametrize("status", ["paused", "completed", "waiting_input"])
def test_t1_manual_compaction_returns_to_origin_without_main_request(tmp_path, status):
    runtime, model, _, _ = make_runtime(tmp_path, assistant("Findings: observed healthy. Next: report."))
    row = seed(runtime, status)
    original = copy.deepcopy(row["messages"])
    assert row["allowed_actions"]["compact"]["enabled"] is True
    queued = compact(runtime, row)
    assert queued["status"] == "queued"
    assert runtime.store.read()["tasks"][row["id"]]["manual_compaction"]["return_status"] == status
    runtime.run_once()
    done = runtime.get(row["id"])
    assert done["status"] == status, done.get("error")
    assert done["messages"] == original
    assert done["result"] == "Original answer"
    assert runtime.store.read()["tasks"][row["id"]]["in_flight"] is None
    assert "manual_compaction" not in runtime.store.read()["tasks"][row["id"]]
    assert len(model.calls) == 1 and is_compaction_call(model.calls[0][1], model.calls[0][2])
    start = next(e for e in done["events"] if e["type"] == "context_compaction_started")
    finish = next(e for e in done["events"] if e["type"] == "context_compacted")
    assert start["source"] == "manual"
    assert finish["request_bytes_before"] > finish["after_bytes"] > 0
    assert done["context_usage"]["request_bytes"] == finish["after_bytes"]
    if status == "waiting_input":
        assert done["pending_input"]["id"] == "question"


@pytest.mark.parametrize("status", ["paused", "completed", "waiting_input"])
def test_t1_rejected_summary_restores_status_and_does_not_run_main(tmp_path, status):
    response = {**assistant("Truncated summary"), "finish_reason": "length"}
    runtime, model, _, _ = make_runtime(tmp_path, response)
    row = seed(runtime, status)
    compact(runtime, row)
    runtime.run_once()
    done = runtime.get(row["id"])
    assert done["status"] == status
    assert len(model.calls) == 1
    assert runtime.store.read()["tasks"][row["id"]]["in_flight"] is None
    assert any(e["type"] == "context_compaction_rejected" and e["error"] == "agent_compaction_incomplete" for e in done["events"])
    assert not runtime.store.read()["tasks"][row["id"]].get("context_checkpoint")


def test_t1_invalid_history_and_active_or_unknown_operations_are_not_admitted(tmp_path):
    runtime, _, _, _ = make_runtime(tmp_path)
    row = seed(runtime)
    raw = runtime.store.read()["tasks"][row["id"]]
    settings = runtime.store.read()["settings"]
    cases = [{"messages": raw["messages"][:1]}, {"status": "running"}, {"status": "queued"},
             {"status": "waiting_approval"}, {"status": "failed"}, {"status": "cancelled"}, {"status": "interrupted"},
             {"error": "agent_write_outcome_unknown"}, {"in_flight": {"kind": "model"}},
             {"in_flight": {"kind": "tool", "effect": "write", "dispatched": True}}, {"archived": True},
             {"context_checkpoint": {"through": len(raw["messages"]), "content": "Already summarized"}}]
    for changes in cases:
        assert allowed_actions({**raw, **changes}, settings)["compact"]["enabled"] is False
    empty = runtime.create({"message": "New task"})
    runtime.store.transaction(lambda d: d["tasks"][empty["id"]].update(status="paused"))
    with pytest.raises(AgentError, match="agent_invalid_transition"):
        compact(runtime, runtime.get(empty["id"]))


def test_t1_owner_cancel_during_summary_wins_over_return_status(tmp_path):
    runtime, model, _, _ = make_runtime(tmp_path)
    row = seed(runtime, "completed")
    def reply(*args, **kwargs):
        model.calls.append(args)
        runtime.action(row["id"], {"action": "cancel"})
        return assistant("Discard this cancelled summary")
    model.complete = reply
    compact(runtime, row)
    runtime.run_once()
    assert runtime.get(row["id"])["status"] == "cancelled"
    assert runtime.store.read()["tasks"][row["id"]]["in_flight"] is None
    assert not runtime.store.read()["tasks"][row["id"]].get("pending_control")
    assert len(model.calls) == 1


def test_t1_compact_alias_uses_the_same_versioned_idempotent_action(tmp_path):
    app = FastAPI()
    runtime = install_agent(app, store=object(), home=tmp_path, workers=BackgroundWorkerRegistry(),
        local_control=lambda _: None, messaging_key=X25519PrivateKey.generate(), integrations=(Model(assistant("Summary")), Tools(), Catalog()))
    runtime.update_settings({"enabled": True, "model_id": "local:test"})
    row = seed(runtime)
    body = {"expected_control_revision": row["control_revision"], "client_request_id": str(uuid.uuid4())}
    with TestClient(app) as client:
        queued = client.post(f'/api/local/agent/tasks/{row["id"]}/compact', json=body)
        assert queued.status_code == 200, queued.text
        repeated = client.post(f'/api/local/agent/tasks/{row["id"]}/actions', json={**body, "action": "compact"})
        assert repeated.status_code == 200 and repeated.json()["status"] == "queued"
        runtime.run_once()
        assert client.get(f'/api/local/agent/tasks/{row["id"]}').json()["status"] == "paused"
        assert len(runtime.model.calls) == 1


def test_public_context_usage_contains_only_known_numbers(tmp_path):
    runtime, _, _, _ = make_runtime(tmp_path)
    row = seed(runtime)
    runtime.store.transaction(lambda d: d["tasks"][row["id"]].update(context_usage={
        "request_bytes": 1000, "capacity_bytes": 2000, "bytes_per_token": 4.0, "step": 2, "context_epoch": 0,
        "secret": "PRIVATE HISTORY", "unexpected": 99}))
    assert runtime.get(row["id"])["context_usage"] == {
        "request_bytes": 1000, "capacity_bytes": 2000, "bytes_per_token": 4.0, "step": 2, "context_epoch": 0}


def test_manual_compaction_does_not_dispatch_an_existing_write_queue(tmp_path):
    runtime, model, tools, _ = make_runtime(tmp_path, assistant(name="share_service", args={"service_id": "svc1"}), assistant("Summary"))
    row = seed(runtime)
    runtime.store.transaction(lambda d: d["tasks"][row["id"]].update(status="queued"))
    runtime.run_once()
    runtime.store.transaction(lambda d: d["tasks"][row["id"]].update(status="paused", pending_control=None))
    row = runtime.get(row["id"])
    queue = copy.deepcopy(runtime.store.read()["tasks"][row["id"]]["queue"])
    assert queue[0]["name"] == "share_service"
    compact(runtime, row)
    runtime.run_once()
    assert runtime.get(row["id"])["status"] == "paused"
    assert runtime.store.read()["tasks"][row["id"]]["queue"] == queue
    assert not tools.shared and not tools.calls
    assert len(model.calls) == 2 and is_compaction_call(model.calls[-1][1], model.calls[-1][2])


def test_manual_compaction_budget_rejection_restores_original_status(tmp_path):
    runtime, model, _, _ = make_runtime(tmp_path)
    row = seed(runtime, "completed")
    def limit(data):
        task = data["tasks"][row["id"]]
        task["limits"]["max_steps"] = 1
        task["limits_source"]["max_steps"] = "owner"
        task["steps"] = 1
    runtime.store.transaction(limit)
    compact(runtime, runtime.get(row["id"]))
    runtime.run_once()
    done = runtime.get(row["id"])
    assert done["status"] == "completed"
    assert not runtime.store.read()["tasks"][row["id"]].get("manual_compaction")
    assert any(e["type"] == "context_compaction_rejected" and e["error"] == "agent_step_limit" for e in done["events"])
    assert not model.calls


def test_task_summary_and_detail_agree_about_compaction_availability(tmp_path):
    runtime, _, _, _ = make_runtime(tmp_path)
    row = seed(runtime, "completed")
    summary = next(t for t in runtime.list_tasks() if t["id"] == row["id"])
    assert summary["allowed_actions"]["compact"]["enabled"] == row["allowed_actions"]["compact"]["enabled"] is True
    assert "messages" not in summary
