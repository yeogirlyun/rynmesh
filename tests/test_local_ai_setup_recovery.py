from __future__ import annotations

import hashlib
import io
import json
import time
import zipfile

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import rynmesh.llm_package.hardware as hardware_module
import rynmesh.llm_package.lifecycle as lifecycle
import rynmesh.llm_package.routes as routes_module
from rynmesh.llm_package.hardware import HardwareReport, setup_recommendations
from rynmesh.llm_package.lifecycle import LifecycleError
from rynmesh.llm_package.routes import install_llm_routes
from rynmesh.services import peer_box
from rynmesh.store import RynmeshStore


class _Response:
    def __init__(self, body: bytes, *, status: int, headers: dict[str, str]):
        self._body = io.BytesIO(body)
        self.status = status
        self.headers = headers

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def getcode(self):
        return self.status

    def read(self, size: int = -1):
        return self._body.read(size)


def _hardware(*, ram: int = 8000, disk: int = 8000) -> HardwareReport:
    return HardwareReport(
        os="Windows",
        architecture="AMD64",
        cpu="fixture",
        logical_cpus=8,
        ram_total_mb=16_000,
        ram_available_mb=ram,
        disk_free_mb=disk,
        nvidia_gpus=[],
        nvidia_probe="not found",
        container_runtime="",
        container_available=False,
        warnings=[],
    )


def test_product_profiles_are_versioned_and_fail_closed(monkeypatch):
    monkeypatch.setattr(hardware_module, "platform_id", lambda: "windows-x86_64")
    catalog = setup_recommendations(_hardware())
    assert catalog["version"] == "ryn.local-ai-hardware.v1"
    assert catalog["recommended_profile"] == "light"
    assert [profile["id"] for profile in catalog["profiles"]] == ["light", "balanced", "quality"]
    assert catalog["profiles"][0]["supported"] is True
    assert catalog["profiles"][1]["supported"] is False

    monkeypatch.setattr(hardware_module, "platform_id", lambda: "linux-riscv64")
    unsupported = setup_recommendations(_hardware())
    assert unsupported["recommended_profile"] is None
    assert all(not profile["supported"] for profile in unsupported["profiles"])


def test_verified_download_resumes_only_a_valid_range(tmp_path, monkeypatch):
    complete = b"GGUF" + b"verified-model-bytes" * 100
    split = 517
    expected = hashlib.sha256(complete).hexdigest()
    destination = tmp_path / "model.gguf"
    partial = destination.with_suffix(".gguf.part")
    partial.write_bytes(complete[:split])
    partial.with_suffix(".part.json").write_text(
        json.dumps({"url": "https://example.test/model", "sha256": expected, "validator": '"v1"'}),
        encoding="utf-8",
    )
    captured = {}

    def urlopen(request, timeout):
        captured.update(dict(request.header_items()))
        return _Response(
            complete[split:],
            status=206,
            headers={
                "content-length": str(len(complete) - split),
                "content-range": f"bytes {split}-{len(complete) - 1}/{len(complete)}",
                "etag": '"v1"',
            },
        )

    monkeypatch.setattr(lifecycle.urllib.request, "urlopen", urlopen)
    lifecycle._download("https://example.test/model", destination, expected)
    assert captured["Range"] == f"bytes={split}-"
    assert destination.read_bytes() == complete
    assert not partial.exists()


def test_download_restarts_when_server_ignores_range_and_rejects_oversize(tmp_path, monkeypatch):
    complete = b"GGUF" + b"fresh" * 20
    expected = hashlib.sha256(complete).hexdigest()
    destination = tmp_path / "model.gguf"
    partial = destination.with_suffix(".gguf.part")
    partial.write_bytes(b"poison")
    partial.with_suffix(".part.json").write_text(
        json.dumps({"url": "https://example.test/model", "sha256": expected}), encoding="utf-8",
    )
    monkeypatch.setattr(
        lifecycle.urllib.request,
        "urlopen",
        lambda *_args, **_kwargs: _Response(
            complete, status=200, headers={"content-length": str(len(complete))},
        ),
    )
    lifecycle._download("https://example.test/model", destination, expected)
    assert destination.read_bytes() == complete

    destination.unlink()
    monkeypatch.setattr(
        lifecycle.urllib.request,
        "urlopen",
        lambda *_args, **_kwargs: _Response(b"", status=200, headers={"content-length": "9999"}),
    )
    with pytest.raises(LifecycleError, match="size limit"):
        lifecycle._download("https://example.test/model", destination, expected, max_bytes=100)


def test_runtime_archive_rejects_path_escape(tmp_path):
    archive = tmp_path / "runtime.zip"
    with zipfile.ZipFile(archive, "w") as bundle:
        bundle.writestr("../escape/llama-server.exe", b"unsafe")
    with pytest.raises(LifecycleError, match="unsafe path"):
        lifecycle._safe_extract_runtime(archive, tmp_path / "runtime")
    assert not (tmp_path / "escape").exists()


def _client(tmp_path, monkeypatch, install):
    monkeypatch.setattr(routes_module, "install_native_managed", install)
    home = tmp_path / "node"
    store = RynmeshStore(home=home, network_dir=tmp_path / "network")
    messaging_key = peer_box.load_or_create_messaging_key(home / "messaging.x25519")
    app = FastAPI()
    install_llm_routes(
        app,
        store=store,
        home=home,
        messaging_key=messaging_key,
        resolve_endpoint=lambda _peer_id: "",
        resolve_pubkey=lambda _peer_id: "",
    )
    return TestClient(app), home


def test_v2_job_exposes_progress_safe_error_and_retry_attempt(tmp_path, monkeypatch):
    def interrupted(**kwargs):
        kwargs["progress"](
            "download_model", 42, "Downloading verified model data", 120, 500,
        )
        raise LifecycleError("download interrupted PRIVATE_PROMPT /private/model/path")

    client, home = _client(tmp_path, monkeypatch, interrupted)
    with client:
        missing_license = client.post("/api/local/llm/setup/async", json={"profile": "light"})
        assert missing_license.status_code == 400
        queued = client.post(
            "/api/local/llm/setup/async",
            json={"profile": "light", "accept_license": True},
        ).json()
        assert queued["version"] == "ryn.local-ai-install.v2"
        assert queued["selected_profile"] == "light"
        deadline = time.monotonic() + 3
        failed = {}
        while time.monotonic() < deadline:
            failed = client.get("/api/local/llm/setup/status").json()
            if failed.get("state") == "failed":
                break
            time.sleep(0.02)
        assert failed["safe_error"] == "download_interrupted"
        assert failed["resumable"] is True
        assert "PRIVATE_PROMPT" not in json.dumps(failed)
        assert "/private/model/path" not in json.dumps(failed)

        retried = client.post(f"/api/local/llm/setup/{queued['job_id']}/retry")
        assert retried.status_code == 200
        assert retried.json()["attempt"] == 2
        assert retried.json()["job_id"] != queued["job_id"]

        readiness = client.get("/api/local/llm/readiness").json()
        assert readiness["version"] == "ryn.local-ai-readiness.v1"
        assert readiness["ready"] is False
        assert "PRIVATE_PROMPT" not in json.dumps(readiness)

    retry_file = (home / "llm" / "setup-retry.json").read_text(encoding="utf-8")
    assert "PRIVATE_PROMPT" not in retry_file
    assert "light" in retry_file
