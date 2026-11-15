"""Tests for S01-I13 (T10..T12): local model context_window & max_output_tokens limits."""

from __future__ import annotations

import json
import struct
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import pytest

import rynmesh.llm_package.catalog as llm_catalog
import rynmesh.llm_package.gguf_import as gguf_import
import rynmesh.llm_package.lifecycle as llm_lifecycle
from rynmesh.llm_package import native_model as nm
from rynmesh.llm_package.lifecycle import connect_local_api
from rynmesh.llm_package.manifest import LLMPackageManifest, load_manifest


def _make_local_api_server(models_payload: dict[str, Any], props_payload: dict[str, Any] | None = None):
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path == "/v1/models":
                self._send(200, models_payload)
            elif self.path == "/props" and props_payload is not None:
                self._send(200, props_payload)
            else:
                self.send_error(404)

        def do_POST(self):
            if self.path != "/v1/chat/completions":
                self.send_error(404)
                return
            length = int(self.headers.get("content-length", "0"))
            body = json.loads(self.rfile.read(length))
            if body.get("stream"):
                raw = b'data: {"choices":[{"delta":{"content":"ok"},"finish_reason":"stop"}]}\n\ndata: [DONE]\n\n'
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)
                return
            self._send(200, {
                "choices": [{"message": {"content": "RYNMESH SELF TEST OK"}}],
                "usage": {"prompt_tokens": 5, "completion_tokens": 4},
            })

        def log_message(self, *_args):
            return None

        def _send(self, status: int, data: dict[str, Any]):
            raw = json.dumps(data).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, thread, f"http://127.0.0.1:{server.server_port}"


def _build_gguf(path: Path, *, arch: str = "qwen3", context_length: int = 131072, name: str = "Qwen-128K") -> Path:
    def gstr(value: str) -> bytes:
        encoded = value.encode("utf-8")
        return struct.pack("<Q", len(encoded)) + encoded

    entries = {
        "general.name": (8, gstr(name)),
        "general.architecture": (8, gstr(arch)),
        "split.count": (4, struct.pack("<I", 1)),
        f"{arch}.context_length": (4, struct.pack("<I", context_length)),
    }
    blob = b"GGUF" + struct.pack("<IQQ", 3, 1, len(entries))
    for key, (kind, raw_val) in entries.items():
        blob += gstr(key) + struct.pack("<I", kind) + raw_val
    blob += gstr("blk.0.weight") + struct.pack("<IQIQ", 1, 32, 0, 0)
    blob += bytes((-len(blob)) % 32) + bytes(128)
    path.write_bytes(blob)
    return path


def test_t10_connect_local_api_reads_v1_models_and_llama_cpp_props(tmp_path):
    # 1. /v1/models provides context_window and max_output_tokens
    srv1, th1, url1 = _make_local_api_server(
        {"object": "list", "data": [{"id": "qwen-api", "context_window": 32768, "max_output_tokens": 6144}]}
    )
    try:
        res1 = connect_local_api(base_url=url1, package_id="pkg-models-cw", alias="Qwen API", root=tmp_path)
        m1 = load_manifest(res1["manifest"])
        assert m1.context_window == 32768
        assert m1.max_output_tokens == 6144
    finally:
        srv1.shutdown()
        th1.join(timeout=2)

    # 2. /v1/models provides vLLM-style max_model_len (and no max_output_tokens -> min(window//4, 8192))
    srv2, th2, url2 = _make_local_api_server(
        {"object": "list", "data": [{"id": "vllm-model", "max_model_len": 16384}]}
    )
    try:
        res2 = connect_local_api(base_url=url2, package_id="pkg-vllm", alias="vLLM", root=tmp_path)
        m2 = load_manifest(res2["manifest"])
        assert m2.context_window == 16384
        assert m2.max_output_tokens == 4096
    finally:
        srv2.shutdown()
        th2.join(timeout=2)

    # 3. /v1/models has no window, llama.cpp /props provides default_generation_settings.n_ctx
    srv3, th3, url3 = _make_local_api_server(
        {"object": "list", "data": [{"id": "llama-local"}]},
        props_payload={"default_generation_settings": {"n_ctx": 24576}},
    )
    try:
        res3 = connect_local_api(base_url=url3, package_id="pkg-llamacpp", alias="LlamaCpp", root=tmp_path)
        m3 = load_manifest(res3["manifest"])
        assert m3.context_window == 24576
        assert m3.max_output_tokens == 6144
    finally:
        srv3.shutdown()
        th3.join(timeout=2)

    # 4. Neither /v1/models nor /props reports window -> falls back to manifest defaults (8192 / 2048)
    srv4, th4, url4 = _make_local_api_server({"object": "list", "data": [{"id": "minimal-local"}]})
    try:
        res4 = connect_local_api(base_url=url4, package_id="pkg-fallback", alias="Minimal", root=tmp_path)
        m4 = load_manifest(res4["manifest"])
        assert m4.context_window == 8192
        assert m4.max_output_tokens == 2048
    finally:
        srv4.shutdown()
        th4.join(timeout=2)


def test_t11_gguf_128k_declared_context_is_recorded_and_capped_by_memory(tmp_path, monkeypatch):
    gguf_path = _build_gguf(tmp_path / "qwen-128k.gguf", context_length=131072)
    inspected = gguf_import.inspect_model(gguf_path)
    # gguf_import must preserve declared 131072 instead of clamping to 4096.
    assert inspected["context_window"] == 131072

    # Managed catalog profiles report real model window (capped at 32768).
    for name in ("light", "balanced", "quality"):
        assert llm_catalog.profile_by_name(name).context_window == 32768

    # Constrained memory (2 GiB available RAM, no GPU): NativeModel caps runtime window below 32768.
    monkeypatch.setattr(nm, "platform_key", lambda: "windows-cpu")
    monkeypatch.setattr(nm, "_memory", lambda: (4096, 2048))
    monkeypatch.setattr(nm, "_nvidia", lambda: ([], ""))
    low_model = nm.NativeModel(tmp_path / "home-low")
    monkeypatch.setattr(low_model, "_engine", lambda _b: Path("fake-llama-server"))
    captured_low_ctx: list[int] = []

    def fake_start_low(_exe, _backend):
        captured_low_ctx.append(low_model.manifest.context_window)

    monkeypatch.setattr(low_model, "_start", fake_start_low)
    low_model.action("import", source=str(gguf_path))
    low_model.worker.join(timeout=5)
    assert low_model.state == "running"
    assert 4096 < low_model.manifest.context_window < 32768
    assert captured_low_ctx == [low_model.manifest.context_window]
    assert low_model.manifest.max_output_tokens == min(low_model.manifest.context_window // 4, 8192)

    # Ample memory (64 GiB available RAM): NativeModel caps 128K GGUF at 32768 and output at 8192.
    monkeypatch.setattr(nm, "_memory", lambda: (65536, 32768))
    high_model = nm.NativeModel(tmp_path / "home-high")
    monkeypatch.setattr(high_model, "_engine", lambda _b: Path("fake-llama-server"))
    monkeypatch.setattr(high_model, "_start", lambda _e, _b: None)
    high_model.action("import", source=str(gguf_path))
    high_model.worker.join(timeout=5)
    assert high_model.state == "running"
    assert high_model.manifest.context_window == 32768
    assert high_model.manifest.max_output_tokens == 8192


def test_t12_manifest_default_context_window_and_max_output_tokens():
    manifest = LLMPackageManifest(
        package_id="default-limits-check",
        mode="managed",
        public_model_alias="local-default",
    )
    assert manifest.context_window == 8192
    assert manifest.max_output_tokens == 2048
    manifest.validate()
