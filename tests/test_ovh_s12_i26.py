"""S12-I26 vendor provenance and bounded real-HTTP limit adaptation."""
import io
import json
import time
from contextlib import contextmanager
from urllib.error import HTTPError

import pytest
from test_agent_integrations import add_source, node  # noqa: F401

from rynmesh.agent_runtime import output_policy
from rynmesh.agent_runtime.bridge import build_agent_integrations
from rynmesh.agent_runtime.runtime import AgentRuntime
from rynmesh.agent_runtime.store import AgentError, AgentModelResponseError, AgentStore
from rynmesh.llm_package import model_settings
from rynmesh.llm_package.transport_failure import classify_stream_error, describe
from rynmesh.services import peer_box


@pytest.mark.parametrize("name", ["deepseek-chat", "claude-legacy", "gpt-5-mini", "qwen3-older"])
def test_t1_family_window_is_a_guess_and_output_is_conservative(name):
    record = {"models": [name], "context_window_mode": "auto"}
    context = model_settings.context_metadata(record, name)
    assert context["context_window"] == model_settings.known_vendor_context_window(name)
    assert context["context_window_known"] is False and context["context_window_basis"] == "vendor_family_guess"
    output = model_settings.output_metadata(record, name)
    assert output["max_output_tokens"] <= 16384 and output["max_output_tokens_known"] is False
    assert output["max_output_basis"] == "vendor_family_guess"
    assert output_policy.configured_output({**context, **output}) == output["max_output_tokens"]
    policy = output_policy.context_budget({**context, **output})
    assert policy["context_window_tokens"] == 32768 and not policy["context_window_known"]


@pytest.mark.parametrize("name", ["claude-opus-5", "gpt-4o", "gpt-4.1", "gpt-5.6"])
def test_t2_t3_exact_model_remains_known(name):
    metadata = model_settings.context_metadata({"context_window_mode": "auto"}, name)
    assert metadata["context_window_known"] and metadata["context_window_basis"] == "vendor_family_default"


def test_t1_exact_prefix_does_not_attest_an_unlisted_variant():
    metadata = model_settings.context_metadata({"context_window_mode": "auto"}, "gpt-4o-unlisted")
    assert not metadata["context_window_known"] and metadata["context_window_basis"] == "vendor_family_guess"


@pytest.mark.parametrize("record,basis", [
    ({"context_window_mode": "auto", "model_settings": {"deepseek-chat": {"overrides": {"context_window": 64000, "max_output_tokens": 6000}}}}, "model_override"),
    ({"context_window_mode": "manual", "context_window": 64000, "max_output_tokens": 6000, "max_output_tokens_explicit": True}, "source_configuration"),
    ({"context_window_mode": "auto", "model_metadata": {"deepseek-chat": {"context_window": 64000, "max_output_tokens": 6000}}}, "provider_model_metadata"),
])
def test_t4_owner_and_provider_values_take_priority(record, basis):
    context = model_settings.context_metadata(record, "deepseek-chat")
    output = model_settings.output_metadata(record, "deepseek-chat")
    assert context == {"context_window": 64000, "context_window_known": True, "context_window_basis": basis}
    assert output["max_output_tokens"] == 6000 and output["max_output_tokens_known"]


def test_t4_new_sources_leave_non_deepseek_output_unset_and_bound_deepseek_default(node):
    app, _ = node
    sources = app.state.llm_agent_services["source_store"]
    body = {"name": "API", "kind": "api", "base_url": "https://vendor.test/v1", "models": ["unlisted"]}
    plain = sources.prepare(body)
    deepseek = sources.prepare({**body, "provider": "deepseek", "models": ["deepseek-chat"]})
    assert plain.get("max_output_tokens") is None and not plain["max_output_tokens_explicit"]
    assert deepseek["max_output_tokens"] == 65536 and not deepseek["max_output_tokens_explicit"]
    explicit = sources.prepare({**body, "max_output_tokens": 4096, "max_output_tokens_explicit": True})
    assert explicit["max_output_tokens"] == 4096 and explicit["max_output_tokens_explicit"]


@pytest.mark.parametrize("published", [False, True])
def test_t1_catalog_local_and_peer_inference_preserves_guess_provenance(node, published):
    app, store = node
    _, _, catalog = build_agent_integrations(app, store, store.home)
    catalog.services["discover_agent"] = lambda _: [{"peer_id": peer, "online": True, "ready": True,
        "chat_protocol": "rynmesh.chat.v1", "service": {"package_id": "family-service", "adapter": "openai_compatible",
        "model_alias": "deepseek-chat", "capabilities": ["client-tools"],
        **({"context_window": 1048576, "context_window_basis": "vendor_family_guess"} if published else {})}}
        for peer in (store.peer_id, "test-peer")]
    rows = catalog.list_models()
    assert len(rows) == 2
    for row in rows:
        assert row["context_window"] == 1048576 and not row["context_window_known"]
        assert row["context_window_basis"] == "vendor_family_guess"
        assert output_policy.context_budget(row)["context_window_tokens"] == 32768


def test_t1_source_publication_carries_the_family_guess_basis(node):
    app, store = node
    _, mid = add_source(app, models=["deepseek-chat"])
    _, _, catalog = build_agent_integrations(app, store, store.home)
    target = catalog.resolve(mid)
    manager = catalog.services["provider_for"](target["service_id"])
    manifest = manager.manifest.to_dict()
    assert manifest["context_window"] == 1048576
    assert manifest.get("context_window_basis") == "vendor_family_guess"
    sources = catalog.services["source_store"]
    record = sources.read()[target["source_id"]]
    record["model_metadata"] = {"deepseek-chat": {"context_window": 1048576}}
    sources.save(record)
    refreshed = catalog.services["provider_for"](target["service_id"]).manifest.to_dict()
    assert refreshed["context_window"] == 1048576
    assert refreshed["context_window_basis"] == "provider_model_metadata"


def script_http(monkeypatch, steps):
    requests = []
    class Response:
        headers = {"content-type": "application/json"}
        def __init__(self, content, finish):
            self.raw = json.dumps({"choices": [{"message": {"role": "assistant", "content": content,
                "reasoning_content": "PRIVATE_PROVIDER_THOUGHTS" if not content else ""}, "finish_reason": finish}],
                "usage": {"prompt_tokens": 20, "completion_tokens": 10}}).encode()
        def read(self, limit):
            result, self.raw = self.raw[:limit], self.raw[limit:]
            return result
        def close(self):
            return None
    @contextmanager
    def urlopen(request, **kwargs):
        requests.append(json.loads(request.data))
        assert len(requests) <= len(steps), "unbounded retry"
        step = steps[len(requests) - 1]
        if isinstance(step, dict):
            raise HTTPError(request.full_url, 400, "Bad Request", {}, io.BytesIO(json.dumps({"error": step}).encode()))
        yield Response(*step)
    monkeypatch.setattr("urllib.request.urlopen", urlopen)
    return requests


def setup_bridge(node, *, known=True):
    app, store = node
    record, model_id = add_source(app)
    if known:
        record["model_metadata"] = {"model-a": {"context_window": 128000, "max_output_tokens": 16384}}
        app.state.llm_agent_services["source_store"].save(record)
    bridge, tools, catalog = build_agent_integrations(app, store, store.home)
    return bridge, tools, catalog, model_id


@pytest.mark.parametrize("kind,numeric", [("output", True), ("output", False), ("context", True), ("context", False)])
def test_t5_real_http_adjusts_once_records_event_and_caches_destination_bound_limit(node, monkeypatch, kind, numeric):
    bridge, _, catalog, mid = setup_bridge(node)
    message = ("max_tokens must be at most 6000" if numeric else "max_tokens is too large") if kind == "output" else (
        "maximum context length is 32768 tokens; requested 150000" if numeric else "context window exceeded")
    error = {"type": "invalid_request_error", "param": "max_tokens" if kind == "output" else "messages", "message": message + " PRIVATE_UPSTREAM_TEXT"}
    requests = script_http(monkeypatch, [error, ("Done", "stop")])
    events = []
    result = bridge.complete(mid, [{"role": "user", "content": "same request"}], [], lambda: False, events.append)
    assert result["message"]["content"] == "Done" and len(requests) == 2
    assert requests[0]["messages"] == requests[1]["messages"]
    assert requests[1]["max_tokens"] == (6000 if kind == "output" and numeric else 8192)
    adjustment = next(e for e in events if e["type"] == "model_limit_adjusted")
    assert adjustment["basis"] == "upstream_rejection" and adjustment["old_max_output_tokens"] == 16384
    assert adjustment["new_max_output_tokens"] == requests[1]["max_tokens"]
    learned = catalog.resolve(mid)
    assert learned["max_output_tokens"] == requests[1]["max_tokens"]
    assert learned["max_output_basis"] == "upstream_rejection"
    if kind == "context":
        assert learned["context_window"] == (32768 if numeric else 64000)
        assert learned["context_window_known"] and learned["context_window_basis"] == "upstream_rejection"
    assert "PRIVATE_UPSTREAM_TEXT" not in json.dumps(events)
    from rynmesh.agent_runtime.models import ModelCatalog
    restored = ModelCatalog(catalog.services, catalog.store).resolve(mid)
    assert restored["max_output_tokens"] == 16384


def test_t5_second_rejection_does_not_retry_again(node, monkeypatch):
    bridge, _, _, mid = setup_bridge(node)
    error = {"param": "max_tokens", "message": "max_tokens must be at most 6000"}
    requests = script_http(monkeypatch, [error, error])
    events = []
    with pytest.raises(AgentError):
        bridge.complete(mid, [{"role": "user", "content": "test"}], [], lambda: False, events.append)
    assert len(requests) == 2 and sum(e["type"] == "model_limit_adjusted" for e in events) == 1


@pytest.mark.parametrize("status", [400, 413, 422])
def test_t5_error_boundary_extracts_only_numeric_limits(status):
    for message, key, expected in [("maximum context length is 32,768 tokens; requested 90000", "context_window_limit", 32768),
                                   ("max_tokens must be at most 6,000; got 16384", "output_tokens_limit", 6000),
                                   ("max_output_tokens must be less than 8192", "output_tokens_limit", 8191)]:
        envelope = {"error": {"type": "invalid_request_error", "message": message + " PRIVATE_BODY"}}
        diagnostics = describe(HTTPError("https://vendor.test", status, "Bad", {}, io.BytesIO()), envelope=envelope)
        assert diagnostics[key] == expected and "PRIVATE_BODY" not in json.dumps(diagnostics)
    code, diagnostics = classify_stream_error({"type": "error", "error": {"code": "context_length_exceeded",
        "message": "maximum context length is 16384 tokens PRIVATE_BODY"}})
    assert code == "upstream_context_overflow" and diagnostics["context_window_limit"] == 16384


def test_t5_smaller_window_that_cannot_fit_input_learns_then_defers_to_compaction(node, monkeypatch):
    bridge, _, catalog, mid = setup_bridge(node)
    error = {"code": "context_length_exceeded", "message": "maximum context length is 8192 tokens"}
    requests = script_http(monkeypatch, [error])
    events = []
    with pytest.raises(AgentError, match="^agent_model_context_overflow$"):
        bridge.complete(mid, [{"role": "user", "content": "x" * 97000}], [], lambda: False, events.append)
    assert len(requests) == 1 and catalog.resolve(mid)["context_window"] == 8192
    assert catalog.resolve(mid)["max_output_tokens"] == 4096
    assert next(e for e in events if e["type"] == "model_limit_adjusted")["new_context_window"] == 8192


@pytest.mark.parametrize("second_empty", [False, True])
def test_t6_empty_length_raises_output_once_and_then_returns_or_fails(node, monkeypatch, second_empty):
    bridge, _, _, mid = setup_bridge(node, known=False)
    requests = script_http(monkeypatch, [("", "length"), ("", "length") if second_empty else ("Done", "stop")])
    events = []
    if second_empty:
        with pytest.raises(AgentModelResponseError):
            bridge.complete(mid, [{"role": "user", "content": "Inspect"}], [], lambda: False, events.append)
    else:
        assert bridge.complete(mid, [{"role": "user", "content": "Inspect"}], [], lambda: False, events.append)["message"]["content"] == "Done"
    assert [r["max_tokens"] for r in requests] == [8192, 16384]
    raised = [e for e in events if e["type"] == "output_limit_raised"]
    assert len(raised) == 1 and raised[0]["old_max_output_tokens"] == 8192 and raised[0]["new_max_output_tokens"] == 16384


@pytest.mark.parametrize("second_empty", [False, True])
def test_t6_real_runtime_records_evidence_usage_and_shares_format_repair_budget(node, monkeypatch, second_empty):
    _, store = node
    bridge, tools, catalog, mid = setup_bridge(node, known=False)
    requests = script_http(monkeypatch, [("", "length"), ("", "length") if second_empty else ("Done", "stop")])
    key = peer_box.load_or_create_messaging_key(store.home / "messaging.x25519")
    runtime = AgentRuntime(AgentStore(store.home / "i26-agent", key), bridge, tools, catalog)
    runtime.update_settings({"enabled": True, "model_id": mid})
    tid = runtime.create({"message": "Inspect model limits"})["id"]
    runtime.run_once()
    row = runtime.get(tid)
    assert row["status"] == ("failed" if second_empty else "completed")
    assert len(requests) == 2 and sum(e["type"] == "output_limit_raised" for e in row["events"]) == 1
    assert [r["max_tokens"] for r in requests] == [8192, 16384]
    assert row["budget"]["lifetime"]["total_tokens"] == 60
    assert "PRIVATE_PROVIDER_THOUGHTS" not in json.dumps(row)
    runtime.run_once()
    assert len(requests) == 2


def test_t6_output_retry_respects_explicit_model_call_budget(node, monkeypatch):
    _, store = node
    bridge, tools, catalog, mid = setup_bridge(node, known=False)
    requests = script_http(monkeypatch, [("", "length")])
    key = peer_box.load_or_create_messaging_key(store.home / "messaging.x25519")
    runtime = AgentRuntime(AgentStore(store.home / "i26-budget", key), bridge, tools, catalog)
    runtime.update_settings({"enabled": True, "model_id": mid})
    tid = runtime.create({"message": "Inspect", "budget_limits": {"max_total_model_calls": 1}})["id"]
    runtime.run_once()
    row = runtime.get(tid)
    assert len(requests) == 1 and row["budget"]["lifetime"]["total_tokens"] == 30
    assert row["error"] == "agent_model_call_budget_exhausted"


def test_t5_limit_adjustment_is_evidence_even_when_budget_blocks_the_retry(node, monkeypatch):
    _, store = node
    bridge, tools, catalog, mid = setup_bridge(node)
    requests = script_http(monkeypatch, [{"param": "max_tokens", "message": "max_tokens must be at most 6000"}])
    key = peer_box.load_or_create_messaging_key(store.home / "messaging.x25519")
    runtime = AgentRuntime(AgentStore(store.home / "i26-limit-budget", key), bridge, tools, catalog)
    runtime.update_settings({"enabled": True, "model_id": mid})
    tid = runtime.create({"message": "Inspect", "budget_limits": {"max_total_model_calls": 1}})["id"]
    runtime.run_once()
    row = runtime.get(tid)
    assert len(requests) == 1 and row["error"] == "agent_model_call_budget_exhausted"
    assert next(e for e in row["events"] if e["type"] == "model_limit_adjusted")["new_max_output_tokens"] == 6000
    assert catalog.resolve(mid)["max_output_tokens"] == 6000


def test_t6_output_retry_respects_explicit_token_budget_without_charging_unsent_request(node, monkeypatch):
    _, store = node
    bridge, tools, catalog, mid = setup_bridge(node, known=False)
    requests = script_http(monkeypatch, [("", "length")])
    key = peer_box.load_or_create_messaging_key(store.home / "messaging.x25519")
    runtime = AgentRuntime(AgentStore(store.home / "i26-token-budget", key), bridge, tools, catalog)
    runtime.update_settings({"enabled": True, "model_id": mid})
    tid = runtime.create({"message": "Inspect", "budget_limits": {"max_total_tokens": 50000}})["id"]
    row = runtime.store.read()["tasks"][tid]
    estimate = output_policy.select(catalog.resolve(mid), runtime._context(row), runtime._specs(row))["estimated_input_tokens"]
    runtime.store.transaction(lambda data: data["tasks"][tid]["budget"]["limits"].update(max_total_tokens=estimate + 4000))
    runtime.run_once()
    row = runtime.get(tid)
    assert len(requests) == 1 and row["budget"]["lifetime"]["total_tokens"] == 30
    assert row["error"] == "agent_token_budget_exhausted"


def test_t6_retry_has_a_distinct_source_dispatch_and_honors_daily_request_quota(node, monkeypatch):
    bridge, _, catalog, mid = setup_bridge(node, known=False)
    sources = catalog.services["source_store"]
    record = sources.read()[catalog.resolve(mid)["source_id"]]
    sources.configure_model(record["id"], {"model_id": "model-a",
        "expected_rule_revision": record["model_settings"]["model-a"]["rule_revision"],
        "configuration": {"requests_per_day": 1}})
    requests = script_http(monkeypatch, [("", "length")])
    with pytest.raises(AgentError, match="^agent_model_quota_exhausted$"):
        bridge.complete(mid, [{"role": "user", "content": "Inspect"}], [], lambda: False)
    assert len(requests) == 1


def test_t6_discovered_window_without_output_limit_can_raise_beyond_the_default(node, monkeypatch):
    app, store = node
    record, mid = add_source(app)
    record["model_metadata"] = {"model-a": {"context_window": 128000}}
    app.state.llm_agent_services["source_store"].save(record)
    bridge, _, _ = build_agent_integrations(app, store, store.home)
    requests = script_http(monkeypatch, [("", "length"), ("Done", "stop")])
    response = bridge.complete(mid, [{"role": "user", "content": "Inspect"}], [], lambda: False)
    assert response["message"]["content"] == "Done"
    assert [r["max_tokens"] for r in requests] == [16384, 32768]


@pytest.mark.parametrize("request_cap", [None, 8192])
def test_t6_vendor_output_ceiling_bounds_the_raise(node, monkeypatch, request_cap):
    app, store = node
    _, mid = add_source(app, models=["gpt-4o"])
    bridge, _, _ = build_agent_integrations(app, store, store.home)
    requests = script_http(monkeypatch, [("", "length")] if request_cap is None else [("", "length"), ("Done", "stop")])
    if request_cap is None:
        with pytest.raises(AgentModelResponseError):
            bridge.complete(mid, [{"role": "user", "content": "Inspect"}], [], lambda: False)
        assert [r["max_tokens"] for r in requests] == [16384]
    else:
        bridge.complete(mid, [{"role": "user", "content": "Inspect"}], [], lambda: False, max_output_tokens=request_cap)
        assert [r["max_tokens"] for r in requests] == [8192, 16384]


def test_t5_real_bridge_keeps_the_runtime_calibrated_context_decision(node, monkeypatch):
    app, store = node
    record, mid = add_source(app)
    record["model_metadata"] = {"model-a": {"context_window": 32000, "max_output_tokens": 4096}}
    app.state.llm_agent_services["source_store"].save(record)
    bridge, tools, catalog = build_agent_integrations(app, store, store.home)
    requests = script_http(monkeypatch, [("Done", "stop")])
    key = peer_box.load_or_create_messaging_key(store.home / "messaging.x25519")
    runtime = AgentRuntime(AgentStore(store.home / "i26-calibrated", key), bridge, tools, catalog)
    runtime.update_settings({"enabled": True, "model_id": mid})
    tid = runtime.create({"message": "Inspect"})["id"]
    def calibrate(data):
        row = data["tasks"][tid]
        row["status"] = "running"
        row["budget"]["records"].append({"id": "prior-density", "kind": "model", "stage": "model", "round": 0,
            "status": "settled", "input_tokens": 10000, "output_tokens": 10, "charged_tokens": 10010,
            "charged_seconds": 0.0, "elapsed_seconds": 0.0, "input_reservation": 0, "output_reservation": 0,
            "output_policy": {"model_id": mid, "projected_context_utf8_bytes": 42000, "estimated_image_tokens": 0}})
    runtime.store.transaction(calibrate)
    row = runtime.store.read()["tasks"][tid]
    row.update(_run_started=time.monotonic(), _run_lifetime_remaining=300)
    messages = [{"role": "user", "content": "x" * 97000}]
    assert runtime._effective_capacity(row)[0] > runtime._context_limit(row)
    response = runtime._complete(row, messages, [], lambda: False, None)
    assert response["message"]["content"] == "Done"
    assert len(requests) == 1 and requests[0]["messages"] == messages
