"""The packaged node serves its own web UI (one process, one port)."""
from __future__ import annotations

from fastapi.testclient import TestClient

from rynmesh import peer_http
from rynmesh.peer_http import create_app
from rynmesh.store import RynmeshStore


def _client(tmp_path, monkeypatch, *, with_ui: bool):
    monkeypatch.setenv("RYNMESH_HOME", str(tmp_path / "node"))
    monkeypatch.setenv("RYNMESH_AUTO_REGISTER", "0")
    monkeypatch.setenv("RYNMESH_ALLOW_REMOTE_CONTROL", "1")
    monkeypatch.setenv("RYNMESH_MODEL_PROVIDER", "none")
    ui = tmp_path / "webui"
    if with_ui:
        (ui / "assets").mkdir(parents=True)
        (ui / "index.html").write_text("<html><body>ryn app</body></html>", encoding="utf-8")
        (ui / "assets" / "app.js").write_text("console.log(1)", encoding="utf-8")
    monkeypatch.setenv("RYNMESH_WEBUI_DIR", str(ui))
    return TestClient(create_app(RynmeshStore()))


def test_serves_index_and_assets(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch, with_ui=True)
    root = client.get("/")
    assert root.status_code == 200 and "ryn app" in root.text
    asset = client.get("/assets/app.js")
    assert asset.status_code == 200 and "console.log" in asset.text


def test_spa_routes_fall_back_to_index(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch, with_ui=True)
    for route in ("/digest", "/peers", "/items/cid_abc"):
        response = client.get(route)
        assert response.status_code == 200, route
        assert "ryn app" in response.text


def test_missing_asset_still_404s(tmp_path, monkeypatch):
    # A broken <script src> must fail loudly, not silently return HTML.
    client = _client(tmp_path, monkeypatch, with_ui=True)
    assert client.get("/assets/nope.js").status_code == 404


def test_api_routes_win_over_static_mount(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch, with_ui=True)
    assert client.get("/health").status_code == 200
    assert client.get("/api/local/sources").json() == []
    assert client.get("/api/local/ai/status").json()["provider"] is None


def test_source_checkout_without_ui_still_serves_api(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch, with_ui=False)
    assert peer_http.webui_dir().name == "webui"
    assert client.get("/health").status_code == 200
    assert client.get("/").status_code == 404   # no UI bundled: API-only node


def test_index_is_never_cached_but_assets_are(tmp_path, monkeypatch):
    # index.html names hash-versioned bundles; if a browser caches it, an
    # updated node keeps serving the old app until a manual hard-refresh.
    client = _client(tmp_path, monkeypatch, with_ui=True)
    for route in ("/", "/index.html", "/digest"):
        assert "no-cache" in client.get(route).headers.get("cache-control", ""), route
    assert "no-cache" not in client.get("/assets/app.js").headers.get("cache-control", "")
