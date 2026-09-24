import json
from contextlib import contextmanager

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from rynmesh.llm_package.routes import install_llm_routes
from rynmesh.llm_package.sources import SourceAdapter, SourceStore
from rynmesh.services import peer_box
from rynmesh.store import RynmeshStore


def source(**updates):
    return {"name": "GLM", "kind": "api", "provider": "zai", "base_url": "https://api.z.ai/api/paas/v4",
            "models": ["glm-5.3-flash"], "context_window": 1000000, "max_output_tokens": 128000,
            "request_defaults": {"thinking": {"type": "enabled"}, "reasoning_effort": "max"}, **updates}


def test_source_secrets_encrypted_preserved_and_cleared_on_endpoint_change(tmp_path):
    store = SourceStore(tmp_path, b"test-private-key")
    record = store.prepare(source(api_key="secret-123"))
    public = store.save(record)
    assert public["has_key"] and "api_key" not in public
    assert b"secret-123" not in store.path.read_bytes()
    assert SourceStore(tmp_path, b"test-private-key").read()[record["id"]]["api_key"] == "secret-123"
    assert store.prepare(source(api_key=""), record["id"])["api_key"] == "secret-123"
    assert not store.prepare(source(base_url="https://other.example/v1"), record["id"])["api_key"]
    assert not store.prepare(source(clear_key=True), record["id"])["api_key"]
    store.delete(record["id"])
    assert not store.read()


@pytest.mark.parametrize("updates", [{"base_url": "http://cloud.example/v1"}, {"base_url": "https://user:secret@cloud.example/v1"},
                                      {"models": "bad"}, {"request_defaults": {"messages": []}}, {"max_output_tokens": 2000001}])
def test_invalid_source_configuration(tmp_path, updates):
    with pytest.raises(ValueError if "base_url" not in updates else Exception):
        SourceStore(tmp_path, b"key").prepare(source(**updates))


def test_vendor_path_and_parameters_are_not_rewritten(monkeypatch):
    requests = []
    class Response:
        def read(self, _limit):
            return json.dumps({"choices": [{"message": {"role": "assistant", "content": "ok"}, "finish_reason": "stop"}],
                               "usage": {"prompt_tokens": 2, "completion_tokens": 1}}).encode()
    @contextmanager
    def open_request(request, **_):
        requests.append(request)
        yield Response()
    monkeypatch.setattr("urllib.request.urlopen", open_request)
    adapter = SourceAdapter(base_url="https://api.z.ai/api/paas/v4", api_prefix="", api_key="secret-test",
                            allow_non_loopback=True, model="glm-5.3-flash", request_defaults={"thinking": {"type": "enabled"}})
    assert adapter.infer(prompt="hi", max_tokens=4096, task_id="test", timeout_s=2)["text"] == "ok"
    request = requests[0]
    assert request.full_url == "https://api.z.ai/api/paas/v4/chat/completions"
    assert request.headers["Authorization"] == "Bearer secret-test"
    assert json.loads(request.data)["thinking"] == {"type": "enabled"}


def test_source_draft_test_catalog_chat_and_api(tmp_path, monkeypatch):
    home = tmp_path / "node"
    store = RynmeshStore(home=home, network_dir=tmp_path / "network")
    app = FastAPI()
    install_llm_routes(app, store=store, home=home,
        messaging_key=peer_box.load_or_create_messaging_key(home / "messaging.x25519"),
        resolve_endpoint=lambda _: "", resolve_pubkey=lambda _: "")
    monkeypatch.setattr(SourceAdapter, "chat", lambda *_, **__: {
        "text": "source reply", "message": {"role": "assistant", "content": "source reply"},
        "finish_reason": "stop", "input_tokens": 3, "output_tokens": 2, "duration_ms": 1})
    with TestClient(app) as client:
        draft = client.put("/api/local/llm/sources/new", json=source()).json()
        assert draft["status"] == "needs_key" and not draft["has_key"]
        sid = draft["id"]
        assert client.post(f"/api/local/llm/sources/{sid}/test").status_code == 400
        assert not client.get("/api/local/llm/api-access").json()["targets"]
        saved = client.put(f"/api/local/llm/sources/{sid}", json=source(api_key="secret-456")).json()
        assert saved["has_key"] and "api_key" not in saved
        assert client.post(f"/api/local/llm/sources/{sid}/test").status_code == 200
        catalog = client.get("/api/local/llm/api-access").json()
        assert len(catalog["targets"]) == 1
        assert "secret-456" not in json.dumps(catalog)
        service_id = catalog["targets"][0]["rynmesh"]["service_id"]
        result = client.post("/api/local/llm/orders", json={"provider_peer_id": store.peer_id,
            "service_id": service_id, "prompt": "hello", "max_tokens": 40})
        assert result.status_code == 200, result.text
        assert result.json()["output"] == "source reply"
        key = client.post("/api/local/llm/api-keys", json={"name": "test"}).json()["key"]
        result = client.post("/v1/chat/completions", headers={"Authorization": "Bearer " + key},
            json={"model": catalog["targets"][0]["id"], "messages": [{"role": "user", "content": "hello"}]})
        assert result.status_code == 200, result.text
        assert result.json()["choices"][0]["message"]["content"] == "source reply"
        assert client.put(f"/api/local/llm/sources/{sid}", json={"sharing": True}).status_code == 409
        assert client.delete(f"/api/local/llm/sources/{sid}").status_code == 200
        assert not client.get("/api/local/llm/api-access").json()["targets"]
