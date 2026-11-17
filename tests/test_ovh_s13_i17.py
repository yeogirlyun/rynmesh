"""S13-I17: real encrypted runtime and partial consumer privacy erasure."""
import base64
import threading
import time
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from fastapi.testclient import TestClient
from test_agent_runtime import assistant, make_runtime
from test_agent_projects import project_runtime
from test_consumer_privacy import order
from test_conversation_cleanup import fixture as cleanup_fixture

from rynmesh.agent_runtime import memory
from rynmesh.agent_runtime.runtime import AgentRuntime
from rynmesh.agent_runtime.store import AgentStore
from rynmesh.agent_runtime.store import AgentError
from rynmesh.agent_runtime.local_processes import Processes
from rynmesh.llm_package.privacy import erase_consumer_results
from rynmesh.llm_package.task_protocol import TaskOrderStore
from rynmesh.file_transactions import file_transaction
from rynmesh.peer_http import create_app
from rynmesh.store import RynmeshStore


def remember(runtime):
    def seed(data):
        memory.upsert_active_entry(data, {"text": "Owner prefers concise replies", "scope": {"kind": "personal"}})
        memory.ensure_memory_state(data)["candidates"]["candidate-private"] = {
            "id": "candidate-private", "text": "Candidate private fact", "sources": [], "scope": {"kind": "personal"}}
    runtime.store.transaction(seed)


def test_T1_history_removes_active_archived_chunks_files_quarantine_and_keeps_configuration(tmp_path):
    runtime, model, tools, key = make_runtime(tmp_path, assistant("Done"))
    remember(runtime)
    completed = runtime.create({"message": "archive me"})
    runtime.run_once()
    row = runtime.store.read()["tasks"][completed["id"]]
    runtime._lifecycle(row["id"], {"expected_control_revision": row["control_revision"]}, "archive")
    runtime.create({"message": "queued private", "attachments": [{"name": "private.txt", "media_type": "text/plain",
        "data_base64": base64.b64encode(b"private attachment").decode()}]})
    for relative in ("quarantine/lost/task.json", "results/orphan/result.json", "files/private-output.txt", "baselines/orphan/before.txt"):
        target = tmp_path / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("private data")
    keep = tmp_path / "extensions_pkgs" / "installed" / "plugin.json"
    keep.parent.mkdir(parents=True)
    keep.write_text("installed plugin")
    credentials = tmp_path / "extensions.json"
    credentials.write_text("credential placeholder")
    settings = runtime.settings()
    runtime.erase_owner_data({"agent_history"})
    assert runtime.list_tasks() == runtime.list_tasks(archived=True) == []
    for directory in ("tasks", "archives", "results", "files", "quarantine", "baselines"):
        assert not [p for p in (tmp_path / directory).rglob("*") if p.is_file()], directory
    assert runtime.settings() == settings
    assert not runtime.store.prev_path.exists()
    assert keep.read_text() == "installed plugin" and credentials.read_text() == "credential placeholder"
    assert runtime.store.read()["memory"]["entries"]
    restarted = AgentRuntime(AgentStore(tmp_path, key), model, tools, runtime.catalog)
    assert restarted.list_tasks() == restarted.list_tasks(archived=True) == []
    assert restarted.store.read()["memory"]["entries"]


def test_T2_memory_only_preserves_history_files_settings_and_clears_candidates(tmp_path):
    runtime, _, _, _ = make_runtime(tmp_path)
    remember(runtime)
    task = runtime.create({"message": "preserve history"})
    output = Path(runtime.store.default_workspace()) / "keep.txt"
    output.write_text("keep")
    settings = runtime.settings()
    runtime.erase_owner_data({"agent_memory"})
    state = runtime.store.read()
    assert not state["memory"]["entries"] and not state["memory"]["candidates"]
    assert task["id"] in state["tasks"] and output.read_text() == "keep"
    assert runtime.settings() == settings
    assert not runtime.store.prev_path.exists()


def test_T2_finished_consumer_results_clear_while_active_result_remains(tmp_path):
    orders = TaskOrderStore(tmp_path)
    for identifier in ("one", "two", "active"):
        order(orders, identifier, state="accepted" if identifier == "active" else "succeeded")
    pending = {"two": {"state": "succeeded", "output": "private"}, "active": {"state": "running", "output": "unfinished"}}
    result = erase_consumer_results(orders, pending, threading.RLock(), ["one", "two", "active"])
    assert result["results_removed"] == 2 and result["skipped_active"] == ["active"]
    assert all("encrypted_response" not in orders.get(identifier) for identifier in ("one", "two"))
    assert "encrypted_response" in orders.get("active") and pending["active"]["output"] == "unfinished"
    assert "output" not in pending["two"]


def test_T1_http_scopes_reach_real_runtime(tmp_path, monkeypatch):
    monkeypatch.setenv("RYNMESH_HOME", str(tmp_path / "node"))
    monkeypatch.setenv("RYNMESH_AUTO_REGISTER", "0")
    monkeypatch.setenv("RYNMESH_ALLOW_REMOTE_CONTROL", "1")
    app = create_app(RynmeshStore())
    runtime, _, _, _ = make_runtime(tmp_path / "agent")
    app.state.agent_runtime = runtime
    remember(runtime)
    runtime.create({"message": "private history"})
    client = TestClient(app)
    response = client.post("/api/local/privacy/erase", json={"scopes": ["agent_history", "agent_memory"]})
    assert response.status_code == 200, response.text
    assert response.json()["erased"] == ["agent_history", "agent_memory"]
    assert runtime.list_tasks() == [] and not runtime.store.read()["memory"]["entries"]


def test_T1_stop_cancels_real_inflight_model_before_deleting(tmp_path):
    runtime, model, _, _ = make_runtime(tmp_path)
    started, exited = threading.Event(), threading.Event()
    def blocking(model_id, messages, tool_specs, cancelled, on_event):
        started.set()
        deadline = time.monotonic() + 10
        while not cancelled() and time.monotonic() < deadline:
            time.sleep(.01)
        assert cancelled()
        exited.set()
        return assistant("late reply")
    model.complete = blocking
    runtime.create({"message": "running"})
    worker = threading.Thread(target=runtime.run_once)
    worker.start()
    try:
        assert started.wait(5)
        runtime.erase_owner_data({"agent_history"})
        assert exited.is_set() and runtime.list_tasks() == []
    finally:
        worker.join(12)
    assert not worker.is_alive()


def test_T2_cleanup_job_retains_skipped_orders_and_retries_real_results(tmp_path):
    job, _ = cleanup_fixture(tmp_path)
    orders = TaskOrderStore(tmp_path / "orders")
    order(orders, "active", state="accepted")
    # The history checkpoint is terminal while the consumer background task
    # is still active: exactly the gap the partial cleanup must survive.
    with file_transaction(job.source.lock):
        envelope, data = job.source._read()
        data["runs"] = {"version": 1, "records": {"active": {
            "task_id": "active", "conversation_id": "conversation-1", "state": "succeeded"}}}
        job.source._write(envelope, data)
    calls = []
    def erase_results(ids):
        calls.append(ids)
        return erase_consumer_results(orders, {}, threading.RLock(), ids)
    job.erase_order_results = erase_results
    result = job.begin(review_token=job.preview()["review_token"])
    assert result["skipped_active"] == ["active"]
    assert not result["local_copies_complete"] and result["pending"] == ["orders"]
    assert job.status(result["id"])["skipped_active"] == ["active"]
    orders.transition(task_id="active", state="running")
    orders.transition(task_id="active", state="succeeded")
    result = job.resume(result["id"])
    assert result["local_copies_complete"] and result["skipped_active"] == []
    assert "encrypted_response" not in orders.get("active")
    assert calls == [["active"], ["active"]]


def test_T1_stops_owned_background_process_and_keeps_other_process(tmp_path):
    runtime, _, tools, _ = make_runtime(tmp_path)
    processes = Processes()
    tools.local = SimpleNamespace(processes=processes)
    task = runtime.create({"message": "background work"})
    own = processes.start([sys.executable, "-c", "import time; time.sleep(60)"], tmp_path, task["id"])
    other = processes.start([sys.executable, "-c", "import time; time.sleep(60)"], tmp_path, "unrelated-task")
    try:
        runtime.erase_owner_data({"agent_history"})
        assert processes.items[own]["process"].poll() is not None
        assert processes.items[other]["process"].poll() is None
    finally:
        processes.close()


def test_T1_project_attachment_copies_removed_originals_and_business_files_remain(tmp_path):
    runtime, _, _, _ = project_runtime(tmp_path / "agent")
    project_dir = tmp_path / "project"
    project_dir.mkdir()
    business = project_dir / "business.txt"
    business.write_text("business")
    project = runtime.load_project({"path": str(project_dir)})
    task = runtime.create({"message": "project", "project_id": project["id"], "attachments": [{"name": "private.txt",
        "media_type": "text/plain", "data_base64": base64.b64encode(b"copy").decode()}]})
    copied = next((project_dir / ".ryn" / "attachments" / task["id"]).iterdir())
    runtime.erase_owner_data({"agent_history"})
    assert not copied.exists() and business.read_text() == "business"
    assert runtime.list_projects()[0]["id"] == project["id"]


def test_T1_replaced_private_directory_never_erases_external_data(tmp_path):
    runtime, _, _, _ = make_runtime(tmp_path / "agent")
    external = tmp_path / "external"
    external.mkdir()
    (external / "private.txt").write_text("keep external")
    (runtime.store.root / "quarantine").symlink_to(external, target_is_directory=True)
    with pytest.raises(AgentError, match="agent_task_cleanup_failed"):
        runtime.erase_owner_data({"agent_history"})
    assert (external / "private.txt").read_text() == "keep external"
