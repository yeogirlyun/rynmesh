"""A stopped conversation stays usable: continue, switch model, archive or delete."""
from __future__ import annotations

import copy

from test_agent_runtime import advance, assistant, make_runtime
from test_agent_runtime_model_switch import body as model_switch_body


def action(runtime, task_id, kind):
    row = runtime.get(task_id)
    return runtime.action(task_id, {"action": kind, "expected_control_revision": row["control_revision"]})


def send(runtime, task_id, message):
    row = runtime.get(task_id)
    return runtime.add_message(task_id, {"message": message, "intent": "information",
                                         "expected_control_revision": row["control_revision"],
                                         "expected_goal_revision": row["goal"]["revision"]})


def test_cancelled_conversation_continues_with_a_new_turn(tmp_path):
    runtime, model, tools, _ = make_runtime(tmp_path, assistant("Answer after cancelling."))
    row = runtime.create({"message": "First question."})
    cancelled = action(runtime, row["id"], "cancel")
    assert cancelled["status"] == "cancelled"
    assert cancelled["allowed_actions"]["information"]["enabled"]
    assert cancelled["allowed_actions"]["model"]["enabled"]
    send(runtime, row["id"], "Second question, please answer this one.")
    advance(runtime)
    row = runtime.get(row["id"])
    assert row["status"] == "completed" and row["result"] == "Answer after cancelling."
    assert tools.calls == []
    assert "Second question" in str(model.calls[-1][1])


def test_cancelled_conversation_can_switch_model(tmp_path):
    runtime, model, _, _ = make_runtime(tmp_path)
    rows = [{"id": identifier, "name": "Same model name", "source": "local_api", "source_name": identifier,
             "source_id": identifier, "peer_id": "self", "destination_revision": identifier + "-v1",
             "available": True, "agent_supported": True} for identifier in ("local:test", "local:other")]
    runtime.catalog.list_models = lambda: copy.deepcopy(rows)
    model.supports_destination_binding = True
    row = runtime.create({"message": "Question."})
    action(runtime, row["id"], "cancel")
    switched = runtime.switch_model(row["id"], model_switch_body(runtime, row["id"]))
    assert switched["model_id"] == "local:other"


def test_owner_can_remove_any_task_that_is_not_working(tmp_path):
    runtime, _, _, _ = make_runtime(tmp_path, assistant("Done."))
    for status, error in (("interrupted", "agent_node_restarted"), ("failed", "agent_model_unavailable"),
                          ("cancelled", None), ("completed", None)):
        row = runtime.create({"message": f"Task {status}."})
        runtime.store.transaction(lambda data, tid=row["id"], st=status, err=error:
                                  data["tasks"][tid].update(status=st, error=err, in_flight=None, queue=[]))
        current = runtime.get(row["id"])
        assert current["allowed_actions"]["archive"]["enabled"], status
        assert current["allowed_actions"]["delete"]["enabled"], status
        archived = runtime.lifecycle(row["id"], {"expected_control_revision": current["control_revision"]}, "archive")
        assert archived["archived"] is True
    running = runtime.create({"message": "Still running."})
    runtime.store.transaction(lambda data: data["tasks"][running["id"]].update(status="running"))
    assert not runtime.get(running["id"])["allowed_actions"]["delete"]["enabled"]


def test_follow_up_extends_a_nearly_used_default_time_budget_instead_of_crashing(tmp_path, monkeypatch):
    from rynmesh.agent_runtime import budget

    runtime, _, _, _ = make_runtime(tmp_path, assistant("First."), assistant("Second."))
    row = runtime.create({"message": "First question."})
    advance(runtime)
    assert runtime.get(row["id"])["status"] == "completed"
    real = budget.time_remaining
    first = [True]

    def remaining(current):
        # Nearly used up on the first check, then the real value after the extension.
        if first[0]:
            first[0] = False
            return 0.0
        return real(current)

    monkeypatch.setattr(budget, "time_remaining", remaining)
    updated = send(runtime, row["id"], "Second question.")
    extended = [event for event in updated["events"] if event["type"] == "budget_auto_extended"]
    assert extended and "max_total_seconds" in extended[-1]["extended"]
