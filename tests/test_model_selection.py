"""Reviewing and selecting the local model (the settings picker's contract)."""
from __future__ import annotations

from fastapi.testclient import TestClient

from rynmesh.peer_http import create_app
from rynmesh.services import model_provider
from rynmesh.store import RynmeshStore

TAGS = {
    "models": [
        {"name": "qwen3.6:35b", "size": 23_000_000_000, "modified_at": "2026-07-20T00:00:00Z"},
        {"name": "gemma3:4b", "size": 3_300_000_000, "modified_at": "2026-04-01T00:00:00Z"},
    ]
}


def _http(url, payload, timeout_s):
    if url.endswith("/api/tags"):
        return TAGS
    if url.endswith("/api/generate"):
        return {"response": "ok", "model": payload["model"]}
    raise AssertionError(url)


def _client(tmp_path, monkeypatch):
    monkeypatch.setenv("RYNMESH_HOME", str(tmp_path / "node"))
    monkeypatch.setenv("RYNMESH_AUTO_REGISTER", "0")
    monkeypatch.setenv("RYNMESH_ALLOW_REMOTE_CONTROL", "1")
    monkeypatch.delenv("RYNMESH_MODEL_PROVIDER", raising=False)
    monkeypatch.delenv("RYNMESH_OLLAMA_MODEL", raising=False)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    # Providers resolve the HTTP fetcher at call time, so this reaches
    # every construction path (resolver, catalog, and validation).
    monkeypatch.setattr(model_provider, "_http_json", _http)
    return TestClient(create_app(RynmeshStore()))


def test_catalog_lists_installed_and_recommended(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    body = client.get("/api/local/ai/models").json()
    assert [m["name"] for m in body["installed"]] == ["qwen3.6:35b", "gemma3:4b"]
    assert body["ollama_running"] is True
    by_name = {m["name"]: m for m in body["recommended"]}
    assert by_name["gemma3:4b"]["installed"] is True       # present locally
    assert by_name["llama3.2:3b"]["installed"] is False    # offer an ollama pull
    assert body["selected"] == ""                          # nothing chosen yet


def test_default_picks_first_installed_then_selection_wins(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    # No choice yet: falls back to Ollama's list order (the old surprising default).
    assert client.get("/api/local/ai/status").json()["model"] == "qwen3.6:35b"

    picked = client.post("/api/local/ai/model", json={"model": "gemma3:4b"}).json()
    assert picked["ok"] is True and picked["model"] == "gemma3:4b"
    assert client.get("/api/local/ai/status").json()["model"] == "gemma3:4b"
    assert client.get("/api/local/ai/models").json()["selected"] == "gemma3:4b"


def test_selection_persists_across_restart(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    client.post("/api/local/ai/model", json={"model": "gemma3:4b"})
    # A fresh app over the same RYNMESH_HOME must remember the choice.
    again = TestClient(create_app(RynmeshStore()))
    assert again.get("/api/local/ai/status").json()["model"] == "gemma3:4b"


def test_clearing_selection_returns_to_automatic(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    client.post("/api/local/ai/model", json={"model": "gemma3:4b"})
    cleared = client.post("/api/local/ai/model", json={"model": ""}).json()
    assert cleared["selected"] == ""
    assert client.get("/api/local/ai/status").json()["model"] == "qwen3.6:35b"


def test_rejects_a_model_that_is_not_installed(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    response = client.post("/api/local/ai/model", json={"model": "llama3.2:3b"})
    assert response.status_code == 400
    assert "ollama pull llama3.2:3b" in response.json()["detail"]


def test_uninstalled_selection_falls_back_instead_of_erroring(tmp_path, monkeypatch):
    # The chosen model can disappear from Ollama later; requests must not 404.
    provider = model_provider.OllamaProvider(model="deleted:9b", http=_http)
    assert provider.available() is True
    assert provider.model == "qwen3.6:35b"
