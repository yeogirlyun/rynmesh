"""S13-I4: startup recovery through production worker and owner API paths."""
import threading
from types import SimpleNamespace

import pytest
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey
from fastapi import FastAPI
from fastapi.testclient import TestClient
from test_agent_runtime import Catalog, Model, Tools, assistant

from rynmesh.agent_runtime import routes
from rynmesh.agent_runtime import store as store_module
from rynmesh.agent_runtime.runtime import AgentRuntime
from rynmesh.agent_runtime.store import AgentError, AgentStore
from rynmesh.background_workers import BackgroundWorkerRegistry, WorkerRunResult


def seed(home, key):
    runtime = AgentRuntime(AgentStore(home / "agent", key), Model(), Tools(), Catalog())
    runtime.update_settings({"enabled": True, "model_id": "local:test"})
    return runtime


def install(home, key, model=None):
    app, workers = FastAPI(), BackgroundWorkerRegistry()
    runtime = routes.install_agent(app, store=object(), home=home, workers=workers,
                                  local_control=lambda _: None, messaging_key=key,
                                  integrations=(model or Model(), Tools(), Catalog()))
    worker = next(s for s in workers.specs() if s.name == "ryn-agent")
    return app, runtime, worker, workers


def lock_candidate_reads(monkeypatch, locked):
    read = AgentStore._read_root_candidate
    def blocked(self, path):
        if locked[0] and path.name in {"sessions.json", "sessions.json.prev"}:
            raise PermissionError("locked root read")
        return read(self, path)
    monkeypatch.setattr(AgentStore, "_read_root_candidate", blocked)


def test_t1_locked_root_recovers_at_30_seconds_and_real_worker_processes_task(tmp_path, monkeypatch):
    key = X25519PrivateKey.generate()
    seeded = seed(tmp_path, key)
    task = seeded.create({"message": "What is 2 + 2?"})
    locked, now, attempts = [True], [0.0], []
    stat = store_module._stat_or_none
    reconcile = AgentRuntime.reconcile
    adopted = []
    adopt = AgentRuntime.adopt_shipped_permission_mode
    def record_adopt(self, mode):
        adopted.append(mode)
        return adopt(self, mode)
    monkeypatch.setattr(AgentRuntime, "adopt_shipped_permission_mode", record_adopt)
    def locked_stat(path):
        if locked[0] and path.name in {"sessions.json", "sessions.json.prev"}:
            raise PermissionError("private locked file")
        return stat(path)
    def count_reconcile(self):
        attempts.append(now[0])
        return reconcile(self)
    monkeypatch.setattr(store_module, "_stat_or_none", locked_stat)
    lock_candidate_reads(monkeypatch, locked)
    monkeypatch.setattr(AgentRuntime, "reconcile", count_reconcile)
    monkeypatch.setattr(routes, "time", SimpleNamespace(monotonic=lambda: now[0]))
    model = Model(assistant("4"))
    app, runtime, worker, workers = install(tmp_path, key, model)
    assert app.state.agent_startup_error
    assert attempts == [0.0]
    now[0], locked[0] = 29.0, False
    for item in workers.specs():
        item.run_once()
    assert attempts == [0.0] and model.calls == []
    now[0] = 30.0
    worker.run_once(cancelled=threading.Event())
    assert attempts == [0.0, 30.0]
    assert app.state.agent_startup_error is None
    assert len(model.calls) == 1
    assert runtime.get(task["id"])["status"] == "completed"
    assert adopted == ["automatic"]
    assert runtime.store.inspect(lambda d: d["settings"]["permission_mode"]) == "workspace"


def test_t2_settings_reports_damaged_task_health(tmp_path):
    key = X25519PrivateKey.generate()
    runtime = seed(tmp_path, key)
    runtime.store.transaction(lambda d: d.update(damaged_tasks={"task_lost": {"reason": "missing_blob", "file": None, "at": 1}}))
    app, _, _, _ = install(tmp_path, key)
    with TestClient(app) as client:
        response = client.get("/api/local/agent/settings")
        assert response.status_code == 200
        assert response.json()["storage_health"] == {
            "state": "damaged", "damaged_tasks": 1,
            "quarantine_dir": str(tmp_path / "agent" / "quarantine"), "recovered_from_prev": False,
        }
        status = client.get("/api/local/agent/computer/status")
        assert status.status_code == 200
        assert status.json()["storage_health"] == response.json()["storage_health"]


def test_corrupt_roots_expose_health_even_when_apis_fail_without_overwriting(tmp_path):
    key = X25519PrivateKey.generate()
    root = tmp_path / "agent"
    root.mkdir()
    (root / "sessions.json").write_bytes(b"damaged root")
    (root / "sessions.json.prev").write_bytes(b"damaged backup")
    app, _, _, _ = install(tmp_path, key)
    with TestClient(app) as client:
        settings = client.get("/api/local/agent/settings")
        assert settings.status_code >= 400
        assert settings.json()["storage_health"]["state"] == "damaged"
        status = client.get("/api/local/agent/computer/status")
        assert status.status_code == 503
        assert status.json()["storage_health"]["state"] == "damaged"
    assert (root / "sessions.json").read_bytes() == b"damaged root"
    assert (root / "sessions.json.prev").read_bytes() == b"damaged backup"


def test_readable_previous_root_is_recovered_without_disabling_workers(tmp_path):
    key = X25519PrivateKey.generate()
    runtime = seed(tmp_path, key)
    runtime.update_settings({"max_steps": 12})
    runtime.store.path.write_bytes(b"damaged root")
    app, _, _, _ = install(tmp_path, key)
    assert app.state.agent_startup_error is None
    with TestClient(app) as client:
        health = client.get("/api/local/agent/settings").json()["storage_health"]
        assert health["state"] == "recovered"
        assert health["recovered_from_prev"] is True


def test_non_storage_startup_fault_does_not_disable_workers(tmp_path, monkeypatch):
    key = X25519PrivateKey.generate()
    seed(tmp_path, key)
    def other_fault(self):
        raise AgentError("agent_task_cleanup_failed")
    monkeypatch.setattr(AgentRuntime, "reconcile", other_fault)
    app, _, _, _ = install(tmp_path, key)
    assert app.state.agent_startup_error is None


def test_lifespan_shutdown_stops_recovery_probe(tmp_path, monkeypatch):
    key = X25519PrivateKey.generate()
    seed(tmp_path, key)
    now, locked, attempts = [0.0], [True], []
    stat, reconcile = store_module._stat_or_none, AgentRuntime.reconcile
    def locked_stat(path):
        if locked[0] and path.name in {"sessions.json", "sessions.json.prev"}:
            raise PermissionError("locked")
        return stat(path)
    def count(self):
        attempts.append(now[0])
        return reconcile(self)
    monkeypatch.setattr(store_module, "_stat_or_none", locked_stat)
    lock_candidate_reads(monkeypatch, locked)
    monkeypatch.setattr(AgentRuntime, "reconcile", count)
    monkeypatch.setattr(routes, "time", SimpleNamespace(monotonic=lambda: now[0]))
    app, _, worker, _ = install(tmp_path, key)
    with TestClient(app):
        assert app.state.agent_startup_error
    now[0], locked[0] = 60.0, False
    worker.run_once()
    assert attempts == [0.0]
    assert app.state.agent_startup_error is not None


def test_backup_button_endpoint_opens_only_server_selected_quarantine(tmp_path, monkeypatch):
    key = X25519PrivateKey.generate()
    seed(tmp_path, key)
    app, _, _, _ = install(tmp_path, key)
    opened = []
    monkeypatch.setattr(routes.os, "startfile", lambda path: opened.append(str(path)), raising=False)
    monkeypatch.setattr(routes, "sys", SimpleNamespace(platform="win32"), raising=False)
    with TestClient(app) as client:
        response = client.post("/api/local/agent/storage/backups/open", json={"path": "ignored-client-input"})
        assert response.status_code == 200
        assert response.json() == {"opened": True}
    assert opened == [str(tmp_path / "agent" / "quarantine")]

def test_shutdown_during_read_probe_does_not_start_reconcile_after_close(tmp_path, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    key = X25519PrivateKey.generate()
    seed(tmp_path, key)
    locked, now, attempts = [True], [0.0], []
    entered, release = threading.Event(), threading.Event()
    stat, reconcile = store_module._stat_or_none, AgentRuntime.reconcile
    def controlled_stat(path):
        if path.name in {"sessions.json", "sessions.json.prev"}:
            if locked[0]:
                raise PermissionError("locked")
            entered.set()
            assert release.wait(3)
        return stat(path)
    def count(self):
        attempts.append(now[0])
        return reconcile(self)
    monkeypatch.setattr(store_module, "_stat_or_none", controlled_stat)
    lock_candidate_reads(monkeypatch, locked)
    monkeypatch.setattr(AgentRuntime, "reconcile", count)
    monkeypatch.setattr(routes, "time", SimpleNamespace(monotonic=lambda: now[0]))
    app, _, worker, _ = install(tmp_path, key)
    with ThreadPoolExecutor(max_workers=1) as executor:
        try:
            with TestClient(app):
                locked[0], now[0] = False, 30.0
                pending = executor.submit(worker.run_once)
                assert entered.wait(2)
            release.set()
            pending.result(timeout=2)
            assert attempts == [0.0]
            assert app.state.agent_startup_error is not None
        finally:
            release.set()

def test_locked_primary_with_readable_backup_is_not_total_startup_failure(tmp_path, monkeypatch):
    key = X25519PrivateKey.generate()
    seeded = seed(tmp_path, key)
    seeded.update_settings({"max_steps": 12})
    stat = store_module._stat_or_none
    def primary_locked(path):
        if path.name == "sessions.json":
            raise PermissionError("primary locked")
        return stat(path)
    monkeypatch.setattr(store_module, "_stat_or_none", primary_locked)
    app, _, worker, _ = install(tmp_path, key)
    assert app.state.agent_startup_error is None
    # The valid backup prevents total startup failure, but a locked primary
    # must not be overwritten by a business invocation.
    assert worker.run_once() == WorkerRunResult()
