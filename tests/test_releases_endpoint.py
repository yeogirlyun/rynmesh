from fastapi.testclient import TestClient

from rynmesh.registry_http import create_app


def test_releases_post_then_get_latest(tmp_path, monkeypatch):
    monkeypatch.setenv("RYNMESH_HOME", str(tmp_path))
    client = TestClient(create_app())
    manifest = {"alg": "ed25519", "public_key": "PUB", "signature": "SIG",
                "payload": {"kind": "rynmesh.release", "version": "0.3.0", "wheel_sha256": "abc"}}
    r = client.post("/api/v1/releases", json=manifest)
    assert r.status_code == 200
    g = client.get("/api/v1/releases/latest")
    assert g.status_code == 200
    assert g.json()["payload"]["version"] == "0.3.0"


def test_releases_get_latest_empty(tmp_path, monkeypatch):
    monkeypatch.setenv("RYNMESH_HOME", str(tmp_path))
    client = TestClient(create_app())
    assert client.get("/api/v1/releases/latest").json() == {}


def test_releases_post_rejects_non_manifest(tmp_path, monkeypatch):
    monkeypatch.setenv("RYNMESH_HOME", str(tmp_path))
    client = TestClient(create_app())
    assert client.post("/api/v1/releases", json={"nope": 1}).status_code == 400
