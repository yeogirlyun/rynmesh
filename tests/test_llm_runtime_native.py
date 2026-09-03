"""Tests for the native llama.cpp runtime backend and runtime selection.

The start/stop/health/self-test paths run a real child process: a small fake
`llama-server` written by the test that speaks the same loopback endpoints
(`/health`, `/v1/models`, `/v1/chat/completions`) as the real one.
"""

from __future__ import annotations

import hashlib
import io
import json
import os
import socket
import stat
import subprocess
import sys
import tarfile
import time
import urllib.request
import zipfile
from pathlib import Path
from typing import Any

import pytest

import rynmesh.llm_package.lifecycle as llm_lifecycle
import rynmesh.llm_package.runtime_docker as llm_runtime_docker
import rynmesh.llm_package.runtime_native as llm_runtime_native
import rynmesh.llm_package.runtime_native_install as llm_runtime_install
from rynmesh.llm_package.lifecycle import LifecycleError
from rynmesh.llm_package.manifest import LLMPackageManifest, fingerprint_file, load_manifest

posix_only = pytest.mark.skipif(
    os.name == "nt", reason="the fake llama-server is an executable POSIX script"
)

SERVER_BODY = """
import json, sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

ARGS = sys.argv[1:]


def option(name, default=""):
    return ARGS[ARGS.index(name) + 1] if name in ARGS else default


PORT = int(option("--port", "0"))
ALIAS = option("--alias", "fake-alias")


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/health":
            self._send({"status": "ok"})
        elif self.path == "/v1/models":
            self._send({"object": "list", "data": [{"id": ALIAS}]})
        else:
            self.send_error(404)

    def do_POST(self):
        if self.path != "/v1/chat/completions":
            self.send_error(404)
            return
        self.rfile.read(int(self.headers.get("content-length", "0")))
        self._send({
            "choices": [{"message": {"content": "RYNMESH SELF TEST OK"}}],
            "usage": {"prompt_tokens": 4, "completion_tokens": 5},
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


server = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
print("fake llama-server listening", flush=True)
server.serve_forever()
"""

EXIT_BODY = "import sys\nsys.exit(3)\n"


@pytest.fixture(autouse=True)
def isolated_runtime_env(tmp_path, monkeypatch):
    """Never read the developer's own bundled runtime or `~/.rynmesh`."""
    monkeypatch.delenv("RYNMESH_LLAMA_SERVER", raising=False)
    monkeypatch.delenv("RYNMESH_LLAMA_DIR", raising=False)
    monkeypatch.setenv("RYNMESH_LLM_HOME", str(tmp_path / "home-llm"))
    monkeypatch.setattr(llm_runtime_native.shutil, "which", lambda _name: None)


def _write_fake_server(path: Path, body: str) -> Path:
    path.write_text(f"#!{sys.executable}\n{body}", encoding="utf-8")
    path.chmod(0o755)
    return path


def _stub_server(path: Path) -> Path:
    """A file that only has to look like an executable server binary."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("stub", encoding="utf-8")
    path.chmod(0o755)
    return path


def _free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


def _wait_for_port(port: int, timeout_s: float = 20.0) -> None:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if llm_runtime_native._port_open(port):
            return
        time.sleep(0.05)
    raise AssertionError("the fake llama-server never accepted a connection")


def _manifest(package_id: str, *, root: Path, model: Path, port: int) -> LLMPackageManifest:
    return LLMPackageManifest(
        package_id=package_id, mode="managed", public_model_alias="rynmesh-fake",
        adapter="openai_compatible", runtime=llm_runtime_native.RUNTIME_ID,
        model="rynmesh-fake", model_path=str(model), checksum=fingerprint_file(model),
        base_url=f"http://127.0.0.1:{port}", runtime_dir=str(root),
        context_window=2048, max_output_tokens=256, max_concurrent=1,
    )


def _model_file(directory: Path) -> Path:
    model = directory / "model.gguf"
    model.write_bytes(b"GGUF" + bytes(96))
    return model


# --------------------------------------------------------------------------
# 1. Binary resolution
# --------------------------------------------------------------------------

def test_resolve_server_walks_env_file_then_dir_then_managed_then_path(tmp_path, monkeypatch):
    name = llm_runtime_install.server_filename()
    root = tmp_path / "llm"
    managed = llm_runtime_install.managed_root(root)
    explicit = _stub_server(tmp_path / "explicit" / name)
    bundled = tmp_path / "bundled"
    on_path = tmp_path / "path"
    for target in (bundled / name, managed / name, on_path / name):
        _stub_server(target)
    monkeypatch.setattr(llm_runtime_native.shutil, "which", lambda _name: str(on_path / name))
    monkeypatch.setenv("RYNMESH_LLAMA_SERVER", str(explicit))
    monkeypatch.setenv("RYNMESH_LLAMA_DIR", str(bundled))

    assert llm_runtime_native.resolve_server(root) == explicit
    monkeypatch.delenv("RYNMESH_LLAMA_SERVER")
    assert llm_runtime_native.resolve_server(root) == bundled / name
    monkeypatch.delenv("RYNMESH_LLAMA_DIR")
    assert llm_runtime_native.resolve_server(root) == managed / name
    (managed / name).unlink()
    assert llm_runtime_native.resolve_server(root) == on_path / name
    monkeypatch.setattr(llm_runtime_native.shutil, "which", lambda _name: None)
    assert llm_runtime_native.resolve_server(root) is None


@posix_only
def test_resolve_server_skips_a_server_without_the_execute_bit(tmp_path, monkeypatch):
    name = llm_runtime_install.server_filename()
    unusable = tmp_path / "bundled" / name
    unusable.parent.mkdir(parents=True)
    unusable.write_text("stub", encoding="utf-8")
    unusable.chmod(0o644)
    monkeypatch.setenv("RYNMESH_LLAMA_SERVER", str(unusable))
    monkeypatch.setenv("RYNMESH_LLAMA_DIR", str(unusable.parent))
    assert llm_runtime_native.resolve_server(tmp_path / "llm") is None


@posix_only
def test_start_reports_a_non_executable_server_without_leaking_its_path(tmp_path, monkeypatch):
    server = tmp_path / "llama-server"
    server.write_text("stub", encoding="utf-8")
    server.chmod(0o644)
    monkeypatch.setenv("RYNMESH_LLAMA_SERVER", str(server))
    manifest = _manifest("native-noexec", root=tmp_path / "llm",
                         model=_model_file(tmp_path), port=_free_port())
    with pytest.raises(LifecycleError) as failure:
        llm_runtime_native.start(manifest)
    assert "/" not in str(failure.value) and "\\" not in str(failure.value)


def test_resolve_server_prefers_the_marker_inside_the_managed_directory(tmp_path):
    root = tmp_path / "llm"
    managed = llm_runtime_install.managed_root(root)
    name = llm_runtime_install.server_filename()
    nested = _stub_server(managed / "build" / "bin" / name)
    (managed / llm_runtime_install.MARKER_NAME).write_text(
        json.dumps({"release": llm_runtime_install.RUNTIME_RELEASE,
                    "server": f"build/bin/{name}", "sha256": "0" * 64}),
        encoding="utf-8",
    )
    assert llm_runtime_native.resolve_server(root) == nested


def test_resolve_server_ignores_a_marker_that_escapes_the_runtime_directory(tmp_path):
    root = tmp_path / "llm"
    managed = llm_runtime_install.managed_root(root)
    managed.mkdir(parents=True, exist_ok=True)
    (managed / llm_runtime_install.MARKER_NAME).write_text(
        json.dumps({"server": "../../../etc/passwd"}), encoding="utf-8",
    )
    assert llm_runtime_native.resolve_server(root) is None


def test_available_reason_never_names_a_filesystem_path(monkeypatch):
    monkeypatch.setattr(llm_runtime_native, "resolve_server", lambda _root=None: None)
    monkeypatch.setattr(llm_runtime_native, "asset", lambda: None)
    ok, reason = llm_runtime_native.available()
    assert ok is False
    assert reason and "/" not in reason and "\\" not in reason

    monkeypatch.setattr(llm_runtime_native, "asset", lambda: ("a.tar.gz", "0" * 64, 1))
    assert llm_runtime_native.available() == (True, "")


def test_available_honours_a_custom_install_root(tmp_path, monkeypatch):
    root = tmp_path / "llm"
    _stub_server(llm_runtime_install.managed_root(root) / llm_runtime_install.server_filename())
    monkeypatch.setattr(llm_runtime_native, "asset", lambda: None)
    assert llm_runtime_native.available(root) == (True, "")
    assert llm_runtime_native.available(tmp_path / "elsewhere")[0] is False


# --------------------------------------------------------------------------
# 2. Managed runtime download
# --------------------------------------------------------------------------

def test_every_platform_asset_is_pinned_by_digest_over_https():
    assert llm_runtime_install.RUNTIME_BASE_URL.startswith("https://")
    assert llm_runtime_install.RUNTIME_RELEASE in llm_runtime_install.RUNTIME_BASE_URL
    assert set(llm_runtime_install.RUNTIME_ASSETS) == {
        ("Darwin", "arm64"), ("Darwin", "x86_64"), ("Linux", "x86_64"),
        ("Linux", "arm64"), ("Windows", "x86_64"), ("Windows", "arm64"),
    }
    for name, digest, size in llm_runtime_install.RUNTIME_ASSETS.values():
        assert llm_runtime_install.RUNTIME_RELEASE in name
        assert len(digest) == 64 and set(digest) <= set("0123456789abcdef")
        assert 0 < size <= llm_runtime_install.MAX_EXTRACTED_BYTES


@pytest.mark.parametrize(("reported", "expected"), [
    ("aarch64", "arm64"), ("arm64", "arm64"), ("AMD64", "x86_64"), ("x86_64", "x86_64"),
])
def test_machine_names_are_normalized_for_the_asset_table(monkeypatch, reported, expected):
    monkeypatch.setattr(llm_runtime_install.platform, "machine", lambda: reported)
    assert llm_runtime_install.machine() == expected


@pytest.mark.parametrize(("system", "reported", "key"), [
    ("Darwin", "arm64", ("Darwin", "arm64")),
    ("darwin", "aarch64", ("Darwin", "arm64")),
    ("Darwin", "x86_64", ("Darwin", "x86_64")),
    ("Linux", "x86_64", ("Linux", "x86_64")),
    ("Linux", "aarch64", ("Linux", "arm64")),
    ("Windows", "AMD64", ("Windows", "x86_64")),
    ("Windows", "ARM64", ("Windows", "arm64")),
])
def test_asset_for_answers_the_desktop_bundler_without_a_second_pin(system, reported, key):
    """The Tauri fetch script reads the pin through this helper."""
    assert llm_runtime_install.asset_for(system, reported) == llm_runtime_install.RUNTIME_ASSETS[key]


def test_asset_for_has_no_entry_for_an_unsupported_platform():
    assert llm_runtime_install.asset_for("Plan9", "x86_64") is None
    assert llm_runtime_install.asset_for("Darwin", "riscv64") is None


def test_asset_delegates_to_asset_for_with_the_running_platform(monkeypatch):
    monkeypatch.setattr(llm_runtime_install.platform, "system", lambda: "Linux")
    monkeypatch.setattr(llm_runtime_install.platform, "machine", lambda: "aarch64")
    assert llm_runtime_install.asset() == llm_runtime_install.RUNTIME_ASSETS[("Linux", "arm64")]


class _FakeResponse:
    def __init__(self, payload: bytes) -> None:
        self._stream = io.BytesIO(payload)
        self.headers = {"content-length": str(len(payload))}

    def read(self, size: int = -1) -> bytes:
        return self._stream.read(size)

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False


def _tar_archive(entries, *, symlink=None) -> bytes:
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as bundle:
        for name, payload in entries:
            info = tarfile.TarInfo(name)
            info.size = len(payload)
            info.mode = 0o644
            bundle.addfile(info, io.BytesIO(payload))
        if symlink is not None:
            info = tarfile.TarInfo(symlink[0])
            info.type = tarfile.SYMTYPE
            info.linkname = symlink[1]
            bundle.addfile(info)
    return buffer.getvalue()


def _zip_archive(entries, *, symlink=None) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as bundle:
        for name, payload in entries:
            info = zipfile.ZipInfo(name)
            info.external_attr = 0o644 << 16
            bundle.writestr(info, payload)
        if symlink is not None:
            info = zipfile.ZipInfo(symlink[0])
            info.external_attr = 0o120777 << 16
            bundle.writestr(info, symlink[1])
    return buffer.getvalue()


def _pin(monkeypatch, payload: bytes, *, name: str = "llama-test-bin.tar.gz",
         digest: str = "", size: int = -1) -> None:
    # `isolated_runtime_env` already guarantees nothing resolves before the
    # download, so `prepare` really exercises the fetch/extract path.
    monkeypatch.setattr(llm_runtime_install, "asset", lambda: (
        name,
        digest or hashlib.sha256(payload).hexdigest(),
        len(payload) if size < 0 else size,
    ))


def _serve(monkeypatch, payload: bytes) -> dict[str, Any]:
    seen: dict[str, Any] = {}

    class _Opener:
        def open(self, request, timeout=0):
            seen["url"] = request.full_url
            seen["agent"] = str(request.get_header("User-agent") or "")
            return _FakeResponse(payload)

    def fake_build_opener(*handlers):
        seen["handlers"] = handlers
        return _Opener()

    monkeypatch.setattr(llm_runtime_install.urllib.request, "build_opener", fake_build_opener)
    return seen


TAR_ENTRIES = [
    ("build/bin/llama-server", b"#!/bin/sh\nexit 0\n"),
    ("build/bin/libggml.so", b"\x00" * 32),
]
ZIP_ENTRIES = [
    ("build/bin/llama-server.exe", b"MZ fake binary"),
    ("build/bin/ggml.dll", b"\x00" * 32),
]


def test_prepare_refuses_a_runtime_url_that_is_not_https(tmp_path, monkeypatch):
    payload = _tar_archive(TAR_ENTRIES)
    _pin(monkeypatch, payload)
    monkeypatch.setattr(llm_runtime_install, "RUNTIME_BASE_URL", "http://127.0.0.1:9/")

    def refuse(*_args, **_kwargs):
        raise AssertionError("a plaintext runtime URL must never be fetched")

    monkeypatch.setattr(llm_runtime_install.urllib.request, "build_opener", refuse)
    with pytest.raises(LifecycleError, match="HTTPS"):
        llm_runtime_native.prepare(root=tmp_path / "llm")


def test_a_redirect_off_https_is_refused_mid_download():
    handler = llm_runtime_install._HttpsOnlyRedirect()
    request = urllib.request.Request("https://example.invalid/a.tar.gz")
    with pytest.raises(LifecycleError, match="non-HTTPS"):
        handler.redirect_request(request, None, 302, "Found", {}, "http://example.invalid/a.tar.gz")


def test_the_runtime_fetch_installs_the_https_only_redirect_handler(tmp_path, monkeypatch):
    payload = _tar_archive(TAR_ENTRIES)
    _pin(monkeypatch, payload)
    seen = _serve(monkeypatch, payload)
    llm_runtime_native.prepare(root=tmp_path / "llm")
    assert llm_runtime_install._HttpsOnlyRedirect in seen["handlers"]


def test_prepare_deletes_the_archive_on_a_checksum_mismatch(tmp_path, monkeypatch):
    root = tmp_path / "llm"
    payload = _tar_archive(TAR_ENTRIES)
    _pin(monkeypatch, payload, digest="f" * 64)
    _serve(monkeypatch, payload)
    with pytest.raises(LifecycleError, match="runtime archive checksum mismatch"):
        llm_runtime_native.prepare(root=root)
    assert list((root / "runtime").glob("*")) == []
    assert not llm_runtime_install.managed_root(root).exists()


def test_prepare_refuses_to_read_past_the_pinned_size(tmp_path, monkeypatch):
    root = tmp_path / "llm"
    payload = _tar_archive(TAR_ENTRIES)
    _pin(monkeypatch, payload, size=16)
    _serve(monkeypatch, payload)
    with pytest.raises(LifecycleError, match="larger than its pinned size"):
        llm_runtime_native.prepare(root=root)
    assert list((root / "runtime").glob("*")) == []


def test_prepare_rejects_a_corrupt_archive_without_leaking_a_path(tmp_path, monkeypatch):
    root = tmp_path / "llm"
    payload = b"this is not a gzip stream" * 8
    _pin(monkeypatch, payload)  # the digest matches; the bytes are still garbage
    _serve(monkeypatch, payload)
    with pytest.raises(LifecycleError) as failure:
        llm_runtime_native.prepare(root=root)
    message = str(failure.value)
    assert "unreadable or corrupt" in message
    assert "/" not in message and "\\" not in message


@pytest.mark.parametrize("archive_kind", ["tar", "zip"])
@pytest.mark.parametrize("member_kind", ["traversal", "absolute", "symlink"])
def test_prepare_rejects_unsafe_archive_members(tmp_path, monkeypatch, archive_kind, member_kind):
    root = tmp_path / "llm"
    build, entries, name = (
        (_tar_archive, TAR_ENTRIES, "llama-test-bin.tar.gz") if archive_kind == "tar"
        else (_zip_archive, ZIP_ENTRIES, "llama-test-bin.zip")
    )
    if member_kind == "traversal":
        payload, expected = build([*entries, ("../evil", b"escaped")]), "unsafe member path"
    elif member_kind == "absolute":
        payload, expected = build([*entries, ("/etc/evil", b"escaped")]), "unsafe member path"
    else:
        payload = build(entries, symlink=("build/bin/link", "/etc/passwd"))
        expected = "unsafe member type"
    _pin(monkeypatch, payload, name=name)
    _serve(monkeypatch, payload)
    with pytest.raises(LifecycleError, match=expected):
        llm_runtime_native.prepare(root=root)
    managed = llm_runtime_install.managed_root(root)
    assert not (managed / llm_runtime_install.MARKER_NAME).exists()
    assert not (managed / "evil").exists() and not (managed / "build" / "bin" / "link").exists()


@pytest.mark.parametrize("archive_kind", ["tar", "zip"])
def test_prepare_refuses_an_archive_that_expands_past_the_size_cap(tmp_path, monkeypatch,
                                                                  archive_kind):
    build, name = ((_tar_archive, "llama-test-bin.tar.gz") if archive_kind == "tar"
                   else (_zip_archive, "llama-test-bin.zip"))
    payload = build([("build/bin/llama-server", b"x" * 4096)])
    _pin(monkeypatch, payload, name=name)
    _serve(monkeypatch, payload)
    monkeypatch.setattr(llm_runtime_install, "MAX_EXTRACTED_BYTES", 1024)
    with pytest.raises(LifecycleError, match="expands past the extraction size limit"):
        llm_runtime_native.prepare(root=tmp_path / "llm")


@pytest.mark.parametrize("archive_kind", ["tar", "zip"])
def test_prepare_extracts_marks_the_server_executable_and_writes_the_marker(tmp_path, monkeypatch,
                                                                           archive_kind):
    root = tmp_path / "llm"
    build, entries, name, binary = (
        (_tar_archive, TAR_ENTRIES, "llama-test-bin.tar.gz", "llama-server")
        if archive_kind == "tar"
        else (_zip_archive, ZIP_ENTRIES, "llama-test-bin.zip", "llama-server.exe")
    )
    monkeypatch.setattr(llm_runtime_install, "server_filename", lambda: binary)
    payload = build(entries)
    digest = hashlib.sha256(payload).hexdigest()
    _pin(monkeypatch, payload, name=name)
    seen = _serve(monkeypatch, payload)
    stages: list[tuple[str, int, str]] = []
    llm_runtime_native.prepare(root=root, progress=lambda *event: stages.append(event))

    assert seen["url"].startswith("https://github.com/ggml-org/llama.cpp/releases/download/")
    assert seen["agent"] == "Rynmesh/0.6"
    managed = llm_runtime_install.managed_root(root)
    server = managed / "build" / "bin" / binary
    assert server.is_file() and os.access(server, os.X_OK)
    assert json.loads((managed / llm_runtime_install.MARKER_NAME).read_text(encoding="utf-8")) == {
        "release": llm_runtime_install.RUNTIME_RELEASE,
        "server": f"build/bin/{binary}",
        "sha256": digest,
    }
    # The marker is written last, so its presence means the binary is there.
    assert llm_runtime_native.resolve_server(root) == server
    assert list((root / "runtime").glob("llama-test-bin*")) == []
    assert {stage for stage, _percent, _message in stages} == {"pull_runtime"}
    assert all(65 <= percent <= 80 for _stage, percent, _message in stages)


def test_prepare_skips_the_download_when_a_server_is_already_present(tmp_path, monkeypatch):
    server = _stub_server(tmp_path / "llama-server")
    monkeypatch.setenv("RYNMESH_LLAMA_SERVER", str(server))

    def refuse(*_args, **_kwargs):
        raise AssertionError("an already-present runtime must never be downloaded")

    monkeypatch.setattr(llm_runtime_install.urllib.request, "build_opener", refuse)
    stages: list[tuple[str, int, str]] = []
    llm_runtime_native.prepare(root=tmp_path / "llm", progress=lambda *event: stages.append(event))
    assert stages == [("pull_runtime", 72, "Local inference runtime already present")]


# --------------------------------------------------------------------------
# 3-5. Process control
# --------------------------------------------------------------------------

@posix_only
def test_start_health_self_test_and_stop_run_a_real_child_process(tmp_path, monkeypatch):
    root = tmp_path / "llm"
    server = _write_fake_server(tmp_path / "llama-server", SERVER_BODY)
    monkeypatch.setenv("RYNMESH_LLAMA_SERVER", str(server))
    manifest = _manifest("native-one", root=root, model=_model_file(tmp_path), port=_free_port())
    try:
        llm_runtime_native.start(manifest)
        assert manifest.runtime_command[0] == str(server)
        assert "--no-webui" in manifest.runtime_command
        assert "-v" not in manifest.runtime_command
        assert llm_lifecycle.wait_healthy(manifest, timeout_s=20)["ok"] is True
        assert llm_lifecycle.self_test(manifest)["ok"] is True

        runtime_state = llm_runtime_native.state(manifest)
        assert runtime_state["running"] is True
        assert runtime_state["status"] == "running"
        assert runtime_state["release"] == llm_runtime_install.RUNTIME_RELEASE
        assert str(server) not in json.dumps(runtime_state)

        pid_file = root / "runtime" / "native-one.pid"
        log_file = root / "runtime" / "native-one.log"
        pid = int(pid_file.read_text(encoding="utf-8"))
        assert stat.S_IMODE(log_file.stat().st_mode) == 0o600
        assert stat.S_IMODE(pid_file.stat().st_mode) == 0o600
        assert stat.S_IMODE((root / "runtime").stat().st_mode) == 0o700
        log = log_file.read_text(encoding="utf-8")
        assert "fake llama-server listening" in log  # child output really is captured
        assert "Reply with exactly" not in log and "RYNMESH SELF TEST OK" not in log

        assert llm_runtime_native.stop(manifest) is True
        assert llm_runtime_native._alive(pid) is False
        assert not pid_file.exists()
        assert llm_runtime_native.state(manifest)["status"] == "stopped"
    finally:
        llm_runtime_native.stop(manifest)


@posix_only
def test_start_fails_fast_when_the_server_exits_without_leaking_a_path(tmp_path, monkeypatch):
    root = tmp_path / "llm"
    server = _write_fake_server(tmp_path / "llama-server", EXIT_BODY)
    monkeypatch.setenv("RYNMESH_LLAMA_SERVER", str(server))
    manifest = _manifest("native-dead", root=root, model=_model_file(tmp_path), port=_free_port())
    with pytest.raises(LifecycleError) as failure:
        llm_runtime_native.start(manifest)
    message = str(failure.value)
    assert "exited during startup" in message
    assert "/" not in message and "\\" not in message
    assert not (root / "runtime" / "native-dead.pid").exists()


@posix_only
def test_start_adopts_a_server_that_already_owns_the_port(tmp_path, monkeypatch):
    root = tmp_path / "llm"
    script = _write_fake_server(tmp_path / "llama-server", SERVER_BODY)
    monkeypatch.setenv("RYNMESH_LLAMA_SERVER", str(script))
    port = _free_port()
    owner = subprocess.Popen(
        [sys.executable, str(script), "--port", str(port), "--alias", "rynmesh-fake"],
        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    try:
        _wait_for_port(port)
        manifest = _manifest("native-adopt", root=root, model=_model_file(tmp_path), port=port)
        llm_runtime_native.start(manifest)
        assert (root / "runtime" / "native-adopt.pid").read_text(encoding="utf-8") == "0"
        assert manifest.runtime_command == []
        assert llm_runtime_native.state(manifest)["status"] == "adopted"
        assert llm_runtime_native.stop(manifest) is False
        assert owner.poll() is None  # an owner-managed server is never killed
    finally:
        owner.terminate()
        owner.wait(timeout=10)


def test_stop_reports_nothing_stopped_for_a_stale_pidfile(tmp_path):
    root = tmp_path / "llm"
    manifest = _manifest("native-stale", root=root, model=_model_file(tmp_path), port=_free_port())
    pid_path = root / "runtime" / "native-stale.pid"
    pid_path.parent.mkdir(parents=True, exist_ok=True)
    pid_path.write_text("999999999", encoding="utf-8")
    assert llm_runtime_native.stop(manifest) is False
    assert not pid_path.exists()
    assert llm_runtime_native.stop(manifest) is False  # no pidfile at all


def test_start_refuses_a_model_whose_checksum_changed(tmp_path, monkeypatch):
    server = _stub_server(tmp_path / "llama-server")
    monkeypatch.setenv("RYNMESH_LLAMA_SERVER", str(server))
    model = _model_file(tmp_path)
    manifest = _manifest("native-tamper", root=tmp_path / "llm", model=model, port=_free_port())
    model.write_bytes(b"GGUF" + bytes(32))
    with pytest.raises(LifecycleError, match="checksum no longer matches"):
        llm_runtime_native.start(manifest)


# --------------------------------------------------------------------------
# 6. Runtime selection
# --------------------------------------------------------------------------

def test_select_runtime_prefers_native_and_falls_back_to_docker(monkeypatch):
    monkeypatch.setattr(llm_runtime_native, "available", lambda _root=None: (True, ""))
    monkeypatch.setattr(llm_runtime_docker, "available", lambda: (True, ""))
    assert llm_lifecycle.select_runtime("auto") is llm_runtime_native
    assert llm_lifecycle.select_runtime("docker") is llm_runtime_docker
    assert llm_lifecycle.select_runtime("native") is llm_runtime_native

    monkeypatch.setattr(llm_runtime_native, "available",
                        lambda _root=None: (False, "native runtime missing"))
    assert llm_lifecycle.select_runtime("auto") is llm_runtime_docker
    with pytest.raises(LifecycleError, match="native runtime missing"):
        llm_lifecycle.select_runtime("native")

    monkeypatch.setattr(llm_runtime_docker, "available", lambda: (False, "docker engine is not running"))
    with pytest.raises(LifecycleError, match="no local inference runtime is available"):
        llm_lifecycle.select_runtime("auto")
    with pytest.raises(LifecycleError, match="must be auto, native, or docker"):
        llm_lifecycle.select_runtime("podman")


def test_select_runtime_passes_the_install_root_to_the_native_backend(tmp_path, monkeypatch):
    seen: list[Any] = []

    def fake_available(root=None):
        seen.append(root)
        return True, ""

    monkeypatch.setattr(llm_runtime_native, "available", fake_available)
    assert llm_lifecycle.select_runtime("auto", root=tmp_path) is llm_runtime_native
    assert seen == [tmp_path]


def test_backend_resolves_the_native_runtime():
    manifest = LLMPackageManifest(
        package_id="native-two", mode="managed", public_model_alias="native-two",
        runtime=llm_runtime_native.RUNTIME_ID,
    )
    assert llm_lifecycle._backend(manifest) is llm_runtime_native
    assert llm_lifecycle.RUNTIME_NATIVE == "native_llama_cpp"
    assert llm_lifecycle.RUNTIME_DOCKER == llm_runtime_docker.RUNTIME_ID


# --------------------------------------------------------------------------
# 7. Managed install end to end
# --------------------------------------------------------------------------

@posix_only
def test_install_managed_on_the_native_runtime_keeps_local_details_private(tmp_path, monkeypatch):
    root = tmp_path / "llm"
    server = _write_fake_server(tmp_path / "llama-server", SERVER_BODY)
    monkeypatch.setattr(llm_runtime_native, "resolve_server", lambda _root=None: server)
    payload = b"GGUF" + bytes(96)
    digest = hashlib.sha256(payload).hexdigest()

    def fake_download(_url, destination, expected_sha256, **_kwargs):
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(payload)
        return expected_sha256

    monkeypatch.setattr(llm_lifecycle, "_download", fake_download)
    result = llm_lifecycle.install_managed(
        package_id="native-managed", root=root, port=_free_port(),
        model_url="https://huggingface.co/example/example/resolve/main/example.gguf",
        expected_sha256=digest, accept_risk=True, runtime="native",
    )
    manifest = load_manifest(result["manifest"])
    try:
        assert manifest.runtime == llm_runtime_native.RUNTIME_ID
        assert manifest.runtime_command[0] == str(server)
        assert manifest.runtime_dir == str(root)
        assert manifest.install_source["runtime_release"] == llm_runtime_install.RUNTIME_RELEASE
        assert manifest.install_source["profile"] == "custom"
        assert result["self_test"]["ok"] is True
        public = json.dumps(manifest.public_dict())
        assert "runtime_command" not in public and "runtime_dir" not in public
        assert str(server) not in public and str(root) not in public
        assert manifest.public_dict()["runtime"] == "native_llama_cpp"
    finally:
        llm_runtime_native.stop(manifest)
