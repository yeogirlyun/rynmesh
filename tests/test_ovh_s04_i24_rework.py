"""S04-I24 rework: real extension dispatch and CodeMode worker admission."""
import json
import queue
import sys
import threading
from types import SimpleNamespace

import pytest
from test_agent_extensions_runtime import ext_env, _assistant, _write_fake_mcp_server

from rynmesh.agent_runtime.code_mode import CodeModeRunner
from rynmesh.agent_runtime.extensions import compute_model_tool_name


def desktop_server(env, tmp_path):
    runtime, model, tools, _, _ = env
    script, counter = tmp_path / "desktop.py", tmp_path / "desktop-count.json"
    _write_fake_mcp_server(script, counter)
    store = tools.extensions.store
    server = store.upsert_mcp_server({"id": "desktop", "name": "Desktop", "transport": "stdio",
        "command": sys.executable, "args": ["-u", str(script)], "enabled": True,
        "computer_access": True, "read_only_tools": ["ping_info"]}, expected_revision=store.overview()["revision"])
    tools.extensions.manager.test_server("desktop", expected_revision=server["revision"])
    return compute_model_tool_name("desktop", "ping_info"), counter


@pytest.mark.parametrize("mode,automation,expected", [
    ("automatic", False, True), ("automatic", True, True), ("workspace", False, False),
])
def test_rework_runtime_computer_extension_dispatch(ext_env, tmp_path, mode, automation, expected):
    runtime, model, tools, _, _ = ext_env
    name, counter = desktop_server(ext_env, tmp_path)
    runtime.update_settings({"permission_mode": mode, "desktop_access": False})
    model.responses = [_assistant(name="ryn_discover_tools", args={"load": [name]}),
                       _assistant(name=name, args={"label": "S04-I24"}, call_id="desktop")]
    tid = runtime.create({"message": "Inspect desktop"})["id"]
    def old_task(data):
        row = data["tasks"][tid]
        row["local_execution"]["desktop_access"] = False
        if automation:
            row.update(automation_run_id="run-s04-i24", automation_kind="scheduled")
    runtime.store.transaction(old_task)
    for _ in range(4):
        runtime.run_once()
    row = runtime.store.read()["tasks"][tid]
    receipt = next(r for r in row["receipts"] if r["tool"] == name)
    if expected:
        assert receipt["status"] == "confirmed", receipt.get("result")
        assert json.loads(counter.read_text(encoding="utf-8"))["ping_info"] == 1
        assert row["pending_approval"] is None
    else:
        assert receipt["status"] == "not_executed"
        assert receipt["result"]["error"] == "agent_desktop_access_required"
        assert not counter.exists()


@pytest.mark.parametrize("task_mode,host_mode,expected", [
    ("automatic", "automatic", True), ("workspace", "workspace", False),
    ("automatic", "workspace", False), ("workspace", "automatic", False),
])
def test_rework_code_mode_uses_effective_policy_for_catalog_and_worker(ext_env, tmp_path, monkeypatch, task_mode, host_mode, expected):
    runtime, _, tools, _, _ = ext_env
    name, _ = desktop_server(ext_env, tmp_path)
    runtime.update_settings({"permission_mode": host_mode, "desktop_access": False})
    tid = runtime.create({"message": "Read desktop via code"})["id"]
    def old_task(data):
        row = data["tasks"][tid]
        row["permission_mode"] = task_mode
        row["local_execution"]["desktop_access"] = False
    runtime.store.transaction(old_task)
    row = runtime.store.read()["tasks"][tid]
    context = runtime._execution_context(row, cheap_only=True)
    runner = CodeModeRunner(tools)
    sent = []
    frames = queue.Queue()
    frames.put({"type": "result", "content_items": []})
    worker = SimpleNamespace(exec_lock=threading.Lock(), frames_q=frames, send=sent.append)
    monkeypatch.setattr(runner, "_acquire_worker", lambda *a, **k: (worker, False))
    try:
        catalog, _, _ = runner._read_only_catalog(context)
        assert (name in catalog) is expected
        runner.execute({"code": "text('ready')"}, operation_id="code", cancelled=lambda: False, context=context)
        assert sent[0]["desktop_access"] is expected
    finally:
        runner.close()
