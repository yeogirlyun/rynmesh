import json

import pytest

from rynmesh.llm_package import antigravity_session as session
from rynmesh.llm_package.adapters import AdapterError
from rynmesh.llm_package.cli_adapter import CLIAgentAdapter


def test_windows_discovery_does_not_require_explorer_path(tmp_path, monkeypatch):
    binary = tmp_path / "agy" / "bin" / "agy.exe"
    binary.parent.mkdir(parents=True)
    binary.write_bytes(b"fixture")
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    monkeypatch.setattr("rynmesh.llm_package.cli_adapter.shutil.which", lambda _: None)
    assert CLIAgentAdapter("antigravity_cli")._command_path() == str(binary)


def test_workspace_denies_provider_execution_and_does_not_copy_owner_config(tmp_path, monkeypatch):
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    env = {"OPENAI_API_KEY": "unrelated-secret"}
    session.prepare_workspace(tmp_path, env)
    settings = json.loads((tmp_path / ".gemini" / "antigravity-cli" / "settings.json").read_text())
    assert settings["permissions"]["allow"] == []
    assert {"read_file(*)", "write_file(*)", "command(*)", "mcp(*)"} <= set(settings["permissions"]["deny"])
    assert "OPENAI_API_KEY" not in env
    assert not (tmp_path / ".gemini" / "GEMINI.md").exists()


@pytest.mark.parametrize("proposal", [
    {"name": "run_shell_command", "arguments": {"command": "whoami"}},
    {"name": "probe", "arguments": {"value": "wrong"}},
])
def test_invalid_or_unknown_client_tool_is_rejected(monkeypatch, proposal):
    def fake(*args, **kwargs):
        return {"text": json.dumps({"content": "", "tool_calls": [proposal]})}
    monkeypatch.setattr(session, "infer", fake)
    tool = {"type": "function", "function": {"name": "probe", "parameters": {
        "type": "object", "properties": {"value": {"const": "ok"}}, "required": ["value"]}}}
    with pytest.raises(AdapterError, match="invalid client tool"):
        CLIAgentAdapter("antigravity_cli").chat({"messages": [{"role": "user", "content": "hello"}],
                                               "tools": [tool], "tool_choice": "required"}, task_id="test", timeout_s=1)


def test_client_tool_result_continuation_and_buffered_stream(monkeypatch):
    seen = []
    def fake(adapter, prompt, **kwargs):
        seen.append(json.loads(prompt.split("\n", 1)[1]))
        return {"text": json.dumps({"content": "ok", "tool_calls": []}), "input_tokens": 2,
                "output_tokens": 1, "message": {}, "finish_reason": "stop"}
    monkeypatch.setattr(session, "infer", fake)
    events = []
    result = CLIAgentAdapter("antigravity_cli").chat({"messages": [
        {"role": "assistant", "content": "", "tool_calls": [{"id": "prior", "type": "function", "function": {"name": "probe", "arguments": "{}"}}]},
        {"role": "tool", "tool_call_id": "prior", "content": "actual result"}], "stream": True},
        task_id="test", timeout_s=1, on_event=events.append)
    assert seen[0]["messages"][-1]["content"] == "actual result"
    assert seen[0]["functions"] == []
    assert result["text"] == "ok"
    assert len(events) == 2
