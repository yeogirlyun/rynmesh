"""An unconfirmed write never locks the conversation and is never repeated."""
from __future__ import annotations

import copy
import json
from datetime import datetime, timedelta, timezone

import pytest
from test_agent_runtime import Catalog, advance, approve, assistant, make_runtime
from test_agent_runtime_model_switch import body as model_switch_body

from rynmesh.agent_runtime.runtime import AgentRuntime
from rynmesh.agent_runtime.store import AgentError, AgentStore


def send(runtime, task_id, message):
    row = runtime.get(task_id)
    return runtime.add_message(task_id, {
        "message": message, "intent": "information",
        "expected_control_revision": row["control_revision"],
        "expected_goal_revision": row["goal"]["revision"],
    })


def unknown_task(tmp_path, *after):
    """A share_service write that was dispatched, then lost its response."""
    runtime, model, tools, key = make_runtime(
        tmp_path, assistant(name="share_service", args={"service_id": "svc"}), *after)
    row = runtime.create({"message": "Share the service."})
    advance(runtime, 2)
    approve(runtime, row["id"])
    dispatched = []

    def uncertain(name, arguments, *, operation_id, cancelled):
        dispatched.append(operation_id)
        tools.shared = True
        raise RuntimeError("connection lost after mutation")

    tools.execute = uncertain
    advance(runtime)
    current = runtime.get(row["id"])
    assert current["status"] == "interrupted" and current["error"] == "agent_write_outcome_unknown"
    return runtime, model, tools, key, row["id"], dispatched


def set_reconciliation(runtime, task_id, record):
    runtime.store.transaction(lambda data: data["tasks"][task_id].__setitem__("reconciliation", record))


def stamp(seconds_ago):
    return (datetime.now(timezone.utc) - timedelta(seconds=seconds_ago)).isoformat()


def test_unknown_outcome_allows_continue_switch_and_delete_but_never_resume(tmp_path):
    runtime, _, _, _, task_id, _ = unknown_task(tmp_path)
    row = runtime.get(task_id)
    assert row["run_state"] == "paused"  # Nothing runs; the sidebar must not spin.
    actions = row["allowed_actions"]
    for action in ("information", "steer", "model", "archive", "delete"):
        assert actions[action]["enabled"], action
    assert not actions["resume"]["enabled"]
    assert not actions["edit_message"]["enabled"]


def test_sending_acknowledges_once_and_never_repeats_the_write(tmp_path):
    runtime, model, _, _, task_id, dispatched = unknown_task(
        tmp_path, assistant("I checked first: the service is already shared."))
    assert len(dispatched) == 1
    send(runtime, task_id, "Did that work? Check before doing anything.")
    advance(runtime)
    row = runtime.get(task_id)
    assert row["status"] == "completed" and row["result"] == "I checked first: the service is already shared."
    assert len(dispatched) == 1  # The unconfirmed write was never dispatched again.
    stored = runtime.store.read()["tasks"][task_id]
    notes = [m for m in stored["messages"]
             if m["role"] == "tool" and "agent_write_outcome_unknown" in m["content"]]
    assert len(notes) == 1 and notes[0]["is_error"] is True
    sent = json.dumps(model.calls[-1][1], ensure_ascii=False)
    assert "never confirmed" in sent and "Did that work?" in sent
    assert stored["acknowledged_unknown"]["tool"] == "share_service"
    assert stored["in_flight"] is None and stored["error"] is None
    assert any(event["type"] == "unknown_outcome_acknowledged" for event in stored["events"])
    # A second message does not add a second note.
    send(runtime, task_id, "Thanks.")
    stored = runtime.store.read()["tasks"][task_id]
    assert len([m for m in stored["messages"]
                if m["role"] == "tool" and "agent_write_outcome_unknown" in m["content"]]) == 1


def test_fresh_reconciliation_holds_input_and_a_stale_one_releases_it(tmp_path):
    runtime, _, _, _, task_id, _ = unknown_task(tmp_path)
    set_reconciliation(runtime, task_id, {"state": "pending", "attempts": 0, "requested_at": stamp(5)})
    row = runtime.get(task_id)
    assert row["run_state"] == "reconciling"
    assert not row["allowed_actions"]["information"]["enabled"]
    assert row["allowed_actions"]["information"]["reason"] == "agent_reconciliation_running"
    with pytest.raises(AgentError, match="agent_operation_unresolved"):
        send(runtime, task_id, "Too early.")
    set_reconciliation(runtime, task_id, {"state": "pending", "attempts": 0, "requested_at": stamp(61)})
    assert runtime.get(task_id)["allowed_actions"]["information"]["enabled"]
    set_reconciliation(runtime, task_id, {"state": "running", "attempts": 1, "started_at": stamp(30)})
    assert not runtime.get(task_id)["allowed_actions"]["information"]["enabled"]
    set_reconciliation(runtime, task_id, {"state": "running", "attempts": 1, "started_at": stamp(121)})
    row = runtime.get(task_id)
    assert row["allowed_actions"]["information"]["enabled"] and row["run_state"] == "paused"


def test_records_written_by_older_versions_unlock_after_upgrade_and_restart(tmp_path):
    runtime, model, tools, key, task_id, dispatched = unknown_task(tmp_path)
    # Older versions stored pending/unknown reconciliations without timestamps.
    set_reconciliation(runtime, task_id, {"state": "pending", "attempts": 0})
    assert runtime.get(task_id)["allowed_actions"]["information"]["enabled"]
    set_reconciliation(runtime, task_id, {"state": "unknown", "attempts": 3, "reason": "agent_reconciliation_limit"})
    restarted = AgentRuntime(AgentStore(tmp_path, key), model, tools, Catalog())
    restarted.reconcile()
    row = restarted.get(task_id)
    assert row["error"] == "agent_write_outcome_unknown" and row["run_state"] == "paused"
    assert row["allowed_actions"]["information"]["enabled"] and row["allowed_actions"]["delete"]["enabled"]
    assert len(dispatched) == 1


def test_owner_can_delete_or_archive_an_unconfirmed_task(tmp_path):
    runtime, _, _, _, task_id, _ = unknown_task(tmp_path)
    row = runtime.get(task_id)
    archived = runtime.lifecycle(task_id, {"expected_control_revision": row["control_revision"]}, "archive")
    assert archived["archived"] is True
    restored = runtime.lifecycle(task_id, {"expected_control_revision": archived["control_revision"]}, "restore")
    assert runtime.lifecycle(task_id, {"expected_control_revision": restored["control_revision"]}, "delete")["deleted"] is True


def test_switching_model_acknowledges_the_unconfirmed_write(tmp_path):
    runtime, model, _, _, task_id, dispatched = unknown_task(tmp_path)
    rows = [{"id": identifier, "name": "Same model name", "source": "local_api", "source_name": identifier,
             "source_id": identifier, "peer_id": "self", "destination_revision": identifier + "-v1",
             "available": True, "agent_supported": True} for identifier in ("local:test", "local:other")]
    runtime.catalog.list_models = lambda: copy.deepcopy(rows)
    model.supports_destination_binding = True
    switched = runtime.switch_model(task_id, model_switch_body(runtime, task_id))
    assert switched["model_id"] == "local:other"
    stored = runtime.store.read()["tasks"][task_id]
    assert stored["in_flight"] is None and stored["acknowledged_unknown"]["tool"] == "share_service"
    assert len(dispatched) == 1
