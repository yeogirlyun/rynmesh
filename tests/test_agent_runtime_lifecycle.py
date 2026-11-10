"""Encrypted archives free active capacity without losing request identity."""
from __future__ import annotations

import copy
import threading
import uuid

import pytest
from test_agent_runtime import advance, assistant, make_runtime
from test_agent_runtime_control import control

from rynmesh.agent_runtime.runtime import AgentRuntime
from rynmesh.agent_runtime.store import AgentError, AgentStore


def change(runtime, task_id, operation, **extra):
    row = runtime.get(task_id)
    return runtime.lifecycle(task_id, {"expected_control_revision": row["control_revision"], "client_request_id": str(uuid.uuid4()), **extra}, operation)


def test_archive_is_encrypted_readable_idempotent_and_restores_without_running(tmp_path):
    runtime, model, tools, key = make_runtime(tmp_path, assistant("private archival answer"))
    create = {"message": "private archival goal", "client_request_id": str(uuid.uuid4())}
    row = runtime.create(create)
    advance(runtime)
    current = runtime.get(row["id"])
    body = {"expected_control_revision": current["control_revision"], "client_request_id": str(uuid.uuid4())}
    archived = runtime.lifecycle(row["id"], body, "archive")
    assert archived["archived"] and not runtime.list_tasks()
    assert runtime.lifecycle(row["id"], body, "archive") == archived
    assert runtime.list_tasks(True)[0]["id"] == row["id"]
    assert runtime.create(create)["id"] == row["id"]
    assert not archived["allowed_actions"]["information"]["enabled"]
    assert archived["allowed_actions"]["restore"]["enabled"]
    files = list((tmp_path / "archives").rglob("sessions.json"))
    assert len(files) == 1 and "private archival" not in files[0].read_text()
    assert row["id"] not in runtime.store.read()["tasks"]
    restarted = AgentRuntime(AgentStore(tmp_path, key), model, tools, runtime.catalog)
    restarted.reconcile()
    restored = change(restarted, row["id"], "restore")
    assert restored["status"] == "completed" and restored["archived"] is False
    assert not restarted.list_tasks(True)
    advance(restarted)
    assert len(model.calls) == 1


@pytest.mark.parametrize("archived", [False, True])
def test_deleted_create_request_never_recreates_task(tmp_path, archived):
    runtime, model, tools, key = make_runtime(tmp_path, assistant("answer"))
    create = {"message": "original", "client_request_id": str(uuid.uuid4())}
    row = runtime.create(create)
    advance(runtime)
    if archived:
        change(runtime, row["id"], "archive")
    current = runtime.get(row["id"])
    body = {"expected_control_revision": current["control_revision"], "client_request_id": str(uuid.uuid4())}
    assert runtime.lifecycle(row["id"], body, "delete") == {"id": row["id"], "deleted": True, "cleanup_pending": False}
    assert runtime.lifecycle(row["id"], body, "delete")["deleted"]
    restarted = AgentRuntime(AgentStore(tmp_path, key), model, tools, runtime.catalog)
    with pytest.raises(AgentError, match="agent_task_deleted"):
        restarted.get(row["id"])
    with pytest.raises(AgentError, match="agent_task_deleted"):
        restarted.create(create)
    with pytest.raises(AgentError, match="agent_request_conflict"):
        restarted.create({**create, "message": "new"})
    assert not restarted.list_tasks() and not restarted.list_tasks(True)
    assert not list((tmp_path / "archives").rglob("sessions.json"))


def test_archive_frees_two_hundred_task_active_capacity(tmp_path):
    runtime, _, _, _ = make_runtime(tmp_path, assistant("answer"))
    row = runtime.create({"message": "original"})
    advance(runtime)
    def fill(data):
        template = data["tasks"][row["id"]]
        for _ in range(199):
            copied = copy.deepcopy(template)
            copied["id"] = "agent_" + uuid.uuid4().hex
            data["tasks"][copied["id"]] = copied
    runtime.store.transaction(fill)
    with pytest.raises(AgentError, match="agent_task_limit"):
        runtime.create({"message": "next"})
    change(runtime, row["id"], "archive")
    assert runtime.create({"message": "next"})["status"] == "queued"
    assert len(runtime.list_tasks()) == 200 and len(runtime.list_tasks(True)) == 1
    with pytest.raises(AgentError, match="agent_task_limit"):
        change(runtime, row["id"], "restore")


def test_delete_and_archive_reject_active_call_then_no_late_resurrection(tmp_path):
    runtime, model, _, _ = make_runtime(tmp_path)
    entered, release = threading.Event(), threading.Event()
    def slow(*args):
        entered.set()
        assert release.wait(3)
        return assistant("Late result")
    model.complete = slow
    row = runtime.create({"message": "go"})
    worker = threading.Thread(target=runtime.run_once)
    worker.start()
    assert entered.wait(3)
    control(runtime, row["id"], "cancel")
    for operation in ("delete", "archive"):
        with pytest.raises(AgentError, match="agent_task_busy"):
            change(runtime, row["id"], operation)
    release.set()
    worker.join(3)
    change(runtime, row["id"], "delete")
    advance(runtime)
    assert row["id"] not in runtime.store.read()["tasks"]


def test_lifecycle_request_id_cannot_be_reused_for_different_operation(tmp_path):
    runtime, _, _, _ = make_runtime(tmp_path, assistant("answer"))
    row = runtime.create({"message": "go"})
    advance(runtime)
    body = {"expected_control_revision": runtime.get(row["id"])["control_revision"], "client_request_id": str(uuid.uuid4())}
    runtime.lifecycle(row["id"], body, "archive")
    with pytest.raises(AgentError, match="agent_request_conflict"):
        runtime.lifecycle(row["id"], body, "restore")
