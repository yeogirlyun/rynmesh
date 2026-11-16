"""S01-I32 tests: local runtime error classification, Docker error mapping, adaptive port/health wait, and LifecycleError codes."""
from __future__ import annotations

import errno
import hashlib
import json
import os
import socket
import subprocess
import sys
import threading
import urllib.error
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from test_native_model import tiny_model, wait

from rynmesh.llm_package import (
    lifecycle,
    native_model,
    native_worker,
    routes,
    runtime_docker,
    runtime_native,
    runtime_native_install,
    setup_recovery,
)
from rynmesh.llm_package.errors import LifecycleError
from rynmesh.llm_package.manifest import (
    LLMPackageManifest,
    fingerprint_file,
    load_manifest,
    save_manifest,
)
from rynmesh.services import peer_box
from rynmesh.store import RynmeshStore


def _make_app(home: Path, network: Path) -> FastAPI:
    store = RynmeshStore(home=home, network_dir=network)
    key = peer_box.load_or_create_messaging_key(home / "messaging.x25519")
    app = FastAPI()
    routes.install_llm_routes(
        app,
        store=store,
        home=home,
        messaging_key=key,
        resolve_endpoint=lambda _: "",
        resolve_pubkey=lambda _: "",
    )
    return app


def test_t1_classify_llama_output_maps_closed_categories_and_bounds_tail():
    """T1 (Change 1): classify_llama_output maps stderr bytes to closed categories and inspects only the trailing 16 KiB."""
    classify = runtime_native.classify_llama_output
    assert classify(b"ggml_backend_buffer: failed to allocate 4096 MiB") == "out_of_memory"
    assert classify(b"CUDAMalloc failed: out of memory") == "out_of_memory"
    assert classify(b"CUDA error: device-side assert triggered in ggml_cuda") == "gpu_error"
    assert classify(b"ggml_vulkan: device lost") == "gpu_error"
    assert classify(b"llama_model_load: error loading model: unknown model architecture 'foo'") == "unsupported_model"
    assert classify(b"invalid magic in header / failed to load model") == "unsupported_model"
    assert classify(b"common_chat_templates_init: jinja parse error in chat template") == "template_error"
    assert classify(b"svr_init: couldn't bind HTTP server socket: Address already in use") == "port_in_use"
    assert classify(b"some unrelated internal line without keywords") == "unknown"

    # Bytes older than the last 16 KiB are discarded in the ring buffer / tail slice
    old_prefix = b"failed to allocate 9999 MiB\n" + (b"x" * (20 * 1024))
    assert classify(old_prefix) == "unknown"
    recent_suffix = (b"x" * (20 * 1024)) + b"\njinja chat template error\n"
    assert classify(recent_suffix) == "template_error"


def test_t2_spawn_and_native_model_capture_stderr_category_without_logging_raw_output(tmp_path, monkeypatch):
    """T2 (Changes 1, 2, 3): Fake llama-server printing 'failed to allocate' + secret canary -> out_of_memory error, log has only category, and native_model records runtime_exited:out_of_memory + error_category."""
    canary = "PRIVATE_PROMPT_SECRET_CANARY_9821"
    cmd = [
        sys.executable,
        "-c",
        f"import sys; sys.stderr.write('ggml: failed to allocate buffer {canary}\\n'); sys.stderr.flush(); sys.exit(2)",
    ]
    root = tmp_path / "llm"
    with pytest.raises(LifecycleError) as exc_info:
        runtime_native._spawn(Path(sys.executable), cmd, root, "oom-pkg", 0)

    msg = str(exc_info.value)
    assert "out_of_memory" in msg
    assert "see the runtime log" not in msg.lower()
    assert canary not in msg

    log_text = (root / "runtime" / "oom-pkg.log").read_text(encoding="utf-8")
    assert "runtime_process_exited code=2 category=out_of_memory" in log_text
    assert canary not in log_text
    assert "failed to allocate" not in log_text

    # Also verify NativeModel._start attaches category to runtime_exited and exposes error_category in status()
    gguf = tiny_model(tmp_path)
    nm = native_model.NativeModel(tmp_path / "nm-home")
    nm.model_path.parent.mkdir(parents=True, exist_ok=True)
    nm.model_path.write_bytes(gguf.read_bytes())
    nm.metadata = native_model.validate_model(gguf)
    nm.config["installed"] = True
    nm._update_manifest()

    if os.name == "nt":
        bad_exe = tmp_path / "fake_llama.cmd"
        bad_exe.write_text(f"@echo ggml failed to allocate {canary} 1>&2\r\n@exit /b 2\r\n", encoding="utf-8")
    else:
        bad_exe = tmp_path / "fake_llama"
        bad_exe.write_text(f"#!/bin/sh\necho 'ggml failed to allocate {canary}' >&2\nexit 2\n", encoding="utf-8")
        bad_exe.chmod(0o755)

    monkeypatch.setattr(native_model, "platform_key", lambda: "windows-cpu")
    monkeypatch.setattr(native_model, "_memory", lambda: (32000, 16000))
    monkeypatch.setattr(native_model, "_nvidia", lambda: ([], ""))
    monkeypatch.setattr(nm, "_engine", lambda _b: bad_exe)
    nm.action("start")
    wait(nm)
    st = nm.status()
    assert st["state"] == "error"
    assert st["error"] == "runtime_exited:out_of_memory"
    assert st["error_category"] == "out_of_memory"
    assert canary not in json.dumps(st)


def test_t3_native_model_classifies_enospc_memory_and_download_exceptions(tmp_path, monkeypatch):
    """T3 (Change 3): Non-NativeModelError exceptions in NativeModel._run map ENOSPC->disk_full, MemoryError->insufficient_memory, URLError/HTTPError->download_failed, others->operation_failed."""
    gguf = tiny_model(tmp_path)
    cases = [
        (OSError(errno.ENOSPC, "No space left on device"), "disk_full"),
        (MemoryError("out of host memory"), "insufficient_memory"),
        (urllib.error.URLError("network down"), "download_failed"),
        (RuntimeError("unexpected internal bug"), "operation_failed"),
    ]
    for idx, (raised_exc, expected_err) in enumerate(cases):
        nm = native_model.NativeModel(tmp_path / f"nm-{idx}")
        nm.model_path.parent.mkdir(parents=True, exist_ok=True)
        nm.model_path.write_bytes(gguf.read_bytes())
        nm.metadata = native_model.validate_model(gguf)
        nm.config["installed"] = True
        monkeypatch.setattr(native_model, "platform_key", lambda: "windows-cpu")
        monkeypatch.setattr(native_model, "_memory", lambda: (32000, 16000))
        monkeypatch.setattr(native_model, "_nvidia", lambda: ([], ""))
        monkeypatch.setattr(nm, "_engine", lambda _b, e=raised_exc: (_ for _ in ()).throw(e))
        nm.action("start")
        wait(nm)
        st = nm.status()
        assert st["state"] == "error"
        assert st["error"] == expected_err
        assert st["error_category"] == expected_err


def test_t4_native_worker_inherits_stderr_and_propagates_child_exit_code(monkeypatch):
    """T4 (Change 4): macOS supervisor native_worker inherits stderr so parent can read category and exits with child's non-zero returncode."""
    child_code = "import sys; sys.stderr.write('ggml_cuda: CUDA error 700\\n'); sys.stderr.flush(); sys.exit(17)"
    runner_code = "import os, sys; from rynmesh.llm_package.native_worker import main; main([str(os.getppid()), *sys.argv[1:]])"
    proc = subprocess.run(
        [sys.executable, "-c", runner_code, sys.executable, "-c", child_code],
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert proc.returncode == 17
    assert "ggml_cuda: CUDA error 700" in proc.stderr

    monkeypatch.setattr(native_worker.os, "getppid", lambda: 4242)
    with pytest.raises(SystemExit) as exit_info:
        native_worker.main(["4242", sys.executable, "-c", "import sys; sys.exit(23)"])
    assert exit_info.value.code == 23


def test_t5_docker_run_classifies_stderr_and_update_captures_output(tmp_path, monkeypatch):
    """T5 (Change 5): docker run stderr is mapped to specific Chinese/category messages without 'inspect Docker logs' while keeping --log-driver none; update uses capture_output=True and wraps errors in LifecycleError."""
    model = tmp_path / "m.gguf"
    model.write_bytes(b"GGUF" + bytes(96))
    manifest = LLMPackageManifest(
        package_id="docker-err",
        mode="managed",
        public_model_alias="alias",
        runtime=runtime_docker.RUNTIME_ID,
        model="alias",
        model_path=str(model),
        checksum=fingerprint_file(model),
        base_url="http://127.0.0.1:18080",
    )
    monkeypatch.setattr(runtime_docker, "_docker", lambda: "docker")
    monkeypatch.setattr(runtime_docker, "_image_present", lambda *_a: True)

    stderr_samples = [
        ("docker: Error response from daemon: Bind for 127.0.0.1:18080 failed: port is already allocated.", "port_in_use"),
        ("docker: write /var/lib/docker: no space left on device.", "disk_full"),
        ("docker: pull access denied for repo, repository does not exist.", "image_unavailable"),
        ("Cannot connect to the Docker daemon at unix:///var/run/docker.sock.", "daemon_unreachable"),
        ("docker: Error response from daemon: OCI runtime create failed: container_linux.go.", "oci_runtime_error"),
    ]
    seen_commands: list[list[str]] = []
    for raw_stderr, expected_token in stderr_samples:
        def fake_run(cmd, raw=raw_stderr, **_kw):
            seen_commands.append(list(cmd))
            if cmd[1:3] == ["rm", "-f"]:
                return subprocess.CompletedProcess(cmd, 0, "", "")
            return subprocess.CompletedProcess(cmd, 125, "", raw)

        monkeypatch.setattr(runtime_docker.subprocess, "run", fake_run)
        with pytest.raises(LifecycleError) as exc_info:
            runtime_docker.start(manifest)
        text = str(exc_info.value)
        assert expected_token in text
        assert "inspect docker logs" not in text.lower()
        assert "查看 docker 日志" not in text.lower()
        assert "--log-driver" in seen_commands[-1]
        assert seen_commands[-1][seen_commands[-1].index("--log-driver") + 1] == "none"

    # Verify update() passes capture_output=True and converts CalledProcessError / TimeoutExpired to LifecycleError
    update_kwargs: list[dict] = []

    def failing_update_run(cmd, **kwargs):
        update_kwargs.append(kwargs)
        raise subprocess.CalledProcessError(1, cmd, stderr="manifest unknown")

    monkeypatch.setattr(runtime_docker.subprocess, "run", failing_update_run)
    with pytest.raises(LifecycleError):
        runtime_docker.update(manifest)
    assert update_kwargs[-1].get("capture_output") is True

    monkeypatch.setattr(
        runtime_docker.subprocess,
        "run",
        lambda cmd, **_kw: (_ for _ in ()).throw(subprocess.TimeoutExpired(cmd, 600)),
    )
    with pytest.raises(LifecycleError) as timeout_exc:
        runtime_docker.update(manifest)
    assert timeout_exc.value.code == "docker_unresponsive"


def test_t6_docker_inspect_and_rm_timeouts_report_docker_unresponsive(tmp_path, monkeypatch):
    """T6 (Change 5): _docker_state catches TimeoutExpired and returns status='docker_unresponsive' without raising; rm -f TimeoutExpired converts to LifecycleError."""
    monkeypatch.setattr(runtime_docker.shutil, "which", lambda _n: "docker")

    def timeout_inspect(cmd, **_kw):
        raise subprocess.TimeoutExpired(cmd, 15)

    monkeypatch.setattr(runtime_docker.subprocess, "run", timeout_inspect)
    st = runtime_docker._docker_state("pkg-timeout")
    assert st["running"] is False
    assert st["status"] == "docker_unresponsive"

    model = tmp_path / "m.gguf"
    model.write_bytes(b"GGUF" + bytes(96))
    manifest = LLMPackageManifest(
        package_id="pkg-rm-timeout",
        mode="managed",
        public_model_alias="alias",
        runtime=runtime_docker.RUNTIME_ID,
        model="alias",
        model_path=str(model),
        checksum=fingerprint_file(model),
        base_url="http://127.0.0.1:18080",
    )
    monkeypatch.setattr(runtime_docker, "_docker", lambda: "docker")
    monkeypatch.setattr(runtime_docker, "_image_present", lambda *_a: True)

    def timeout_rm(cmd, **_kw):
        if cmd[1:3] == ["rm", "-f"]:
            raise subprocess.TimeoutExpired(cmd, 30)
        return subprocess.CompletedProcess(cmd, 0, "", "")

    monkeypatch.setattr(runtime_docker.subprocess, "run", timeout_rm)
    with pytest.raises(LifecycleError) as exc_info:
        runtime_docker.start(manifest)
    assert exc_info.value.code == "docker_unresponsive"


def test_t7_port_auto_fallback_and_wait_healthy_size_scaling_and_distinct_reasons(tmp_path, monkeypatch):
    """T7 (Change 6): Occupying 18080 causes default port=None to pick a free port into manifest; wait_healthy scales timeout (120s + 30s/GB capped at 600s) and distinguishes exited(category), port occupied by other, and still loading timeout."""
    # 1) Port 18080 occupied -> picks a free port != 18080 when port is None
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as blocker:
        try:
            blocker.bind(("127.0.0.1", 18080))
            blocker.listen(1)
            occupied_18080 = True
        except OSError:
            occupied_18080 = False
        chosen = lifecycle._select_managed_port(None, root=tmp_path / "llm", package_id="auto-port")
        if occupied_18080:
            assert chosen != 18080
            assert 1024 <= chosen <= 65535

    # Also verify force-occupied 18080 via monkeypatch when 18080 was already bound or free on host
    monkeypatch.setattr(lifecycle, "_port_in_use", lambda p: p == 18080)
    chosen_patched = lifecycle._select_managed_port(None, root=tmp_path / "llm", package_id="auto-port-2")
    assert chosen_patched != 18080
    assert lifecycle._select_managed_port(19123, root=tmp_path / "llm", package_id="explicit") == 19123

    # Verify install_managed default signature has port=None and writes the selected port to manifest
    payload = b"GGUF" + bytes(96)
    digest = hashlib.sha256(payload).hexdigest()
    monkeypatch.setattr(
        lifecycle,
        "_download",
        lambda _u, dest, sha, **_kw: (dest.parent.mkdir(parents=True, exist_ok=True), dest.write_bytes(payload), sha)[2],
    )
    monkeypatch.setattr(runtime_native, "available", lambda root=None: (True, ""))
    monkeypatch.setattr(runtime_native, "prepare", lambda **_kw: None)
    monkeypatch.setattr(runtime_native, "start", lambda _m: None)
    monkeypatch.setattr(lifecycle, "wait_healthy", lambda _m: {"ok": True})
    monkeypatch.setattr(lifecycle, "self_test", lambda _m: {"ok": True})

    installed = lifecycle.install_managed(
        package_id="auto-port-pkg",
        root=tmp_path / "llm",
        model_url="https://huggingface.co/example/m/resolve/main/m.gguf",
        expected_sha256=digest,
        accept_risk=True,
        runtime="native",
    )
    saved_mf = load_manifest(installed["manifest"])
    assert saved_mf.base_url != "http://127.0.0.1:18080"

    # 2) Timeout scaling: 120s + 30s/GiB capped at 600s
    m_small = tmp_path / "small.gguf"
    m_small.write_bytes(b"GGUF")
    mf = LLMPackageManifest(
        package_id="scale-pkg",
        mode="managed",
        public_model_alias="alias",
        runtime=runtime_native.RUNTIME_ID,
        model="alias",
        model_path=str(m_small),
        base_url="http://127.0.0.1:18999",
        runtime_dir=str(tmp_path / "rt"),
    )
    assert 120.0 <= lifecycle._default_health_timeout(mf) < 121.0

    orig_stat = Path.stat
    monkeypatch.setattr(
        Path,
        "stat",
        lambda self, *a, **kw: type("S", (), {"st_size": 4 * (1024 ** 3), "st_mode": 0o100644})()
        if self == m_small
        else orig_stat(self, *a, **kw),
    )
    assert lifecycle._default_health_timeout(mf) == pytest.approx(240.0)

    monkeypatch.setattr(
        Path,
        "stat",
        lambda self, *a, **kw: type("S", (), {"st_size": 30 * (1024 ** 3), "st_mode": 0o100644})()
        if self == m_small
        else orig_stat(self, *a, **kw),
    )
    assert lifecycle._default_health_timeout(mf) == 600.0
    monkeypatch.setattr(Path, "stat", orig_stat)


def test_t7b_wait_healthy_distinguishes_exited_port_occupied_and_loading_timeout(tmp_path, monkeypatch):
    """T7b (Change 6): wait_healthy reports distinct messages for (a) process exited with category, (b) port occupied by another program, and (c) model still loading on timeout."""
    from rynmesh.llm_package import lifecycle as real_lifecycle

    rt_root = tmp_path / "rt"
    (rt_root / "runtime").mkdir(parents=True, exist_ok=True)
    model = tmp_path / "m.gguf"
    model.write_bytes(b"GGUF" + bytes(64))

    # Case A: Runtime process exited with category out_of_memory
    (rt_root / "runtime" / "exited-pkg.log").write_text(
        "runtime_process_starting\nruntime_process_exited code=1 category=out_of_memory\n",
        encoding="utf-8",
    )
    mf_exited = LLMPackageManifest(
        package_id="exited-pkg",
        mode="managed",
        public_model_alias="my-alias",
        runtime=runtime_native.RUNTIME_ID,
        model="my-alias",
        model_path=str(model),
        base_url="http://127.0.0.1:19871",
        runtime_dir=str(rt_root),
    )
    monkeypatch.setattr(runtime_native, "state", lambda _m: {"installed": True, "running": False, "status": "stopped"})
    monkeypatch.setattr(real_lifecycle, "_port_in_use", lambda _p: False)
    with pytest.raises(LifecycleError) as exited_err:
        real_lifecycle.wait_healthy(mf_exited, timeout_s=0.05)
    assert "退出" in str(exited_err.value)
    assert "out_of_memory" in str(exited_err.value)

    # Case B: Port occupied by another program (unrelated HTTP server answering on port with wrong model)
    class OtherHandler(BaseHTTPRequestHandler):
        def log_message(self, *_a):
            return

        def do_GET(self):  # noqa: N802
            body = json.dumps({"data": [{"id": "unrelated-other-service"}]}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    other_srv = ThreadingHTTPServer(("127.0.0.1", 0), OtherHandler)
    t = threading.Thread(target=other_srv.serve_forever, daemon=True)
    t.start()
    try:
        mf_port = LLMPackageManifest(
            package_id="port-pkg",
            mode="managed",
            public_model_alias="expected-ryn-model",
            runtime=runtime_native.RUNTIME_ID,
            model="expected-ryn-model",
            model_path=str(model),
            base_url=f"http://127.0.0.1:{other_srv.server_port}",
            runtime_dir=str(rt_root),
        )
        monkeypatch.setattr(runtime_native, "state", lambda _m: {"installed": True, "running": False, "status": "stopped"})
        monkeypatch.setattr(real_lifecycle, "_port_in_use", lambda _p: True)
        with pytest.raises(LifecycleError) as port_err:
            real_lifecycle.wait_healthy(mf_port, timeout_s=0.05)
        assert "占用" in str(port_err.value)
    finally:
        other_srv.shutdown()
        other_srv.server_close()

    # Case C: Owned runtime process is still running & loading weights when timeout expires
    mf_loading = LLMPackageManifest(
        package_id="loading-pkg",
        mode="managed",
        public_model_alias="big-model",
        runtime=runtime_native.RUNTIME_ID,
        model="big-model",
        model_path=str(model),
        base_url="http://127.0.0.1:19873",
        runtime_dir=str(rt_root),
    )
    monkeypatch.setattr(runtime_native, "state", lambda _m: {"installed": True, "running": True, "status": "running"})
    monkeypatch.setattr(real_lifecycle, "_port_in_use", lambda _p: False)
    with pytest.raises(LifecycleError) as loading_err:
        real_lifecycle.wait_healthy(mf_loading, timeout_s=0.05)
    assert "加载" in str(loading_err.value)
    assert "超时" in str(loading_err.value)


def test_t8_lifecycle_error_codes_and_route_error_code_details(tmp_path, monkeypatch):
    """T8 (Change 7): docker_not_installed, docker_engine_not_running, runtime_archive_corrupt, runtime_unavailable_for_platform, runtime_state_unwritable carry exact .code, and routes include error_code in detail."""
    # 1) docker_not_installed
    monkeypatch.setattr(runtime_docker.shutil, "which", lambda _n: None)
    with pytest.raises(LifecycleError) as e_no_docker:
        runtime_docker._docker()
    assert e_no_docker.value.code == "docker_not_installed"

    # 2) docker_engine_not_running
    monkeypatch.setattr(runtime_docker.shutil, "which", lambda _n: "docker")
    monkeypatch.setattr(
        runtime_docker.subprocess,
        "run",
        lambda cmd, **_kw: (_ for _ in ()).throw(subprocess.CalledProcessError(1, cmd)),
    )
    with pytest.raises(LifecycleError) as e_daemon:
        runtime_docker._docker()
    assert e_daemon.value.code == "docker_engine_not_running"

    # 3) runtime_archive_corrupt
    corrupt_zip = tmp_path / "corrupt.zip"
    corrupt_zip.write_bytes(b"not-a-valid-zip-stream")
    with pytest.raises(LifecycleError) as e_corrupt:
        runtime_native_install._extract(corrupt_zip, tmp_path / "extract-dest")
    assert e_corrupt.value.code == "runtime_archive_corrupt"

    # 4) runtime_unavailable_for_platform
    monkeypatch.setattr(runtime_native_install, "asset", lambda: None)
    with pytest.raises(LifecycleError) as e_unavail:
        runtime_native_install.download(tmp_path / "no-asset")
    assert e_unavail.value.code == "runtime_unavailable_for_platform"

    # 5) Route response bodies (/api/local/llm/setup, /api/local/llm/cli-services/{kind}/setup, /api/local/llm/service/actions/{action}) include error_code in detail
    home = tmp_path / "node"
    mf_path = home / "llm" / "configured.json"
    save_manifest(
        LLMPackageManifest(
            package_id="cfg",
            mode="managed",
            public_model_alias="cfg",
            runtime=runtime_docker.RUNTIME_ID,
            base_url="http://127.0.0.1:18080",
        ),
        mf_path,
    )
    (home / "llm" / "provider-settings.json").write_text(
        json.dumps({"manifest": str(mf_path), "publication_enabled": False}),
        encoding="utf-8",
    )

    app = _make_app(home, tmp_path / "net")
    monkeypatch.setattr(
        routes,
        "install_managed",
        lambda **_kw: (_ for _ in ()).throw(LifecycleError("Docker is not installed", code="docker_not_installed")),
    )
    monkeypatch.setattr(
        routes,
        "connect_cli",
        lambda **_kw: (_ for _ in ()).throw(LifecycleError("unable to write runtime state", code="runtime_state_unwritable")),
    )
    monkeypatch.setattr(
        routes,
        "update_runtime",
        lambda *_a, **_kw: (_ for _ in ()).throw(LifecycleError("runtime archive is unreadable or corrupt", code="runtime_archive_corrupt")),
    )
    with TestClient(app) as client:
        r_setup = client.post("/api/local/llm/setup", json={"mode": "managed"})
        assert r_setup.status_code == 409
        assert r_setup.json()["detail"]["error_code"] == "docker_not_installed"

        r_cli = client.post("/api/local/llm/cli-services/codex_cli/setup")
        assert r_cli.status_code == 409
        assert r_cli.json()["detail"]["error_code"] == "runtime_state_unwritable"

        r_act = client.post("/api/local/llm/service/actions/update", json={})
        assert r_act.status_code == 409
        assert r_act.json()["detail"]["error_code"] == "runtime_archive_corrupt"
    app.state.native_model.close()


def test_t9_setup_recovery_failures_carry_setup_recovery_failed_code(tmp_path, monkeypatch):
    """T9 (Change 7): SetupRecovery raises SetupRecoveryError with code='setup_recovery_failed' across _read, capture, and restore, and /api/local/llm/setup returns error_code='setup_recovery_failed'."""
    home = tmp_path / "rec-home"
    rec = setup_recovery.SetupRecovery(home)
    mf = tmp_path / "prev.json"
    mf.write_bytes(b'{"ok": true}')
    rec.capture(str(mf))

    # 1) capture() while pending -> code == 'setup_recovery_failed'
    with pytest.raises(setup_recovery.SetupRecoveryError) as cap_err:
        rec.capture(str(mf))
    assert cap_err.value.code == "setup_recovery_failed"

    # 2) restore() write failure -> code == 'setup_recovery_failed'
    monkeypatch.setattr(
        setup_recovery,
        "atomic_write_bytes",
        lambda *_a, **_kw: (_ for _ in ()).throw(PermissionError("denied")),
    )
    with pytest.raises(setup_recovery.SetupRecoveryError) as rest_err:
        rec.restore()
    assert rest_err.value.code == "setup_recovery_failed"

    # 3) HTTP /api/local/llm/setup surfaces error_code == 'setup_recovery_failed' when restore fails
    app = _make_app(home, tmp_path / "rec-net")
    # Re-create pending journal after startup restore attempt
    rec2 = setup_recovery.SetupRecovery(home)
    if not rec2.pending:
        monkeypatch.undo()
        rec2.capture(str(mf))
        monkeypatch.setattr(
            setup_recovery,
            "atomic_write_bytes",
            lambda *_a, **_kw: (_ for _ in ()).throw(PermissionError("denied")),
        )
    app_home2 = tmp_path / "route-rec-home"
    rec_route = setup_recovery.SetupRecovery(app_home2)
    monkeypatch.undo()
    rec_route.capture(str(mf))
    app2 = _make_app(app_home2, tmp_path / "rec-net-2")
    # Make setup_recovery.pending = True and restore() fail during POST /api/local/llm/setup
    rec_route.capture(str(mf))
    monkeypatch.setattr(
        setup_recovery,
        "atomic_write_bytes",
        lambda *_a, **_kw: (_ for _ in ()).throw(PermissionError("denied")),
    )
    with TestClient(app2) as client:
        # Ensure the route's setup_recovery sees pending=True by touching setup-recovery.json and triggering restore
        resp = client.post("/api/local/llm/setup", json={"mode": "managed"})
        assert resp.status_code == 409
        assert resp.json()["detail"]["error_code"] == "setup_recovery_failed"
    app.state.native_model.close()
    app2.state.native_model.close()
