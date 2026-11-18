"""S13-I6: watchdog activity is memory-only and includes background commands."""
import threading
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from rynmesh.agent_runtime.runtime import AgentRuntime
from tests.test_ovh_s02_i12 import _make_app_with_llm


def runtime():
    rt = AgentRuntime.__new__(AgentRuntime)
    rt._active_lock = threading.Lock()
    rt._active_runs = {1: {"task_id": "one"}}
    rt.tools = SimpleNamespace(local=SimpleNamespace(processes=SimpleNamespace(
        lock=threading.Lock(), items={
            "live": {"process": SimpleNamespace(poll=lambda: None)},
            "done": {"process": SimpleNamespace(poll=lambda: 0)},
        })))
    rt.store = SimpleNamespace(_published=(None, {"tasks": {}}))
    return rt


def test_t1_agent_and_background_processes_reach_desktop_status(tmp_path):
    app, _ = _make_app_with_llm(tmp_path)
    rt = runtime()
    app.state.agent_runtime = rt
    with TestClient(app) as client:
        body = client.get("/api/local/desktop/status").json()
        assert body["active_tasks"] == 2
        assert body["agent_tasks"] == 2
        assert body["agent_active_runs"] == 1
        rt._active_runs.clear()
        assert client.get("/api/local/desktop/status").json()["agent_tasks"] == 1
        rt.tools.local.processes.items.clear()
        assert client.get("/api/local/desktop/status").json()["active_tasks"] == 0


def test_active_count_does_not_wait_for_runtime_or_process_lock():
    rt = runtime()
    with ThreadPoolExecutor() as pool:
        with rt._active_lock, rt.tools.local.processes.lock:
            assert pool.submit(rt.active_run_count).result(timeout=1) == 2


@pytest.mark.parametrize("agent", [None, SimpleNamespace(), SimpleNamespace(
    active_run_count=lambda: (_ for _ in ()).throw(RuntimeError("unavailable")))])
def test_status_handles_missing_or_failing_agent(tmp_path, agent):
    app, _ = _make_app_with_llm(tmp_path)
    app.state.agent_runtime = agent
    with TestClient(app) as client:
        body = client.get("/api/local/desktop/status").json()
        assert body["agent_tasks"] == body["active_tasks"] == 0


def test_watchdog_does_not_read_provider_settings_or_agent_storage(tmp_path, monkeypatch):
    from pathlib import Path
    app, _ = _make_app_with_llm(tmp_path)
    app.state.agent_runtime = runtime()
    def no_disk(*args, **kwargs):
        raise AssertionError("watchdog must not read settings or agent storage")
    monkeypatch.setattr(Path, "read_text", no_disk)
    with TestClient(app) as client:
        assert client.get("/api/local/desktop/status").json()["agent_tasks"] == 2


def test_real_background_process_holds_activity_until_it_exits(tmp_path):
    import subprocess
    import sys
    rt = runtime()
    rt._active_runs.clear()
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    rt.tools = SimpleNamespace(processes=SimpleNamespace(items={"live": {"process": child}}))
    app, _ = _make_app_with_llm(tmp_path)
    app.state.agent_runtime = rt
    try:
        with TestClient(app) as client:
            assert client.get("/api/local/desktop/status").json()["active_tasks"] == 1
            child.terminate()
            child.wait(timeout=5)
            assert client.get("/api/local/desktop/status").json()["active_tasks"] == 0
    finally:
        if child.poll() is None:
            child.kill()
            child.wait(timeout=5)


def test_status_remains_responsive_while_agent_locks_are_held(tmp_path):
    app, _ = _make_app_with_llm(tmp_path)
    rt = runtime()
    app.state.agent_runtime = rt
    with TestClient(app) as client, ThreadPoolExecutor() as pool:
        with rt._active_lock, rt.tools.local.processes.lock:
            body = pool.submit(lambda: client.get("/api/local/desktop/status").json()).result(timeout=2)
        assert body["agent_tasks"] == 2
        assert body["agent_active_runs"] == 1
