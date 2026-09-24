import threading
from datetime import UTC, datetime
from concurrent.futures import ThreadPoolExecutor

import pytest
from fastapi.testclient import TestClient

from rynmesh.peer_http import create_app
from rynmesh.services.peer_health import PeerHealthProbe
from rynmesh.store import RynmeshStore


def test_startup_status_and_quick_peers_do_not_wait_for_registry(tmp_path, monkeypatch):
    monkeypatch.setenv("RYNMESH_HOME", str(tmp_path / "node"))
    monkeypatch.setenv("RYNMESH_REGISTRY_URL", "")
    monkeypatch.setenv("RYNMESH_AUTO_REGISTER", "0")
    monkeypatch.setenv("RYNMESH_DEFAULT_DISCOVERY", "0")
    store = RynmeshStore(home=tmp_path / "node", network_dir=tmp_path / "network")
    calls = []

    def discover(**kwargs):
        calls.append(kwargs.get("cache_only"))
        return {"peers": []}

    monkeypatch.setattr(store, "discover_peers", discover)
    client = TestClient(create_app(store))
    assert client.get("/api/local/node/status").status_code == 200
    assert client.get("/api/local/peers?quick=true").status_code == 200
    assert calls == [True, True]


def test_health_api_keeps_registration_evidence_for_unreachable_peer(tmp_path, monkeypatch):
    monkeypatch.setenv("RYNMESH_HOME", str(tmp_path / "node"))
    monkeypatch.setenv("RYNMESH_REGISTRY_URL", "")
    monkeypatch.setenv("RYNMESH_AUTO_REGISTER", "0")
    monkeypatch.setenv("RYNMESH_DEFAULT_DISCOVERY", "0")
    store = RynmeshStore(home=tmp_path / "node", network_dir=tmp_path / "network")
    remote = RynmeshStore(home=tmp_path / "remote", network_dir=tmp_path / "network")
    timestamp = datetime.now(UTC).isoformat()
    monkeypatch.setattr(store, "discover_peers", lambda **_: {"peers": [{
        "peer_id": remote.peer_id, "node_name": "remote", "updated_at": timestamp,
        "endpoints": ["http://10.10.10.200:8791"], "discovery_source": "registry",
    }]})
    monkeypatch.setattr(PeerHealthProbe, "_check_one", lambda *_: False)
    client = TestClient(create_app(store))
    response = client.post("/api/local/peers/health")
    assert response.status_code == 200
    result = next(item for item in response.json() if item["peerId"] == remote.peer_id)
    assert result["online"] is True
    assert result["httpReachable"] is False
    assert result["presenceSource"] == "heartbeat"
    assert result["lastSeen"] == timestamp


@pytest.mark.parametrize("blocked_stage", ["registry", "probe"])
def test_slow_peer_health_does_not_block_node_event_loop(tmp_path, monkeypatch, blocked_stage):
    monkeypatch.setenv("RYNMESH_HOME", str(tmp_path / "node"))
    monkeypatch.setenv("RYNMESH_REGISTRY_URL", "")
    monkeypatch.setenv("RYNMESH_AUTO_REGISTER", "0")
    monkeypatch.setenv("RYNMESH_DEFAULT_DISCOVERY", "0")
    store = RynmeshStore(home=tmp_path / "node", network_dir=tmp_path / "network")
    entered, release = threading.Event(), threading.Event()

    def wait():
        entered.set()
        release.wait(5)

    def discover(**_):
        if blocked_stage == "registry":
            wait()
        return {"peers": []}

    def check(_self, _peers):
        if blocked_stage == "probe":
            wait()
        return []

    monkeypatch.setattr(store, "discover_peers", discover)
    monkeypatch.setattr(PeerHealthProbe, "check", check)
    with TestClient(create_app(store)) as client, ThreadPoolExecutor() as executor:
        watchdog = threading.Timer(3, release.set)
        try:
            pending = executor.submit(client.post, "/api/local/peers/health")
            assert entered.wait(5)
            watchdog.start()
            assert client.get("/health").status_code == 200
            assert not release.is_set(), "health probe blocked the node event loop"
        finally:
            release.set()
            watchdog.cancel()
        assert pending.result(timeout=5).status_code == 200
