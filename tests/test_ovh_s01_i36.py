"""Tests for S01-I36: OpenAICompatibleAdapter & NativeProtocolAdapter & AgentModelBridge polish."""
from __future__ import annotations

import ast
import io
import json
import pathlib
import urllib.error
from email.message import Message
from types import SimpleNamespace

import pytest

from rynmesh.agent_runtime.bridge import AgentModelBridge
from rynmesh.agent_runtime.store import AgentError
from rynmesh.llm_package import adapters
from rynmesh.llm_package.adapters import (
    RUNTIME_ERROR_CODES,
    AdapterError,
    OpenAICompatibleAdapter,
    actual_usage,
)
from rynmesh.llm_package.protocol_adapter import NativeProtocolAdapter


class _FakeHTTPResponse(io.BytesIO):
    def __init__(self, body: bytes, content_type: str = "application/json", status: int = 200):
        super().__init__(body)
        self.headers = Message()
        self.headers["Content-Type"] = content_type
        self.status = status

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()
        return False

    def getcode(self):
        return self.status


def _http_error(url: str, status: int, payload: dict) -> urllib.error.HTTPError:
    hdrs = Message()
    hdrs["Content-Type"] = "application/json"
    body = io.BytesIO(json.dumps(payload).encode("utf-8"))
    return urllib.error.HTTPError(url, status, "Bad Request", hdrs, body)


def test_t1_local_health_missing_model_keeps_model_not_found_and_api_allows_unlisted(monkeypatch):
    """T1: 本机来源列表缺模型保持 model_not_found 且自动选模加锁；API 来源缺模型不判死。"""
    local_adapter = OpenAICompatibleAdapter(
        base_url="http://127.0.0.1:18080",
        model="missing-local",
        allow_non_loopback=False,
    )
    monkeypatch.setattr(local_adapter, "models", lambda: [{"id": "llama-local"}])
    local_health = local_adapter.health()
    assert local_health["ok"] is False
    assert local_health["error_code"] == "model_not_found"

    auto_adapter = OpenAICompatibleAdapter(
        base_url="http://127.0.0.1:18080",
        model="",
        allow_non_loopback=False,
    )
    monkeypatch.setattr(auto_adapter, "models", lambda: [{"id": "auto-picked"}])
    auto_health = auto_adapter.health()
    assert auto_health["ok"] is True
    assert auto_adapter.model == "auto-picked"

    api_adapter = OpenAICompatibleAdapter(
        base_url="https://api.example.test",
        model="unlisted-gateway-model",
        allow_non_loopback=True,
    )
    monkeypatch.setattr(api_adapter, "models", lambda: [{"id": "other-model"}])
    api_health = api_adapter.health()
    assert api_health["ok"] is True
    assert api_health["model_listed"] is False


def test_t2_api_gateway_unlisted_model_health_ok_with_model_listed_false(monkeypatch):
    """T2: API 网关 /v1/models 未列出配置的模型时，health() 返回 ok=True 且 model_listed=False。"""
    adapter = OpenAICompatibleAdapter(
        base_url="https://gateway.example.test",
        model="custom-hidden-model",
        allow_non_loopback=True,
    )
    monkeypatch.setattr(adapter, "models", lambda: [{"id": "listed-a"}, {"id": "listed-b"}])
    res = adapter.health()
    assert res["ok"] is True
    assert res["model_listed"] is False
    assert res["model"] == "custom-hidden-model"
    assert res["model_count"] == 2


def test_t3_capabilities_caches_10_minutes_and_missing_env_does_not_bubble(monkeypatch):
    """T3: capabilities() 按 (base_url, model) 缓存 10 分钟，两次调用只发一次请求；缺环境变量不冒泡 AdapterError。"""
    clock = [1000.0]
    monkeypatch.setattr(adapters.time, "monotonic", lambda: clock[0])

    calls: list[str] = []

    def fake_urlopen(req, timeout=None):
        calls.append(req.full_url)
        return _FakeHTTPResponse(b'data: {"choices":[{"delta":{"content":"ok"}}]}\n\ndata: [DONE]\n\n', "text/event-stream")

    monkeypatch.setattr(adapters, "model_urlopen", fake_urlopen)

    a1 = OpenAICompatibleAdapter(base_url="https://cache-cap.example.test", model="m-cap", allow_non_loopback=True)
    a2 = OpenAICompatibleAdapter(base_url="https://cache-cap.example.test", model="m-cap", allow_non_loopback=True)

    cap1 = a1.capabilities()
    cap2 = a2.capabilities()
    assert cap1 == {"chat_completions": True, "streaming": True, "cancel": "best_effort"}
    assert cap2 == cap1
    assert len(calls) == 1

    # Advance past 10 minutes -> re-probes once.
    clock[0] += 601.0
    cap3 = a2.capabilities()
    assert cap3["streaming"] is True
    assert len(calls) == 2

    # Missing API key env var inside capabilities() must not raise AdapterError.
    env_name = "RYN_TEST_MISSING_CAP_KEY_S01_I36"
    monkeypatch.delenv(env_name, raising=False)
    missing_env_adapter = OpenAICompatibleAdapter(
        base_url="https://missing-env.example.test",
        model="m-env",
        api_key_env=env_name,
        allow_non_loopback=True,
    )
    cap_missing = missing_env_adapter.capabilities()
    assert isinstance(cap_missing, dict)
    assert cap_missing["streaming"] is False


def test_t4_infer_only_reasoning_rejected_and_length_uses_output_limit_code(monkeypatch):
    """T4: infer() 只取 content/choices[0].text，不把 reasoning_content 当正文；finish=length 报 upstream_output_limit_reached，否则报 upstream_response_invalid。"""
    adapter = OpenAICompatibleAdapter(base_url="http://127.0.0.1:18080", model="m-infer")

    # Case 1: reasoning_content only + finish_reason == "stop" -> upstream_response_invalid
    monkeypatch.setattr(
        adapter,
        "_json",
        lambda *a, **kw: {
            "choices": [{"message": {"role": "assistant", "content": "", "reasoning_content": "secret chain of thought"}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 5, "completion_tokens": 9},
        },
    )
    with pytest.raises(AdapterError) as exc_stop:
        adapter.infer(prompt="hi", max_tokens=32, task_id="t_infer_stop", timeout_s=5)
    assert exc_stop.value.code == "upstream_response_invalid"
    assert "secret chain of thought" not in str(exc_stop.value)

    # Case 2: reasoning_content only + finish_reason == "length" -> upstream_output_limit_reached
    monkeypatch.setattr(
        adapter,
        "_json",
        lambda *a, **kw: {
            "choices": [{"message": {"role": "assistant", "content": None, "reasoning_content": "exhausted tokens thinking"}, "finish_reason": "length"}],
            "usage": {"prompt_tokens": 8, "completion_tokens": 32},
        },
    )
    with pytest.raises(AdapterError) as exc_len:
        adapter.infer(prompt="hi", max_tokens=32, task_id="t_infer_len", timeout_s=5)
    assert exc_len.value.code == "upstream_output_limit_reached"
    assert exc_len.value.finish_reason == "length"
    assert exc_len.value.reported_usage == {"input_tokens": 8, "output_tokens": 32}

    # Case 3: legacy choices[0].text still accepted
    monkeypatch.setattr(
        adapter,
        "_json",
        lambda *a, **kw: {
            "choices": [{"text": "plain completion", "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 3, "completion_tokens": 2},
        },
    )
    ok_res = adapter.infer(prompt="hi", max_tokens=32, task_id="t_infer_ok", timeout_s=5)
    assert ok_res["text"] == "plain completion"


def test_t5_chat_400_stream_options_retries_without_field_and_remembers(monkeypatch):
    """T5: chat() 流式首次 400 且 request_rejected + param 指向 stream_options 时去掉该字段重发成功，按 (base_url, model) 记住且用量记为未知。"""
    sent_bodies: list[dict] = []
    sse_payload = (
        b'data: {"choices":[{"index":0,"delta":{"role":"assistant","content":"hello fallback"},"finish_reason":null}]}\n\n'
        b'data: {"choices":[{"index":0,"delta":{},"finish_reason":"stop"}]}\n\n'
        b"data: [DONE]\n\n"
    )

    def fake_urlopen(req, timeout=None):
        parsed = json.loads(req.data.decode("utf-8"))
        sent_bodies.append(parsed)
        if "stream_options" in parsed:
            raise _http_error(
                req.full_url,
                400,
                {
                    "error": {
                        "message": "Unrecognized request argument supplied: stream_options",
                        "type": "invalid_request_error",
                        "param": "stream_options",
                        "code": "unknown_parameter",
                    }
                },
            )
        return _FakeHTTPResponse(sse_payload, "text/event-stream")

    monkeypatch.setattr(adapters, "model_urlopen", fake_urlopen)

    url = "https://no-stream-opts.example.test"
    adapter1 = OpenAICompatibleAdapter(base_url=url, model="compat-v1", allow_non_loopback=True)
    res1 = adapter1.chat(
        {"messages": [{"role": "user", "content": "ping"}], "max_tokens": 16, "stream": True},
        task_id="t_so_1",
        timeout_s=5,
    )
    assert res1["text"] == "hello fallback"
    assert len(sent_bodies) == 2
    assert "stream_options" in sent_bodies[0]
    assert "stream_options" not in sent_bodies[1]
    assert actual_usage(res1) == {"input_tokens": None, "output_tokens": None}

    # Second adapter with same (base_url, model) skips stream_options immediately without 400.
    adapter2 = OpenAICompatibleAdapter(base_url=url, model="compat-v1", allow_non_loopback=True)
    res2 = adapter2.chat(
        {"messages": [{"role": "user", "content": "ping 2"}], "max_tokens": 16, "stream": True},
        task_id="t_so_2",
        timeout_s=5,
    )
    assert res2["text"] == "hello fallback"
    assert len(sent_bodies) == 3
    assert "stream_options" not in sent_bodies[2]


def test_t6_infer_stream_ignores_sse_event_id_and_retry_lines(monkeypatch):
    """T6: infer_stream 忽略 SSE 的 event:、id:、retry: 行，正常拼接流式文本。"""
    sse_with_fields = (
        b": keepalive\n"
        b"event: completion\n"
        b"id: msg_001\n"
        b"retry: 1500\n"
        b'data: {"choices":[{"delta":{"content":"part1 "},"finish_reason":null}]}\n\n'
        b"event: completion\n"
        b"id: msg_002\n"
        b'data: {"choices":[{"delta":{"content":"part2"},"finish_reason":"stop"}],"usage":{"prompt_tokens":4,"completion_tokens":2}}\n\n'
        b"event: done\n"
        b"data: [DONE]\n\n"
    )
    monkeypatch.setattr(
        adapters,
        "model_urlopen",
        lambda req, timeout=None: _FakeHTTPResponse(sse_with_fields, "text/event-stream"),
    )
    adapter = OpenAICompatibleAdapter(base_url="http://127.0.0.1:18080", model="sse-model")
    deltas: list[str] = []
    out = adapter.infer_stream(
        prompt="hello",
        max_tokens=16,
        task_id="t_sse_fields",
        timeout_s=5,
        on_delta=deltas.append,
    )
    assert out["text"] == "part1 part2"
    assert deltas == ["part1 ", "part2"]
    assert out["reported_usage"] == {"input_tokens": 4, "output_tokens": 2}


def test_t7_unknown_adapter_error_code_degrades_and_records_original_code():
    """T7: AdapterError 遇到未知 code 降级为 inference_failed，并在合法 [a-z_]{1,64} 时记录 diagnostics['original_code']。"""
    caller_diag = {"http_status": 502}
    err = AdapterError("failed", code="custom_unlisted_code", diagnostics=caller_diag)
    assert err.code == "inference_failed"
    assert err.diagnostics["original_code"] == "custom_unlisted_code"
    assert err.diagnostics["http_status"] == 502
    assert "original_code" not in caller_diag

    # Unsafe / malformed strings are NOT recorded in diagnostics["original_code"].
    unsafe_err = AdapterError("failed", code="Bad Code https://secret.example/x")
    assert unsafe_err.code == "inference_failed"
    assert "original_code" not in unsafe_err.diagnostics

    known_err = AdapterError("timeout", code="inference_timeout")
    assert known_err.code == "inference_timeout"
    assert "original_code" not in known_err.diagnostics


def test_t8_source_scan_all_adapter_error_code_literals_in_runtime_error_codes():
    """T8: 源码扫描所有 AdapterError(..., code='...') 字符串字面量均在 RUNTIME_ERROR_CODES 内（含新增 task_cancelled）。"""
    assert "task_cancelled" in RUNTIME_ERROR_CODES
    pkg_root = pathlib.Path(adapters.__file__).resolve().parents[1]
    invalid_literals: list[tuple[str, int, str]] = []
    checked = 0
    for py_file in sorted(pkg_root.rglob("*.py")):
        tree = ast.parse(py_file.read_text(encoding="utf-8"), filename=str(py_file))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            is_adapter_err = (
                (isinstance(node.func, ast.Name) and node.func.id == "AdapterError")
                or (isinstance(node.func, ast.Attribute) and node.func.attr == "AdapterError")
            )
            if not is_adapter_err:
                continue
            for kw in node.keywords:
                if kw.arg == "code" and isinstance(kw.value, ast.Constant) and isinstance(kw.value.value, str):
                    checked += 1
                    if kw.value.value not in RUNTIME_ERROR_CODES:
                        invalid_literals.append((str(py_file.relative_to(pkg_root)), node.lineno, kw.value.value))
    assert checked >= 30
    assert invalid_literals == []


def test_t9_cancel_nonexistent_or_finished_task_leaves_no_residue_and_bounds_1024(monkeypatch):
    """T9: 对不存在或已结束任务调用 cancel() 不向 _cancelled 留残留，正在启动/运行中的任务可取消且 _cancelled 上限 1024。"""
    adapter = OpenAICompatibleAdapter(base_url="http://127.0.0.1:18080", model="m-cancel")
    assert adapter.cancel("ghost-task") is False
    assert "ghost-task" not in adapter._cancelled

    # Subsequent call with that same task_id succeeds normally instead of hitting stale _cancelled residue.
    monkeypatch.setattr(
        adapter,
        "_json",
        lambda *a, **kw: {
            "choices": [{"message": {"role": "assistant", "content": "alive"}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1},
        },
    )
    res = adapter.infer(prompt="test", max_tokens=8, task_id="ghost-task", timeout_s=5)
    assert res["text"] == "alive"
    assert adapter.cancel("ghost-task") is False
    assert "ghost-task" not in adapter._cancelled

    # Cancelling while in _starting (before _active_responses is populated) succeeds and marks _cancelled.
    def cancel_during_request(*args, task_id="", **kwargs):
        assert adapter.cancel(task_id) is True
        return {
            "choices": [{"message": {"role": "assistant", "content": "discarded"}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 2, "completion_tokens": 2},
        }

    monkeypatch.setattr(adapter, "_json", cancel_during_request)
    with pytest.raises(AdapterError, match="task_cancelled"):
        adapter.chat({"messages": [{"role": "user", "content": "hi"}]}, task_id="starting-task", timeout_s=5)
    assert "starting-task" not in adapter._cancelled

    # Bounded to 1024 entries when 1030 in-flight tasks are cancelled concurrently.
    for i in range(1030):
        tid = f"inflight_{i}"
        adapter._starting.add(tid)
        assert adapter.cancel(tid) is True
    assert len(adapter._cancelled) == 1024
    assert "inflight_0" not in adapter._cancelled
    assert "inflight_5" not in adapter._cancelled
    assert "inflight_6" in adapter._cancelled
    assert "inflight_1029" in adapter._cancelled


def test_t10_native_protocol_cancel_uses_task_cancelled_code_and_bridge_maps_cancellation_reason(monkeypatch):
    """T10: NativeProtocolAdapter 取消返回 code='task_cancelled'；AgentModelBridge 将其与 cancelled() 一致映射为 cancellation_reason。"""
    from rynmesh.llm_package import protocol_adapter

    native = NativeProtocolAdapter(
        protocol="openai_responses",
        base_url="https://api.example.test",
        api_prefix="/v1",
        model="gpt-native",
        allow_non_loopback=True,
    )

    def cancel_on_urlopen(req, timeout=None):
        assert native.cancel("native-cancel-1") is True
        raise OSError("socket closed on cancel")

    monkeypatch.setattr(protocol_adapter, "model_urlopen", cancel_on_urlopen)
    with pytest.raises(AdapterError) as native_exc:
        native.chat({"messages": [{"role": "user", "content": "hello"}]}, task_id="native-cancel-1", timeout_s=5)
    assert native_exc.value.code == "task_cancelled"
    assert str(native_exc.value) == "task_cancelled"

    # Verify AgentModelBridge maps code="task_cancelled" via cancellation_reason(cancelled) even if cancelled() was not yet True,
    # and preserves budget reason (e.g. agent_time_limit) when cancelled.reason is set.
    fake_slot = SimpleNamespace(acquire=lambda blocking=False: True, release=lambda: None)
    import threading

    class _RaiseCancelledAdapter:
        supports_request_deadline = True

        def cancel(self, task_id):
            return True

        def chat(self, chat_body, **opts):
            raise AdapterError("task_cancelled", code="task_cancelled", reported_usage={"input_tokens": 4, "output_tokens": 1})

    manager = SimpleNamespace(
        manifest=SimpleNamespace(timeout_seconds=60),
        adapter=_RaiseCancelledAdapter(),
        _slots=fake_slot,
        _lock=threading.Lock(),
        _running=0,
    )
    target = {
        "id": "local/svc1",
        "service_id": "svc1",
        "model_id": "gpt-native",
        "source": "local_api",
        "source_id": "src1",
        "peer_id": "node_local",
        "available": True,
        "agent_supported": True,
        "streaming": False,
        "max_output_tokens": 1024,
        "context_window": 32000,
    }
    catalog = SimpleNamespace(
        resolve=lambda mid: target,
        store=SimpleNamespace(peer_id="node_local"),
        network="local",
    )
    bridge = AgentModelBridge({"provider_for": lambda sid: manager}, catalog)

    with pytest.raises(AgentError) as bridge_exc_plain:
        bridge.complete("local/svc1", [{"role": "user", "content": "hi"}], [], lambda: False, None)
    assert str(bridge_exc_plain.value) == "agent_cancelled"

    def budget_cancelled():
        return True

    budget_cancelled.reason = "agent_time_limit"
    with pytest.raises(AgentError) as bridge_exc_budget:
        bridge.complete("local/svc1", [{"role": "user", "content": "hi"}], [], budget_cancelled, None)
    assert str(bridge_exc_budget.value) == "agent_time_limit"
