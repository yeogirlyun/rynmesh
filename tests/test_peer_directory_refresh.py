"""Device-list requests read caches; a refresher updates them in the background.

The desktop polls /peers and /directory. Asking the registry (10 s per call
when unreachable) and probing every device inside those requests made one
request take ~40 s on a real machine, and the next poll queued behind it.
"""
from __future__ import annotations

import threading
import time
from concurrent.futures import wait

import pytest
from fastapi.testclient import TestClient

from rynmesh.peer_directory_refresh import DirectoryRefresher, PhaseTimer, SingleFlight
from rynmesh.peer_http import create_app
from rynmesh.services.peer_health import PeerHealthProbe
from rynmesh.store import RynmeshStore


class FakeClock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def _wait(refresher: DirectoryRefresher, network: str = "net") -> None:
    """Wait for the current refresh to end, whether it succeeded or failed."""
    future = refresher._networks[network].future
    if future is not None:
        wait([future], timeout=10)


# -- DirectoryRefresher ------------------------------------------------------

def test_refreshes_at_most_once_per_interval_and_never_twice_at_once():
    calls = []
    release = threading.Event()

    def refresh(network):
        calls.append(network)
        release.wait(5)

    clock = FakeClock()
    refresher = DirectoryRefresher(refresh, min_interval=15, cold_wait=0.05, clock=clock)
    try:
        refresher.request("net")          # cold: starts and waits briefly
        for _ in range(5):
            refresher.request("net")      # in flight: joins, never starts another
        assert len(calls) == 1
        release.set()
        _wait(refresher)
        clock.now += 14
        refresher.request("net")
        assert len(calls) == 1            # inside the interval
        clock.now += 1
        refresher.request("net")
        _wait(refresher)
        assert len(calls) == 2            # due again
    finally:
        release.set()
        refresher.close()


def test_only_the_first_read_waits_and_that_wait_is_bounded():
    release = threading.Event()
    refresher = DirectoryRefresher(lambda network: release.wait(10), min_interval=15, cold_wait=0.3)
    try:
        started = time.monotonic()
        refresher.request("net")
        cold = time.monotonic() - started
        assert 0.25 <= cold < 2
        release.set()
        _wait(refresher)
        release.clear()
        refresher._networks["net"].last_started -= 60  # due again, and the next refresh hangs
        started = time.monotonic()
        refresher.request("net")
        assert time.monotonic() - started < 0.1  # warm: never waits
    finally:
        release.set()
        refresher.close()


def test_a_failed_refresh_neither_raises_nor_blocks_the_next_one():
    attempts = []

    def refresh(network):
        attempts.append(network)
        raise RuntimeError("registry unreachable at 203.0.113.9")

    clock = FakeClock()
    refresher = DirectoryRefresher(refresh, min_interval=15, cold_wait=2, clock=clock)
    try:
        refresher.request("net")
        assert attempts == ["net"] and refresher._networks["net"].completed
        clock.now += 15
        refresher.request("net")
        _wait(refresher)
        assert len(attempts) == 2
    finally:
        refresher.close()


def test_refresh_runs_on_its_own_threads_not_the_default_pool():
    names = []
    refresher = DirectoryRefresher(lambda network: names.append(threading.current_thread().name), cold_wait=2)
    try:
        refresher.request("net")
        assert names and names[0].startswith("ryn-directory-refresh")
    finally:
        refresher.close()


def test_close_then_reuse_starts_a_new_pool():
    calls = []
    clock = FakeClock()
    refresher = DirectoryRefresher(lambda network: calls.append(network), min_interval=15, cold_wait=2, clock=clock)
    refresher.request("net")
    refresher.close()
    clock.now += 15
    refresher.request("net")
    _wait(refresher)
    refresher.close()
    assert calls == ["net", "net"]


def test_single_flight_shares_one_call_and_its_error():
    flight = SingleFlight()
    entered = threading.Event()
    release = threading.Event()
    calls = []

    def slow():
        calls.append(1)
        entered.set()
        release.wait(5)
        raise ValueError("probe failed")

    errors = []

    def caller():
        try:
            flight.run("net", slow)
        except ValueError as error:
            errors.append(error)

    owner = threading.Thread(target=caller)
    owner.start()
    entered.wait(5)
    joiners = [threading.Thread(target=caller) for _ in range(4)]
    for thread in joiners:
        thread.start()
    time.sleep(0.2)
    release.set()
    for thread in [owner, *joiners]:
        thread.join(5)
    assert len(calls) == 1 and len(errors) == 5
    assert flight.run("net", lambda: "fresh") == "fresh"  # nothing left behind


def test_phase_timer_logs_only_slow_work_without_details(caplog):
    fast_clock, slow_clock = FakeClock(), FakeClock()
    fast = PhaseTimer("fast", clock=fast_clock)
    with fast.phase("devices"):
        fast_clock.now += 0.2
    slow = PhaseTimer("local_directory", clock=slow_clock)
    with slow.phase("services"):
        slow_clock.now += 1.5
    with caplog.at_level("WARNING", logger="rynmesh.peer_directory_refresh"):
        slow.finish()
        fast.finish()
    lines = [record.getMessage() for record in caplog.records]
    assert len(lines) == 1
    assert "stage=local_directory" in lines[0] and "'services': 1.5" in lines[0]


# -- Routes ------------------------------------------------------------------

@pytest.fixture
def node(tmp_path, monkeypatch):
    monkeypatch.setenv("RYNMESH_HOME", str(tmp_path / "node"))
    monkeypatch.setenv("RYNMESH_REGISTRY_URL", "")
    monkeypatch.setenv("RYNMESH_AUTO_REGISTER", "0")
    monkeypatch.setenv("RYNMESH_DEFAULT_DISCOVERY", "0")
    store = RynmeshStore(home=tmp_path / "node", network_dir=tmp_path / "network")
    RynmeshStore(home=tmp_path / "other", network_dir=tmp_path / "network").register_node(network_id="rynmesh-main")
    monkeypatch.setattr(PeerHealthProbe, "_check_one", lambda *_: False)
    gate = {"open": threading.Event(), "calls": 0}
    gate["open"].set()
    real = store.registry.list_peers

    def gated_list_peers(*args, **kwargs):
        gate["calls"] += 1
        gate["open"].wait(30)
        return real(*args, **kwargs)

    monkeypatch.setattr(store.registry, "list_peers", gated_list_peers)
    app = create_app(store)
    client = TestClient(app)
    yield store, app, client, gate
    gate["open"].set()
    app.state.directory_refresher.close()


def test_directory_answers_from_cache_while_the_registry_hangs(node):
    store, app, client, gate = node
    assert client.get("/api/local/directory").status_code == 200  # cold start fills the cache
    refresher = app.state.directory_refresher
    _wait(refresher, "rynmesh-main")
    gate["open"].clear()
    gate["calls"] = 0
    refresher._networks["rynmesh-main"].last_started -= 60  # the cache is due for a refresh
    for _ in range(5):
        started = time.monotonic()
        response = client.get("/api/local/directory")
        assert response.status_code == 200 and time.monotonic() - started < 1
        assert any(peer["isSelf"] for peer in response.json()["peers"])
    assert gate["calls"] == 1  # one background refresh, still waiting on the registry


def test_first_full_read_waits_at_most_the_cold_start_bound(node):
    store, app, client, gate = node
    gate["open"].clear()
    started = time.monotonic()
    response = client.get("/api/local/peers")
    elapsed = time.monotonic() - started
    assert response.status_code == 200
    assert elapsed < 8.5
    assert any(peer["isSelf"] for peer in response.json())


def test_quick_reads_never_start_a_refresh(node):
    store, app, client, gate = node
    client.get("/api/local/peers?quick=1")
    client.get("/api/local/directory?quick=1")
    assert gate["calls"] == 0
    assert "rynmesh-main" not in app.state.directory_refresher._networks


def test_concurrent_health_checks_share_one_probe(node, monkeypatch):
    store, app, client, gate = node
    release = threading.Event()
    probes = []

    def held_check(self, peers):
        probes.append(len(peers))
        release.wait(10)
        return [{"peerId": peer["id"], "online": False, "checkedAt": ""} for peer in peers]

    monkeypatch.setattr(PeerHealthProbe, "check", held_check)
    results = []
    threads = [threading.Thread(target=lambda: results.append(client.post("/api/local/peers/health").status_code))
               for _ in range(5)]
    for thread in threads:
        thread.start()
    time.sleep(1.0)
    release.set()
    for thread in threads:
        thread.join(15)
    assert results == [200] * 5
    assert len(probes) == 1
