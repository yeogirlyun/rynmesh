"""Independent no-network regressions for the two-host development harness."""
import base64
import importlib.util
import json as jsonlib
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey

from rynmesh.crypto import public_key_from_private
from rynmesh.llm_package.model_dispatch import ModelDispatchAdapter
from rynmesh.llm_package.routes import ProviderService
from rynmesh.llm_package.task_protocol import seal_task
from rynmesh.services import peer_box

HERE = Path(__file__).resolve().parents[1] / "docs/acceptance/ryn-agent-delivery/two-host"


@pytest.fixture
def modules(monkeypatch):
    loaded = {}
    for name in ("node", "probe"):
        spec = importlib.util.spec_from_file_location(name, HERE / (name + ".py"))
        module = importlib.util.module_from_spec(spec)
        monkeypatch.setitem(sys.modules, name, module)
        spec.loader.exec_module(module)
        loaded[name] = module
    return loaded


def test_restricted_adapter_preserves_real_provider_cancel(modules):
    calls = []
    wrapper = modules["node"].RestrictedAdapter(
        SimpleNamespace(cancel=lambda task: calls.append(task) or True), Path("unused"), "unused",
        SimpleNamespace(add=lambda *args, **kwargs: None))
    gate = object.__new__(ModelDispatchAdapter)
    gate.inner = wrapper
    provider = object.__new__(ProviderService)
    provider.adapter = gate
    provider.task_store = SimpleNamespace(get=lambda task: None)
    assert provider.cancel("synthetic-cancel-task") is True
    assert calls == ["synthetic-cancel-task"]


@pytest.mark.parametrize("wrong", ["sender", "task", "service", None])
def test_negative_probe_rejects_other_peer_or_other_request_response(modules, tmp_path, monkeypatch, wrong):
    consumer = Ed25519PrivateKey.generate().private_bytes_raw()
    provider = Ed25519PrivateKey.generate().private_bytes_raw()
    stranger = Ed25519PrivateKey.generate().private_bytes_raw()
    consumer_messaging, provider_messaging = X25519PrivateKey.generate(), X25519PrivateKey.generate()
    (tmp_path / "identity.ed25519").write_text(base64.b64encode(consumer).decode())
    (tmp_path / "messaging.x25519").write_bytes(consumer_messaging.private_bytes_raw())
    identity = {"peer_id": public_key_from_private(provider), "messaging_pub": peer_box.public_key_b64(provider_messaging),
                "endpoint": "http://synthetic.invalid:18781"}

    def post(url, *, json: dict, **kwargs):
        assert url == identity["endpoint"] + "/api/peer/llm/tasks"
        task_id = "unrelated-task" if wrong == "task" else json["payload"]["task_id"]
        sender = stranger if wrong == "sender" else provider
        body = {"task_id": task_id, "state": "rejected", "error_code": "ai_permission_denied",
                "service_id": "other-model" if wrong == "service" else "synthetic-model"}
        wire = seal_task(body=body, task_id=task_id, kind="llm_response",
                         sender_peer_id=public_key_from_private(sender), recipient_peer_id=public_key_from_private(consumer),
                         sender_signing_key=sender, recipient_messaging_pub=peer_box.public_key_b64(consumer_messaging),
                         expires_at=(datetime.now(timezone.utc) + timedelta(minutes=5)).isoformat()).to_dict()
        return SimpleNamespace(status_code=200, text=jsonlib.dumps(wire), json=lambda: wire)

    monkeypatch.setattr(modules["probe"].httpx, "post", post)
    result = modules["probe"].probe(tmp_path, identity, "synthetic-model", {"relationship_id": "test", "revision": 1})
    if wrong:
        assert "authenticated_response" not in result, jsonlib.dumps({"accepted_wrong": wrong})
        assert result.get("error_type") == "TaskProtocolError"
    else:
        assert result["authenticated_response"]["error_code"] == "ai_permission_denied"
        assert result.get("error_type") is None
