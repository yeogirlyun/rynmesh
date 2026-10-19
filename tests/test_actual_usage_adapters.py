"""Actual usage must remain distinct from estimates across adapter entry points.

All HTTP responses and Claude processes are in-memory fakes; no service is used.
"""
from __future__ import annotations

import copy
import io
import json
from contextlib import contextmanager
from email.message import Message

import pytest

from rynmesh.llm_package import adapters, cli_adapter, codex_session
from rynmesh.llm_package.adapters import OpenAICompatibleAdapter, actual_usage
from rynmesh.llm_package.cli_adapter import CLIAgentAdapter

UNKNOWN = {"input_tokens": None, "output_tokens": None}
MISSING = object()


@pytest.mark.parametrize("marker,expected", [
    (None, UNKNOWN),
    (False, UNKNOWN),
    (0, UNKNOWN),
    ([], UNKNOWN),
    ("invalid", UNKNOWN),
    ({}, UNKNOWN),
    ({"input_tokens": None, "output_tokens": None}, UNKNOWN),
    ({"input_tokens": 7}, {"input_tokens": 7, "output_tokens": None}),
    ({"output_tokens": 3}, {"input_tokens": None, "output_tokens": 3}),
    ({"input_tokens": 0, "output_tokens": 0}, {"input_tokens": 0, "output_tokens": 0}),
    ({"input_tokens": True, "output_tokens": -1}, UNKNOWN),
    ({"input_tokens": float("nan"), "output_tokens": "12"}, UNKNOWN),
])
def test_explicit_reported_usage_wins_by_presence_without_top_level_fallback(marker, expected):
    result = {"input_tokens": 120, "output_tokens": 80, "reported_usage": marker}
    assert actual_usage(result) == expected


@pytest.mark.parametrize("result,expected", [
    (None, UNKNOWN),
    ({}, UNKNOWN),
    ({"input_tokens": 120, "output_tokens": 80}, {"input_tokens": 120, "output_tokens": 80}),
    ({"input_tokens": 0, "output_tokens": 0}, {"input_tokens": 0, "output_tokens": 0}),
    ({"input_tokens": 7}, {"input_tokens": 7, "output_tokens": None}),
    ({"input_tokens": True, "output_tokens": False}, UNKNOWN),
    ({"input_tokens": -1, "output_tokens": float("nan")}, UNKNOWN),
    ({"input_tokens": 1.0, "output_tokens": "2"}, UNKNOWN),
])
def test_legacy_top_level_usage_accepts_only_typed_nonnegative_actual_counters(result, expected):
    assert actual_usage(result) == expected


USAGE_CASES = [
    pytest.param(MISSING, UNKNOWN, id="absent"),
    pytest.param(None, UNKNOWN, id="null"),
    pytest.param({}, UNKNOWN, id="empty"),
    pytest.param({"prompt_tokens": 0, "completion_tokens": 0},
                 {"input_tokens": 0, "output_tokens": 0}, id="reported-zero"),
    pytest.param({"prompt_tokens": 7}, {"input_tokens": 7, "output_tokens": None}, id="input-only"),
    pytest.param({"completion_tokens": 3}, {"input_tokens": None, "output_tokens": 3}, id="output-only"),
    pytest.param({"prompt_tokens": True, "completion_tokens": -1}, UNKNOWN, id="boolean-negative"),
    pytest.param({"prompt_tokens": float("nan"), "completion_tokens": "3"}, UNKNOWN, id="nan-string"),
    pytest.param({"prompt_tokens": 1.0, "completion_tokens": False}, UNKNOWN, id="float-boolean"),
]


class _Response(io.BytesIO):
    def __init__(self, body, content_type):
        super().__init__(body)
        self.headers = Message()
        self.headers["Content-Type"] = content_type
        self.status = 200

    def getcode(self):
        return self.status


def _sse(usage):
    chunks = [
        {"choices": [{"index": 0, "delta": {"role": "assistant", "content": "fixture answer"},
                      "finish_reason": None}]},
        {"choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]},
    ]
    if usage is not MISSING:
        chunks.append({"choices": [], "usage": usage})
    return ("".join("data: " + json.dumps(chunk) + "\n\n" for chunk in chunks)
            + "data: [DONE]\n\n").encode()


@pytest.mark.parametrize("entry", ["infer", "chat-json", "chat-sse", "infer-stream"])
@pytest.mark.parametrize("usage,expected", USAGE_CASES)
def test_openai_adapter_entry_points_keep_unknown_zero_partial_and_invalid_usage(
        monkeypatch, entry, usage, expected):
    adapter = OpenAICompatibleAdapter(base_url="http://127.0.0.1:1", model="fixture-only")
    payload = {"choices": [{"message": {"role": "assistant", "content": "fixture answer"},
                            "finish_reason": "stop"}]}
    if usage is not MISSING:
        payload["usage"] = usage
    requests = []

    def fake_json(path, body, timeout_s, **kwargs):
        requests.append(path)
        return copy.deepcopy(payload)

    def fake_urlopen(request, **kwargs):
        requests.append(request.full_url)
        assert request.full_url == "http://127.0.0.1:1/v1/chat/completions"
        return _Response(_sse(usage), "text/event-stream")

    monkeypatch.setattr(adapter, "_json", fake_json)
    monkeypatch.setattr(adapters.urllib.request, "urlopen", fake_urlopen)
    emitted = []
    if entry == "infer":
        result = adapter.infer(prompt="fixture request", max_tokens=16, task_id="usage-infer", timeout_s=1)
    elif entry == "infer-stream":
        result = adapter.infer_stream(prompt="fixture request", max_tokens=16, task_id="usage-infer-stream",
                                      timeout_s=1, on_delta=emitted.append)
        assert "".join(emitted) == "fixture answer"
    else:
        result = adapter.chat({"messages": [{"role": "user", "content": "fixture request"}],
                               "max_tokens": 16, "stream": entry == "chat-sse"},
                              task_id="usage-chat", timeout_s=1, on_event=emitted.append)
    assert result["text"] == "fixture answer"
    assert {key: result[key] for key in UNKNOWN} == expected
    assert result["reported_usage"] == expected
    assert actual_usage(result) == expected
    assert len(requests) == 1  # Unknown usage must not cause another inference.


@pytest.mark.parametrize("usage,expected", USAGE_CASES)
def test_claude_stdout_decoder_does_not_invent_missing_or_invalid_usage(
        tmp_path, monkeypatch, usage, expected):
    raw = {"result": "fixture answer"}
    if usage is not MISSING:
        raw["usage"] = ({"input_tokens": usage.get("prompt_tokens"),
                         "output_tokens": usage.get("completion_tokens")}
                        if isinstance(usage, dict) else usage)
    output = json.dumps(raw).encode()
    created = []

    @contextmanager
    def private_workspace(*, copy_auth):
        assert copy_auth is False
        root = tmp_path / "isolated"
        root.mkdir()
        yield root, {}

    class FakeProcess:
        returncode = 0

        def kill(self):
            pytest.fail("this decoder fixture was not cancelled")

    def fake_spawn(args, **kwargs):
        created.append((args, kwargs))
        return FakeProcess()

    def fake_communicate(process, data, **kwargs):
        assert b"fixture request" in data
        return output

    # Every authentication lookup points at an empty temporary fixture location.
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "no-credentials"))
    for name in ("ANTHROPIC_API_KEY", "CLAUDE_CODE_OAUTH_TOKEN", "OPENAI_API_KEY"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(codex_session, "private_workspace", private_workspace)
    monkeypatch.setattr(CLIAgentAdapter, "_command_path", lambda self: "fixture-claude")
    monkeypatch.setattr(cli_adapter, "spawn", fake_spawn)
    monkeypatch.setattr(cli_adapter, "communicate_bounded", fake_communicate)
    result = CLIAgentAdapter("claude_cli").infer(
        prompt="fixture request", max_tokens=16, task_id="usage-claude", timeout_s=1)
    assert result["text"] == "fixture answer"
    assert {key: result[key] for key in UNKNOWN} == expected
    assert result["reported_usage"] == expected
    assert len(created) == 1
