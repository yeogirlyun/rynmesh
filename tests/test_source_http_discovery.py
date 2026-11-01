"""Local programs can discover source creation without guessing update URLs."""
from fastapi import FastAPI
from fastapi.testclient import TestClient

from rynmesh.llm_package.routes import install_llm_routes
from rynmesh.services import peer_box
from rynmesh.store import RynmeshStore


def test_discovered_create_contract_and_unknown_update_are_distinct(tmp_path):
    home = tmp_path / "node"
    store = RynmeshStore(home=home, network_dir=tmp_path / "network")
    app = FastAPI()
    install_llm_routes(app, store=store, home=home,
                       messaging_key=peer_box.load_or_create_messaging_key(home / "messaging.x25519"),
                       resolve_endpoint=lambda _: "", resolve_pubkey=lambda _: "")
    with TestClient(app) as client:
        operation = client.get("/openapi.json").json()["paths"]["/api/local/llm/sources/new"]["put"]
        schema = operation["requestBody"]["content"]["application/json"]["schema"]
        assert schema["properties"]["api_key"]["writeOnly"] is True
        body = {"name": "Local program fixture", "kind": "api", "base_url": "https://provider.example/v1",
                "models": ["example-model"], "api_key": "synthetic-source-secret", "sharing": False}
        assert client.put("/api/local/llm/sources/invented-id", json=body).status_code == 404
        assert not client.get("/api/local/llm/sources").json()["sources"]
        created = client.put("/api/local/llm/sources/new", json=body)
        assert created.status_code == 200
        record = created.json()
        assert record["id"].startswith("src-") and record["has_key"]
        assert "api_key" not in record and "synthetic-source-secret" not in created.text
        assert not record["sharing"] and record["shared_models"] == []
        assert client.put("/api/local/llm/sources/" + record["id"], json={"name": "Updated"}).status_code == 200
