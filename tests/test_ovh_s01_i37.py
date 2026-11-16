"""Tests for S01-I37 Steps 2, 3, 4, 5, 6, 9 (T1, T2, T3, T6, T10, T11)."""
from __future__ import annotations

import io
import json
import urllib.error
from email.message import Message
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from test_agent_default_model import setup as base_cli_setup
from test_agent_runtime import advance, approve, assistant

from rynmesh.agent_runtime import cli_sources
from rynmesh.agent_runtime.cli_sources import CONNECT
from rynmesh.llm_package import adapters, model_settings
from rynmesh.llm_package.adapters import AdapterError
from rynmesh.llm_package.routes import install_llm_routes
from rynmesh.llm_package.sources import SourceAdapter, SourceStore
from rynmesh.services import peer_box
from rynmesh.store import RynmeshStore


def _source_payload(**updates):
    return {
        "name": "ProbeVendor",
        "kind": "api",
        "provider": "openai",
        "base_url": "https://api.vendor.example.test/v1",
        "models": ["gpt-4o"],
        "api_key": "sk-top-secret-never-leak-999",
        **updates,
    }


def test_t1_image_probe_code_ryn_red_and_colour_fallback():
    """T1: probe_adapter_image 使用 RYN-RED/RYN-OTHER 题目；答 RYN-RED 或 crimson 判 supported，答 RYN-OTHER 判 unknown。"""
    seen_prompts: list[str] = []

    class _ImgAdapter:
        def __init__(self, reply_text: str):
            self.reply_text = reply_text

        def chat(self, body, *, task_id, timeout_s):
            text_part = body["messages"][0]["content"][0]["text"]
            seen_prompts.append(text_part)
            return {"text": self.reply_text, "message": {"role": "assistant", "content": self.reply_text}}

    r_code = SourceStore.probe_adapter_image(_ImgAdapter("RYN-RED"), 1024)
    assert r_code == {"image_support": "supported"}
    assert "RYN-RED" in seen_prompts[0] and "RYN-OTHER" in seen_prompts[0]

    r_crimson = SourceStore.probe_adapter_image(_ImgAdapter("The square appears crimson."), 1024)
    assert r_crimson == {"image_support": "supported"}

    r_other = SourceStore.probe_adapter_image(_ImgAdapter("RYN-OTHER"), 1024)
    assert r_other == {"image_support": "unknown"}


def test_t2_tool_probe_missing_id_uses_normalize_call_id_and_wraps_errors():
    """T2: probe_adapter_tools 在模型省略 tool_call id 时用 normalize_call_id 补齐不抛 KeyError；坏 JSON 统一抛 ValueError。"""
    recorded_messages = []

    class _NoIdToolAdapter:
        def chat(self, body, *, task_id, timeout_s):
            recorded_messages.append(body["messages"])
            if body.get("tool_choice") == "required":
                return {
                    "message": {
                        "role": "assistant",
                        "content": None,
                        "tool_calls": [
                            {
                                "type": "function",
                                "function": {"name": "ryn_capability_probe", "arguments": '{"value":"ryn-tool-test"}'},
                            }
                        ],
                    }
                }
            return {"text": "ryn-tool-test-ok", "message": {"role": "assistant", "content": "ryn-tool-test-ok"}}

    res = SourceStore.probe_adapter_tools(_NoIdToolAdapter(), 2048)
    assert res == {"tool_support": "supported"}
    second_turn_msgs = recorded_messages[1]
    gen_id = second_turn_msgs[1]["tool_calls"][0]["id"]
    assert isinstance(gen_id, str) and gen_id
    assert second_turn_msgs[2]["tool_call_id"] == gen_id

    class _BadJsonToolAdapter:
        def chat(self, body, *, task_id, timeout_s):
            return {
                "message": {
                    "role": "assistant",
                    "tool_calls": [
                        {"id": "c1", "type": "function", "function": {"name": "ryn_capability_probe", "arguments": "{not-json"}}
                    ],
                }
            }

    with pytest.raises(ValueError, match="^model did not return a structured client tool call$"):
        SourceStore.probe_adapter_tools(_BadJsonToolAdapter(), 2048)


def test_t3_explicit_4096_max_output_tokens_preserved_and_prepare_dedup(tmp_path):
    """T3: prepare 显式传 max_output_tokens=4096 时写 max_output_tokens_explicit=True，defaults 保留 4096 不被厂商表 16384 覆盖；未显式传时用厂商表。"""
    store = SourceStore(tmp_path, b"unit-test-key")
    explicit_rec = store.prepare(_source_payload(models=["gpt-4o"], max_output_tokens=4096))
    assert explicit_rec["max_output_tokens_explicit"] is True
    assert model_settings.defaults(explicit_rec, "gpt-4o")["max_output_tokens"] == 4096

    implicit_payload = _source_payload(models=["gpt-4o"])
    implicit_payload.pop("max_output_tokens", None)
    implicit_rec = store.prepare(implicit_payload)
    assert implicit_rec["max_output_tokens_explicit"] is False
    assert model_settings.defaults(implicit_rec, "gpt-4o")["max_output_tokens"] == 16384

    # Legacy record without max_output_tokens_explicit preserves legacy heuristic.
    legacy_rec = dict(explicit_rec)
    legacy_rec.pop("max_output_tokens_explicit", None)
    assert model_settings.defaults(legacy_rec, "gpt-4o")["max_output_tokens"] == 16384


def test_t6_cli_connect_selftest_uses_manifest_timeout_and_distinguishes_timeout_vs_empty(tmp_path, monkeypatch):
    """T6: CLISources 连接自检超时取 manifest.timeout_seconds（如 300），超时报 agent_cli_chat_probe_timeout，空输出报 agent_cli_chat_probe_failed。"""
    runtime, model, tools, _key, _rows = base_cli_setup(tmp_path)
    executable = tmp_path / "agy.exe"
    executable.write_bytes(b"cli-bin")
    monkeypatch.setattr(cli_sources.CLIAgentAdapter, "_command_path", lambda self: str(executable))

    seen_timeouts: list[float] = []
    mode = {"behavior": "ok"}

    class _FakeCLIAdapter:
        def infer(self, **kwargs):
            seen_timeouts.append(kwargs["timeout_s"])
            if mode["behavior"] == "timeout":
                raise AdapterError("cli timed out", code="inference_timeout")
            if mode["behavior"] == "empty":
                return {"text": "   ", "usage": {"input_tokens": 1, "output_tokens": 0}}
            return {"text": "RYNMESH SELF TEST OK", "usage": {"input_tokens": 3, "output_tokens": 4}}

        def cancel(self, task_id):
            return True

    fake_adapter = _FakeCLIAdapter()
    manifest = SimpleNamespace(package_id="antigravity-cli", timeout_seconds=300)
    manager = SimpleNamespace(adapter=fake_adapter, manifest=manifest)
    tools.services.update(
        active_cli_managers=lambda: {"antigravity_cli": manager} if tools.cli_sources.read().get("antigravity_cli") else {},
        cli_services=lambda: {"services": [{"kind": "antigravity_cli", "installed": True, "configured": False}], "personal_space_ready": True},
    )

    def fake_connect(**kwargs):
        kwargs["self_test_fn"](manifest)
        return {"manifest": str(tmp_path / "manifest.json")}

    monkeypatch.setattr(cli_sources, "connect_cli", fake_connect)
    monkeypatch.setattr(cli_sources, "adapter_from_manifest", lambda _m: fake_adapter)

    # 1) Timeout -> agent_cli_chat_probe_timeout, and timeout_s == 300 from manifest.timeout_seconds
    mode["behavior"] = "timeout"
    model.responses.extend([assistant(name=CONNECT, args={"kind": "antigravity_cli"}), assistant("Timed out.")])
    t1 = runtime.create({"message": "Connect CLI timeout case"})
    advance(runtime, 2)
    approve(runtime, t1["id"])
    advance(runtime, 2)
    raw1 = runtime.store.read()["tasks"][t1["id"]]
    tool_msg_1 = json.loads([m["content"] for m in raw1["messages"] if m["role"] == "tool"][-1])
    assert tool_msg_1["error"] == "agent_cli_chat_probe_timeout"
    assert seen_timeouts[-1] == 300.0

    # 2) Empty output -> agent_cli_chat_probe_failed
    mode["behavior"] = "empty"
    model.responses.extend([assistant(name=CONNECT, args={"kind": "antigravity_cli"}), assistant("Empty probe.")])
    t2 = runtime.create({"message": "Connect CLI empty case"})
    advance(runtime, 2)
    approve(runtime, t2["id"])
    advance(runtime, 2)
    raw2 = runtime.store.read()["tasks"][t2["id"]]
    tool_msg_2 = json.loads([m["content"] for m in raw2["messages"] if m["role"] == "tool"][-1])
    assert tool_msg_2["error"] == "agent_cli_chat_probe_failed"

    # 3) Success with custom manifest.timeout_seconds == 240
    manifest.timeout_seconds = 240
    mode["behavior"] = "ok"
    model.responses.extend([assistant(name=CONNECT, args={"kind": "antigravity_cli"}), assistant("Connected.")])
    t3 = runtime.create({"message": "Connect CLI ok case"})
    advance(runtime, 2)
    approve(runtime, t3["id"])
    advance(runtime, 2)
    raw3 = runtime.store.read()["tasks"][t3["id"]]
    assert raw3["receipts"][-1]["result"]["chat_probe"] == "passed"
    assert seen_timeouts[-1] == 240.0


def _make_routes_client(tmp_path):
    home = tmp_path / "node"
    store = RynmeshStore(home=home, network_dir=tmp_path / "network")
    app = FastAPI()
    install_llm_routes(
        app,
        store=store,
        home=home,
        messaging_key=peer_box.load_or_create_messaging_key(home / "messaging.x25519"),
        resolve_endpoint=lambda _: "",
        resolve_pubkey=lambda _: "",
    )
    return TestClient(app, base_url="http://127.0.0.1")


def test_t10_source_test_and_models_routes_expose_closed_error_codes_and_network_reason(tmp_path, monkeypatch):
    """T10: /sources/{id}/test 与 /sources/models 在 401 -> upstream_request_rejected、429 -> 额度类码、超时 -> network_reason=timeout。"""
    behavior = {"kind": "401"}

    def fake_urlopen(req, timeout=None):
        hdrs = Message()
        hdrs["Content-Type"] = "application/json"
        if behavior["kind"] == "401":
            body = json.dumps({"error": {"message": "Invalid API key sk-top-secret-never-leak-999", "type": "authentication_error", "code": "invalid_api_key"}}).encode()
            raise urllib.error.HTTPError(req.full_url, 401, "Unauthorized", hdrs, io.BytesIO(body))
        if behavior["kind"] == "429":
            body = json.dumps({"error": {"message": "Quota exceeded for https://api.vendor.example.test/v1", "type": "insufficient_quota", "code": "insufficient_quota"}}).encode()
            raise urllib.error.HTTPError(req.full_url, 429, "Too Many Requests", hdrs, io.BytesIO(body))
        raise TimeoutError("socket timed out connecting to https://api.vendor.example.test/v1")

    monkeypatch.setattr(adapters, "model_urlopen", fake_urlopen)

    with _make_routes_client(tmp_path) as client:
        saved = client.put("/api/local/llm/sources/new", json=_source_payload()).json()
        sid = saved["id"]

        # 401 -> upstream_request_rejected, http_status=401
        behavior["kind"] = "401"
        r401 = client.post(f"/api/local/llm/sources/{sid}/test")
        assert r401.status_code == 400
        body401 = r401.json()
        assert isinstance(body401["detail"], str)
        assert body401["error_code"] == "upstream_request_rejected"
        assert body401["http_status"] == 401
        assert r401.headers.get("X-Ryn-Error-Code") == "upstream_request_rejected"

        m401 = client.post("/api/local/llm/sources/models", json=_source_payload())
        assert m401.status_code == 400
        assert m401.json()["error_code"] == "upstream_request_rejected"

        # 429 -> quota-class code (model_daily_quota_exhausted), http_status=429, network_reason=quota
        behavior["kind"] = "429"
        r429 = client.post(f"/api/local/llm/sources/{sid}/test")
        assert r429.status_code == 400
        body429 = r429.json()
        assert body429["error_code"] == "model_daily_quota_exhausted"
        assert body429["network_reason"] == "quota"
        assert body429["http_status"] == 429

        # Timeout -> error_code=inference_timeout, network_reason=timeout
        behavior["kind"] = "timeout"
        r_to = client.post(f"/api/local/llm/sources/{sid}/test")
        assert r_to.status_code == 400
        body_to = r_to.json()
        assert body_to["error_code"] == "inference_timeout"
        assert body_to["network_reason"] == "timeout"

        # ValueError (invalid URL / missing key) -> source_request_invalid
        r_inv = client.post("/api/local/llm/sources/models", json=_source_payload(base_url="http://not-https.example/v1"))
        assert r_inv.status_code == 400
        assert r_inv.json()["error_code"] == "source_request_invalid"


def test_t11_source_error_responses_never_leak_upstream_body_api_key_or_url(tmp_path, monkeypatch):
    """T11: 测试连接与读取模型列表错误响应绝不包含上游正文、API 密钥或 URL。"""
    secret_body = "UPSTREAM_RAW_SECRET_TRACE_987654"
    secret_key = "sk-top-secret-never-leak-999"
    secret_url = "https://api.vendor.example.test/v1"

    def fake_urlopen(req, timeout=None):
        hdrs = Message()
        hdrs["Content-Type"] = "application/json"
        raw = json.dumps({"error": {"message": f"{secret_body} key={secret_key} url={secret_url}", "code": "invalid_api_key"}}).encode()
        raise urllib.error.HTTPError(req.full_url, 401, "Unauthorized", hdrs, io.BytesIO(raw))

    monkeypatch.setattr(adapters, "model_urlopen", fake_urlopen)

    with _make_routes_client(tmp_path) as client:
        saved = client.put("/api/local/llm/sources/new", json=_source_payload()).json()
        sid = saved["id"]
        resp_test = client.post(f"/api/local/llm/sources/{sid}/test")
        resp_models = client.post("/api/local/llm/sources/models", json=_source_payload())

        for resp in (resp_test, resp_models):
            assert resp.status_code == 400
            assert resp.json().get("error_code") == "upstream_request_rejected"
            serialized = resp.text + " " + json.dumps(dict(resp.headers))
            assert secret_body not in serialized
            assert secret_key not in serialized
            assert secret_url not in serialized
