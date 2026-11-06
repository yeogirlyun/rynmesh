"""A nearly full Agent store archives old finished tasks instead of failing work."""
from __future__ import annotations

import copy
import threading
import uuid

from test_agent_runtime import Tools, advance, assistant, make_runtime

from rynmesh.agent_runtime import storage_relief
from rynmesh.agent_runtime.protocol import RESULT_ON_RECEIPT
from rynmesh.agent_runtime.store import AgentError


def finished_history(runtime, count):
    """`count` finished tasks, oldest first, copied from one real completed task."""
    template_id = runtime.create({"message": "template"})["id"]
    advance(runtime)
    assert runtime.get(template_id)["status"] == "completed"
    ids = []

    def fill(data):
        template = data["tasks"][template_id]
        for index in range(count):
            copied = copy.deepcopy(template)
            copied["id"] = "agent_" + uuid.uuid4().hex
            copied["client_request_id"] = None
            copied["updated_at"] = copied["created_at"] = f"2026-01-01T00:00:{index:02d}+00:00"
            data["tasks"][copied["id"]] = copied
            ids.append(copied["id"])
        data["tasks"].pop(template_id)
    runtime.store.transaction(fill)
    return ids


def set_watermarks(monkeypatch, runtime, *, high_offset=-1):
    total = runtime.store.usage()["total"]
    monkeypatch.setattr(storage_relief, "HIGH_WATER_BYTES", total + high_offset)
    monkeypatch.setattr(storage_relief, "LOW_WATER_BYTES", total // 2)


def test_step_archives_oldest_finished_tasks_and_keeps_recent_and_running(tmp_path, monkeypatch):
    runtime, _, _, _ = make_runtime(tmp_path, assistant("template answer"), assistant("new answer"))
    history = finished_history(runtime, 30)
    running = runtime.create({"message": "new work"})["id"]
    set_watermarks(monkeypatch, runtime)
    advance(runtime)

    assert runtime.get(running)["status"] == "completed"
    active = {row["id"] for row in runtime.list_tasks()}
    archived = [task_id for task_id in history if task_id not in active]
    assert archived, "nothing was archived"
    # Oldest first, and the newest conversations always stay active.
    assert archived == history[:len(archived)]
    assert set(history[-(storage_relief.KEEP_RECENT - 1):]) <= active
    assert runtime.store.usage()["total"] <= storage_relief.HIGH_WATER_BYTES
    row = runtime.get(archived[0])
    assert row["archived"] is True and row["archive_reason"] == "storage"
    assert any(event["type"] == "archived" and event.get("reason") == "storage" for event in row["events"])
    assert {item["id"] for item in runtime.list_tasks(True)} >= set(archived)


def test_idle_worker_archives_in_one_commit_off_the_scheduler(tmp_path, monkeypatch):
    runtime, _, _, _ = make_runtime(tmp_path, assistant("template answer"))
    history = finished_history(runtime, 40)
    set_watermarks(monkeypatch, runtime)
    commits = []
    original = runtime.store.transaction

    def counted(mutate):
        commits.append(getattr(getattr(mutate, "__code__", None), "co_qualname", ""))
        return original(mutate)
    monkeypatch.setattr(runtime.store, "transaction", counted)
    runtime._next_idle_relief = 0
    runtime.run_once()  # nothing is queued: the relief runs on its own thread
    worker = next(thread for thread in threading.enumerate() if thread.name == "ryn-agent-storage-relief")
    worker.join(10)
    archived = [task_id for task_id in history if runtime.get(task_id)["archived"]]
    assert len(archived) > 10
    assert len([name for name in commits if "archive_many" in name]) == 1
    # Throttled: an immediate second idle tick does not start another pass.
    runtime.run_once()
    assert not any(thread.name == "ryn-agent-storage-relief" and thread.is_alive() for thread in threading.enumerate())


def test_archived_task_restores_with_its_history(tmp_path, monkeypatch):
    runtime, _, _, _ = make_runtime(tmp_path, assistant("template answer"))
    history = finished_history(runtime, 20)
    set_watermarks(monkeypatch, runtime)
    assert runtime._relieve_storage("test") > 0
    oldest = runtime.get(history[0])
    assert oldest["archived"] is True
    monkeypatch.setattr(storage_relief, "HIGH_WATER_BYTES", 10 ** 9)
    restored = runtime.lifecycle(history[0], {"expected_control_revision": oldest["control_revision"],
                                              "client_request_id": str(uuid.uuid4())}, "restore")
    assert restored["archived"] is False and "archive_reason" not in restored
    assert [m["content"] for m in restored["messages"]] == [m["content"] for m in oldest["messages"]]


def test_unfinished_automation_and_subagent_tasks_are_never_archived(tmp_path):
    runtime, _, _, _ = make_runtime(tmp_path, assistant("template answer"))
    history = finished_history(runtime, 20)
    waiting, target, parent, child = history[:4]

    def mark(data):
        data["tasks"][waiting]["status"] = "waiting_input"
        data["tasks"][parent]["subagent_ids"] = [child]
        data["tasks"][child]["parent_task_id"] = parent
        data.setdefault("automations", {})["auto_1"] = {"status": "ACTIVE", "target_task_id": target}
    runtime.store.transaction(mark)
    plan = runtime.store.inspect(lambda data: storage_relief.candidates(data, runtime.store.usage()["tasks"], {history[4]}))
    planned = [item["id"] for item in plan]
    assert not {waiting, target, parent, child, history[4]} & set(planned)
    assert planned == history[5:len(history) - storage_relief.KEEP_RECENT]
    assert all(type(item["bytes"]) is int and item["bytes"] > 0 for item in plan)


def test_full_store_commit_frees_space_and_retries_the_step_once(tmp_path, monkeypatch):
    runtime, model, _, _ = make_runtime(tmp_path, assistant("template answer"))
    history = finished_history(runtime, 20)
    task_id = runtime.create({"message": "needs a retry"})["id"]
    set_watermarks(monkeypatch, runtime)  # the store is above its low watermark
    monkeypatch.setattr(storage_relief, "needed", lambda *args, **kwargs: False)  # no proactive relief
    answers = [AgentError("agent_storage_limit"), assistant("answer after relief")]

    def complete(*args, **kwargs):
        model.calls.append(args)
        value = answers.pop(0)
        if isinstance(value, Exception):
            raise value
        return value
    model.complete = complete
    advance(runtime, 3)

    row = runtime.get(task_id)
    assert row["status"] == "completed" and row["error"] is None
    assert [event["error"] for event in row["events"] if event["type"] == "model_retry_scheduled"] == ["agent_storage_limit"]
    assert runtime.get(history[0])["archive_reason"] == "storage"


def test_full_store_with_nothing_to_archive_fails_clearly(tmp_path, monkeypatch):
    runtime, model, _, _ = make_runtime(tmp_path)
    monkeypatch.setattr(storage_relief, "needed", lambda *args, **kwargs: False)
    task_id = runtime.create({"message": "nothing old"})["id"]

    def complete(*args, **kwargs):
        model.calls.append(args)
        raise AgentError("agent_storage_limit")
    model.complete = complete
    advance(runtime, 3)
    row = runtime.get(task_id)
    assert row["status"] == "failed" and row["error"] == "agent_storage_limit"
    assert len(model.calls) == 1


def test_creating_the_201st_task_archives_old_ones_instead_of_refusing(tmp_path):
    runtime, _, _, _ = make_runtime(tmp_path, assistant("template answer"))
    finished_history(runtime, 200)
    assert len(runtime.list_tasks()) == 200
    created = runtime.create({"message": "one more"})
    assert created["status"] == "queued"
    assert len(runtime.list_tasks()) <= storage_relief.LOW_WATER_TASKS + 1
    assert all(item["archive_reason"] == "storage" for item in runtime.list_tasks(True))


class LargeResultTools(Tools):
    def execute(self, name, arguments, *, operation_id, cancelled):
        self.calls.append((name, arguments, operation_id))
        if arguments["service_id"] == "small":
            return {"service_id": "small", "ok": True}
        return {"service_id": arguments["service_id"], "path": "reports/shops.json",
                "rows": [{"shop": f"Caesar {index}", "site": "US"} for index in range(200)]}


def test_large_tool_result_is_stored_once_and_read_back_in_full(tmp_path):
    runtime, _, _, _ = make_runtime(
        tmp_path,
        assistant(name="inspect_service", args={"service_id": "big"}),
        assistant(name="inspect_service", args={"service_id": "small"}),
        assistant("done"))
    runtime.tools = LargeResultTools()
    task_id = runtime.create({"message": "inspect"})["id"]
    advance(runtime, 6)

    stored = runtime.store.read()["tasks"][task_id]
    assert stored["status"] == "completed"
    big_event, small_event = [event for event in stored["events"] if event["type"] == "tool_result"]
    receipt = next(r for r in stored["receipts"] if r["operation_id"] == big_event["operation_id"])
    assert len(receipt["result"]["rows"]) == 200
    # Stored once: the event keeps scalars and a marker, not the rows.
    assert big_event["result"] == {"service_id": "big", "path": "reports/shops.json", RESULT_ON_RECEIPT: True}
    assert small_event["result"] == {"service_id": "small", "ok": True}

    for events in (runtime.get(task_id)["events"], runtime.events(task_id)["events"]):
        shown = [event for event in events if event["type"] == "tool_result"]
        assert shown[0]["result"] == receipt["result"]
        assert shown[1]["result"] == {"service_id": "small", "ok": True}
