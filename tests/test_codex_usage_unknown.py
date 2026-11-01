"""Codex usage stays reported-or-unknown through protocol, API and Agent state.

Every Codex event is an authored fixture. No Codex process/provider is invoked.
"""
import copy
import json
import time
from collections import deque
from types import SimpleNamespace

import pytest
from test_agent_runtime import advance, assistant, make_runtime
from test_inference_api import api  # noqa: F401

from rynmesh.agent_runtime.bridge import AgentModelBridge
from rynmesh.agent_runtime.runtime import AgentRuntime
from rynmesh.agent_runtime.store import AgentStore
from rynmesh.credits import TASK_BALANCE_CATEGORY
from rynmesh.llm_package import codex_session, routes
from rynmesh.llm_package.api_formats import StreamFormat, convert_response
from rynmesh.llm_package.chat import completion
from rynmesh.llm_package.task_balance import TaskBalanceLedger
from rynmesh.llm_package.task_protocol import TaskOrderStore
from rynmesh.store import RynmeshStore


class FixtureRPC(SimpleNamespace):
    def call(self, method, params):
        assert method == "turn/start" and params["threadId"] == "fixture-thread"
        return {"turn": {"id": "fixture-turn"}}

    def receive(self):
        raise AssertionError("Authored event fixture exhausted")


def decode(events, *, names=None, schemas=None):
    rpc = FixtureRPC(pending=deque(copy.deepcopy(events)))
    answer, usage, calls = codex_session._turn(rpc, "fixture-thread", {}, "authored prompt",
        deadline=time.monotonic() + 1, names=names, schemas=schemas)
    return codex_session._result(answer, usage, calls, "auto", "offline prompt " * 300, time.monotonic())


def completed_events(*usage_events):
    return [*usage_events,
        {"method": "item/completed", "params": {"threadId": "fixture-thread", "turnId": "fixture-turn",
            "item": {"type": "agentMessage", "text": "Authored answer"}}},
        {"method": "turn/completed", "params": {"threadId": "fixture-thread",
            "turn": {"id": "fixture-turn", "status": "completed"}}}]


def usage_event(value):
    return {"method": "thread/tokenUsage/updated", "params": {
        "threadId": "fixture-thread", "turnId": "fixture-turn", "tokenUsage": value}}


def test_turn_reuses_preparsed_image_inputs_without_reading_the_message_body(monkeypatch):
    from rynmesh.llm_package import cli_images

    images = [{"type": "image", "url": "data:image/png;base64,authored-fixture"}]
    inputs = []
    rpc = FixtureRPC(pending=deque(completed_events()))

    def call(method, params):
        assert method == "turn/start"
        inputs.extend(params["input"])
        return {"turn": {"id": "fixture-turn"}}

    rpc.call = call
    monkeypatch.setattr(cli_images, "image_inputs", lambda _: pytest.fail("preparsed images must not be decoded again"))
    answer, _, _ = codex_session._turn(rpc, "fixture-thread", {"_image_inputs": images}, "authored prompt",
        deadline=time.monotonic() + 1)
    assert answer == "Authored answer"
    assert inputs == [{"type": "text", "text": "authored prompt", "text_elements": []}, *images]


@pytest.mark.parametrize("token_usage", [None, [], "invalid", True, {"last": None}, {"last": []},
    {"last": "invalid"}, {"last": True}, {"last": {"inputTokens": float("nan"), "outputTokens": float("inf")}},
    {"last": {"inputTokens": True, "outputTokens": False}},
    {"last": {"inputTokens": -1, "outputTokens": -2}},
    {"last": {"inputTokens": "7", "outputTokens": 3.0}}], ids=[
        "absent", "list", "text", "bool", "last-null", "last-list", "last-text", "last-bool", "nonfinite", "booleans", "negative", "not-integers"])
def test_turn_malformed_usage_never_crashes_or_fabricates_tokens(token_usage):
    result = decode(completed_events(usage_event(token_usage)))
    assert result["input_tokens"] is None and result["output_tokens"] is None
    json.dumps(result, allow_nan=False)


@pytest.mark.parametrize("usage,expected", [
    ({}, (None, None)), (None, (None, None)),
    ({"inputTokens": 0, "outputTokens": 0}, (0, 0)),
    ({"inputTokens": 11, "outputTokens": 3}, (11, 3)),
    ({"inputTokens": 11}, (11, None)),
    ({"inputTokens": False, "outputTokens": 3}, (None, 3)),
])
def test_decoder_preserves_only_reported_nonnegative_integer_fields(usage, expected):
    result = codex_session._result("Authored answer", usage, [], "auto", "long prompt " * 300, time.monotonic())
    assert (result["input_tokens"], result["output_tokens"]) == expected
    normalized = AgentModelBridge._response(result, {"source": "local_service"}, [], {})
    assert tuple(normalized["usage"].values()) == expected


@pytest.mark.parametrize("tool_first", [False, True], ids=["completed-without-usage", "tool-before-usage"])
def test_decoder_bridge_actual_runtime_keeps_unknown_charge_and_reopen(tmp_path, tool_first):
    if tool_first:
        events = [{"id": 1, "method": "item/tool/call", "params": {
            "threadId": "fixture-thread", "turnId": "fixture-turn", "tool": "ryn_client_0",
            "arguments": {"service_id": "fixture"}}}]
        result = decode(events, names={"ryn_client_0": "inspect_service"}, schemas={"ryn_client_0": {
            "type": "object", "properties": {"service_id": {"type": "string"}}, "required": ["service_id"]}})
    else:
        result = decode(completed_events())
    normalized = AgentModelBridge._response(result, {"source": "local_service"}, [], {})
    runtime, model, tools, key = make_runtime(tmp_path, normalized, assistant("Read complete."))
    task_id = runtime.create({"message": "Offline authored fixture only"})["id"]
    advance(runtime, 3 if tool_first else 1)
    assert runtime.get(task_id)["status"] == "completed"
    assert len(tools.calls) == int(tool_first)
    row = runtime.store.read()["tasks"][task_id]
    record = row["budget"]["records"][0]
    assert record["input_tokens"] is None and record["output_tokens"] is None
    assert record["charged_tokens"] == record["input_reservation"] + record["output_reservation"] > 0
    before = copy.deepcopy(runtime.get(task_id)["budget"])
    assert before["lifetime"]["input_tokens"] is None
    assert before["lifetime"]["output_tokens"] is None
    assert before["lifetime"]["total_tokens"] is None
    assert before["lifetime"]["usage_complete"] is False
    reopened = AgentRuntime(AgentStore(tmp_path, key), model, tools, runtime.catalog)
    reopened.reconcile()
    assert reopened.get(task_id)["budget"] == before


def test_partial_reported_usage_charges_unknown_component_from_reservation(tmp_path):
    result = decode(completed_events(usage_event({"last": {"inputTokens": 7}})))
    normalized = AgentModelBridge._response(result, {"source": "local_service"}, [], {})
    runtime, _, _, _ = make_runtime(tmp_path, normalized)
    task_id = runtime.create({"message": "Authored partial-usage fixture"})["id"]
    advance(runtime)
    record = runtime.store.read()["tasks"][task_id]["budget"]["records"][0]
    assert record["input_tokens"] == 7 and record["output_tokens"] is None
    assert record["charged_tokens"] == 7 + record["output_reservation"]
    assert runtime.get(task_id)["budget"]["lifetime"]["usage_complete"] is False


@pytest.mark.parametrize("protocol", ["chat", "responses", "messages"])
@pytest.mark.parametrize("counters", [(None, None), (7, None), (None, 3), (0, 0), (7, 3)])
def test_completed_api_formats_allow_unknown_without_zero_or_typeerror(protocol, counters):
    result = codex_session._result("Authored answer", {"inputTokens": counters[0], "outputTokens": counters[1]},
                                   [], "auto", "fixture prompt", time.monotonic())
    chat = completion(result, "offline/model", "fixture")
    converted = convert_response(chat, protocol)
    usage = converted["usage"]
    keys = ("prompt_tokens", "completion_tokens") if protocol == "chat" else ("input_tokens", "output_tokens")
    assert tuple(usage[key] for key in keys) == counters
    if protocol != "messages":
        assert usage["total_tokens"] == (sum(counters) if None not in counters else None)
    json.dumps(converted, allow_nan=False)
    formatter = StreamFormat(protocol, "offline/model", "fixture")
    initial = formatter.start()
    chunks = formatter.delta({"choices": [{"index": 0, "delta": {"content": "Authored answer"}, "finish_reason": None}]})
    final = formatter.finish(chat)
    assert chunks and final
    if protocol == "messages":
        start = json.loads(initial[0].split("data: ", 1)[1])
        assert start["message"]["usage"] == {"input_tokens": None, "output_tokens": None}
        end = next(json.loads(event.split("data: ", 1)[1]) for event in final if "event: message_delta\n" in event)
        assert end["usage"] == usage
    elif protocol == "responses":
        end = json.loads(final[-1].split("data: ", 1)[1])
        assert end["response"]["usage"] == usage


@pytest.mark.parametrize("protocol,path", [("chat", "chat/completions"), ("responses", "responses"), ("messages", "messages")])
@pytest.mark.parametrize("stream", [False, True])
def test_public_http_completion_missing_usage_returns_success_and_keeps_quota_reservation(api, protocol, path, stream):  # noqa: F811
    client, adapter, key = api
    def fixture_chat(body, *, task_id, timeout_s, on_event=None):
        result = decode(completed_events())
        if on_event is not None:
            codex_session.Stream(on_event).finish(result)
        return result
    adapter.chat = fixture_chat
    body = {"model": "local/test", "stream": stream}
    if protocol == "responses":
        body.update(input="Authored fixture", max_output_tokens=30)
    else:
        body.update(messages=[{"role": "user", "content": "Authored fixture"}], max_tokens=30)
        if protocol == "chat" and stream:
            body["stream_options"] = {"include_usage": True}
    response = client.post("/v1/" + path, json=body)
    assert response.status_code == 200, response.text
    if not stream:
        usage = response.json()["usage"]
        keys = ("prompt_tokens", "completion_tokens") if protocol == "chat" else ("input_tokens", "output_tokens")
        assert all(usage[field] is None for field in keys)
    else:
        assert '"input_tokens": null' in response.text if protocol != "chat" else '"prompt_tokens": null' in response.text
    projects = client.get("/api/local/llm/api-access").json()["keys"]
    project = next(value for value in projects if value["id"] == key["id"])
    assert project["used_output_tokens"] == 30


def test_encrypted_provider_consumer_unknown_usage_keeps_result_quote_and_replay(tmp_path, monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from test_llm_package import _FakeAdapter

    from rynmesh.llm_package.manifest import LLMPackageManifest
    from rynmesh.llm_package.routes import CAPABILITY, ProviderService, install_llm_routes
    from rynmesh.llm_package.task_balance import TaskBalanceLedger
    from rynmesh.llm_package.task_protocol import TaskOrderStore
    from rynmesh.services import peer_box
    from rynmesh.store import RynmeshStore

    monkeypatch.setenv("RYNMESH_LLM_TRANSPORT", "direct")
    net = tmp_path / "net"
    provider = RynmeshStore(home=tmp_path / "provider", network_dir=net)
    consumer = RynmeshStore(home=tmp_path / "consumer", network_dir=net)
    pk = peer_box.load_or_create_messaging_key(provider.home / "fixture-messaging")
    ck = peer_box.load_or_create_messaging_key(consumer.home / "fixture-messaging")
    class UnknownAdapter(_FakeAdapter):
        def infer(self, **options):
            self.calls += 1
            return decode(completed_events())
    adapter = UnknownAdapter()
    provider_balance = TaskBalanceLedger(provider.home / "fixture-balance.json")
    service = ProviderService(manifest=LLMPackageManifest(package_id="svc", mode="openai_compatible",
        public_model_alias="fixture", base_url="http://127.0.0.1:1"), adapter=adapter, store=provider,
        task_store=TaskOrderStore(provider.home / "fixture-orders"), balance=provider_balance,
        messaging_key=pk, access_check=lambda *_: {})
    # Authored discovery and an injected authorization grant isolate billing;
    # the real service still validates the sealed sender, quote and task id.
    provider.register_node(capabilities=[CAPABILITY], network_id="usage-net")
    provider.register_job_capacity(capabilities=[CAPABILITY], network_id="usage-net", capacity_units=1,
        max_concurrent=1, metadata={"llm_service": {"online": True,
            "service": service.manifest.public_dict(), "node_messaging_pub": peer_box.public_key_b64(pk),
            "capacity": {"available": 1}, "delivery_protocols": ["complete-v1"]}})
    class FixturePeer:
        requests = 0
        settlements = 0
        def __init__(self, endpoint, *, timeout_s):
            pass
        def post_json(self, path, payload, *, max_bytes):
            if path == "/api/peer/llm/tasks":
                type(self).requests += 1
                return service.handle(payload)
            type(self).settlements += 1
            raise AssertionError("Unknown usage must not dispatch a settlement")
    monkeypatch.setattr("rynmesh.peer_http.HttpPeerClient", FixturePeer)
    def install():
        app = FastAPI()
        commands = install_llm_routes(app, store=consumer, home=consumer.home, messaging_key=ck,
            resolve_endpoint=lambda _: "http://127.0.0.1:9", resolve_pubkey=lambda _: peer_box.public_key_b64(pk))
        return app, commands
    app, _ = install()
    body = {"network_id": "usage-net", "provider_peer_id": provider.peer_id, "service_id": "svc",
            "prompt": "Authored fixture request", "max_tokens": 8, "transport": "direct", "task_id": "unknown-peer"}
    with TestClient(app) as client:
        assert client.put("/api/local/llm/privacy", json={"result_retention_seconds": 3600}).status_code == 200
        first = client.post("/api/local/llm/orders", json=body)
        assert first.status_code == 200, first.text
        result = first.json()
        assert result["state"] == "succeeded" and result["output"] == "Authored answer"
        assert result["input_tokens"] is result["output_tokens"] is result["amount"] is None
        assert result["usage_complete"] is False and result["billing_status"] == "pending_usage"
        before = client.get("/api/local/task-balance").json()
        repeated = client.post("/api/local/llm/orders", json=body)
        assert repeated.status_code == 200, repeated.text
        assert repeated.json()["amount"] is None
    assert adapter.calls == FixturePeer.requests == 1 and FixturePeer.settlements == 0
    balance = TaskBalanceLedger(consumer.home / "llm/task-balance.json", credit_ledger=consumer.credit_ledger,
        peer_id=consumer.peer_id, private_key_bytes=consumer.private_key_bytes)
    assert balance.summary()["held"] > 0
    events = balance.events()
    assert len([event for event in events if event["kind"] == "hold"]) == 1
    assert not any(event["kind"] in {"settle", "release", "earning"} for event in events)
    assert provider_balance.summary()["earned"] == 0 and provider_balance.events() == []
    held = copy.deepcopy(balance.summary())
    for _ in range(2):
        _, commands = install()  # Actual startup recovery, not a direct ledger settlement.
        status = commands.status("unknown-peer")
        assert status["state"] == "succeeded" and status["output"] == "Authored answer"
        assert status["amount"] is None and status["billing_pending_usage"] is True
        assert balance.summary() == held and FixturePeer.settlements == 0
    assert before["held"] == held["held"]


def test_free_own_request_has_explicit_price_basis_but_unknown_usage(tmp_path):
    from test_llm_package import _FakeAdapter

    from rynmesh.llm_package.manifest import LLMPackageManifest
    from rynmesh.llm_package.routes import ProviderService, _expires, _open_provider_response
    from rynmesh.llm_package.task_protocol import seal_task
    from rynmesh.services import peer_box

    node = RynmeshStore(home=tmp_path / "own", network_dir=tmp_path / "net")
    key = peer_box.load_or_create_messaging_key(node.home / "fixture-key")
    adapter = _FakeAdapter()
    adapter.infer = lambda **kwargs: decode(completed_events())
    balance = TaskBalanceLedger(node.home / "fixture-balance.json")
    service = ProviderService(manifest=LLMPackageManifest(package_id="svc", mode="openai_compatible",
        public_model_alias="own", base_url="http://127.0.0.1:1"), adapter=adapter, store=node,
        task_store=TaskOrderStore(node.home / "fixture-orders"), balance=balance, messaging_key=key)
    request = seal_task(body={"task_id": "own-free", "service_id": "svc", "prompt": "Authored own request", "max_tokens": 8,
        "max_amount": 0, "reply_messaging_pub": peer_box.public_key_b64(key)}, task_id="own-free",
        kind="llm_request", sender_peer_id=node.peer_id, recipient_peer_id=node.peer_id,
        sender_signing_key=node.private_key_bytes, recipient_messaging_pub=peer_box.public_key_b64(key),
        expires_at=_expires(300)).to_dict()
    _, result = _open_provider_response(service.handle(request), recipient_peer_id=node.peer_id,
        messaging_key=key, task_id="own-free", provider_peer_id=node.peer_id, service_id="svc")
    assert result["state"] == "succeeded" and result["amount"] == 0
    assert result["billing_basis"] == result["billing_status"] == "own_request"
    assert result["input_tokens"] is result["output_tokens"] is None
    assert result["usage_complete"] is False and balance.events() == []


def test_signed_success_omitting_usage_fields_projects_unknown_not_keyerror(tmp_path):
    from rynmesh.llm_package.routes import _expires, _open_provider_response
    from rynmesh.llm_package.task_protocol import seal_task
    from rynmesh.services import peer_box

    provider = RynmeshStore(home=tmp_path / "provider", network_dir=tmp_path / "net")
    consumer = RynmeshStore(home=tmp_path / "consumer", network_dir=tmp_path / "net")
    key = peer_box.load_or_create_messaging_key(consumer.home / "fixture-key")
    signed = seal_task(body={"task_id": "missing-fields", "service_id": "svc", "state": "succeeded",
        "output": "Authored response", "duration_ms": 1, "amount": None}, task_id="missing-fields", kind="llm_response",
        sender_peer_id=provider.peer_id, recipient_peer_id=consumer.peer_id, sender_signing_key=provider.private_key_bytes,
        recipient_messaging_pub=peer_box.public_key_b64(key), expires_at=_expires(300)).to_dict()
    _, result = _open_provider_response(signed, recipient_peer_id=consumer.peer_id, messaging_key=key,
        task_id="missing-fields", provider_peer_id=provider.peer_id, service_id="svc")
    assert result["input_tokens"] is result["output_tokens"] is None


def _usage_balance(tmp_path, ledger_backed):
    # Same identity binding as test_task_balance_ledger._ledger; temporary only.
    node = (RynmeshStore(home=tmp_path / "node", network_dir=tmp_path / "network")
            if ledger_backed else None)
    path = tmp_path / "balance.json"

    def reopen(initial=10):
        kwargs = ({"credit_ledger": node.credit_ledger, "peer_id": node.peer_id,
                   "private_key_bytes": node.private_key_bytes} if node else {})
        return TaskBalanceLedger(path, initial_dev_balance=initial, **kwargs)

    return reopen(), reopen, path, node


def _unknown_usage(value):
    assert value["input_tokens"] is None
    assert value["output_tokens"] is None
    assert value["usage_complete"] is False


@pytest.mark.parametrize("ledger_backed", [False, True], ids=["standalone", "signed"])
def test_explicit_own_free_unknown_usage_survives_reopen_rebuild_and_dedup(tmp_path, ledger_backed):
    balance, reopen, path, node = _usage_balance(tmp_path, ledger_backed)
    # This fixture is explicitly own/free. A zero price is NOT zero token usage.
    task_id = "own-free-usage-unreported"
    hold = dict(task_id=task_id, amount=0, service_id="own-free-service",
                provider_peer_id="own-node", idempotency_key=task_id,
                request_fingerprint="own-free-fixture")
    settled = dict(task_id=task_id, amount=0, input_tokens=None, output_tokens=None,
                   duration_ms=7, service_id="own-free-service", provider_peer_id="own-node")
    earned = dict(task_id=task_id, amount=0, input_tokens=None, output_tokens=None,
                  duration_ms=7, service_id="own-free-service", consumer_peer_id="own-node")
    balance.hold(**hold)
    first_settle, first_earn = balance.settle(**settled), balance.earn(**earned)
    _unknown_usage(first_settle)
    _unknown_usage(first_earn)
    assert first_settle["settled_amount"] == first_earn["amount"] == 0
    assert balance.settle(**settled) == first_settle
    assert balance.earn(**earned) == first_earn

    def assert_persisted(view):
        _unknown_usage(view.settle(**settled))  # Existing operation, not a new charge.
        _unknown_usage(view.earn(**earned))
        events = view.events()
        for kind in ("settle", "earning"):
            matching = [item for item in events if item["kind"] == kind and item["task_id"] == task_id]
            assert len(matching) == 1
            _unknown_usage(matching[0])
        assert view.summary()["available"] == 10
        assert view.summary()["held"] == view.summary()["earned"] == 0
        persisted = json.loads(path.read_text(encoding="utf-8"))
        _unknown_usage(persisted["holds"][task_id])
        _unknown_usage(persisted["earnings"]["earning:" + task_id])

    assert_persisted(reopen())
    if ledger_backed:
        # Lost materialized snapshot forces reconstruction from signed events.
        path.unlink()
        reconstructed = reopen(initial=999)
        assert_persisted(reconstructed)
        reconstructed.rebuild()  # Explicit replay also retains null, not zero.
        assert_persisted(reconstructed)
        signed = node.credit_ledger.list_events(
            subject_peer_id=node.peer_id, category=TASK_BALANCE_CATEGORY)
        assert sorted(item.payload["kind"] for item in signed) == [
            "task_balance_opening", "task_earning", "task_hold", "task_settle"]
        for item in signed:
            if item.payload["kind"] in {"task_settle", "task_earning"}:
                assert item.payload["metadata"]["input_tokens"] is None
                assert item.payload["metadata"]["output_tokens"] is None


@pytest.mark.parametrize("initial_state", ["running", "succeeded"])
@pytest.mark.parametrize("ledger_backed", [False, True], ids=["standalone", "signed"])
@pytest.mark.parametrize("marker_present", [False, True], ids=["legacy-marker", "explicit-marker"])
def test_pending_usage_recovery_keeps_original_hold_and_never_settles_or_dispatches(
        tmp_path, monkeypatch, initial_state, ledger_backed, marker_present):
    balance, reopen, path, node = _usage_balance(tmp_path, ledger_backed)
    orders = TaskOrderStore(tmp_path / "orders")
    task_id = "successful-result-pending-usage"
    bindings = {"provider_peer_id": "provider-peer", "service_id": "private-service",
                "idempotency_key": task_id, "request_fingerprint": "pending-usage-fixture"}
    hold_args = dict(task_id=task_id, amount=0.25, **bindings)
    original_hold = balance.hold(**hold_args)
    orders.claim(task_id=task_id, bindings=bindings)
    orders.transition(task_id=task_id, state="accepted")
    orders.transition(task_id=task_id, state="running")
    pending = {"billing_pending_usage": True, "settlement_pending": False,
               "provider_peer_id": "provider-peer", "service_id": "private-service",
               "network_id": "rynmesh-main", "amount": None,
               "input_tokens": None, "output_tokens": None, "usage_complete": False,
               "duration_ms": 7, "billing_status": "pending_usage", "billing_basis": "pending_usage"}
    if not marker_present:
        pending.pop("billing_pending_usage")
        pending["settlement_pending"] = True
        pending["amount"] = 0.1  # A legacy quote is not measured billing evidence.
    orders.checkpoint(task_id=task_id, metadata=pending)
    if initial_state == "succeeded":
        orders.transition(task_id=task_id, state="succeeded", metadata=pending)

    # Reopen the actual stores first: recovery must use their durable checkpoint.
    orders = TaskOrderStore(tmp_path / "orders")
    balance = reopen()
    before_summary, before_events = balance.summary(), balance.events()
    before_orders = orders.get(task_id)
    signed_count = (len(node.credit_ledger.list_events(
        subject_peer_id=node.peer_id, category=TASK_BALANCE_CATEGORY)) if node else None)

    def forbidden(*args, **kwargs):
        pytest.fail("pending_usage recovery must not settle, release, earn, or dispatch")

    monkeypatch.setattr(balance, "settle", forbidden)
    monkeypatch.setattr(balance, "release", forbidden)
    monkeypatch.setattr(balance, "earn", forbidden)
    monkeypatch.setattr(routes, "dispatch_settlement", forbidden)
    # A non-None store makes an accidental settlement-dispatch path observable.
    routes._recover_consumer_orders(orders, balance, store=node if node else object())
    once = orders.get(task_id)
    routes._recover_consumer_orders(orders, balance, store=node if node else object())
    twice = orders.get(task_id)

    assert once == twice  # No repeated transition/history entry on second recovery.
    assert twice["state"] == "succeeded"  # Successful inference is not downgraded.
    assert len(twice["history"]) == len(before_orders["history"]) + (initial_state == "running" or not marker_present)
    terminal = next(item for item in reversed(twice["history"]) if item["state"] == "succeeded")
    _unknown_usage(terminal)
    assert terminal["amount"] is None and terminal["billing_pending_usage"] is True
    assert terminal["billing_status"] == "pending_usage"
    assert not any(item.get("settlement_dispatched") for item in twice["history"])
    assert balance.summary() == before_summary
    assert balance.events() == before_events
    assert balance.hold(**hold_args) == original_hold  # Original hold/binding unchanged.
    assert balance.summary()["held"] == 0.25
    assert balance.summary()["available"] == 9.75
    assert not any(item["kind"] in {"settle", "release", "earning"} for item in balance.events())
    snapshot = json.loads(path.read_text(encoding="utf-8"))
    assert snapshot["holds"][task_id] == original_hold
    if node:
        assert len(node.credit_ledger.list_events(
            subject_peer_id=node.peer_id, category=TASK_BALANCE_CATEGORY)) == signed_count
