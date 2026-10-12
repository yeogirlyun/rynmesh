"""No owner credentials and no vendor API in these development-harness checks."""
from __future__ import annotations

import asyncio
import base64
import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from node import Audit, RestrictedAdapter, WireAudit, build, digest, install_provider
from probe import probe

from rynmesh.llm_package import model_settings
from rynmesh.llm_package.model_dispatch import ModelDispatchAdapter
from rynmesh.llm_package.sources import SourceStore
from rynmesh.llm_package.task_protocol import seal_task
from rynmesh.services import peer_box
from rynmesh.store import RynmeshStore


def test_source_mapping_has_no_secret_or_unearned_tool_support(tmp_path, monkeypatch):
    owner = RynmeshStore(home=tmp_path / "owner", network_dir=tmp_path / "network")
    sources = SourceStore(owner.home, owner.private_key_bytes)
    record = sources.prepare({"name": "synthetic configured owner", "kind": "api",
        "base_url": "https://example.invalid", "api_key": "TEST_SECRET_NEVER_EXPORT",
        "models": ["glm-5.3-flash"]}, "src-7de9fb1d36c44bc8")
    record["status"] = "ready"
    sources.save(record)
    # Production identity file format, synthetic freshly generated key only.
    (owner.home / "identity.ed25519").write_text(base64.b64encode(owner.private_key_bytes).decode())
    isolated = RynmeshStore(home=tmp_path / "provider", network_dir=tmp_path / "network")
    target = SourceStore(isolated.home, isolated.private_key_bytes)
    cached = {}
    def managers():
        for saved in target.read().values():
            for model in saved["models"]:
                sid = target.model_service_id(saved, model)
                revision = model_settings.fingerprint(saved, model)
                if sid not in cached or cached[sid]._source_revision != revision:
                    cached[sid] = SimpleNamespace(_source_revision=revision,
                        adapter=ModelDispatchAdapter(target, saved, model, target.adapter(saved, model)))
        return dict(cached)
    app = SimpleNamespace(state=SimpleNamespace(llm_agent_services={
        "source_store": target, "active_source_managers": managers}))
    monkeypatch.setattr(SourceStore, "adapter", staticmethod(lambda *args: SimpleNamespace()))
    before = digest(sources.path)
    result = install_provider(app, SimpleNamespace(owner_home=owner.home, home=isolated.home), Audit(tmp_path / "audit"))
    mapped = target.read()["src-two-host-glm"]
    assert digest(sources.path) == before
    assert mapped["tool_support"] == {}
    assert mapped["api_key"] == ""
    assert "TEST_SECRET_NEVER_EXPORT" not in json.dumps(mapped)
    assert result["tool_support_basis"] == "isolated_production_probe_required"
    assert not (tmp_path / "audit").exists()  # Startup dispatches nothing.
    assert target.adapter(mapped, "private-boundary-canary").allowed is False
    # Simulate create_app's cached managers after restarting with saved sources.
    for manager in cached.values():
        manager.adapter.inner = SimpleNamespace()
    install_provider(app, SimpleNamespace(owner_home=owner.home, home=isolated.home), Audit(tmp_path / "audit"))
    assert all(isinstance(manager.adapter.inner, RestrictedAdapter) for manager in cached.values())


def test_each_dispatch_rechecks_owner_and_forbids_other_model(tmp_path):
    source = tmp_path / "sources.enc"
    source.write_bytes(b"ciphertext")
    calls = []
    adapter = RestrictedAdapter(SimpleNamespace(chat=lambda *a, **k: calls.append(a) or {"text": "ok"}),
        source, digest(source), Audit(tmp_path / "audit"))
    assert adapter.chat({"messages": []}) == {"text": "ok"}
    adapter.allowed = False
    with pytest.raises(RuntimeError, match="other_model_dispatch_forbidden"):
        adapter.chat({})
    adapter.allowed = True
    source.write_bytes(b"changed")
    with pytest.raises(RuntimeError, match="owner_source_changed"):
        adapter.chat({})
    assert len(calls) == 1


def test_owner_change_during_dispatch_detected(tmp_path):
    source = tmp_path / "sources.enc"
    source.write_bytes(b"before")
    def inner(*args, **kwargs):
        source.write_bytes(b"after")
        return {"text": "ok"}
    adapter = RestrictedAdapter(SimpleNamespace(chat=inner), source, digest(source), Audit(tmp_path / "audit"))
    with pytest.raises(RuntimeError, match="owner_source_changed"):
        adapter.chat({})


def test_cancel_preserves_production_inner_result(tmp_path):
    calls = []
    adapter = RestrictedAdapter(SimpleNamespace(cancel=lambda task_id: calls.append(task_id) or True),
        tmp_path / "unused", "unused", Audit(tmp_path / "audit"))
    assert adapter.cancel("actual-inference-task") is True
    assert calls == ["actual-inference-task"]
    assert json.loads((tmp_path / "audit").read_text())["kind"] == "upstream_cancel"


@pytest.mark.parametrize("mismatch", [None, "signer", "task", "service"])
def test_probe_authenticates_selected_provider_task_and_service(tmp_path, monkeypatch, mismatch):
    consumer = RynmeshStore(home=tmp_path / "consumer", network_dir=tmp_path / "network")
    provider = RynmeshStore(home=tmp_path / "provider", network_dir=tmp_path / "network")
    other = RynmeshStore(home=tmp_path / "other", network_dir=tmp_path / "network")
    consumer_key = peer_box.load_or_create_messaging_key(consumer.home / "messaging.x25519")
    provider_key = peer_box.load_or_create_messaging_key(provider.home / "messaging.x25519")
    def response(_url, **kwargs):
        task_id = kwargs["json"]["payload"]["task_id"] if mismatch != "task" else "another-task"
        signer = other if mismatch == "signer" else provider
        body = {"task_id": task_id, "service_id": "another-service" if mismatch == "service" else "test-service",
                "state": "rejected", "error_code": "ai_permission_denied"}
        wire = seal_task(body=body, task_id=task_id, kind="llm_response", sender_peer_id=signer.peer_id,
            recipient_peer_id=consumer.peer_id, sender_signing_key=signer.private_key_bytes,
            recipient_messaging_pub=peer_box.public_key_b64(consumer_key),
            expires_at=(datetime.now(timezone.utc) + timedelta(minutes=2)).isoformat()).to_dict()
        return SimpleNamespace(status_code=200, text=json.dumps(wire), json=lambda: wire)
    monkeypatch.setattr("probe.httpx.post", response)
    result = probe(consumer.home, {"peer_id": provider.peer_id, "messaging_pub": peer_box.public_key_b64(provider_key),
                   "endpoint": "http://synthetic.invalid"}, "test-service", {"relationship_id": "test", "revision": 1})
    assert ("authenticated_response" in result) == (mismatch is None)
    if mismatch is not None:
        assert result["error_type"] == "TaskProtocolError"


@pytest.mark.parametrize("path,client,expected", [
    ("/api/local/agent/settings", "192.0.2.2", 403),
    ("/api/peer/health", "192.0.2.3", 403),
    ("/api/peer/health", "192.0.2.2", 204),
    ("/api/local/agent/settings", "127.0.0.1", 204)])
def test_network_boundary_is_extra_test_wrapper(tmp_path, path, client, expected):
    sent = []
    async def app(scope, receive, send):
        await receive()
        await send({"type": "http.response.start", "status": 204})
        await send({"type": "http.response.body", "body": b""})
    async def receive():
        return {"type": "http.request", "body": b"{}"}
    async def send(item):
        sent.append(item)
    wrapper = WireAudit(app, Audit(tmp_path / "audit"), "synthetic", None, "192.0.2.2")
    asyncio.run(wrapper({"type": "http", "path": path, "client": (client, 50000)}, receive, send))
    assert sent[0]["status"] == expected


def test_consumer_bootstrap_snapshot_no_sources_or_tasks(tmp_path):
    args = SimpleNamespace(home=tmp_path / "consumer", role="consumer", bind="127.0.0.1", port=18889,
                           allowed_peer=None)
    app = build(args)
    token = (args.home / "two-host-control-token").read_text()
    with TestClient(app) as client:
        response = client.get("/api/local/two-host/evidence", headers={"X-Ryn-Local-Token": token})
        assert response.status_code == 200
        evidence = response.json()
        assert evidence["tasks"] == evidence["sources"] == []
        assert evidence["runtime_consumer_node_id"] == evidence["identity"]["peer_id"]
        assert client.get("/api/local/two-host/evidence").status_code == 403
