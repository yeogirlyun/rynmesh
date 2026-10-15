"""Budget stop causes survive real bridge/worker boundaries and durable accounting."""
import json
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from test_agent_runtime import assistant, make_runtime
from test_agent_runtime_verification import VerifyingTools

from rynmesh.agent_runtime.bridge import AgentModelBridge
from rynmesh.agent_runtime.store import AgentModelResponseError
from rynmesh.llm_package.adapters import AdapterError


def peer_bridge(execute):
    return AgentModelBridge({"execute_order": execute, "cancel_order": lambda _: None},
        SimpleNamespace(network="test", resolve=lambda _: {
            "source": "peer", "peer_id": "provider", "service_id": "service"}))


@pytest.mark.parametrize("reason", ["agent_time_limit", "agent_time_budget_exhausted",
    "agent_permission_denied", "agent_owner_changed", "agent_cancelled", "PRIVATE_CALLBACK_TEXT", {}])
@pytest.mark.parametrize("path", ["before", "response", "adapter", "http"])
def test_bridge_stop_reason_is_safe_and_usage_is_retained(reason, path):
    calls = []
    stopped = [path == "before"]

    def cancelled():
        return stopped[0]
    cancelled.reason = reason

    async def execute(request):
        calls.append(request)
        stopped[0] = True
        if path == "adapter":
            exc = AdapterError("PRIVATE_TRANSPORT_TEXT")
            exc.reported_usage = {"input_tokens": 7, "output_tokens": 3}
            exc.finish_reason = "stop"
            raise exc
        if path == "http":
            raise HTTPException(503, "PRIVATE_TRANSPORT_TEXT")
        return {"state": "cancelled", "error_code": "consumer_cancelled",
            "reported_usage": {"input_tokens": 7, "output_tokens": 3},
            "message": {"role": "assistant", "content": "PRIVATE_LATE_RESPONSE"}, "finish_reason": "stop"}

    expected = reason if isinstance(reason, str) and reason in {"agent_time_limit", "agent_time_budget_exhausted"} else "agent_cancelled"
    with pytest.raises(ValueError) as caught:
        peer_bridge(execute).complete("test", [{"role": "user", "content": "Inspect"}], [], cancelled)
    assert str(caught.value) == expected
    assert len(calls) == (0 if path == "before" else 1)
    if path in {"response", "adapter"}:
        assert isinstance(caught.value, AgentModelResponseError)
        assert caught.value.usage == {"input_tokens": 7, "output_tokens": 3}
        assert caught.value.finish_reason == "stop"
    assert "PRIVATE" not in str(caught.value) + json.dumps(vars(caught.value))


@pytest.mark.parametrize("stage", ["main", "verification"])
@pytest.mark.parametrize("stop", ["round", "lifetime", "owner"])
def test_worker_records_stop_cause_without_verifying_or_repeating(tmp_path, monkeypatch, stage, stop):
    runtime, _, tools, _ = make_runtime(tmp_path)
    runtime.update_settings({"max_seconds": 60, "verify_text_responses": stage == "verification"})
    task = runtime.create({"message": "Inspect", "budget_limits": {"max_total_seconds": 30 if stop == "lifetime" else 3600}})
    if stage == "verification":
        runtime.tools = tools = VerifyingTools()
        runtime.model.complete = lambda *args: assistant("Provisional answer")
        runtime.run_once()
        assert runtime.store.read()["tasks"][task["id"]].get("verification")

    clock = [1000.0]
    monkeypatch.setattr("rynmesh.agent_runtime.runtime.time", SimpleNamespace(monotonic=lambda: clock[0]))
    monkeypatch.setattr("rynmesh.agent_runtime.bridge.time", SimpleNamespace(monotonic=lambda: clock[0]))
    requests = []

    async def execute(request):
        requests.append(request)
        if stop == "owner":
            runtime.action(task["id"], {"action": "cancel"})
        else:
            clock[0] += 61 if stop == "round" else 31
        return {"state": "cancelled", "error_code": "consumer_cancelled",
            "reported_usage": {"input_tokens": 7, "output_tokens": 3},
            "message": {"role": "assistant", "content": "PRIVATE_LATE_RESPONSE"}, "finish_reason": "stop"}

    runtime.model = peer_bridge(execute)
    runtime.run_once()
    row = runtime.store.read()["tasks"][task["id"]]
    expected = {"round": "agent_time_limit", "lifetime": "agent_time_budget_exhausted", "owner": "agent_cancelled"}[stop]
    rejected = [e for e in row["events"] if e["type"] == "model_response_rejected"]
    assert rejected[-1]["error"] == expected
    assert rejected[-1]["usage"] == {"input_tokens": 7, "output_tokens": 3}
    record = row["budget"]["records"][-1]
    assert record["charged_tokens"] == 10
    assert row["outcome"]["status"] != "verified"
    if stop == "owner":
        assert row["status"] == "cancelled"
    elif stage == "verification":
        assert expected in row["outcome"]["uncertainties"]
    else:
        assert row["error"] == expected and row["status"] == "failed"
    runtime.run_once()
    assert len(requests) == 1 and tools.calls == []
    assert "PRIVATE_LATE_RESPONSE" not in json.dumps(runtime.get(task["id"]))
