"""S13-I64: messaging routing, loop responsiveness and bounded subscribers."""
import asyncio
import inspect
import json
import threading
import time
from urllib.parse import urlparse

import httpx
import pytest
from fastapi.testclient import TestClient
from starlette.requests import Request

from rynmesh import peer_http
from rynmesh.services import peer_box
from rynmesh.services.peer_messenger import PeerMessenger
from rynmesh.store import RynmeshStore
from rynmesh.transport import network_key_header

AUTH = {"X-Ryn-Local-Token": "message-owner-test"}
MIB = 1024 * 1024


@pytest.fixture
def node(tmp_path, monkeypatch):
    monkeypatch.setenv("RYNMESH_HOME", str(tmp_path / "node"))
    monkeypatch.setenv("RYNMESH_AUTO_REGISTER", "0")
    monkeypatch.setenv("RYNMESH_DISABLE_DISCOVERY", "1")
    monkeypatch.setenv("RYNMESH_LOCAL_TOKEN", "message-owner-test")
    monkeypatch.setenv("RYNMESH_REGISTRY_URL", "")
    monkeypatch.delenv("RYNMESH_MESSAGING_FORCE_MAILBOX", raising=False)
    store = RynmeshStore(home=tmp_path / "node", network_dir=tmp_path / "network")
    app = peer_http.create_app(store)
    yield app, store
    app.state.llm_shutdown()


def test_T1_unreachable_send_does_not_block_health(node, monkeypatch):
    app, _ = node
    started = threading.Event()
    def stalled_send(self, peer_id, **kwargs):
        started.set()
        time.sleep(2)
        return {"msg_id": "offline", "delivered": False}
    monkeypatch.setattr(PeerMessenger, "send", stalled_send)
    async def scenario():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                    base_url="http://127.0.0.1", headers=AUTH) as client:
            pending = asyncio.create_task(client.post("/api/local/messages/send", json={"peer_id": "offline", "text": "hello"}))
            await asyncio.to_thread(started.wait, 10)
            assert started.is_set()
            before = time.perf_counter()
            health = await client.get("/health")
            elapsed = time.perf_counter() - before
            # Also check that the slow send was still pending when health returned.
            still_sending = not pending.done()
            sent = await pending
            assert health.status_code == 200 and sent.status_code == 200
            assert elapsed < 0.5 and still_sending, f"health waited for send: {elapsed:.3f}s"
    asyncio.run(scenario())


def test_T2_pubkey_and_direct_delivery_use_transport_and_cached_discovery(node, tmp_path, monkeypatch):
    app, store = node
    remote = RynmeshStore(home=tmp_path / "remote", network_dir=tmp_path / "network")
    remote_key = peer_box.load_or_create_messaging_key(remote.home / "messaging.x25519")
    remote.register_node(endpoints=("https://peer.example",))
    remote_app = peer_http.create_app(remote)
    remote_app.state.pubkey_cache[store.peer_id] = peer_box.public_key_b64(
        peer_box.load_or_create_messaging_key(store.home / "messaging.x25519"))
    remote_client = TestClient(remote_app)
    calls, discovery = [], []
    def discover(**options):
        discovery.append(options)
        return {"peers": [{"peer_id": remote.peer_id, "endpoints": ["https://peer.example"]}]}
    monkeypatch.setattr(store, "discover_peers", discover)
    monkeypatch.setenv("RYNMESH_NETWORK_KEY", "message-network-test")
    class Transport:
        def get_bytes(self, url, **options):
            calls.append(("get", url, options))
            return remote_client.get(urlparse(url).path, headers=network_key_header()).content
        def post_bytes(self, url, body, **options):
            calls.append(("post", url, options))
            response = remote_client.post(urlparse(url).path, content=body, headers=options["headers"])
            assert response.status_code == 200, response.text
            return response.content
    monkeypatch.setattr(peer_http, "get_transport", lambda: Transport())
    def no_raw_urlopen(*args, **kwargs):
        raise AssertionError("messaging bypassed configured Transport")
    monkeypatch.setattr("urllib.request.urlopen", no_raw_urlopen)
    try:
        client = TestClient(app, headers=AUTH)
        response = client.post("/api/local/messages/send", json={"peer_id": remote.peer_id, "text": "Transport delivered this"})
        assert response.status_code == 200 and response.json()["delivered"] is True
        assert [kind for kind, *_ in calls] == ["get", "post"]
        assert calls[0][1].endswith("/api/peer/pubkey") and calls[0][2]["timeout_s"] == 10
        assert calls[1][1].endswith("/api/peer/msg") and calls[1][2]["timeout_s"] == 15
        assert calls[1][2]["headers"]["X-Ryn-Auth"] == network_key_header()["X-Ryn-Auth"]
        assert discovery and all(item.get("cache_only") is True and item.get("use_cache_on_error") is True for item in discovery)
        history = remote_client.get("/api/local/messages", params={"peer_id": store.peer_id}, headers=AUTH).json()
        assert history[0]["text"] == "Transport delivered this"
        # The verified key cache prevents another pubkey transport request.
        assert client.post("/api/local/messages/send", json={"peer_id": remote.peer_id, "text": "Cached key"}).json()["delivered"]
        assert sum(kind == "get" for kind, *_ in calls) == 1
        client.close()
    finally:
        remote_client.close()
        remote_app.state.llm_shutdown()


def test_T3_thirty_mib_attachment_is_rejected_before_base64_decode(node, monkeypatch):
    app, _ = node
    import base64
    def must_not_decode(*args, **kwargs):
        raise AssertionError("oversized attachment was decoded")
    monkeypatch.setattr(base64, "b64decode", must_not_decode)
    client = TestClient(app, headers=AUTH)
    response = client.post("/api/local/messages/send", json={"peer_id": "offline", "attachment": {
        "filename": "large.bin", "mime": "application/octet-stream", "bytes_b64": "eHh4" * (10 * MIB)}})
    assert response.status_code == 413 and response.json()["detail"] == "attachment_too_large"
    client.close()


def test_T3_existing_five_mib_service_limit_remains_a_friendly_413(node):
    app, _ = node
    client = TestClient(app, headers=AUTH)
    response = client.post("/api/local/messages/send", json={"peer_id": "offline", "attachment": {
        "filename": "six.bin", "bytes_b64": "eHh4" * (2 * MIB)}})
    assert response.status_code == 413 and response.json()["detail"] == "attachment_too_large"
    client.close()


def test_T3_peer_content_length_is_rejected_before_json_parse(node):
    app, _ = node
    client = TestClient(app)
    response = client.post("/api/peer/msg", content=b"not json", headers={"Content-Length": str(36 * MIB + 1)})
    assert response.status_code == 413
    client.close()


def test_T3_chunked_peer_body_also_obeys_wire_limit(node):
    app, _ = node
    async def scenario():
        async def chunks():
            yield b"x" * (19 * MIB)
            yield b"x" * (19 * MIB)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1") as client:
            response = await client.post("/api/peer/msg", content=chunks())
            assert response.status_code == 413
    asyncio.run(scenario())


@pytest.mark.parametrize("worker_thread", [False, True])
def test_T4_overflow_closes_real_subscription_without_unbounded_queue(node, worker_thread):
    app, _ = node
    stream = next(route.endpoint for route in app.routes if getattr(route, "path", "") == "/api/local/messages/stream")
    receiver = next(route.endpoint for route in app.routes if getattr(route, "path", "") == "/api/peer/msg")
    # Invoke the real fan-out callback also used by the mailbox worker.
    publish = inspect.getclosurevars(receiver).nonlocals["_publish"]
    async def scenario():
        loop = asyncio.get_running_loop()
        loop.set_debug(True)
        failures = []
        loop.set_exception_handler(lambda _, context: failures.append(context))
        app.state.loop = loop
        request = Request({"type": "http", "method": "GET", "path": "/api/local/messages/stream",
                           "headers": [(b"x-ryn-local-token", b"message-owner-test")],
                           "client": ("127.0.0.1", 12345), "server": ("127.0.0.1", 8791), "scheme": "http"})
        response = await stream(request)
        [queue] = app.state.message_subscribers
        assert queue.maxsize == 256
        def flood():
            for index in range(300):
                publish({"msg_id": str(index), "text": "event"})
        if worker_thread:
            await asyncio.to_thread(flood)
            await asyncio.sleep(0)
        else:
            flood()
        assert queue.qsize() <= 256 and queue not in app.state.message_subscribers
        with pytest.raises(StopAsyncIteration):
            await asyncio.wait_for(anext(response.body_iterator), timeout=1)
        assert failures == []
        # A reconnection gets a new usable subscription, not the detached queue.
        again = await stream(request)
        publish({"msg_id": "after-reconnect"})
        event = await anext(again.body_iterator)
        assert "after-reconnect" in event
        await again.body_iterator.aclose()
        assert app.state.message_subscribers == []
        app.state.loop = None
    asyncio.run(scenario())
