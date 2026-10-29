"""Sidebar labels remain durable without altering task execution or project grants."""
from __future__ import annotations

import copy
import threading
import uuid

import pytest
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
from test_agent_projects import project_runtime
from test_agent_runtime import Catalog, Model, Tools, advance, assistant, make_runtime
from test_agent_runtime_lifecycle import change

from rynmesh.agent_runtime.routes import install_agent
from rynmesh.agent_runtime.runtime import AgentRuntime
from rynmesh.agent_runtime.store import AgentError, AgentStore
from rynmesh.background_workers import BackgroundWorkerRegistry


def rename_value(row, title):
    return {"title": title, "expected_control_revision": row["control_revision"], "client_request_id": str(uuid.uuid4())}


def test_project_alias_survives_reload_restart_and_preserves_task_snapshot(tmp_path):
    root = tmp_path / "original-folder"
    root.mkdir()
    marker = root / "keep.txt"
    marker.write_text("Keep project files", encoding="utf-8")
    runtime, model, tools, key = project_runtime(tmp_path / "state")
    project = runtime.load_project({"path": str(root)})
    row = runtime.create({"message": "Inspect", "project_id": project["id"]})
    snapshot = copy.deepcopy(row["project"])

    renamed = runtime.rename_project(project["id"], {"name": "  工作项目  "})
    assert renamed["name"] == "工作项目"
    assert renamed["generation"] == project["generation"]
    assert runtime.get(row["id"])["project"] == snapshot
    assert runtime.load_project({"path": str(root)})["name"] == "工作项目"
    fresh = runtime.create({"message": "Inspect again", "project_id": project["id"]})
    assert fresh["project"]["name"] == "工作项目"
    restarted = AgentRuntime(AgentStore(runtime.store.root, key), model, tools, runtime.catalog)
    assert restarted.list_projects()[0]["name"] == "工作项目"

    restarted.remove_project(project["id"])
    assert not restarted.list_projects() and restarted.project_selection() == ""
    assert restarted.get(row["id"])["project"] == snapshot
    assert marker.read_text(encoding="utf-8") == "Keep project files"
    with pytest.raises(AgentError, match="agent_project_unavailable"):
        restarted.rename_project(project["id"], {"name": "Removed"})


def test_rename_during_inference_keeps_active_result_and_goal(tmp_path):
    runtime, model, _, _ = make_runtime(tmp_path)
    entered, release = threading.Event(), threading.Event()

    def slow(*args):
        entered.set()
        assert release.wait(5)
        return assistant("Original task completed")

    model.complete = slow
    row = runtime.create({"message": "Original instructions"})
    worker = threading.Thread(target=runtime.run_once)
    worker.start()
    try:
        assert entered.wait(5)
        current = runtime.get(row["id"])
        renamed = runtime.rename_task(row["id"], rename_value(current, "  New label  "))
        assert renamed["title"] == "New label"
        assert renamed["goal"] == current["goal"]
        assert renamed["messages"] == current["messages"]
        assert renamed["control_revision"] == current["control_revision"]
        assert renamed["revision"] > current["revision"]
        assert renamed["allowed_actions"]["rename"]["enabled"]
    finally:
        release.set()
        worker.join(5)
    done = runtime.get(row["id"])
    assert not worker.is_alive()
    assert done["status"] == "completed" and done["result"] == "Original task completed"
    assert done["title"] == "New label"


def test_rename_preserves_pending_approval(tmp_path):
    runtime, _, _, _ = make_runtime(tmp_path, assistant(name="share_service", args={"service_id": "svc1"}))
    row = runtime.create({"message": "Share svc1"})
    advance(runtime, 2)
    waiting = runtime.get(row["id"])
    assert waiting["status"] == "waiting_approval"
    renamed = runtime.rename_task(row["id"], rename_value(waiting, "Pending work"))
    assert renamed["pending_approval"] == waiting["pending_approval"]
    approved = runtime.action(row["id"], {"action": "approve", "approval_id": waiting["pending_approval"]["id"],
        "expected_control_revision": waiting["control_revision"]})
    assert approved["status"] == "queued"


@pytest.mark.parametrize("archived", [False, True])
def test_task_rename_is_encrypted_idempotent_and_restart_safe(tmp_path, archived):
    runtime, model, tools, key = make_runtime(tmp_path, assistant("Done"))
    create = {"message": "Original", "client_request_id": str(uuid.uuid4())}
    row = runtime.create(create)
    advance(runtime)
    if archived:
        change(runtime, row["id"], "archive")
    body = rename_value(runtime.get(row["id"]), "private sidebar name")
    renamed = runtime.rename_task(row["id"], body)
    assert runtime.rename_task(row["id"], body) == renamed
    with pytest.raises(AgentError, match="agent_request_conflict"):
        runtime.rename_task(row["id"], {**body, "title": "Different"})
    assert runtime.create(create)["id"] == row["id"]
    restarted = AgentRuntime(AgentStore(runtime.store.root, key), model, tools, runtime.catalog)
    assert restarted.get(row["id"])["title"] == "private sidebar name"
    assert restarted.list_tasks(archived)[0]["title"] == "private sidebar name"
    for path in tmp_path.rglob("*.json"):
        assert "private sidebar name" not in path.read_text(encoding="utf-8")
    if archived:
        assert change(restarted, row["id"], "restore")["title"] == "private sidebar name"


def test_metadata_http_owner_validation_and_stale_task_control(tmp_path):
    app = FastAPI()

    def local_control(request):
        if request.headers.get("x-owner") != "yes":
            raise HTTPException(401, detail="local_control_unauthorized")

    install_agent(app, store=object(), home=tmp_path / "state", workers=BackgroundWorkerRegistry(),
        local_control=local_control, messaging_key=X25519PrivateKey.generate(), integrations=(Model(), Tools(), Catalog()))
    root = tmp_path / "project"
    root.mkdir()
    prefix = "/api/local/agent"
    with TestClient(app) as client:
        assert client.patch(f"{prefix}/projects/unknown", json={"name": "x"}).status_code == 401
        assert client.patch(f"{prefix}/tasks/unknown", json={"title": "x"}).status_code == 401
        headers = {"x-owner": "yes"}
        client.put(f"{prefix}/settings", headers=headers, json={"enabled": True, "model_id": "local:test"})
        project = client.post(f"{prefix}/projects", headers=headers, json={"path": str(root)}).json()
        project_url = f"{prefix}/projects/{project['id']}"
        response = client.patch(project_url, headers=headers, json={"name": "Project name"})
        assert response.status_code == 200 and response.json()["name"] == "Project name"
        assert response.headers["cache-control"] == "no-store"
        for name in ("", " ", "x" * 161, None, "line\nbreak"):
            assert client.patch(project_url, headers=headers, json={"name": name}).status_code == 400
        assert client.patch(project_url, headers=headers, json={"name": "x", "path": "elsewhere"}).status_code == 400

        row = client.post(f"{prefix}/tasks", headers=headers, json={"message": "Original"}).json()
        task_url = f"{prefix}/tasks/{row['id']}"
        body = rename_value(row, "Renamed")
        response = client.patch(task_url, headers=headers, json=body)
        assert response.status_code == 200 and response.json()["title"] == "Renamed"
        assert response.headers["cache-control"] == "no-store"
        for title in ("", " ", "x" * 161, None, "line\nbreak"):
            assert client.patch(task_url, headers=headers, json=rename_value(row, title)).status_code == 400
        assert client.patch(task_url, headers=headers, json={**rename_value(row, "x"), "goal": {}}).status_code == 400
        client.post(f"{task_url}/actions", headers=headers, json={"action": "pause"})
        conflict = client.patch(task_url, headers=headers, json=rename_value(row, "Stale"))
        assert conflict.status_code == 409 and conflict.json()["detail"] == "agent_control_revision_conflict"
        assert conflict.json()["current"]["title"] == "Renamed"
