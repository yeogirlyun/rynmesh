"""Tests for S01-I35: local inference API quota accounting, key PATCH/purge/404, concurrency & 10s slot queue, closed error codes, catalog/health cache, and disconnect release."""

from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
import copy
import json
import sqlite3
import threading
import time
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from rynmesh.llm_package import routes
from rynmesh.llm_package.adapters import AdapterError
from rynmesh.llm_package.api import InferenceKeys, install_inference_api
from rynmesh.llm_package.cli_adapter import CLIAgentAdapter
from rynmesh.llm_package.manifest import LLMPackageManifest, save_manifest
from rynmesh.services import peer_box
from rynmesh.store import RynmeshStore


class _StubAdapter:
    def __init__(self) -> None:
        self.health_calls = 0
        self.chat_calls = 0
        self.cancelled_tasks: list[str] = []
        self._lock = threading.Lock()
        self.chat_fn = None
        self.health_fn = None

    def health(self) -> dict:
        with self._lock:
            self.health_calls += 1
        if self.health_fn is not None:
            return self.health_fn()
        return {"ok": True}

    def chat(self, body: dict, *, task_id: str, timeout_s: float, on_event=None, **_kw) -> dict:
        with self._lock:
            self.chat_calls += 1
        if self.chat_fn is not None:
            return self.chat_fn(body, task_id=task_id, timeout_s=timeout_s, on_event=on_event)
        if body.get("stream") and on_event:
            on_event({"choices": [{"index": 0, "delta": {"content": "ok"}, "finish_reason": None}]})
            on_event({"choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]})
            on_event({"choices": [], "usage": {"prompt_tokens": 5, "completion_tokens": 3, "total_tokens": 8}})
        return {
            "text": "ok",
            "message": {"role": "assistant", "content": "ok"},
            "finish_reason": "stop",
            "input_tokens": 5,
            "output_tokens": 3,
            "duration_ms": 2,
        }

    def cancel(self, task_id: str) -> bool:
        with self._lock:
            self.cancelled_tasks.append(task_id)
        return True


def _build_api(tmp_path, *, slots: int = 2, discover=None):
    adapter = _StubAdapter()
    manifest = LLMPackageManifest(
        package_id="test",
        mode="openai_compatible",
        public_model_alias="test-model",
        base_url="http://127.0.0.1:1",
        max_output_tokens=512,
        context_window=8192,
    )
    manager = SimpleNamespace(
        adapter=adapter,
        manifest=manifest,
        _slots=threading.BoundedSemaphore(slots),
        _lock=threading.Lock(),
        _running=0,
    )
    app = FastAPI()
    install_inference_api(
        app,
        home=tmp_path,
        store=SimpleNamespace(peer_id="self"),
        active_manager=lambda: manager,
        discover=discover or (lambda _: []),
        execute_order=None,
        cancel_order=None,
    )
    client = TestClient(app, base_url="http://127.0.0.1")
    key = client.post("/api/local/llm/api-keys", json={"name": "agent", "output_token_limit": 1000}).json()
    client.headers["Authorization"] = "Bearer " + key["key"]
    return app, client, adapter, manager, key


def _key_row(client: TestClient, key_id: str) -> dict:
    keys = client.get("/api/local/llm/api-access").json()["keys"]
    return next(item for item in keys if item["id"] == key_id)


def test_t1_upstream_500_keeps_used_unchanged_and_partial_reported_usage_charges_actual(tmp_path) -> None:
    _app, client, adapter, _manager, key = _build_api(tmp_path)

    # First normal call consumes 3 output tokens.
    ok_resp = client.post(
        "/v1/chat/completions",
        json={"model": "local/test", "messages": [{"role": "user", "content": "hi"}], "max_tokens": 120},
    )
    assert ok_resp.status_code == 200
    assert _key_row(client, key["id"])["used_output_tokens"] == 3

    # Upstream 500 without reported usage -> used stays 3 (does NOT charge the 120 reservation).
    def fail_500(body, *, task_id, timeout_s, on_event=None):
        raise AdapterError("upstream 500 secret detail", code="upstream_http_failed")

    adapter.chat_fn = fail_500
    err_resp = client.post(
        "/v1/chat/completions",
        json={"model": "local/test", "messages": [{"role": "user", "content": "hi"}], "max_tokens": 120},
    )
    assert err_resp.status_code == 502
    assert _key_row(client, key["id"])["used_output_tokens"] == 3

    # Streaming upstream failure without reported usage -> used still stays 3.
    stream_err = client.post(
        "/v1/chat/completions",
        json={"model": "local/test", "messages": [{"role": "user", "content": "hi"}], "max_tokens": 120, "stream": True},
    )
    assert stream_err.status_code == 200
    assert "upstream_http_failed" in stream_err.text
    assert _key_row(client, key["id"])["used_output_tokens"] == 3

    # Failure WITH partial upstream reported usage charges only reported output_tokens (11), not 120.
    def fail_partial(body, *, task_id, timeout_s, on_event=None):
        raise AdapterError(
            "truncated after 11 tokens",
            code="upstream_stream_truncated",
            reported_usage={"input_tokens": 9, "output_tokens": 11},
        )

    adapter.chat_fn = fail_partial
    part_resp = client.post(
        "/v1/chat/completions",
        json={"model": "local/test", "messages": [{"role": "user", "content": "hi"}], "max_tokens": 120},
    )
    assert part_resp.status_code == 502
    assert _key_row(client, key["id"])["used_output_tokens"] == 14


def test_t2_patch_adjusts_quota_resets_used_and_delete_purge_removes_only_revoked(tmp_path) -> None:
    _app, client, _adapter, _manager, _default_key = _build_api(tmp_path)
    small = client.post("/api/local/llm/api-keys", json={"name": "small", "output_token_limit": 3}).json()
    headers = {"Authorization": "Bearer " + small["key"]}
    payload = {"model": "local/test", "messages": [{"role": "user", "content": "hi"}], "max_tokens": 3}

    assert client.post("/v1/chat/completions", json=payload, headers=headers).status_code == 200
    assert _key_row(client, small["id"])["used_output_tokens"] == 3
    assert client.post("/v1/chat/completions", json=payload, headers=headers).status_code == 429

    # PATCH raises output_token_limit to 6 -> another 3-token call succeeds (used becomes 6).
    patched = client.patch(f"/api/local/llm/api-keys/{small['id']}", json={"output_token_limit": 6})
    assert patched.status_code == 200, patched.text
    assert patched.json()["output_token_limit"] == 6
    assert client.post("/v1/chat/completions", json=payload, headers=headers).status_code == 200
    assert _key_row(client, small["id"])["used_output_tokens"] == 6

    # PATCH reset_used=True resets used_output_tokens back to 0.
    reset_resp = client.patch(f"/api/local/llm/api-keys/{small['id']}", json={"reset_used": True})
    assert reset_resp.status_code == 200, reset_resp.text
    assert reset_resp.json()["used_output_tokens"] == 0
    assert _key_row(client, small["id"])["used_output_tokens"] == 0

    # DELETE ?purge=true on an unrevoked key is rejected and preserves the key.
    purge_active = client.delete(f"/api/local/llm/api-keys/{small['id']}?purge=true")
    assert purge_active.status_code == 409
    assert any(k["id"] == small["id"] for k in client.get("/api/local/llm/api-access").json()["keys"])

    # Revoke first, then DELETE ?purge=true permanently deletes the revoked key.
    rev = client.delete(f"/api/local/llm/api-keys/{small['id']}")
    assert rev.status_code == 200 and rev.json()["revoked"] is True
    purged = client.delete(f"/api/local/llm/api-keys/{small['id']}?purge=true")
    assert purged.status_code == 200
    assert not any(k["id"] == small["id"] for k in client.get("/api/local/llm/api-access").json()["keys"])


def test_t3_revoke_nonexistent_key_returns_404(tmp_path) -> None:
    _app, client, _adapter, _manager, _key = _build_api(tmp_path)
    missing_del = client.delete("/api/local/llm/api-keys/does_not_exist_key_id")
    assert missing_del.status_code == 404
    missing_purge = client.delete("/api/local/llm/api-keys/does_not_exist_key_id?purge=true")
    assert missing_purge.status_code == 404
    missing_patch = client.patch("/api/local/llm/api-keys/does_not_exist_key_id", json={"reset_used": True})
    assert missing_patch.status_code == 404


def test_t4_key_max_concurrent_3_takes_effect_and_legacy_sqlite_migrates_default_2(tmp_path) -> None:
    # Verify legacy SQLite table without max_concurrent migrates cleanly with default 2.
    legacy_home = tmp_path / "legacy"
    (legacy_home / "llm").mkdir(parents=True)
    db_path = legacy_home / "llm" / "inference-keys.sqlite3"
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "CREATE TABLE keys (id TEXT PRIMARY KEY, digest TEXT UNIQUE, name TEXT, created REAL, revoked INTEGER DEFAULT 0, token_limit INTEGER, used INTEGER DEFAULT 0)"
        )
        conn.execute(
            "INSERT INTO keys(id, digest, name, created, revoked, token_limit, used) VALUES('legacy1', 'd1', 'old', 1.0, 0, 500, 0)"
        )
    migrated = InferenceKeys(legacy_home)
    listed = migrated.list()
    assert listed[0]["id"] == "legacy1" and listed[0]["max_concurrent"] == 2

    app, client, adapter, _manager, key = _build_api(tmp_path / "conc", slots=8)

    # Validation: max_concurrent > 16 is rejected with 400.
    assert client.patch(f"/api/local/llm/api-keys/{key['id']}", json={"max_concurrent": 17}).status_code == 400
    assert client.patch(f"/api/local/llm/api-keys/{key['id']}", json={"max_concurrent": 0}).status_code == 400

    # Set max_concurrent = 3 via PATCH.
    patched = client.patch(f"/api/local/llm/api-keys/{key['id']}", json={"max_concurrent": 3})
    assert patched.status_code == 200, patched.text
    assert patched.json()["max_concurrent"] == 3

    all_three_entered = threading.Event()
    release = threading.Event()
    in_flight = 0
    lock = threading.Lock()

    def barrier_chat(body, *, task_id, timeout_s, on_event=None):
        nonlocal in_flight
        with lock:
            in_flight += 1
            if in_flight == 3:
                all_three_entered.set()
        assert release.wait(timeout=5.0)
        return {
            "text": "done",
            "message": {"role": "assistant", "content": "done"},
            "finish_reason": "stop",
            "input_tokens": 2,
            "output_tokens": 1,
            "duration_ms": 1,
        }

    adapter.chat_fn = barrier_chat
    payload = {"model": "local/test", "messages": [{"role": "user", "content": "hi"}], "max_tokens": 8}

    def post_once():
        with TestClient(app, base_url="http://127.0.0.1") as c:
            return c.post("/v1/chat/completions", json=payload, headers={"Authorization": "Bearer " + key["key"]})

    with ThreadPoolExecutor(max_workers=4) as pool:
        futures = [pool.submit(post_once) for _ in range(3)]
        try:
            assert all_three_entered.wait(timeout=3.0)
            # 4th concurrent request exceeds max_concurrent=3 -> 429 project_concurrency_limit
            fourth = post_once()
            assert fourth.status_code == 429
            assert fourth.json()["error"]["code"] == "project_concurrency_limit"
        finally:
            release.set()
        statuses = [f.result(timeout=5.0).status_code for f in futures]
    assert statuses == [200, 200, 200]


def test_t5_local_model_busy_for_1s_queues_and_succeeds(tmp_path) -> None:
    app, _client, adapter, _manager, key = _build_api(tmp_path, slots=1)
    first_entered = threading.Event()

    def hold_1s_chat(body, *, task_id, timeout_s, on_event=None):
        if not first_entered.is_set():
            first_entered.set()
            time.sleep(1.0)
        return {
            "text": "queued-ok",
            "message": {"role": "assistant", "content": "queued-ok"},
            "finish_reason": "stop",
            "input_tokens": 3,
            "output_tokens": 2,
            "duration_ms": 10,
        }

    adapter.chat_fn = hold_1s_chat
    payload = {"model": "local/test", "messages": [{"role": "user", "content": "hi"}], "max_tokens": 8}

    def send():
        with TestClient(app, base_url="http://127.0.0.1") as c:
            return c.post("/v1/chat/completions", json=payload, headers={"Authorization": "Bearer " + key["key"]})

    with ThreadPoolExecutor(max_workers=2) as pool:
        f1 = pool.submit(send)
        assert first_entered.wait(timeout=2.0)
        t0 = time.monotonic()
        r2 = pool.submit(send).result(timeout=5.0)
        elapsed = time.monotonic() - t0
        r1 = f1.result(timeout=5.0)

    assert r1.status_code == 200
    assert r2.status_code == 200, r2.text
    assert r2.json()["choices"][0]["message"]["content"] == "queued-ok"
    assert 0.5 <= elapsed < 4.0


def test_t6_local_model_busy_for_full_10s_returns_429_with_retry_after_5(tmp_path) -> None:
    app, _client, _adapter, manager, key = _build_api(tmp_path, slots=1)
    assert manager._slots.acquire(blocking=False) is True
    try:
        t0 = time.monotonic()
        with TestClient(app, base_url="http://127.0.0.1") as c:
            resp = c.post(
                "/v1/chat/completions",
                json={"model": "local/test", "messages": [{"role": "user", "content": "hi"}], "max_tokens": 8},
                headers={"Authorization": "Bearer " + key["key"]},
            )
        elapsed = time.monotonic() - t0
    finally:
        manager._slots.release()

    assert elapsed >= 9.8
    assert resp.status_code == 429
    assert resp.headers.get("Retry-After") == "5"
    assert resp.json()["error"]["code"] == "local_model_busy"


def test_t7_adapter_error_codes_map_to_http_statuses_fixed_messages_and_stream_error_codes(tmp_path) -> None:
    _app, client, adapter, _manager, _key = _build_api(tmp_path)

    cases = [
        ("inference_timeout", 504),
        ("runtime_busy", 429),
        ("model_daily_quota_exhausted", 429),
        ("upstream_image_rejected", 422),
        ("upstream_context_overflow", 400),
        ("source_protocol_parameters_unsupported", 400),
        ("upstream_http_failed", 502),
    ]
    for code, expected_status in cases:
        secret_msg = f"RAW_UPSTREAM_SECRET_FOR_{code}_http://internal.local:9999"

        def raise_err(body, *, task_id, timeout_s, on_event=None, _c=code, _m=secret_msg):
            raise AdapterError(_m, code=_c)

        adapter.chat_fn = raise_err

        # Non-streaming: status, closed error.code, and fixed message (never raw exception text)
        resp = client.post(
            "/v1/chat/completions",
            json={"model": "local/test", "messages": [{"role": "user", "content": "hi"}], "max_tokens": 16},
        )
        assert resp.status_code == expected_status, (code, resp.status_code, resp.text)
        err_obj = resp.json()["error"]
        assert err_obj["code"] == code
        assert "RAW_UPSTREAM_SECRET" not in resp.text
        assert "internal.local" not in resp.text
        assert err_obj["message"] and err_obj["message"] != code

    # Streaming across all 3 protocols uses the exact same closed code (e.g. inference_timeout, not inference_failed).
    def raise_timeout(body, *, task_id, timeout_s, on_event=None):
        raise AdapterError("secret timeout url", code="inference_timeout")

    adapter.chat_fn = raise_timeout
    r_resp_stream = client.post(
        "/v1/responses",
        json={"model": "local/test", "input": "hi", "max_output_tokens": 16, "stream": True},
    )
    assert r_resp_stream.status_code == 200
    data_lines = [json.loads(line[6:]) for line in r_resp_stream.text.splitlines() if line.startswith("data: ") and line[6:] != "[DONE]"]
    err_evt = data_lines[-1]
    assert err_evt["type"] == "error"
    assert err_evt["code"] == "inference_timeout"
    assert "secret timeout" not in r_resp_stream.text

    r_chat_stream = client.post(
        "/v1/chat/completions",
        json={"model": "local/test", "messages": [{"role": "user", "content": "hi"}], "max_tokens": 16, "stream": True},
    )
    chat_err = [json.loads(line[6:]) for line in r_chat_stream.text.splitlines() if line.startswith("data: ")][-1]
    assert chat_err["error"]["code"] == "inference_timeout"
    assert "secret timeout" not in r_chat_stream.text


def test_t8_consecutive_requests_call_health_once_with_catalog_and_background_health_cache(tmp_path, monkeypatch) -> None:
    import rynmesh.llm_package.api as api_mod

    discover_calls = 0

    def counting_discover(_net):
        nonlocal discover_calls
        discover_calls += 1
        return []

    _app, client, adapter, _manager, _key = _build_api(tmp_path, discover=counting_discover)
    assert adapter.health_calls == 0

    assert client.get("/v1/models").status_code == 200
    assert client.post(
        "/v1/chat/completions",
        json={"model": "local/test", "messages": [{"role": "user", "content": "1"}], "max_tokens": 8},
    ).status_code == 200
    assert client.post(
        "/v1/chat/completions",
        json={"model": "local/test", "messages": [{"role": "user", "content": "2"}], "max_tokens": 8},
    ).status_code == 200

    # Both catalog (5s) and health (10s) were cached: only 1 health() and 1 discover() call occurred.
    assert adapter.health_calls == 1
    assert discover_calls == 1

    # Advance monotonic clock by 6s (>5s catalog TTL, <10s health TTL): catalog refreshes, health still cached!
    base_now = time.monotonic()
    clock = [base_now + 6.0]
    monkeypatch.setattr(api_mod.time, "monotonic", lambda: clock[0])
    assert client.get("/v1/models").status_code == 200
    assert discover_calls == 2
    assert adapter.health_calls == 1

    # Advance past 10s health TTL: triggers background thread refresh without blocking caller.
    refreshed = threading.Event()

    def bg_health():
        refreshed.set()
        return {"ok": True}

    adapter.health_fn = bg_health
    clock[0] = base_now + 12.0
    assert client.get("/v1/models").status_code == 200
    assert refreshed.wait(timeout=2.0)
    assert adapter.health_calls == 2


def test_t9_client_disconnect_before_first_byte_releases_slot_and_quota_without_background_task(tmp_path) -> None:
    app, client, adapter, manager, key = _build_api(tmp_path, slots=1)

    # Case A: Stream generator closed after initial frame before run() ever starts, WITHOUT invoking Starlette BackgroundTask.
    async def close_before_run():
        class _DummyReq:
            headers = {"host": "127.0.0.1", "authorization": "Bearer " + key["key"]}
            client = SimpleNamespace(host="127.0.0.1")

            async def stream(self):
                yield json.dumps(
                    {"model": "local/test", "input": "hello", "max_output_tokens": 50, "stream": True}
                ).encode("utf-8")

            async def is_disconnected(self):
                return False

        route = next(r for r in app.routes if getattr(r, "path", None) == "/v1/responses")
        streaming_resp = await route.endpoint(_DummyReq())
        # Pull ONLY the very first start frame (before events(run, cancel) begins), then close body_iterator
        # deliberately skipping streaming_resp.background() to prove generate()'s finally releases slot & quota!
        it = streaming_resp.body_iterator.__aiter__()
        first_chunk = await it.__anext__()
        assert "response.created" in first_chunk
        await streaming_resp.body_iterator.aclose()

    asyncio.run(close_before_run())
    assert adapter.chat_calls == 0
    assert _key_row(client, key["id"])["used_output_tokens"] == 0
    assert manager._slots.acquire(blocking=False) is True
    manager._slots.release()

    # Case B: Client disconnects over ASGI while adapter.chat is blocked before emitting first byte -> cancelled & released.
    chat_entered = threading.Event()
    chat_cancelled = threading.Event()

    def slow_first_byte(body, *, task_id, timeout_s, on_event=None):
        chat_entered.set()
        assert chat_cancelled.wait(timeout=3.0)
        raise AdapterError("cancelled by client", code="inference_failed")

    orig_cancel = adapter.cancel

    def notify_cancel(task_id: str) -> bool:
        chat_cancelled.set()
        return orig_cancel(task_id)

    adapter.chat_fn = slow_first_byte
    adapter.cancel = notify_cancel

    async def asgi_disconnect_before_first_byte():
        sent_body = False
        disconnect_now = asyncio.Event()

        async def receive():
            nonlocal sent_body
            if not sent_body:
                sent_body = True
                return {
                    "type": "http.request",
                    "body": json.dumps(
                        {"model": "local/test", "messages": [{"role": "user", "content": "hi"}], "max_tokens": 60}
                    ).encode("utf-8"),
                    "more_body": False,
                }
            await disconnect_now.wait()
            return {"type": "http.disconnect"}

        async def send(_msg):
            return None

        scope = {
            "type": "http",
            "asgi": {"version": "3.0"},
            "http_version": "1.1",
            "method": "POST",
            "scheme": "http",
            "path": "/v1/chat/completions",
            "raw_path": b"/v1/chat/completions",
            "query_string": b"",
            "headers": [
                (b"host", b"127.0.0.1"),
                (b"content-type", b"application/json"),
                (b"authorization", ("Bearer " + key["key"]).encode("ascii")),
            ],
            "client": ("127.0.0.1", 51000),
            "server": ("127.0.0.1", 8791),
        }
        req_task = asyncio.create_task(app(scope, receive, send))
        await asyncio.to_thread(chat_entered.wait, 2.0)
        disconnect_now.set()
        await asyncio.wait_for(req_task, timeout=3.0)

    asyncio.run(asgi_disconnect_before_first_byte())
    assert chat_cancelled.is_set()
    assert _key_row(client, key["id"])["used_output_tokens"] == 0
    assert manager._slots.acquire(blocking=False) is True
    manager._slots.release()


def test_t10_local_cli_order_timeout_records_inference_timeout_not_local_cli_failed(tmp_path, monkeypatch) -> None:
    home = tmp_path / "node_cli_orders"
    node_store = RynmeshStore(home=home, network_dir=tmp_path / "net_cli_orders")
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
            max_output_tokens=512,
            context_window=8192,
        ),
        home / "llm/codex-cli/manifest.json",
    )
    monkeypatch.setattr(routes, "connect_cli", lambda **_: {"manifest": str(cli_manifest_path)})
    monkeypatch.setattr(CLIAgentAdapter, "health", lambda _: {"ok": True})

    mode = ["timeout"]

    def fake_cli_infer(self, prompt, *, max_tokens, timeout_s, task_id="", messages=None, model=""):
        if mode[0] == "timeout":
            raise AdapterError("CLI timed out at /secret/home", code="inference_timeout")
        raise RuntimeError("unexpected non-adapter crash")

    monkeypatch.setattr(CLIAgentAdapter, "infer", fake_cli_infer)

    with TestClient(app) as order_client:
        assert order_client.post("/api/local/llm/cli-services/codex_cli/setup").status_code == 200

        res_timeout = order_client.post(
            "/api/local/llm/orders",
            json={"provider_peer_id": node_store.peer_id, "service_id": "codex-cli", "prompt": "slow prompt"},
        )
        assert res_timeout.status_code == 200, res_timeout.text
        data_timeout = res_timeout.json()
        assert data_timeout["state"] == "failed"
        assert data_timeout["error_code"] == "inference_timeout"
        assert "/secret/home" not in res_timeout.text
        persisted = order_client.get(f"/api/local/llm/orders/{data_timeout['task_id']}").json()
        assert persisted["error_code"] == "inference_timeout"

        # Non-AdapterError still falls back to local_cli_failed.
        mode[0] = "crash"
        res_crash = order_client.post(
            "/api/local/llm/orders",
            json={"provider_peer_id": node_store.peer_id, "service_id": "codex-cli", "prompt": "crash prompt"},
        )
        assert res_crash.status_code == 200
        assert res_crash.json()["error_code"] == "local_cli_failed"
