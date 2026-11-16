"""Tests for S01-I30: mirrors, Range resume & retry, Chinese failure classification, Docker pull, and GGUF picker check."""

from __future__ import annotations

import hashlib
import io
import os
import socket
import ssl
import subprocess
import tarfile
import urllib.error
import urllib.request
import zipfile
from pathlib import Path

import pytest

import rynmesh
from rynmesh.llm_package import (
    catalog as llm_catalog,
    model_download,
    native_model,
    runtime_docker,
    runtime_native_install,
)
from rynmesh.llm_package.errors import LifecycleError
from rynmesh.llm_package.gguf_import import NativeModelError
from rynmesh.llm_package.manifest import LLMPackageManifest, fingerprint_file
from rynmesh.settings_store import SettingsStore


def _make_runtime_archive() -> tuple[str, bytes, str]:
    buf = io.BytesIO()
    if os.name == "nt":
        name = "llama-test-win.zip"
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
            zf.writestr("build/bin/llama-server.exe", b"MZ fake server binary")
    else:
        name = "llama-test-unix.tar.gz"
        with tarfile.open(fileobj=buf, mode="w:gz") as tf:
            payload = b"#!/bin/sh\nexit 0\n"
            info = tarfile.TarInfo("build/bin/llama-server")
            info.size = len(payload)
            info.mode = 0o755
            tf.addfile(info, io.BytesIO(payload))
    raw = buf.getvalue()
    return name, raw, hashlib.sha256(raw).hexdigest()


class _FakeHTTPResponse:
    def __init__(self, body: bytes, *, status: int = 200, headers: dict[str, str] | None = None, fail_after: int | None = None, error: BaseException | None = None):
        self._body = body
        self._pos = 0
        self.status = status
        base_headers = {"Content-Length": str(len(body))}
        if headers:
            base_headers.update(headers)
        self.headers = base_headers
        self._fail_after = fail_after
        self._error = error

    def read(self, size: int = -1) -> bytes:
        if self._fail_after is not None and self._pos >= self._fail_after:
            raise self._error or ConnectionResetError("connection reset mid-stream")
        if self._pos >= len(self._body):
            return b""
        limit = len(self._body) - self._pos if size < 0 else size
        if self._fail_after is not None:
            limit = min(limit, max(1, self._fail_after - self._pos))
        chunk = self._body[self._pos : self._pos + limit]
        self._pos += len(chunk)
        return chunk

    def close(self) -> None:
        self.closed = True

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False


def test_t1_mirror_prefix_replaces_host_and_rejects_http(tmp_path, monkeypatch):
    home = tmp_path / "node-home"
    home.mkdir()
    monkeypatch.setenv("RYNMESH_HOME", str(home))
    monkeypatch.delenv("RYNMESH_HF_MIRROR", raising=False)
    monkeypatch.delenv("RYNMESH_DOWNLOAD_MIRROR", raising=False)
    monkeypatch.delenv("RYNMESH_DOCKER_REGISTRY_MIRROR", raising=False)

    # Plain HTTP mirror is rejected by owner settings, catalog, runtime installer, native model, and Docker mirror.
    store = SettingsStore(home / "settings.json")
    with pytest.raises(ValueError):
        store.patch({"llm_download_mirror": "http://mirror.example.com/prefix"})
    with pytest.raises(ValueError):
        llm_catalog._pinned_url("Qwen/Test", "deadbeef", "m.gguf", mirror="http://mirror.example.com")

    monkeypatch.setenv("RYNMESH_DOWNLOAD_MIRROR", "http://mirror.example.com")
    archive_name, archive_bytes, archive_sha = _make_runtime_archive()
    monkeypatch.setattr(runtime_native_install, "asset", lambda: (archive_name, archive_sha, len(archive_bytes)))
    with pytest.raises(LifecycleError):
        runtime_native_install.download(home / "llm")
    with pytest.raises(NativeModelError):
        native_model._engine_base_url(home)

    monkeypatch.setenv("RYNMESH_DOCKER_REGISTRY_MIRROR", "http://ghcr.mirror.example.com")
    with pytest.raises(LifecycleError):
        runtime_docker._pinned_runtime_image(None)
    monkeypatch.delenv("RYNMESH_DOCKER_REGISTRY_MIRROR", raising=False)
    monkeypatch.delenv("RYNMESH_DOWNLOAD_MIRROR", raising=False)

    # Owner setting `llm_download_mirror` with HTTPS host + path prefix applies to all 3 download URL builders.
    updated = store.patch({"llm_download_mirror": "https://mirror.example.com/ryn-cache/"})
    assert updated["llm_download_mirror"] == "https://mirror.example.com/ryn-cache"

    pinned_hf = llm_catalog._pinned_url("Qwen/Qwen2.5-0.5B-Instruct-GGUF", "872f8a96", "qwen.gguf")
    assert pinned_hf == "https://mirror.example.com/ryn-cache/Qwen/Qwen2.5-0.5B-Instruct-GGUF/resolve/872f8a96/qwen.gguf"
    assert llm_catalog.profile_by_name("light").url.startswith("https://mirror.example.com/ryn-cache/Qwen/")

    assert native_model._engine_base_url(home) == (
        f"https://mirror.example.com/ryn-cache/ggml-org/llama.cpp/releases/download/{native_model.ENGINE_VERSION}/"
    )

    seen_runtime_urls: list[str] = []

    class _Opener:
        def open(self, request, timeout=0):
            seen_runtime_urls.append(request.full_url)
            return _FakeHTTPResponse(archive_bytes)

    monkeypatch.setattr(runtime_native_install, "build_https_only_opener", lambda: _Opener())
    runtime_native_install.download(home / "llm")
    assert seen_runtime_urls == [
        f"https://mirror.example.com/ryn-cache/ggml-org/llama.cpp/releases/download/{runtime_native_install.RUNTIME_RELEASE}/{archive_name}"
    ]

    # Env var RYNMESH_DOWNLOAD_MIRROR overrides and works identically.
    monkeypatch.setenv("RYNMESH_DOWNLOAD_MIRROR", "https://env-mirror.example.com/sub")
    assert llm_catalog._pinned_url("Qwen/Test", "rev1", "a.gguf") == "https://env-mirror.example.com/sub/Qwen/Test/resolve/rev1/a.gguf"
    assert runtime_native_install.runtime_base_url(home / "llm") == (
        f"https://env-mirror.example.com/sub/ggml-org/llama.cpp/releases/download/{runtime_native_install.RUNTIME_RELEASE}/"
    )
    assert native_model._engine_base_url(home) == (
        f"https://env-mirror.example.com/sub/ggml-org/llama.cpp/releases/download/{native_model.ENGINE_VERSION}/"
    )

    # Docker registry mirror replaces only `ghcr.io` host while preserving `@sha256:` digest.
    monkeypatch.setenv("RYNMESH_DOCKER_REGISTRY_MIRROR", "ghcr.mirror.example.com")
    mirrored_image = runtime_docker._pinned_runtime_image(None)
    expected_digest = runtime_docker.DEFAULT_IMAGE.split("@", 1)[1]
    assert mirrored_image == f"ghcr.mirror.example.com/ggml-org/llama.cpp:server@{expected_digest}"


def test_t2_interrupted_download_resumes_via_range_and_uses_version_ua(tmp_path, monkeypatch):
    expected_ua = f"Rynmesh/{rynmesh.__version__}"
    payload = b"0123456789abcdef" * 512
    sha = hashlib.sha256(payload).hexdigest()
    half = len(payload) // 2

    # 1. runtime_native_install._fetch: keeps .part on mid-stream disconnect, resumes with Range, uses 60s timeout & version UA.
    rt_calls: list[tuple[str | None, str, float]] = []
    rt_sleeps: list[float] = []
    monkeypatch.setattr("time.sleep", lambda s: rt_sleeps.append(s))

    class _RtOpener:
        def open(self, request, timeout=0):
            rng = request.get_header("Range")
            ua = request.get_header("User-agent") or ""
            rt_calls.append((rng, ua, timeout))
            if len(rt_calls) == 1:
                return _FakeHTTPResponse(payload, status=200, fail_after=half, error=ConnectionResetError("dropped"))
            assert rng == f"bytes={half}-"
            return _FakeHTTPResponse(
                payload[half:],
                status=206,
                headers={"Content-Range": f"bytes {half}-{len(payload) - 1}/{len(payload)}"},
            )

    monkeypatch.setattr(runtime_native_install, "build_https_only_opener", lambda: _RtOpener())
    rt_dest = tmp_path / "runtime" / "llama-bin.zip"
    runtime_native_install._fetch("https://example.com/llama-bin.zip", rt_dest, sha, len(payload))
    assert rt_dest.read_bytes() == payload
    assert rt_calls == [(None, expected_ua, 60), (f"bytes={half}-", expected_ua, 60)]
    assert rt_sleeps == [2.0]

    # 2. model_download.download: mid-stream OSError keeps .part, retries with Range, uses version UA.
    md_calls: list[tuple[str | None, str]] = []

    def _md_urlopen(request, timeout=60):
        rng = request.get_header("Range")
        ua = request.get_header("User-agent") or ""
        md_calls.append((rng, ua))
        if len(md_calls) == 1:
            return _FakeHTTPResponse(payload, status=200, fail_after=half, error=ConnectionResetError("dropped"))
        return _FakeHTTPResponse(
            payload[half:],
            status=206,
            headers={"Content-Range": f"bytes {half}-{len(payload) - 1}/{len(payload)}"},
        )

    monkeypatch.setattr(model_download, "_urlopen", _md_urlopen)
    md_dest = tmp_path / "models" / "m.gguf"
    assert model_download.download("https://example.com/m.gguf", md_dest, sha, size_bytes=len(payload)) == sha
    assert md_dest.read_bytes() == payload
    assert md_calls == [(None, expected_ua), (f"bytes={half}-", expected_ua)]

    # 3. native_model.download: mid-stream disconnect keeps .part, resumes with Range, uses version UA.
    nm_calls: list[tuple[str | None, str]] = []

    def _nm_urlopen(request, timeout=60):
        rng = request.get_header("Range")
        ua = request.get_header("User-agent") or ""
        nm_calls.append((rng, ua))
        if len(nm_calls) == 1:
            return _FakeHTTPResponse(payload, status=200, fail_after=half, error=ConnectionResetError("dropped"))
        return _FakeHTTPResponse(
            payload[half:],
            status=206,
            headers={"Content-Range": f"bytes {half}-{len(payload) - 1}/{len(payload)}"},
        )

    monkeypatch.setattr(native_model.urllib.request, "urlopen", _nm_urlopen)
    nm_dest = tmp_path / "downloads" / "engine.zip"
    native_model.download("https://example.com/engine.zip", nm_dest, len(payload), sha, lambda *_: None, lambda: False)
    assert nm_dest.read_bytes() == payload
    assert nm_calls == [(None, expected_ua), (f"bytes={half}-", expected_ua)]


def test_t3_http_429_with_retry_after_waits_and_retries(tmp_path, monkeypatch):
    payload = b"rate-limited-payload" * 32
    sha = hashlib.sha256(payload).hexdigest()

    # model_download.download honors Retry-After on 429
    md_sleeps: list[float] = []
    md_attempts = 0
    monkeypatch.setattr("time.sleep", lambda s: md_sleeps.append(s))

    def _md_open(request, timeout=60):
        nonlocal md_attempts
        md_attempts += 1
        if md_attempts == 1:
            raise urllib.error.HTTPError(request.full_url, 429, "Too Many Requests", {"Retry-After": "7"}, None)
        return _FakeHTTPResponse(payload, status=200)

    monkeypatch.setattr(model_download, "_urlopen", _md_open)
    dest = tmp_path / "m429.gguf"
    assert model_download.download("https://example.com/m429.gguf", dest, sha, size_bytes=len(payload)) == sha
    assert md_attempts == 2
    assert md_sleeps == [7.0]

    # runtime_native_install._fetch honors Retry-After on 429
    rt_sleeps: list[float] = []
    rt_attempts = 0
    monkeypatch.setattr("time.sleep", lambda s: rt_sleeps.append(s))

    class _RtOpener429:
        def open(self, request, timeout=0):
            nonlocal rt_attempts
            rt_attempts += 1
            if rt_attempts == 1:
                raise urllib.error.HTTPError(request.full_url, 429, "Too Many Requests", {"Retry-After": "5"}, None)
            return _FakeHTTPResponse(payload, status=200)

    monkeypatch.setattr(runtime_native_install, "build_https_only_opener", lambda: _RtOpener429())
    rt_dest = tmp_path / "rt429.zip"
    runtime_native_install._fetch("https://example.com/rt429.zip", rt_dest, sha, len(payload))
    assert rt_attempts == 2
    assert rt_sleeps == [5.0]


def test_t4_http_4xx_fails_immediately_without_retry(tmp_path, monkeypatch):
    payload = b"unused"
    sha = hashlib.sha256(payload).hexdigest()

    md_sleeps: list[float] = []
    md_calls = 0
    monkeypatch.setattr("time.sleep", lambda s: md_sleeps.append(s))

    def _md_403(request, timeout=60):
        nonlocal md_calls
        md_calls += 1
        raise urllib.error.HTTPError(request.full_url, 403, "Forbidden", {}, None)

    monkeypatch.setattr(model_download, "_urlopen", _md_403)
    with pytest.raises(LifecycleError) as md_err:
        model_download.download("https://example.com/forbidden.gguf", tmp_path / "forbidden.gguf", sha, size_bytes=6)
    assert md_calls == 1
    assert md_sleeps == []
    assert "HTTP 403" in str(md_err.value)

    rt_sleeps: list[float] = []
    rt_calls = 0
    monkeypatch.setattr("time.sleep", lambda s: rt_sleeps.append(s))

    class _RtOpener404:
        def open(self, request, timeout=0):
            nonlocal rt_calls
            rt_calls += 1
            raise urllib.error.HTTPError(request.full_url, 404, "Not Found", {}, None)

    monkeypatch.setattr(runtime_native_install, "build_https_only_opener", lambda: _RtOpener404())
    with pytest.raises(LifecycleError) as rt_err:
        runtime_native_install._fetch("https://example.com/missing.zip", tmp_path / "missing.zip", sha, 6)
    assert rt_calls == 1
    assert rt_sleeps == []
    assert "HTTP 404" in str(rt_err.value)

    nm_sleeps: list[float] = []
    nm_calls = 0
    monkeypatch.setattr("time.sleep", lambda s: nm_sleeps.append(s))

    def _nm_403(request, timeout=60):
        nonlocal nm_calls
        nm_calls += 1
        raise urllib.error.HTTPError(request.full_url, 403, "Forbidden", {}, None)

    monkeypatch.setattr(native_model.urllib.request, "urlopen", _nm_403)
    with pytest.raises(NativeModelError) as nm_err:
        native_model.download("https://example.com/engine.zip", tmp_path / "downloads" / "engine.zip", 6, sha, lambda *_: None, lambda: False)
    assert nm_calls == 1
    assert nm_sleeps == []
    assert "HTTP 403" in str(nm_err.value)


def test_t5_failure_messages_include_closed_reason_and_manual_placement_and_preplaced_files_accepted(tmp_path, monkeypatch):
    monkeypatch.setattr("time.sleep", lambda _s: None)

    sha = "ab" * 32
    cases = [
        (urllib.error.URLError(socket.gaierror(8, "secret-internal-host")), "下载失败：DNS 解析失败"),
        (urllib.error.URLError(ConnectionResetError("secret-peer-reset")), "下载失败：连接被重置"),
        (urllib.error.URLError(ssl.SSLCertVerificationError(1, "secret-cert-detail")), "下载失败：证书校验失败"),
        (urllib.error.URLError(TimeoutError("secret-timeout-detail")), "下载失败：超时"),
        (urllib.error.HTTPError("https://secret.example/m.gguf", 403, "Forbidden", {}, None), "下载失败：HTTP 403"),
    ]

    model_dir = tmp_path / "models" / "local-small"
    for exc, expected_label in cases:
        monkeypatch.setattr(model_download, "_urlopen", lambda _req, timeout=60, _e=exc: (_ for _ in ()).throw(_e))
        with pytest.raises(LifecycleError) as caught:
            model_download.download("https://secret.example/light.gguf", model_dir / "light.gguf", sha, size_bytes=128)
        msg = str(caught.value)
        assert expected_label in msg
        assert "light.gguf" in msg
        assert sha in msg
        assert str(model_dir) in msg
        assert "secret" not in msg

    # Verify native_model failure message points to root/downloads/<filename>
    nm_downloads_dir = tmp_path / "native-root" / "downloads"
    monkeypatch.setattr(
        native_model.urllib.request,
        "urlopen",
        lambda _req, timeout=60: (_ for _ in ()).throw(urllib.error.URLError(TimeoutError("secret-timeout"))),
    )
    with pytest.raises(NativeModelError) as nm_caught:
        native_model.download(
            "https://secret.example/llama-engine.zip",
            nm_downloads_dir / "llama-engine.zip",
            256,
            sha,
            lambda *_: None,
            lambda: False,
        )
    nm_msg = str(nm_caught.value)
    assert "下载失败：超时" in nm_msg
    assert "llama-engine.zip" in nm_msg
    assert sha in nm_msg
    assert str(nm_downloads_dir) in nm_msg
    assert "secret" not in nm_msg

    # Verify runtime_native_install failure message points to base/runtime/<filename>,
    # AND pre-placing a verified archive at base/runtime/<filename> skips network download completely.
    archive_name, archive_bytes, archive_sha = _make_runtime_archive()
    base = tmp_path / "llm-home"
    monkeypatch.setattr(runtime_native_install, "asset", lambda: (archive_name, archive_sha, len(archive_bytes)))

    class _FailingOpener:
        def open(self, _request, timeout=0):
            raise urllib.error.URLError(socket.gaierror(8, "secret-dns"))

    monkeypatch.setattr(runtime_native_install, "build_https_only_opener", lambda: _FailingOpener())
    with pytest.raises(LifecycleError) as rt_caught:
        runtime_native_install.download(base)
    rt_msg = str(rt_caught.value)
    assert "下载失败：DNS 解析失败" in rt_msg
    assert archive_name in rt_msg
    assert archive_sha in rt_msg
    assert str(base / "runtime") in rt_msg
    assert "secret" not in rt_msg

    # Now manually place the verified archive in base/runtime/<archive_name>: download() must succeed with 0 HTTP calls.
    manual_archive = base / "runtime" / archive_name
    manual_archive.parent.mkdir(parents=True, exist_ok=True)
    manual_archive.write_bytes(archive_bytes)
    server_path = runtime_native_install.download(base)
    assert server_path.is_file()


def test_t6_model_download_retries_three_times_with_exponential_backoff(tmp_path, monkeypatch):
    payload = b"model-weights-data-" * 64
    sha = hashlib.sha256(payload).hexdigest()

    # Persistent OSError: 1 initial attempt + 3 retries = 4 calls, sleeps [2.0, 4.0, 8.0]
    sleeps: list[float] = []
    calls = 0
    monkeypatch.setattr("time.sleep", lambda s: sleeps.append(s))

    def _always_fail(_request, timeout=60):
        nonlocal calls
        calls += 1
        raise TimeoutError("timed out")

    monkeypatch.setattr(model_download, "_urlopen", _always_fail)
    with pytest.raises(LifecycleError, match="下载失败：超时"):
        model_download.download("https://example.com/model.gguf", tmp_path / "model.gguf", sha, size_bytes=len(payload))
    assert calls == 4
    assert sleeps == [2.0, 4.0, 8.0]

    # Succeeds on 3rd retry (4th call) after 3 HTTP 503 errors
    sleeps.clear()
    calls = 0

    def _succeed_on_fourth(request, timeout=60):
        nonlocal calls
        calls += 1
        if calls < 4:
            raise urllib.error.HTTPError(request.full_url, 503, "Service Unavailable", {}, None)
        return _FakeHTTPResponse(payload, status=200)

    monkeypatch.setattr(model_download, "_urlopen", _succeed_on_fourth)
    out = tmp_path / "recovered.gguf"
    assert model_download.download("https://example.com/recovered.gguf", out, sha, size_bytes=len(payload)) == sha
    assert calls == 4
    assert sleeps == [2.0, 4.0, 8.0]
    assert out.read_bytes() == payload


def test_t7_docker_prepare_pulls_manifest_image_and_run_pulls_when_missing(tmp_path, monkeypatch):
    monkeypatch.delenv("RYNMESH_DOCKER_REGISTRY_MIRROR", raising=False)
    monkeypatch.setattr(runtime_docker, "_docker", lambda: "docker")

    custom_image = "registry.example.test/team/llama:v2@sha256:" + ("c" * 64)
    model = tmp_path / "m.gguf"
    model.write_bytes(b"GGUF" + bytes(96))
    manifest = LLMPackageManifest(
        package_id="custom-docker",
        mode="managed",
        public_model_alias="custom-docker",
        runtime=runtime_docker.RUNTIME_ID,
        model="custom-docker",
        model_path=str(model),
        checksum=fingerprint_file(model),
        base_url="http://127.0.0.1:18080",
        install_source={"runtime_image": custom_image},
    )

    events: list[tuple[str, list[str]]] = []
    progress_events: list[tuple[str, int, str]] = []

    class _FakePopen:
        def __init__(self, cmd, **_kwargs):
            events.append(("popen", list(cmd)))
            self.returncode = 0

        def poll(self):
            return 0

        def terminate(self):
            self.returncode = -15

        def kill(self):
            self.returncode = -9

        def wait(self, timeout=None):
            return 0

    monkeypatch.setattr(runtime_docker.subprocess, "Popen", _FakePopen)

    # 1. prepare(manifest) pulls the manifest's pinned image rather than DEFAULT_IMAGE.
    runtime_docker.prepare(manifest, progress=lambda *ev: progress_events.append(ev))
    assert events == [("popen", ["docker", "pull", custom_image])]
    assert any(stage == "pull_runtime" for stage, _, _ in progress_events)

    # 2. start(manifest) checks `docker image inspect` first; if missing (returncode=1), pulls before `docker run`.
    events.clear()
    progress_events.clear()

    def _run_missing_image(cmd, **_kwargs):
        events.append(("run", list(cmd)))
        if cmd[:3] == ["docker", "image", "inspect"]:
            return subprocess.CompletedProcess(cmd, 1, "", "No such image")
        return subprocess.CompletedProcess(cmd, 0, "", "")

    monkeypatch.setattr(runtime_docker.subprocess, "run", _run_missing_image)
    runtime_docker.start(manifest, progress=lambda *ev: progress_events.append(ev))

    inspect_idx = next(i for i, (kind, cmd) in enumerate(events) if kind == "run" and cmd[:3] == ["docker", "image", "inspect"])
    pull_idx = next(i for i, (kind, cmd) in enumerate(events) if kind == "popen" and cmd == ["docker", "pull", custom_image])
    docker_run_idx = next(i for i, (kind, cmd) in enumerate(events) if kind == "run" and cmd[:2] == ["docker", "run"])
    assert inspect_idx < pull_idx < docker_run_idx
    assert any(stage == "pull_runtime" for stage, _, _ in progress_events)

    # 3. When `docker image inspect` returns 0, `docker pull` is skipped.
    events.clear()
    monkeypatch.setattr(
        runtime_docker.subprocess,
        "run",
        lambda cmd, **_kw: (events.append(("run", list(cmd))) or subprocess.CompletedProcess(cmd, 0, "", "")),
    )
    runtime_docker.start(manifest)
    assert not any(kind == "popen" for kind, _ in events)
    assert any(cmd[:3] == ["docker", "image", "inspect"] for _, cmd in events)
    assert events[-1][1][:2] == ["docker", "run"]


def test_t8_rust_local_model_gguf_magic_check_and_unit_tests_present():
    rust_source = (
        Path(__file__).resolve().parents[1] / "webapp" / "src-tauri" / "src" / "local_model.rs"
    ).read_text(encoding="utf-8")
    assert "fn is_gguf(" in rust_source
    assert 'set_title("选择 GGUF 模型文件")' in rust_source
    assert '"local_model_not_gguf"' in rust_source
    assert '"local_model_unreadable"' in rust_source
    assert "#[cfg(test)]" in rust_source
