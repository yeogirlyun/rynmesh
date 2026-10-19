"""Configured generation, private continuation and accounted rejection bounds."""
import copy
import json

import pytest
from test_agent_integrations import add_source, fake_upstream, node  # noqa: F401
from test_agent_runtime import advance, assistant, make_runtime

from rynmesh.agent_runtime import output_policy
from rynmesh.agent_runtime.bridge import AgentModelBridge, build_agent_integrations
from rynmesh.agent_runtime.runtime import AgentRuntime
from rynmesh.agent_runtime.store import AgentError, AgentModelResponseError, AgentStore, encoded


@pytest.mark.parametrize("configured,window,request_cap,expected,reason", [
    (128, 1000000, None, 128, None), (128000, 1000000, None, 128000, None),
    (256000, 1000000, None, 131072, "transport"), (128000, 1000000, 9000, 9000, "request"),
    (128000, 1024, None, 510, "context"),
])
def test_shared_policy_clamps(configured, window, request_cap, expected, reason):
    result = output_policy.select({"max_output_tokens": configured, "context_window": window}, [], [], request_cap=request_cap)
    assert result["effective_cap"] == expected
    assert result["configured_cap"] == configured
    if reason:
        assert reason in result["clamp_reasons"]


def test_window_smaller_than_framing_rejects_even_an_empty_request():
    with pytest.raises(AgentError, match="agent_context_limit"):
        output_policy.select({"max_output_tokens": 128000, "context_window": 100}, [], [])


@pytest.mark.parametrize("cap", [0, -1, True, "128000"])
def test_invalid_explicit_request_fails_closed(cap):
    with pytest.raises(AgentError, match="agent_token_limit"):
        output_policy.select({}, [], [], request_cap=cap)


@pytest.mark.parametrize("configured,requested", [(128, None), (128000, None), (256000, None), (128000, 7000)])
def test_real_bridge_and_outer_dispatch_gate_use_configured_bound(node, monkeypatch, configured, requested):  # noqa: F811
    app, store = node
    source, model_id = add_source(app)
    sources = app.state.llm_agent_services["source_store"]
    source.update(context_window=1000000, max_output_tokens=configured)
    # Generation changes invalidate the probe evidence; tests explicitly supply
    # the synthetic verified result before rebuilding the provider.
    sources.save(source)
    source["status"], source["tool_support"] = "ready", {"model-a": "supported"}
    sources.save(source)
    bridge, _, _ = build_agent_integrations(app, store, store.home)
    requests = fake_upstream(monkeypatch, [{"role": "assistant", "content": "ok"}], json_only=True)
    result = bridge.complete(model_id, [{"role": "user", "content": "test"}], [], lambda: False, max_output_tokens=requested)
    assert requests[0]["max_tokens"] == min(configured, 131072, requested or configured)
    assert result["output_policy"]["effective_cap"] == requests[0]["max_tokens"]


def test_lifetime_reduction_is_reserved_recorded_and_settled(tmp_path):
    runtime, model, _, _ = make_runtime(tmp_path)
    runtime.catalog.list_models = lambda: [{"id": "local:test", "available": True, "agent_supported": True,
        "max_output_tokens": 128000, "context_window": 1000000}]
    model.supports_output_token_limit = True
    seen = []
    def complete(*args, **kwargs):
        seen.append(kwargs["max_output_tokens"])
        return {**assistant("done"), "usage": {"input_tokens": 13, "output_tokens": 7}}
    model.complete = complete
    row = runtime.create({"message": "Explain", "budget_limits": {"max_total_tokens": 20000}})
    advance(runtime)
    assert 0 < seen[0] < 20000
    stored = runtime.store.read()["tasks"][row["id"]]
    record = stored["budget"]["records"][0]
    assert record["output_reservation"] == seen[0]
    assert record["output_policy"]["configured_cap"] == 128000
    assert "lifetime_budget" in record["output_policy"]["clamp_reasons"]
    assert record["charged_tokens"] == 20


def test_private_continuation_exceeds_visible_limit_survives_restart_without_public_leak(tmp_path):
    response = assistant(name="inspect_service", args={"service_id": "svc"})
    private = "PRIVATE_CONTINUATION_" * 1200
    response["message"]["reasoning_content"] = private
    runtime, model, tools, key = make_runtime(tmp_path, response, assistant("done"))
    runtime.update_settings({"max_output_chars": 1000})
    task = runtime.create({"message": "Inspect"})
    reservations = []
    original = model.complete
    def tracked(*args, **kwargs):
        reservations.append(copy.deepcopy(runtime.store.read()["storage_reservations"][task["id"]]))
        return original(*args, **kwargs)
    model.complete = tracked
    advance(runtime)
    assert reservations[0]["bytes"] >= output_policy.MAX_RAW_RESPONSE_BYTES + 8000
    assert runtime.store.read()["tasks"][task["id"]]["messages"][-1]["reasoning_content"] == private
    restored = AgentRuntime(AgentStore(tmp_path, key), model, tools, runtime.catalog)
    restored.reconcile()
    assert private not in json.dumps(restored.get(task["id"]))
    advance(restored, 2)
    assert any(m.get("reasoning_content") == private for m in model.calls[-1][1])
    assert restored.get(task["id"])["status"] == "completed"
    assert private not in (tmp_path / "sessions.json").read_text()


@pytest.mark.parametrize("bad", ["overflow", "role", "calls_type", "call_identity", "reasoning_type"])
def test_bridge_structural_rejections_retain_only_safe_accounting(bad):
    result = {"message": {"role": "assistant", "content": "ok"}, "reported_usage": {"input_tokens": 11, "output_tokens": 8000},
              "finish_reason": "length"}
    if bad == "overflow":
        result["message"]["reasoning_content"] = "PRIVATE" * 40000
    elif bad == "role":
        result["message"]["role"] = "user"
    elif bad == "calls_type":
        result["message"]["tool_calls"] = "PRIVATE"
    elif bad == "call_identity":
        result["message"]["tool_calls"] = [{"id": "", "function": {"name": "unknown", "arguments": "PRIVATE"}}]
    else:
        result["message"]["reasoning_content"] = {"private": True}
    with pytest.raises(AgentModelResponseError) as error:
        AgentModelBridge._response(result, {"source": "local_api"}, [], {})
    assert error.value.usage == {"input_tokens": 11, "output_tokens": 8000}
    assert error.value.finish_reason == "length" and "PRIVATE" not in str(error.value)
    if bad == "overflow":
        assert str(error.value) == "agent_model_output_limit"


@pytest.mark.parametrize("bad", ["overflow", "malformed"])
def test_received_rejection_is_charged_in_actual_runtime(tmp_path, bad):
    runtime, model, _, _ = make_runtime(tmp_path)
    def rejected(*args):
        raw = {"message": {"role": "assistant", "content": "ok" if bad == "overflow" else None},
               "reported_usage": {"input_tokens": 31, "output_tokens": 7000}, "finish_reason": "length"}
        if bad == "overflow":
            raw["message"]["reasoning_content"] = "x" * (output_policy.MAX_RAW_RESPONSE_BYTES + 1)
        return AgentModelBridge._response(raw, {"source": "local_api"}, [], {})
    model.complete = rejected
    task = runtime.create({"message": "Explain"})
    advance(runtime)
    stored = runtime.store.read()["tasks"][task["id"]]
    assert stored["budget"]["records"][0]["charged_tokens"] == 7031
    rejected_event = next(e for e in stored["events"] if e["type"] == "model_response_rejected")
    assert rejected_event["usage"]["output_tokens"] == 7000
    assert not any("reasoning_content" in encoded(e).decode() for e in stored["events"])


@pytest.mark.parametrize("finish", [[], {}, 5])
def test_malformed_finish_metadata_never_loses_received_usage(finish):
    raw = {"message": {"role": "assistant", "content": None}, "finish_reason": finish,
           "reported_usage": {"input_tokens": 3, "output_tokens": 5}}
    with pytest.raises(AgentModelResponseError) as error:
        AgentModelBridge._response(raw, {"source": "local_api"}, [], {})
    assert error.value.finish_reason == "unknown" and error.value.usage["output_tokens"] == 5
    raw["message"]["content"] = "ok"
    result = AgentModelBridge._response(raw, {"source": "local_api"}, [], {})
    assert result["finish_reason"] == "unknown" and result["usage"]["output_tokens"] == 5


def test_checker_private_reasoning_does_not_consume_visible_answer_limit(tmp_path):
    from test_agent_runtime import approve
    from test_agent_runtime_verification import assessment, verified_runtime

    runtime, model, _, _ = verified_runtime(tmp_path, assistant(name="share_service", args={"service_id": "A"}), assistant("A shared"), assessment())
    complete = model.complete
    private = "CHECKER_PRIVATE" * 2000
    def with_reasoning(*args):
        result = complete(*args)
        if not args[2]:
            result["message"]["reasoning_content"] = private
        return result
    model.complete = with_reasoning
    runtime.update_settings({"max_output_chars": 1000})
    task = runtime.create({"message": "Share A"})
    advance(runtime, 2)
    approve(runtime, task["id"])
    advance(runtime, 4)
    assert runtime.get(task["id"])["outcome"]["status"] == "verified"
    assert private not in encoded(runtime.store.read()).decode()
