"""Owner form secrets stay in the integration vault, never task state."""
import json
import threading
import uuid

import pytest
from test_agent_runtime import make_runtime

from rynmesh.agent_runtime import credential_control
from rynmesh.agent_runtime.store import AgentError


def form(runtime, task_id, **extra):
    row = runtime.get(task_id)
    return {"expected_control_revision": row["control_revision"], "expected_goal_revision": row["goal"]["revision"],
            "client_request_id": str(uuid.uuid4()), "kind": "openai_compatible", "base_url": "https://api.example.test/v1",
            "api_key": "FORM-PRIVATE-SECRET", **extra}


def vault(runtime):
    values = {}
    def create(task_id, body, **kwargs):
        # SourceStore callbacks must be able to read AgentStore while this
        # method is executing. Holding AgentStore across this call deadlocks.
        thread = threading.Thread(target=runtime.store.read)
        thread.start()
        thread.join(1)
        assert not thread.is_alive()
        fingerprint = json.dumps(body, sort_keys=True)
        prior = values.get(body["client_request_id"])
        if prior is not None and prior != fingerprint:
            raise AgentError("agent_request_conflict")
        values[body["client_request_id"]] = fingerprint
        return {"has_credential": True, "kind": body["kind"], "base_url": body["base_url"], "source_id": None,
                "credential_version": 1, "expires_at": "later", "api_key": body["api_key"]}
    runtime.tools.create_source_credential = create
    return values


def test_owner_form_is_bound_idempotent_and_never_persists_secret_in_task(tmp_path):
    runtime, _, _, _ = make_runtime(tmp_path)
    row = runtime.create({"message": "Configure the source"})
    values = vault(runtime)
    body = form(runtime, row["id"])
    response = credential_control.change(runtime, row["id"], body)
    assert response["credential"]["has_credential"] is True
    assert "api_key" not in response["credential"]
    assert "FORM-PRIVATE-SECRET" not in json.dumps(runtime.store.read())
    assert credential_control.change(runtime, row["id"], body)["credential"] == response["credential"]
    assert len(values) == 1
    with pytest.raises(AgentError, match="agent_request_conflict"):
        credential_control.change(runtime, row["id"], {**body, "api_key": "CHANGED-SECRET"})


def test_secure_answer_closes_only_current_credential_question_with_safe_metadata(tmp_path):
    runtime, _, _, _ = make_runtime(tmp_path)
    row = runtime.create({"message": "Configure source"})
    vault(runtime)
    def waiting(data):
        target = data["tasks"][row["id"]]
        target["status"] = "waiting_input"
        target["pending_input"] = {"id": "question1", "kind": "credential_ref", "prompt": "Configure the secure credential"}
    runtime.store.transaction(waiting)
    body = form(runtime, row["id"], pending_input_id="question1")
    response = credential_control.change(runtime, row["id"], body)
    assert response["task"]["status"] == "queued" and response["task"]["pending_input"] is None
    message = response["task"]["messages"][-1]
    assert message["ui"]["kind"] == "source_credential"
    assert message["ui"]["endpoint"] == "https://api.example.test/v1" and message["ui"]["has_credential"] is True
    assert "api.example.test" in message["content"] and "FORM-PRIVATE-SECRET" not in message["content"]


def test_goal_change_during_secure_save_returns_conflict_without_resuming_new_goal(tmp_path):
    runtime, _, _, _ = make_runtime(tmp_path)
    row = runtime.create({"message": "Configure source"})
    vault(runtime)
    original = runtime.tools.create_source_credential
    def changed(*args, **kwargs):
        metadata = original(*args, **kwargs)
        runtime.store.transaction(lambda data: data["tasks"][row["id"]].update(control_revision=999))
        return metadata
    runtime.tools.create_source_credential = changed
    with pytest.raises(AgentError, match="agent_credential_changed_control_conflict"):
        credential_control.change(runtime, row["id"], form(runtime, row["id"]))
    assert not runtime.store.read()["tasks"][row["id"]].get("credential_requests")
