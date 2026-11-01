"""Background CLI calls keep updater children and console windows out of the UI."""
import json
import os
import sys
from types import SimpleNamespace

import pytest

from rynmesh.llm_package import antigravity_session, cli_models, process_guard
from rynmesh.llm_package.cli_adapter import CLIAgentAdapter


@pytest.mark.parametrize("operation", ["models", "infer"])
def test_antigravity_background_calls_disable_only_their_own_updater(monkeypatch, operation):
    monkeypatch.setenv("AGY_CLI_DISABLE_AUTO_UPDATE", "false")
    captured = []

    def spawn(args, **kwargs):
        captured.append(kwargs["env"].copy())
        return SimpleNamespace(returncode=0)

    module = cli_models if operation == "models" else antigravity_session
    monkeypatch.setattr(module, "spawn", spawn)
    monkeypatch.setattr(module, "terminate", lambda _: None)
    output = b"model-a\tModel A\n" if operation == "models" else json.dumps({
        "status": "SUCCESS", "response": "ok", "usage": {},
        "structured_output": {"content": "ok", "tool_calls": []}}).encode()
    monkeypatch.setattr(module, "communicate_bounded", lambda *a, **kw: output)
    monkeypatch.setattr(cli_models, "_cache", {})
    if operation == "models":
        assert len(cli_models.list_models("antigravity_cli", "fixture-agy")) == 2
    else:
        adapter = CLIAgentAdapter("antigravity_cli")
        monkeypatch.setattr(adapter, "_command_path", lambda: "fixture-agy")
        assert adapter.infer(prompt="hello", task_id="hidden", timeout_s=1, max_tokens=8)["text"] == "ok"
    assert captured[0]["AGY_CLI_DISABLE_AUTO_UPDATE"] == "true"
    assert os.environ["AGY_CLI_DISABLE_AUTO_UPDATE"] == "false"


@pytest.mark.skipif(os.name != "nt", reason="Windows console and Job Object integration")
def test_windows_cli_has_no_console_and_keeps_piped_io(tmp_path):
    script = ("import ctypes,json,sys; "
              "k=ctypes.WinDLL('kernel32'); u=ctypes.WinDLL('user32'); "
              "k.GetConsoleWindow.restype=ctypes.c_void_p; "
              "u.IsWindowVisible.argtypes=[ctypes.c_void_p]; "
              "h=k.GetConsoleWindow(); "
              "print(json.dumps({'console_visible':bool(h and u.IsWindowVisible(h)), "
              "'input':sys.stdin.read()}))")
    process = process_guard.spawn([sys.executable, "-c", script], cwd=str(tmp_path),
                                  env=dict(os.environ), timeout=10)
    try:
        assert getattr(process, "_ryn_job", None)
        output = process_guard.communicate_bounded(process, b"background input", timeout=10, root=tmp_path)
        assert process.returncode == 0
        assert json.loads(output) == {"console_visible": False, "input": "background input"}
    finally:
        process_guard.terminate(process)
