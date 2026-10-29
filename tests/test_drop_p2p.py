"""Device drop over ICE + QUIC when no HTTP endpoint is reachable.

Two real engines with no endpoints at all: the only way they can reach each
other is the P2P path — signaling through an in-memory work-order bus shaped
like the registry API, real aioice candidates on this machine, and a real
aioquic connection on the nominated pair. Bytes, signatures and resume logic
are the production ones.
"""

from __future__ import annotations

import threading
import time
import uuid
from pathlib import Path
from typing import Any

import pytest
from test_drop_engine import MiB, Node, sha, status, wait_for, write_random

from rynmesh.drop import codec, p2p_link
from rynmesh.drop.p2p_link import _allowed_target, is_p2p_endpoint

pytest.importorskip("aioquic")
ice = pytest.importorskip("aioice.ice")


class SignalBus:
    """Work orders and results, as the registry stores them (no signatures)."""

    def __init__(self) -> None:
        self.lock = threading.Condition()
        self.orders: dict[str, dict[str, Any]] = {}
        self.results: dict[str, list[dict[str, Any]]] = {}

    def view(self, peer_id: str) -> "SignalView":
        return SignalView(self, peer_id)


class SignalView:
    supports_order_wait = True

    def __init__(self, bus: SignalBus, peer_id: str) -> None:
        self.bus = bus
        self.peer_id = peer_id

    def submit_work_order(self, *, provider_peer_id: str, capability: str, operation: str, params: dict,
                          network_id: str, idempotency_key: str = "", expires_in_hours: float = 1.0) -> dict:
        order = {"work_order_id": uuid.uuid4().hex, "provider_peer_id": provider_peer_id,
                 "requester_peer_id": self.peer_id, "capability": capability, "operation": operation,
                 "params": params, "network_id": network_id}
        with self.bus.lock:
            self.bus.orders[order["work_order_id"]] = order
            self.bus.lock.notify_all()
        return {"order": order}

    def _open(self, capability: str) -> list[dict]:
        return [o for o in self.bus.orders.values()
                if o["provider_peer_id"] == self.peer_id and o["capability"] == capability
                and not self.bus.results.get(o["work_order_id"])]

    def poll_work_orders(self, *, network_id: str, capability: str, wait_s: float = 0.0) -> dict:
        deadline = time.monotonic() + min(wait_s, 1.0)
        with self.bus.lock:
            while not self._open(capability) and time.monotonic() < deadline:
                self.bus.lock.wait(max(0.0, deadline - time.monotonic()))
            return {"work_orders": [dict(o) for o in self._open(capability)], "waited": wait_s > 0}

    def publish_work_result(self, *, work_order_id: str, requester_peer_id: str, status: str, message: str = "",
                            result_refs: dict | None = None, network_id: str = "") -> dict:
        with self.bus.lock:
            self.bus.results.setdefault(work_order_id, []).append({
                "work_order_id": work_order_id, "provider_peer_id": self.peer_id,
                "requester_peer_id": requester_peer_id, "status": status, "result_refs": result_refs or {},
            })
        return {"ok": True}

    def list_work_results(self, *, work_order_id: str, network_id: str = "") -> dict:
        with self.bus.lock:
            return {"work_results": [dict(r) for r in self.bus.results.get(work_order_id, [])]}


def p2p_node(root: Path, name: str, bus: SignalBus) -> Node:
    node = Node(root, name)
    view = bus.view(node.identity.peer_id)
    node.identity.registry = view
    for method in ("submit_work_order", "poll_work_orders", "publish_work_result", "list_work_results"):
        setattr(node.identity, method, getattr(view, method))
    return node


def _native_installed() -> bool:
    try:
        import ryn_transport  # noqa: F401
    except ImportError:
        return False
    return True


@pytest.fixture(params=["native", "aioice"])
def p2p_pair(tmp_path: Path, monkeypatch, request):
    """Both data planes: the native extension when installed, and aioice."""
    if request.param == "native" and not _native_installed():
        pytest.skip("ryn_transport (native/ryn_transport) is not installed")
    monkeypatch.setenv("RYNMESH_P2P_NATIVE", "auto" if request.param == "native" else "off")
    # No STUN traffic from tests: host candidates on this machine suffice.
    monkeypatch.setenv("RYNMESH_P2P_STUN", "off")
    if request.param == "aioice":
        monkeypatch.setattr(ice, "get_host_addresses", lambda **kwargs: ["127.0.0.1"])
    bus = SignalBus()
    a = p2p_node(tmp_path, "alpha", bus).start()
    b = p2p_node(tmp_path, "beta", bus).start()
    # Paired (own devices) but with no endpoint records: HTTP cannot reach them.
    a.own[b.identity.peer_id] = "beta"
    b.own[a.identity.peer_id] = "alpha"
    a.transport = request.param
    yield a, b, bus
    a.stop()
    b.stop()


def test_only_drop_peer_routes_cross_the_tunnel():
    assert _allowed_target("POST", "/api/peer/drop/offer")
    assert _allowed_target("PUT", "/api/peer/drop/data/abc/def?offset=0&salt=00")
    for method, target in [("POST", "/api/local/drop/transfers"), ("GET", "/api/peer/drop/../../local/x"),
                           ("DELETE", "/api/peer/drop/offer"), ("GET", "/api/v1/peers"),
                           ("GET", "/api/peer/drop/%2e%2e/x")]:
        assert not _allowed_target(method, target), target


def test_message_and_large_file_cross_ice_and_quic_without_http(p2p_pair, tmp_path: Path):
    a, b, _bus = p2p_pair
    assert a.e.capabilities()["nat_traversal"] is True
    msg = a.e.send_message({"peer_id": b.identity.peer_id, "text": "over the internet", "client_id": "p1"})
    wait_for(lambda: a.e.db.message(msg["message_id"])["status"] == "delivered", timeout=60, message="delivered")
    assert is_p2p_endpoint(a.e.dir.cached(b.identity.peer_id).endpoint)
    assert a.e.peer_meta(b.identity.peer_id)["address"].startswith("P2P")

    # Larger than the stream high-water mark, so backpressure is exercised.
    src = write_random(tmp_path / "src" / "video.bin", 20 * MiB + 77)
    t = a.e.create_transfer({"peer_id": b.identity.peer_id, "paths": [str(src)], "client_id": codec.new_id()})
    tid = t["transfer_id"]
    wait_for(lambda: status(b, tid) == "awaiting_accept", timeout=60, message="offer")
    b.e.action(tid, "accept", {})
    wait_for(lambda: status(a, tid) == "completed", timeout=120, message="sender completed")
    assert status(b, tid) == "completed"
    assert sha(Path(b.e.db.transfer(tid)["final_path"])) == sha(src)

    # The receiver answered requests in the other direction over the same link.
    assert b.e.p2p.link_info(a.identity.peer_id) is not None
    # And the data plane is the one negotiated for this run.
    info = a.e.p2p.link_info(b.identity.peer_id)
    assert info["native"] is (a.transport == "native")
    if info["native"]:
        assert info["congestion"] in {"cubic", "bbr"}


def test_unpaired_device_cannot_open_a_path(p2p_pair, tmp_path: Path):
    a, b, bus = p2p_pair
    stranger = p2p_node(tmp_path, "stranger", bus).start()
    try:
        stranger.own[b.identity.peer_id] = "beta"  # it may think so; beta does not
        with pytest.raises(ConnectionError) as refused:
            stranger.e.p2p.connect(b.identity.peer_id, urgent=True)
        assert "not_paired" in str(refused.value)
        assert b.e.p2p.link_info(stranger.identity.peer_id) is None
    finally:
        stranger.stop()


def test_failed_dials_back_off(p2p_pair, monkeypatch):
    a, _b, _bus = p2p_pair
    monkeypatch.setattr(p2p_link, "ANSWER_TIMEOUT_S", 0.5)
    ghost = "Z" * 43 + "="
    a.own[ghost] = "ghost"
    with pytest.raises((ConnectionError, TimeoutError)):
        a.e.p2p.connect(ghost, urgent=True)
    started = time.monotonic()
    with pytest.raises(ConnectionError, match="backoff"):
        a.e.p2p.connect(ghost)  # periodic probes do not redial an offline device
    assert time.monotonic() - started < 1.0
