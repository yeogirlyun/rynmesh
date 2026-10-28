import json

import pytest

from rynmesh.llm_package import antigravity_session as session
from rynmesh.llm_package.adapters import AdapterError
from rynmesh.llm_package.cli_adapter import CLIAgentAdapter


def test_model_list_parses_progress_duplicates_and_preserves_cli_default():
    from rynmesh.llm_package.cli_models import parse_antigravity_models
    rows = parse_antigravity_models("Fetching available models...\nmodel-a\tModel A\nmodel-a\tDuplicate\nmodel-b\tModel B\n")
    assert rows == [{"id": "", "name": "CLI default", "default": True},
                    {"id": "model-a", "name": "Model A", "default": False},
                    {"id": "model-b", "name": "Model B", "default": False}]


@pytest.mark.parametrize("output", ["Fetching available models...", "--flag\tBad", "model\t"])
def test_invalid_or_empty_model_list_fails(output):
    from rynmesh.llm_package.cli_models import parse_antigravity_models
    with pytest.raises(AdapterError):
        parse_antigravity_models(output)


@pytest.mark.parametrize("model", ["", "gemini-3.8-flash-high"])
def test_selected_model_reaches_process_arguments(monkeypatch, model):
    from types import SimpleNamespace
    commands = []
    adapter = CLIAgentAdapter("antigravity_cli")
    monkeypatch.setattr(adapter, "_command_path", lambda: "agy")
    monkeypatch.setattr(session, "spawn", lambda args, **kwargs: commands.append(args) or SimpleNamespace(returncode=0))
    monkeypatch.setattr(session, "terminate", lambda _: None)
    monkeypatch.setattr(session, "communicate_bounded", lambda *args, **kwargs: json.dumps({
        "status": "SUCCESS", "response": "ok", "structured_output": {"content": "ok", "tool_calls": []}}))
    result = adapter.infer(prompt="hello", max_tokens=32, task_id="test", timeout_s=1, model=model)
    assert result["text"] == "ok"
    if model:
        assert commands[0][commands[0].index("--model") + 1] == model
    else:
        assert "--model" not in commands[0]


def test_discovery_is_cached_and_returns_independent_rows(monkeypatch):
    from types import SimpleNamespace

    from rynmesh.llm_package import cli_models
    cli_models._cache.clear()
    commands = []
    monkeypatch.setattr(cli_models, "spawn", lambda args, **kwargs: commands.append(args) or SimpleNamespace(returncode=0))
    monkeypatch.setattr(cli_models, "terminate", lambda _: None)
    monkeypatch.setattr(cli_models, "communicate_bounded", lambda *args, **kwargs: b"model-a\tModel A\n")
    first = cli_models.list_models("antigravity_cli", "fixture-agy")
    first[1]["id"] = "tampered"
    assert cli_models.list_models("antigravity_cli", "fixture-agy")[1]["id"] == "model-a"
    assert commands == [["fixture-agy", "models"]]
    cli_models._cache.clear()


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


@pytest.mark.parametrize("answer", ["1591。暗号是蓝桥。", "```json\n{\"answer\": 1600}\n```", "A plain-text follow-up answer."])
def test_plain_conversations_do_not_require_tool_proposal_json(monkeypatch, answer):
    seen = []
    def fake(adapter, prompt, **kwargs):
        seen.append((json.loads(prompt.split("\n", 1)[1]), kwargs))
        return {"text": answer, "message": {"role": "assistant", "content": answer}, "finish_reason": "stop"}
    monkeypatch.setattr(session, "infer", fake)
    messages = [{"role": "user", "content": "37*43; remember blue bridge"}, {"role": "assistant", "content": "1591"}, {"role": "user", "content": "Add 9"}]
    result = CLIAgentAdapter("antigravity_cli").chat({"messages": messages}, task_id="plain", timeout_s=1, model="gemini-3.8-flash-medium")
    assert result["text"] == answer
    assert seen[0][0]["messages"] == messages
    assert seen[0][1]["model"] == "gemini-3.8-flash-medium"
    assert "output_schema" not in seen[0][0]


def test_client_protocol_schema_reaches_actual_spawn_and_structured_output_is_consumed(monkeypatch):
    from types import SimpleNamespace
    commands = []
    adapter = CLIAgentAdapter("antigravity_cli")
    monkeypatch.setattr(adapter, "_command_path", lambda: "pinned-agy")
    monkeypatch.setattr(session, "spawn", lambda args, **kwargs: commands.append(args) or SimpleNamespace(returncode=0))
    monkeypatch.setattr(session, "terminate", lambda _: None)
    structured = {"content": "Requested inspection.", "tool_calls": [{"name": "probe", "arguments": {"value": "ok"}}]}
    monkeypatch.setattr(session, "communicate_bounded", lambda *args, **kwargs: json.dumps({
        "status": "SUCCESS", "response": "Display text, not protocol JSON.", "structured_output": structured,
        "usage": {"input_tokens": 19, "output_tokens": 7}}))
    tool = {"type": "function", "function": {"name": "probe", "parameters": {
        "type": "object", "properties": {"value": {"const": "ok"}}, "required": ["value"]}}}
    result = adapter.chat({"messages": [{"role": "user", "content": "Inspect"}], "tools": [tool], "tool_choice": "required"},
                          task_id="schema-spawn", timeout_s=1, model="gemini-4-argon-low")
    args = commands[0]
    schema = json.loads(args[args.index("--json-schema") + 1])
    assert args[0] == "pinned-agy" and args[args.index("--model") + 1] == "gemini-4-argon-low"
    assert args[args.index("--output-format") + 1] == "json"
    assert "--disable-slash-commands" in args and "--dangerously-skip-permissions" not in args
    assert schema["type"] == "object" and schema["additionalProperties"] is False
    assert schema["properties"]["tool_calls"]["minItems"] == 1
    assert schema["properties"]["tool_calls"]["items"]["properties"]["name"]["enum"] == ["probe"]
    assert result["text"] == "Requested inspection." and result["finish_reason"] == "tool_calls"
    assert result["input_tokens"] == 19 and result["output_tokens"] == 7
    assert result["message"]["tool_calls"][0]["function"] == {"name": "probe", "arguments": '{"value": "ok"}'}


def test_invalid_structured_output_cannot_fall_back_to_valid_display_text(monkeypatch):
    monkeypatch.setattr(session, "infer", lambda *args, **kwargs: {
        "text": json.dumps({"content": "ok", "tool_calls": []}),
        "structured_output": {"content": "", "tool_calls": [{"name": "run_shell_command", "arguments": {}}]}})
    tool = {"type": "function", "function": {"name": "probe", "parameters": {"type": "object"}}}
    with pytest.raises(AdapterError) as info:
        CLIAgentAdapter("antigravity_cli").chat({"messages": [{"role": "user", "content": "Inspect"}], "tools": [tool]},
                                               task_id="strict-structured", timeout_s=1)
    assert info.value.proposal_feedback_code == "tool_name"
