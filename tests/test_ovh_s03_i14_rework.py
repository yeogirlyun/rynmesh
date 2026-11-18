"""S03-I14: later owner messages supersede unstarted manual compaction."""
import uuid

import pytest

from test_agent_runtime import assistant, make_runtime
from test_agent_context_compaction_policy import is_compaction_call
from test_ovh_s03_i14 import compact, seed


@pytest.mark.parametrize("status", ["paused", "completed", "waiting_input"])
@pytest.mark.parametrize("intent", ["steer", "information"])
def test_owner_message_before_claim_runs_a_normal_turn(tmp_path, status, intent):
    runtime, model, _, _ = make_runtime(tmp_path, assistant("Handled the new owner message."))
    queued = compact(runtime, seed(runtime, status))
    runtime.add_message(queued["id"], {
        "message": "Please report the latest service status.", "intent": intent,
        "expected_control_revision": queued["control_revision"],
        "expected_goal_revision": queued["goal"]["revision"],
        "client_request_id": str(uuid.uuid4()),
    })
    runtime.run_once()
    done = runtime.get(queued["id"])
    assert done["result"] == "Handled the new owner message."
    assert len(model.calls) == 1
    assert not is_compaction_call(model.calls[0][1], model.calls[0][2])
    assert "manual_compaction" not in runtime.store.read()["tasks"][queued["id"]]


def test_paused_restart_discards_interrupted_manual_compaction(tmp_path):
    runtime, model, _, _ = make_runtime(tmp_path, assistant("Continued normally."))
    queued = compact(runtime, seed(runtime))
    runtime.store.transaction(lambda data: data["tasks"][queued["id"]].update(
        status="paused", pending_control={"kind": "pause"},
        in_flight={"kind": "model", "stage": "compaction", "dispatched": True}))
    runtime.reconcile()
    restored = runtime.get(queued["id"])
    assert restored["status"] == "paused"
    assert "manual_compaction" not in runtime.store.read()["tasks"][queued["id"]]
    runtime.action(queued["id"], {"action": "resume",
        "expected_control_revision": restored["control_revision"],
        "client_request_id": str(uuid.uuid4())})
    runtime.run_once()
    assert runtime.get(queued["id"])["result"] == "Continued normally."
    assert len(model.calls) == 1
    assert not is_compaction_call(model.calls[0][1], model.calls[0][2])
