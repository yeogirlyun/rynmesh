"""S01-I33 regression tests for local runtime process handling, hash caching, policy eviction, and hardware probes."""
from __future__ import annotations

import ctypes
import json
import os
import socket
import subprocess
from pathlib import Path

from test_native_model import tiny_model, wait

import rynmesh.llm_package.hardware as llm_hardware
import rynmesh.llm_package.manifest as llm_manifest
import rynmesh.llm_package.native_model as llm_native_model
import rynmesh.llm_package.native_policy as llm_native_policy
import rynmesh.llm_package.runtime_docker as llm_runtime_docker
import rynmesh.llm_package.runtime_native as llm_runtime_native
from rynmesh.llm_package.manifest import LLMPackageManifest

CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)
CREATE_NEW_PROCESS_GROUP = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0x00000200)


def test_windows_alive_uses_ctypes_without_tasklist_and_falls_back_to_port(monkeypatch):
    """T1 (Change 2): Windows _alive uses ctypes OpenProcess/GetExitCodeProcess/QueryFullProcessImageNameW, never tasklist, and falls back to port on API error."""
    monkeypatch.setattr(llm_runtime_native.os, "name", "nt")

    def forbid_subprocess(*_args, **_kwargs):
        raise AssertionError("tasklist / subprocess.run must never be called in Windows _alive")

    monkeypatch.setattr(llm_runtime_native.subprocess, "run", forbid_subprocess)

    state = {
        "open_ok": True,
        "last_error": 0,
        "exit_ok": True,
        "exit_code": 259,  # STILL_ACTIVE
        "image_ok": True,
        "image_path": r"D:\runtime\llama-server.exe",
        "closed_handles": [],
    }

    class FakeKernel32:
        def OpenProcess(self, access, inherit, pid):  # noqa: N802
            assert access == 0x1000  # PROCESS_QUERY_LIMITED_INFORMATION
            if not state["open_ok"]:
                return None
            return 0x123456789

        def GetExitCodeProcess(self, handle, out_ptr):  # noqa: N802
            if not state["exit_ok"]:
                return 0
            ctypes.cast(out_ptr, ctypes.POINTER(ctypes.c_ulong))[0] = state["exit_code"]
            return 1

        def QueryFullProcessImageNameW(self, handle, flags, buf, size_ptr):  # noqa: N802
            if not state["image_ok"]:
                return 0
            text = state["image_path"]
            ctypes.memmove(buf, ctypes.create_unicode_buffer(text), (len(text) + 1) * ctypes.sizeof(ctypes.c_wchar))
            ctypes.cast(size_ptr, ctypes.POINTER(ctypes.c_ulong))[0] = len(text)
            return 1

        def CloseHandle(self, handle):  # noqa: N802
            state["closed_handles"].append(handle)
            return 1

    monkeypatch.setattr(llm_runtime_native, "_win32_kernel32", lambda: FakeKernel32())
    monkeypatch.setattr(llm_runtime_native, "_win32_last_error", lambda: state["last_error"])

    # Active process matching server_name
    assert llm_runtime_native._alive(4321, "llama-server.exe") is True
    assert state["closed_handles"] == [0x123456789]

    # Active process with mismatched image name (PID reuse)
    state["image_path"] = r"C:\Windows\System32\notepad.exe"
    assert llm_runtime_native._alive(4321, "llama-server.exe") is False

    # Exited process
    state["image_path"] = r"D:\runtime\llama-server.exe"
    state["exit_code"] = 0
    assert llm_runtime_native._alive(4321, "llama-server.exe") is False

    # Non-existent PID (ERROR_INVALID_PARAMETER = 87) -> False even if port open
    state["open_ok"] = False
    state["last_error"] = 87
    monkeypatch.setattr(llm_runtime_native, "_port_open", lambda port: True)
    assert llm_runtime_native._alive(4321, "llama-server.exe", port=18080) is False

    # Win32 API failure (e.g. ACCESS_DENIED = 5): port reachable -> True, port unreachable -> False
    state["last_error"] = 5
    monkeypatch.setattr(llm_runtime_native, "_port_open", lambda port: port == 18080)
    assert llm_runtime_native._alive(4321, "llama-server.exe", port=18080) is True
    assert llm_runtime_native._alive(4321, "llama-server.exe", port=18081) is False


def test_quiet_creationflags_applied_to_every_windows_subprocess_call(tmp_path, monkeypatch):
    """T2 (Change 1): _quiet() returns CREATE_NO_WINDOW on Windows and appears on every subprocess.run/Popen in runtime_native and runtime_docker."""
    monkeypatch.setattr(llm_runtime_native.os, "name", "nt")
    monkeypatch.setattr(llm_runtime_docker.os, "name", "nt")

    assert llm_runtime_native._quiet() == {"creationflags": CREATE_NO_WINDOW}
    assert llm_runtime_docker._quiet() == {"creationflags": CREATE_NO_WINDOW}

    native_runs: list[tuple[list[str], int]] = []
    native_popens: list[tuple[list[str], int]] = []

    def fake_native_run(cmd, **kwargs):
        native_runs.append((list(cmd), int(kwargs.get("creationflags", 0))))
        return subprocess.CompletedProcess(cmd, 0, "", "")

    class DummyPopen:
        pid = 5555

        def poll(self):
            return None

        def terminate(self):
            pass

        def wait(self, timeout=None):
            return 0

        def kill(self):
            pass

    def fake_native_popen(cmd, **kwargs):
        native_popens.append((list(cmd), int(kwargs.get("creationflags", 0))))
        return DummyPopen()

    monkeypatch.setattr(llm_runtime_native.subprocess, "run", fake_native_run)
    monkeypatch.setattr(llm_runtime_native.subprocess, "Popen", fake_native_popen)
    monkeypatch.setattr(llm_runtime_native, "_port_open", lambda _port: True)

    server = tmp_path / "llama-server.exe"
    server.write_bytes(b"MZ")
    llm_runtime_native._spawn(server, [str(server)], tmp_path / "root", "pkg", 18080)
    assert native_popens and native_popens[-1][1] == (CREATE_NEW_PROCESS_GROUP | CREATE_NO_WINDOW)

    llm_runtime_native._terminate(5555, force=True)
    assert native_runs and native_runs[-1][1] == CREATE_NO_WINDOW

    docker_flags: list[tuple[list[str], int]] = []

    def fake_docker_run(cmd, **kwargs):
        docker_flags.append((list(cmd), int(kwargs.get("creationflags", 0))))
        if "image" in cmd and "inspect" in cmd:
            return subprocess.CompletedProcess(cmd, 1, "", "")
        if "inspect" in cmd:
            return subprocess.CompletedProcess(cmd, 0, '{"Running":true,"Status":"running"}', "")
        return subprocess.CompletedProcess(cmd, 0, "27.0", "")

    class ImmediatePopen:
        returncode = 0

        def poll(self):
            return 0

        def kill(self):
            pass

    def fake_docker_popen(cmd, **kwargs):
        docker_flags.append((list(cmd), int(kwargs.get("creationflags", 0))))
        return ImmediatePopen()

    monkeypatch.setattr(llm_runtime_docker.shutil, "which", lambda _name: "docker.exe")
    monkeypatch.setattr(llm_runtime_docker.subprocess, "run", fake_docker_run)
    monkeypatch.setattr(llm_runtime_docker.subprocess, "Popen", fake_docker_popen)

    model_file = tmp_path / "m.gguf"
    model_file.write_bytes(b"GGUF" + bytes(64))
    manifest = LLMPackageManifest(
        package_id="docker-quiet",
        mode="managed",
        public_model_alias="alias",
        runtime=llm_runtime_docker.RUNTIME_ID,
        model="alias",
        model_path=str(model_file),
        checksum=llm_manifest.fingerprint_file(model_file),
        base_url="http://127.0.0.1:18090",
    )
    llm_runtime_docker.start(manifest)
    llm_runtime_docker.state(manifest)
    llm_runtime_docker.stop(manifest)
    llm_runtime_docker.remove(manifest)
    llm_runtime_docker.update(manifest)

    assert len(docker_flags) >= 8
    assert all(flags == CREATE_NO_WINDOW for _, flags in docker_flags)


def test_windows_stop_pid_waits_two_seconds_before_force_kill(monkeypatch):
    """T3 (Change 3): On Windows, _stop_pid waits 2 s before taskkill /F; POSIX keeps 10 s."""
    clock = [100.0]
    terminations: list[tuple[int, bool, float]] = []
    force_killed = [False]

    monkeypatch.setattr(llm_runtime_native.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(llm_runtime_native.time, "sleep", lambda dt: clock.__setitem__(0, clock[0] + dt))
    monkeypatch.setattr(llm_runtime_native, "_alive", lambda pid, *_a, **_k: not force_killed[0])

    def fake_terminate(pid, *, force):
        terminations.append((pid, force, round(clock[0] - 100.0, 2)))
        if force:
            force_killed[0] = True

    monkeypatch.setattr(llm_runtime_native, "_terminate", fake_terminate)

    monkeypatch.setattr(llm_runtime_native.os, "name", "nt")
    assert llm_runtime_native._stop_pid(777) is True
    assert len(terminations) == 2
    assert terminations[0] == (777, False, 0.0)
    assert terminations[1][1] is True
    assert 1.95 <= terminations[1][2] <= 2.25

    # Verify POSIX still waits 10 s before force kill
    clock[0] = 100.0
    terminations.clear()
    force_killed[0] = False
    monkeypatch.setattr(llm_runtime_native.os, "name", "posix")
    assert llm_runtime_native._stop_pid(888) is True
    assert terminations[1][1] is True
    assert 9.95 <= terminations[1][2] <= 10.25


def test_cached_fingerprint_reuses_sidecar_until_mtime_or_size_changes(tmp_path, monkeypatch):
    """T4 (Change 4 & Change 6): cached_fingerprint skips hashing on unchanged size+mtime_ns and recomputes when mtime changes; runtime_native & runtime_docker use it and Docker CPUs honor max(1, cpu_count-1) + override."""
    model = tmp_path / "weights.gguf"
    model.write_bytes(b"GGUF-PAYLOAD-V1")

    hash_calls: list[Path] = []
    real_sha = llm_manifest.sha256_file

    def counting_sha(path, *, chunk_size=1024 * 1024):
        hash_calls.append(Path(path))
        return real_sha(path, chunk_size=chunk_size)

    monkeypatch.setattr(llm_manifest, "sha256_file", counting_sha)

    first = llm_manifest.cached_fingerprint(model)
    sidecar = tmp_path / "weights.gguf.ryn-sha256.json"
    assert sidecar.is_file()
    saved_meta = json.loads(sidecar.read_text(encoding="utf-8"))
    assert set(saved_meta) == {"size", "mtime_ns", "sha256"}
    assert len(hash_calls) == 1

    # Second call with unchanged size + mtime_ns does NOT recompute sha256_file
    second = llm_manifest.cached_fingerprint(model)
    assert second == first
    assert len(hash_calls) == 1

    # runtime_native.start and runtime_docker.start use cached_fingerprint (no extra sha256_file calls)
    server = tmp_path / "llama-server.exe"
    server.write_bytes(b"MZ")
    monkeypatch.setattr(llm_runtime_native, "resolve_server", lambda _root=None: server)
    monkeypatch.setattr(llm_runtime_native, "_endpoint_serves_alias", lambda _m: True)
    native_manifest = LLMPackageManifest(
        package_id="cached-native",
        mode="managed",
        public_model_alias="alias",
        runtime=llm_runtime_native.RUNTIME_ID,
        model="alias",
        model_path=str(model),
        checksum=first,
        base_url="http://127.0.0.1:18091",
        runtime_dir=str(tmp_path / "rt"),
    )
    llm_runtime_native.start(native_manifest)
    assert len(hash_calls) == 1

    docker_cmds: list[list[str]] = []
    monkeypatch.setattr(llm_runtime_docker, "_docker", lambda: "docker")
    monkeypatch.setattr(llm_runtime_docker, "_image_present", lambda *_a: True)
    monkeypatch.setattr(
        llm_runtime_docker.subprocess,
        "run",
        lambda cmd, **_kw: (docker_cmds.append(list(cmd)), subprocess.CompletedProcess(cmd, 0, "", ""))[1],
    )
    monkeypatch.setattr(llm_runtime_docker.os, "cpu_count", lambda: 8)
    docker_manifest = LLMPackageManifest(
        package_id="cached-docker",
        mode="managed",
        public_model_alias="alias",
        runtime=llm_runtime_docker.RUNTIME_ID,
        model="alias",
        model_path=str(model),
        checksum=first,
        base_url="http://127.0.0.1:18092",
    )
    llm_runtime_docker.start(docker_manifest)
    assert len(hash_calls) == 1
    run_cmd = docker_cmds[-1]
    assert run_cmd[run_cmd.index("--cpus") + 1] == "7"

    docker_manifest.hardware_requirements = {"cpus": 3}
    llm_runtime_docker.start(docker_manifest)
    override_cmd = docker_cmds[-1]
    assert override_cmd[override_cmd.index("--cpus") + 1] == "3"

    # Changing mtime forces recomputation
    st = model.stat()
    os.utime(model, ns=(st.st_atime_ns, st.st_mtime_ns + 1_000_000_000))
    third = llm_manifest.cached_fingerprint(model)
    assert third == first
    assert len(hash_calls) == 2


def test_engine_verified_json_rehashes_only_changed_file_and_writes_atomically(tmp_path, monkeypatch):
    """T5 (Change 4): native_model._engine records size+mtime_ns in verified.json, rehashes only changed files, and uses atomic_write_json."""
    home = tmp_path / "home"
    model = llm_native_model.NativeModel(home)
    backend = "windows-cpu"
    exe_name = "llama-server.exe" if os.name == "nt" else "llama-server"
    runtime_dir = model.root / "runtime" / llm_native_model.ENGINE_VERSION / backend

    def fake_download(_url, target, *_args):
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b"archive")

    def fake_extract(_archive, destination):
        destination.mkdir(parents=True, exist_ok=True)
        (destination / exe_name).write_bytes(b"EXE-BINARY")
        (destination / "ggml.dll").write_bytes(b"GGML-LIB")
        (destination / "cuda.dll").write_bytes(b"CUDA-LIB")

    atomic_writes: list[Path] = []
    real_atomic_write = llm_native_model.atomic_write_json

    def spy_atomic_write(path, payload, **kwargs):
        atomic_writes.append(Path(path))
        return real_atomic_write(path, payload, **kwargs)

    monkeypatch.setattr(llm_native_model, "download", fake_download)
    monkeypatch.setattr(llm_native_model, "extract_runtime", fake_extract)
    monkeypatch.setattr(llm_native_model, "atomic_write_json", spy_atomic_write)

    resolved = model._engine(backend)
    assert resolved == runtime_dir / exe_name
    index_path = runtime_dir / "verified.json"
    assert index_path in atomic_writes

    verified = json.loads(index_path.read_text(encoding="utf-8"))
    assert set(verified.keys()) == {exe_name, "ggml.dll", "cuda.dll"}
    for entry in verified.values():
        assert isinstance(entry, dict)
        assert {"sha256", "size", "mtime_ns"} <= set(entry.keys())

    hashed_files: list[str] = []
    real_digest = llm_native_model.digest_file

    def spy_digest(path: Path, cancel=lambda: False):
        hashed_files.append(path.name)
        return real_digest(path, cancel)

    monkeypatch.setattr(llm_native_model, "digest_file", spy_digest)

    # Unchanged files -> zero digest_file calls
    atomic_writes.clear()
    assert model._engine(backend) == runtime_dir / exe_name
    assert hashed_files == []
    assert atomic_writes == []

    # Touch mtime on ONLY ggml.dll -> only ggml.dll is rehashed and verified.json is atomically updated
    ggml = runtime_dir / "ggml.dll"
    st = ggml.stat()
    os.utime(ggml, ns=(st.st_atime_ns, st.st_mtime_ns + 2_000_000_000))
    assert model._engine(backend) == runtime_dir / exe_name
    assert hashed_files == ["ggml.dll"]
    assert atomic_writes == [index_path]


def test_native_model_start_retries_once_when_port_is_occupied_on_exit(tmp_path, monkeypatch):
    """Change 5: _start retries once on a fresh port if child exits during startup and chosen port is occupied by another listener."""
    home = tmp_path / "home"
    model = llm_native_model.NativeModel(home)
    exe = tmp_path / ("llama-server.exe" if os.name == "nt" else "llama-server")
    exe.write_bytes(b"EXE")

    blocker = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    blocker.bind(("127.0.0.1", 0))
    blocker.listen(1)
    occupied_port = blocker.getsockname()[1]

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as free_sock:
        free_sock.bind(("127.0.0.1", 0))
        second_port = free_sock.getsockname()[1]

    chosen_ports = iter([occupied_port, second_port])
    spawned_ports: list[int] = []

    class ExitedProc:
        def poll(self):
            return 1

        def kill(self):
            pass

        def wait(self, timeout=None):
            return 1

    class HealthyProc:
        def poll(self):
            return None

        def kill(self):
            pass

        def wait(self, timeout=None):
            return 0

    class FakeUpstream:
        def __init__(self, base_url, **_kwargs):
            self.port = int(base_url.rsplit(":", 1)[1])

        def health(self):
            return {"ok": self.port == second_port}

        def infer(self, **_kwargs):
            return {"text": "OK"}

        def shutdown(self):
            pass

    monkeypatch.setattr(llm_native_model, "_pick_loopback_port", lambda: next(chosen_ports))
    if os.name == "nt":
        import rynmesh.llm_package.process_guard as pg

        monkeypatch.setattr(pg, "_windows_job", lambda *_a, **_k: None)
    monkeypatch.setattr(llm_native_model, "_kill_process_handle", lambda _p: None, raising=False)
    monkeypatch.setattr(model, "_kill", lambda: None)

    def fake_popen(cmd, **_kwargs):
        port_arg = int(cmd[cmd.index("--port") + 1])
        spawned_ports.append(port_arg)
        return ExitedProc() if port_arg == occupied_port else HealthyProc()

    monkeypatch.setattr(llm_native_model.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(llm_native_model, "OpenAICompatibleAdapter", FakeUpstream)

    try:
        model._start(exe, "windows-cpu")
    finally:
        blocker.close()

    assert spawned_ports == [occupied_port, second_port]


def test_policy_table_evicts_oldest_non_current_row_at_capacity(tmp_path):
    """T6 (Change 7): NativeModelPolicy.ensure evicts the non-current row with the oldest day instead of raising at 128 rows."""
    policy = llm_native_policy.NativeModelPolicy(tmp_path)
    seed_rows = {}
    for idx in range(128):
        sid = f"pkg-{idx:03d}"
        day = "2024-01-05" if idx == 42 else f"2026-06-{(idx % 28) + 1:02d}"
        seed_rows[sid] = {
            "configuration": {
                "max_output_tokens": 512,
                "max_concurrent": 1,
                "requests_per_day": None,
                "tokens_per_day": None,
                "shared_tokens_per_day": None,
            },
            "model_generation": f"gen-{idx}",
            "rule_revision": f"rev-{idx}",
            "sharing": False,
            "day": day,
            "requests": 1,
        }
    policy._save(seed_rows)

    new_manifest = LLMPackageManifest(
        package_id="pkg-new-129",
        mode="managed",
        public_model_alias="New Model",
        max_output_tokens=1024,
        context_window=4096,
    )
    ensured = policy.ensure(new_manifest, False)
    assert ensured["configuration"]["max_output_tokens"] == 1024
    saved = policy._read()
    assert len(saved) == 128
    assert "pkg-042" not in saved
    assert "pkg-new-129" in saved


def test_uninstall_removes_policy_row_from_policy_store(tmp_path, monkeypatch):
    """T7 (Change 7): NativeModelPolicy.remove deletes row, and NativeModel uninstall removes old package_id row."""
    gguf = tiny_model(tmp_path)
    model = llm_native_model.NativeModel(tmp_path / "home")
    monkeypatch.setattr(llm_native_model, "platform_key", lambda: "windows-cpu")
    monkeypatch.setattr(llm_native_model, "_memory", lambda: (16000, 8000))
    monkeypatch.setattr(llm_native_model, "_nvidia", lambda: ([], ""))
    monkeypatch.setattr(model, "_engine", lambda _b: Path("engine"))
    monkeypatch.setattr(model, "_start", lambda *_a: None)

    model.action("import", source=str(gguf))
    wait(model)
    installed_pkg_id = model.manifest.package_id
    assert model.policies.current(installed_pkg_id) is not None

    model.action("uninstall")
    wait(model)
    assert model.policies.current(installed_pkg_id) is None


def test_unsupported_platform_status_returns_connect_local_api_alternative_and_unlocks_owner_lock(
    tmp_path, monkeypatch
):
    """T8 (Change 8 & Change 9): status() includes alternatives=['connect_local_api'] when supported=False and runs policies.current / usage_store.public_usage outside owner.lock."""
    model = llm_native_model.NativeModel(tmp_path / "home")
    monkeypatch.setattr(llm_native_model.platform, "system", lambda: "Linux")
    monkeypatch.setattr(llm_native_model.platform, "machine", lambda: "x86_64")

    lock_owned_checks: list[bool] = []
    real_current = model.policies.current
    real_usage = model.usage_store.public_usage

    def check_current(service_id):
        lock_owned_checks.append(model.lock._is_owned())
        return real_current(service_id)

    def check_usage(*args, **kwargs):
        lock_owned_checks.append(model.lock._is_owned())
        return real_usage(*args, **kwargs)

    monkeypatch.setattr(model.policies, "current", check_current)
    monkeypatch.setattr(model.usage_store, "public_usage", check_usage)

    info = model.status()
    assert info["supported"] is False
    assert info["alternatives"] == ["connect_local_api"]
    assert lock_owned_checks == [False, False]


def test_nvidia_bad_line_does_not_clear_gpu_list_and_hardware_probes_cache_and_count_purgeable(
    tmp_path, monkeypatch
):
    """T9 (Change 10): One [N/A] line in nvidia-smi does not clear valid GPUs; _nvidia and docker info cache for 60 s; _parse_vm_stat counts Pages purgeable."""
    vm_text = (
        "Mach Virtual Memory Statistics: (page size of 16384 bytes)\n"
        "Pages free:                              1000.\n"
        "Pages inactive:                           500.\n"
        "Pages speculative:                        100.\n"
        "Pages purgeable:                          448.\n"
    )
    assert llm_hardware._parse_vm_stat(vm_text) == (2048 * 16384) // 2**20

    now = [1000.0]
    nvidia_calls = [0]
    docker_calls = [0]

    if hasattr(llm_hardware, "_PROBE_CACHE"):
        llm_hardware._PROBE_CACHE.clear()

    monkeypatch.setattr(llm_hardware, "_probe_clock", lambda: now[0], raising=False)
    monkeypatch.setattr(
        llm_hardware.shutil,
        "which",
        lambda name: f"/fake/bin/{name}" if name in {"nvidia-smi", "docker"} else None,
    )

    def fake_run(cmd, **_kwargs):
        if cmd[0].endswith("nvidia-smi"):
            nvidia_calls[0] += 1
            stdout = (
                "NVIDIA GeForce RTX 4090, 24564, 20100, 560.94\n"
                "MIG / Display Adapter, [N/A], [N/A], 560.94\n"
                "NVIDIA RTX A6000, 49140, 45000, 560.94\n"
            )
            return subprocess.CompletedProcess(cmd, 0, stdout=stdout, stderr="")
        if cmd[0].endswith("docker"):
            docker_calls[0] += 1
            return subprocess.CompletedProcess(cmd, 0, stdout='"27.1.1"\n', stderr="")
        raise AssertionError(f"unexpected command: {cmd}")

    monkeypatch.setattr(llm_hardware.subprocess, "run", fake_run)
    monkeypatch.setattr(llm_hardware, "_memory", lambda: (32768, 16384))

    report_1 = llm_hardware.detect_hardware(tmp_path)
    assert [gpu.name for gpu in report_1.nvidia_gpus] == [
        "NVIDIA GeForce RTX 4090",
        "NVIDIA RTX A6000",
    ]
    assert report_1.nvidia_probe == "ok"
    assert report_1.container_available is True
    assert nvidia_calls[0] == 1 and docker_calls[0] == 1

    # Within 60 s: cached, no new subprocess calls
    now[0] += 30.0
    report_2 = llm_hardware.detect_hardware(tmp_path)
    assert len(report_2.nvidia_gpus) == 2 and report_2.container_available is True
    assert nvidia_calls[0] == 1 and docker_calls[0] == 1

    # After 60 s: cache expires and probes run again
    now[0] += 31.0
    llm_hardware.detect_hardware(tmp_path)
    assert nvidia_calls[0] == 2 and docker_calls[0] == 2
