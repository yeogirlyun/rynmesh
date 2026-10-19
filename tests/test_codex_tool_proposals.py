"""Codex proposals stay data until validated by the origin client."""
import io
import json
import queue
import threading
from collections import deque
from types import SimpleNamespace

import pytest

from rynmesh.llm_package import codex_session
from rynmesh.llm_package.adapters import AdapterError
from rynmesh.llm_package.cli_adapter import CLIAgentAdapter

TOOL = {"type": "function", "function": {"name": "probe", "parameters": {
    "type": "object", "properties": {"value": {"const": "ok"}},
    "required": ["value"], "additionalProperties": False}}}


@pytest.mark.parametrize("structured", [False, True])
def test_native_prompt_keeps_roles_without_double_escaping_structured_transcript(monkeypatch, tmp_path, structured):
    captured = {}
    class RPC:
        def __init__(self, *_):
            self.root, self.process = tmp_path, SimpleNamespace()
            self.pending = deque([
                {"method": "item/completed", "params": {"threadId": "thread", "turnId": "turn",
                    "item": {"type": "agentMessage", "text": "answer"}}},
                {"method": "turn/completed", "params": {"threadId": "thread", "turn": {"id": "turn", "status": "completed"}}},
            ])
        def initialize(self): pass
        def call(self, method, params):
            captured[method] = params
            return {"thread": {"id": "thread"}} if method == "thread/start" else {"turn": {"id": "turn"}}
        def close(self): captured["closed"] = True
    monkeypatch.setattr(codex_session, "CodexRPC", RPC)
    transcript = [{"role": "system", "content": "rules"}, {"role": "tool", "content": 'const s = "中文";\nnext();'}]
    framed = "Trusted proposal framing\n" + json.dumps({"messages": transcript}, ensure_ascii=False)
    messages = [{"role": "user", "content": framed}] if structured else transcript
    schema = {"type": "object"} if structured else None
    result = codex_session._native_chat(CLIAgentAdapter("codex_cli"),
        {"messages": messages, "max_tokens": 512, "reasoning_effort": "high"}, task_id="prompt", timeout_s=1, output_schema=schema)
    wire = captured["turn/start"]
    text = wire["input"][0]["text"]
    assert text == framed if structured else json.loads(text) == {"messages": transcript, "tool_choice": "auto"}
    assert wire.get("outputSchema") == schema
    assert wire["effort"] == "high"
    assert captured["thread/start"]["sandbox"] == "read-only"
    assert captured["thread/start"]["dynamicTools"] == []
    assert captured["thread/start"]["runtimeWorkspaceRoots"] == []
    assert wire["environments"] == [] and captured["closed"]
    assert result["text"] == "answer"


def test_unused_delta_burst_cannot_overflow_rpc_queue_or_hide_execution_requests():
    rpc = object.__new__(codex_session.CodexRPC)
    important = [
        {"id": 1, "result": {}},
        {"id": 2, "method": "item/tool/call", "params": {"tool": "unexpected"}},
        {"method": "item/started", "params": {"item": {"type": "commandExecution"}}},
        {"method": "item/completed", "params": {"item": {"type": "agentMessage", "text": "complete"}}},
        {"method": "thread/tokenUsage/updated", "params": {}},
        {"method": "turn/completed", "params": {"turn": {"status": "completed"}}},
    ]
    events = [{"method": "item/agentMessage/delta", "params": {"delta": "x"}} for _ in range(2048)] + important
    killed = []
    rpc.process = SimpleNamespace(stdout=io.BytesIO(b''.join(json.dumps(e).encode() + b'\n' for e in events)), kill=lambda: killed.append(True))
    rpc.queue = queue.Queue(maxsize=8)
    rpc.closed = threading.Event()
    rpc.failure = ""
    rpc._read()
    assert not rpc.failure and not killed
    assert [rpc.queue.get_nowait() for _ in important] == important
    assert rpc.queue.get_nowait() is None


def test_private_workspace_cleanup_does_not_mask_result_when_windows_file_is_locked(tmp_path, monkeypatch):
    monkeypatch.setattr(codex_session.tempfile, 'gettempdir', lambda: str(tmp_path))
    opened = None
    try:
        with codex_session.private_workspace(copy_auth=False) as (root, _env):
            opened = (root / 'locked.db').open('wb')
            opened.write(b'pending handle')
            opened.flush()
            result = 'completed answer'
        assert result == 'completed answer'
    finally:
        if opened:
            opened.close()


@pytest.mark.parametrize("proposal", [
    {"content": "", "tool_calls": [{"name": "exec_command", "arguments": "{}"}]},
    {"content": "", "tool_calls": []},
    {"content": "", "tool_calls": [], "extra": "untrusted"},
])
def test_invalid_proposal_never_reaches_client(monkeypatch, proposal):
    monkeypatch.setattr(codex_session, "_native_chat", lambda *args, **kwargs: {"text": json.dumps(proposal)})
    with pytest.raises(AdapterError, match="invalid client tool proposal") as rejected:
        CLIAgentAdapter("codex_cli").chat({"messages": [{"role": "user", "content": "hello"}],
                                         "tools": [TOOL], "tool_choice": "required"}, task_id="probe", timeout_s=1)
    assert rejected.value.code == "upstream_response_invalid"


def test_none_choice_cannot_return_a_tool(monkeypatch):
    monkeypatch.setattr(codex_session, "_native_chat", lambda *args, **kwargs: {"text": json.dumps({
        "content": "", "tool_calls": [{"name": "probe", "arguments": '{"value":"ok"}'}]})})
    with pytest.raises(AdapterError):
        CLIAgentAdapter("codex_cli").chat({"messages": [{"role": "user", "content": "hello"}],
                                         "tools": [TOOL], "tool_choice": "none"}, task_id="probe", timeout_s=1)


@pytest.mark.parametrize("age_hours, window, expected", [(0, 500000, 500000), (25, 500000, None), (0, True, None), (0, 999999999, None)])
def test_model_window_cache_is_fresh_bounded_and_cannot_introduce_models(tmp_path, monkeypatch, age_hours, window, expected):
    from datetime import datetime, timedelta, timezone
    monkeypatch.setenv("CODEX_HOME", str(tmp_path))
    (tmp_path / "models_cache.json").write_text(json.dumps({
        "fetched_at": (datetime.now(timezone.utc) - timedelta(hours=age_hours)).isoformat(),
        "identity": "never-export", "models": [{"slug": "known", "context_window": window},
            {"slug": "injected", "context_window": 900000}]}), encoding="utf-8")
    original = [{"id": "known", "name": "Known", "default": True}]
    result = codex_session._model_windows(original)
    assert len(result) == 1 and result[0].get("context_window") == expected
    assert "never-export" not in json.dumps(result)
    assert "context_window" not in original[0]


def test_cli_discovery_exports_bounded_reasoning_metadata_without_upstream_text(tmp_path, monkeypatch):
    class RPC:
        def __init__(self, *_): self.root = tmp_path
        def initialize(self): pass
        def call(self, method, params):
            assert method == "model/list"
            return {"data": [{"model": "known", "displayName": "Known", "isDefault": True,
                "supportedReasoningEfforts": [{"reasoningEffort": "low", "description": "private-upstream-text"},
                    {"reasoningEffort": "ultra"}, {"reasoningEffort": "invented"}], "defaultReasoningEffort": "low"}], "nextCursor": None}
        def close(self): pass
    monkeypatch.setattr(codex_session, "CodexRPC", RPC)
    monkeypatch.setattr(codex_session, "_model_windows", lambda models, cache: models)
    result = codex_session.list_models("fixture")
    assert result == [{"id": "known", "name": "Known", "default": True,
        "supported_reasoning_efforts": ["low", "ultra"], "default_reasoning_effort": "low"}]
    assert "private-upstream-text" not in json.dumps(result)


@pytest.mark.parametrize("age_hours", [0, 25])
def test_reasoning_cache_only_supplements_fresh_known_models(tmp_path, monkeypatch, age_hours):
    from datetime import datetime, timedelta, timezone
    monkeypatch.setenv("CODEX_HOME", str(tmp_path))
    (tmp_path / "models_cache.json").write_text(json.dumps({
        "fetched_at": (datetime.now(timezone.utc) - timedelta(hours=age_hours)).isoformat(),
        "models": [{"slug": "known", "supported_reasoning_levels": [{"effort": "low"}, {"effort": "ultra"}], "default_reasoning_level": "low"},
            {"slug": "injected", "supported_reasoning_levels": [{"effort": "high"}]}]}), encoding="utf-8")
    original = [{"id": "known", "name": "Known", "default": True}]
    result = codex_session._model_windows(original)
    assert len(result) == 1
    assert result[0].get("supported_reasoning_efforts") == (["low", "ultra"] if age_hours == 0 else None)
    assert result[0].get("default_reasoning_effort") == ("low" if age_hours == 0 else None)
    assert "supported_reasoning_efforts" not in original[0]


@pytest.mark.parametrize("arguments", ["not json", '{"value":"bad"}', '{"value":"ok","private-secret":"never-export"}',
                                      {"value": "bad"}, [], 1])
def test_bad_arguments_remain_paired_for_origin_client_feedback(monkeypatch, arguments):
    from rynmesh.agent_runtime.tool_router import normalize_tool_calls
    monkeypatch.setattr(codex_session, "_native_chat", lambda *args, **kwargs: {"text": json.dumps({"content": "", "tool_calls": [{"name": "probe", "arguments": arguments}]})})
    result = CLIAgentAdapter("codex_cli").chat({"messages": [{"role": "user", "content": "hello"}], "tools": [TOOL]}, task_id="feedback", timeout_s=1)
    raw = result["message"]["tool_calls"]
    calls = normalize_tool_calls(raw, [TOOL])
    assert len(calls) == 1 and calls[0]["call_id"] == raw[0]["id"]
    error = calls[0]["error_result"]
    assert error["error"] == "agent_tool_arguments_invalid" and error["not_executed"]
    assert "never-export" not in json.dumps(error) and "private-secret" not in json.dumps(error)
