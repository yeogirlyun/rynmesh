"""Large outputs stay encrypted and cannot bypass task/scope isolation."""
from __future__ import annotations

import json
import uuid

import pytest
from test_agent_runtime import advance, assistant, make_runtime
from test_agent_runtime_lifecycle import change
from test_agent_runtime_retry import failing_model

from rynmesh.agent_runtime import results
from rynmesh.agent_runtime.store import AgentError


def large_result(tmp_path):
    runtime, model, tools, _ = make_runtime(tmp_path, assistant(name="inspect_service", args={"service_id": "A"}))
    runtime.update_settings({"max_output_chars": 2000})
    execute = tools.execute
    def large(*args, **kwargs):
        return {**execute(*args, **kwargs), "text": "PRIVATE_LARGE_RESULT_512 " * 300}
    tools.execute = large
    row = runtime.create({"message": "Inspect evidence"})
    advance(runtime, 2)
    current = runtime.get(row["id"])
    reference = current["result_refs"][0]
    return runtime, model, row, reference


def test_large_result_reference_has_encrypted_pageable_original(tmp_path):
    runtime, model, row, reference = large_result(tmp_path)
    current = runtime.get(row["id"])
    assert "PRIVATE_LARGE_RESULT_512" not in json.dumps(current)
    assert "PRIVATE_LARGE_RESULT_512" not in results.path(runtime.store, row["id"], reference["id"]).read_text()
    output = ""
    offset = 0
    while offset is not None:
        page = runtime.get_result(row["id"], reference["id"], offset, 1024)
        output += page["content"]
        offset = page["next_offset"]
    decoded = json.loads(output)
    assert decoded["text"] == "PRIVATE_LARGE_RESULT_512 " * 300
    assert decoded["api_key"] == "[redacted]"
    failing_model(model, assistant(name="ryn_read_result", args={"result_id": reference["id"]}), assistant("Read a bounded excerpt"))
    advance(runtime, 3)
    assert "PRIVATE_LARGE_RESULT_512" in json.dumps(model.calls[-1][1])
    assert runtime.get(row["id"])["status"] == "completed"


def test_result_reference_cannot_cross_tasks_or_scope_epoch(tmp_path):
    runtime, model, row, reference = large_result(tmp_path)
    other = runtime.create({"message": "Other task"})
    with pytest.raises(AgentError, match="agent_result_not_found"):
        runtime.get_result(other["id"], reference["id"])
    runtime.store.transaction(lambda data: data["tasks"][row["id"]].update(context_epoch=1))
    selected = runtime.store.read()["tasks"][row["id"]]
    with pytest.raises(AgentError, match="agent_result_scope_changed"):
        results.read(runtime.store, selected, reference["id"], provider=True)
    assert "PRIVATE_LARGE_RESULT_512" in runtime.get_result(row["id"], reference["id"])["content"]


def test_delete_cleanup_retry_removes_blob_and_old_archive_without_resurrection(tmp_path, monkeypatch):
    runtime, model, row, reference = large_result(tmp_path)
    failing_model(model, assistant("done"))
    advance(runtime)
    change(runtime, row["id"], "archive")
    change(runtime, row["id"], "restore")
    current = runtime.get(row["id"])
    body = {"expected_control_revision": current["control_revision"], "client_request_id": str(uuid.uuid4())}
    purge = results.purge
    monkeypatch.setattr(results, "purge", lambda *args: (_ for _ in ()).throw(OSError("simulated disk cleanup failure")))
    deleted = runtime.lifecycle(row["id"], body, "delete")
    assert deleted == {"id": row["id"], "deleted": True, "cleanup_pending": True}
    with pytest.raises(AgentError, match="agent_task_deleted"):
        runtime.get(row["id"])
    assert results.path(runtime.store, row["id"], reference["id"]).exists()
    forgotten = []
    runtime.tools.forget_task = lambda task_id, *, operation_ids: forgotten.append((task_id, operation_ids))
    monkeypatch.setattr(results, "purge", purge)
    assert runtime.lifecycle(row["id"], body, "delete")["cleanup_pending"] is False
    assert not results.path(runtime.store, row["id"], reference["id"]).exists()
    assert not list((tmp_path / "archives").rglob("sessions.json"))
    assert forgotten[0][0] == row["id"] and reference["operation_id"] in forgotten[0][1]
    assert not runtime.list_tasks() and not runtime.list_tasks(True)
