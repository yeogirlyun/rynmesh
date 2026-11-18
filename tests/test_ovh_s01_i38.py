"""S01-I38: production bridge, durable retry, cancellation and diagnostic paths."""
import json
import threading
from types import SimpleNamespace

import pytest

from test_agent_integrations import add_source, node as node
from rynmesh.agent_runtime import budget, diagnostic_log, diagnostics, retry_policy
from rynmesh.agent_runtime.bridge import build_agent_integrations
from rynmesh.agent_runtime.models import ModelCatalog
from rynmesh.agent_runtime.runtime import AgentRuntime
from rynmesh.agent_runtime.store import AgentError, AgentStore
from rynmesh.llm_package.adapters import AdapterError
from rynmesh.services import peer_box


@pytest.fixture
def live(node):
    app, store = node
    _, model_id = add_source(app)
    bridge, tools, catalog = build_agent_integrations(app, store, store.home)
    key = peer_box.load_or_create_messaging_key(store.home / "messaging.x25519")
    runtime = AgentRuntime(AgentStore(store.home / "i38", key), bridge, tools, catalog)
    runtime.update_settings({"enabled": True, "model_id": model_id})
    app.state.agent_runtime = runtime
    manager = app.state.llm_agent_services["provider_for"](model_id[6:])
    yield runtime, bridge, manager, catalog, app, model_id
    tools.local.processes.close()


def reply():
    return {"message": {"role": "assistant", "content": "Recovered."}, "finish_reason": "stop",
            "reported_usage": {"input_tokens": 2, "output_tokens": 1}}


def task_diagnostics(live, task_id):
    runtime, _, _, catalog, app, _ = live
    return diagnostics.DiagnosticTools(app, catalog, None).execute(
        "ryn_diagnose_current_task", {}, context={"task_id": task_id}, cancelled=lambda: False)


@pytest.mark.parametrize("adapter_code", ["model_not_ready", "runtime_unavailable"])
def test_t1_two_starting_failures_retry_then_complete(live, monkeypatch, adapter_code):
    runtime, _, manager, _, _, _ = live
    clock, calls = [1000.0], []
    monkeypatch.setattr(retry_policy, "now", lambda: clock[0])

    def chat(*args, **kwargs):
        calls.append(kwargs["task_id"])
        if len(calls) <= 2:
            raise AdapterError("synthetic starting", code=adapter_code)
        return reply()

    monkeypatch.setattr(manager.adapter, "chat", chat)
    row = runtime.create({"message": "Check readiness."})
    for count in (1, 2):
        runtime.run_once()
        current = runtime.get(row["id"])
        assert current["status"] == "queued" and current["error"] is None
        recovery = current["connection_recovery"]
        assert recovery["error_code"] == "agent_model_starting"
        assert recovery["retry_count"] == count and recovery["max_retries"] == 6
        assert recovery["next_retry_at"] == clock[0] + 5
        runtime.run_once()
        assert len(calls) == count
        clock[0] += 5
    runtime.run_once()
    done = runtime.get(row["id"])
    assert done["status"] == "completed" and done["result"] == "Recovered."
    assert len(calls) == 3
    assert runtime.store.read()["tasks"][row["id"]]["model_retry_count"] == 0
    assert len([event for event in done["events"] if event["type"] == "model_retry_scheduled"]) == 2


def test_t2_starting_exhaustion_is_unavailable_after_thirty_seconds(live, monkeypatch):
    runtime, _, manager, _, _, _ = live
    clock, calls = [1000.0], []
    monkeypatch.setattr(retry_policy, "now", lambda: clock[0])

    def chat(*args, **kwargs):
        calls.append(1)
        raise AdapterError("synthetic starting", code="model_not_ready")

    monkeypatch.setattr(manager.adapter, "chat", chat)
    row = runtime.create({"message": "Wait for this model only."})
    for count in range(7):
        runtime.run_once()
        current = runtime.get(row["id"])
        if count < 6:
            assert current["status"] == "queued"
            assert current["connection_recovery"]["next_retry_at"] == clock[0] + 5
            clock[0] += 5
    done = runtime.get(row["id"])
    assert len(calls) == 7 and clock[0] == 1030
    assert done["status"] == "failed" and done["error"] == "agent_model_unavailable"
    assert not done.get("connection_recovery")
    assert "检查本机模型服务是否已启动" in task_diagnostics(live, row["id"])["suggestions"][0]["instruction"]
    assert retry_policy.limit("agent_model_starting", {}) == 6
    assert retry_policy.delay(6, {"retry_after_seconds": 100}, "agent_model_starting") == 5


@pytest.mark.parametrize("state", ["starting", "installing"])
@pytest.mark.parametrize("advertised", [False, True])
def test_missing_manager_uses_real_native_status_from_build(node, monkeypatch, state, advertised):
    app, store = node
    native = app.state.native_model
    with native.lock:
        native.state = state
    service_id = native.manifest.package_id
    services = app.state.llm_agent_services
    assert services["provider_for"](service_id) is None
    if advertised:
        monkeypatch.setitem(services, "discover_local_model", lambda *args: [{
            "service": {**native.manifest.public_dict(), "capabilities": ["client-tools"]},
            "peer_id": store.peer_id, "online": True, "ready": True, "chat_protocol": "rynmesh.chat.v1",
        }])
    bridge, _, _ = build_agent_integrations(app, store, store.home)
    with pytest.raises(ValueError, match="^agent_model_starting$"):
        bridge.complete("local/" + service_id, [{"role": "user", "content": "Wait."}], [], lambda: False)


def test_t3_cancel_failure_is_logged_and_persisted_in_request_policy(live, monkeypatch, caplog):
    runtime, _, manager, _, _, _ = live
    recorded = threading.Event()
    original = budget.record_request_policy

    def observe(row, policy):
        original(row, policy)
        if policy.get("cancel_failed") is True:
            recorded.set()

    monkeypatch.setattr(budget, "record_request_policy", observe)
    row = runtime.create({"message": "Cancelable request."})

    def cancel(request_id):
        raise RuntimeError("PRIVATE_CANCEL_TOKEN=https://secret.example")

    def chat(*args, **kwargs):
        current = runtime.get(row["id"])
        runtime.action(row["id"], {"action": "cancel", "expected_revision": current["revision"]})
        assert recorded.wait(10), "cancel failure must reach the real budget policy recorder"
        return reply()

    monkeypatch.setattr(manager.adapter, "cancel", cancel)
    monkeypatch.setattr(manager.adapter, "chat", chat)
    runtime.run_once()
    saved = runtime.store.read()["tasks"][row["id"]]
    assert saved["status"] == "cancelled"
    assert any(record.get("request_policy", {}).get("cancel_failed") is True
               for record in saved["budget"]["records"])
    assert "stage=inference_cancel" in caplog.text and "RuntimeError" in caplog.text
    assert "PRIVATE_CANCEL_TOKEN" not in caplog.text and "secret.example" not in caplog.text


@pytest.mark.parametrize("code", ["agent_model_starting", "agent_model_peer_unreachable",
                                  "agent_model_output_limit_reached", "agent_model_image_rejected",
                                  "agent_model_quota_exhausted"])
def test_t4_specific_error_guidance_exists(code):
    key, instruction, path = diagnostics._ERROR_GUIDANCE[code]
    assert key != "review_task" and len(instruction) > 10 and path.startswith("/")
    if code == "agent_model_quota_exhausted":
        assert "该模型今日额度已用完" in instruction and "额度重置" in instruction
    if code == "agent_model_image_rejected":
        assert "所选模型不接受图片" in instruction and "去掉图片" in instruction


def test_t4_quota_reset_timestamp_reaches_diagnostic_guidance_without_switching(live, monkeypatch):
    runtime, _, manager, _, _, model_id = live
    calls = []
    runtime.update_settings({"permission_mode": "automatic"})

    def chat(*args, **kwargs):
        calls.append(1)
        raise AdapterError("synthetic quota", code="model_daily_quota_exhausted",
                           diagnostics={"network_reason": "quota", "reset_at": 1800000000})

    monkeypatch.setattr(manager.adapter, "chat", chat)
    row = runtime.create({"message": "Use the selected model."})
    runtime.run_once()
    done = runtime.get(row["id"])
    assert done["status"] == "failed" and done["error"] == "agent_model_quota_exhausted"
    guidance = task_diagnostics(live, row["id"])
    assert guidance["reset_at"] == 1800000000
    assert guidance["suggestions"][0]["reset_at"] == 1800000000
    assert done["model_id"] == model_id and len(calls) == 1
    assert diagnostic_log.safe_diagnostics({"reset_at": "PRIVATE", "prompt": "PRIVATE"}) == {}


def test_t5_discovery_failure_is_coded_and_diagnostic_body_free(monkeypatch, caplog):
    def discover(network):
        raise RuntimeError("PRIVATE_DISCOVERY_URL=https://secret.example/?token=private")

    catalog = ModelCatalog({"discover": discover}, SimpleNamespace(peer_id="self"))
    with pytest.raises(ValueError, match="^agent_model_unavailable$"):
        catalog.refresh_destination("peer/fixture/service")
    assert "stage=discovery_failed" in caplog.text and "RuntimeError" in caplog.text
    assert "PRIVATE_DISCOVERY_URL" not in caplog.text and "secret.example" not in caplog.text


def test_t6_internal_type_error_is_not_retried_as_full_enumeration(monkeypatch):
    catalog = ModelCatalog({}, SimpleNamespace(peer_id="self"))
    calls = []

    def listing(*, selected_model_id=None):
        calls.append(selected_model_id)
        if selected_model_id is not None:
            raise TypeError("synthetic implementation bug")
        return [{"id": "local/fixture"}]

    monkeypatch.setattr(catalog, "list_models", listing)
    with pytest.raises(TypeError, match="synthetic implementation bug"):
        catalog.get_model("local/fixture")
    assert calls == ["local/fixture"]
