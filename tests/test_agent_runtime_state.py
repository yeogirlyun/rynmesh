"""T02 authoritative task state, migration and internal proposal boundary."""
from __future__ import annotations

import copy
import json

import pytest
from test_agent_runtime import advance, assistant, make_runtime

from rynmesh.agent_runtime.runtime import AgentRuntime
from rynmesh.agent_runtime.state import PUBLIC_STATE_FIELDS
from rynmesh.agent_runtime.store import CHANNEL, VERSION, AgentStore, encoded
from rynmesh.atomic_io import atomic_write_json
from rynmesh.services import peer_box


def make_legacy(runtime, task_id, mutate):
    data = runtime.store.read()
    data.pop("schema_version", None)
    row = data["tasks"][task_id]
    for field in (*PUBLIC_STATE_FIELDS, "scope", "criteria_proposals", "prompt_version"):
        row.pop(field, None)
    for message in row["messages"]:
        message.pop("message_id", None)
    mutate(row)
    nonce, ciphertext = peer_box.seal(runtime.store.key, runtime.store.pub, encoded(data), info=CHANNEL)
    atomic_write_json(runtime.store.path, {"version": VERSION, "nonce": nonce, "ciphertext": ciphertext})


def test_simple_question_has_no_required_planning_call_and_no_verified_outcome(tmp_path):
    runtime, model, _, _ = make_runtime(tmp_path, assistant("An answer, not a business receipt."))
    row = runtime.create({"message": "What can you inspect?"})
    advance(runtime)
    done = runtime.get(row["id"])
    assert done["status"] == "completed" and done["run_state"] == "ended"
    assert done["schema_version"] == 2 and done["outcome"]["status"] == "unverified"
    assert done["plan"]["steps"] == [] and len(model.calls) == 1
    assert done["criteria"][0]["required"] and done["criteria"][0]["source"] == "user"
    assert "prompt_version" in next(m["content"] for m in model.calls[0][1]
                                    if m["role"] == "developer" and "Server control state:" in m["content"])
    assert all("message_id" not in message for message in model.calls[0][1])


def test_plan_is_proposal_and_cannot_mark_work_done(tmp_path):
    args = {"goal_revision": 1, "steps": [{"id": "inspect", "text": "Inspect service", "criterion_ids": ["goal-coverage"], "depends_on": []}],
            "criteria_proposals": [{"description": "Read back exact service state", "kind": "mechanical", "evidence_hint": "business receipt"}]}
    runtime, _, tools, _ = make_runtime(tmp_path, assistant(name="ryn_update_plan", args=args), assistant("Plan proposed."))
    row = runtime.create({"message": "Inspect service"})
    advance(runtime, 3)
    done = runtime.get(row["id"])
    assert done["plan"]["steps"][0]["status"] == "pending"
    assert len(done["criteria"]) == 1 and done["criteria"][0]["status"] == "pending"
    proposal = runtime.store.read()["tasks"][row["id"]]["criteria_proposals"][0]
    assert proposal["proposal_origin"] == "model" and proposal["accepted"] is False
    assert done["outcome"]["status"] == "unverified" and not tools.calls


@pytest.mark.parametrize("forged", [{"source": "user"}, {"source": "system"}, {"required": False}, {"status": "passed"}, {"verified": True}])
def test_model_cannot_forge_authoritative_criterion_fields(tmp_path, forged):
    args = {"goal_revision": 1, "steps": [], "criteria_proposals": [{"description": "Trust me", "kind": "semantic", **forged}]}
    runtime, model, _, _ = make_runtime(tmp_path, assistant(name="ryn_update_plan", args=args), assistant("Rejected."))
    row = runtime.create({"message": "Never share without approval"})
    initial = copy.deepcopy(row["criteria"])
    advance(runtime, 3)
    done = runtime.get(row["id"])
    assert done["criteria"] == initial and done["plan"]["steps"] == []
    assert "agent_invalid_control_proposal" in model.calls[-1][1][-1]["content"]
    assert done["outcome"]["status"] == "unverified"


def test_needed_input_is_durable_and_closes_unexecuted_calls(tmp_path):
    response = assistant(name="ryn_request_input", args={"goal_revision": 1, "prompt": "Which service?", "kind": "choice",
                         "fields": [{"id": "service", "label": "Service", "type": "choice", "required": True,
                                     "options": [{"value": "A", "label": "A"}, {"value": "B", "label": "B"}]}]})
    response["message"]["tool_calls"].extend(assistant(name="share_service", args={"service_id": "A"})["message"]["tool_calls"])
    runtime, model, tools, key = make_runtime(tmp_path, response)
    row = runtime.create({"message": "Inspect the selected service"})
    advance(runtime, 3)
    waiting = runtime.get(row["id"])
    assert waiting["status"] == waiting["run_state"] == "waiting_input"
    assert waiting["pending_input"]["goal_revision"] == waiting["goal"]["revision"]
    assert waiting["pending_input"]["control_revision"] == waiting["control_revision"]
    assert len([m for m in waiting["messages"] if m["role"] == "tool"]) == 2
    assert json.loads(waiting["messages"][-1]["content"])["not_executed"] is True
    restarted = AgentRuntime(AgentStore(tmp_path, key), model, tools, runtime.catalog)
    restarted.reconcile()
    assert restarted.get(row["id"])["pending_input"] == waiting["pending_input"]
    assert not tools.calls


@pytest.mark.parametrize("kind", ["completed", "approval", "unknown_write"])
def test_encrypted_v1_migration_preserves_uncertainty_and_requires_new_approval(tmp_path, kind):
    runtime, model, tools, key = make_runtime(tmp_path)
    row = runtime.create({"message": "Original private goal"})

    def change(old):
        if kind == "completed":
            old.update(status="completed", result="Successfully verified everything")
        elif kind == "approval":
            old.update(status="waiting_approval", pending_approval={"id": "legacy-approval"}, approved=None)
        else:
            old.update(status="running", in_flight={"kind": "tool", "effect": "write", "dispatched": True, "operation_id": "legacy-op"})
    make_legacy(runtime, row["id"], change)
    restarted = AgentRuntime(AgentStore(tmp_path, key), model, tools, runtime.catalog)
    restarted.reconcile()
    migrated = restarted.get(row["id"])
    assert migrated["schema_version"] == 2
    assert migrated["goal"]["text"] == "Original private goal"
    assert migrated["outcome"]["status"] == ("unknown" if kind == "unknown_write" else "unverified")
    if kind == "approval":
        assert migrated["pending_approval"] is None and migrated["status"] == "queued"
    elif kind == "unknown_write":
        assert migrated["error"] == "agent_write_outcome_unknown"
        assert restarted.store.read()["tasks"][row["id"]]["in_flight"]["operation_id"] == "legacy-op"
    envelope = json.loads(runtime.store.path.read_text())
    payload = json.loads(peer_box.open_sealed(key, runtime.store.pub, envelope["nonce"], envelope["ciphertext"], info=CHANNEL))
    assert payload["schema_version"] == 2
    assert "Original private goal" not in runtime.store.path.read_text()


def test_denied_share_and_false_final_claim_never_becomes_verified(tmp_path):
    runtime, _, tools, _ = make_runtime(tmp_path, assistant(name="share_service", args={"service_id": "A"}),
                                      assistant(name="inspect_service", args={"service_id": "A"}), assistant("Sharing was verified successful."))
    row = runtime.create({"message": "Share A"})
    advance(runtime, 2)
    waiting = runtime.get(row["id"])
    runtime.action(row["id"], {"action": "deny", "expected_revision": waiting["revision"], "approval_id": waiting["pending_approval"]["id"]})
    advance(runtime, 3)
    done = runtime.get(row["id"])
    assert done["result"] == "Sharing was verified successful."  # Preserve actual model text for audit.
    assert done["outcome"]["status"] == "unverified"
    assert not tools.shared and all(call[0] == "inspect_service" for call in tools.calls)


def test_legacy_multiple_owner_constraints_keep_unique_ids_after_followup(tmp_path):
    runtime, _, _, _ = make_runtime(tmp_path)
    row = runtime.create({"message": "First"})
    def legacy(old):
        old.update(status="completed", messages=[{"role": "user", "content": text} for text in ("First", "Second", "Third")])
    make_legacy(runtime, row["id"], legacy)
    runtime.reconcile()
    current = runtime.get(row["id"])
    updated = runtime.add_message(row["id"], {"message": "Fourth", "expected_revision": current["revision"]})
    ids = [c["id"] for c in updated["goal"]["constraints"]]
    assert len(ids) == len(set(ids)) == 4
