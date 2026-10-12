"""Local API/storage/dispatch tests; no external inference or owner data."""
import copy
import threading
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from test_agent_integrations import add_source, fake_upstream
from test_agent_integrations import node as integration_node

from rynmesh.agent_runtime.bridge import build_agent_integrations
from rynmesh.agent_runtime.store import AgentError
from rynmesh.llm_package.adapters import AdapterError
from rynmesh.llm_package.model_dispatch import ModelDispatchAdapter
from rynmesh.llm_package.routes import _expires
from rynmesh.llm_package.sources import SourceStore
from rynmesh.llm_package.task_protocol import TaskProtocolError, open_task, seal_task
from rynmesh.services import peer_box
from rynmesh.store import RynmeshStore

node = integration_node


def patch(client, record, model, **config):
    return client.put(f"/api/local/llm/sources/{record['id']}/models/config", json={
        "model_id": model, "expected_rule_revision": record["model_settings"][model]["rule_revision"],
        "configuration": config})


def test_actual_api_per_model_cas_sibling_identity_and_new_private(node):
    app, store = node
    record, _ = add_source(app, models=["same-name", "second"])
    other, _ = add_source(app, models=["same-name", "second"])
    sources = app.state.llm_agent_services["source_store"]
    before = copy.deepcopy(record["model_settings"]["second"])
    manager_b = app.state.llm_agent_services["provider_for"](sources.model_service_id(record, "second"))
    with TestClient(app) as client:
        changed = patch(client, record, "same-name", max_output_tokens=99, request_defaults={"temperature": 0.2}, max_concurrent=2)
        assert changed.status_code == 200, changed.text
        changed = changed.json()
        assert changed["model_settings"]["same-name"]["effective"]["max_output_tokens"] == 99
        assert changed["model_settings"]["second"]["destination_revision"] == before["destination_revision"]
        assert changed["tool_support"] == {"second": "supported"}
        assert patch(client, record, "same-name", max_output_tokens=100).status_code == 409
        assert sources.read()[other["id"]]["model_settings"] == other["model_settings"]
        assert app.state.llm_agent_services["provider_for"](sources.model_service_id(record, "second")) is manager_b
        added = client.put(f"/api/local/llm/sources/{record['id']}", json={"models": ["same-name", "second", "new"]}).json()
        assert "new" not in added["shared_models"]
        assert added["model_settings"]["second"]["destination_revision"] == before["destination_revision"]
        assert added["tool_support"] == {"second": "supported"}
        assert "secret-do-not-export" not in str(added)


def test_source_owner_cas_support_and_stale_sharing_set_cannot_overwrite(node):
    app, _ = node
    record, _ = add_source(app, models=["a", "b"])
    sources = app.state.llm_agent_services["source_store"]
    with TestClient(app) as client:
        assert client.get("/api/local/llm/sources").json()["source_revision_cas_supported"] is True
        configured = patch(client, record, "a", share_scope="friends").json()
        endpoint = f"/api/local/llm/sources/{record['id']}"
        assert client.put(endpoint, json={"shared_models": ["a"], "expected_source_revision": record["source_revision"]}).status_code == 409
        saved = client.put(endpoint, json={"shared_models": ["a"], "expected_source_revision": configured["source_revision"]})
        assert saved.status_code == 200, saved.text
        assert saved.json()["shared_models"] == ["a"]
        assert client.put(endpoint, json={"shared_models": [], "expected_source_revision": configured["source_revision"]}).status_code == 409
        assert sources.read()[record["id"]]["shared_models"] == ["a"]


def test_effective_settings_reach_real_http_adapter_and_quota_shared_with_agent(node, monkeypatch):
    app, store = node
    record, model_id = add_source(app, models=["a", "b"])
    sources = app.state.llm_agent_services["source_store"]
    with TestClient(app) as client:
        configured = patch(client, record, "a", max_output_tokens=30, request_defaults={"temperature": 0.1}, requests_per_day=1).json()
        # Explicit fake probe evidence restores only a's tool capability.
        raw = sources.read()[record["id"]]
        raw["tool_support"]["a"] = "supported"
        sources.save(raw)
        requests = fake_upstream(monkeypatch, [{"role": "assistant", "content": "ok"}], json_only=True)
        response = client.post("/api/local/llm/orders", json={"provider_peer_id": store.peer_id,
            "service_id": sources.model_service_id(record, "a"), "prompt": "Only this request", "max_tokens": 20})
        assert response.status_code == 200 and response.json()["state"] == "succeeded", response.text
        assert requests[0]["model"] == "a" and requests[0]["temperature"] == 0.1 and requests[0]["max_tokens"] == 20
        bridge, _, _ = build_agent_integrations(app, store, store.home)
        with pytest.raises(AgentError, match="agent_model_quota_exhausted"):
            bridge.complete(model_id, [{"role": "user", "content": "must not dispatch"}], [], lambda: False)
        assert len(requests) == 1
        assert configured["model_settings"]["b"]["effective"]["requests_per_day"] is None
        restarted = SourceStore(store.home, store.private_key_bytes)
        class Inner:
            def chat(self, *args, **kwargs):
                pytest.fail("quota cannot reset on restart")
        adapter = ModelDispatchAdapter(restarted, restarted.read()[record["id"]], "a", Inner())
        with pytest.raises(AdapterError, match="model_daily_quota_exhausted"):
            adapter.chat({}, task_id="restart", timeout_s=1)


def test_inflight_model_admission_survives_rule_rebuild_and_sibling_independent(node):
    app, _ = node
    record, _ = add_source(app, models=["a", "b"])
    sources = app.state.llm_agent_services["source_store"]
    entered, release = threading.Event(), threading.Event()
    class Inner:
        def chat(self, body, **kwargs):
            entered.set()
            assert release.wait(3)
            return {"text": "done"}
    first = ModelDispatchAdapter(sources, record, "a", Inner())
    with ThreadPoolExecutor() as pool:
        flight = pool.submit(first.chat, {}, task_id="one", timeout_s=1)
        assert entered.wait(2)
        changed = sources.configure_model(record["id"], {"model_id": "a", "expected_rule_revision": record["model_settings"]["a"]["rule_revision"],
            "configuration": {"requests_per_day": 20}})
        second = ModelDispatchAdapter(sources, sources.read()[changed["id"]], "a", Inner())
        try:
            with pytest.raises(AdapterError, match="runtime_busy"):
                second.chat({}, task_id="two", timeout_s=1)
            with pytest.raises(AdapterError, match="model_configuration_changed"):
                first.chat({}, task_id="stale", timeout_s=1)
            assert sources._active_models.get(sources.model_service_id(record, "b"), 0) == 0
        finally:
            release.set()
        assert flight.result()["text"] == "done"


def test_captured_adapter_cannot_start_after_its_tool_capability_changes(node):
    app, _ = node
    record, _ = add_source(app, models=["a", "b"])
    sources = app.state.llm_agent_services["source_store"]
    calls = []
    class Inner:
        def chat(self, body, **kwargs):
            calls.append(kwargs["task_id"])
            return {"text": "done"}
    old_a = ModelDispatchAdapter(sources, record, "a", Inner())
    sibling_b = ModelDispatchAdapter(sources, record, "b", Inner())
    changed = sources.read()[record["id"]]
    changed["tool_support"]["a"] = "unsupported"
    sources.save(changed)
    assert sources.read()[record["id"]]["model_settings"]["a"]["rule_revision"] == record["model_settings"]["a"]["rule_revision"]
    with pytest.raises(AdapterError, match="model_configuration_changed"):
        old_a.chat({}, task_id="stale-capability", timeout_s=1)
    assert calls == []
    sibling_b.chat({}, task_id="unchanged-sibling", timeout_s=1)
    fresh_a = ModelDispatchAdapter(sources, sources.read()[record["id"]], "a", Inner())
    fresh_a.chat({}, task_id="fresh-chat-only", timeout_s=1)
    assert calls == ["unchanged-sibling", "fresh-chat-only"]


def test_separate_evidence_times_and_parameter_invalidation(node, monkeypatch):
    app, _ = node
    record, _ = add_source(app, models=["a", "b"])
    sources = app.state.llm_agent_services["source_store"]
    monkeypatch.setattr(sources, "probe", lambda _record, **kw: {"models": ["a"]} if kw.get("models_only") else {"ok": True})
    monkeypatch.setattr(sources, "probe_model_tools", lambda _record, _model: {"tool_support": "supported"})
    path = f"/api/local/llm/sources/{record['id']}/models/test"
    with TestClient(app) as client:
        listing = client.post(path, json={"model_id": "a", "probe": "model_exists"}).json()["source"]
        evidence = listing["model_settings"]["a"]["evidence"]
        assert evidence["model_exists"]["status"] == "present" and evidence["model_exists"]["checked_at"]
        assert evidence["connection"]["status"] == "unknown" and evidence["tools"]["status"] == "unknown"
        tested = client.post(path, json={"model_id": "a"}).json()["source"]
        assert tested["model_settings"]["a"]["evidence"]["tools"]["status"] == "passed"
        changed = patch(client, tested, "a", request_defaults={"temperature": 0.7}).json()
        assert changed["model_settings"]["a"]["evidence"]["tools"]["status"] == "unknown"
        assert changed["tool_support"] == {"b": "supported"}
        # Endpoint/key changes invalidate both, including an explicit override.
        edited = client.put(f"/api/local/llm/sources/{record['id']}", json={"api_key": "new-key"}).json()
        assert not edited["tool_support"]
        for model in ("a", "b"):
            assert edited["model_settings"][model]["destination_revision"] != changed["model_settings"][model]["destination_revision"]


def test_inherited_defaults_override_reset_and_legacy_migration(node):
    app, store = node
    record, _ = add_source(app, models=["a", "b"])
    sources = app.state.llm_agent_services["source_store"]
    with TestClient(app) as client:
        overridden = patch(client, record, "a", max_output_tokens=77).json()
        revised = client.put(f"/api/local/llm/sources/{record['id']}", json={"max_output_tokens": 88}).json()
        assert revised["status"] == "ready"
        assert revised["model_settings"]["a"]["effective"]["max_output_tokens"] == 77
        assert revised["model_settings"]["a"]["destination_revision"] == overridden["model_settings"]["a"]["destination_revision"]
        assert revised["model_settings"]["b"]["effective"]["max_output_tokens"] == 88
        reset = patch(client, revised, "a", max_output_tokens=None).json()
        assert reset["model_settings"]["a"]["effective"]["max_output_tokens"] == 88
        assert "max_output_tokens" in reset["model_settings"]["a"]["inherited_fields"]
    raw = sources.read()
    raw[record["id"]].pop("model_settings")
    raw[record["id"]]["request_defaults"] = {"thinking": {"type": "enabled", "budget_tokens": 4096}}
    sources._write(raw)
    legacy_bytes = sources.path.read_bytes()
    readonly = SourceStore(store.home, store.private_key_bytes, migrate=False)
    assert "model_settings" not in readonly.read()[record["id"]]
    assert sources.path.read_bytes() == legacy_bytes
    migrated = SourceStore(store.home, store.private_key_bytes).read()[record["id"]]
    assert set(migrated["model_settings"]) == {"a", "b"}
    assert migrated["source_revision"] == raw[record["id"]]["source_revision"]
    assert migrated["api_key"] == raw[record["id"]]["api_key"]


def test_stream_and_unknown_failure_consume_quota_without_refund(node):
    app, _ = node
    record, _ = add_source(app)
    sources = app.state.llm_agent_services["source_store"]
    sources.configure_model(record["id"], {"model_id": "model-a", "expected_rule_revision": record["model_settings"]["model-a"]["rule_revision"],
        "configuration": {"requests_per_day": 1, "max_output_tokens": 12}})
    calls = []
    class Inner:
        def infer_stream(self, **kwargs):
            calls.append(kwargs)
            raise OSError("transport lost after dispatch")
    adapter = ModelDispatchAdapter(sources, sources.read()[record["id"]], "model-a", Inner())
    with pytest.raises(OSError):
        adapter.infer_stream(max_tokens=999, prompt="hi", on_delta=lambda _: None, task_id="stream", timeout_s=1)
    assert calls[0]["max_tokens"] == 12
    with pytest.raises(AdapterError, match="model_daily_quota_exhausted"):
        adapter.infer_stream(max_tokens=999, prompt="hi", on_delta=lambda _: None, task_id="stream2", timeout_s=1)
    assert len(calls) == 1


@pytest.mark.parametrize("configuration", [{"max_concurrent": 0}, {"requests_per_day": False}, {"max_output_tokens": 999999},
    {"request_defaults": {"messages": []}}, {"request_defaults": {"temperature": 99}}, {"request_defaults": {"reasoning_effort": []}}])
def test_invalid_model_policy_never_saved(node, configuration):
    app, _ = node
    record, _ = add_source(app)
    sources = app.state.llm_agent_services["source_store"]
    before = sources.path.read_bytes()
    with TestClient(app) as client:
        assert patch(client, record, "model-a", **configuration).status_code == 400
    assert sources.path.read_bytes() == before


def test_signed_peer_dispatch_cannot_bypass_model_quota_or_share_revocation(node, monkeypatch, tmp_path):
    app, provider = node
    record, _ = add_source(app, models=["a", "b"])
    sources = app.state.llm_agent_services["source_store"]
    sources.configure_model(record["id"], {"model_id": "a", "expected_rule_revision": record["model_settings"]["a"]["rule_revision"],
        "configuration": {"requests_per_day": 1}})
    record = sources.read()[record["id"]]
    record["shared_models"] = ["a", "b"]
    sources.save(record)
    consumer = RynmeshStore(home=tmp_path / "consumer", network_dir=tmp_path / "network")
    other = RynmeshStore(home=tmp_path / "other", network_dir=tmp_path / "network")
    pkey = peer_box.load_or_create_messaging_key(provider.home / "messaging.x25519")
    ckey = peer_box.load_or_create_messaging_key(consumer.home / "messaging.x25519")
    # Explicit membership fixture; signatures and sealed payloads are real.
    provider.personal_space = SimpleNamespace(enforces_ai=lambda: True, allows_ai=lambda peer: peer == consumer.peer_id)
    service = app.state.llm_agent_services["provider_for"](sources.model_service_id(record, "a"))
    requests = fake_upstream(monkeypatch, [{"role": "assistant", "content": "consumer only"}], json_only=True)
    def request(task_id, sender=consumer):
        return seal_task(body={"task_id": task_id, "service_id": service.manifest.package_id,
            "prompt": "consumer secret, no provider history", "max_tokens": 8, "max_amount": 0.01,
            "reply_messaging_pub": peer_box.public_key_b64(ckey)}, task_id=task_id, kind="llm_request",
            sender_peer_id=sender.peer_id, recipient_peer_id=provider.peer_id, sender_signing_key=sender.private_key_bytes,
            recipient_messaging_pub=peer_box.public_key_b64(pkey), expires_at=_expires(300)).to_dict()
    def unwrap(result):
        return open_task(result, recipient_peer_id=consumer.peer_id, recipient_messaging_key=ckey, expected_kind="llm_response")[1]
    with pytest.raises(TaskProtocolError):
        service.handle(request("denied", other))
    first_request = request("first")
    assert unwrap(service.handle(first_request))["state"] == "succeeded"
    assert unwrap(service.handle(first_request))["state"] == "succeeded"  # same operation is not billed twice
    assert unwrap(service.handle(request("second")))["error_code"] == "model_daily_quota_exhausted"
    assert len(requests) == 1
    record = sources.read()[record["id"]]
    record["shared_models"] = ["b"]
    sources.save(record)
    # An already captured old service instance still cannot dispatch after revoke.
    assert unwrap(service.handle(request("after-revoke")))["error_code"] == "model_configuration_changed"
    assert len(requests) == 1
