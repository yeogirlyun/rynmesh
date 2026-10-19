"""Actual usage takes precedence at accounting, persistence and public boundaries."""
import copy
import json

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from test_agent_runtime import advance, make_runtime
from test_inference_api import api  # noqa: F401
from test_llm_package import _FakeAdapter

from rynmesh.agent_runtime.bridge import AgentModelBridge
from rynmesh.agent_runtime.runtime import AgentRuntime
from rynmesh.agent_runtime.store import AgentStore
from rynmesh.llm_package import cli_models, routes
from rynmesh.llm_package.cli_adapter import CLIAgentAdapter
from rynmesh.llm_package.manifest import LLMPackageManifest, Pricing, save_manifest
from rynmesh.llm_package.routes import ProviderService, _expires, _open_provider_response
from rynmesh.llm_package.task_balance import TaskBalanceLedger
from rynmesh.llm_package.task_protocol import TaskOrderStore, seal_task
from rynmesh.services import peer_box
from rynmesh.store import RynmeshStore


def legacy(marker):
    return {"text": "Authored answer", "message": {"role": "assistant", "content": "Authored answer"},
            "finish_reason": "stop", "input_tokens": 120, "output_tokens": 80,
            "reported_usage": copy.deepcopy(marker), "duration_ms": 1}


COUNTS = [pytest.param(None, (None, None), 30, id="explicit-null"),
          pytest.param({"input_tokens": 7}, (7, None), 30, id="partial"),
          pytest.param({"input_tokens": 0, "output_tokens": 0}, (0, 0), 0, id="reported-zero")]


@pytest.mark.parametrize("source", ["local_service", "peer"])
@pytest.mark.parametrize("marker,expected,quota", COUNTS)
def test_bridge_runtime_reservation_ignores_conflicting_legacy_estimate(tmp_path, source, marker, expected, quota):
    normalized = AgentModelBridge._response(legacy(marker), {"source": source}, [], {})
    runtime, model, tools, key = make_runtime(tmp_path, normalized)
    task_id = runtime.create({"message": "Authored fixture"})["id"]
    advance(runtime)
    row = runtime.store.read()["tasks"][task_id]
    record = row["budget"]["records"][0]
    assert (record["input_tokens"], record["output_tokens"]) == expected
    expected_charge = sum(value if value is not None else record[bound] for value, bound in zip(
        expected, ("input_reservation", "output_reservation"), strict=True))
    assert record["charged_tokens"] == expected_charge
    before = runtime.get(task_id)["budget"]
    assert before["lifetime"]["usage_complete"] is (None not in expected)
    reopened = AgentRuntime(AgentStore(tmp_path, key), model, tools, runtime.catalog)
    reopened.reconcile()
    assert reopened.get(task_id)["budget"] == before


@pytest.mark.parametrize("protocol,path", [("chat", "chat/completions"), ("responses", "responses"), ("messages", "messages")])
@pytest.mark.parametrize("stream", [False, True])
@pytest.mark.parametrize("marker,expected,quota", COUNTS)
def test_api_formats_and_quota_use_reported_marker_even_when_events_disagree(api, protocol, path, stream, marker, expected, quota):  # noqa: F811
    client, adapter, key = api
    def complete(body, *, task_id, timeout_s, on_event=None):
        if on_event is not None:
            on_event({"choices": [{"index": 0, "delta": {"role": "assistant", "content": "Authored answer"}, "finish_reason": None}]})
            on_event({"choices": [], "usage": {"prompt_tokens": 120, "completion_tokens": 80, "total_tokens": 200}})
            on_event({"choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]})
        return legacy(marker)
    adapter.chat = complete
    body = {"model": "local/test", "stream": stream}
    if protocol == "responses":
        body.update(input="Authored fixture", max_output_tokens=30)
    else:
        body.update(messages=[{"role": "user", "content": "Authored fixture"}], max_tokens=30)
        if protocol == "chat" and stream:
            body["stream_options"] = {"include_usage": True}
    response = client.post("/v1/" + path, json=body)
    assert response.status_code == 200, response.text
    if stream:
        events = [json.loads(line[6:]) for line in response.text.splitlines() if line.startswith("data: ") and line != "data: [DONE]"]
        if protocol == "chat":
            usages = [event["usage"] for event in events if "usage" in event]
            assert len(usages) == 1
            usage = usages[0]
        elif protocol == "messages":
            usage = next(event["usage"] for event in events if event.get("type") == "message_delta")
        else:
            usage = next(event["response"]["usage"] for event in events if event.get("type") == "response.completed")
    else:
        usage = response.json()["usage"]
    fields = ("prompt_tokens", "completion_tokens") if protocol == "chat" else ("input_tokens", "output_tokens")
    assert tuple(usage[field] for field in fields) == expected
    project = next(item for item in client.get("/api/local/llm/api-access").json()["keys"] if item["id"] == key["id"])
    assert project["used_output_tokens"] == quota


@pytest.mark.parametrize("minimum", [0.001, 0.0], ids=["default-minimum", "zero-minimum"])
def test_provider_reported_zero_keeps_zero_counts_and_configured_minimum(tmp_path, minimum):
    provider = RynmeshStore(home=tmp_path / "provider", network_dir=tmp_path / "net")
    consumer = RynmeshStore(home=tmp_path / "consumer", network_dir=tmp_path / "net")
    pk = peer_box.load_or_create_messaging_key(provider.home / "fixture-key")
    ck = peer_box.load_or_create_messaging_key(consumer.home / "fixture-key")
    adapter = _FakeAdapter()
    adapter.infer = lambda **kwargs: legacy({"input_tokens": 0, "output_tokens": 0})
    service = ProviderService(manifest=LLMPackageManifest(package_id="svc", mode="openai_compatible", public_model_alias="fixture",
        base_url="http://127.0.0.1:1", pricing=Pricing(minimum=minimum)), adapter=adapter, store=provider,
        task_store=TaskOrderStore(tmp_path / "orders"), balance=TaskBalanceLedger(tmp_path / "balance.json"),
        messaging_key=pk, access_check=lambda *_: {})
    request = seal_task(body={"task_id": "zero", "service_id": "svc", "prompt": "Authored fixture", "max_tokens": 8,
        "max_amount": 1, "reply_messaging_pub": peer_box.public_key_b64(ck)}, task_id="zero", kind="llm_request",
        sender_peer_id=consumer.peer_id, recipient_peer_id=provider.peer_id, sender_signing_key=consumer.private_key_bytes,
        recipient_messaging_pub=peer_box.public_key_b64(pk), expires_at=_expires(300)).to_dict()
    _, value = _open_provider_response(service.handle(request), recipient_peer_id=consumer.peer_id, messaging_key=ck,
        task_id="zero", provider_peer_id=provider.peer_id, service_id="svc")
    assert value["state"] == "succeeded"
    assert value["input_tokens"] == value["output_tokens"] == 0
    assert value["usage_complete"] is True and value["billing_status"] == "priced"
    assert value["amount"] == minimum  # Independent explicit contract; no production _price call as oracle.


@pytest.mark.parametrize("marker,expected,quota", COUNTS[:2])
def test_old_peer_sealed_estimates_cannot_reappear_in_status_billing(tmp_path, marker, expected, quota):
    provider = RynmeshStore(home=tmp_path / "provider", network_dir=tmp_path / "net")
    consumer = RynmeshStore(home=tmp_path / "consumer", network_dir=tmp_path / "net")
    ck = peer_box.load_or_create_messaging_key(consumer.home / "fixture-key")
    signed = seal_task(body={**legacy(marker), "task_id": "old-peer", "service_id": "svc", "state": "succeeded", "amount": 0.001},
        task_id="old-peer", kind="llm_response", sender_peer_id=provider.peer_id, recipient_peer_id=consumer.peer_id,
        sender_signing_key=provider.private_key_bytes, recipient_messaging_pub=peer_box.public_key_b64(ck), expires_at=_expires(300)).to_dict()
    _, value = _open_provider_response(signed, recipient_peer_id=consumer.peer_id, messaging_key=ck,
        task_id="old-peer", provider_peer_id=provider.peer_id, service_id="svc")
    assert tuple(value[field] for field in ("input_tokens", "output_tokens")) == expected
    assert value["amount"] is None and value["billing_pending_usage"] is True


@pytest.mark.parametrize("structured", [False, True], ids=["infer", "chat"])
def test_local_order_response_terminal_metadata_retry_and_restart_preserve_actual(tmp_path, monkeypatch, structured):
    node = RynmeshStore(home=tmp_path / "node", network_dir=tmp_path / "net")
    key = peer_box.load_or_create_messaging_key(node.home / "fixture-key")
    manifest = LLMPackageManifest(package_id="codex-cli", mode="codex_cli", adapter="codex_cli",
        runtime="external", public_model_alias="fixture", capabilities=["text"])
    manifest_path = node.home / "llm/codex-cli/manifest.json"
    save_manifest(manifest, manifest_path)
    monkeypatch.setattr(routes, "connect_cli", lambda **kwargs: {"manifest": str(manifest_path)})
    monkeypatch.setattr(CLIAgentAdapter, "_command_path", lambda self: "fixture-no-process")
    monkeypatch.setattr(CLIAgentAdapter, "health", lambda self: {"ok": True})
    monkeypatch.setattr(cli_models, "list_models", lambda *args, **kwargs: [{"id": "fixture", "name": "fixture", "default": True,
        "context_window": 32768, "context_window_known": True}])
    calls = []
    def result(*args, **kwargs):
        calls.append(True)
        return legacy(None)
    monkeypatch.setattr(CLIAgentAdapter, "infer", result)
    monkeypatch.setattr(CLIAgentAdapter, "chat", result)
    def install():
        app = FastAPI()
        commands = routes.install_llm_routes(app, store=node, home=node.home, messaging_key=key,
            resolve_endpoint=lambda _: pytest.fail("local must not use peer transport"),
            resolve_pubkey=lambda _: pytest.fail("local must not use peer transport"))
        return app, commands
    app, _ = install()
    with TestClient(app) as client:
        assert client.post("/api/local/llm/cli-services/codex_cli/setup").status_code == 200
        assert client.put("/api/local/llm/privacy", json={"result_retention_seconds": 3600}).status_code == 200
        body = {"task_id": "local-actual", "provider_peer_id": node.peer_id, "service_id": "codex-cli",
                "prompt": "Authored fixture", "max_tokens": 8}
        if structured:
            body["chat"] = {"messages": [{"role": "user", "content": "Authored fixture"}], "max_tokens": 8}
        before = len(calls)
        for _ in range(2):
            reply = client.post("/api/local/llm/orders", json=body)
            assert reply.status_code == 200, reply.text
            value = reply.json()
            assert value["input_tokens"] is value["output_tokens"] is None
            assert value["amount"] == 0 and value["billing_basis"] == "own_request"
        assert len(calls) == before + 1
    row = TaskOrderStore(node.home / "llm/consumer-orders").get("local-actual")
    terminal = row["history"][-1]
    assert terminal["reported_usage"] == {"input_tokens": None, "output_tokens": None}
    assert terminal["usage_complete"] is False and terminal["input_tokens"] is None
    _, restarted = install()
    assert restarted.status("local-actual")["reported_usage"] == terminal["reported_usage"]


@pytest.mark.parametrize("state", ["running", "succeeded"])
def test_legacy_checkpoint_reported_unknown_wins_over_estimates_without_refund(tmp_path, monkeypatch, state):
    orders = TaskOrderStore(tmp_path / "orders")
    balance = TaskBalanceLedger(tmp_path / "balance.json")
    bindings = {"service_id": "svc", "provider_peer_id": "provider", "idempotency_key": "legacy", "request_fingerprint": "fixture"}
    orders.claim(task_id="legacy", bindings=bindings)
    orders.transition(task_id="legacy", state="accepted")
    orders.transition(task_id="legacy", state="running")
    pending = {**legacy(None), "settlement_pending": True, "amount": 0.1, "service_id": "svc", "provider_peer_id": "provider"}
    pending.pop("message")
    pending.pop("text")
    orders.checkpoint(task_id="legacy", metadata=pending)
    if state == "succeeded":
        orders.transition(task_id="legacy", state="succeeded", metadata=pending)
    balance.hold(task_id="legacy", amount=0.25, **bindings)
    monkeypatch.setattr(routes, "dispatch_settlement", lambda *args, **kwargs: pytest.fail("unknown usage cannot settle"))
    before = balance.summary()
    for _ in range(2):
        routes._recover_consumer_orders(orders, balance, object())
        assert balance.summary() == before
    terminal = orders.get("legacy")["history"][-1]
    assert terminal["reported_usage"] == {"input_tokens": None, "output_tokens": None}
    assert terminal["input_tokens"] is terminal["output_tokens"] is terminal["amount"] is None
    assert not any(event["kind"] != "hold" for event in balance.events())


@pytest.mark.parametrize("marker,expected,quota", COUNTS[:2])
def test_legacy_acknowledged_status_and_list_project_actual_without_changing_history(tmp_path, marker, expected, quota):
    node = RynmeshStore(home=tmp_path / "node", network_dir=tmp_path / "net")
    key = peer_box.load_or_create_messaging_key(node.home / "fixture-key")
    orders = TaskOrderStore(node.home / "llm/consumer-orders")
    orders.claim(task_id="legacy-public", bindings={"provider_peer_id": "remote-fixture", "service_id": "svc"})
    orders.transition(task_id="legacy-public", state="accepted")
    orders.transition(task_id="legacy-public", state="running")
    metadata = legacy(marker)
    for name in ("message", "text"):
        metadata.pop(name)
    orders.transition(task_id="legacy-public", state="succeeded", metadata={**metadata, "amount": 0.001, "billing_status": "priced"})
    orders.mark_acknowledged("legacy-public")
    before = copy.deepcopy(orders.get("legacy-public"))
    app = FastAPI()
    routes.install_llm_routes(app, store=node, home=node.home, messaging_key=key,
        resolve_endpoint=lambda _: "", resolve_pubkey=lambda _: "")
    with TestClient(app) as client:
        status = client.get("/api/local/llm/orders/legacy-public").json()
        listing = next(row for row in client.get("/api/local/llm/orders").json()["orders"] if row["task_id"] == "legacy-public")
        for value in (status, listing):
            assert tuple(value[name] for name in ("input_tokens", "output_tokens")) == expected
            assert value["amount"] is None and value["billing_status"] == "pending_usage"
    assert orders.get("legacy-public") == before
