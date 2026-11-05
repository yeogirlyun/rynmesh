"""Browsing other devices' content never stalls the node.

POST /api/local/recommendations (requested by Home on every visit) used to ask
every device for its listing on the event loop, one device after another with
the full fetch timeout. With unreachable devices the whole node, health checks
and Agent reads included, stopped answering for minutes.
"""
from __future__ import annotations

import threading
import time

import pytest
from fastapi.testclient import TestClient

from rynmesh.peer_http import CONTENT_LISTING_TIMEOUT_S, create_app
from rynmesh.services.peer_health import PeerHealthProbe
from rynmesh.store import RynmeshStore


@pytest.fixture
def node(tmp_path, monkeypatch):
    monkeypatch.setenv("RYNMESH_HOME", str(tmp_path / "node"))
    monkeypatch.setenv("RYNMESH_REGISTRY_URL", "")
    monkeypatch.setenv("RYNMESH_AUTO_REGISTER", "0")
    monkeypatch.setenv("RYNMESH_DEFAULT_DISCOVERY", "0")
    store = RynmeshStore(home=tmp_path / "node", network_dir=tmp_path / "network")
    for n in range(6):
        monkeypatch.setenv("RYNMESH_PEER_ENDPOINT", f"http://192.0.2.{20 + n}:8791")
        RynmeshStore(home=tmp_path / f"device-{n}", network_dir=tmp_path / "network").register_node(
            network_id="rynmesh-main")
    monkeypatch.delenv("RYNMESH_PEER_ENDPOINT")
    store.discover_peers(network_id="rynmesh-main")  # fills the peer cache the scan reads
    monkeypatch.setattr(PeerHealthProbe, "_check_one", lambda *_: False)
    calls: list[tuple[str, float]] = []
    lock = threading.Lock()
    delay = {"seconds": 1.5}

    def slow_listing(endpoint, *, timeout_s):
        with lock:
            calls.append((endpoint, timeout_s))
        time.sleep(delay["seconds"])
        raise OSError("unreachable")

    monkeypatch.setattr(store, "list_peer_content", slow_listing)
    app = create_app(store)
    client = TestClient(app)
    yield client, calls, delay
    app.state.directory_refresher.close()


def test_health_answers_while_a_content_scan_waits_on_devices(node):
    client, calls, delay = node
    delay["seconds"] = 3.0
    done = threading.Event()
    statuses = []

    def recommend():
        statuses.append(client.post("/api/local/recommendations", json={}).status_code)
        done.set()

    worker = threading.Thread(target=recommend)
    worker.start()
    deadline = time.monotonic() + 5
    while not calls and time.monotonic() < deadline:
        time.sleep(0.05)
    assert calls, "the scan never reached the devices"
    started = time.monotonic()
    assert client.get("/health").status_code == 200
    assert client.get("/api/local/agent/tasks").status_code in {200, 404}
    assert time.monotonic() - started < 1.0
    assert not done.is_set()  # still waiting on the devices
    worker.join(15)
    assert statuses == [200]


def test_devices_are_asked_in_parallel_with_a_bounded_timeout(node):
    client, calls, delay = node
    started = time.monotonic()
    assert client.post("/api/local/recommendations", json={}).status_code == 200
    elapsed = time.monotonic() - started
    assert len(calls) == 6
    assert elapsed < 6 * delay["seconds"] / 2  # one after another would take 9 s
    assert all(timeout <= CONTENT_LISTING_TIMEOUT_S for _, timeout in calls)


def test_concurrent_scans_share_one_round_of_listings(node):
    client, calls, delay = node
    client.post("/api/local/recommendations", json={})  # warm start: refresher cache filled
    calls.clear()
    statuses = []
    threads = [threading.Thread(target=lambda: statuses.append(client.post("/api/local/recommendations", json={}).status_code))
               for _ in range(2)]
    threads.append(threading.Thread(target=lambda: statuses.append(client.get("/api/local/content").status_code)))
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(20)
    assert statuses.count(200) == 3
    assert len(calls) <= 6 * 2  # without sharing: 3 scans x 6 devices
