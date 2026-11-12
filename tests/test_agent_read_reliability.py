import pytest
import test_agent_local_tools as consumer_fixtures
from test_agent_runtime import advance, assistant, make_runtime

from rynmesh.agent_runtime.read_reliability import check_shell_retry
from rynmesh.agent_runtime.store import AgentToolRejected

local = consumer_fixtures.local


def row(path, root="C:\\"):
    return {"goal": {"revision": 1}, "scope_revision": 1, "context_epoch": 0,
            "local_execution": {"workspace_root": root}, "receipts": [{"tool": "ryn_file_read",
            "arguments": {"path": path}, "status": "not_executed", "goal_revision": 1,
            "scope_revision": 1, "context_epoch": 0,
            "result": {"error": "agent_local_file_missing_or_too_large"}}]}


@pytest.mark.parametrize("command", [
    "$p='C:\\不存在的目录\\a.txt'; while ($true) { try { [System.IO.File]::ReadAllText($p); break } catch { Start-Sleep -Seconds 5 } }",
    "while ($true) { try { Get-Content 'C:\\不存在的目录\\a.txt'; break } catch { Start-Sleep 5 } }",
])
def test_recorded_windows_ui_retry_loops_are_refused_before_dispatch(command):
    with pytest.raises(AgentToolRejected, match="agent_unbounded_read_retry"):
        check_shell_retry(row("C:\\不存在的目录\\a.txt"), {"name": "ryn_shell_execute", "arguments": {"command": command}})


def test_quoted_code_comments_bounded_programs_and_unrelated_reads_remain_usable():
    source = row("C:\\missing.txt")
    commands = ["Write-Output 'while ($true) ReadAllText C:\\missing.txt'",
                "# ReadAllText C:\\missing.txt\nwhile ($true) { Write-Output 'alive' }",
                "for ($i=0; $i -lt 3; $i++) { Get-Content 'C:\\missing.txt' }",
                "while ($true) { Get-Content 'C:\\other.txt' }"]
    for command in commands:
        check_shell_retry(source, {"name": "ryn_shell_execute", "arguments": {"command": command}})
    source["goal"]["revision"] = 2
    check_shell_retry(source, {"name": "ryn_shell_execute", "arguments": {"command": "while ($true) { Get-Content 'C:\\missing.txt' }"}})


def test_managed_wait_has_a_real_attempt_limit_and_can_recover(local, monkeypatch):
    tools, context, workspace, _ = local
    original, calls = tools.text, []
    def text(path):
        calls.append(path)
        if len(calls) == 2:
            path.write_text("ready", encoding="utf-8")
        return original(path)
    monkeypatch.setattr(tools, "text", text)
    result = tools.perform("ryn_file_wait", {"path": "later.txt", "max_attempts": 3, "retry_delay_seconds": 0}, context, lambda: False)
    assert result["text"] == "ready" and result["attempts"] == 2 and len(calls) == 2


def test_managed_wait_counts_earlier_failures_and_cancels_promptly(local, monkeypatch):
    tools, context, workspace, store = local
    receipt = row(str(workspace / "missing.txt"), str(workspace))["receipts"][0]
    store.transaction(lambda data: data["tasks"]["task"].setdefault("receipts", []).append(receipt))
    original, calls = tools.text, []
    def text(path):
        calls.append(path)
        return original(path)
    monkeypatch.setattr(tools, "text", text)
    result = tools.perform("ryn_file_wait", {"path": "missing.txt", "retry_delay_seconds": 0}, context, lambda: False)
    assert result["error"] == "agent_file_retry_exhausted" and result["attempts"] == 2 and len(calls) == 2
    calls.clear()
    with pytest.raises(AgentToolRejected, match="agent_cancelled"):
        tools.perform("ryn_file_wait", {"path": "other.txt", "retry_delay_seconds": 2}, context, lambda: bool(calls))
    assert len(calls) == 1


def test_runtime_rejects_loop_without_asking_for_approval(tmp_path):
    path = str(tmp_path / "missing.txt")
    command = f"while true; do cat '{path}'; sleep 5; done"
    runtime, _, tools, _ = make_runtime(tmp_path / "agent",
        assistant(name="ryn_file_read", args={"path": path}),
        assistant(name="ryn_shell_execute", args={"command": command}), assistant("The input file is missing."))
    def specs():
        return [{"type": "function", "function": {"name": name, "description": name,
            "parameters": {"type": "object", "properties": {field: {"type": "string"}}, "required": [field], "additionalProperties": False}}}
            for name, field in (("ryn_file_read", "path"), ("ryn_shell_execute", "command"))]
    tools.specs = specs
    tools.metadata = lambda name: {"effect": "write" if name == "ryn_shell_execute" else "read"}
    def execute(name, *args, **kwargs):
        assert name == "ryn_file_read"
        raise AgentToolRejected("agent_local_file_missing_or_too_large")
    tools.execute = execute
    tools.preview = lambda *args, **kwargs: pytest.fail("unbounded retry must stop before approval preview")
    runtime.update_settings({"permission_mode": "confirm_changes", "workspace_root": str(tmp_path)})
    task = runtime.create({"message": "Read the missing file, retry until it works."})
    advance(runtime, 6)
    final = runtime.get(task["id"])
    assert final["status"] == "completed" and not final["pending_approval"]
    receipts = runtime.store.read()["tasks"][task["id"]]["receipts"]
    assert any(r["result"].get("error") == "agent_unbounded_read_retry" and r["status"] == "not_executed" for r in receipts)
