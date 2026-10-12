"""Unknown provider counters must not become free or complete Agent usage."""

import json
from contextlib import contextmanager
from types import SimpleNamespace

import pytest

from rynmesh.agent_runtime import budget
from rynmesh.agent_runtime.bridge import AgentModelBridge
from rynmesh.llm_package import antigravity_session as session
from rynmesh.llm_package.cli_adapter import CLIAgentAdapter


@pytest.mark.parametrize(
    "usage,expected",
    [
        (None, {"input_tokens": None, "output_tokens": None}),
        (["malformed"], {"input_tokens": None, "output_tokens": None}),
        (
            {"input_tokens": True, "output_tokens": -1},
            {"input_tokens": None, "output_tokens": None},
        ),
        (
            {"input_tokens": "7", "output_tokens": 2.5},
            {"input_tokens": None, "output_tokens": None},
        ),
        ({"input_tokens": 0, "output_tokens": 0}, {"input_tokens": 0, "output_tokens": 0}),
        ({"input_tokens": 7}, {"input_tokens": 7, "output_tokens": None}),
    ],
)
def test_usage_preserved_through_real_parser_bridge_and_budget(
    tmp_path, monkeypatch, usage, expected
):
    @contextmanager
    def workspace(**kwargs):
        yield tmp_path, {}

    monkeypatch.setattr(session, "private_workspace", workspace)
    monkeypatch.setattr(session, "prepare_workspace", lambda *_: None)
    monkeypatch.setattr(session, "spawn", lambda *_args, **_kwargs: SimpleNamespace(returncode=0))
    monkeypatch.setattr(session, "terminate", lambda *_: None)
    monkeypatch.setattr(
        session,
        "communicate_bounded",
        lambda *_args, **_kwargs: json.dumps(
            {"status": "SUCCESS", "response": "Nonempty answer", "usage": usage}
        ),
    )
    adapter = CLIAgentAdapter("antigravity_cli", executable="synthetic")
    monkeypatch.setattr(adapter, "_command_path", lambda: "synthetic")
    result = session.infer(adapter, "Nonempty prompt", task_id="review", timeout_s=1, max_tokens=32)
    actual, _ = AgentModelBridge._response_metadata(result, {"source": "local_service"})
    assert actual == expected
    row = {"limits": {"max_seconds": 180}, "events": [], "steps": 0, "elapsed_seconds": 0}
    reservation = budget.reserve(row, "model", "main", input_bound=200, output_bound=32)
    budget.settle(row, reservation["id"], usage=actual, elapsed_seconds=0.01)
    total = budget.public(row)["lifetime"]
    assert total["charged_tokens"] == sum(
        expected[k] if expected[k] is not None else bound
        for k, bound in (("input_tokens", 200), ("output_tokens", 32))
    )
    assert total["usage_complete"] == all(v is not None for v in expected.values())


def test_buffered_stream_keeps_unknown_usage_without_arithmetic_failure(monkeypatch):
    monkeypatch.setattr(
        session,
        "chat",
        lambda *_args, **_kwargs: {
            "message": {"role": "assistant", "content": "Answer"},
            "finish_reason": "stop",
            "input_tokens": None,
            "output_tokens": None,
        },
    )
    events = []
    result = CLIAgentAdapter("antigravity_cli").chat(
        {"messages": [{"role": "user", "content": "Synthetic"}], "stream": True},
        task_id="review",
        timeout_s=1,
        on_event=events.append,
    )
    assert result["input_tokens"] is None
    assert events[-1]["usage"] == {
        "prompt_tokens": None,
        "completion_tokens": None,
        "total_tokens": None,
    }
