from __future__ import annotations

import asyncio
import json
import threading
import time
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from rynmesh.crypto import SignatureError, sign_payload
from rynmesh.llm_package.adapters import AdapterError, OpenAICompatibleAdapter, validate_local_url
from rynmesh.llm_package.lifecycle import LifecycleError, connect_local_api, validate_gguf
from rynmesh.llm_package.manifest import LLMPackageManifest, fingerprint_file
from rynmesh.llm_package.p2p import (
    P2PError,
    apply_remote_signal,
    gather_signal,
    new_connection,
    receive_json,
    selected_pair,
    send_json,
)
from rynmesh.llm_package.routes import (
    ProviderService,
    _open_provider_response,
    _recover_consumer_orders,
    install_llm_routes,
)
from rynmesh.llm_package.task_balance import TaskBalanceError, TaskBalanceLedger
from rynmesh.llm_package.task_protocol import TaskOrderStore, open_task, seal_task
from rynmesh.services import peer_box
from rynmesh.store import RynmeshStore


def _expires() -> str:
    return (datetime.now(timezone.utc) + timedelta(minutes=5)).isoformat()


class _OpenAIHandler(BaseHTTPRequestHandler):
    calls = 0

    def do_GET(self):
        if self.path == "/v1/models":
            self._send({"object": "list", "data": [{"id": "test-real-api"}]})
        else:
            self.send_error(404)

    def do_POST(self):
        if self.path != "/v1/chat/completions":
            self.send_error(404)
            return
        size = int(self.headers.get("content-length", "0"))
        body = json.loads(self.rfile.read(size))
        type(self).calls += 1
        if body["stream"] is True:
            self._send({"choices": [{"delta": {"content": "stream supported"}}]})
            return
        self._send({
            "choices": [{"message": {"content": "test adapter completion"}}],
            "usage": {"prompt_tokens": 4, "completion_tokens": 3},
        })

    def log_message(self, *_args):
        pass

    def _send(self, value):
        raw = json.dumps(value).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)


@pytest.fixture
def openai_server():
    server = ThreadingHTTPServer(("127.0.0.1", 0), _OpenAIHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        thread.join(timeout=3)


def test_openai_compatible_health_and_real_request(openai_server):
    adapter = OpenAICompatibleAdapter(base_url=openai_server, model="test-real-api")
    assert adapter.health()["ok"] is True
    result = adapter.infer(prompt="private prompt", max_tokens=8, task_id="t1", timeout_s=5)
    assert result["text"] == "test adapter completion"
    assert result["input_tokens"] == 4 and result["output_tokens"] == 3
    assert adapter.metrics()["requests"] == 1
    assert adapter.capabilities()["streaming"] is False


def test_openai_adapter_blocks_non_loopback_by_default():
    with pytest.raises(AdapterError, match="non-loopback"):
        validate_local_url("http://192.0.2.10:8080")
    assert validate_local_url("http://192.0.2.10:8080", allow_non_loopback=True)


def test_lifecycle_rejects_package_path_traversal_before_writing(tmp_path, openai_server):
    with pytest.raises(LifecycleError, match="lowercase slug"):
        connect_local_api(
            base_url=openai_server, package_id="../../outside", alias="safe-alias",
            root=tmp_path / "llm",
        )
    assert not (tmp_path / "outside").exists()


def test_local_setup_publish_pause_flow_is_explicit_and_persistent(tmp_path, openai_server):
    home = tmp_path / "node"
    store = RynmeshStore(home=home, network_dir=tmp_path / "network")
    messaging_key = peer_box.load_or_create_messaging_key(home / "messaging.x25519")
    app = FastAPI()
    install_llm_routes(
        app, store=store, home=home, messaging_key=messaging_key,
        resolve_endpoint=lambda _peer_id: "", resolve_pubkey=lambda _peer_id: "",
    )

    with TestClient(app) as client:
        setup = client.post("/api/local/llm/setup", json={
            "mode": "openai-compatible", "package_id": "configured-api",
            "alias": "public-safe-alias", "base_url": openai_server,
            "model": "test-real-api",
        })
        assert setup.status_code == 200
        setup_body = setup.json()
        assert setup_body["configured"] is True
        assert setup_body["publication_enabled"] is False
        assert "output_preview" not in setup_body["setup"]["self_test"]

        offline = client.get("/api/local/llm/service/status").json()
        assert offline["configured"] is True
        assert offline["online"] is False
        assert offline["accepting_orders"] is False

        published = client.post("/api/local/llm/services/publish", json={
            "network_id": "provider-test", "benchmark": False,
        })
        assert published.status_code == 200
        assert published.json()["record"]["metadata"]["llm_service"]["online"] is True

        online = client.get("/api/local/llm/service/status").json()
        assert online["online"] is True
        assert online["publication_enabled"] is True
        assert online["network_id"] == "provider-test"

        paused = client.post("/api/local/llm/services/pause").json()
        assert paused["online"] is False
        assert paused["accepting_orders"] is False
        assert paused["publication_enabled"] is False

        started = time.monotonic()
        accepted = client.post("/api/local/llm/orders/async", json={
            "provider_peer_id": "missing-provider", "service_id": "missing-service",
            "prompt": "private prompt that must not be stored", "max_tokens": 8,
        })
        assert accepted.status_code == 200
        assert time.monotonic() - started < 2
        task_id = accepted.json()["task_id"]
        deadline = time.monotonic() + 3
        status = {}
        while time.monotonic() < deadline:
            status = client.get(f"/api/local/llm/orders/{task_id}").json()
            if status.get("state") in {"failed", "cancelled"}:
                break
            time.sleep(0.02)
        assert status["state"] == "failed"
        assert "private prompt" not in json.dumps(status)

        privacy = client.put("/api/local/llm/privacy", json={
            "result_retention_seconds": 0,
        })
        assert privacy.status_code == 200
        assert privacy.json()["result_retention_seconds"] == 0
        assert privacy.json()["plaintext_persisted"] is False

        history_store = TaskOrderStore(home / "llm" / "consumer-orders")
        history_store.claim(task_id="history_cleanup", bindings={"request": "test"})
        history_store.transition(task_id="history_cleanup", state="accepted")
        history_store.transition(task_id="history_cleanup", state="running")
        history_store.transition(
            task_id="history_cleanup", state="succeeded",
            encrypted_response={"ciphertext": "encrypted-only"},
        )
        privacy = client.put("/api/local/llm/privacy", json={
            "result_retention_seconds": 0,
        })
        assert privacy.status_code == 200
        assert "encrypted_response" not in history_store.get("history_cleanup")
        cleared = client.delete("/api/local/llm/orders").json()
        assert cleared["removed"] >= 1
        assert history_store.get("history_cleanup") is None

    settings = json.loads((home / "llm" / "provider-settings.json").read_text(encoding="utf-8"))
    assert Path(settings["manifest"]).parts[-2:] == ("configured-api", "manifest.json")
    assert settings["publication_enabled"] is False
    assert settings["network_id"] == "provider-test"


def test_manifest_public_view_has_no_paths_urls_or_key_names(tmp_path):
    manifest = LLMPackageManifest(
        package_id="private-model", mode="import_gguf", public_model_alias="private-alias",
        base_url="http://127.0.0.1:8080", api_key_env="VERY_SECRET_KEY",
        model_path=str(tmp_path / "commercial-secret.gguf"), runtime_command=["secret-bin"],
        model_fingerprint="sha256:" + "a" * 64,
    )
    public = json.dumps(manifest.public_dict())
    assert "commercial-secret" not in public
    assert "VERY_SECRET_KEY" not in public
    assert "127.0.0.1" not in public
    assert "secret-bin" not in public
    assert "private-alias" in public


def test_gguf_import_is_read_only_and_fingerprinted(tmp_path):
    model = tmp_path / "owned.gguf"
    original = b"GGUF" + b"\x00" * 64
    model.write_bytes(original)
    details = validate_gguf(model, allow_risk=True)
    assert details["format"] == "GGUF"
    assert details["fingerprint"] == fingerprint_file(model)
    assert model.read_bytes() == original
    with pytest.raises(LifecycleError, match="expected GGUF"):
        bad = tmp_path / "bad.bin"
        bad.write_bytes(b"nope" * 10)
        validate_gguf(bad, allow_risk=True)


def test_task_balance_hold_settle_release_and_dedupe(tmp_path):
    ledger = TaskBalanceLedger(tmp_path / "task-balance.json", initial_dev_balance=10)
    first = ledger.hold(task_id="one", amount=2, service_id="svc", provider_peer_id="p")
    assert ledger.hold(task_id="one", amount=2, service_id="svc", provider_peer_id="p") == first
    settled = ledger.settle(task_id="one", amount=1.25, input_tokens=3, output_tokens=4,
                            duration_ms=5, service_id="svc", provider_peer_id="p")
    assert settled["state"] == "settled"
    assert ledger.settle(task_id="one", amount=1.25, input_tokens=3, output_tokens=4,
                         duration_ms=5, service_id="svc", provider_peer_id="p")["settled_amount"] == 1.25
    ledger.hold(task_id="two", amount=3, service_id="svc", provider_peer_id="p")
    released = ledger.release(task_id="two", reason="cancelled")
    assert released["state"] == "released"
    reheld = ledger.hold(task_id="two", amount=3, service_id="svc", provider_peer_id="p")
    assert reheld["state"] == "held"
    ledger.release(task_id="two", reason="retry_failed")
    with pytest.raises(TaskBalanceError):
        ledger.settle(task_id="two", amount=1, input_tokens=1, output_tokens=1,
                      duration_ms=1, service_id="svc", provider_peer_id="p")
    summary = ledger.summary()
    assert summary["development_only"] is True
    assert summary["available"] == 8.75 and summary["held"] == 0
    assert all("prompt" not in json.dumps(event) for event in ledger.events())


def test_task_balance_rejects_task_id_reuse_for_different_request(tmp_path):
    ledger = TaskBalanceLedger(tmp_path / "task-balance.json", initial_dev_balance=10)
    ledger.hold(
        task_id="same", amount=2, service_id="svc", provider_peer_id="provider-a",
        idempotency_key="key", request_fingerprint="fingerprint-a",
    )
    with pytest.raises(TaskBalanceError, match="idempotency conflict"):
        ledger.hold(
            task_id="same", amount=2, service_id="svc", provider_peer_id="provider-b",
            idempotency_key="key", request_fingerprint="fingerprint-b",
        )


def test_task_store_rejects_state_rollback_and_binding_changes(tmp_path):
    orders = TaskOrderStore(tmp_path / "orders")
    bindings = {
        "consumer_peer_id": "consumer", "service_id": "svc",
        "idempotency_key": "key", "request_fingerprint": "fingerprint",
    }
    _, claimed = orders.claim(task_id="bound", bindings=bindings)
    assert claimed is True
    _, claimed_again = orders.claim(task_id="bound", bindings=bindings)
    assert claimed_again is False
    orders.transition(task_id="bound", state="accepted")
    orders.transition(task_id="bound", state="running")
    with pytest.raises(Exception, match="invalid task transition"):
        orders.transition(task_id="bound", state="accepted")
    with pytest.raises(Exception, match="idempotency conflict"):
        orders.claim(task_id="bound", bindings={**bindings, "service_id": "other"})


def test_task_envelope_is_ciphertext_and_authenticated(tmp_path):
    sender = RynmeshStore(home=tmp_path / "s", network_dir=tmp_path / "net")
    recipient = RynmeshStore(home=tmp_path / "r", network_dir=tmp_path / "net")
    recipient_msg = peer_box.load_or_create_messaging_key(tmp_path / "r-msg")
    signed = seal_task(
        body={"task_id": "task_one", "prompt": "NEVER VISIBLE IN RELAY"},
        task_id="task_one", kind="llm_request", sender_peer_id=sender.peer_id,
        recipient_peer_id=recipient.peer_id, sender_signing_key=sender.private_key_bytes,
        recipient_messaging_pub=peer_box.public_key_b64(recipient_msg), expires_at=_expires(),
    )
    wire = json.dumps(signed.to_dict())
    assert "NEVER VISIBLE IN RELAY" not in wire
    _, body = open_task(signed, recipient_peer_id=recipient.peer_id,
                        recipient_messaging_key=recipient_msg, expected_kind="llm_request")
    assert body["prompt"] == "NEVER VISIBLE IN RELAY"
    tampered = signed.to_dict()
    tampered["payload"]["ciphertext"] += "A"
    with pytest.raises(SignatureError):
        open_task(tampered, recipient_peer_id=recipient.peer_id,
                  recipient_messaging_key=recipient_msg, expected_kind="llm_request")


def test_ice_udp_direct_transport_exchanges_chunked_json_without_relay(monkeypatch):
    monkeypatch.setenv("RYNMESH_P2P_STUN", "off")
    monkeypatch.delenv("RYNMESH_P2P_REQUIRE_PUBLIC", raising=False)

    async def scenario():
        consumer = new_connection(controlling=True)
        provider = new_connection(controlling=False)
        try:
            consumer_signal, provider_signal = await asyncio.gather(
                gather_signal(consumer), gather_signal(provider),
            )
            await asyncio.gather(
                apply_remote_signal(consumer, provider_signal),
                apply_remote_signal(provider, consumer_signal),
            )
            await asyncio.gather(consumer.connect(), provider.connect())
            consumer_evidence = selected_pair(consumer)
            provider_evidence = selected_pair(provider)
            assert consumer_evidence["transport"] == "ice_udp_direct"
            assert provider_evidence["relay_used"] is False
            assert consumer_evidence["remote"]["type"] != "relay"

            request = {"ciphertext": "x" * 5000}
            response = {"ciphertext": "y" * 7000}

            async def provider_side():
                received, request_bytes = await receive_json(provider, timeout_s=5)
                assert received == request and request_bytes > 5000
                await send_json(provider, response, timeout_s=5)

            provider_task = asyncio.create_task(provider_side())
            await send_json(consumer, request, timeout_s=5)
            received, response_bytes = await receive_json(consumer, timeout_s=5)
            await provider_task
            assert received == response and response_bytes > 7000
        finally:
            await consumer.close()
            await provider.close()

    asyncio.run(scenario())


def test_public_nat_mode_refuses_to_fall_back_to_host_candidate(monkeypatch):
    monkeypatch.setenv("RYNMESH_P2P_STUN", "off")
    monkeypatch.setenv("RYNMESH_P2P_REQUIRE_PUBLIC", "1")

    async def scenario():
        connection = new_connection(controlling=True)
        try:
            with pytest.raises(P2PError, match="server-reflexive STUN candidate"):
                await gather_signal(connection)
        finally:
            await connection.close()

    asyncio.run(scenario())


def test_restart_recovery_fails_interrupted_order_and_releases_hold(tmp_path):
    orders = TaskOrderStore(tmp_path / "orders")
    balance = TaskBalanceLedger(tmp_path / "balance.json")
    orders.transition(task_id="task_interrupted", state="created")
    orders.transition(task_id="task_interrupted", state="accepted")
    orders.transition(task_id="task_interrupted", state="running")
    balance.hold(
        task_id="task_interrupted",
        amount=0.25,
        service_id="private-service",
        provider_peer_id="provider-peer",
    )

    _recover_consumer_orders(orders, balance)
    _recover_consumer_orders(orders, balance)

    assert orders.get("task_interrupted")["state"] == "failed"
    assert balance.summary()["held"] == 0.0
    assert balance.summary()["available"] == 100.0
    releases = [event for event in balance.events() if event["kind"] == "release"]
    assert len(releases) == 1
    assert releases[0]["reason"] == "consumer_restart_recovery"


def test_restart_recovery_completes_received_result_settlement(tmp_path):
    orders = TaskOrderStore(tmp_path / "orders")
    balance = TaskBalanceLedger(tmp_path / "balance.json")
    orders.claim(task_id="task_received", bindings={
        "provider_peer_id": "provider-peer", "service_id": "private-service",
        "idempotency_key": "task_received", "request_fingerprint": "fingerprint",
    })
    orders.transition(task_id="task_received", state="accepted")
    orders.transition(task_id="task_received", state="running")
    orders.checkpoint(task_id="task_received", metadata={
        "settlement_pending": True, "provider_peer_id": "provider-peer",
        "service_id": "private-service", "network_id": "rynmesh-main",
        "amount": 0.1, "input_tokens": 4, "output_tokens": 2, "duration_ms": 7,
    })
    balance.hold(
        task_id="task_received", amount=0.25, service_id="private-service",
        provider_peer_id="provider-peer", idempotency_key="task_received",
        request_fingerprint="fingerprint",
    )

    _recover_consumer_orders(orders, balance)
    _recover_consumer_orders(orders, balance)

    assert orders.get("task_received")["state"] == "succeeded"
    assert balance.summary()["held"] == 0.0
    assert balance.summary()["available"] == 99.9
    settlements = [event for event in balance.events() if event["kind"] == "settle"]
    assert len(settlements) == 1


class _FakeAdapter:
    def __init__(self):
        self.calls = 0
        self.cancelled = []

    def health(self): return {"ok": True, "model": "fake-test-only"}
    def models(self): return [{"id": "fake-test-only"}]
    def capabilities(self): return {"chat_completions": True}
    def metrics(self): return {"requests": self.calls}
    def shutdown(self): pass
    def cancel(self, task_id): self.cancelled.append(task_id); return True
    def infer(self, *, prompt, max_tokens, task_id, timeout_s):
        self.calls += 1
        return {"text": "provider output", "model": "fake-test-only", "input_tokens": 5,
                "output_tokens": 2, "duration_ms": 7}


class _BlockingAdapter(_FakeAdapter):
    def __init__(self):
        super().__init__()
        self.started = threading.Event()
        self.release = threading.Event()

    def infer(self, *, prompt, max_tokens, task_id, timeout_s):
        self.calls += 1
        self.started.set()
        assert self.release.wait(timeout=5)
        return {"text": "provider output", "model": "fake-test-only", "input_tokens": 5,
                "output_tokens": 2, "duration_ms": 7}


def test_provider_executes_and_settles_once_without_persisting_bodies(tmp_path):
    net = tmp_path / "net"
    provider = RynmeshStore(home=tmp_path / "provider", network_dir=net)
    consumer = RynmeshStore(home=tmp_path / "consumer", network_dir=net)
    provider_msg = peer_box.load_or_create_messaging_key(tmp_path / "provider-msg")
    consumer_msg = peer_box.load_or_create_messaging_key(tmp_path / "consumer-msg")
    adapter = _FakeAdapter()
    manifest = LLMPackageManifest(
        package_id="svc", mode="openai_compatible", public_model_alias="alias",
        base_url="http://127.0.0.1:1",
    )
    orders = TaskOrderStore(tmp_path / "orders")
    balance = TaskBalanceLedger(tmp_path / "provider-balance.json")
    service = ProviderService(manifest=manifest, adapter=adapter, store=provider,
                              task_store=orders, balance=balance, messaging_key=provider_msg)
    request = seal_task(
        body={"task_id": "task_same", "service_id": "svc", "prompt": "TOP SECRET PROMPT",
              "max_tokens": 8, "max_amount": 1, "reply_messaging_pub": peer_box.public_key_b64(consumer_msg)},
        task_id="task_same", kind="llm_request", sender_peer_id=consumer.peer_id,
        recipient_peer_id=provider.peer_id, sender_signing_key=consumer.private_key_bytes,
        recipient_messaging_pub=peer_box.public_key_b64(provider_msg), expires_at=_expires(),
    ).to_dict()
    first = service.handle(request)
    second = service.handle(request)
    assert first == second and adapter.calls == 1
    _, result = open_task(first, recipient_peer_id=consumer.peer_id,
                          recipient_messaging_key=consumer_msg, expected_kind="llm_response")
    assert result["output"] == "provider output" and result["state"] == "succeeded"
    disk = "\n".join(path.read_text(encoding="utf-8") for path in tmp_path.rglob("*.json"))
    assert "TOP SECRET PROMPT" not in disk
    assert "provider output" not in disk
    settlement = sign_payload({
        "kind": "llm_settlement", "task_id": "task_same", "from_peer_id": consumer.peer_id,
        "to_peer_id": provider.peer_id, "amount": result["amount"], "service_id": "svc",
        "settlement_id": "settle:task_same",
    }, private_key_bytes=consumer.private_key_bytes).to_dict()
    attacker = RynmeshStore(home=tmp_path / "attacker", network_dir=net)
    forged = sign_payload({
        "kind": "llm_settlement", "task_id": "task_same", "from_peer_id": attacker.peer_id,
        "to_peer_id": provider.peer_id, "amount": result["amount"], "service_id": "svc",
        "settlement_id": "settle:task_same",
    }, private_key_bytes=attacker.private_key_bytes).to_dict()
    with pytest.raises(Exception, match="not the task consumer"):
        service.settle_earning(forged)
    one = service.settle_earning(settlement)
    two = service.settle_earning(settlement)
    assert one["event_id"] == two["event_id"] == "earning:task_same"
    assert balance.summary()["earned"] == result["amount"]


def test_provider_concurrent_duplicate_executes_once(tmp_path):
    net = tmp_path / "net"
    provider = RynmeshStore(home=tmp_path / "provider", network_dir=net)
    consumer = RynmeshStore(home=tmp_path / "consumer", network_dir=net)
    provider_msg = peer_box.load_or_create_messaging_key(tmp_path / "provider-msg")
    consumer_msg = peer_box.load_or_create_messaging_key(tmp_path / "consumer-msg")
    adapter = _BlockingAdapter()
    service = ProviderService(
        manifest=LLMPackageManifest(
            package_id="svc", mode="openai_compatible", public_model_alias="alias",
            base_url="http://127.0.0.1:1", timeout_seconds=2,
        ),
        adapter=adapter, store=provider, task_store=TaskOrderStore(tmp_path / "orders"),
        balance=TaskBalanceLedger(tmp_path / "balance.json"), messaging_key=provider_msg,
    )
    request = seal_task(
        body={"task_id": "task_concurrent", "idempotency_key": "same-request",
              "service_id": "svc", "prompt": "private", "max_tokens": 8, "max_amount": 1,
              "reply_messaging_pub": peer_box.public_key_b64(consumer_msg)},
        task_id="task_concurrent", kind="llm_request", sender_peer_id=consumer.peer_id,
        recipient_peer_id=provider.peer_id, sender_signing_key=consumer.private_key_bytes,
        recipient_messaging_pub=peer_box.public_key_b64(provider_msg), expires_at=_expires(),
    ).to_dict()
    responses = []
    threads = [threading.Thread(target=lambda: responses.append(service.handle(request))) for _ in range(2)]
    threads[0].start()
    assert adapter.started.wait(timeout=2)
    threads[1].start()
    adapter.release.set()
    for thread in threads:
        thread.join(timeout=5)
        assert not thread.is_alive()
    assert adapter.calls == 1
    assert len(responses) == 2 and responses[0] == responses[1]
    assert service.task_store.get("task_concurrent")["state"] == "succeeded"


def test_signed_cancel_reaches_running_provider_and_rejects_other_identity(tmp_path):
    net = tmp_path / "net"
    provider = RynmeshStore(home=tmp_path / "provider", network_dir=net)
    consumer = RynmeshStore(home=tmp_path / "consumer", network_dir=net)
    attacker = RynmeshStore(home=tmp_path / "attacker", network_dir=net)
    provider_msg = peer_box.load_or_create_messaging_key(tmp_path / "provider-msg")
    consumer_msg = peer_box.load_or_create_messaging_key(tmp_path / "consumer-msg")
    adapter = _BlockingAdapter()
    service = ProviderService(
        manifest=LLMPackageManifest(
            package_id="svc", mode="openai_compatible", public_model_alias="alias",
            base_url="http://127.0.0.1:1", timeout_seconds=2,
        ),
        adapter=adapter, store=provider, task_store=TaskOrderStore(tmp_path / "orders"),
        balance=TaskBalanceLedger(tmp_path / "balance.json"), messaging_key=provider_msg,
    )
    request = seal_task(
        body={"task_id": "task_cancel_running", "idempotency_key": "cancel-running",
              "service_id": "svc", "prompt": "private", "max_tokens": 8, "max_amount": 1,
              "reply_messaging_pub": peer_box.public_key_b64(consumer_msg)},
        task_id="task_cancel_running", kind="llm_request", sender_peer_id=consumer.peer_id,
        recipient_peer_id=provider.peer_id, sender_signing_key=consumer.private_key_bytes,
        recipient_messaging_pub=peer_box.public_key_b64(provider_msg), expires_at=_expires(),
    ).to_dict()
    responses = []
    worker = threading.Thread(target=lambda: responses.append(service.handle(request)))
    worker.start()
    assert adapter.started.wait(timeout=2)

    def cancellation(sender: RynmeshStore) -> dict:
        return sign_payload({
            "kind": "llm_cancel", "task_id": "task_cancel_running",
            "from_peer_id": sender.peer_id, "to_peer_id": provider.peer_id,
            "service_id": "svc", "cancel_id": "cancel:task_cancel_running",
        }, private_key_bytes=sender.private_key_bytes).to_dict()

    with pytest.raises(Exception, match="not the task consumer"):
        service.cancel_signed(cancellation(attacker))
    assert service.cancel_signed(cancellation(consumer)) is True
    assert service.task_store.get("task_cancel_running")["state"] == "cancelled"
    adapter.release.set()
    worker.join(timeout=5)
    assert not worker.is_alive()
    _, result = open_task(
        responses[0], recipient_peer_id=consumer.peer_id,
        recipient_messaging_key=consumer_msg, expected_kind="llm_response",
    )
    assert result["state"] == "cancelled"
    assert result["error_code"] == "consumer_cancelled"
    assert adapter.cancelled == ["task_cancel_running"]


def test_consumer_rejects_response_signed_by_another_provider(tmp_path):
    net = tmp_path / "net"
    expected = RynmeshStore(home=tmp_path / "expected", network_dir=net)
    rogue = RynmeshStore(home=tmp_path / "rogue", network_dir=net)
    consumer = RynmeshStore(home=tmp_path / "consumer", network_dir=net)
    consumer_msg = peer_box.load_or_create_messaging_key(tmp_path / "consumer-msg")
    response = seal_task(
        body={"task_id": "task_response", "service_id": "svc", "state": "succeeded",
              "input_tokens": 1, "output_tokens": 1, "duration_ms": 1, "amount": 0.001,
              "output": "rogue"},
        task_id="task_response", kind="llm_response", sender_peer_id=rogue.peer_id,
        recipient_peer_id=consumer.peer_id, sender_signing_key=rogue.private_key_bytes,
        recipient_messaging_pub=peer_box.public_key_b64(consumer_msg), expires_at=_expires(),
    ).to_dict()
    with pytest.raises(Exception, match="not the selected provider"):
        _open_provider_response(
            response, recipient_peer_id=consumer.peer_id, messaging_key=consumer_msg,
            task_id="task_response", provider_peer_id=expected.peer_id, service_id="svc",
        )


def test_provider_explicitly_rejects_capacity_and_cancel_is_terminal(tmp_path):
    net = tmp_path / "net"
    provider = RynmeshStore(home=tmp_path / "provider", network_dir=net)
    consumer = RynmeshStore(home=tmp_path / "consumer", network_dir=net)
    provider_msg = peer_box.load_or_create_messaging_key(tmp_path / "provider-msg")
    consumer_msg = peer_box.load_or_create_messaging_key(tmp_path / "consumer-msg")
    adapter = _FakeAdapter()
    manifest = LLMPackageManifest(
        package_id="svc", mode="openai_compatible", public_model_alias="alias",
        base_url="http://127.0.0.1:1", max_concurrent=1,
    )
    orders = TaskOrderStore(tmp_path / "orders")
    service = ProviderService(
        manifest=manifest, adapter=adapter, store=provider, task_store=orders,
        balance=TaskBalanceLedger(tmp_path / "balance.json"), messaging_key=provider_msg,
    )
    request = seal_task(
        body={"task_id": "busy_task", "service_id": "svc", "prompt": "body",
              "max_tokens": 8, "max_amount": 1,
              "reply_messaging_pub": peer_box.public_key_b64(consumer_msg)},
        task_id="busy_task", kind="llm_request", sender_peer_id=consumer.peer_id,
        recipient_peer_id=provider.peer_id, sender_signing_key=consumer.private_key_bytes,
        recipient_messaging_pub=peer_box.public_key_b64(provider_msg), expires_at=_expires(),
    ).to_dict()
    assert service._slots.acquire(blocking=False)
    try:
        encrypted = service.handle(request)
    finally:
        service._slots.release()
    _, result = open_task(encrypted, recipient_peer_id=consumer.peer_id,
                          recipient_messaging_key=consumer_msg, expected_kind="llm_response")
    assert result["state"] == "rejected" and result["error_code"] == "capacity_exhausted"
    assert adapter.calls == 0
    orders.transition(task_id="cancel_me", state="created", metadata={"service_id": "svc"})
    assert service.cancel("cancel_me") is True
    assert orders.get("cancel_me")["state"] == "cancelled"
    assert adapter.cancelled == ["cancel_me"]
