"""Real tools + installed HTTP API + temporary Git repositories; no model calls."""
import copy
import os
import subprocess
import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
from test_agent_local_tools import execute
from test_agent_runtime import Catalog, Model, Tools

from rynmesh.agent_runtime.local_tools import LocalTools
from rynmesh.agent_runtime.operation_ledger import OperationLedger
from rynmesh.agent_runtime.routes import install_agent
from rynmesh.agent_runtime.store import AgentError
from rynmesh.background_workers import BackgroundWorkerRegistry
from rynmesh.services import peer_box


def environment(base, *, guard=True):
    base = Path(base)
    home, workspace = base / "home", base / "workspace"
    workspace.mkdir(parents=True, exist_ok=True)
    home.mkdir(parents=True, exist_ok=True)
    if not (workspace / ".git").exists():
        subprocess.run(["git", "init", "-q", str(workspace)], check=True)
    app, adapter, model = FastAPI(), Tools(), Model()
    def owner(request):
        if guard and request.headers.get("x-test-owner") != "yes":
            raise HTTPException(403, "local_control_required")
    runtime = install_agent(app, store=object(), home=home, workers=BackgroundWorkerRegistry(), local_control=owner,
                            messaging_key=peer_box.load_or_create_messaging_key(home / "messaging.x25519"),
                            integrations=(model, adapter, Catalog()))
    ledger = OperationLedger(home)
    local = LocalTools(SimpleNamespace(home=home), lambda: ledger)
    local.store = runtime.store
    adapter.local = local
    adapter.change_review = local.change_review
    runtime.update_settings({"enabled": True, "model_id": "local:test", "workspace_root": str(workspace)})
    return app, runtime, local, workspace, model


@pytest.fixture
def env(tmp_path):
    app, runtime, local, workspace, model = environment(tmp_path)
    task = runtime.create({"message": "Review files"})
    runtime.store.transaction(lambda data: data["tasks"][task["id"]].update(status="paused"))
    context = {"task_id": task["id"], "local_execution": runtime.store.read()["tasks"][task["id"]]["local_execution"]}
    client = TestClient(app, headers={"x-test-owner": "yes"})
    yield SimpleNamespace(app=app, runtime=runtime, local=local, workspace=workspace, model=model,
                          task=task["id"], context=context, client=client, base=tmp_path)
    assert not model.calls
    local.processes.close()
    client.close()


def tool(e, name, args):
    return execute(e.local, e.context, name, args, uuid.uuid4().hex)


def write(e, path, content):
    return tool(e, "ryn_file_write", {"path": path, "content": content})


def get(e):
    response = e.client.get(f"/api/local/agent/tasks/{e.task}/changes")
    assert response.status_code == 200, response.text
    return response.json()


def preview(e, ids=None):
    response = e.client.post(f"/api/local/agent/tasks/{e.task}/changes/preview", json={"ids": ids})
    assert response.status_code == 200, response.text
    return response.json()


def undo(e, p=None, ids=None, request_id=None):
    p = p or preview(e, ids)
    response = e.client.post(f"/api/local/agent/tasks/{e.task}/changes/undo", json={"ids": ids, "token": p["token"], "confirm": True, "request_id": request_id or uuid.uuid4().hex})
    assert response.status_code == 200, response.text
    return response.json()


def test_create_modify_delete_rename_untracked_binary_and_git_history(env):
    e = env
    original = {"tracked.txt": b"original\r\n", "untracked.txt": b"untracked\n", "gone.txt": b"delete me", "old.txt": b"move me", "image.bin": b"\xff\x80\x00\x01"}
    for path, raw in original.items():
        (e.workspace / path).write_bytes(raw)
    subprocess.run(["git", "-C", str(e.workspace), "add", "tracked.txt"], check=True)
    subprocess.run(["git", "-C", str(e.workspace), "-c", "user.name=Test", "-c", "user.email=test@example.test", "commit", "-qm", "base"], check=True)
    head = subprocess.check_output(["git", "-C", str(e.workspace), "rev-parse", "HEAD"])
    write(e, "tracked.txt", "changed\n")
    write(e, "untracked.txt", "changed untracked\n")
    write(e, "new.txt", "new\n")
    tool(e, "ryn_file_manage", {"path": "gone.txt", "action": "delete"})
    tool(e, "ryn_file_manage", {"path": "old.txt", "destination": "new-name.txt", "action": "move"})
    tool(e, "ryn_file_manage", {"path": "image.bin", "action": "delete"})
    review = get(e)
    files = {f["path"]: f for f in review["files"]}
    assert files["tracked.txt"]["diff"] and files["tracked.txt"]["status"] == "modified"
    assert files["image.bin"]["binary"] and "diff" not in files["image.bin"]
    assert files["image.bin"]["before_bytes"] == 4
    assert files["old.txt"]["id"] == files["new-name.txt"]["id"]
    selected = [files["new-name.txt"]["id"]]
    assert len(preview(e, selected)["items"]) == 2
    assert undo(e, ids=selected)["status"] == "completed"
    p = preview(e)
    assert p["can_undo"]
    request_id = uuid.uuid4().hex
    result = undo(e, p, request_id=request_id)
    assert result["status"] == "completed"
    assert undo(e, p, request_id=request_id) == result
    assert not (e.workspace / "new.txt").exists() and not (e.workspace / "new-name.txt").exists()
    for path, raw in original.items():
        assert (e.workspace / path).read_bytes() == raw
    assert not get(e)["files"]
    assert subprocess.check_output(["git", "-C", str(e.workspace), "rev-parse", "HEAD"]) == head
    assert b"untracked.txt" in subprocess.check_output(["git", "-C", str(e.workspace), "ls-files", "--others"])


def test_user_conflict_blocks_whole_batch_even_after_preview(env):
    e = env
    write(e, "a.txt", "agent a")
    write(e, "b.txt", "agent b")
    p = preview(e)
    (e.workspace / "b.txt").write_bytes(b"user later")
    result = undo(e, p)
    assert result["status"] == "conflict" and not result["completed"]
    assert (e.workspace / "a.txt").read_bytes() == b"agent a"
    assert (e.workspace / "b.txt").read_bytes() == b"user later"
    assert not preview(e)["can_undo"]
    assert any(f.get("conflict") == "file_changed" for f in get(e)["files"])


def test_same_bytes_rewritten_by_user_are_a_different_version(env):
    e = env
    write(e, "x", "same bytes")
    path = e.workspace / "x"
    stamp = path.stat().st_mtime_ns
    path.write_bytes(b"same bytes")
    os.utime(path, ns=(stamp + 1000000000, stamp + 1000000000))
    assert not preview(e)["can_undo"]
    assert undo(e)["status"] == "conflict"
    assert path.read_bytes() == b"same bytes"


def test_symlink_and_hardlink_file_rejected(env):
    e = env
    target = e.workspace / "target"
    target.write_bytes(b"keep")
    alias = e.workspace / "alias"
    os.link(target, alias)
    with pytest.raises(AgentError):
        write(e, "alias", "bad")
    alias.unlink()
    try:
        alias.symlink_to(target)
    except OSError:
        # Windows symlink privilege is not guaranteed; junction coverage is unconditional.
        return
    with pytest.raises(AgentError):
        write(e, "alias", "bad")
    assert target.read_bytes() == b"keep"


def test_preview_token_stale_after_new_file_tool_change(env):
    e = env
    write(e, "x", "first")
    p = preview(e)
    write(e, "x", "second")
    response = e.client.post(f"/api/local/agent/tasks/{e.task}/changes/undo", json={"token": p["token"], "confirm": True, "request_id": "old-preview"})
    assert response.status_code == 409 and response.json()["detail"] == "agent_review_preview_changed"
    assert (e.workspace / "x").read_bytes() == b"second"


def test_cross_task_conflict_and_new_task_restoration(env):
    e = env
    write(e, "shared.txt", "task A")
    other = e.runtime.create({"message": "second task"})
    e.runtime.store.transaction(lambda d: d["tasks"][other["id"]].update(status="paused"))
    ctx = {**e.context, "task_id": other["id"]}
    execute(e.local, ctx, "ryn_file_write", {"path": "shared.txt", "content": "task B"}, "other")
    assert undo(e)["status"] == "conflict"
    assert (e.workspace / "shared.txt").read_bytes() == b"task B"
    other_env = copy.copy(e)
    other_env.task = other["id"]
    assert undo(other_env)["status"] == "completed"
    assert (e.workspace / "shared.txt").read_bytes() == b"task A"


def test_intervening_user_change_inside_task_is_not_rolled_back(env):
    e = env
    (e.workspace / "x").write_bytes(b"original")
    write(e, "x", "first agent")
    (e.workspace / "x").write_bytes(b"user insertion")
    tool(e, "ryn_file_read", {"path": "x"})
    write(e, "x", "second agent")
    p = preview(e)
    assert p["conflicts"][0]["reason"] == "intervening_changes"
    assert undo(e, p)["status"] == "conflict"
    assert (e.workspace / "x").read_bytes() == b"second agent"


def test_restart_reads_saved_before_and_after_and_undo(env):
    e = env
    (e.workspace / "x").write_bytes(b"before")
    write(e, "x", "after")
    before = get(e)
    app, runtime, local, _, model = environment(e.base)
    restarted = copy.copy(e)
    restarted.local, restarted.runtime = local, runtime
    restarted.client = TestClient(app, headers={"x-test-owner": "yes"})
    assert get(restarted)["files"] == before["files"]
    assert undo(restarted)["status"] == "completed"
    assert (e.workspace / "x").read_bytes() == b"before"
    assert not model.calls
    local.processes.close()


def test_undo_failure_reports_partial_progress_and_preserves_remaining(env, monkeypatch):
    from rynmesh.agent_runtime.file_review import FileReview
    e = env
    write(e, "a", "A")
    write(e, "b", "B")
    original = FileReview.replace
    calls = []
    def fail_second(self, *args):
        calls.append(1)
        if len(calls) == 2:
            raise OSError("injected disk failure")
        return original(self, *args)
    monkeypatch.setattr(FileReview, "replace", fail_second)
    result = undo(e)
    assert result["status"] == "interrupted" and len(result["completed"]) == 1
    assert len(result["remaining"]) == 1
    assert not (e.workspace / "a").exists() and (e.workspace / "b").read_bytes() == b"B"
    monkeypatch.setattr(FileReview, "replace", original)
    assert get(e)["undo_operations"][-1]["status"] == "interrupted"
    assert undo(e)["status"] == "completed"


def test_process_exit_after_undo_write_recovers_without_replay(env):
    import json
    import sys
    e = env
    (e.workspace / "a").write_bytes(b"before")
    write(e, "a", "after")
    p = preview(e)
    child = """
import os, sys, json
sys.path.insert(0, 'tests')
from test_agent_file_changes import environment
from fastapi.testclient import TestClient
from rynmesh.agent_runtime.file_review import FileReview
app, runtime, local, workspace, model = environment(sys.argv[1])
original = FileReview.save
def crash(self, state):
    if state['undos'] and state['undos'][-1]['items'][0]['state'] == 'done':
        os._exit(73)
    return original(self, state)
FileReview.save = crash
client = TestClient(app, headers={'x-test-owner': 'yes'})
client.post('/api/local/agent/tasks/' + sys.argv[2] + '/changes/undo', json=json.loads(sys.argv[3]))
os._exit(74)
"""
    result = subprocess.run([sys.executable, '-X', 'utf8', '-c', child, str(e.base), e.task,
                             json.dumps({"token": p["token"], "confirm": True, "request_id": "crash"})],
                            cwd=Path(__file__).resolve().parents[1], timeout=90, capture_output=True)
    assert result.returncode == 73, result.stderr.decode(errors="replace")
    review = get(e)
    assert review["undo_operations"][-1]["status"] == "interrupted"
    assert review["undo_operations"][-1]["completed"] == ["a"]
    assert (e.workspace / "a").read_bytes() == b"before"
    (e.workspace / "a").write_bytes(b"user after recovery")
    repeat = undo(e, p, request_id="crash")
    assert repeat["status"] == "interrupted"
    assert (e.workspace / "a").read_bytes() == b"user after recovery"


def test_tool_interruption_never_claims_uncertain_path(env, monkeypatch):
    from rynmesh.agent_runtime.file_review import FileReview
    e = env
    original = FileReview.save
    def fail(self, state):
        if state["records"] and state["records"][-1]["steps"][0]["state"] == "applied":
            raise OSError("simulated journal failure")
        original(self, state)
    monkeypatch.setattr(FileReview, "save", fail)
    with pytest.raises(AgentError):
        write(e, "uncertain", "bytes did get written")
    monkeypatch.setattr(FileReview, "save", original)
    assert (e.workspace / "uncertain").read_bytes() == b"bytes did get written"
    assert get(e)["files"][0]["problem"] == "interrupted_file_operation"
    assert not preview(e)["can_undo"]


@pytest.mark.parametrize("path", ["../outside", ".git/config", ".git/objects/evil"])
def test_tool_boundary(env, path):
    before = (env.workspace / ".git/config").read_bytes()
    with pytest.raises(AgentError):
        write(env, path, "do not write")
    assert (env.workspace / ".git/config").read_bytes() == before
    assert not (env.base / "outside").exists()


def test_junction_replacement_after_snapshot_blocks_undo(env):
    e = env
    folder = e.workspace / "folder"
    folder.mkdir()
    write(e, "folder/x", "agent")
    outside = e.base / "outside"
    folder.rename(outside)
    if os.name == "nt":
        subprocess.run(["cmd", "/c", "mklink", "/J", str(folder), str(outside)], check=True, capture_output=True)
    else:
        folder.symlink_to(outside, target_is_directory=True)
    try:
        assert not preview(e)["can_undo"]
        assert undo(e)["status"] == "conflict"
        assert (outside / "x").read_bytes() == b"agent"
        with pytest.raises(AgentError):
            write(e, "folder/new", "escape")
    finally:
        folder.rmdir() if os.name == "nt" else folder.unlink()


def test_capacity_rejects_before_mutation_and_missing_blob_disables_undo(env, monkeypatch):
    from rynmesh.agent_runtime import file_review
    e = env
    monkeypatch.setattr(file_review, "MAX_BYTES", 10)
    with pytest.raises(AgentError, match="agent_review_storage_limit"):
        write(e, "x", "not written")
    assert not (e.workspace / "x").exists()
    monkeypatch.setattr(file_review, "MAX_BYTES", 8 * 1024 * 1024)
    (e.workspace / "x").write_bytes(b"original")
    write(e, "x", "after")
    original_copy = get(e)["files"][0]["original_copy"]
    Path(original_copy).unlink()
    assert get(e)["files"][0]["can_undo"] is False
    assert not preview(e)["can_undo"]
    assert undo(e)["status"] == "conflict"
    assert (e.workspace / "x").read_bytes() == b"after"


def test_owner_policy_confirmation_and_active_task_checks(env):
    e = env
    write(e, "x", "data")
    url = f"/api/local/agent/tasks/{e.task}/changes"
    with TestClient(e.app) as client:
        assert client.get(url).status_code == 403
        assert client.post(url + "/undo", json={}).status_code == 403
    assert e.client.post(url + "/undo", json={}).status_code == 409
    e.runtime.store.transaction(lambda d: d["tasks"][e.task].update(status="running"))
    assert e.client.post(url + "/preview", json={}).json()["detail"] == "agent_review_pause_task_first"
    e.runtime.store.transaction(lambda d: d["tasks"][e.task].update(status="paused"))
    e.runtime.update_settings({"permission_mode": "read_only"})
    assert e.client.post(url + "/preview", json={}).json()["detail"] == "agent_review_read_only"
    e.runtime.update_settings({"enabled": False})
    assert e.client.get(url).status_code == 409
    assert (e.workspace / "x").read_bytes() == b"data"


def test_command_observation_is_never_authorized_for_undo(env):
    e = env
    e.runtime.store.transaction(lambda d: d["tasks"][e.task].update(status="running"))
    result = tool(e, "ryn_shell_execute", {"command": "Set-Content observed.txt observed" if os.name == "nt" else "echo observed > observed.txt"})
    assert result["exit_code"] == 0, result
    e.runtime.store.transaction(lambda d: d["tasks"][e.task].update(status="paused"))
    e.runtime.store.transaction(lambda d: d["tasks"][e.task].setdefault("receipts", []).append({"tool": "ryn_shell_execute", "result": result}))
    review = get(e)
    assert review["observed"] and not review["files"]
    assert not preview(e)["can_undo"]
    assert (e.workspace / "observed.txt").exists()
