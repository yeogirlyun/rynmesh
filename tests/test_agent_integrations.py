import copy
import json
import sys
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from rynmesh.agent_runtime.bridge import build_agent_integrations
from rynmesh.agent_runtime.models import ModelCatalog
from rynmesh.agent_runtime.runtime import AgentRuntime
from rynmesh.agent_runtime.store import AgentError, AgentStore, AgentToolRejected
from rynmesh.ai_access.routes import AIAccessState
from rynmesh.ai_access.store import AIAccessStore
from rynmesh.llm_package.adapters import AdapterError
from rynmesh.llm_package.cli_adapter import CLIAgentAdapter
from rynmesh.llm_package.manifest import LLMPackageManifest
from rynmesh.llm_package.routes import CAPABILITY, install_llm_routes
from rynmesh.llm_package.sources import SourceStore
from rynmesh.services import peer_box
from rynmesh.store import RynmeshStore


@pytest.fixture
def node(tmp_path):
    store = RynmeshStore(home=tmp_path / "node", network_dir=tmp_path / "network")
    app = FastAPI()
    install_llm_routes(app, store=store, home=store.home,
        messaging_key=peer_box.load_or_create_messaging_key(store.home / "messaging.x25519"),
        resolve_endpoint=lambda _: "", resolve_pubkey=lambda _: "")
    yield app, store
    app.state.llm_shutdown()


def add_source(app, *, support="supported", models=None):
    sources = app.state.llm_agent_services["source_store"]
    record = sources.prepare({"name": "Owner API", "kind": "api", "base_url": "https://vendor.test/v1",
                              "api_key": "secret-do-not-export", "models": models or ["model-a"]})
    record.update(status="ready", tool_support=dict.fromkeys(record["models"], support))
    sources.save(record)
    return record, "local/" + sources.model_service_id(record, record["models"][0])


def fake_upstream(monkeypatch, messages, *, json_only=False, finish_reason=None, usage="default"):
    requests = []
    returned_usage = {"prompt_tokens": 20, "completion_tokens": 10} if usage == "default" else usage

    class Response:
        def __init__(self, message):
            self.message = message
            self.headers = {"content-type": "application/json" if json_only else "text/event-stream"}
            delta = copy.deepcopy(message)
            if delta.get("tool_calls"):
                delta["tool_calls"] = [{"index": index, **item} for index, item in enumerate(delta["tool_calls"])]
            self.lines = [b"data: " + json.dumps({"choices": [{"index": 0, "delta": delta,
                "finish_reason": finish_reason or ("tool_calls" if message.get("tool_calls") else "stop")}],
                "usage": returned_usage}).encode() + b"\n", b"data: [DONE]\n"]

        def readline(self, _limit):
            return self.lines.pop(0) if self.lines else b""

        def close(self):
            pass

        def read(self, _limit):
            return json.dumps({"choices": [{"message": self.message,
                "finish_reason": finish_reason or ("tool_calls" if self.message.get("tool_calls") else "stop")}],
                "usage": returned_usage}).encode()

    @contextmanager
    def urlopen(request, **kwargs):
        requests.append(json.loads(request.data))
        message = messages.pop(0)
        yield Response(message(requests[-1]) if callable(message) else message)

    monkeypatch.setattr("urllib.request.urlopen", urlopen)
    return requests


def call(name="ryn_list_devices", arguments="{}", call_id="call-real"):
    return {"role": "assistant", "content": None, "tool_calls": [
        {"id": call_id, "type": "function", "function": {"name": name, "arguments": arguments}}]}


def passed_checker(request):
    payload = json.loads(request["messages"][-1]["content"])
    return {"role": "assistant", "content": json.dumps({"status": "passed", "summary": "Full owner requirements covered",
            "uncovered_requirements": [], "evidence_refs": [row["id"] for row in payload["evidence"]]})}


@pytest.mark.parametrize("finish_reason", ["length", "content_filter", "upstream-private-diagnostic"])
def test_source_bridge_preserves_safe_finish_reason(node, monkeypatch, finish_reason):
    app, store = node
    _, model_id = add_source(app)
    model, _, _ = build_agent_integrations(app, store, store.home)
    fake_upstream(monkeypatch, [{"role": "assistant", "content": '{"status":"passed",'}], json_only=True, finish_reason=finish_reason)
    result = model.complete(model_id, [{"role": "user", "content": "Return verification JSON"}], [], lambda: False)
    assert result["finish_reason"] == (finish_reason if finish_reason != "upstream-private-diagnostic" else "unknown")
    assert "upstream-private-diagnostic" not in json.dumps(result)


@pytest.mark.parametrize("usage,expected", [(None, {"input_tokens": None, "output_tokens": None}),
    ({"prompt_tokens": 0, "completion_tokens": 0}, {"input_tokens": 0, "output_tokens": 0})])
def test_bridge_usage_unknown_is_not_zero_and_requested_token_cap_reaches_provider(node, monkeypatch, usage, expected):
    app, store = node
    _, model_id = add_source(app)
    model, _, _ = build_agent_integrations(app, store, store.home)
    requests = fake_upstream(monkeypatch, [{"role": "assistant", "content": "Bounded reply"}], json_only=True, usage=usage)
    result = model.complete(model_id, [{"role": "user", "content": "Reply"}], [], lambda: False, max_output_tokens=73)
    assert requests[0]["max_tokens"] == 73 and result["usage"] == expected
    with pytest.raises(AgentError, match="agent_token_limit"):
        model.complete(model_id, [{"role": "user", "content": "Reply"}], [], lambda: False, max_output_tokens=0)
    assert len(requests) == 1


@pytest.mark.parametrize("finish_reason", ["length", "private-upstream-stop"])
def test_empty_model_response_keeps_safe_usage_and_stop_reason_without_reasoning(node, monkeypatch, finish_reason):
    app, store = node
    _, model_id = add_source(app)
    bridge, tools, catalog = build_agent_integrations(app, store, store.home)
    requests = fake_upstream(monkeypatch, [{"role": "assistant", "content": "", "reasoning_content": "PRIVATE_PROVIDER_THOUGHTS"}],
                             json_only=True, finish_reason=finish_reason)
    key = peer_box.load_or_create_messaging_key(store.home / "messaging.x25519")
    runtime = AgentRuntime(AgentStore(store.home / "empty-response-agent", key), bridge, tools, catalog)
    runtime.update_settings({"enabled": True, "model_id": model_id})
    task = runtime.create({"message": "Inspect model settings."})
    runtime.run_once()
    row = runtime.get(task["id"])
    assert row["status"] == "queued" and len(requests) == 1, row.get("error")
    rejected = next(event for event in row["events"] if event["type"] == "model_response_rejected")
    assert rejected["finish_reason"] == ("length" if finish_reason == "length" else "unknown")
    assert rejected["usage"] == {"input_tokens": 20, "output_tokens": 10}
    assert row["budget"]["lifetime"]["total_tokens"] == 30
    assert "PRIVATE_PROVIDER_THOUGHTS" not in json.dumps(row) and "private-upstream-stop" not in json.dumps(row)


def test_real_source_bridge_roundtrips_structured_tools_without_provider_context(node, monkeypatch):
    app, store = node
    record, model_id = add_source(app)
    model, tools, catalog = build_agent_integrations(app, store, store.home)
    requests = fake_upstream(monkeypatch, [call(), {"role": "assistant", "content": "One device inspected."}])
    history = [{"role": "system", "content": "Only consumer context"}, {"role": "user", "content": "Inspect devices"}]
    first = model.complete(model_id, history, tools.specs(), lambda: False)
    evidence = tools.execute("ryn_list_devices", {}, operation_id="task-1-call-1", cancelled=lambda: False)
    history += [first["message"], {"role": "tool", "tool_call_id": "call-real", "content": json.dumps(evidence)}]
    second = model.complete(model_id, history, tools.specs(), lambda: False)
    assert second["message"]["content"] == "One device inspected."
    assert requests[0]["messages"] == history[:2]
    assert requests[1]["messages"] == history
    assert requests[0]["tools"] == tools.specs()
    assert requests[0]["model"] == "model-a"
    assert "secret-do-not-export" not in json.dumps(catalog.list_models())
    assert not record["sharing"]  # Agent access does not enable outgoing sharing.


@pytest.mark.parametrize(("failure", "code"), [
    (TimeoutError("private-upstream-detail"), "agent_inference_timeout"),
    (ConnectionError("private-upstream-detail"), "agent_model_connection_failed"),
    (AdapterError("private-upstream-detail", code="not-a-trusted-code"), "agent_inference_failed"),
])
def test_bridge_preserves_safe_real_adapter_transport_error_codes(node, monkeypatch, failure, code):
    app, store = node
    _record, model_id = add_source(app)
    bridge, tools, _catalog = build_agent_integrations(app, store, store.home)

    def fail(*_args, **_kwargs):
        raise failure

    monkeypatch.setattr("urllib.request.urlopen", fail)
    with pytest.raises(AgentError) as caught:
        bridge.complete(model_id, [{"role": "user", "content": "Inspect models"}], tools.specs(), lambda: False)
    assert str(caught.value) == code
    assert "private-upstream-detail" not in str(caught.value)


def test_bridge_cancellation_takes_precedence_over_transport_error(node, monkeypatch):
    app, store = node
    _record, model_id = add_source(app)
    bridge, tools, _catalog = build_agent_integrations(app, store, store.home)
    cancelled = False

    def fail(*_args, **_kwargs):
        nonlocal cancelled
        cancelled = True
        raise TimeoutError("private-upstream-detail")

    monkeypatch.setattr("urllib.request.urlopen", fail)
    with pytest.raises(ValueError, match="^agent_cancelled$"):
        bridge.complete(model_id, [{"role": "user", "content": "Inspect models"}], tools.specs(), lambda: cancelled)


def test_device_evidence_distinguishes_live_expired_and_unknown(node, monkeypatch):
    app, store = node
    _model, tools, _catalog = build_agent_integrations(app, store, store.home)
    now = datetime.now(timezone.utc)
    monkeypatch.setattr(store, "discover_peers", lambda **_: {"peers": [
        {"peer_id": "recent", "node_name": "Recent", "updated_at": now.isoformat(), "discovery_source": "registry", "endpoints": ["https://recent.test"]},
        {"peer_id": "expired", "node_name": "Expired", "updated_at": (now - timedelta(minutes=5)).isoformat(), "discovery_source": "registry", "endpoints": ["https://expired.test"]},
        {"peer_id": "cached", "node_name": "Cached", "updated_at": (now - timedelta(days=1)).isoformat(), "discovery_source": "cache", "endpoints": []}]})
    probes = []

    def failed_probe(url, *_):
        probes.append(url)
        return False

    monkeypatch.setattr(tools.peer_health, "_probe", failed_probe)
    result = tools.execute("ryn_list_devices", {}, operation_id="inspect-presence", cancelled=lambda: False)
    rows = {row["peer_id"]: row for row in result["devices"]}
    assert rows[store.peer_id]["online"] is True and rows[store.peer_id]["evidence_source"] == "self"
    assert rows[store.peer_id]["http_probe_performed"] is False
    assert rows[store.peer_id]["http_reachable"] is None
    assert rows[store.peer_id]["lan_probe_performed"] is False
    assert rows[store.peer_id]["lan_reachable"] is None
    assert rows["recent"]["online"] is True and rows["recent"]["evidence_source"] == "heartbeat"
    assert rows["expired"]["online"] is False and rows["expired"]["evidence_source"] == "heartbeat_expired"
    assert rows["cached"]["online"] is None and rows["cached"]["evidence_source"] == "unknown"
    assert rows["recent"]["http_probe_performed"] is True and rows["recent"]["http_reachable"] is False
    assert rows["expired"]["http_probe_performed"] is True and rows["expired"]["http_reachable"] is False
    assert rows["cached"]["http_probe_performed"] is False and rows["cached"]["http_reachable"] is None
    assert sorted(probes) == ["https://expired.test/health", "https://recent.test/health"]
    assert "https://" not in json.dumps(result)


def test_two_consumer_histories_remain_separate_on_same_provider(node, monkeypatch):
    app, store = node
    _record, model_id = add_source(app)
    model, tools, _catalog = build_agent_integrations(app, store, store.home)
    requests = fake_upstream(monkeypatch, [{"role": "assistant", "content": "A reply"}, {"role": "assistant", "content": "B reply"}])
    a = [{"role": "user", "content": "consumer A secret conversation"}]
    b = [{"role": "user", "content": "consumer B conversation"}]
    model.complete(model_id, a, tools.specs(), lambda: False)
    model.complete(model_id, b, tools.specs(), lambda: False)
    assert requests[0]["messages"] == a
    assert requests[1]["messages"] == b
    assert "consumer A" not in json.dumps(requests[1])


def test_real_bridge_invalid_control_proposal_is_rejected_and_model_can_correct(node, monkeypatch):
    app, store = node
    _record, model_id = add_source(app)
    model, tools, catalog = build_agent_integrations(app, store, store.home)
    requests = fake_upstream(monkeypatch, [call("ryn_update_plan", json.dumps({"goal_revision": 1, "steps": [],
        "criteria_proposals": [{"description": "Trust me", "kind": "semantic", "source": "user", "status": "passed"}]})),
        {"role": "assistant", "content": "My unsupported proposal was rejected."},
        {"role": "assistant", "content": json.dumps({"status": "unknown", "summary": "Business inspection is not evidenced.",
                                                     "uncovered_requirements": ["Inspect business state"], "evidence_refs": []})},
        {"role": "assistant", "content": "The invalid proposal remains rejected; no business inspection occurred."},
        {"role": "assistant", "content": json.dumps({"status": "unknown", "summary": "Business inspection remains unverified.",
                                                     "uncovered_requirements": ["Inspect business state"], "evidence_refs": []})}], json_only=True)
    runtime = AgentRuntime(AgentStore(store.home / "agent-test", peer_box.load_or_create_messaging_key(store.home / "messaging.x25519")), model, tools, catalog)
    runtime.update_settings({"enabled": True, "model_id": model_id})
    row = runtime.create({"message": "Inspect only, never change permissions"})
    for _ in range(8):
        if runtime.get(row["id"])["status"] not in {"queued", "running"}:
            break
        runtime.run_once()
    done = runtime.get(row["id"])
    assert done["status"] == "completed" and done["outcome"]["status"] == "unverified", {"error": done["error"], "events": [(e["type"], e.get("error")) for e in done["events"]]}
    authority_fields = ("id", "goal_revision", "source", "source_ref", "required", "kind", "description", "verifier")
    assert [{key: item[key] for key in authority_fields} for item in done["criteria"]] == [
        {key: item[key] for key in authority_fields} for item in row["criteria"]]
    assert done["criteria"][0]["status"] == "unknown" and not done["plan"]["steps"]
    assert len(requests) == 5
    assert "agent_invalid_control_proposal" in requests[1]["messages"][-1]["content"]


def test_real_bridge_keeps_business_tool_argument_validation(node, monkeypatch):
    app, store = node
    _record, model_id = add_source(app)
    model, tools, _catalog = build_agent_integrations(app, store, store.home)
    fake_upstream(monkeypatch, [call("ryn_set_model_sharing", '{"enabled":true}')], json_only=True)
    with pytest.raises(ValueError):
        model.complete(model_id, [{"role": "user", "content": "Inspect only"}], tools.specs(), lambda: False)


def test_destination_generation_changes_for_credentials_not_sharing_and_never_resurrects(node):
    app, store = node
    record, model_id = add_source(app)
    _model, _tools, catalog = build_agent_integrations(app, store, store.home)
    sources = app.state.llm_agent_services["source_store"]
    first = catalog.resolve(model_id)["destination_revision"]
    record["shared_models"] = list(record["models"])
    sources.save(record)
    assert catalog.resolve(model_id)["destination_revision"] == first
    old_key = record["api_key"]
    record["api_key"] = "replacement-private-key"
    sources.save(record)
    second = catalog.resolve(model_id, require_tools=False)["destination_revision"]
    assert not catalog.resolve(model_id, require_tools=False)["agent_supported"]
    assert second != first
    record["api_key"] = old_key
    sources.save(record)
    third = catalog.resolve(model_id, require_tools=False)["destination_revision"]
    assert third not in {first, second}
    assert old_key not in json.dumps(catalog.list_models())


def test_bound_bridge_uses_exact_authorized_source_adapter(node, monkeypatch):
    app, store = node
    _record, model_id = add_source(app)
    model, tools, catalog = build_agent_integrations(app, store, store.home)
    destination = catalog.destination(catalog.resolve(model_id))
    requests = fake_upstream(monkeypatch, [{"role": "assistant", "content": "Authorized receiver."}], json_only=True)
    result = model.complete(model_id, [{"role": "user", "content": "Authorized source data"}], tools.specs(), lambda: False,
                            expected_destination=destination)
    assert result["message"]["content"] == "Authorized receiver." and len(requests) == 1


def test_source_change_between_catalog_and_actual_adapter_selection_never_dispatches(node, monkeypatch):
    app, store = node
    record, model_id = add_source(app)
    model, tools, catalog = build_agent_integrations(app, store, store.home)
    destination = catalog.destination(catalog.resolve(model_id))
    sources = app.state.llm_agent_services["source_store"]
    original = model.services["provider_for"]
    changed = False

    def provider(service_id):
        nonlocal changed
        if not changed:
            changed = True
            record["base_url"] = "https://different-receiver.test/v1"
            sources.save(record)
        return original(service_id)

    monkeypatch.setitem(model.services, "provider_for", provider)
    requests = fake_upstream(monkeypatch, [], json_only=True)
    with pytest.raises(AgentError, match="agent_model_destination_changed"):
        model.complete(model_id, [{"role": "user", "content": "Must never reach replacement receiver"}], tools.specs(), lambda: False,
                       expected_destination=destination)
    assert not requests


def test_revalidated_changed_source_keeps_model_id_but_cannot_reuse_old_destination(node, monkeypatch):
    app, store = node
    record, model_id = add_source(app)
    model, tools, catalog = build_agent_integrations(app, store, store.home)
    old_destination = catalog.destination(catalog.resolve(model_id))
    sources = app.state.llm_agent_services["source_store"]
    edited = sources.prepare({"base_url": "https://replacement-provider.test/v1", "api_key": "replacement-key"}, record["id"])
    assert edited["tool_support"] == {}
    sources.save(edited)
    # Model/tool verification can restore availability, never the earlier data grant.
    edited.update(status="ready", tool_support={"model-a": "supported"})
    sources.save(edited)
    current = catalog.resolve(model_id)
    assert current["id"] == model_id and current["agent_supported"]
    assert current["destination_revision"] != old_destination["destination_revision"]
    requests = fake_upstream(monkeypatch, [], json_only=True)
    with pytest.raises(AgentError, match="agent_model_destination_changed"):
        model.complete(model_id, [{"role": "user", "content": "Prior private resource body"}], tools.specs(), lambda: False,
                       expected_destination=old_destination)
    assert not requests


def test_runtime_real_tools_preview_approval_change_and_verify(node, monkeypatch):
    app, store = node
    record, model_id = add_source(app, models=["model-a", "model-b"])
    store.personal_space.create("Integration space")
    store.personal_space.set_policy("space")
    model, tools, catalog = build_agent_integrations(app, store, store.home)
    requests = fake_upstream(monkeypatch, [call("ryn_list_models", call_id="inspect"),
        call("ryn_set_model_sharing", json.dumps({"model_id": model_id, "enabled": True}), call_id="change"),
        call("ryn_list_models", call_id="verify"), {"role": "assistant", "content": "Verified model-a shared; model-b private."},
        passed_checker], json_only=True)
    runtime = AgentRuntime(AgentStore(store.home / "agent-test", peer_box.load_or_create_messaging_key(store.home / "messaging.x25519")), model, tools, catalog)
    runtime.update_settings({"enabled": True, "model_id": model_id})
    task = runtime.create({"message": "Inspect and share only model-a"})
    for _ in range(4):
        runtime.run_once()
    waiting = runtime.get(task["id"])
    assert waiting["status"] == "waiting_approval", waiting
    assert waiting["pending_approval"]["preview"]["after"] == {"sharing": True, "scope": "personal_space"}
    sources = app.state.llm_agent_services["source_store"]
    assert sources.read()[record["id"]]["shared_models"] == []
    runtime.action(task["id"], {"action": "approve", "expected_revision": waiting["revision"],
                               "approval_id": waiting["pending_approval"]["id"]})
    for _ in range(12):
        if runtime.get(task["id"])["status"] not in {"queued", "running"}:
            break
        runtime.run_once()
    done = runtime.get(task["id"])
    assert done["status"] == "completed", done
    assert sources.read()[record["id"]]["shared_models"] == ["model-a"]
    evidence = json.loads(requests[-2]["messages"][-1]["content"])
    assert [row["sharing"] for row in evidence["models"]] == [True, False]
    assert "secret-do-not-export" not in json.dumps(done)
    assert all(not request.get("stream") for request in requests)
    assert not requests[-1].get("tools")
    assert done["outcome"]["status"] == "verified"
    assert {criterion["kind"] for criterion in done["criteria"]} == {"mechanical", "semantic"}


def test_source_sharing_migrates_legacy_and_new_models_do_not_inherit(tmp_path):
    sources = SourceStore(tmp_path, b"key")
    record = sources.prepare({"name": "legacy", "kind": "local", "base_url": "http://127.0.0.1:9999",
                              "models": ["a", "b"], "sharing": True})
    record.pop("shared_models")
    sources.save(record)
    updated = sources.prepare({"models": ["a", "b", "c"]}, record["id"])
    assert updated["shared_models"] == ["a", "b"]
    sources.save(updated)
    changed = sources.prepare({"models": ["a", "c"]}, record["id"])
    assert changed["shared_models"] == ["a"]
    assert sources.prepare({"shared_models": ["b"]}, record["id"])["shared_models"] == ["b"]
    with pytest.raises(ValueError):
        sources.prepare({"shared_models": ["not-configured"]}, record["id"])


def test_tools_validate_schema_preview_and_share_only_one_model_idempotently(node):
    app, store = node
    record, model_id = add_source(app, models=["model-a", "model-b"])
    store.personal_space.create("Test space")
    store.personal_space.set_policy("space")
    _model, tools, catalog = build_agent_integrations(app, store, store.home)
    args = {"model_id": model_id, "enabled": True}
    preview = tools.preview("ryn_set_model_sharing", args)
    assert preview["before"] == {"sharing": False, "scope": "personal_space"}
    assert preview["after"] == {"sharing": True, "scope": "personal_space"}
    assert "secret-do-not-export" not in json.dumps(preview)
    assert tools.metadata("ryn_set_model_sharing")["effect"] == "write"
    with pytest.raises(ValueError):
        tools.execute("ryn_set_model_sharing", {**args, "approved": True}, operation_id="fake", cancelled=lambda: False)
    result = tools.execute("ryn_set_model_sharing", args, operation_id="approved-op", cancelled=lambda: False, expected_preview=preview)
    assert result == tools.execute("ryn_set_model_sharing", args, operation_id="approved-op", cancelled=lambda: False)
    with pytest.raises(ValueError, match="operation_id_conflict"):
        tools.execute("ryn_set_model_sharing", {**args, "enabled": False}, operation_id="approved-op", cancelled=lambda: False)
    sources = app.state.llm_agent_services["source_store"]
    saved = sources.read()[record["id"]]
    assert saved["shared_models"] == ["model-a"]
    managers = app.state.llm_agent_services["active_source_managers"]()
    assert managers[sources.model_service_id(saved, "model-a")].accepting_orders
    assert not managers[sources.model_service_id(saved, "model-b")].accepting_orders
    rows = catalog.list_models()
    assert [row["sharing"] for row in rows] == [True, False]
    assert tools.preview("ryn_set_model_sharing", args)["configuration_revision"] != preview["configuration_revision"]


def test_sharing_rechecks_personal_space_and_cancel_before_mutation(node):
    app, store = node
    record, model_id = add_source(app)
    _model, tools, _catalog = build_agent_integrations(app, store, store.home)
    with pytest.raises(ValueError, match="personal_space_required"):
        tools.execute("ryn_set_model_sharing", {"model_id": model_id, "enabled": True}, operation_id="op", cancelled=lambda: False)
    with pytest.raises(ValueError, match="cancelled"):
        tools.execute("ryn_set_model_sharing", {"model_id": model_id, "enabled": False}, operation_id="op", cancelled=lambda: True)
    assert not app.state.llm_agent_services["source_store"].read()[record["id"]]["sharing"]


def test_sharing_write_binds_approved_source_snapshot_inside_storage_lock(node):
    app, store = node
    record, model_id = add_source(app)
    store.personal_space.create("Test space")
    store.personal_space.set_policy("space")
    _model, tools, _catalog = build_agent_integrations(app, store, store.home)
    args = {"model_id": model_id, "enabled": True}
    approved = tools.preview("ryn_set_model_sharing", args)
    sources = app.state.llm_agent_services["source_store"]
    sources.save({**record, "name": "Changed after approval"})
    with pytest.raises(AgentToolRejected, match="approval_preview_changed"):
        tools.execute("ryn_set_model_sharing", args, operation_id="stale-approved", cancelled=lambda: False, expected_preview=approved)
    with pytest.raises(ValueError, match="approval_preview_changed"):
        tools.execute("ryn_set_model_sharing", args, operation_id="missing-approved", cancelled=lambda: False)
    assert sources.read()[record["id"]]["shared_models"] == []


def test_per_model_share_immediately_publishes_and_last_delete_withdraws(node):
    app, store = node
    record, _ = add_source(app, models=["model-a", "model-b"])
    store.personal_space.create("Test space")
    store.personal_space.set_policy("space")
    with TestClient(app) as client:
        response = client.put(f"/api/local/llm/sources/{record['id']}", json={"shared_models": ["model-b"]})
        assert response.json()["publication"] == "published"
        capacities = store.list_job_capacities(network_id="rynmesh-main", capability=CAPABILITY, max_age_hours=1)["capacities"]
        # The stable service IDs make the explicit model selection observable in discovery.
        advertised = [service for capacity in capacities for service in capacity.get("metadata", {}).get("llm_services", [])]
        assert [service["service"]["model_alias"] for service in advertised] == ["Owner API · model-b"]
        assert client.delete(f"/api/local/llm/sources/{record['id']}").json()["publication"] == "published"
        capacities = store.list_job_capacities(network_id="rynmesh-main", capability=CAPABILITY, max_age_hours=1)["capacities"]
        assert not [service for capacity in capacities for service in capacity.get("metadata", {}).get("llm_services", [])]


def test_failed_last_model_withdrawal_retries_from_existing_publication_worker(node, monkeypatch):
    app, store = node
    record, _ = add_source(app)
    store.personal_space.create("Test space")
    store.personal_space.set_policy("space")
    original = store.register_job_capacity
    with TestClient(app) as client:
        assert client.put(f"/api/local/llm/sources/{record['id']}", json={"shared_models": ["model-a"]}).json()["publication"] == "published"
        def offline(**_):
            raise OSError("registry offline")
        monkeypatch.setattr(store, "register_job_capacity", offline)
        assert client.delete(f"/api/local/llm/sources/{record['id']}").json()["publication"] == "pending"
        pending = store.home / "llm" / "publication-pending.json"
        assert pending.exists()
        monkeypatch.setattr(store, "register_job_capacity", original)
        app.state.background_workers._specs["llm.publish-refresh"].run_once()
        assert not pending.exists()
        capacities = store.list_job_capacities(network_id="rynmesh-main", capability=CAPABILITY, max_age_hours=1)["capacities"]
        assert not [service for capacity in capacities for service in capacity.get("metadata", {}).get("llm_services", [])]


def test_native_service_probe_persists_verified_capability_without_guessing(node, monkeypatch):
    app, store = node
    native = app.state.native_model
    native.config["installed"] = True
    monkeypatch.setattr(native.adapter, "health", lambda: {"ok": True})
    sources = app.state.llm_agent_services["source_store"]
    record = sources.prepare({"name": "Native fake transport", "kind": "local", "base_url": "http://127.0.0.1:1234", "models": ["native-test"]})
    transport = sources.adapter(record)
    monkeypatch.setattr(native.adapter, "chat", transport.chat)
    fake_upstream(monkeypatch, [call("ryn_capability_probe", '{"value":"ryn-tool-test"}'), {"role": "assistant", "content": "ryn-tool-test-ok"}])
    _model, _tools, catalog = build_agent_integrations(app, store, store.home)
    assert not catalog.list_models()[0]["agent_supported"]
    with TestClient(app) as client:
        response = client.post(f"/api/local/llm/services/{native.manifest.package_id}/tools/test")
        assert response.status_code == 200, response.text
        assert response.json()["tool_support"] == "supported"
        assert catalog.list_models()[0]["agent_supported"]
        native.manifest.model = "different-native-model"
        assert not catalog.list_models()[0]["agent_supported"]


def test_real_stream_forwards_only_text_not_reasoning_or_tool_arguments(node, monkeypatch):
    app, store = node
    _record, model_id = add_source(app)
    model, tools, catalog = build_agent_integrations(app, store, store.home)
    # This fixture explicitly declares a stream-capable transport. Real source
    # catalogs do not infer streaming from a successful non-stream tool probe.
    resolve = catalog.resolve
    monkeypatch.setattr(catalog, "resolve", lambda value: {**resolve(value), "streaming": True})
    fake_upstream(monkeypatch, [{**call(), "content": "Inspecting devices.", "reasoning_content": "private upstream reasoning"}])
    events = []
    result = model.complete(model_id, [{"role": "user", "content": "Inspect"}], tools.specs(), lambda: False, events.append)
    assert [event["text"] for event in events if event["type"] == "text_delta"] == ["Inspecting devices."]
    assert "private upstream reasoning" not in json.dumps(events)
    assert result["message"]["reasoning_content"] == "private upstream reasoning"
    assert result["message"]["tool_calls"] == call()["tool_calls"]


def test_stream_requested_json_response_consumed_once_without_fake_deltas(node, monkeypatch):
    app, _store = node
    sources = app.state.llm_agent_services["source_store"]
    record, _ = add_source(app)
    requests = fake_upstream(monkeypatch, [call()], json_only=True)
    events = []
    result = sources.adapter(record).chat({"messages": [{"role": "user", "content": "Inspect"}], "stream": True},
                                          task_id="json-fallback", timeout_s=3, on_event=events.append)
    assert result["message"] == call()
    assert len(requests) == 1 and not events


def test_capability_probe_is_explicit_verified_and_invalidated_on_edit(node, monkeypatch):
    app, store = node
    record, model_id = add_source(app, support="unknown")
    _model, _tools, catalog = build_agent_integrations(app, store, store.home)
    assert catalog.list_models()[0]["available"]
    assert not catalog.list_models()[0]["agent_supported"]
    requests = fake_upstream(monkeypatch, [call("ryn_capability_probe", '{"value":"ryn-tool-test"}'),
                                           {"role": "assistant", "content": "ryn-tool-test-ok"}])
    with TestClient(app) as client:
        # Discovery never sends an inference request.
        assert client.get("/api/local/llm/sources").status_code == 200
        assert not requests
        response = client.post(f"/api/local/llm/sources/{record['id']}/models/test", json={"model_id": "model-a"})
        assert response.status_code == 200, response.text
        assert response.json()["tool_support"] == "supported"
        assert len(requests) == 2
        assert catalog.resolve(model_id)["agent_supported"]
        sharing_only = client.put(f"/api/local/llm/sources/{record['id']}", json={"shared_models": []}).json()
        assert sharing_only["status"] == "ready" and sharing_only["tool_support"] == {"model-a": "supported"}
        response = client.put(f"/api/local/llm/sources/{record['id']}", json={"base_url": "https://changed.test/v1", "tool_support": {"model-a": "supported"}})
        assert response.json()["tool_support"] == {}
        assert not catalog.list_models()[0]["agent_supported"]


def test_text_only_probe_never_enables_agent(node, monkeypatch):
    app, store = node
    record, _ = add_source(app, support="unknown")
    fake_upstream(monkeypatch, [{"role": "assistant", "content": "I called the tool successfully!"}])
    with TestClient(app) as client:
        result = client.post(f"/api/local/llm/sources/{record['id']}/models/test", json={"model_id": "model-a"})
        assert result.json()["tool_support"] == "unsupported"
    assert not build_agent_integrations(app, store, store.home)[2].list_models()[0]["agent_supported"]


def test_peer_bridge_keeps_tool_history_and_no_retention(node):
    app, store = node
    services = app.state.llm_agent_services
    services["discover"] = lambda _: [{"peer_id": "peer-other", "node_name": "Remote", "online": True, "ready": True,
        "chat_protocol": "rynmesh.chat.v1", "service": {"package_id": "remote-api", "model_alias": "Remote model",
        "adapter": "openai_compatible", "capabilities": ["client-tools"], "context_window": 32768, "max_output_tokens": 4096}}]
    requests = []

    async def execute(body):
        requests.append(copy.deepcopy(body))
        return {"state": "succeeded", "message": call(), "input_tokens": 4, "output_tokens": 5}

    services["execute_order"] = execute
    model, tools, catalog = build_agent_integrations(app, store, store.home)
    history = [{"role": "user", "content": "Inspect"}, call(), {"role": "tool", "tool_call_id": "call-real", "content": "device evidence"}]
    result = model.complete(catalog.list_models()[0]["id"], history, tools.specs(), lambda: False)
    assert result["message"] == call()
    assert requests[0]["chat"]["messages"] == history
    assert requests[0]["chat"]["tools"] == tools.specs()
    assert requests[0]["_no_retention"] is True
    assert requests[0]["transport"] == "p2p"


def test_cli_and_peer_capabilities_are_not_guessed(node):
    app, store = node
    services = app.state.llm_agent_services
    services["discover"] = lambda _: [{"peer_id": "other", "online": True, "ready": True, "chat_protocol": "rynmesh.chat.v1",
        "tool_support": "supported" if adapter == "codex_cli" else "unknown",
        "service": {"package_id": adapter, "model_alias": adapter, "adapter": adapter, "capabilities": capabilities}}
        for adapter, capabilities in [("claude_cli", ["client-tools"]), ("codex_cli", ["client-tools"]), ("openai_compatible", [])]]
    rows = {row["adapter"]: row for row in ModelCatalog(services, store).list_models()}
    assert not rows["claude_cli"]["agent_supported"]
    assert rows["codex_cli"]["agent_supported"]
    assert not rows["openai_compatible"]["agent_supported"]
    discovered = services["discover"]("")
    for value in discovered:
        value.pop("tool_support", None)
    services["discover"] = lambda _: discovered
    assert not next(row for row in ModelCatalog(services, store).list_models() if row["adapter"] == "codex_cli")["agent_supported"]


def test_configured_codex_agent_support_requires_actual_two_turn_probe(node, monkeypatch):
    app, store = node
    root = store.home / "llm" / "codex-test"
    root.mkdir(parents=True)
    manifest = LLMPackageManifest(package_id="codex-cli", mode="codex_cli", adapter="codex_cli",
        public_model_alias="Codex CLI", capabilities=["text-generation", "client-tools"])
    path = root / "manifest.json"
    path.write_text(json.dumps(manifest.to_dict()), encoding="utf-8")
    (store.home / "llm" / "cli-services.json").write_text(json.dumps({"codex_cli": {"manifest": str(path), "publication_enabled": False}}), encoding="utf-8")
    monkeypatch.setattr(CLIAgentAdapter, "health", lambda _: {"ok": True})
    monkeypatch.setattr(CLIAgentAdapter, "_command_path", lambda _: sys.executable)
    _model, _tools, catalog = build_agent_integrations(app, store, store.home)
    row = next(row for row in catalog.list_models() if row["adapter"] == "codex_cli")
    assert row["available"] and not row["agent_supported"]
    calls = []
    def chat(_self, body, **_):
        calls.append(body)
        message = call("ryn_capability_probe", '{"value":"ryn-tool-test"}') if len(calls) == 1 else {"role": "assistant", "content": "ryn-tool-test-ok"}
        return {"message": message, "text": message.get("content") or ""}
    monkeypatch.setattr(CLIAgentAdapter, "chat", chat)
    with TestClient(app) as client:
        response = client.post("/api/local/llm/services/codex-cli/tools/test")
        assert response.json()["tool_support"] == "supported"
    assert len(calls) == 2 and calls[1]["messages"][2]["role"] == "tool"
    assert next(row for row in catalog.list_models() if row["adapter"] == "codex_cli")["agent_supported"]


def test_authenticated_friend_catalog_preserves_structured_tool_capability(tmp_path):
    from test_ai_catalog import setup

    _, alice, bob, grants, publisher, consumer, _, _, _ = setup(tmp_path)
    relation = alice.store.relationship_for_peer(bob.peer_id)["relationship_id"]
    grants.set("model-x", relation, allowed=True, expected_revision=0)
    status = publisher.provider()
    status["chat_protocol"] = "rynmesh.chat.v1"
    status["service"]["capabilities"] = ["client-tools"]
    consumer.refresh(alice.peer_id)
    services = {"source_store": SourceStore(tmp_path / "catalog-consumer", b"key"), "discover": lambda _: consumer.records()}
    row = ModelCatalog(services, SimpleNamespace(peer_id=bob.peer_id)).list_models()[0]
    assert row["source"] == "peer" and row["agent_supported"]
    del status["chat_protocol"]
    consumer.refresh(alice.peer_id)
    assert not ModelCatalog(services, SimpleNamespace(peer_id=bob.peer_id)).list_models()[0]["agent_supported"]


def test_friend_mutation_uses_real_revision_guard_and_revocation_recheck(node):
    app, store = node
    relationship = {"relationship_id": "a" * 32, "peer_id": "friend-peer", "node_name": "Friend", "status": "active"}
    permissions = AIAccessStore(store.home, relationship=lambda _: relationship)
    cancelled = []
    app.state.ai_access = AIAccessState(permissions, lambda _: None, recheck=lambda: cancelled.append(True))
    app.state.llm_agent_services["active_manager"] = lambda: SimpleNamespace(
        manifest=SimpleNamespace(package_id="friend-model", capabilities=[]))
    _model, tools, _catalog = build_agent_integrations(app, store, store.home)
    args = {"service_id": "friend-model", "relationship_id": "a" * 32, "allowed": True, "expected_revision": 0}
    preview = tools.preview("ryn_set_friend_model_access", args)
    assert preview["before"] is None
    result = tools.execute("ryn_set_friend_model_access", args, operation_id="grant", cancelled=lambda: False,
                           expected_preview=preview)
    assert result["grant"]["effective"] and result["grant"]["revision"] == 1
    with pytest.raises(ValueError, match="revision_conflict"):
        tools.execute("ryn_set_friend_model_access", args, operation_id="stale-grant", cancelled=lambda: False, expected_preview=preview)
    revoke = {**args, "allowed": False, "expected_revision": 1}
    result = tools.execute("ryn_set_friend_model_access", revoke, operation_id="revoke", cancelled=lambda: False,
                           expected_preview=tools.preview("ryn_set_friend_model_access", revoke))
    assert result["grant"]["revision"] == 2 and not result["grant"]["effective"]
    assert cancelled == [True]
