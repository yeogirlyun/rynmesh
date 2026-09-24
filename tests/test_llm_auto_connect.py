"""Real HTTP and ICE/UDP tests; these do not claim public NAT traversal."""

import asyncio
import json
import socket
import threading
import time
from contextlib import contextmanager
from types import SimpleNamespace

import pytest
import uvicorn
from fastapi import FastAPI
from fastapi.testclient import TestClient

from rynmesh.llm_package import p2p, routes
from rynmesh.llm_package.manifest import LLMPackageManifest, save_manifest
from rynmesh.llm_package.task_balance import TaskBalanceLedger
from rynmesh.personal_space import PersonalSpace
from rynmesh.services import peer_box
from rynmesh.store import RynmeshStore


class Adapter:
    def __init__(self):
        self.calls = 0

    def health(self):
        return {"ok": True}

    def infer(self, **_kwargs):
        self.calls += 1
        return {"text": "Private response", "input_tokens": 4, "output_tokens": 2, "duration_ms": 1}

    def shutdown(self):
        pass

    def chat(self, body, *, task_id, timeout_s, on_event=None):
        result = self.infer()
        if body.get("stream") and on_event:
            on_event({"choices": [{"delta": {"content": result["text"]}}]})
        return {
            **result,
            "message": {"role": "assistant", "content": result["text"]},
            "finish_reason": "stop",
        }


@contextmanager
def serve(app):
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(app, log_level="error", lifespan="off"))
    thread = threading.Thread(target=server.run, kwargs={"sockets": [sock]}, daemon=True)
    thread.start()
    deadline = time.monotonic() + 5
    while not server.started and time.monotonic() < deadline:
        time.sleep(0.01)
    assert server.started
    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        server.should_exit = True
        thread.join(5)
        sock.close()


@pytest.fixture
def mesh(tmp_path, monkeypatch):
    for key in (
        "RYNMESH_REGISTRY_URL",
        "RYNMESH_LLM_SERVICE_MANIFEST",
        "RYNMESH_LLM_RELAY_URL",
        "RYNMESH_LLM_FORCE_RELAY",
        "RYNMESH_LLM_TRANSPORT",
        "RYNMESH_P2P_BIND_PORT",
        "RYNMESH_P2P_REQUIRE_PUBLIC",
        "RYNMESH_P2P_REQUIRE_DISTINCT_PUBLIC",
    ):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("RYNMESH_P2P_STUN", "off")
    monkeypatch.setenv("RYNMESH_REGISTRY_DIR", str(tmp_path / "registry"))
    monkeypatch.setenv("RYNMESH_P2P_CONNECT_TIMEOUT_S", "3")
    nodes = [
        RynmeshStore(home=tmp_path / name, network_dir=tmp_path / "network", node_name=name)
        for name in ("home", "laptop", "work")
    ]
    spaces = [PersonalSpace(node) for node in nodes]
    for node, space in zip(nodes, spaces, strict=True):
        node.personal_space = space
    root = spaces[0]
    root.create("My devices")
    for child in spaces[1:]:
        child.join(root.act("invite", {})["invitation"], child.store.node_name)
        child.tick()
        root.tick()
        child.tick()
    root.set_policy("space")
    provider = nodes[0]
    manifest = save_manifest(
        LLMPackageManifest(
            package_id="test-model",
            mode="openai_compatible",
            public_model_alias="Test model",
            base_url="http://127.0.0.1:1",
            timeout_seconds=10,
        ),
        provider.home / "llm" / "manifest.json",
    )
    (provider.home / "llm" / "provider-settings.json").write_text(
        json.dumps(
            {
                "manifest": str(manifest),
                "publication_enabled": True,
                "network_id": "test-space",
            }
        )
    )
    adapter = Adapter()
    monkeypatch.setattr(routes, "adapter_from_manifest", lambda _: adapter)
    endpoint = [""]
    apps = []
    for node in nodes:
        app = FastAPI()
        routes.install_llm_routes(
            app,
            store=node,
            home=node.home,
            messaging_key=peer_box.load_or_create_messaging_key(node.home / "msg.key"),
            resolve_endpoint=lambda _: endpoint[0],
            resolve_pubkey=lambda _: "",
        )
        apps.append(app)
    apps[0].state.llm_publish_once()
    stop = threading.Event()
    failures = []

    def poll():
        while not stop.wait(0.03):
            try:
                apps[0].state.llm_relay_once()
            except Exception as exc:
                failures.append(exc)

    thread = threading.Thread(target=poll, daemon=True)
    thread.start()
    with serve(apps[0]) as url:
        endpoint[0] = url
        yield SimpleNamespace(
            nodes=nodes, spaces=spaces, apps=apps, endpoint=endpoint, adapter=adapter
        )
    stop.set()
    thread.join(5)
    assert not failures


def order(mesh, client_index=1, **overrides):
    body = {
        "network_id": "test-space",
        "provider_peer_id": mesh.nodes[0].peer_id,
        "service_id": "test-model",
        "prompt": "SECRET request",
        "max_tokens": 16,
        "transport": "auto",
        **overrides,
    }
    with TestClient(mesh.apps[client_index]) as client:
        response = client.post("/api/local/llm/orders", json=body)
        if response.status_code == 200:
            result = response.json()
            persisted = client.get("/api/local/llm/orders/" + result["task_id"]).json()
            assert persisted["transport"] == result["transport"]
        return response


def test_three_devices_direct_then_real_ice_and_address_change(mesh):
    result = order(mesh).json()
    assert result["state"] == "succeeded" and result["transport"] == "peer_http_direct"
    current = mesh.endpoint[0]
    mesh.endpoint[0] = "http://127.0.0.1:1"  # stale / unreachable advertised endpoint
    response = order(mesh, 2)
    assert response.status_code == 200, response.text
    result = response.json()
    assert result["state"] == "succeeded" and result["transport"] == "ice_udp_direct"
    assert result["transport_evidence"]["relay_used"] is False
    assert result["connection_attempts"][0]["transport"] == "direct"
    mesh.endpoint[0] = current
    assert order(mesh, 2).json()["transport"] == "peer_http_direct"
    assert mesh.adapter.calls == 3


def test_lan_is_used_before_stale_registry_endpoint(mesh, monkeypatch):
    local_endpoint = mesh.endpoint[0]
    mesh.endpoint[0] = "http://127.0.0.1:1"
    monkeypatch.setattr(mesh.nodes[1], "lan_peer_endpoints", lambda *_, **__: [local_endpoint])
    calls = []
    real_post = routes._peer_post_json

    def post(url, *args, **kwargs):
        calls.append(url)
        return real_post(url, *args, **kwargs)

    monkeypatch.setattr(routes, "_peer_post_json", post)
    response = order(mesh)
    assert response.status_code == 200, response.text
    assert response.json()["transport_evidence"]["route"] == "lan"
    assert response.json()["connection_attempts"] == []
    assert calls == [local_endpoint + "/api/peer/llm/tasks",
                     local_endpoint + "/api/peer/llm/settlements"]
    assert mesh.adapter.calls == 1


def test_failed_lan_tries_registry_http_before_p2p(mesh, monkeypatch):
    monkeypatch.setattr(mesh.nodes[1], "lan_peer_endpoints",
                        lambda *_, **__: ["http://127.0.0.1:1", "http://127.0.0.1:1"])

    async def unexpected(**kwargs):
        pytest.fail("reachable registry HTTP endpoint must precede P2P")

    monkeypatch.setattr(routes, "consumer_exchange", unexpected)
    response = order(mesh)
    assert response.status_code == 200, response.text
    assert response.json()["transport"] == "peer_http_direct"
    assert len(response.json()["connection_attempts"]) == 1  # duplicated addresses are skipped
    assert mesh.adapter.calls == 1


def test_strict_p2p_does_not_scan_or_attempt_lan_http(mesh, monkeypatch):
    def unexpected(*args, **kwargs):
        pytest.fail("explicit P2P must not use LAN HTTP discovery")

    monkeypatch.setattr(mesh.nodes[1], "lan_peer_endpoints", unexpected)
    response = order(mesh, transport="p2p")
    assert response.status_code == 200, response.text
    assert response.json()["transport"] == "ice_udp_direct"


def test_lan_discovery_does_not_bypass_space_permissions(mesh, monkeypatch):
    mesh.spaces[0].act("remove", {"peer_id": mesh.nodes[2].peer_id})
    monkeypatch.setattr(mesh.nodes[2], "lan_peer_endpoints", lambda *_, **__: [mesh.endpoint[0]])
    assert order(mesh, 2, transport="direct").status_code == 502
    assert mesh.adapter.calls == 0


def test_lost_http_reply_falls_back_without_duplicate_inference(mesh, monkeypatch):
    real_post = routes._peer_post_json

    def lose_reply(url, payload, **kwargs):
        value = real_post(url, payload, **kwargs)
        if url.endswith("/tasks"):
            raise OSError("simulated lost HTTP response")
        return value

    monkeypatch.setattr(routes, "_peer_post_json", lose_reply)
    response = order(mesh)
    assert response.status_code == 200, response.text
    assert response.json()["transport"] == "ice_udp_direct"
    assert mesh.adapter.calls == 1
    ledger = TaskBalanceLedger(mesh.nodes[1].home / "llm" / "task-balance.json")
    assert len([event for event in ledger.events() if event["kind"] == "settle"]) == 1


def test_cli_only_provider_routes_encrypted_p2p_request_without_registry_service_id(mesh):
    provider = mesh.nodes[0]
    path = save_manifest(
        LLMPackageManifest(package_id="codex-cli", mode="codex_cli", adapter="codex_cli",
                           runtime="external", public_model_alias="ChatGPT", timeout_seconds=10),
        provider.home / "llm" / "codex-cli" / "manifest.json",
    )
    (provider.home / "llm" / "cli-services.json").write_text(json.dumps({
        "codex_cli": {"manifest": str(path), "publication_enabled": True},
    }))
    # Remove the generic provider from a fresh app so routing cannot fall back
    # to the default model and accidentally pass this regression test.
    (provider.home / "llm" / "provider-settings.json").write_text(json.dumps({"network_id": "test-space"}))
    app = FastAPI()
    routes.install_llm_routes(
        app, store=provider, home=provider.home,
        messaging_key=peer_box.load_or_create_messaging_key(provider.home / "msg.key"),
        resolve_endpoint=lambda _: "", resolve_pubkey=lambda _: "",
    )
    mesh.apps[0] = app
    app.state.llm_publish_once()
    mesh.endpoint[0] = ""
    response = order(mesh, service_id="codex-cli", transport="p2p")
    assert response.status_code == 200, response.text
    assert response.json()["transport"] == "ice_udp_direct"
    assert mesh.adapter.calls == 1
    offers = provider.registry.list_work_orders(network_id="test-space", status="")
    offer = next(item.payload for item in offers if item.payload["operation"].endswith(".p2p_offer"))
    assert set(offer["params"]) == {"session_id", "ice_signal", "timeout_seconds"}


def test_udp_unavailable_reports_timeout_and_does_not_relay(mesh, monkeypatch):
    mesh.endpoint[0] = ""

    async def unavailable(**kwargs):
        raise asyncio.TimeoutError()

    monkeypatch.setattr(routes, "consumer_exchange", unavailable)
    response = order(mesh, task_id="blocked_udp")
    assert response.status_code == 502
    with TestClient(mesh.apps[1]) as client:
        status = client.get("/api/local/llm/orders/blocked_udp").json()
    assert status["error_code"] == "p2p_connection_timed_out"
    assert status["connection_attempts"] == [
        {"transport": "p2p", "error_code": "p2p_connection_timed_out"}
    ]
    assert mesh.adapter.calls == 0
    assert (
        TaskBalanceLedger(mesh.nodes[1].home / "llm" / "task-balance.json").summary()["held"] == 0
    )


def test_strict_direct_does_not_attempt_ice(mesh, monkeypatch):
    mesh.endpoint[0] = "http://127.0.0.1:1"

    async def unexpected(**kwargs):
        pytest.fail("strict direct must not try P2P")

    monkeypatch.setattr(routes, "consumer_exchange", unexpected)
    assert order(mesh, transport="direct").status_code == 502
    assert mesh.adapter.calls == 0


def test_p2p_still_enforces_space_membership(mesh):
    mesh.spaces[0].act("remove", {"peer_id": mesh.nodes[2].peer_id})
    mesh.endpoint[0] = ""
    assert order(mesh, 2).status_code == 502
    assert mesh.adapter.calls == 0


def test_auto_stream_uses_real_ice_when_http_unreachable(mesh):
    mesh.endpoint[0] = "http://127.0.0.1:1"
    response = order(
        mesh, chat={"messages": [{"role": "user", "content": "SECRET"}], "stream": True}
    )
    assert response.status_code == 200, response.text
    assert response.json()["transport"] == "ice_udp_direct"
    assert response.json()["message"]["content"] == "Private response"
    assert mesh.adapter.calls == 1


def test_partial_http_stream_is_not_replayed_over_p2p(mesh, monkeypatch):
    def interrupted(_url, payload, *, timeout_s, on_event):
        mesh.apps[0].state.llm_provider.handle(payload, on_event)
        raise OSError("lost stream after a delta")

    async def unexpected(**kwargs):
        pytest.fail("partial streams must not be replayed")

    monkeypatch.setattr(routes, "peer_stream", interrupted)
    monkeypatch.setattr(routes, "consumer_exchange", unexpected)
    response = order(
        mesh, chat={"messages": [{"role": "user", "content": "SECRET"}], "stream": True}
    )
    assert response.status_code == 502
    assert mesh.adapter.calls == 1


def test_setup_timeout_closes_ice_without_sending_request(monkeypatch):
    class Connection:
        closed = False

        async def close(self):
            self.closed = True

    connection = Connection()
    monkeypatch.setattr(p2p, "new_connection", lambda **_: connection)

    async def gather(_):
        await asyncio.sleep(10)

    monkeypatch.setattr(p2p, "gather_signal", gather)
    with pytest.raises(p2p.P2PTimeoutError) as caught:
        asyncio.run(
            p2p.consumer_exchange(
                signed_request={}, publish_offer=None, timeout_s=100, connect_timeout_s=0.02
            )
        )
    assert connection.closed
    assert caught.value.stage == "gathering"


def test_signaling_does_not_consume_ice_check_budget(monkeypatch):
    class Connection:
        closed = False

        async def connect(self):
            await asyncio.sleep(0.08)

        async def close(self):
            self.closed = True

    connection = Connection()
    monkeypatch.setattr(p2p, "new_connection", lambda **_: connection)

    async def gather(_):
        await asyncio.sleep(0.08)
        return object()

    async def signal(_):
        await asyncio.sleep(0.08)
        return object()

    async def apply(*_):
        pass

    async def transfer(*_, **__):
        raise RuntimeError("reached encrypted transfer")

    monkeypatch.setattr(p2p, "gather_signal", gather)
    monkeypatch.setattr(p2p, "apply_remote_signal", apply)
    monkeypatch.setattr(p2p, "validate_distinct_public_egress", lambda *_: None)
    monkeypatch.setattr(p2p, "selected_pair", lambda _: {})
    monkeypatch.setattr(p2p, "send_json", transfer)
    with pytest.raises(RuntimeError, match="reached encrypted transfer"):
        asyncio.run(p2p.consumer_exchange(signed_request={}, publish_offer=signal,
                                         timeout_s=10, connect_timeout_s=0.2))
    assert connection.closed


def test_auto_endpoint_changes_without_identity_change(tmp_path, monkeypatch):
    import rynmesh.store as store_module

    monkeypatch.setenv("RYNMESH_AUTO_PEER_ENDPOINT", "1")
    monkeypatch.setenv("RYNMESH_PEER_PORT", "8792")
    monkeypatch.setenv("RYNMESH_PEER_ENDPOINT", "http://192.168.0.2:8792")
    node = RynmeshStore(home=tmp_path / "node", network_dir=tmp_path / "network")
    original_id = node.peer_id
    monkeypatch.setattr(store_module, "_primary_lan_ip", lambda: "192.168.1.5")
    assert node._default_peer_endpoint() == "http://192.168.1.5:8792"
    monkeypatch.setattr(store_module, "_primary_lan_ip", lambda: "10.0.0.3")
    assert node._default_peer_endpoint() == "http://10.0.0.3:8792"
    assert node.peer_id == original_id
    monkeypatch.setenv("RYNMESH_AUTO_PEER_ENDPOINT", "0")
    assert node._default_peer_endpoint() == "http://192.168.0.2:8792"
