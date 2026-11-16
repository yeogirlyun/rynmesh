from __future__ import annotations

import asyncio
import copy
import json
import threading
from types import SimpleNamespace

import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from rynmesh.llm_package.api import InferenceKeys, install_inference_api
from rynmesh.llm_package.manifest import LLMPackageManifest
from rynmesh.llm_package.routes import ProviderService, _expires, _open_provider_response
from rynmesh.llm_package.streaming import events
from rynmesh.llm_package.task_balance import TaskBalanceLedger
from rynmesh.llm_package.task_protocol import (
    TaskOrderStore,
    TaskProtocolError,
    open_task,
    seal_task,
)
from rynmesh.services import peer_box
from rynmesh.store import RynmeshStore


class ChatAdapter:
    calls = 0

    def health(self):
        return {"ok": True}

    def chat(self, body, *, task_id, timeout_s, on_event=None):
        self.calls += 1
        self.last_body = copy.deepcopy(body)
        message = {"role": "assistant", "content": "Hello world"}
        finish = "stop"
        deltas = [{"content": "Hello "}, {"content": "world"}]
        if body.get("tools") and body["messages"][-1]["role"] != "tool":
            function = {"name": "weather", "arguments": '{"city":"Shanghai"}'}
            message = {"role": "assistant", "content": None, "tool_calls": [{"id": "call_1", "type": "function", "function": function}]}
            finish = "tool_calls"
            deltas = [{"tool_calls": [{"index": 0, "id": "call_1", "type": "function", "function": {"name": "weather", "arguments": '{"city":'}}]},
                      {"tool_calls": [{"index": 0, "function": {"arguments": '"Shanghai"}'}}]}]
        if body.get("enable_thinking"):
            message["reasoning_content"] = "Test reasoning"
            deltas.insert(0, {"reasoning_content": "Test reasoning"})
        if body.get("stream") and on_event:
            for delta in deltas:
                on_event({"choices": [{"index": 0, "delta": delta, "finish_reason": None}]})
            on_event({"choices": [{"index": 0, "delta": {}, "finish_reason": finish}]})
            on_event({"choices": [], "usage": {"prompt_tokens": 7, "completion_tokens": 3, "total_tokens": 10}})
        return {"text": message["content"] or "", "message": message, "finish_reason": finish,
                "input_tokens": 7, "output_tokens": 3, "duration_ms": 4}

    def cancel(self, task_id):
        return True


@pytest.fixture
def api(tmp_path):
    adapter = ChatAdapter()
    manager = SimpleNamespace(adapter=adapter, manifest=LLMPackageManifest(package_id="test", mode="openai_compatible", public_model_alias="test-model", base_url="http://127.0.0.1:1"),
                              _slots=threading.BoundedSemaphore(1), _lock=threading.Lock(), _running=0)
    app = FastAPI()
    install_inference_api(app, home=tmp_path, store=SimpleNamespace(peer_id="self"), active_manager=lambda: manager,
                          discover=lambda _: [], execute_order=None, cancel_order=None)
    client = TestClient(app)
    key = client.post("/api/local/llm/api-keys", json={"name": "agent", "output_token_limit": 1000}).json()
    client.headers["Authorization"] = "Bearer " + key["key"]
    return client, adapter, key


def test_key_auth_loopback_revocation_and_quota(api, tmp_path):
    client, adapter, key = api
    assert client.get("/v1/models").json()["data"][0]["id"] == "local/test"
    assert client.get("/v1/models", headers={"Authorization": "Bearer invalid"}).status_code == 401
    assert client.get("/v1/models", headers={"X-Forwarded-For": "127.0.0.1"}).status_code == 403
    assert client.get("/v1/models", headers={"Host": "attacker.example"}).status_code == 403
    assert client.get("/v1/models", headers={"Origin": "https://attacker.example"}).status_code == 403
    assert key["key"] not in (tmp_path / "llm" / "inference-keys.sqlite3").read_bytes().decode(errors="ignore")
    assert InferenceKeys(tmp_path).authenticate(key["key"]) == key["id"]
    small = client.post("/api/local/llm/api-keys", json={"name": "small", "output_token_limit": 4}).json()
    payload = {"model": "local/test", "messages": [{"role": "user", "content": "hi"}], "max_tokens": 4}
    assert client.post("/v1/chat/completions", json=payload, headers={"Authorization": "Bearer " + small["key"]}).status_code == 200
    assert client.post("/v1/chat/completions", json=payload, headers={"Authorization": "Bearer " + small["key"]}).status_code == 429
    client.delete("/api/local/llm/api-keys/" + key["id"])
    assert client.get("/v1/models").status_code == 401
    assert adapter.calls == 1


@pytest.mark.parametrize("protocol,path", [("chat", "chat/completions"), ("responses", "responses"), ("messages", "messages")])
@pytest.mark.parametrize("stream", [False, True])
def test_three_protocols_tool_call_round_trip(api, protocol, path, stream):
    client, adapter, _ = api
    assert client.put("/api/local/llm/model-aliases/qwen", json={"target": "local/test"}).status_code == 200
    body = {"model": "qwen", "stream": stream}
    if protocol == "responses":
        body.update(input="weather?", max_output_tokens=30, tools=[{"type": "function", "name": "weather", "parameters": {"type": "object"}}])
    elif protocol == "messages":
        body.update(messages=[{"role": "user", "content": "weather?"}], max_tokens=30, tools=[{"name": "weather", "input_schema": {"type": "object"}}])
    else:
        body.update(messages=[{"role": "user", "content": "weather?"}], max_tokens=30, tools=[{"type": "function", "function": {"name": "weather", "parameters": {"type": "object"}}}])
    response = client.post("/v1/" + path, json=body)
    assert response.status_code == 200, response.text
    if stream:
        assert "text/event-stream" in response.headers["content-type"]
        if protocol == "chat":
            assert '"arguments": "{\\"city\\":"' in response.text
            assert response.text.endswith("data: [DONE]\n\n")
        else:
            expected = "response.function_call_arguments.delta" if protocol == "responses" else "input_json_delta"
            assert expected in response.text
            assert "response.completed" in response.text if protocol == "responses" else "message_stop" in response.text
        body["stream"] = False
        response = client.post("/v1/" + path, json=body)
    result = response.json()
    assert result["model"] == "qwen"
    if protocol == "responses":
        call = result["output"][0]
        body["input"] = [{"role": "user", "content": "weather?"}, call, {"type": "function_call_output", "call_id": call["call_id"], "output": "Sunny"}]
    elif protocol == "messages":
        call = result["content"][0]
        body["messages"] += [{"role": "assistant", "content": result["content"]}, {"role": "user", "content": [{"type": "tool_result", "tool_use_id": call["id"], "content": "Sunny"}]}]
    else:
        message = result["choices"][0]["message"]
        body["messages"] += [message, {"role": "tool", "tool_call_id": message["tool_calls"][0]["id"], "content": "Sunny"}]
    response = client.post("/v1/" + path, json=body)
    assert response.status_code == 200, response.text
    assert "Hello world" in response.text
    assert adapter.last_body["messages"][-1] == {"role": "tool", "tool_call_id": "call_1", "content": "Sunny"}


def test_unsupported_features_fail_before_inference(api):
    client, adapter, _ = api
    for values in ({"previous_response_id": "resp_old"}, {"store": True}, {"tools": [{"type": "web_search"}]}):
        response = client.post("/v1/responses", json={"model": "local/test", "input": "hi", **values})
        assert response.status_code == 400
    assert adapter.calls == 0


@pytest.mark.parametrize("enabled", [True, False])
@pytest.mark.parametrize("nested", [True, False])
@pytest.mark.parametrize("stream", [True, False])
def test_qwen_thinking_option(api, enabled, nested, stream):
    client, adapter, _ = api
    flag = {"enable_thinking": enabled}
    body = {"model": "local/test", "messages": [{"role": "user", "content": "hi"}], "max_tokens": 64, "stream": stream}
    body.update({"chat_template_kwargs": flag} if nested else flag)
    response = client.post("/v1/chat/completions", json=body)
    assert response.status_code == 200, response.text
    assert adapter.last_body["enable_thinking"] is enabled
    assert "chat_template_kwargs" not in adapter.last_body
    assert ("reasoning_content" in response.text) is enabled
    if stream:
        assert response.text.endswith("data: [DONE]\n\n")
    else:
        assert response.json()["choices"][0]["message"]["content"] == "Hello world"


def test_invalid_thinking_options_fail_before_inference(api):
    client, adapter, _ = api
    for options in [{"enable_thinking": "false"}, {"enable_thinking": 1},
                    {"enable_thinking": False, "chat_template_kwargs": {"enable_thinking": True}},
                    {"chat_template_kwargs": {"unknown": True}}, {"chat_template_kwargs": []}]:
        response = client.post("/v1/chat/completions", json={"model": "local/test", "messages": [{"role": "user", "content": "hi"}], **options})
        assert response.status_code == 400
    assert adapter.calls == 0


@pytest.mark.parametrize("protocol,path", [("chat", "chat/completions"), ("responses", "responses"), ("messages", "messages")])
def test_large_client_output_budget_is_capped_before_quota_reservation(api, protocol, path):
    client, adapter, key = api
    body = {"model": "local/test"}
    if protocol == "responses":
        body.update(input="hi", max_output_tokens=128000)
    else:
        body.update(messages=[{"role": "user", "content": "hi"}], max_tokens=128000)
    response = client.post("/v1/" + path, json=body)
    assert response.status_code == 200, response.text
    assert adapter.last_body["max_tokens"] == 512
    # The project's 1000-token quota covers the effective 512-token reservation.
    access = client.get("/api/local/llm/api-access").json()
    project = next(k for k in access["keys"] if k["id"] == key["id"])
    assert project["used_output_tokens"] == 3


def test_output_budget_fits_remaining_context_without_truncating_prompt(api):
    client, adapter, _ = api
    text = "x" * 15000
    body = {"model": "local/test", "messages": [{"role": "user", "content": text}], "max_tokens": 128000}
    response = client.post("/v1/chat/completions", json=body)
    assert response.status_code == 200, response.text
    assert 0 < adapter.last_body["max_tokens"] < 512
    assert (len(json.dumps(adapter.last_body, ensure_ascii=False)) + 3) // 4 + adapter.last_body["max_tokens"] <= 4096
    assert adapter.last_body["messages"][0]["content"] == text
    body["messages"][0]["content"] = "x" * 17000
    assert client.post("/v1/chat/completions", json=body).status_code == 400
    assert adapter.calls == 1


def test_alias_validation_and_persistence(api, tmp_path):
    client, _, _ = api
    for name, target in [("bad name", "local/test"), ("x" * 65, "local/test"), ("qwen", "missing")]:
        assert client.put("/api/local/llm/model-aliases/" + name, json={"target": target}).status_code == 400
    assert client.put("/api/local/llm/model-aliases/qwen", json={"target": "local/test"}).status_code == 200
    assert InferenceKeys(tmp_path).aliases() == {"qwen": "local/test"}
    assert [m["id"] for m in client.get("/v1/models").json()["data"]] == ["qwen"]
    access = client.get("/api/local/llm/api-access").json()
    assert access["aliases"] == {"qwen": "local/test"}
    assert access["targets"][0]["id"] == "local/test"
    # Previously configured clients still accept the canonical ID.
    assert client.post("/v1/chat/completions", json={"model": "local/test", "messages": [{"role": "user", "content": "hi"}], "max_tokens": 8}).status_code == 200


def test_alias_pins_remote_provider_when_discovery_changes(tmp_path):
    providers = [{"peer_id": name, "online": True, "chat_protocol": "rynmesh.chat.v1",
                  "service": {"package_id": "qwen", "model_alias": "Qwen", "max_output_tokens": 64, "context_window": 4096}}
                 for name in ["provider-a", "provider-b"]]
    calls = []

    async def execute(order, emit):
        calls.append(order)
        return {"state": "succeeded", **ChatAdapter().chat(order["chat"], task_id=order["task_id"], timeout_s=10)}

    def client_for_node():
        app = FastAPI()
        install_inference_api(app, home=tmp_path, store=SimpleNamespace(peer_id="self"), active_manager=lambda: None,
                              discover=lambda _: providers, execute_order=execute, cancel_order=lambda _: None)
        return TestClient(app)

    client = client_for_node()
    key = client.post("/api/local/llm/api-keys", json={"name": "agent"}).json()["key"]
    target = client.get("/api/local/llm/api-access").json()["targets"][0]["id"]
    assert client.put("/api/local/llm/model-aliases/qwen", json={"target": target}).status_code == 200
    client = client_for_node()  # A restarted node reads the same saved mapping and key.
    client.headers["Authorization"] = "Bearer " + key
    payload = {"model": "qwen", "messages": [{"role": "user", "content": "hi"}], "max_tokens": 16}
    providers.reverse()
    result = client.post("/v1/chat/completions", json=payload)
    assert result.status_code == 200 and result.json()["model"] == "qwen"
    assert calls[0]["provider_peer_id"] == "provider-a"
    assert calls[0]["transport"] == "p2p"
    providers.pop()
    assert client.post("/v1/chat/completions", json=payload).status_code == 503
    assert len(calls) == 1
    assert "qwen" not in [m["id"] for m in client.get("/v1/models").json()["data"]]
    # Switching a provider is an explicit local configuration change.
    target = client.get("/api/local/llm/api-access").json()["targets"][0]["id"]
    assert client.put("/api/local/llm/model-aliases/qwen", json={"target": target}).status_code == 200
    assert client.post("/v1/chat/completions", json=payload).status_code == 200
    assert calls[-1]["provider_peer_id"] == "provider-b"


def test_provider_keeps_full_messages_and_seals_each_delta(tmp_path):
    provider = RynmeshStore(home=tmp_path / "provider", network_dir=tmp_path / "net")
    consumer = RynmeshStore(home=tmp_path / "consumer", network_dir=tmp_path / "net")
    pk = peer_box.load_or_create_messaging_key(tmp_path / "pk")
    ck = peer_box.load_or_create_messaging_key(tmp_path / "ck")
    adapter = ChatAdapter()
    def authorize(peer_id, service_id, permission):
        assert peer_id == consumer.peer_id and service_id == "svc"

    service = ProviderService(manifest=LLMPackageManifest(package_id="svc", mode="openai_compatible", public_model_alias="alias", base_url="http://127.0.0.1:1"),
                              adapter=adapter, store=provider, task_store=TaskOrderStore(tmp_path / "orders"), balance=TaskBalanceLedger(tmp_path / "balance.json"), messaging_key=pk, access_check=authorize)
    chat = {"messages": [{"role": "system", "content": "SECRET SYSTEM"}, {"role": "user", "content": "SECRET USER"}], "stream": True, "max_tokens": 20, "enable_thinking": True}
    request = seal_task(body={"task_id": "test", "service_id": "svc", "chat": chat, "max_tokens": 20, "max_amount": 1, "reply_messaging_pub": peer_box.public_key_b64(ck)},
                        task_id="test", kind="llm_request", sender_peer_id=consumer.peer_id, recipient_peer_id=provider.peer_id, sender_signing_key=consumer.private_key_bytes, recipient_messaging_pub=peer_box.public_key_b64(pk), expires_at=_expires(300)).to_dict()
    deltas = []
    result = service.handle(request, deltas.append)
    for index, envelope in enumerate(deltas):
        assert "Hello" not in json.dumps(envelope)
        _, opened = open_task(envelope, recipient_peer_id=consumer.peer_id, recipient_messaging_key=ck, expected_kind="llm_stream")
        assert opened["sequence"] == index
    _, opened = _open_provider_response(result, recipient_peer_id=consumer.peer_id, messaging_key=ck, task_id="test", provider_peer_id=provider.peer_id, service_id="svc")
    assert opened["message"]["content"] == "Hello world"
    assert adapter.last_body["messages"] == chat["messages"]
    assert adapter.last_body["enable_thinking"] is True
    assert opened["message"]["reasoning_content"] == "Test reasoning"
    with pytest.raises(TaskProtocolError, match="task mismatch"):
        _open_provider_response(result, recipient_peer_id=consumer.peer_id, messaging_key=ck, task_id="other", provider_peer_id=provider.peer_id, service_id="svc")
    disk = "".join(path.read_text() for path in tmp_path.rglob("*.json"))
    assert "SECRET" not in disk and "Hello world" not in disk


def test_stream_bridge_delivers_before_completion_and_cancels():
    async def scenario():
        emitted = threading.Event()
        cancelled = threading.Event()

        def worker(emit):
            emit({"content": "early"})
            emitted.set()
            assert cancelled.wait(3)
            return {"done": True}

        async def run(emit):
            return await asyncio.to_thread(worker, emit)

        stream = events(run, cancelled.set)
        kind, value = await anext(stream)
        assert kind == "event" and value["content"] == "early"
        await stream.aclose()
        assert await asyncio.to_thread(cancelled.wait, 2)
    asyncio.run(scenario())


def test_inference_key_cannot_manage_node(tmp_path, monkeypatch):
    monkeypatch.setenv("RYNMESH_LOCAL_TOKEN", "desktop-secret")
    monkeypatch.delenv("RYNMESH_LLM_SERVICE_MANIFEST", raising=False)
    from rynmesh.peer_http import create_app
    app = create_app(RynmeshStore(home=tmp_path / "node", network_dir=tmp_path / "net"))
    with TestClient(app) as client:
        denied = client.post("/api/local/llm/api-keys", json={"name": "agent"})
        assert denied.status_code == 403
        key = client.post("/api/local/llm/api-keys", json={"name": "agent"}, headers={"x-ryn-local-token": "desktop-secret"}).json()["key"]
        assert client.get("/v1/models", headers={"Authorization": "Bearer " + key}).status_code == 200
        assert client.get("/api/local/node/status", headers={"Authorization": "Bearer " + key}).status_code == 403
        assert client.put("/api/local/llm/model-aliases/qwen", json={"target": "local/test"}, headers={"Authorization": "Bearer " + key}).status_code == 403


def test_p2p_transmits_incremental_events_before_final(monkeypatch):
    from rynmesh.llm_package.p2p import consumer_exchange, provider_exchange

    monkeypatch.setenv("RYNMESH_P2P_REQUIRE_PUBLIC", "0")
    monkeypatch.setenv("RYNMESH_P2P_REQUIRE_DISTINCT_PUBLIC", "0")
    monkeypatch.setenv("RYNMESH_P2P_STUN", "")

    async def scenario():
        loop = asyncio.get_running_loop()
        answer = loop.create_future()
        jobs = []
        seen = []

        def handler(request, emit):
            assert request == {"request": True}
            for i in range(3):
                emit({"payload": {"kind": "llm_stream"}, "sequence": i})
            return {"payload": {"kind": "llm_response"}, "done": True}

        async def offer(signal):
            jobs.append(asyncio.create_task(provider_exchange(offer=signal, publish_answer=answer.set_result,
                                                             handle_request=None, handle_stream_request=handler, timeout_s=10)))
            return await answer

        result, _ = await consumer_exchange(signed_request={"request": True}, publish_offer=offer,
                                            timeout_s=10, on_event=lambda event: seen.append(event["sequence"]))
        assert result["done"] and seen == [0, 1, 2]
        await asyncio.gather(*jobs)
    asyncio.run(scenario())


def test_order_reads_share_writer_lock(tmp_path):
    from concurrent.futures import ThreadPoolExecutor

    orders = TaskOrderStore(tmp_path / "orders")
    orders.claim(task_id="concurrent", bindings={"consumer_peer_id": "p"})

    def update():
        for i in range(100):
            orders.checkpoint(task_id="concurrent", metadata={"duration_ms": i})

    def read():
        for _ in range(100):
            assert orders.get("concurrent")["task_id"] == "concurrent"

    with ThreadPoolExecutor(max_workers=4) as pool:
        jobs = [pool.submit(update), pool.submit(read), pool.submit(read), pool.submit(read)]
        for job in jobs:
            job.result()


def test_responses_stream_keeps_tool_first_output_indexes():
    from rynmesh.llm_package.api_formats import StreamFormat
    from rynmesh.llm_package.chat import completion

    formatter = StreamFormat("responses", "local/test", "r1")
    formatter.delta({"choices": [{"delta": {"tool_calls": [{"index": 0, "id": "c1", "function": {"name": "weather", "arguments": "{}"}}]}}]})
    formatter.delta({"choices": [{"delta": {"content": "Checking"}}]})
    final = completion({"input_tokens": 1, "output_tokens": 2, "message": {"role": "assistant", "content": "Checking", "tool_calls": [{"id": "c1", "function": {"name": "weather", "arguments": "{}"}}]}, "finish_reason": "tool_calls"}, "local/test", "r1")
    last = formatter.finish(final)[-1]
    payload = json.loads(last.split("data: ", 1)[1])
    assert [item["type"] for item in payload["response"]["output"]] == ["function_call", "message"]


# ---------------------------------------------------------------------------
# S01-I34 tests (T1 - T12)
# ---------------------------------------------------------------------------

_TINY_PNG_B64 = (
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="
)
_TINY_PNG_DATA_URL = "data:image/png;base64," + _TINY_PNG_B64


def _make_api_with_manifest(tmp_path, *, max_output_tokens=2048, context_window=8192, adapter_kind="openai_compatible"):
    adapter = ChatAdapter()
    manifest = LLMPackageManifest(
        package_id="test",
        mode=adapter_kind if adapter_kind in {"openai_compatible", "codex_cli", "claude_cli", "antigravity_cli"} else "openai_compatible",
        adapter=adapter_kind,
        runtime="external" if adapter_kind.endswith("_cli") else "local",
        public_model_alias="test-model",
        base_url="" if adapter_kind.endswith("_cli") else "http://127.0.0.1:1",
        max_output_tokens=max_output_tokens,
        context_window=context_window,
    )
    manager = SimpleNamespace(
        adapter=adapter,
        manifest=manifest,
        context_window_for_model=lambda *_args, **_kw: context_window,
        _slots=threading.BoundedSemaphore(2),
        _lock=threading.Lock(),
        _running=0,
    )
    app = FastAPI()
    install_inference_api(
        app,
        home=tmp_path,
        store=SimpleNamespace(peer_id="self"),
        active_manager=lambda: manager,
        discover=lambda _: [],
        execute_order=None,
        cancel_order=None,
    )
    client = TestClient(app)
    key = client.post("/api/local/llm/api-keys", json={"name": "agent", "output_token_limit": 100000}).json()
    client.headers["Authorization"] = "Bearer " + key["key"]
    return client, adapter, manager


def test_s01_i34_t1_default_max_tokens_uses_model_limit_and_messages_requires_max_tokens(tmp_path):
    client, adapter, _ = _make_api_with_manifest(tmp_path, max_output_tokens=2048, context_window=8192)

    resp_chat = client.post(
        "/v1/chat/completions",
        json={"model": "local/test", "messages": [{"role": "user", "content": "hello"}]},
    )
    assert resp_chat.status_code == 200, resp_chat.text
    assert adapter.last_body["max_tokens"] == 2048

    resp_responses = client.post(
        "/v1/responses",
        json={"model": "local/test", "input": "hello"},
    )
    assert resp_responses.status_code == 200, resp_responses.text
    assert adapter.last_body["max_tokens"] == 2048

    resp_msg_missing = client.post(
        "/v1/messages",
        json={"model": "local/test", "messages": [{"role": "user", "content": "hello"}]},
    )
    assert resp_msg_missing.status_code == 400
    assert resp_msg_missing.json()["error"]["message"] == "max_tokens is required"


def test_s01_i34_t2_ignorable_parameters_accepted_and_listed_in_header_unknown_still_400(api):
    client, adapter, _ = api
    resp = client.post(
        "/v1/chat/completions",
        json={
            "model": "local/test",
            "messages": [{"role": "user", "content": "hi"}],
            "max_tokens": 16,
            "metadata": {"trace_id": "abc"},
            "user": "sdk-user-1",
            "top_k": 40,
            "service_tier": "auto",
            "text": {"format": {"type": "text"}},
            "truncation": "auto",
        },
    )
    assert resp.status_code == 200, resp.text
    header = resp.headers.get("X-Ryn-Ignored-Parameters", "")
    ignored = {part.strip() for part in header.split(",") if part.strip()}
    assert ignored == {"metadata", "service_tier", "text", "top_k", "truncation", "user"}
    for k in ignored:
        assert k not in adapter.last_body

    # Messages ignores prompt_cache_key and reports it in header (stream too)
    resp_msg = client.post(
        "/v1/messages",
        json={
            "model": "local/test",
            "messages": [{"role": "user", "content": "hi"}],
            "max_tokens": 16,
            "stream": True,
            "metadata": {"user_id": "u1"},
            "top_k": 10,
            "prompt_cache_key": "sdk_cache_1",
        },
    )
    assert resp_msg.status_code == 200
    msg_ignored = {p.strip() for p in resp_msg.headers.get("X-Ryn-Ignored-Parameters", "").split(",") if p.strip()}
    assert msg_ignored == {"metadata", "prompt_cache_key", "top_k"}

    # Unknown parameter still 400
    bad = client.post(
        "/v1/chat/completions",
        json={"model": "local/test", "messages": [{"role": "user", "content": "hi"}], "completely_unknown": 1},
    )
    assert bad.status_code == 400
    assert "unsupported parameters" in bad.json()["error"]["message"]


def test_s01_i34_t3_responses_reasoning_effort_and_messages_thinking_mapped(api):
    client, adapter, _ = api
    resp = client.post(
        "/v1/responses",
        json={
            "model": "local/test",
            "input": "solve this",
            "max_output_tokens": 32,
            "reasoning": {"effort": "high"},
        },
    )
    assert resp.status_code == 200, resp.text
    assert adapter.last_body["reasoning_effort"] == "high"

    msg_on = client.post(
        "/v1/messages",
        json={
            "model": "local/test",
            "messages": [{"role": "user", "content": "think"}],
            "max_tokens": 32,
            "thinking": {"type": "enabled", "budget_tokens": 1024},
        },
    )
    assert msg_on.status_code == 200, msg_on.text
    assert adapter.last_body["enable_thinking"] is True

    msg_off = client.post(
        "/v1/messages",
        json={
            "model": "local/test",
            "messages": [{"role": "user", "content": "no think"}],
            "max_tokens": 32,
            "thinking": {"type": "disabled"},
        },
    )
    assert msg_off.status_code == 200, msg_off.text
    assert adapter.last_body["enable_thinking"] is False


def test_s01_i34_t4_disable_parallel_tool_use_and_parallel_tool_calls_accepted(api):
    client, adapter, _ = api
    msg_resp = client.post(
        "/v1/messages",
        json={
            "model": "local/test",
            "messages": [{"role": "user", "content": "weather?"}],
            "max_tokens": 32,
            "tools": [{"name": "weather", "input_schema": {"type": "object"}}],
            "tool_choice": {"type": "auto", "disable_parallel_tool_use": True},
        },
    )
    assert msg_resp.status_code == 200, msg_resp.text
    assert adapter.last_body["parallel_tool_calls"] is False

    chat_resp = client.post(
        "/v1/chat/completions",
        json={
            "model": "local/test",
            "messages": [{"role": "user", "content": "weather?"}],
            "max_tokens": 32,
            "tools": [{"type": "function", "function": {"name": "weather", "parameters": {"type": "object"}}}],
            "parallel_tool_calls": False,
        },
    )
    assert chat_resp.status_code == 200, chat_resp.text
    assert adapter.last_body["parallel_tool_calls"] is False


def test_s01_i34_t5_three_protocols_user_image_blocks_normalized_to_image_url(api):
    client, adapter, _ = api

    # 1. Chat image_url
    r_chat = client.post(
        "/v1/chat/completions",
        json={
            "model": "local/test",
            "max_tokens": 16,
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": "what is this?"},
                        {"type": "image_url", "image_url": {"url": _TINY_PNG_DATA_URL}},
                    ],
                }
            ],
        },
    )
    assert r_chat.status_code == 200, r_chat.text
    assert adapter.last_body["messages"][0]["content"][1] == {
        "type": "image_url",
        "image_url": {"url": _TINY_PNG_DATA_URL},
    }

    # 2. Responses input_image (image_url string)
    r_resp = client.post(
        "/v1/responses",
        json={
            "model": "local/test",
            "max_output_tokens": 16,
            "input": [
                {
                    "role": "user",
                    "content": [
                        {"type": "input_text", "text": "describe"},
                        {"type": "input_image", "image_url": _TINY_PNG_DATA_URL},
                    ],
                }
            ],
        },
    )
    assert r_resp.status_code == 200, r_resp.text
    assert adapter.last_body["messages"][0]["content"][1] == {
        "type": "image_url",
        "image_url": {"url": _TINY_PNG_DATA_URL},
    }

    # 3. Messages image (base64 source)
    r_msg = client.post(
        "/v1/messages",
        json={
            "model": "local/test",
            "max_tokens": 16,
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": "inspect"},
                        {
                            "type": "image",
                            "source": {"type": "base64", "media_type": "image/png", "data": _TINY_PNG_B64},
                        },
                    ],
                }
            ],
        },
    )
    assert r_msg.status_code == 200, r_msg.text
    assert adapter.last_body["messages"][0]["content"][1] == {
        "type": "image_url",
        "image_url": {"url": _TINY_PNG_DATA_URL},
    }

    # 4. system and tool content still only allow text
    bad_sys = client.post(
        "/v1/responses",
        json={
            "model": "local/test",
            "max_output_tokens": 16,
            "instructions": [{"type": "input_image", "image_url": _TINY_PNG_DATA_URL}],
            "input": "hi",
        },
    )
    assert bad_sys.status_code == 400
    bad_tool = client.post(
        "/v1/messages",
        json={
            "model": "local/test",
            "max_tokens": 16,
            "messages": [
                {
                    "role": "assistant",
                    "content": [{"type": "tool_use", "id": "t1", "name": "f", "input": {}}],
                },
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "tool_result",
                            "tool_use_id": "t1",
                            "content": [
                                {
                                    "type": "image",
                                    "source": {"type": "base64", "media_type": "image/png", "data": _TINY_PNG_B64},
                                }
                            ],
                        }
                    ],
                },
            ],
        },
    )
    assert bad_tool.status_code == 400


def test_s01_i34_t6_malformed_inputs_return_400_with_field_path(api):
    client, adapter, _ = api
    r_missing_call_id = client.post(
        "/v1/responses",
        json={
            "model": "local/test",
            "max_output_tokens": 16,
            "input": [
                {"role": "user", "content": "weather?"},
                {"type": "function_call", "call_id": "c1", "name": "weather", "arguments": "{}"},
                {"type": "function_call_output", "output": "Sunny"},
            ],
        },
    )
    assert r_missing_call_id.status_code == 400
    assert "input[2].call_id" in r_missing_call_id.json()["error"]["message"]

    r_bad_item = client.post(
        "/v1/responses",
        json={"model": "local/test", "max_output_tokens": 16, "input": ["not-an-object"]},
    )
    assert r_bad_item.status_code == 400
    assert "input[0]" in r_bad_item.json()["error"]["message"]

    r_missing_role = client.post(
        "/v1/messages",
        json={"model": "local/test", "max_tokens": 16, "messages": [{"content": "hi"}]},
    )
    assert r_missing_role.status_code == 400
    assert "messages[0].role" in r_missing_role.json()["error"]["message"]
    assert adapter.calls == 0


def test_s01_i34_t7_internal_keyerror_attributeerror_indexerror_return_500_without_raw_message(tmp_path):
    for bug_exc in (KeyError("secret_internal_key"), AttributeError("secret_attr"), IndexError("secret_idx")):
        client, adapter, _ = _make_api_with_manifest(tmp_path / type(bug_exc).__name__)

        def buggy_chat(body, *, task_id, timeout_s, on_event=None, _exc=bug_exc):
            raise _exc

        adapter.chat = buggy_chat
        resp = client.post(
            "/v1/chat/completions",
            json={"model": "local/test", "messages": [{"role": "user", "content": "hi"}], "max_tokens": 16},
        )
        assert resp.status_code == 500
        payload = resp.json()
        assert payload["error"]["message"] == "internal_error"
        assert payload["error"]["type"] == "api_error"
        assert "secret_" not in resp.text


def test_s01_i34_t8_malformed_tool_arguments_json_in_messages_returns_raw_arguments_without_400(tmp_path):
    client, adapter, _ = _make_api_with_manifest(tmp_path)

    def broken_tool_json_chat(body, *, task_id, timeout_s, on_event=None):
        return {
            "text": "",
            "message": {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {
                        "id": "call_bad_1",
                        "type": "function",
                        "function": {"name": "weather", "arguments": '{"city": "Shanghai"'},
                    }
                ],
            },
            "finish_reason": "tool_calls",
            "input_tokens": 5,
            "output_tokens": 4,
            "duration_ms": 2,
        }

    adapter.chat = broken_tool_json_chat
    resp = client.post(
        "/v1/messages",
        json={
            "model": "local/test",
            "messages": [{"role": "user", "content": "weather?"}],
            "max_tokens": 32,
            "tools": [{"name": "weather", "input_schema": {"type": "object"}}],
        },
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["content"][0]["type"] == "tool_use"
    assert data["content"][0]["input"] == {"_raw_arguments": '{"city": "Shanghai"'}


def test_s01_i34_t9_responses_echoes_tools_tool_choice_parallel_and_nullable_reasoning_tokens_and_messages_start_usage(
    tmp_path,
):
    client, adapter, _ = _make_api_with_manifest(tmp_path)
    req_tools = [{"type": "function", "name": "weather", "parameters": {"type": "object", "properties": {}}}]

    # 1. Without upstream reasoning_tokens -> reasoning_tokens is null, tools/tool_choice/parallel_tool_calls echoed
    resp = client.post(
        "/v1/responses",
        json={
            "model": "local/test",
            "input": "weather?",
            "max_output_tokens": 32,
            "tools": req_tools,
            "tool_choice": "required",
            "parallel_tool_calls": False,
        },
    )
    assert resp.status_code == 200, resp.text
    r_json = resp.json()
    assert r_json["tools"] == req_tools
    assert r_json["tool_choice"] == "required"
    assert r_json["parallel_tool_calls"] is False
    assert r_json["usage"]["output_tokens_details"]["reasoning_tokens"] is None

    # 2. With upstream reasoning_tokens -> preserved
    orig_chat = adapter.chat

    def chat_with_reasoning_tokens(body, *, task_id, timeout_s, on_event=None):
        res = orig_chat(body, task_id=task_id, timeout_s=timeout_s, on_event=on_event)
        res["reasoning_tokens"] = 19
        return res

    adapter.chat = chat_with_reasoning_tokens
    resp2 = client.post(
        "/v1/responses",
        json={"model": "local/test", "input": "hi", "max_output_tokens": 32},
    )
    assert resp2.status_code == 200, resp2.text
    assert resp2.json()["usage"]["output_tokens_details"]["reasoning_tokens"] == 19

    # 3. Streaming Messages message_start.usage has {"input_tokens": 0, "output_tokens": 0}
    msg_stream = client.post(
        "/v1/messages",
        json={"model": "local/test", "messages": [{"role": "user", "content": "hi"}], "max_tokens": 32, "stream": True},
    )
    assert msg_stream.status_code == 200
    first_data = next(line[6:] for line in msg_stream.text.splitlines() if line.startswith("data: "))
    start_evt = json.loads(first_data)
    assert start_evt["type"] == "message_start"
    assert start_evt["message"]["usage"] == {"input_tokens": 0, "output_tokens": 0}


def test_s01_i34_t10_cli_target_ignores_temperature_and_top_p_with_header_but_rejects_stop_and_thinking(tmp_path):
    client, adapter, _ = _make_api_with_manifest(tmp_path, adapter_kind="codex_cli")

    ok_resp = client.post(
        "/v1/chat/completions",
        json={
            "model": "local/test",
            "messages": [{"role": "user", "content": "hi"}],
            "max_tokens": 32,
            "temperature": 0.7,
            "top_p": 0.9,
        },
    )
    assert ok_resp.status_code == 200, ok_resp.text
    assert "temperature" not in adapter.last_body
    assert "top_p" not in adapter.last_body
    ignored = {p.strip() for p in ok_resp.headers.get("X-Ryn-Ignored-Parameters", "").split(",") if p.strip()}
    assert ignored == {"temperature", "top_p"}

    for forbidden in ({"stop": ["END"]}, {"enable_thinking": True}):
        bad = client.post(
            "/v1/chat/completions",
            json={"model": "local/test", "messages": [{"role": "user", "content": "hi"}], "max_tokens": 32, **forbidden},
        )
        assert bad.status_code == 400


def test_s01_i34_t11_chat_prompt_distinct_unsupported_format_and_oversized_messages():
    from rynmesh.llm_package.chat_prompt import CHAT_FORMAT, decode_chat_prompt

    with pytest.raises(ValueError) as exc_fmt:
        decode_chat_prompt("hello", "unsupported_v99")
    assert str(exc_fmt.value) == "prompt format is not supported"

    oversized = json.dumps([{"role": "user", "content": "a" * (65 * 1024)}])
    with pytest.raises(ValueError) as exc_big:
        decode_chat_prompt(oversized, CHAT_FORMAT)
    assert str(exc_big.value) == "chat prompt exceeds 64 KiB; send messages in the chat field instead"


def test_s01_i34_t12_provider_handle_execute_order_and_submit_order_default_max_tokens_to_manifest_limit(
    tmp_path, monkeypatch
):
    import time

    from rynmesh.llm_package import routes
    from rynmesh.llm_package.cli_adapter import CLIAgentAdapter
    from rynmesh.llm_package.manifest import save_manifest

    # 1. ProviderService.handle without max_tokens -> uses self.manifest.max_output_tokens (1536, not 64)
    provider = RynmeshStore(home=tmp_path / "provider", network_dir=tmp_path / "net")
    consumer = RynmeshStore(home=tmp_path / "consumer", network_dir=tmp_path / "net")
    pk = peer_box.load_or_create_messaging_key(tmp_path / "pk")
    ck = peer_box.load_or_create_messaging_key(tmp_path / "ck")

    seen_provider_max: list[int] = []

    class InferTrackingAdapter(ChatAdapter):
        def infer(self, prompt, *, max_tokens, timeout_s, task_id="", messages=None):
            seen_provider_max.append(max_tokens)
            return {"text": "ok", "input_tokens": 3, "output_tokens": 2, "duration_ms": 1}

    manifest_1536 = LLMPackageManifest(
        package_id="svc",
        mode="openai_compatible",
        public_model_alias="alias",
        base_url="http://127.0.0.1:1",
        max_output_tokens=1536,
        context_window=8192,
    )
    service = ProviderService(
        manifest=manifest_1536,
        adapter=InferTrackingAdapter(),
        store=provider,
        task_store=TaskOrderStore(tmp_path / "orders"),
        balance=TaskBalanceLedger(tmp_path / "balance.json"),
        messaging_key=pk,
        access_check=lambda *_: None,
    )
    sealed_req = seal_task(
        body={
            "task_id": "t_prov_default",
            "service_id": "svc",
            "prompt": "hello",
            "max_amount": 1,
            "reply_messaging_pub": peer_box.public_key_b64(ck),
        },
        task_id="t_prov_default",
        kind="llm_request",
        sender_peer_id=consumer.peer_id,
        recipient_peer_id=provider.peer_id,
        sender_signing_key=consumer.private_key_bytes,
        recipient_messaging_pub=peer_box.public_key_b64(pk),
        expires_at=_expires(300),
    ).to_dict()
    service.handle(sealed_req)
    assert seen_provider_max == [1536]

    # 2 & 3. execute_order (/api/local/llm/orders) and submit_order (/api/local/llm/orders/async) without max_tokens
    home = tmp_path / "node_orders"
    node_store = RynmeshStore(home=home, network_dir=tmp_path / "net_orders")
    app = FastAPI()
    routes.install_llm_routes(
        app,
        store=node_store,
        home=home,
        messaging_key=peer_box.load_or_create_messaging_key(home / "messaging.x25519"),
        resolve_endpoint=lambda _: "",
        resolve_pubkey=lambda _: "",
    )
    cli_manifest_path = save_manifest(
        LLMPackageManifest(
            package_id="codex-cli",
            mode="codex_cli",
            adapter="codex_cli",
            runtime="external",
            public_model_alias="Codex CLI",
            max_output_tokens=1536,
            context_window=8192,
        ),
        home / "llm/codex-cli/manifest.json",
    )
    seen_cli_max: list[int] = []
    monkeypatch.setattr(routes, "connect_cli", lambda **_: {"manifest": str(cli_manifest_path)})
    monkeypatch.setattr(CLIAgentAdapter, "health", lambda _: {"ok": True})

    def fake_cli_infer(self, prompt, *, max_tokens, timeout_s, task_id="", messages=None, model=""):
        seen_cli_max.append(max_tokens)
        return {"text": "cli ok", "input_tokens": 4, "output_tokens": 2, "duration_ms": 1}

    monkeypatch.setattr(CLIAgentAdapter, "infer", fake_cli_infer)

    with TestClient(app) as order_client:
        assert order_client.post("/api/local/llm/cli-services/codex_cli/setup").status_code == 200

        sync_res = order_client.post(
            "/api/local/llm/orders",
            json={"provider_peer_id": node_store.peer_id, "service_id": "codex-cli", "prompt": "sync prompt"},
        )
        assert sync_res.status_code == 200, sync_res.text

        async_res = order_client.post(
            "/api/local/llm/orders/async",
            json={"provider_peer_id": node_store.peer_id, "service_id": "codex-cli", "prompt": "async prompt"},
        )
        assert async_res.status_code == 200, async_res.text
        async_tid = async_res.json()["task_id"]
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline:
            st = order_client.get(f"/api/local/llm/orders/{async_tid}").json()
            if st.get("state") == "succeeded":
                break
            time.sleep(0.02)
        assert st.get("state") == "succeeded", st

    assert seen_cli_max == [1536, 1536]


# ---------------------------------------------------------------------------
# S01-I29 tests (T1 - T4)
# ---------------------------------------------------------------------------


async def _asgi_call(app, *, method: str, path: str, headers: list[tuple[bytes, bytes]], receive_fn):
    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": method,
        "scheme": "http",
        "path": path,
        "raw_path": path.encode("ascii"),
        "query_string": b"",
        "headers": headers,
        "client": ("127.0.0.1", 50000),
        "server": ("127.0.0.1", 8791),
    }
    status_code = None
    resp_headers: dict[str, str] = {}
    body_parts: list[bytes] = []

    async def send(message):
        nonlocal status_code
        if message["type"] == "http.response.start":
            status_code = message["status"]
            for k, v in message.get("headers", []):
                resp_headers[k.decode("latin1").lower()] = v.decode("latin1")
        elif message["type"] == "http.response.body":
            body_parts.append(message.get("body", b""))

    await app(scope, receive_fn, send)
    return status_code, resp_headers, b"".join(body_parts)


def test_s01_i29_t1_slow_chunked_body_every_2s_for_30s_is_accepted(tmp_path):
    adapter = ChatAdapter()
    manifest = LLMPackageManifest(
        package_id="test",
        mode="openai_compatible",
        public_model_alias="test-model",
        base_url="http://127.0.0.1:1",
        max_output_tokens=512,
        context_window=8_000_000,
    )
    manager = SimpleNamespace(
        adapter=adapter,
        manifest=manifest,
        _slots=threading.BoundedSemaphore(2),
        _lock=threading.Lock(),
        _running=0,
    )
    app = FastAPI()
    install_inference_api(
        app,
        home=tmp_path,
        store=SimpleNamespace(peer_id="self"),
        active_manager=lambda: manager,
        discover=lambda _: [],
        execute_order=None,
        cancel_order=None,
    )
    with TestClient(app) as client:
        key = client.post("/api/local/llm/api-keys", json={"name": "slow-ok", "output_token_limit": 100000}).json()

    # Build a ~15 MiB valid JSON payload split into 15 x 1 MiB chunks sent 2 s apart (30 s total)
    blob = "x" * (15 * 1024 * 1024 - 256)
    full_payload = json.dumps(
        {
            "model": "local/test",
            "messages": [{"role": "user", "content": "hi"}],
            "max_tokens": 16,
            "metadata": {"padding": blob},
        }
    ).encode("utf-8")
    chunk_size = 1024 * 1024
    chunks = [full_payload[i : i + chunk_size] for i in range(0, len(full_payload), chunk_size)]
    assert len(chunks) == 15

    async def scenario():
        idx = 0

        async def slow_receive():
            nonlocal idx
            if idx < len(chunks):
                if idx > 0:
                    await asyncio.sleep(2.0)
                else:
                    await asyncio.sleep(2.0)
                part = chunks[idx]
                idx += 1
                return {"type": "http.request", "body": part, "more_body": idx < len(chunks)}
            await asyncio.sleep(60)
            return {"type": "http.disconnect"}

        return await _asgi_call(
            app,
            method="POST",
            path="/v1/chat/completions",
            headers=[
                (b"host", b"127.0.0.1:8791"),
                (b"content-type", b"application/json"),
                (b"authorization", ("Bearer " + key["key"]).encode("ascii")),
            ],
            receive_fn=slow_receive,
        )

    status, _headers, raw_body = asyncio.run(scenario())
    assert status == 200, raw_body
    assert json.loads(raw_body)["choices"][0]["message"]["content"] == "Hello world"


def test_s01_i29_t2_idle_timeout_and_total_timeout_return_distinct_408_reasons(monkeypatch):
    from rynmesh.llm_package import safety
    from rynmesh.llm_package.safety import InferenceBodyLimit

    raw_app = FastAPI()

    @raw_app.post("/v1/chat/completions")
    async def dummy():
        return {"ok": True}

    # 1. Default 15 s idle timeout without monkeypatching, plus 10 s silence with idle_timeout=8 s -> 408 request_body_idle_timeout
    assert safety.BODY_IDLE_TIMEOUT_S == 15
    app_default_idle = InferenceBodyLimit(raw_app)

    async def default_15s_idle_scenario():
        async def no_data_receive():
            await asyncio.sleep(15.2)
            return {"type": "http.request", "body": b"{}", "more_body": False}

        return await _asgi_call(
            app_default_idle,
            method="POST",
            path="/v1/chat/completions",
            headers=[(b"host", b"127.0.0.1")],
            receive_fn=no_data_receive,
        )

    st_def, _, body_def = asyncio.run(default_15s_idle_scenario())
    assert st_def == 408
    assert body_def == b"request_body_idle_timeout"

    monkeypatch.setattr(safety, "BODY_IDLE_TIMEOUT_S", 8.0)
    app_idle = InferenceBodyLimit(raw_app)

    async def idle_10s_scenario():
        sent_first = False

        async def stalled_receive():
            nonlocal sent_first
            if not sent_first:
                sent_first = True
                return {"type": "http.request", "body": b'{"model":', "more_body": True}
            await asyncio.sleep(10.0)
            return {"type": "http.request", "body": b' "x"}', "more_body": False}

        return await _asgi_call(
            app_idle,
            method="POST",
            path="/v1/chat/completions",
            headers=[(b"host", b"127.0.0.1")],
            receive_fn=stalled_receive,
        )

    st_idle, _, body_idle = asyncio.run(idle_10s_scenario())
    assert st_idle == 408
    assert b"request_body_idle_timeout" in body_idle

    # 2. Total timeout: steady trickle faster than idle_timeout exceeding total_timeout -> 408 request_body_total_timeout
    app_total = InferenceBodyLimit(raw_app, idle_timeout=1.0, total_timeout=0.45)

    async def total_timeout_scenario():
        async def trickle_receive():
            await asyncio.sleep(0.15)
            return {"type": "http.request", "body": b" ", "more_body": True}

        return await _asgi_call(
            app_total,
            method="POST",
            path="/v1/chat/completions",
            headers=[(b"host", b"127.0.0.1")],
            receive_fn=trickle_receive,
        )

    st_tot, _, body_tot = asyncio.run(total_timeout_scenario())
    assert st_tot == 408
    assert b"request_body_total_timeout" in body_tot


def test_s01_i29_t3_get_requests_bypass_http_slots_when_32_posts_occupy_all_slots(tmp_path):
    client_sync, _adapter, _ = _make_api_with_manifest(tmp_path)
    app = client_sync.app
    auth_header = client_sync.headers["Authorization"].encode("ascii")

    async def scenario():
        all_32_in = asyncio.Event()
        release_posts = asyncio.Event()
        in_flight = 0

        async def hold_post_slot():
            nonlocal in_flight

            async def blocking_receive():
                nonlocal in_flight
                in_flight += 1
                if in_flight == 32:
                    all_32_in.set()
                await release_posts.wait()
                return {"type": "http.disconnect"}

            return await _asgi_call(
                app,
                method="POST",
                path="/v1/chat/completions",
                headers=[(b"host", b"127.0.0.1:8791"), (b"authorization", auth_header)],
                receive_fn=blocking_receive,
            )

        post_tasks = [asyncio.create_task(hold_post_slot()) for _ in range(32)]
        try:
            await asyncio.wait_for(all_32_in.wait(), timeout=5.0)

            # A 33rd POST is rejected with 429 because all 32 HTTP_SLOTS are full
            async def empty_body():
                return {"type": "http.request", "body": b"{}", "more_body": False}

            st_post33, _, body_post33 = await _asgi_call(
                app,
                method="POST",
                path="/v1/chat/completions",
                headers=[(b"host", b"127.0.0.1:8791"), (b"authorization", auth_header)],
                receive_fn=empty_body,
            )
            assert st_post33 == 429
            assert b"inference_connection_limit" in body_post33

            # GET status & model list requests still succeed with 200 without taking HTTP_SLOTS
            for get_path in ("/v1/models", "/api/local/llm/api-access"):
                st_get, _, body_get = await _asgi_call(
                    app,
                    method="GET",
                    path=get_path,
                    headers=[(b"host", b"127.0.0.1:8791"), (b"authorization", auth_header)],
                    receive_fn=empty_body,
                )
                assert st_get == 200, (get_path, st_get, body_get)
        finally:
            release_posts.set()
            await asyncio.gather(*post_tasks)

    asyncio.run(scenario())


def test_s01_i29_t4_413_returns_json_with_limit_bytes_and_image_compression_hint(tmp_path):
    from rynmesh.llm_package.safety import MAX_REQUEST, install_body_limit, read_json

    # 1. InferenceBodyLimit on /api/local/llm/ (16 MiB) and /api/peer/llm/ (64 MiB) + safety.read_json
    custom_app = FastAPI()
    install_body_limit(custom_app)

    @custom_app.post("/api/local/llm/small-json")
    async def small_json_route(request: Request):
        return await read_json(request, limit=1024)

    with TestClient(custom_app) as c:
        r_small = c.post("/api/local/llm/small-json", content=b"x" * 2048)
        assert r_small.status_code == 413
        assert r_small.json() == {"detail": "request_too_large", "limit_bytes": 1024}

        r_local = c.post(
            "/api/local/llm/orders",
            content=iter([b"a" * (MAX_REQUEST // 2 + 100), b"b" * (MAX_REQUEST // 2 + 100)]),
        )
        assert r_local.status_code == 413
        assert r_local.json() == {"detail": "request_too_large", "limit_bytes": MAX_REQUEST}

        r_peer = c.post(
            "/api/peer/llm/task",
            content=iter([b"a" * (2 * MAX_REQUEST + 100), b"b" * (2 * MAX_REQUEST + 100)]),
        )
        assert r_peer.status_code == 413
        assert r_peer.json() == {"detail": "request_too_large", "limit_bytes": 4 * MAX_REQUEST}

    # 2. Loopback /v1/ inference API 413 (both middleware and api.py:handle) includes limit_bytes AND Chinese multi-image compression hint
    v1_client, _adapter, _ = _make_api_with_manifest(tmp_path)
    r_v1 = v1_client.post(
        "/v1/chat/completions",
        content=iter([b"a" * (MAX_REQUEST // 2 + 100), b"b" * (MAX_REQUEST // 2 + 100)]),
    )
    assert r_v1.status_code == 413
    r_v1_json = r_v1.json()
    assert r_v1_json["limit_bytes"] == MAX_REQUEST
    assert "多张图片时请先压缩或减少张数" in json.dumps(r_v1_json, ensure_ascii=False)

    direct_app = FastAPI()
    direct_app.state.inference_body_limit = True  # exercise api.py:handle stream limit directly
    install_inference_api(
        direct_app,
        home=tmp_path / "direct_v1",
        store=SimpleNamespace(peer_id="self"),
        active_manager=lambda: None,
        discover=lambda _: [],
        execute_order=None,
        cancel_order=None,
    )
    with TestClient(direct_app) as direct_client:
        dk = direct_client.post("/api/local/llm/api-keys", json={"name": "d", "output_token_limit": 1000}).json()
        r_direct = direct_client.post(
            "/v1/chat/completions",
            headers={"Authorization": "Bearer " + dk["key"]},
            content=iter([b"a" * (MAX_REQUEST // 2 + 100), b"b" * (MAX_REQUEST // 2 + 100)]),
        )
        assert r_direct.status_code == 413
        r_direct_json = r_direct.json()
        assert r_direct_json["limit_bytes"] == MAX_REQUEST
        assert "多张图片时请先压缩或减少张数" in r_direct_json["error"]["message"]
