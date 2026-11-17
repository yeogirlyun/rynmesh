"""S01-I5 context overflow classification, routing and one-shot compaction recovery."""
import io
import json
from types import SimpleNamespace
from urllib.error import HTTPError

import pytest
from fastapi import HTTPException
from test_agent_context_compaction_policy import WindowCatalog, exchange, is_compaction_call, seed
from test_agent_integrations import add_source, node  # noqa: F401
from test_agent_runtime import advance, assistant, make_runtime
from test_native_protocol_transport import endpoint

from rynmesh.agent_runtime import budget, context_compaction, retry_policy
from rynmesh.agent_runtime.bridge import AgentModelBridge, build_agent_integrations
from rynmesh.agent_runtime.diagnostic_log import safe_diagnostics
from rynmesh.agent_runtime.runtime import AgentRuntime
from rynmesh.agent_runtime.store import AgentError, AgentStore
from rynmesh.llm_package.adapters import AdapterError, OpenAICompatibleAdapter
from rynmesh.llm_package.protocol_adapter import NativeProtocolAdapter
from rynmesh.llm_package.transport_failure import classify_stream_error, context_overflow, read_http_failure


ENVELOPES = [
    {"error": {"code": "context_length_exceeded", "message": "PRIVATE"}},
    {"error": {"type": "invalid_request_error", "message": "prompt is too long PRIVATE"}},
    {"error": {"message": "maximum context length is 32768 tokens PRIVATE"}},
    {"error": {"type": "context_window_exceeded", "message": "PRIVATE"}},
    {"error": {"message": "input is too long PRIVATE"}},
    {"error": {"message": "too many tokens PRIVATE"}},
    {"error": {"message": "estimated context window exceeded PRIVATE"}},
    {"error": {"code": "context_length_exceeded", "param": "messages[0].content[0].image_url", "message": "PRIVATE"}},
    {"error": {"code": "ContextWindowExceeded", "message": "PRIVATE"}},
]


def http_error(body, status=400):
    return HTTPError("https://private.test/secret", status, "PRIVATE", {}, io.BytesIO(json.dumps(body).encode()))


@pytest.mark.parametrize("body", ENVELOPES)
def test_t1_vendor_envelopes_and_streams_are_body_free(body):
    diag = read_http_failure(http_error(body))
    assert diag["network_reason"] == "context_overflow"
    assert "PRIVATE" not in json.dumps(diag) and "private.test" not in json.dumps(diag)
    assert safe_diagnostics(diag)["network_reason"] == "context_overflow"
    code, stream_diag = classify_stream_error(body)
    assert code == "upstream_context_overflow"
    assert stream_diag["network_reason"] == "context_overflow"
    assert "PRIVATE" not in json.dumps(stream_diag)


def test_t1_http_413_even_without_body_and_other_failures_remain_distinct():
    assert read_http_failure(http_error({}, 413))["network_reason"] == "context_overflow"
    for code in ("image_too_large", "insufficient_quota"):
        assert read_http_failure(http_error({"error": {"code": code}}, 413))["network_reason"] == "context_overflow"
    assert read_http_failure(http_error(ENVELOPES[0], 500))["network_reason"] == "http"
    assert context_overflow(ENVELOPES[0], status=500) is False
    assert read_http_failure(http_error({"error": {"message": "ordinary invalid parameter"}}))["network_reason"] != "context_overflow"
    assert retry_policy.limit("agent_model_context_overflow", {"network_reason": "context_overflow"}) == 1


@pytest.mark.parametrize("route", ["json", "json_unclassified", "chat", "openai_responses", "anthropic_messages"])
@pytest.mark.parametrize("status", [400, 413])
def test_http_adapters_raise_shared_overflow_code(monkeypatch, route, status):
    def reject(*a, **kw):
        raise http_error(ENVELOPES[0] if status == 400 else {"error": {"code": "image_too_large"}}, status)
    monkeypatch.setattr("urllib.request.urlopen", reject)
    adapter = (NativeProtocolAdapter(protocol=route, base_url="http://127.0.0.1:1234", model="demo")
               if route in {"openai_responses", "anthropic_messages"}
               else OpenAICompatibleAdapter(base_url="http://127.0.0.1:1234", model="demo"))
    with pytest.raises(AdapterError) as error:
        if route.startswith("json"):
            adapter._json("/v1/chat", {"messages": []}, 10, classify_errors=route == "json")
        else:
            adapter.chat({"messages": [{"role": "user", "content": "hello"}], "stream": False}, task_id="overflow", timeout_s=10)
    assert error.value.code == "upstream_context_overflow"
    assert error.value.diagnostics["network_reason"] == "context_overflow"
    assert "PRIVATE" not in str(error.value)


@pytest.mark.parametrize("protocol", ["chat", "openai_responses", "anthropic_messages"])
def test_real_http_stream_error_uses_same_overflow_code(protocol):
    envelope = ({"type": "response.failed", "response": {"status": "failed", "error": ENVELOPES[0]["error"]}}
                if protocol == "openai_responses" else
                {"type": "error", "error": {"type": "invalid_request_error", "message": "prompt is too long PRIVATE"}})
    wire = "event: " + envelope["type"] + "\ndata: " + json.dumps(envelope) + "\n\n"
    with endpoint([("text/event-stream", wire)]) as (url, requests):
        adapter = (OpenAICompatibleAdapter(base_url=url, api_prefix="", model="demo") if protocol == "chat" else
                   NativeProtocolAdapter(protocol=protocol, base_url=url, api_prefix="", model="demo"))
        with pytest.raises(AdapterError) as error:
            adapter.chat({"messages": [{"role": "user", "content": "hello"}], "stream": True}, task_id="stream-overflow", timeout_s=5)
        assert error.value.code == "upstream_context_overflow"
        assert error.value.diagnostics["network_reason"] == "context_overflow"
        assert len(requests) == 1


@pytest.mark.parametrize("source_code", ["upstream_context_overflow", "peer_request_too_large", "estimated_context_window_exceeded"])
def test_bridge_preserves_shared_source_code(node, monkeypatch, source_code):  # noqa: F811
    app, store = node
    _, model_id = add_source(app)
    bridge, _, catalog = build_agent_integrations(app, store, store.home)
    target = catalog.get_model(model_id)
    manager = bridge.services["provider_for"](target["service_id"])
    def reject(*a, **kw):
        if source_code != "upstream_context_overflow":
            raise HTTPException(400, source_code)
        raise AdapterError("PRIVATE", code=source_code, diagnostics={"network_reason": "context_overflow"})
    monkeypatch.setattr(manager.adapter, "chat", reject)
    with pytest.raises(AgentError, match="^agent_model_context_overflow$"):
        bridge.complete(model_id, [{"role": "user", "content": "hello"}], [], lambda: False)


@pytest.mark.parametrize("code", ["upstream_context_overflow", "peer_request_too_large", "estimated_context_window_exceeded"])
def test_failed_peer_result_reaches_same_runtime_error(code):
    target = dict(id="peer/test/demo", source="peer", service_id="demo", peer_id="provider",
                  model_id="demo", adapter="openai_compatible", available=True, agent_supported=True)
    requests = []
    async def execute_order(request):
        requests.append(request)
        return {"state": "failed", "error_code": code}
    bridge = AgentModelBridge({"execute_order": execute_order},
        SimpleNamespace(resolve=lambda _: target, network="test"))
    with pytest.raises(AgentError, match="^agent_model_context_overflow$"):
        bridge.complete(target["id"], [{"role": "user", "content": "hello"}], [], lambda: False)
    assert requests[0]["provider_peer_id"] == "provider"


def test_codex_cli_preserves_shared_overflow_code_from_its_session(monkeypatch):
    from rynmesh.llm_package.cli_adapter import CLIAgentAdapter
    def reject(*a, **kw):
        raise AdapterError("upstream_context_overflow", code="upstream_context_overflow",
                           diagnostics={"network_reason": "context_overflow"})
    monkeypatch.setattr("rynmesh.llm_package.codex_session.chat", reject)
    with pytest.raises(AdapterError) as error:
        CLIAgentAdapter("codex_cli").chat({"messages": [{"role": "user", "content": "hello"}], "max_tokens": 64},
            task_id="codex-overflow", timeout_s=5)
    assert error.value.code == "upstream_context_overflow"
    assert error.value.diagnostics["network_reason"] == "context_overflow"


def runtime_with_overflow(tmp_path, *, overflows=1, tool_then_calibrate=False):
    runtime, model, _, key = make_runtime(tmp_path)
    runtime.catalog = WindowCatalog(64000, 4096)
    task_id = runtime.create({"message": "Inspect the task history and continue."})["id"]
    for i in range(10):
        seed(runtime, task_id, exchange(f"old-{i}", "observation " + "x" * 5000))
    main_count = [0]
    def complete(model_id, messages, specs, cancelled, on_event):
        model.calls.append((model_id, messages, specs))
        if is_compaction_call(messages, specs):
            return assistant("Goal: inspect. Findings: earlier observations retained. Next: continue.")
        main_count[0] += 1
        if main_count[0] <= overflows:
            error = AgentError("agent_model_context_overflow")
            error.diagnostics = {"network_reason": "context_overflow"}
            raise error
        if tool_then_calibrate and main_count[0] == 2:
            return assistant(name="inspect_service", args={"service_id": "svc1"})
        out = assistant("Inspection finished.")
        if tool_then_calibrate:
            size = runtime._request_context_bytes(runtime.store.read()["tasks"][task_id], messages, specs)
            out["usage"] = {"input_tokens": size // 2, "output_tokens": 5}
        return out
    model.complete = complete
    return runtime, model, task_id, main_count, key


def test_t2_first_overflow_tightens_fifteen_percent_forces_compaction_and_recovers(tmp_path):
    runtime, model, task_id, mains, _ = runtime_with_overflow(tmp_path)
    advance(runtime)
    row = runtime.store.read()["tasks"][task_id]
    assert row["status"] == "queued" and row.get("model_retry_count", 0) == 0
    marker = row["context_overflow"]
    assert marker["step"] == 1 and marker["request_bytes"] == row["context_usage"]["request_bytes"]
    assert runtime._effective_capacity(row)[0] == pytest.approx(int(marker["request_bytes"] * .85), abs=1)
    advance(runtime)
    row = runtime.store.read()["tasks"][task_id]
    assert row["status"] == "completed" and mains[0] == 2
    assert row.get("model_retry_count", 0) == 0 and "context_overflow" not in row
    starts = [e for e in row["events"] if e["type"] == "context_compaction_started"]
    assert starts and starts[0]["reason"] == "upstream_overflow"
    assert any(is_compaction_call(messages, specs) for _, messages, specs in model.calls)
    assert not any(e["type"] == "connection_paused" for e in row["events"])


def test_t3_second_overflow_fails_context_limit_without_connection_pause(tmp_path):
    runtime, _, task_id, mains, _ = runtime_with_overflow(tmp_path, overflows=2)
    advance(runtime, 2)
    row = runtime.store.read()["tasks"][task_id]
    assert row["status"] == "failed" and row["error"] == "agent_context_limit"
    assert mains[0] == 2 and row.get("model_retry_count", 0) == 0
    assert not any(e["type"] == "connection_paused" for e in row["events"])


def test_t4_penalty_survives_compaction_next_step_and_clears_on_calibration(tmp_path):
    runtime, _, task_id, _, _ = runtime_with_overflow(tmp_path, tool_then_calibrate=True)
    advance(runtime, 2)
    row = runtime.store.read()["tasks"][task_id]
    penalty = row["context_density_penalty"]
    assert 0 < penalty < 1 and "context_overflow" not in row
    unpenalized = {**row, "context_density_penalty": 1}
    assert runtime._effective_capacity(row)[0] == pytest.approx(runtime._effective_capacity(unpenalized)[0] * penalty, abs=1)
    advance(runtime, 2)  # dispatch read, then a main request with actual token usage
    row = runtime.store.read()["tasks"][task_id]
    assert row["status"] == "completed"
    assert "context_density_penalty" not in row
    assert context_compaction.observed_bytes_per_token(row) is not None


@pytest.mark.parametrize("summary_failures", [1, 2])
def test_summary_overflow_reuses_existing_menu_release_path(tmp_path, summary_failures):
    from test_agent_tool_menu import ManyTools
    runtime, model, task_id, mains, _ = runtime_with_overflow(tmp_path)
    runtime.tools = ManyTools()
    original = model.complete
    calls = {"discovery": False, "summary": 0}
    def complete(model_id, messages, specs, cancelled, on_event):
        if not calls["discovery"]:
            calls["discovery"] = True
            model.calls.append((model_id, messages, specs))
            return assistant(name="ryn_discover_tools", args={"load": ["inspect_service"]})
        if is_compaction_call(messages, specs):
            calls["summary"] += 1
            if calls["summary"] <= summary_failures:
                model.calls.append((model_id, messages, specs))
                raise AgentError("agent_model_context_overflow")
        return original(model_id, messages, specs, cancelled, on_event)
    model.complete = complete
    advance(runtime, 3)  # discover, load the schema, then fail a main request
    row = runtime.store.read()["tasks"][task_id]
    assert row["status"] == "queued" and row["tool_menu"]["loaded_names"]
    advance(runtime)
    row = runtime.store.read()["tasks"][task_id]
    assert calls["summary"] == 2
    if summary_failures == 1:
        assert row["status"] == "completed" and mains[0] == 2
    else:
        assert row["status"] == "failed" and row["error"] == "agent_context_limit" and mains[0] == 1
    assert any(e["type"] == "tool_menu_changed" and e.get("reason") == "context_space" for e in row["events"])
    assert row.get("model_retry_count", 0) == 0


def test_recovery_is_still_one_shot_after_encrypted_restart(tmp_path):
    runtime, model, task_id, mains, key = runtime_with_overflow(tmp_path, overflows=2)
    advance(runtime)
    restarted = AgentRuntime(AgentStore(tmp_path, key), model, runtime.tools, runtime.catalog)
    restarted.reconcile()
    advance(restarted)
    row = restarted.store.read()["tasks"][task_id]
    assert row["status"] == "failed" and row["error"] == "agent_context_limit" and mains[0] == 2


@pytest.mark.parametrize("stage,size,images,tokens,clears", [
    ("model", 40000, 0, 20000, True), ("compaction", 40000, 0, 20000, False),
    ("model", 1000, 0, 500, False), ("model", 40000, 10, 20000, False),
    ("model", 40000, 0, None, False),
])
def test_only_valid_main_calibration_removes_penalty(tmp_path, stage, size, images, tokens, clears):
    runtime, _, _, _ = make_runtime(tmp_path)
    task_id = runtime.create({"message": "Inspect"})["id"]
    def reserve(data):
        row = data["tasks"][task_id]
        row["context_density_penalty"] = .85
        record = budget.reserve(row, "model", stage, input_bound=30000, output_bound=100)
        row["budget"]["records"][-1]["output_policy"] = dict(projected_context_utf8_bytes=size,
            estimated_image_tokens=images, model_id=row["model_id"])
        return record["id"]
    reservation = runtime.store.transaction(reserve)
    runtime._settle_usage(task_id, reservation, usage={"input_tokens": tokens, "output_tokens": 5})
    row = runtime.store.read()["tasks"][task_id]
    assert ("context_density_penalty" not in row) is clears
    if clears:
        runtime.store.transaction(lambda data: data["tasks"][task_id].update(context_density_penalty=.8))
        runtime._settle_usage(task_id, reservation, usage={"input_tokens": tokens, "output_tokens": 5})
        assert runtime.store.read()["tasks"][task_id]["context_density_penalty"] == .8


def test_overflow_without_summarizable_history_is_bounded(tmp_path):
    runtime, model, _, _ = make_runtime(tmp_path)
    task_id = runtime.create({"message": "Inspect"})["id"]
    model.complete = lambda *a, **kw: (_ for _ in ()).throw(AgentError("agent_model_context_overflow"))
    advance(runtime, 2)
    row = runtime.store.read()["tasks"][task_id]
    assert row["status"] == "failed" and row["error"] == "agent_context_limit"
    assert not any(e["type"] == "context_compacted" for e in row["events"])


def test_successful_menu_only_recovery_clears_overflow_marker(tmp_path):
    from test_agent_runtime_retry import failing_model
    runtime, model, _, _ = make_runtime(tmp_path)
    task_id = runtime.create({"message": "Inspect"})["id"]
    failing_model(model, AgentError("agent_model_context_overflow"), assistant("Recovered."))
    advance(runtime, 2)
    row = runtime.store.read()["tasks"][task_id]
    assert row["status"] == "completed"
    assert "context_overflow" not in row


def test_model_switch_retires_pending_old_epoch_overflow(tmp_path):
    from test_agent_runtime_model_switch import setup, body
    from test_agent_runtime_retry import failing_model
    runtime, model, _, _, task_id, _ = setup(tmp_path)
    def pending(data):
        row = data["tasks"][task_id]
        row.update(context_overflow={"step": 1, "request_bytes": 20000},
            context_density_penalty=.25, context_overflow_recovery=[0, row["goal"]["revision"]])
    runtime.store.transaction(pending)
    runtime.switch_model(task_id, body(runtime, task_id))
    model.destination = runtime.store.read()["tasks"][task_id]["model_destination"]
    failing_model(model, assistant("New model inspected."))
    runtime.action(task_id, {"action": "resume", "expected_control_revision": runtime.get(task_id)["control_revision"]})
    advance(runtime)
    row = runtime.store.read()["tasks"][task_id]
    assert row["status"] == "completed"
    assert "context_overflow" not in row and "context_density_penalty" not in row
    assert not any(e["type"] == "context_compaction_started" for e in row["events"])
