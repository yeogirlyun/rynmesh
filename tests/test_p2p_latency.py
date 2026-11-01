"""Connection-setup latency shared by private AI and device drop.

- Signaling: an idle node long-polls its open work orders, so a P2P offer is
  seen as soon as it is submitted (real registry HTTP server and client).
- STUN: the first configured server that answers is used; a silent one does
  not cost every connection a gathering timeout (local fake STUN servers).
- Reused AI sessions: no liveness probe right after traffic, and idle dead
  sessions are dropped by the keepalive instead of by the next request.
"""

from __future__ import annotations

import asyncio
import socket
import struct
import threading
import time
from pathlib import Path

import pytest
from test_drop_engine import free_port

from rynmesh import RynmeshStore
from rynmesh.llm_package import p2p, p2p_session
from rynmesh.registry import FilePeerRegistry, HttpPeerRegistry
from rynmesh.registry_http import create_app as create_registry_app


@pytest.fixture
def registry_url(tmp_path: Path):
    uvicorn = pytest.importorskip("uvicorn")
    app = create_registry_app(FilePeerRegistry(tmp_path / "registry"))
    port = free_port()
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning", lifespan="off"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.time() + 10
    while not server.started and time.time() < deadline:
        time.sleep(0.02)
    yield f"http://127.0.0.1:{port}"
    server.should_exit = True
    thread.join(timeout=10)


def _store(tmp_path: Path, name: str, url: str) -> RynmeshStore:
    store = RynmeshStore(home=tmp_path / name, network_dir=tmp_path / f"{name}-mesh")
    store.registry = HttpPeerRegistry(url)
    return store


def test_long_poll_delivers_a_new_offer_immediately(tmp_path: Path, registry_url: str):
    provider = _store(tmp_path, "provider", registry_url)
    requester = _store(tmp_path, "requester", registry_url)
    result: dict = {}

    def wait_for_order() -> None:
        started = time.monotonic()
        result["polled"] = provider.poll_work_orders(network_id="lp", capability="cap.v1", wait_s=10)
        result["elapsed"] = time.monotonic() - started

    poller = threading.Thread(target=wait_for_order)
    poller.start()
    time.sleep(0.8)
    submitted = requester.submit_work_order(provider_peer_id=provider.peer_id, capability="cap.v1",
                                            operation="cap.offer", params={}, network_id="lp")
    poller.join(timeout=15)
    assert result["polled"]["waited"] is True
    assert [o["work_order_id"] for o in result["polled"]["work_orders"]] == [submitted["work_order_id"]]
    assert 0.6 < result["elapsed"] < 4.0  # woke on submit, not at the end of the wait


def test_long_poll_returns_empty_when_nothing_arrives(tmp_path: Path, registry_url: str):
    provider = _store(tmp_path, "provider", registry_url)
    started = time.monotonic()
    polled = provider.poll_work_orders(network_id="lp", capability="cap.v1", wait_s=1)
    assert polled["work_orders"] == [] and polled["waited"] is True
    assert 0.9 <= time.monotonic() - started < 5.0


def test_file_registry_polls_without_waiting(tmp_path: Path):
    store = RynmeshStore(home=tmp_path / "local", network_dir=tmp_path / "local-mesh")
    store.registry = FilePeerRegistry(tmp_path / "registry")
    started = time.monotonic()
    assert store.poll_work_orders(network_id="lp", wait_s=10)["waited"] is False
    assert time.monotonic() - started < 2.0
    assert p2p.signal_poll_idle_max_s(store) == 3.0


class _FakeStun:
    def __init__(self, *, answer: bool) -> None:
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.bind(("127.0.0.1", 0))
        self.sock.settimeout(0.2)
        self.port = self.sock.getsockname()[1]
        self.answer = answer
        self.requests = 0
        self.stop = threading.Event()
        threading.Thread(target=self._run, daemon=True).start()

    def _run(self) -> None:
        while not self.stop.is_set():
            try:
                data, addr = self.sock.recvfrom(2048)
            except OSError:
                continue
            self.requests += 1
            if self.answer and len(data) >= 20:
                self.sock.sendto(struct.pack("!HHI", 0x0101, 0, 0x2112A442) + data[8:20], addr)

    def close(self) -> None:
        self.stop.set()
        self.sock.close()


def test_first_answering_stun_server_is_chosen(monkeypatch):
    silent, live = _FakeStun(answer=False), _FakeStun(answer=True)
    try:
        monkeypatch.setenv("RYNMESH_P2P_STUN", f"127.0.0.1:{silent.port},127.0.0.1:{live.port}")
        monkeypatch.setattr(p2p, "_STUN_CHOICE", p2p._StunChoice())
        started = time.monotonic()
        assert p2p.stun_server_from_env() == ("127.0.0.1", live.port)
        assert time.monotonic() - started < p2p.STUN_PROBE_TIMEOUT_S
        assert silent.requests == 1  # both were asked in parallel
        # Remembered: the next connection does not probe again.
        assert p2p.stun_server_from_env() == ("127.0.0.1", live.port)
        assert live.requests == 1
    finally:
        silent.close()
        live.close()


def test_stun_list_parsing_and_off(monkeypatch):
    monkeypatch.setenv("RYNMESH_P2P_STUN", "a.example:3478, [2001:db8::1]:19302,a.example:3478")
    assert p2p.stun_servers_from_env() == [("a.example", 3478), ("2001:db8::1", 19302)]
    monkeypatch.setenv("RYNMESH_P2P_STUN", "off")
    assert p2p.stun_server_from_env() is None
    monkeypatch.delenv("RYNMESH_P2P_STUN")
    assert len(p2p.stun_servers_from_env()) == 3  # several operators by default


class _FakeChannel:
    def __init__(self, alive: bool = True) -> None:
        self.closed = False
        self.alive = alive
        self.probes = 0
        self.last_used = time.monotonic()
        self.last_received = time.monotonic()

    async def probe(self) -> bool:
        self.probes += 1
        return self.alive

    async def close(self) -> None:
        self.closed = True


def test_recent_traffic_skips_the_reuse_probe_and_keepalive_drops_dead_sessions(monkeypatch):
    monkeypatch.setattr(p2p_session, "KEEPALIVE_SECONDS", 0.2)
    pool = p2p_session.SessionPool(max_sessions=2)
    channel = _FakeChannel()

    async def factory():
        return channel, {"transport": "fake"}

    async def exchange(ch, evidence):
        ch.last_received = time.monotonic()
        return evidence["connection_reused"]

    async def scenario() -> None:
        assert await pool.run("peer", factory, exchange) is False
        assert await pool.run("peer", factory, exchange) is True
        assert channel.probes == 0  # answered moments ago
        channel.last_received -= 10
        assert await pool.run("peer", factory, exchange) is True
        assert channel.probes == 1
        # Idle and dead: the keepalive finds it, so no request pays for that.
        channel.alive = False
        channel.last_received -= 10
        deadline = time.monotonic() + 3
        while pool.has_session("peer") and time.monotonic() < deadline:
            await asyncio.sleep(0.05)

    try:
        asyncio.run(scenario())
        assert not pool.has_session("peer")
        assert channel.closed
    finally:
        pool.close()
