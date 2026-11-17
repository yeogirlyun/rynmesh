"""S06-I25: production registry concurrency and independent cleanup attempts."""
import copy
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import pytest
from test_agent_integrations import node, add_source
from rynmesh.agent_runtime.bridge import build_agent_integrations
from rynmesh.agent_runtime import tools as module
from rynmesh.agent_runtime.store import AgentError, AgentToolRejected


def registry(node):
    app, store = node
    return build_agent_integrations(app, store, store.home)[1]


def test_T1_s06_i25_two_device_discoveries_overlap(node, monkeypatch):
    tools = registry(node)
    calls = []
    def discover(**kwargs):
        calls.append(kwargs)
        time.sleep(1)
        return {"peers": []}
    monkeypatch.setattr(tools.store, "discover_peers", discover)
    start = threading.Barrier(3)
    def run(index):
        start.wait()
        return tools.execute("ryn_list_devices", {}, operation_id=f"discovery-{index}", cancelled=lambda: False)
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(run, index) for index in range(2)]
        before = time.monotonic()
        start.wait()
        rows = [future.result(timeout=4) for future in futures]
        elapsed = time.monotonic() - before
    assert len(calls) == 2 and elapsed < 1.5
    assert all(row["self_id"] == tools.store.peer_id for row in rows)


@pytest.mark.parametrize("name", ["ryn_list_models", "ryn_inspect_services"])
def test_T1_s06_i25_other_readers_run_while_device_probe_is_blocked(node, monkeypatch, name):
    tools = registry(node)
    entered, release = threading.Event(), threading.Event()
    monkeypatch.setattr(tools.store, "discover_peers", lambda **kwargs: {"peers": []})
    def check(peers):
        assert tools.peer_health_lock.locked()
        entered.set()
        assert release.wait(5)
        return []
    monkeypatch.setattr(tools.peer_health, "check", check)
    with ThreadPoolExecutor(max_workers=2) as pool:
        probing = pool.submit(tools.execute, "ryn_list_devices", {}, operation_id="probe", cancelled=lambda: False)
        try:
            assert entered.wait(2)
            reading = pool.submit(tools.execute, name, {}, operation_id="other", cancelled=lambda: False)
            assert "models" in reading.result(timeout=0.5)
        finally:
            release.set()
        probing.result(timeout=3)


def test_T1_s06_i25_cache_copy_conflict_and_capacity(node, monkeypatch):
    tools = registry(node)
    calls = []
    monkeypatch.setattr(tools.catalog, "list_models", lambda: calls.append(1) or [{"id": "model"}])
    first = tools.execute("ryn_list_models", {}, operation_id="same", cancelled=lambda: False)
    first["models"].clear()
    assert tools.execute("ryn_list_models", {}, operation_id="same", cancelled=lambda: False)["models"] == [{"id": "model"}]
    assert len(calls) == 1
    with pytest.raises(AgentToolRejected, match="agent_operation_id_conflict"):
        tools.execute("ryn_list_devices", {}, operation_id="same", cancelled=lambda: False)
    for i in range(260):
        tools.execute("ryn_list_models", {}, operation_id=f"cached-{i}", cancelled=lambda: False)
    assert len(tools.completed) == 256 and "same" not in tools.completed


def test_T2_s06_i25_read_does_not_observe_sharing_half_write(node, monkeypatch):
    app, store = node
    record, model_id = add_source(app)
    store.personal_space.create("Test space")
    store.personal_space.set_policy("space")
    tools = registry(node)
    args = {"model_id": model_id, "enabled": True}
    preview = tools.preview("ryn_set_model_sharing", args)
    sources = tools.services["source_store"]
    entered, release = threading.Event(), threading.Event()
    update = sources.save
    def held(*args, **kwargs):
        entered.set()
        assert release.wait(5)
        return update(*args, **kwargs)
    monkeypatch.setattr(sources, "save", held)
    with ThreadPoolExecutor(max_workers=2) as pool:
        writer = pool.submit(tools.execute, "ryn_set_model_sharing", args, operation_id="write", cancelled=lambda: False, expected_preview=preview)
        try:
            assert entered.wait(2)
            reader = pool.submit(tools.execute, "ryn_list_models", {}, operation_id="read", cancelled=lambda: False)
            time.sleep(0.05)
            assert not reader.done()
        finally:
            release.set()
        assert writer.result(timeout=3)["ok"]
        assert any(model["id"] == model_id and model["sharing"] for model in reader.result(timeout=3)["models"])


@pytest.mark.parametrize("multiple", [False, True])
@pytest.mark.parametrize("coded", [False, True])
def test_T3_s06_i25_cleanup_attempts_every_module_and_clears_cache(node, monkeypatch, caplog, multiple, coded):
    tools = registry(node)
    calls = []
    names = ["artifacts", "resource_writes", "source_drafts", "friend_sharing", "local", "craft", "extensions", "code_mode", "native_computer"]
    def callback(name):
        def invoke(*args, **kwargs):
            calls.append((name, args, kwargs))
            if name == "source_drafts" or multiple and name == "code_mode":
                if coded:
                    raise AgentError("agent_private_cleanup_marker")
                raise ValueError("PRIVATE-CLEANUP-MESSAGE /secret/path sk-abcdef012345")
        return invoke
    monkeypatch.setattr(module, "ArtifactTransfers", lambda store: SimpleNamespace(forget_task=callback("artifacts")))
    for name in names[1:]:
        method = "forget_baselines" if name == "local" else "close_task" if name == "craft" else "forget_task"
        if name == "local":
            monkeypatch.setattr(tools.local, method, callback(name))
        else:
            monkeypatch.setattr(tools, name, SimpleNamespace(**{method: callback(name)}))
    tools.completed.update({"owned": ("fingerprint", {}), "other": ("fingerprint", {})})
    with pytest.raises(AgentError, match="^agent_task_cleanup_failed$"):
        tools.forget_task("task", operation_ids=["owned"])
    assert [name for name, _, _ in calls] == names
    assert "owned" not in tools.completed and "other" in tools.completed
    assert next(kwargs for name, _, kwargs in calls if name == "extensions") == {"operation_ids": ["owned"]}
    assert "source_drafts" in caplog.text and ("AgentError" if coded else "ValueError") in caplog.text
    assert "PRIVATE-CLEANUP-MESSAGE" not in caplog.text and "/secret/path" not in caplog.text
    assert "agent_private_cleanup_marker" not in caplog.text
    if multiple:
        assert "code_mode" in caplog.text
