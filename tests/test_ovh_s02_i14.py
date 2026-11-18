"""S02-I14: durable owner follow-up queues on real store/worker/HTTP paths."""
import base64
from pathlib import Path

import pytest
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from test_agent_integrations import node
from test_agent_runtime_lifecycle import change
from test_agent_runtime import Catalog, Model, Tools, advance, assistant, make_runtime
from rynmesh.agent_runtime.bridge import build_agent_integrations
from rynmesh.agent_runtime.routes import install_agent
from rynmesh.agent_runtime.runtime import AgentRuntime
from rynmesh.agent_runtime.store import AgentError, AgentStore
from rynmesh.background_workers import BackgroundWorkerRegistry


def test_T1_completion_dispatches_one_followup_in_same_transaction(tmp_path, monkeypatch):
    runtime, model, _, _ = make_runtime(tmp_path, assistant("first"), assistant("second"), assistant("third"))
    row = runtime.create({"message": "start"})
    row = runtime.queue_message(row["id"], {"message": "next", "intent": "information"})
    runtime.queue_message(row["id"], {"message": "last", "intent": "steer"})
    assert row["queued_messages"][0]["seq"] == 1
    writes, original_write = [], runtime.store._write
    def record_write(data):
        current = data["tasks"][row["id"]]
        writes.append((current["status"], [item["message"] for item in current.get("queued_messages", [])]))
        return original_write(data)
    monkeypatch.setattr(runtime.store, "_write", record_write)
    advance(runtime)
    row = runtime.get(row["id"])
    assert row["status"] == "queued"
    assert [m["content"] for m in row["messages"]][-2:] == ["first", "next"]
    assert [m["message"] for m in row["queued_messages"]] == ["last"]
    assert not any(status == "completed" and "next" in items for status, items in writes)
    advance(runtime, 2)
    row = runtime.get(row["id"])
    assert row["result"] == "third" and row["queued_messages"] == []
    assert len(model.calls) == 3


@pytest.mark.parametrize("stop", ["failed", "stop", "pause", "cancel"])
def test_T2_failure_and_owner_stop_preserve_paused_queue(tmp_path, stop):
    runtime, model, _, _ = make_runtime(tmp_path, assistant("resumed"))
    row = runtime.create({"message": "start"})
    runtime.queue_message(row["id"], {"message": "keep me"})
    if stop == "failed":
        runtime._fail(row["id"], "agent_test_failure")
    else:
        runtime.action(row["id"], {"action": stop})
    row = runtime.get(row["id"])
    assert row["queued_messages"][0]["message"] == "keep me"
    assert row["queued_messages_paused_reason"]
    runtime.run_once()
    assert model.calls == []
    item = row["queued_messages"][0]
    row = runtime.update_queued(row["id"], item["id"], {"send_now": True})
    assert row["status"] == "queued" and row["queued_messages"] == []
    advance(runtime)
    assert runtime.get(row["id"])["result"] == "resumed"


def test_T3_restart_keeps_queue_and_uploaded_bytes(tmp_path):
    runtime, model, tools, key = make_runtime(tmp_path, assistant("first"), assistant("file reviewed"))
    row = runtime.create({"message": "start"})
    runtime.queue_message(row["id"], {"message": "review this", "attachments": [
        {"name": "note.txt", "media_type": "text/plain", "data_base64": base64.b64encode(b"QUEUE_BYTES").decode()}]})
    runtime = AgentRuntime(AgentStore(tmp_path, key), model, tools, Catalog())
    runtime.reconcile()
    assert len(runtime.get(row["id"])["queued_messages"]) == 1
    advance(runtime, 2)
    assert "QUEUE_BYTES" in str(model.calls[-1][1])
    assert runtime.get(row["id"])["result"] == "file reviewed"


def test_T3_deleting_task_cleans_undispatched_queue_upload(node, tmp_path):
    app, server_store = node
    tools = build_agent_integrations(app, server_store, server_store.home)[1]
    runtime, _, _, _ = make_runtime(tmp_path)
    row = runtime.create({"message": "start"})
    row = runtime.queue_message(row["id"], {"message": "review this", "attachments": [
        {"name": "note.txt", "media_type": "text/plain", "data_base64": base64.b64encode(b"QUEUE_BYTES").decode()}]})
    upload = Path(row["queued_messages"][0]["attachments"][0]["path"])
    assert upload.is_file()
    runtime.action(row["id"], {"action": "cancel"})
    runtime = AgentRuntime(runtime.store, runtime.model, tools, runtime.catalog)
    result = change(runtime, row["id"], "delete")
    assert result["deleted"] and not result["cleanup_pending"]
    assert not upload.parent.exists()


def test_T4_count_and_row_size_limits_are_atomic(tmp_path):
    runtime, _, _, _ = make_runtime(tmp_path)
    row = runtime.create({"message": "start"})
    for index in range(100):
        runtime.queue_message(row["id"], {"message": str(index)})
    with pytest.raises(AgentError, match="agent_queue_limit"):
        runtime.queue_message(row["id"], {"message": "overflow"})
    assert len(runtime.get(row["id"])["queued_messages"]) == 100
    first = runtime.get(row["id"])["queued_messages"][0]
    with pytest.raises(AgentError, match="agent_task_limit"):
        runtime.update_queued(row["id"], first["id"], {"message": "中" * 800000})
    assert runtime.get(row["id"])["queued_messages"][0]["message"] == "0"


def test_T5_edit_reorder_delete_and_retry_does_not_duplicate(tmp_path):
    runtime, _, _, _ = make_runtime(tmp_path)
    row = runtime.create({"message": "start"})
    value = {"message": "a", "client_request_id": "b22fa4f7-699e-47e9-a413-b22d41a11ac2"}
    runtime.queue_message(row["id"], value)
    row = runtime.queue_message(row["id"], value)
    assert len(row["queued_messages"]) == 1
    row = runtime.queue_message(row["id"], {"message": "b"})
    a, b = row["queued_messages"]
    runtime.update_queued(row["id"], a["id"], {"message": "edited", "intent": "steer"})
    row = runtime.reorder_queued(row["id"], {"ids": [b["id"], a["id"]]})
    assert [item["message"] for item in row["queued_messages"]] == ["b", "edited"]
    with pytest.raises(AgentError, match="agent_invalid_request"):
        runtime.reorder_queued(row["id"], {"ids": [a["id"], a["id"]]})
    row = runtime.delete_queued(row["id"], b["id"])
    assert [item["id"] for item in row["queued_messages"]] == [a["id"]]


@pytest.mark.parametrize("flag", ["archived", "continued_to"])
def test_T6_archived_and_continued_reject_enqueue(tmp_path, flag):
    runtime, _, _, _ = make_runtime(tmp_path)
    row = runtime.create({"message": "start"})
    runtime.store.transaction(lambda data: data["tasks"][row["id"]].update({flag: True if flag == "archived" else "child"}))
    with pytest.raises(AgentError, match="agent_task_archived|agent_task_continued"):
        runtime.queue_message(row["id"], {"message": "rejected"})


def test_T6_continuation_moves_remaining_queue(tmp_path):
    runtime, _, _, _ = make_runtime(tmp_path)
    row = runtime.create({"message": "start"})
    runtime.queue_message(row["id"], {"message": "remaining"})
    runtime.action(row["id"], {"action": "stop"})
    child = runtime._continue_in_new_task(row["id"], {"message": "new turn"}, "new turn")
    assert child["queued_messages"][0]["message"] == "remaining"
    assert child["queued_messages_paused_reason"] is None
    assert runtime.get(row["id"])["queued_messages"] == []
    with pytest.raises(AgentError, match="agent_task_continued"):
        runtime.queue_message(row["id"], {"message": "cannot enqueue to the old task"})


def test_T6_real_cold_archive_keeps_queue_readable_and_rejects_enqueue(tmp_path):
    runtime, _, _, _ = make_runtime(tmp_path)
    row = runtime.create({"message": "start"})
    runtime.queue_message(row["id"], {"message": "retained in archive"})
    row = runtime.action(row["id"], {"action": "stop"})
    archived = runtime.lifecycle(row["id"], {"expected_control_revision": row["control_revision"]}, "archive")
    assert archived["archived"]
    assert row["id"] not in runtime.store.read()["tasks"]
    assert runtime.get_queued(row["id"])["queued_messages"][0]["message"] == "retained in archive"
    with pytest.raises(AgentError, match="agent_task_archived"):
        runtime.queue_message(row["id"], {"message": "reject"})


def test_T7_owner_guarded_http_queue_crud(tmp_path):
    app = FastAPI()
    def guard(request):
        if request.headers.get("x-owner") != "yes":
            raise HTTPException(401)
    runtime = install_agent(app, store=object(), home=tmp_path, workers=BackgroundWorkerRegistry(),
        local_control=guard, messaging_key=X25519PrivateKey.generate(), integrations=(Model(), Tools(), Catalog()))
    runtime.update_settings({"enabled": True, "model_id": "local:test"})
    row = runtime.create({"message": "start"})
    root = f"/api/local/agent/tasks/{row['id']}/queue"
    with TestClient(app) as client:
        assert client.get(root).status_code == 401
        assert client.post(root, json={"message": "no owner"}).status_code == 401
        assert client.patch(root + "/unknown", json={"message": "no owner"}).status_code == 401
        assert client.delete(root + "/unknown").status_code == 401
        assert client.post(root + "/reorder", json={"ids": []}).status_code == 401
        headers = {"x-owner": "yes"}
        response = client.post(root, headers=headers, json={"message": "queued"})
        assert response.status_code == 200
        item = response.json()["queued_messages"][0]
        assert client.get(root, headers=headers).json()["queued_messages"][0]["id"] == item["id"]
        assert client.patch(root + "/" + item["id"], headers=headers, json={"message": "edited"}).status_code == 200
        assert client.post(root + "/reorder", headers=headers, json={"ids": [item["id"]]}).status_code == 200
        assert client.delete(root + "/" + item["id"], headers=headers).json()["queued_messages"] == []


def test_T1_queued_context_overflow_continues_and_moves_the_rest(tmp_path, monkeypatch):
    from rynmesh.agent_runtime import event_compaction
    runtime, model, _, _ = make_runtime(tmp_path, assistant("first"), assistant("second"), assistant("third"))
    row = runtime.create({"message": "start"})
    runtime.queue_message(row["id"], {"message": "next"})
    runtime.queue_message(row["id"], {"message": "last"})
    monkeypatch.setattr(event_compaction, "over_hard_limit", lambda candidate, *a, **kw: candidate["id"] == row["id"])
    advance(runtime)
    old = runtime.get(row["id"])
    assert old["result"] == "first" and old["continued_to"]
    child = runtime.get(old["continued_to"])
    assert child["messages"][0]["content"].endswith("next")
    assert [item["message"] for item in child["queued_messages"]] == ["last"]
    assert child["queued_messages_paused_reason"] is None
    advance(runtime, 2)
    assert runtime.get(child["id"])["result"] == "third"
    assert len(model.calls) == 3


def test_T2_bad_queued_attachment_preserves_completed_answer_and_queue(tmp_path):
    runtime, _, _, _ = make_runtime(tmp_path, assistant("completed before missing attachment"))
    row = runtime.create({"message": "start"})
    row = runtime.queue_message(row["id"], {"message": "review", "attachments": [
        {"name": "note.txt", "data_base64": base64.b64encode(b"file").decode()}]})
    Path(row["queued_messages"][0]["attachments"][0]["path"]).unlink()
    advance(runtime)
    row = runtime.get(row["id"])
    assert row["result"] == "completed before missing attachment" and row["status"] == "completed"
    assert len(row["queued_messages"]) == 1
    assert row["queued_messages_paused_reason"] == "agent_attachment_unavailable"


def test_T2_explicit_resume_reenables_remaining_queue(tmp_path):
    runtime, _, _, _ = make_runtime(tmp_path, assistant("resumed"), assistant("queued"))
    row = runtime.create({"message": "start"})
    runtime.queue_message(row["id"], {"message": "next"})
    runtime.action(row["id"], {"action": "pause"})
    row = runtime.get(row["id"])
    runtime.action(row["id"], {"action": "resume", "expected_control_revision": row["control_revision"]})
    advance(runtime, 2)
    assert runtime.get(row["id"])["result"] == "queued"


def test_T5_enqueue_replay_after_dispatch_cannot_duplicate_a_turn(tmp_path):
    runtime, _, _, _ = make_runtime(tmp_path, assistant("first"), assistant("second"))
    row = runtime.create({"message": "start"})
    value = {"message": "migrated", "client_request_id": "b22fa4f7-699e-47e9-a413-b22d41a11ac2"}
    runtime.queue_message(row["id"], value)
    advance(runtime, 2)
    row = runtime.queue_message(row["id"], value)
    assert row["queued_messages"] == []
    assert [m["content"] for m in row["messages"]].count("migrated") == 1


def test_T5_send_now_steers_an_active_turn_and_preserves_the_rest(tmp_path):
    runtime, _, _, _ = make_runtime(tmp_path, assistant("urgent done"), assistant("remaining done"))
    row = runtime.create({"message": "start"})
    row = runtime.queue_message(row["id"], {"message": "urgent"})
    item_id = row["queued_messages"][0]["id"]
    runtime.queue_message(row["id"], {"message": "remaining"})
    row = runtime.update_queued(row["id"], item_id, {"send_now": True})
    assert row["goal"]["revision"] == 2
    assert row["goal"]["constraints"][-1]["text"] == "urgent"
    assert [message["ui"]["kind"] for message in row["messages"]
            if message["role"] == "user" and message["content"] == "urgent"] == ["steer"]
    assert [item["message"] for item in row["queued_messages"]] == ["remaining"]
    advance(runtime, 2)
    assert runtime.get(row["id"])["result"] == "remaining done"


def test_T3_migration_to_already_completed_task_starts_without_browser_dispatch(tmp_path):
    runtime, _, _, _ = make_runtime(tmp_path, assistant("first"), assistant("migrated"))
    row = runtime.create({"message": "start"})
    advance(runtime)
    row = runtime.queue_message(row["id"], {"message": "migrated followup"})
    assert row["status"] == "queued" and row["queued_messages"] == []
    advance(runtime)
    assert runtime.get(row["id"])["result"] == "migrated"


def test_T3_migrated_paused_queue_does_not_start_until_owner_requests(tmp_path):
    runtime, model, _, _ = make_runtime(tmp_path, assistant("first"), assistant("migrated"))
    row = runtime.create({"message": "start"})
    advance(runtime)
    row = runtime.queue_message(row["id"], {"message": "migrated paused", "paused_reason": "failed"})
    assert row["status"] == "completed" and row["queued_messages_paused_reason"] == "failed"
    advance(runtime)
    assert len(model.calls) == 1
    runtime.update_queued(row["id"], row["queued_messages"][0]["id"], {"send_now": True})
    advance(runtime)
    assert runtime.get(row["id"])["result"] == "migrated"
