"""Background terminal ownership must outlive a single Agent dispatch tick."""
import json
import os
import sys
import time

import pytest
from test_agent_integrations import add_source, call, fake_upstream
from test_agent_integrations import node as node

from rynmesh.agent_runtime.bridge import build_agent_integrations
from rynmesh.agent_runtime.runtime import AgentRuntime
from rynmesh.agent_runtime.store import AgentStore
from rynmesh.services import peer_box


@pytest.mark.skipif(os.name != "nt", reason="Windows shell and ConPTY integration")
@pytest.mark.parametrize("tty", [False, True])
@pytest.mark.parametrize("launch", [{"background": True}, {"yield_seconds": 0.2}])
def test_process_survives_dispatch_handoff_then_obeys_task_cancel(node, monkeypatch, tmp_path, tty, launch):  # noqa: F811 — imported pytest fixtures
    app, owner_store = node
    _, model_id = add_source(app)
    bridge, tools, catalog = build_agent_integrations(app, owner_store, owner_store.home)
    runtime = AgentRuntime(AgentStore(owner_store.home / "terminal-agent",
        peer_box.load_or_create_messaging_key(owner_store.home / "messaging.x25519")), bridge, tools, catalog)
    runtime.update_settings({"enabled": True, "model_id": model_id, "workspace_root": str(tmp_path)})
    probe = tmp_path / "prompt.py"
    probe.write_text("import time\nprint('READY', flush=True)\nprint('ANSWER='+input(), flush=True)\ntime.sleep(60)\n", encoding="utf-8")
    command = f"& '{sys.executable}' -u '{probe}'"
    args = {"command": command, "tty": tty, "timeout_seconds": 30, **launch}
    fake_upstream(monkeypatch, [call("ryn_shell_execute", json.dumps(args), "terminal")])
    created = runtime.create({"message": "Start an interactive local command and keep its session available."})
    ident = None
    try:
        for _ in range(4):
            runtime.run_once()
            current = runtime.get(created["id"])
            results = [e["result"] for e in current["events"]
                       if e["type"] == "tool_result" and e.get("tool") == "ryn_shell_execute"]
            if results:
                ident = results[-1].get("process_id")
                assert ident, results
                break
        assert ident and current["status"] == "queued", current
        # The command is still authorized while the scheduler yields between
        # ticks. A per-dispatch cancelled() becomes true in this normal state.
        deadline = time.monotonic() + 8
        while time.monotonic() < deadline:
            result = tools.local.processes.read(ident, created["id"], wait_seconds=0)
            assert result["running"], result
            if "READY" in result["output"]:
                break
            time.sleep(0.05)
        assert "READY" in result["output"], result
        time.sleep(0.6)  # Allow several lifetime-watch checks while queued.
        assert tools.local.processes.read(ident, created["id"], wait_seconds=0)["running"]
        tools.local.processes.write(ident, created["id"], "continued\r" if tty else "continued\n")
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            result = tools.local.processes.read(ident, created["id"], wait_seconds=0)
            if "ANSWER=continued" in result["output"]:
                break
            time.sleep(0.05)
        assert "ANSWER=continued" in result["output"] and result["running"], result
        runtime.action(created["id"], {"action": "cancel"})
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            result = tools.local.processes.read(ident, created["id"], wait_seconds=0)
            if not result["running"]:
                break
            time.sleep(0.05)
        assert not result["running"] and result["stop_reason"] == "permission_or_task_stopped", result
    finally:
        tools.local.processes.close()
