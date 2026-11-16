"""Regression tests for S01-I34-fix: omitted max_tokens caps reservation to remaining project key quota."""

from __future__ import annotations

import copy
import threading
from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.testclient import TestClient

from rynmesh.llm_package.api import install_inference_api
from rynmesh.llm_package.manifest import LLMPackageManifest


class _RecordingAdapter:
    def __init__(self) -> None:
        self.bodies: list[dict] = []

    def health(self):
        return {"ok": True}

    def chat(self, body, *, task_id, timeout_s, on_event=None):
        self.bodies.append(copy.deepcopy(body))
        return {
            "text": "ok",
            "message": {"role": "assistant", "content": "ok"},
            "finish_reason": "stop",
            "input_tokens": 5,
            "output_tokens": 3,
            "duration_ms": 1,
        }

    def cancel(self, task_id):
        return True


def test_s01_i34_fix_omitted_max_tokens_caps_to_remaining_project_quota_and_rejects_when_exhausted(tmp_path):
    adapter = _RecordingAdapter()
    manifest = LLMPackageManifest(
        package_id="glm-large",
        mode="openai_compatible",
        public_model_alias="glm-5.3-flash",
        base_url="http://127.0.0.1:1",
        max_output_tokens=128000,
        context_window=1000000,
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
    with TestClient(app, base_url="http://127.0.0.1") as client:
        # 1. Default API key (100_000 limit) < model max_output_tokens (128_000): omitted max_tokens caps to 100_000
        default_key = client.post("/api/local/llm/api-keys", json={"name": "default"}).json()
        r1 = client.post(
            "/v1/chat/completions",
            headers={"Authorization": "Bearer " + default_key["key"]},
            json={"model": "local/glm-large", "messages": [{"role": "user", "content": "hello"}]},
        )
        assert r1.status_code == 200, r1.text
        assert adapter.bodies[-1]["max_tokens"] == 100000

        # Second call on same key (remaining 99_997) also succeeds and caps to 99_997
        r2 = client.post(
            "/v1/responses",
            headers={"Authorization": "Bearer " + default_key["key"]},
            json={"model": "local/glm-large", "input": "hello again"},
        )
        assert r2.status_code == 200, r2.text
        assert adapter.bodies[-1]["max_tokens"] == 99997

        # 2. Small key (limit 3): first call reserves 3, uses 3 -> remaining 0 -> next call gets 429
        tiny_key = client.post("/api/local/llm/api-keys", json={"name": "tiny", "output_token_limit": 3}).json()
        r_tiny_1 = client.post(
            "/v1/chat/completions",
            headers={"Authorization": "Bearer " + tiny_key["key"]},
            json={"model": "local/glm-large", "messages": [{"role": "user", "content": "hi"}]},
        )
        assert r_tiny_1.status_code == 200, r_tiny_1.text
        assert adapter.bodies[-1]["max_tokens"] == 3

        r_tiny_2 = client.post(
            "/v1/chat/completions",
            headers={"Authorization": "Bearer " + tiny_key["key"]},
            json={"model": "local/glm-large", "messages": [{"role": "user", "content": "hi"}]},
        )
        assert r_tiny_2.status_code == 429
        assert r_tiny_2.json()["error"]["message"] == "project_output_token_limit"
