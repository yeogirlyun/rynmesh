"""M1 resource-lease mount tests for `JsRuntimeManager`, `VerifiedJsResourceLease`, and `process_identity`.

Covers:
1. Pure in-memory `JsRuntimeManager()` construction and `get_status()` / `is_js_runtime_available()`
   under poisoned `os.stat`, `os.lstat`, `os.open`, `Path.stat`, `shutil.which`, `subprocess.Popen`,
   and poisoned `PATH` / `RYN_JS_NODE_EXECUTABLE` / `NODE_OPTIONS` / `NODE_PATH`.
2. Production fail-closed behavior with zero spawns when `sys.frozen` is not `True`, when
   `_EMBEDDED_PRODUCTION_PROFILE_SPEC` is `None`, or when a file is replaced and self-hashed in
   `manifest.json` without matching the trusted `expected_manifest_sha256`.
3. Whole-closure handle pinning (all 146 files in the real profile and all files in synthetic profiles,
   including `node/node.exe`, `worker.mjs`, `sky-shim.mjs`, all `package.json`/`package-lock.json`,
   all 4 QuickJS `.wasm` binaries, and licenses) with Windows `FILE_SHARE_READ`-only sharing barrier
   denying write/delete/rename between verify, suspended spawn, `before_resume`, and runtime.
4. Suspended-process pre-resume OS identity verification (`GetProcessId`, `GetProcessTimes`,
   `QueryFullProcessImageNameW`, pinned Node `FileId` / `VolumeSerialNumber` comparison) and zero-resume
   termination when any pre-resume check or hook fails.
5. Strict `ready` handshake validation (`v == 1`, `session_id`, exact `generation`, `engine == 'quickjs-emscripten'`,
   `release_variant == 'RELEASE_SYNC'`, and `pid == verified_process_identity.pid`).
6. Independent per-session lease lifecycle across `cancel_task`, `end_turn`, worker loss, `close_task`,
   and `manager.close()`, plus `sanitized_worker_env` stripping poisoned environment variables.
"""

from __future__ import annotations

import dataclasses
import hashlib
import importlib.util
import io
import json
import os
import shutil
import subprocess
import sys
import threading
from pathlib import Path
from typing import Any

import pytest

import rynmesh.agent_runtime.js_runtime.manager as manager_mod
import rynmesh.agent_runtime.js_runtime.process_identity as pid_mod
from rynmesh.agent_runtime.js_runtime.manager import (
    JsRuntimeDevelopmentConfig,
    JsRuntimeManager,
    is_js_runtime_available,
    resolve_node_executable,
    sanitized_worker_env,
)
from rynmesh.agent_runtime.js_runtime.process_identity import (
    JsProcessIdentityError,
    VerifiedProcessIdentity,
    verify_suspended_process_identity_on_windows,
)
from rynmesh.agent_runtime.js_runtime.protocol import JsRuntimeError
from rynmesh.agent_runtime.js_runtime.resources import (
    BUILD_RECEIPT_FILENAME,
    MANIFEST_FILENAME,
    NODE_EXECUTABLE_REL_PATH_WIN32,
    PACKAGE_JSON_REL_PATH,
    PACKAGE_LOCK_REL_PATH,
    QUICKJS_VARIANT_PACKAGES,
    RELEASE_SYNC_WASM_REL_PATH,
    SKY_SHIM_REL_PATH,
    WORKER_REL_PATH,
    ExpectedJsRuntimeProfileSpec,
    JsResourceError,
    active_resource_lease_count,
    expected_profile_directory_relpaths,
    is_verified_production_lease,
    open_development_js_runtime_lease,
    open_production_js_runtime_lease,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
PREPARED_SNAPSHOT_ROOT = (
    REPO_ROOT
    / "work"
    / "computer-use-parity-20261019"
    / "prepared-js-runtime-independent-98ac78c9d0"
)
PREPARE_SCRIPT_PATH = REPO_ROOT / "webapp" / "src-tauri" / "scripts" / "prepare-js-runtime.py"
TEST_RESOURCES_PATH = REPO_ROOT / "tests" / "test_agent_js_resources.py"


def _resolve_test_dev_profile_root(default_candidate: Path = PREPARED_SNAPSHOT_ROOT) -> Path | None:
    env_override = os.environ.get("RYN_JS_TEST_PROFILE_ROOT", "").strip()
    if env_override:
        explicit_path = Path(env_override)
        if not (explicit_path / MANIFEST_FILENAME).is_file():
            raise RuntimeError(
                f"RYN_JS_TEST_PROFILE_ROOT={env_override!r} does not point to a prepared JS runtime profile with {MANIFEST_FILENAME}"
            )
        return explicit_path
    if (default_candidate / MANIFEST_FILENAME).is_file():
        return default_candidate
    return None


def _load_test_resources_helpers() -> Any:
    spec = importlib.util.spec_from_file_location("test_agent_js_resources_helpers", TEST_RESOURCES_PATH)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


_RES_HELPERS = _load_test_resources_helpers()
_build_fake_source_fixture = _RES_HELPERS._build_fake_source_fixture
prepare_js_runtime_profile = _RES_HELPERS.prepare_js_runtime_profile


class _FakePipe(io.BytesIO):
    def __init__(self, initial_bytes: bytes = b"") -> None:
        super().__init__(initial_bytes)
        self._closed_flag = False

    def close(self) -> None:
        self._closed_flag = True
        super().close()


class _FakeSuspendedPopen:
    """Deterministic fake Popen object for unit-testing pre-resume failure and frame rejection paths."""

    def __init__(
        self,
        *,
        pid: int = 44556,
        handle: int = 0x1234,
        stdout_lines: list[bytes] | None = None,
        stderr_lines: list[bytes] | None = None,
    ) -> None:
        self.pid = pid
        self._handle = handle
        self.returncode: int | None = None
        self.killed = False
        self.waited = False
        self._stdout_lines = list(stdout_lines or [])
        self._stderr_lines = list(stderr_lines or [])
        self._eof_event = threading.Event()
        self.stdin = _FakePipe()
        self.stdout = self._make_reader(self._stdout_lines, wait_for_kill=True)
        self.stderr = self._make_reader(self._stderr_lines, wait_for_kill=False)

    def _make_reader(self, lines: list[bytes], *, wait_for_kill: bool) -> Any:
        parent = self

        class _Reader:
            def __init__(self) -> None:
                self._idx = 0
                self.closed = False

            def readline(self, _limit: int = -1) -> bytes:
                if self.closed:
                    return b""
                if self._idx < len(lines):
                    line = lines[self._idx]
                    self._idx += 1
                    return line
                if wait_for_kill:
                    parent._eof_event.wait(timeout=0.5)
                return b""

            def close(self) -> None:
                self.closed = True
                parent._eof_event.set()

        return _Reader()

    def poll(self) -> int | None:
        return self.returncode

    def kill(self) -> None:
        self.killed = True
        self.returncode = -9
        self._eof_event.set()

    def terminate(self) -> None:
        self.killed = True
        self.returncode = -15
        self._eof_event.set()

    def wait(self, timeout: float | None = None) -> int:
        self.waited = True
        if self.returncode is None:
            self.returncode = 0
        self._eof_event.set()
        return self.returncode


def test_constructor_and_status_are_pure_memory_under_filesystem_and_spawn_tripwires(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verify JsRuntimeManager() and get_status() perform zero FS/env/which/Popen operations."""
    def _forbidden(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError(f"Forbidden side effect invoked during pure-memory call: {args!r} {kwargs!r}")

    monkeypatch.setenv("PATH", "C:\\Malicious\\Bin")
    monkeypatch.setenv("RYN_JS_NODE_EXECUTABLE", "C:\\Malicious\\Bin\\node.exe")
    monkeypatch.setenv("NODE_OPTIONS", "--require C:\\Malicious\\evil.js")
    monkeypatch.setenv("NODE_PATH", "C:\\Malicious\\node_modules")

    monkeypatch.setattr(os, "stat", _forbidden)
    monkeypatch.setattr(os, "lstat", _forbidden)
    monkeypatch.setattr(os, "open", _forbidden)
    monkeypatch.setattr(Path, "stat", _forbidden)
    monkeypatch.setattr(Path, "lstat", _forbidden)
    monkeypatch.setattr(Path, "exists", _forbidden)
    monkeypatch.setattr(Path, "is_file", _forbidden)
    monkeypatch.setattr(Path, "is_dir", _forbidden)
    monkeypatch.setattr(shutil, "which", _forbidden)
    monkeypatch.setattr(subprocess, "Popen", _forbidden)

    manager = JsRuntimeManager()
    status = manager.get_status()
    assert status["mode"] == "packaged_release"
    assert status["is_production"] is True
    assert status["is_development"] is False
    assert status["release_verified"] is False
    assert status["installed_ready"] is False
    assert status["files_verified"] is False
    assert status["actual_process_identity_verified"] is False
    assert status["runtime_handshake_verified"] is False
    assert status["active_session_count"] == 0
    assert status["profile"] is None

    assert is_js_runtime_available() is False
    dev_mgr = JsRuntimeManager(development_profile_root="D:\\nonexistent_dev_profile_not_touched_in_init")
    dev_status = dev_mgr.get_status()
    assert dev_status["mode"] == "source_dev"
    assert dev_status["is_production"] is False
    assert dev_status["is_development"] is True
    assert dev_status["release_verified"] is False
    assert dev_status["installed_ready"] is False
    assert dev_status["files_verified"] is False
    assert is_js_runtime_available(development_profile_root="D:\\nonexistent_dev_profile_not_touched_in_init") is True

    with pytest.raises(JsRuntimeError) as node_err:
        resolve_node_executable("C:\\Malicious\\Bin\\node.exe")
    assert node_err.value.code == "ryn_js_node_unavailable"

    manager.close()
    dev_mgr.close()


def test_production_default_manager_fails_closed_with_zero_spawn_and_no_dev_or_env_fallback(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verify default production manager refuses non-frozen and missing-commitment states with 0 spawns."""
    spawn_calls = 0

    def _forbidden_popen(*args: Any, **kwargs: Any) -> Any:
        nonlocal spawn_calls
        spawn_calls += 1
        raise AssertionError("subprocess.Popen must never be called when production resolution fails")

    monkeypatch.setattr(subprocess, "Popen", _forbidden_popen)
    monkeypatch.setenv("PATH", str(tmp_path))
    monkeypatch.setenv("RYN_JS_NODE_EXECUTABLE", str(tmp_path / "node.exe"))
    monkeypatch.setenv("NODE_OPTIONS", "--inspect")

    # 1. Non-frozen host -> fails closed with ryn_js_runtime_not_packaged, 0 spawns
    monkeypatch.setattr(sys, "frozen", False, raising=False)
    mgr_unfrozen = JsRuntimeManager()
    with pytest.raises(JsResourceError) as exc_unfrozen:
        mgr_unfrozen.get_or_create_session("task_prod_1")
    assert exc_unfrozen.value.code == "ryn_js_runtime_not_packaged"
    assert spawn_calls == 0
    assert mgr_unfrozen.get_status()["last_error_code"] == "ryn_js_runtime_not_packaged"
    assert mgr_unfrozen.get_status()["files_verified"] is False
    mgr_unfrozen.close()

    # 2. Frozen host but _EMBEDDED_PRODUCTION_PROFILE_SPEC is None -> fails closed with ryn_js_resource_missing_commitment
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(manager_mod, "_EMBEDDED_PRODUCTION_PROFILE_SPEC", None)
    mgr_no_spec = JsRuntimeManager()
    with pytest.raises(JsResourceError) as exc_no_spec:
        mgr_no_spec.get_or_create_session("task_prod_2")
    assert exc_no_spec.value.code == "ryn_js_resource_missing_commitment"
    assert spawn_calls == 0
    assert mgr_no_spec.get_status()["last_error_code"] == "ryn_js_resource_missing_commitment"
    mgr_no_spec.close()

    # 3. Frozen host with embedded commitment, but attacker replaces worker.mjs AND updates manifest.json
    # on disk to match the tampered worker hash (self-hashed replacement) -> rejected by expected_manifest_sha256!
    fixture = _build_fake_source_fixture(tmp_path)
    install_dir = tmp_path / "installed-app"
    profile_dir = install_dir / "computer-use" / "js-runtime" / "win32-x64"
    profile_dir.parent.mkdir(parents=True, exist_ok=False)
    prep = prepare_js_runtime_profile(**fixture, output_profile_dir=profile_dir)

    tampered_worker_bytes = b'export const TAMPERED_WORKER = "evil";\n'
    tampered_worker_sha = hashlib.sha256(tampered_worker_bytes).hexdigest()
    (profile_dir / Path(WORKER_REL_PATH)).write_bytes(tampered_worker_bytes)

    manifest_data = json.loads((profile_dir / MANIFEST_FILENAME).read_text(encoding="utf-8"))
    old_worker_size = next(f["size"] for f in manifest_data["files"] if f["path"] == WORKER_REL_PATH)
    for f_entry in manifest_data["files"]:
        if f_entry["path"] == WORKER_REL_PATH:
            f_entry["sha256"] = tampered_worker_sha
            f_entry["size"] = len(tampered_worker_bytes)
    manifest_data["worker_sha256"] = tampered_worker_sha
    manifest_data["total_bytes"] = manifest_data["total_bytes"] - old_worker_size + len(tampered_worker_bytes)
    (profile_dir / MANIFEST_FILENAME).write_text(json.dumps(manifest_data, indent=2) + "\n", encoding="utf-8")

    monkeypatch.setattr(sys, "executable", str(install_dir / "RynApp.exe"))
    monkeypatch.setattr(manager_mod, "_EMBEDDED_PRODUCTION_PROFILE_SPEC", prep.expected_spec)

    mgr_tampered = JsRuntimeManager()
    with pytest.raises(JsResourceError) as exc_tampered:
        mgr_tampered.get_or_create_session("task_prod_3")
    assert exc_tampered.value.code == "ryn_js_resource_manifest_digest_mismatch"
    assert spawn_calls == 0
    mgr_tampered.close()


def test_whole_closure_handle_pinning_and_windows_sharing_barriers(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verify every file and directory in the profile is pinned on Windows and cleaned up on partial failure."""
    fixture = _build_fake_source_fixture(tmp_path)
    out_dir = tmp_path / "pinned-profile"
    prep = prepare_js_runtime_profile(**fixture, output_profile_dir=out_dir)

    baseline_leases = active_resource_lease_count()
    expected_all_relpaths = {entry.path for entry in prep.manifest.files}
    expected_all_dirs = set(expected_profile_directory_relpaths(prep.manifest))
    assert "." in expected_all_dirs
    assert "node" in expected_all_dirs
    assert "package" in expected_all_dirs
    assert "package/src" in expected_all_dirs
    assert NODE_EXECUTABLE_REL_PATH_WIN32 in expected_all_relpaths
    assert WORKER_REL_PATH in expected_all_relpaths
    assert SKY_SHIM_REL_PATH in expected_all_relpaths
    assert RELEASE_SYNC_WASM_REL_PATH in expected_all_relpaths
    assert PACKAGE_JSON_REL_PATH in expected_all_relpaths
    assert PACKAGE_LOCK_REL_PATH in expected_all_relpaths
    for variant_pkg in QUICKJS_VARIANT_PACKAGES:
        assert f"package/node_modules/{variant_pkg}/dist/emscripten-module.wasm" in expected_all_relpaths
        assert f"package/node_modules/{variant_pkg}/dist/index.mjs" in expected_all_relpaths

    with open_development_js_runtime_lease(explicit_profile_root=out_dir) as dev_lease:
        assert dev_lease.profile.mode == "source_dev"
        assert dev_lease.profile.is_development is True
        assert dev_lease.profile.is_production is False
        assert is_verified_production_lease(dev_lease) is False
        assert set(dev_lease.pinned_relpaths) == expected_all_relpaths
        assert dev_lease.pinned_handle_count == len(expected_all_relpaths)
        assert set(dev_lease.pinned_directory_relpaths) == expected_all_dirs
        assert dev_lease.pinned_directory_count == len(expected_all_dirs)
        assert active_resource_lease_count() == baseline_leases + 1

        for dir_rel in expected_all_dirs:
            dir_pin = dev_lease.get_pinned_directory_identity(dir_rel)
            assert dir_pin is not None
            expected_dir_path = out_dir.resolve() if dir_rel == "." else (out_dir / Path(dir_rel)).resolve()
            assert dir_pin.final_path == expected_dir_path

        # On Windows, FILE_SHARE_READ-only file handles block writing, unlinking, and renaming pinned files!
        if os.name == "nt":
            for sample_rel in (
                NODE_EXECUTABLE_REL_PATH_WIN32,
                WORKER_REL_PATH,
                SKY_SHIM_REL_PATH,
                RELEASE_SYNC_WASM_REL_PATH,
                PACKAGE_JSON_REL_PATH,
                "package/node_modules/quickjs-emscripten/dist/index.mjs",
            ):
                target_file = out_dir / Path(sample_rel)
                with pytest.raises(OSError):
                    target_file.write_bytes(b"overwrite-blocked-by-pin")
                with pytest.raises(OSError):
                    target_file.unlink()
                with pytest.raises(OSError):
                    target_file.rename(target_file.with_suffix(".swapped"))

        # Concurrent read is still permitted (FILE_SHARE_READ)
        assert (out_dir / Path(NODE_EXECUTABLE_REL_PATH_WIN32)).read_bytes() == fixture["node_executable_path"].read_bytes()
        dev_lease.revalidate_critical_files()

        # Simulated directory identity mismatch fails closed during revalidate_critical_files()
        orig_pkg_dir_pin = dev_lease._pinned_directories["package"]
        dev_lease._pinned_directories["package"] = dataclasses.replace(
            orig_pkg_dir_pin,
            file_id=orig_pkg_dir_pin.file_id ^ 0x13579BDF,
        )
        try:
            with pytest.raises(JsResourceError) as exc_dir_id:
                dev_lease.revalidate_critical_files()
            assert exc_dir_id.value.code == "ryn_js_resource_pinned_identity_mismatch"
        finally:
            dev_lease._pinned_directories["package"] = orig_pkg_dir_pin

    assert dev_lease.closed is True
    assert dev_lease.pinned_handle_count == 0
    assert dev_lease.pinned_directory_count == 0
    assert active_resource_lease_count() == baseline_leases

    # Partial-acquisition failure during file pinning cleans up all previously opened directory and file handles
    import rynmesh.agent_runtime.js_runtime.resources as res_mod

    orig_open_entry = res_mod._open_and_verify_profile_entry
    call_counter = 0

    def _fail_mid_acquisition(*args: Any, **kwargs: Any) -> Any:
        nonlocal call_counter
        call_counter += 1
        if call_counter == 3:
            raise JsResourceError("ryn_js_resource_hash_mismatch", "simulated partial acquisition failure")
        return orig_open_entry(*args, **kwargs)

    monkeypatch.setattr(res_mod, "_open_and_verify_profile_entry", _fail_mid_acquisition)
    with pytest.raises(JsResourceError) as exc_partial:
        open_development_js_runtime_lease(explicit_profile_root=out_dir)
    assert exc_partial.value.code == "ryn_js_resource_hash_mismatch"
    assert active_resource_lease_count() == baseline_leases
    monkeypatch.setattr(res_mod, "_open_and_verify_profile_entry", orig_open_entry)

    # After cleanup, all directory and file handles are released so files can be modified or unlinked
    worker_file = out_dir / Path(WORKER_REL_PATH)
    orig_bytes = worker_file.read_bytes()
    worker_file.write_bytes(orig_bytes)


def test_windows_pinned_directory_handles_deny_rename_when_executed(
    tmp_path: Path,
) -> None:
    """Isolated test for Windows directory-rename denial under pinned directory handles."""
    fixture = _build_fake_source_fixture(tmp_path)
    out_dir = tmp_path / "pinned-dir-rename-profile"
    prepare_js_runtime_profile(**fixture, output_profile_dir=out_dir)

    with open_development_js_runtime_lease(explicit_profile_root=out_dir) as dev_lease:
        assert dev_lease.pinned_directory_count > 0
        if os.name == "nt":
            for sample_dir_rel in ("node", "package", "package/src"):
                target_dir = out_dir / Path(sample_dir_rel)
                with pytest.raises(OSError):
                    target_dir.rename(target_dir.with_name(target_dir.name + "_swapped"))


def test_pre_resume_verification_rejects_mismatched_pid_creation_time_image_and_hook_errors(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verify that any failure in before_resume kills the suspended process before NtResumeProcess is ever called."""
    fixture = _build_fake_source_fixture(tmp_path)
    out_dir = tmp_path / "pre-resume-profile"
    prepare_js_runtime_profile(**fixture, output_profile_dir=out_dir)

    resumed_count = 0
    real_process_guard_terminate_calls = 0
    created_procs: list[_FakeSuspendedPopen] = []

    def _safe_fake_terminate(proc: Any) -> None:
        # Never forward synthetic integer handles to process_guard.terminate / kernel32.CloseHandle
        proc.kill()

    monkeypatch.setattr(manager_mod, "terminate", _safe_fake_terminate)

    def _fake_popen(cmd: list[str], **kwargs: Any) -> _FakeSuspendedPopen:
        assert cmd == [
            str(out_dir.resolve() / Path(NODE_EXECUTABLE_REL_PATH_WIN32)),
            str(out_dir.resolve() / Path(WORKER_REL_PATH)),
        ]
        assert kwargs.get("cwd") == str(out_dir.resolve() / "package")
        assert (kwargs.get("creationflags", 0) & 0x4) == 0x4
        proc = _FakeSuspendedPopen(pid=55111, handle=0x7788)
        created_procs.append(proc)
        return proc

    def _fake_windows_job(proc: _FakeSuspendedPopen, *, before_resume: Any = None, **kwargs: Any) -> int:
        nonlocal resumed_count
        try:
            if before_resume is not None:
                before_resume(proc, 0x9999)
            resumed_count += 1
            return 0x9999
        except BaseException:
            proc.kill()
            raise

    monkeypatch.setattr(subprocess, "Popen", _fake_popen)
    monkeypatch.setattr(manager_mod, "_windows_job", _fake_windows_job)

    node_resolved = str((out_dir / Path(NODE_EXECUTABLE_REL_PATH_WIN32)).resolve())
    other_exe = tmp_path / "wrong-node.exe"
    other_exe.write_bytes(b"MZ-different-binary-content")

    # Case 1: OS GetProcessId mismatches Popen.pid -> rejected before resume
    monkeypatch.setattr(
        pid_mod,
        "query_win32_suspended_process_os_info",
        lambda _h: (99999, 133700000000000000, node_resolved),
    )
    mgr = JsRuntimeManager(development_profile_root=out_dir)
    with pytest.raises(JsProcessIdentityError) as exc_pid:
        mgr.get_or_create_session("task_bad_pid")
    assert exc_pid.value.code == "ryn_js_process_identity_mismatch"
    assert resumed_count == 0
    assert created_procs[-1].killed is True
    assert active_resource_lease_count() == 0
    mgr.close()

    # Case 2: OS GetProcessTimes returns 0 creation time -> rejected before resume
    monkeypatch.setattr(
        pid_mod,
        "query_win32_suspended_process_os_info",
        lambda _h: (55111, 0, node_resolved),
    )
    mgr_time = JsRuntimeManager(development_profile_root=out_dir)
    with pytest.raises(JsProcessIdentityError) as exc_time:
        mgr_time.get_or_create_session("task_bad_time")
    assert exc_time.value.code == "ryn_js_process_identity_mismatch"
    assert resumed_count == 0
    assert created_procs[-1].killed is True
    assert active_resource_lease_count() == 0
    mgr_time.close()

    # Case 3: Image path points to wrong executable -> rejected before resume
    monkeypatch.setattr(
        pid_mod,
        "query_win32_suspended_process_os_info",
        lambda _h: (55111, 133700000000000000, str(other_exe.resolve())),
    )
    mgr_img = JsRuntimeManager(development_profile_root=out_dir)
    with pytest.raises(JsProcessIdentityError) as exc_img:
        mgr_img.get_or_create_session("task_bad_img")
    assert exc_img.value.code == "ryn_js_process_identity_mismatch"
    assert resumed_count == 0
    assert created_procs[-1].killed is True
    assert active_resource_lease_count() == 0
    mgr_img.close()

    # Case 4: Tampered pinned file identity (simulated swap before resume) -> rejected before resume
    monkeypatch.setattr(
        pid_mod,
        "query_win32_suspended_process_os_info",
        lambda _h: (55111, 133700000000000000, node_resolved),
    )

    def _tamper_node_pin_before_resume(*, process: Any, lease: Any, session_id: str, generation: int) -> VerifiedProcessIdentity:
        orig_pin = lease._pinned_files[NODE_EXECUTABLE_REL_PATH_WIN32]
        lease._pinned_files[NODE_EXECUTABLE_REL_PATH_WIN32] = dataclasses.replace(
            orig_pin,
            file_id=orig_pin.file_id ^ 0xABCDEF,
        )
        return verify_suspended_process_identity_on_windows(
            process=process,
            lease=lease,
            session_id=session_id,
            generation=generation,
        )

    mgr_fid = JsRuntimeManager(
        development_profile_root=out_dir,
        _before_resume_verifier=_tamper_node_pin_before_resume,
    )
    with pytest.raises(JsProcessIdentityError) as exc_fid:
        mgr_fid.get_or_create_session("task_bad_fid")
    assert exc_fid.value.code == "ryn_js_process_identity_mismatch"
    assert resumed_count == 0
    assert created_procs[-1].killed is True
    assert active_resource_lease_count() == 0
    mgr_fid.close()

    # Case 5: Arbitrary exception inside before_resume hook -> kills suspended child, closes lease, 0 resumes
    def _raising_hook(*, process: Any, lease: Any, session_id: str, generation: int) -> VerifiedProcessIdentity:
        raise RuntimeError("simulated before_resume fault")

    mgr_err = JsRuntimeManager(
        development_profile_root=out_dir,
        _before_resume_verifier=_raising_hook,
    )
    with pytest.raises(JsRuntimeError) as exc_hook:
        mgr_err.get_or_create_session("task_hook_err")
    assert exc_hook.value.code == "ryn_js_process_identity_mismatch"
    assert resumed_count == 0
    assert created_procs[-1].killed is True
    assert active_resource_lease_count() == 0
    assert real_process_guard_terminate_calls == 0
    mgr_err.close()


def test_ready_handshake_strictly_validates_session_generation_engine_variant_and_pid(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verify malformed or mismatched `ready` frames fail closed and release both process and lease."""
    fixture = _build_fake_source_fixture(tmp_path)
    out_dir = tmp_path / "ready-validation-profile"
    prepare_js_runtime_profile(**fixture, output_profile_dir=out_dir)
    node_resolved = (out_dir / Path(NODE_EXECUTABLE_REL_PATH_WIN32)).resolve()

    monkeypatch.setattr(manager_mod, "terminate", lambda proc: proc.kill())

    bad_ready_cases = [
        (
            "wrong_session_id",
            lambda sid: {
                "v": 1,
                "type": "ready",
                "session_id": "foreign_session",
                "generation": 1,
                "engine": "quickjs-emscripten",
                "release_variant": "RELEASE_SYNC",
                "pid": 60123,
            },
            "ryn_js_session_mismatch",
        ),
        (
            "wrong_generation",
            lambda sid: {
                "v": 1,
                "type": "ready",
                "session_id": sid,
                "generation": 99,
                "engine": "quickjs-emscripten",
                "release_variant": "RELEASE_SYNC",
                "pid": 60123,
            },
            "ryn_js_ready_generation_mismatch",
        ),
        (
            "wrong_engine",
            lambda sid: {
                "v": 1,
                "type": "ready",
                "session_id": sid,
                "generation": 1,
                "engine": "v8-untrusted",
                "release_variant": "RELEASE_SYNC",
                "pid": 60123,
            },
            "ryn_js_ready_engine_mismatch",
        ),
        (
            "wrong_variant",
            lambda sid: {
                "v": 1,
                "type": "ready",
                "session_id": sid,
                "generation": 1,
                "engine": "quickjs-emscripten",
                "release_variant": "DEBUG_ASYNC",
                "pid": 60123,
            },
            "ryn_js_ready_variant_mismatch",
        ),
        (
            "wrong_pid",
            lambda sid: {
                "v": 1,
                "type": "ready",
                "session_id": sid,
                "generation": 1,
                "engine": "quickjs-emscripten",
                "release_variant": "RELEASE_SYNC",
                "pid": 99999,
            },
            "ryn_js_ready_pid_mismatch",
        ),
    ]

    for case_name, frame_builder, expected_code in bad_ready_cases:
        spawned: list[_FakeSuspendedPopen] = []

        def _make_popen(cmd: list[str], **kwargs: Any) -> _FakeSuspendedPopen:
            env = kwargs["env"]
            sid = env["RYN_JS_SESSION_ID"]
            line = (json.dumps(frame_builder(sid)) + "\n").encode("utf-8")  # noqa: B023 - this callback finishes within the current case.
            proc = _FakeSuspendedPopen(pid=60123, handle=0x5566, stdout_lines=[line])
            spawned.append(proc)  # noqa: B023 - this callback finishes within the current case.
            return proc

        def _fake_job(proc: _FakeSuspendedPopen, *, before_resume: Any = None, **kwargs: Any) -> int:
            if before_resume is not None:
                before_resume(proc, 0x8888)
            return 0x8888

        def _stub_verifier(*, process: Any, lease: Any, session_id: str, generation: int) -> VerifiedProcessIdentity:
            node_pin = lease.get_pinned_file_identity(NODE_EXECUTABLE_REL_PATH_WIN32)
            assert node_pin is not None
            return VerifiedProcessIdentity(
                pid=process.pid,
                creation_time_filetime=133700000000000000,
                image_path=node_resolved,
                node_volume_serial=node_pin.volume_serial,
                node_file_id=node_pin.file_id,
                node_sha256=node_pin.sha256,
                node_bytes=node_pin.size,
                profile_digest=lease.profile.profile_digest,
                mode=lease.profile.mode,
                session_id=session_id,
                generation=generation,
            )

        monkeypatch.setattr(subprocess, "Popen", _make_popen)
        monkeypatch.setattr(manager_mod, "_windows_job", _fake_job)

        mgr = JsRuntimeManager(
            development_profile_root=out_dir,
            startup_timeout=0.8,
            _before_resume_verifier=_stub_verifier,
        )
        with pytest.raises(JsRuntimeError) as exc_info:
            mgr.get_or_create_session(f"task_{case_name}")
        assert exc_info.value.code == expected_code, f"Case {case_name} expected {expected_code}, got {exc_info.value.code}"
        assert spawned and spawned[-1].killed is True
        assert active_resource_lease_count() == 0
        assert mgr.get_status()["runtime_handshake_verified"] is False
        mgr.close()


def test_unconfirmed_child_exit_retains_lease_pins_in_quarantine_until_exit_confirmed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verify stdout EOF or terminate failure never releases file/directory pins while proc.poll() is None."""
    fixture = _build_fake_source_fixture(tmp_path)
    out_dir = tmp_path / "quarantine-profile"
    prepare_js_runtime_profile(**fixture, output_profile_dir=out_dir)
    node_resolved = (out_dir / Path(NODE_EXECUTABLE_REL_PATH_WIN32)).resolve()

    baseline_leases = active_resource_lease_count()
    spawned_procs: list[_FakeSuspendedPopen] = []
    allow_terminate_exit = False

    def _controlled_terminate(proc: _FakeSuspendedPopen) -> None:
        if not allow_terminate_exit:
            raise RuntimeError("simulated terminate failure while child still alive")
        proc.kill()

    def _make_popen(cmd: list[str], **kwargs: Any) -> _FakeSuspendedPopen:
        env = kwargs["env"]
        sid = env["RYN_JS_SESSION_ID"]
        gen = int(env["RYN_JS_GENERATION"])
        pid_val = 71000 + len(spawned_procs)
        ready_line = (
            json.dumps(
                {
                    "v": 1,
                    "type": "ready",
                    "session_id": sid,
                    "generation": gen,
                    "engine": "quickjs-emscripten",
                    "release_variant": "RELEASE_SYNC",
                    "pid": pid_val,
                }
            )
            + "\n"
        ).encode("utf-8")
        proc = _FakeSuspendedPopen(pid=pid_val, handle=0x3344, stdout_lines=[ready_line])
        spawned_procs.append(proc)
        return proc

    def _fake_job(proc: _FakeSuspendedPopen, *, before_resume: Any = None, **kwargs: Any) -> int:
        if before_resume is not None:
            before_resume(proc, 0x4455)
        return 0x4455

    def _stub_verifier(*, process: Any, lease: Any, session_id: str, generation: int) -> VerifiedProcessIdentity:
        node_pin = lease.get_pinned_file_identity(NODE_EXECUTABLE_REL_PATH_WIN32)
        assert node_pin is not None
        return VerifiedProcessIdentity(
            pid=process.pid,
            creation_time_filetime=133700000000000000,
            image_path=node_resolved,
            node_volume_serial=node_pin.volume_serial,
            node_file_id=node_pin.file_id,
            node_sha256=node_pin.sha256,
            node_bytes=node_pin.size,
            profile_digest=lease.profile.profile_digest,
            mode=lease.profile.mode,
            session_id=session_id,
            generation=generation,
        )

    monkeypatch.setattr(manager_mod, "terminate", _controlled_terminate)
    monkeypatch.setattr(subprocess, "Popen", _make_popen)
    monkeypatch.setattr(manager_mod, "_windows_job", _fake_job)

    mgr = JsRuntimeManager(
        development_profile_root=out_dir,
        _before_resume_verifier=_stub_verifier,
    )
    session_1 = mgr.get_or_create_session("task_quarantine")
    lease_1 = session_1.resource_lease
    proc_1 = spawned_procs[0]
    assert lease_1.closed is False
    assert lease_1.pinned_handle_count > 0
    assert lease_1.pinned_directory_count > 0
    assert active_resource_lease_count() == baseline_leases + 1

    # 1. Closing stdout (EOF) while child is still alive (proc_1.poll() is None) must NOT close the lease
    session_1._on_stdout_eof()
    assert proc_1.poll() is None
    assert session_1.status == "closed"
    assert session_1.actual_process_identity_verified is False
    assert session_1.runtime_handshake_verified is False
    assert lease_1.closed is False
    assert active_resource_lease_count() == baseline_leases + 1

    # 2. Attempting to close_task while terminate fails and proc_1.poll() is None quarantines the session
    # and keeps all file/directory handles pinned!
    mgr.close_task("task_quarantine")
    assert session_1.cleanup_incomplete is True
    assert lease_1.closed is False
    assert lease_1.pinned_handle_count > 0
    assert lease_1.pinned_directory_count > 0
    assert active_resource_lease_count() == baseline_leases + 1

    st_quarantined = mgr.get_status()
    assert st_quarantined["quarantined_session_count"] == 1
    assert st_quarantined["cleanup_incomplete"] is True
    assert st_quarantined["actual_process_identity_verified"] is False
    assert st_quarantined["runtime_handshake_verified"] is False
    assert st_quarantined["last_error_code"] == "ryn_js_cleanup_incomplete"

    # Replacement session for the same task is refused while the unconfirmed child is still alive
    with pytest.raises(JsRuntimeError) as exc_replace:
        mgr.get_or_create_session("task_quarantine")
    assert exc_replace.value.code == "ryn_js_cleanup_incomplete"
    assert len(spawned_procs) == 1
    assert lease_1.closed is False

    # 3. Once the old process finally exits, reaping releases lease_1 and permits replacement
    allow_terminate_exit = True
    proc_1.returncode = -9
    session_2 = mgr.get_or_create_session("task_quarantine")
    assert session_2 is not session_1
    assert lease_1.closed is True
    assert lease_1.pinned_handle_count == 0
    assert lease_1.pinned_directory_count == 0
    assert session_2.resource_lease.closed is False
    assert mgr.get_status()["quarantined_session_count"] == 0
    assert mgr.get_status()["cleanup_incomplete"] is False

    mgr.close()
    assert session_2.resource_lease.closed is True
    assert active_resource_lease_count() == baseline_leases


def test_manager_live_status_tracks_active_sessions_and_rejects_relabelled_dev_lease(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verify get_status() reflects current live sessions and rejects custom hooks or non-installed roots before Popen."""
    fixture = _build_fake_source_fixture(tmp_path)
    out_dir = tmp_path / "status-truth-profile"
    prep = prepare_js_runtime_profile(**fixture, output_profile_dir=out_dir)
    node_resolved = (out_dir / Path(NODE_EXECUTABLE_REL_PATH_WIN32)).resolve()

    spawned_procs: list[_FakeSuspendedPopen] = []
    monkeypatch.setattr(manager_mod, "terminate", lambda proc: proc.kill())

    def _make_popen(cmd: list[str], **kwargs: Any) -> _FakeSuspendedPopen:
        env = kwargs["env"]
        sid = env["RYN_JS_SESSION_ID"]
        gen = int(env["RYN_JS_GENERATION"])
        pid_val = 81000 + len(spawned_procs)
        ready_line = (
            json.dumps(
                {
                    "v": 1,
                    "type": "ready",
                    "session_id": sid,
                    "generation": gen,
                    "engine": "quickjs-emscripten",
                    "release_variant": "RELEASE_SYNC",
                    "pid": pid_val,
                }
            )
            + "\n"
        ).encode("utf-8")
        proc = _FakeSuspendedPopen(pid=pid_val, handle=0x6677, stdout_lines=[ready_line])
        spawned_procs.append(proc)
        return proc

    def _fake_job(proc: _FakeSuspendedPopen, *, before_resume: Any = None, **kwargs: Any) -> int:
        if before_resume is not None:
            before_resume(proc, 0x7788)
        return 0x7788

    def _stub_verifier(*, process: Any, lease: Any, session_id: str, generation: int) -> VerifiedProcessIdentity:
        node_pin = lease.get_pinned_file_identity(NODE_EXECUTABLE_REL_PATH_WIN32)
        assert node_pin is not None
        return VerifiedProcessIdentity(
            pid=process.pid,
            creation_time_filetime=133700000000000000,
            image_path=node_resolved,
            node_volume_serial=node_pin.volume_serial,
            node_file_id=node_pin.file_id,
            node_sha256=node_pin.sha256,
            node_bytes=node_pin.size,
            profile_digest=lease.profile.profile_digest,
            mode=lease.profile.mode,
            session_id=session_id,
            generation=generation,
        )

    monkeypatch.setattr(subprocess, "Popen", _make_popen)
    monkeypatch.setattr(manager_mod, "_windows_job", _fake_job)

    # Part A: Live status flags track currently active ready sessions, not sticky historical booleans
    mgr = JsRuntimeManager(
        development_profile_root=out_dir,
        _before_resume_verifier=_stub_verifier,
    )
    s1 = mgr.get_or_create_session("task_live_1")
    s2 = mgr.get_or_create_session("task_live_2")
    st_both = mgr.get_status()
    assert st_both["active_session_count"] == 2
    assert st_both["actual_process_identity_verified"] is True
    assert st_both["runtime_handshake_verified"] is True
    assert st_both["profile"]["handshake_verified"] is True

    # Closing one session while another ready session remains keeps live verification True
    mgr.close_task("task_live_1")
    assert s1.actual_process_identity_verified is False
    assert s1.runtime_handshake_verified is False
    st_one = mgr.get_status()
    assert st_one["active_session_count"] == 1
    assert st_one["actual_process_identity_verified"] is True
    assert st_one["runtime_handshake_verified"] is True

    # Last-session invalidation via unexpected worker loss immediately clears live verification in get_status()
    spawned_procs[-1].returncode = -9
    s2._mark_lost("simulated last session loss")
    st_lost = mgr.get_status()
    assert st_lost["active_session_count"] == 0
    assert st_lost["actual_process_identity_verified"] is False
    assert st_lost["runtime_handshake_verified"] is False
    assert st_lost["release_verified"] is False
    assert st_lost["profile"]["handshake_verified"] is False

    mgr.close_task("task_live_2")
    st_none = mgr.get_status()
    assert st_none["active_session_count"] == 0
    assert st_none["actual_process_identity_verified"] is False
    assert st_none["runtime_handshake_verified"] is False
    assert st_none["release_verified"] is False
    assert st_none["profile"]["handshake_verified"] is False
    mgr.close()

    # Part B: Production manager rejects a relabelled source_dev lease and any custom hook BEFORE Popen
    install_dir = tmp_path / "InstallAppRoot"
    installed_profile_dir = install_dir / "resources" / "js-runtime" / "win32-x64"
    prepare_js_runtime_profile(**fixture, output_profile_dir=installed_profile_dir)

    spawn_before_prod_checks = len(spawned_procs)
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", str(install_dir / "RynApp.exe"))
    monkeypatch.setattr(manager_mod, "_EMBEDDED_PRODUCTION_PROFILE_SPEC", prep.expected_spec)

    relabelled_loader_calls = 0

    def _relabelled_dev_loader() -> Any:
        nonlocal relabelled_loader_calls
        relabelled_loader_calls += 1
        dev_lease = open_development_js_runtime_lease(explicit_profile_root=out_dir)
        relabelled_profile = dataclasses.replace(dev_lease.profile, mode="packaged_release")
        object.__setattr__(dev_lease, "profile", relabelled_profile)
        return dev_lease

    mgr_spoofed = JsRuntimeManager(
        _production_loader=_relabelled_dev_loader,
        _before_resume_verifier=_stub_verifier,
    )
    with pytest.raises(JsResourceError) as exc_spoof:
        mgr_spoofed.get_or_create_session("task_spoofed_prod")
    assert exc_spoof.value.code == "ryn_js_resource_invalid_mode"
    assert len(spawned_procs) == spawn_before_prod_checks
    assert relabelled_loader_calls == 0
    assert mgr_spoofed.get_status()["release_verified"] is False
    assert mgr_spoofed.get_status()["last_error_code"] == "ryn_js_resource_invalid_mode"
    mgr_spoofed.close()

    # Part C: Without explicit source_dev config, custom _production_loader (even returning a genuine
    # public open_production_js_runtime_lease with matching digest) and custom _before_resume_verifier
    # each fail BEFORE Popen (zero spawns).
    custom_loader_calls = 0

    def _custom_prod_loader() -> Any:
        nonlocal custom_loader_calls
        custom_loader_calls += 1
        return open_production_js_runtime_lease(
            trusted_profile_root=out_dir,
            expected_spec=prep.expected_spec,
        )

    for hook_name, mgr_kwargs in (
        (
            "custom_production_loader_only",
            {"_production_loader": _custom_prod_loader},
        ),
        (
            "custom_before_resume_verifier_only",
            {"_before_resume_verifier": _stub_verifier},
        ),
        (
            "both_custom_hooks",
            {
                "_production_loader": _custom_prod_loader,
                "_before_resume_verifier": _stub_verifier,
            },
        ),
    ):
        mgr_hooked_prod = JsRuntimeManager(**mgr_kwargs)
        with pytest.raises(JsResourceError) as exc_hooked:
            mgr_hooked_prod.get_or_create_session(f"task_{hook_name}")
        assert exc_hooked.value.code == "ryn_js_resource_invalid_mode", hook_name
        assert len(spawned_procs) == spawn_before_prod_checks, hook_name
        assert custom_loader_calls == 0, hook_name
        st_hooked = mgr_hooked_prod.get_status()
        assert st_hooked["active_session_count"] == 0
        assert st_hooked["release_verified"] is False
        assert st_hooked["installed_ready"] is False
        assert st_hooked["last_error_code"] == "ryn_js_resource_invalid_mode"
        mgr_hooked_prod.close()

    # Also verify that passing _production_loader alongside explicit source_dev config is rejected before spawn
    mgr_mixed = JsRuntimeManager(
        development_profile_root=out_dir,
        _production_loader=_custom_prod_loader,
    )
    with pytest.raises(JsResourceError) as exc_mixed:
        mgr_mixed.get_or_create_session("task_mixed_hooks")
    assert exc_mixed.value.code == "ryn_js_resource_invalid_mode"
    assert len(spawned_procs) == spawn_before_prod_checks
    assert custom_loader_calls == 0
    mgr_mixed.close()

    # Part D: Installed-root authority and dataclasses.replace profile-fact binding
    # 1. A caller-supplied public open_production_js_runtime_lease at another root (`out_dir`) is NOT
    #    admitted as host-installed production authority even when its manifest digest matches.
    with open_production_js_runtime_lease(
        trusted_profile_root=out_dir,
        expected_spec=prep.expected_spec,
    ) as external_prod_lease:
        assert is_verified_production_lease(
            external_prod_lease,
            expected_manifest_sha256=prep.expected_spec.expected_manifest_sha256,
        ) is True
        assert is_verified_production_lease(
            external_prod_lease,
            expected_manifest_sha256=prep.expected_spec.expected_manifest_sha256,
            expected_profile_root=installed_profile_dir,
            expected_spec=prep.expected_spec,
        ) is False
        assert manager_mod._verify_host_installed_production_lease(external_prod_lease) is False

    # 2. The private host loader opens from the fixed installed root and is admitted, but any
    #    dataclasses.replace mutation on lease.profile (preserving profile_digest) immediately
    #    invalidates both is_verified_production_lease and _verify_host_installed_production_lease!
    with manager_mod._open_production_lease_from_host() as host_prod_lease:
        assert manager_mod._verify_host_installed_production_lease(host_prod_lease) is True
        orig_profile = host_prod_lease.profile

        for mutated_profile in (
            dataclasses.replace(
                orig_profile,
                node_executable=(out_dir / Path(NODE_EXECUTABLE_REL_PATH_WIN32)).resolve(),
            ),
            dataclasses.replace(
                orig_profile,
                worker_path=(out_dir / Path(WORKER_REL_PATH)).resolve(),
            ),
            dataclasses.replace(
                orig_profile,
                package_root=(out_dir / "package").resolve(),
            ),
            dataclasses.replace(
                orig_profile,
                node_sha256="0" * 64,
            ),
            dataclasses.replace(
                orig_profile,
                total_files=orig_profile.total_files + 1,
            ),
        ):
            assert mutated_profile.profile_digest == orig_profile.profile_digest
            object.__setattr__(host_prod_lease, "profile", mutated_profile)
            assert is_verified_production_lease(
                host_prod_lease,
                expected_manifest_sha256=prep.expected_spec.expected_manifest_sha256,
                expected_profile_root=installed_profile_dir,
                expected_spec=prep.expected_spec,
            ) is False
            assert manager_mod._verify_host_installed_production_lease(host_prod_lease) is False

        object.__setattr__(host_prod_lease, "profile", orig_profile)
        assert manager_mod._verify_host_installed_production_lease(host_prod_lease) is True


def test_manager_session_lock_order_concurrent_startup_quarantine_and_single_flight(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verify zero M<->S lock inversion across startup failure/quarantine, concurrent single-flight start, and concurrent close."""
    fixture = _build_fake_source_fixture(tmp_path)
    out_dir = tmp_path / "lock-order-profile"
    prepare_js_runtime_profile(**fixture, output_profile_dir=out_dir)
    node_resolved = (out_dir / Path(NODE_EXECUTABLE_REL_PATH_WIN32)).resolve()
    baseline_leases = active_resource_lease_count()

    def _stub_verifier(*, process: Any, lease: Any, session_id: str, generation: int) -> VerifiedProcessIdentity:
        node_pin = lease.get_pinned_file_identity(NODE_EXECUTABLE_REL_PATH_WIN32)
        assert node_pin is not None
        return VerifiedProcessIdentity(
            pid=process.pid,
            creation_time_filetime=133700000000000000,
            image_path=node_resolved,
            node_volume_serial=node_pin.volume_serial,
            node_file_id=node_pin.file_id,
            node_sha256=node_pin.sha256,
            node_bytes=node_pin.size,
            profile_digest=lease.profile.profile_digest,
            mode=lease.profile.mode,
            session_id=session_id,
            generation=generation,
        )

    def _fake_job(proc: _FakeSuspendedPopen, *, before_resume: Any = None, **kwargs: Any) -> int:
        if before_resume is not None:
            before_resume(proc, 0x7788)
        return 0x7788

    monkeypatch.setattr(manager_mod, "_windows_job", _fake_job)

    # Case 1: Startup fails with unconfirmed exit (proc.poll() is None) while reader thread notifies
    # quarantine concurrently with manager.get_status() and manager.get_session().
    unconfirmed_procs: list[_FakeSuspendedPopen] = []
    allow_exit_case1 = False

    def _terminate_case1(proc: _FakeSuspendedPopen) -> None:
        if not allow_exit_case1:
            raise RuntimeError("unconfirmed exit during startup failure")
        proc.kill()

    def _popen_case1(cmd: list[str], **kwargs: Any) -> _FakeSuspendedPopen:
        # Emit an invalid ready frame so reader thread triggers _force_kill_unlocked -> quarantine
        # while _start_process is waiting on self._cond!
        bad_ready = (
            json.dumps(
                {
                    "v": 1,
                    "type": "ready",
                    "session_id": "wrong_session_id",
                    "generation": 1,
                    "engine": "quickjs-emscripten",
                    "release_variant": "RELEASE_SYNC",
                    "pid": 91001,
                }
            )
            + "\n"
        ).encode("utf-8")
        proc = _FakeSuspendedPopen(pid=91001, handle=0x1234, stdout_lines=[bad_ready])
        unconfirmed_procs.append(proc)
        return proc

    monkeypatch.setattr(manager_mod, "terminate", _terminate_case1)
    monkeypatch.setattr(subprocess, "Popen", _popen_case1)

    mgr1 = JsRuntimeManager(
        development_profile_root=out_dir,
        startup_timeout=0.6,
        _before_resume_verifier=_stub_verifier,
    )
    stop_observer = threading.Event()
    observer_errors: list[BaseException] = []

    def _status_observer() -> None:
        try:
            while not stop_observer.is_set():
                mgr1.get_status()
                mgr1.get_session("task_startup_quarantine")
        except BaseException as exc:
            observer_errors.append(exc)

    obs_thread = threading.Thread(target=_status_observer, daemon=True)
    obs_thread.start()
    try:
        with pytest.raises(JsRuntimeError) as exc_start_q:
            mgr1.get_or_create_session("task_startup_quarantine")
        assert exc_start_q.value.code == "ryn_js_session_mismatch"
    finally:
        stop_observer.set()
        obs_thread.join(timeout=2.0)

    assert not obs_thread.is_alive()
    assert not observer_errors
    # Failed-startup session with unconfirmed exit MUST remain strongly referenced in quarantine
    # with its VerifiedJsResourceLease still open!
    st_q1 = mgr1.get_status()
    assert st_q1["quarantined_session_count"] == 1
    assert st_q1["cleanup_incomplete"] is True
    assert st_q1["last_error_code"] == "ryn_js_cleanup_incomplete"
    assert len(mgr1._quarantined_sessions) == 1
    assert len(mgr1._quarantined_by_task.get("task_startup_quarantine", ())) == 1
    q_sess = next(iter(mgr1._quarantined_sessions.values()))
    assert q_sess.resource_lease.closed is False
    assert active_resource_lease_count() == baseline_leases + 1

    # Once exit is confirmed, close() reaps the quarantined startup session and releases its lease
    allow_exit_case1 = True
    unconfirmed_procs[0].returncode = -9
    mgr1.close()
    assert mgr1.get_status()["quarantined_session_count"] == 0
    assert q_sess.resource_lease.closed is True
    assert active_resource_lease_count() == baseline_leases

    # Case 2: Concurrent first get_or_create_session callers share a single-flight reservation
    # (only 1 Popen) and do not block get_status() while waiting for ready.
    monkeypatch.setattr(manager_mod, "terminate", lambda proc: proc.kill())
    gate_ready = threading.Event()
    popen_entered = threading.Event()
    case2_procs: list[_FakeSuspendedPopen] = []

    class _GatedStdoutPipe(io.BytesIO):
        def __init__(self, line: bytes) -> None:
            super().__init__(line)
            self._gated = True

        def readline(self, size: int = -1) -> bytes:
            if self._gated:
                self._gated = False
                gate_ready.wait(timeout=5.0)
            return super().readline(size)

    def _popen_case2(cmd: list[str], **kwargs: Any) -> _FakeSuspendedPopen:
        env = kwargs["env"]
        sid = env["RYN_JS_SESSION_ID"]
        gen = int(env["RYN_JS_GENERATION"])
        pid_val = 92000 + len(case2_procs)
        ready_line = (
            json.dumps(
                {
                    "v": 1,
                    "type": "ready",
                    "session_id": sid,
                    "generation": gen,
                    "engine": "quickjs-emscripten",
                    "release_variant": "RELEASE_SYNC",
                    "pid": pid_val,
                }
            )
            + "\n"
        ).encode("utf-8")
        proc = _FakeSuspendedPopen(pid=pid_val, handle=0x2345, stdout_lines=[])
        proc.stdout = _GatedStdoutPipe(ready_line)
        case2_procs.append(proc)
        popen_entered.set()
        return proc

    monkeypatch.setattr(subprocess, "Popen", _popen_case2)
    mgr2 = JsRuntimeManager(
        development_profile_root=out_dir,
        startup_timeout=5.0,
        _before_resume_verifier=_stub_verifier,
    )

    results_case2: list[ Any ] = []
    errors_case2: list[BaseException] = []

    def _caller_case2() -> None:
        try:
            results_case2.append(mgr2.get_or_create_session("task_single_flight"))
        except BaseException as exc:
            errors_case2.append(exc)

    t1 = threading.Thread(target=_caller_case2, daemon=True)
    t2 = threading.Thread(target=_caller_case2, daemon=True)
    t1.start()
    assert popen_entered.wait(timeout=2.0)
    t2.start()
    # While startup is in-flight outside manager._lock, get_status() returns immediately without blocking
    inflight_status = mgr2.get_status()
    assert inflight_status["files_verified"] is True
    gate_ready.set()
    t1.join(timeout=2.0)
    t2.join(timeout=2.0)
    assert not t1.is_alive() and not t2.is_alive()
    assert not errors_case2
    assert len(case2_procs) == 1
    assert len(results_case2) == 2
    assert results_case2[0] is results_case2[1]
    assert results_case2[0].generation == 1
    mgr2.close()
    assert active_resource_lease_count() == baseline_leases

    # Case 3: Concurrent close_task during in-flight startup cancels publication and closes the
    # late-finishing session without resurrecting it or leaking its lease.
    gate_ready.clear()
    popen_entered.clear()
    case3_procs: list[_FakeSuspendedPopen] = []

    def _popen_case3(cmd: list[str], **kwargs: Any) -> _FakeSuspendedPopen:
        env = kwargs["env"]
        sid = env["RYN_JS_SESSION_ID"]
        gen = int(env["RYN_JS_GENERATION"])
        pid_val = 93000 + len(case3_procs)
        ready_line = (
            json.dumps(
                {
                    "v": 1,
                    "type": "ready",
                    "session_id": sid,
                    "generation": gen,
                    "engine": "quickjs-emscripten",
                    "release_variant": "RELEASE_SYNC",
                    "pid": pid_val,
                }
            )
            + "\n"
        ).encode("utf-8")
        proc = _FakeSuspendedPopen(pid=pid_val, handle=0x3456, stdout_lines=[])
        proc.stdout = _GatedStdoutPipe(ready_line)
        case3_procs.append(proc)
        popen_entered.set()
        return proc

    monkeypatch.setattr(subprocess, "Popen", _popen_case3)
    mgr3 = JsRuntimeManager(
        development_profile_root=out_dir,
        startup_timeout=5.0,
        _before_resume_verifier=_stub_verifier,
    )
    errors_case3: list[BaseException] = []

    def _starter_case3() -> None:
        try:
            mgr3.get_or_create_session("task_cancel_inflight")
        except BaseException as exc:
            errors_case3.append(exc)

    t_start = threading.Thread(target=_starter_case3, daemon=True)
    t_start.start()
    assert popen_entered.wait(timeout=2.0)

    # close_task completes immediately while startup is still waiting on gate_ready
    mgr3.close_task("task_cancel_inflight")
    gate_ready.set()
    t_start.join(timeout=2.0)
    assert not t_start.is_alive()
    assert len(errors_case3) == 1
    assert isinstance(errors_case3[0], JsRuntimeError)
    assert errors_case3[0].code == "ryn_js_session_closed"
    assert mgr3.get_session("task_cancel_inflight") is None
    assert mgr3.get_status()["active_session_count"] == 0
    assert case3_procs[0].killed is True
    assert active_resource_lease_count() == baseline_leases
    mgr3.close()



def test_worker_environment_strips_poisoned_path_and_node_variables(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verify ambient child environment sanitization strips caller PATH, NODE_*, and provider secrets."""
    monkeypatch.setenv("PATH", "C:\\Attacker\\Dir;C:\\Windows\\System32")
    monkeypatch.setenv("RYN_JS_NODE_EXECUTABLE", "C:\\Attacker\\Dir\\node.exe")
    monkeypatch.setenv("NODE_OPTIONS", "--require C:\\Attacker\\preload.js")
    monkeypatch.setenv("NODE_PATH", "C:\\Attacker\\node_modules")
    monkeypatch.setenv("NODE_EXTRA_CA_CERTS", "C:\\Attacker\\ca.pem")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-live-secret")
    monkeypatch.setenv("RYNMESH_TOKEN", "secret-token")

    clean_env = sanitized_worker_env("sess_clean_1", 2)
    assert "C:\\Attacker\\Dir" not in clean_env.get("PATH", "")
    assert "RYN_JS_NODE_EXECUTABLE" not in clean_env
    assert "NODE_OPTIONS" not in clean_env
    assert "NODE_PATH" not in clean_env
    assert "NODE_EXTRA_CA_CERTS" not in clean_env
    assert "OPENAI_API_KEY" not in clean_env
    assert "RYNMESH_TOKEN" not in clean_env
    assert clean_env["RYN_JS_SESSION_ID"] == "sess_clean_1"
    assert clean_env["RYN_JS_GENERATION"] == "2"
    assert clean_env["NODE_NO_WARNINGS"] == "1"


@pytest.mark.skipif(
    _resolve_test_dev_profile_root(PREPARED_SNAPSHOT_ROOT) is None,
    reason="Prepared 146-file runtime snapshot not present",
)
def test_live_mounted_sessions_pin_all_146_files_verify_os_identity_and_isolate_session_leases(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """End-to-end live verification of the 146-file prepared profile mounted in JsRuntimeManager."""
    snapshot_root = _resolve_test_dev_profile_root(PREPARED_SNAPSHOT_ROOT)
    assert snapshot_root is not None
    receipt = json.loads((snapshot_root / BUILD_RECEIPT_FILENAME).read_text(encoding="utf-8"))
    commitment = receipt["expected_profile_commitment"]
    expected_spec = ExpectedJsRuntimeProfileSpec(**commitment)

    # Poison ambient environment variables to prove live worker startup ignores them completely
    monkeypatch.setenv("PATH", "C:\\NonExistentPoisonedPath")
    monkeypatch.setenv("RYN_JS_NODE_EXECUTABLE", "C:\\NonExistentPoisonedPath\\node.exe")
    monkeypatch.setenv("NODE_OPTIONS", "--invalid-node-option-that-would-crash-if-inherited")
    monkeypatch.setenv("NODE_PATH", "C:\\NonExistentPoisonedPath\\node_modules")

    baseline_leases = active_resource_lease_count()
    dev_cfg = JsRuntimeDevelopmentConfig(
        explicit_profile_root=snapshot_root,
        expected_spec=expected_spec,
    )
    manager = JsRuntimeManager(development_config=dev_cfg)
    try:
        # Before first evaluation, zero leases and zero sessions exist
        init_status = manager.get_status()
        assert init_status["mode"] == "source_dev"
        assert init_status["files_verified"] is False
        assert init_status["actual_process_identity_verified"] is False
        assert init_status["runtime_handshake_verified"] is False
        assert init_status["release_verified"] is False
        assert init_status["installed_ready"] is False

        snap_a = manager.run_to_completion(
            "task_mount_a",
            """
            const { sky } = await import('@oai/sky');
            globalThis.mountValue = 100;
            return { target: sky.target, provider: sky.provider, val: globalThis.mountValue };
            """,
        )
        assert snap_a.status == "completed"
        assert snap_a.value == {"target": "windows", "provider": "ryn", "val": 100}

        session_a = manager.get_session("task_mount_a")
        assert session_a is not None
        assert session_a.files_verified is True
        assert session_a.actual_process_identity_verified is True
        assert session_a.runtime_handshake_verified is True
        assert session_a.verified_process_identity is not None
        assert session_a.verified_process_identity.pid == session_a.pid
        assert session_a.verified_process_identity.creation_time_filetime > 0
        assert session_a.verified_process_identity.node_sha256 == commitment["node_sha256"]
        assert session_a.verified_process_identity.node_bytes == commitment["node_bytes"]
        assert session_a.verified_process_identity.profile_digest == commitment["expected_manifest_sha256"]
        assert session_a.resource_lease.closed is False
        assert session_a.resource_lease.pinned_handle_count == 146
        assert len(session_a.resource_lease.pinned_relpaths) == 146
        assert session_a.resource_lease.pinned_directory_count == 22
        assert len(session_a.resource_lease.pinned_directory_relpaths) == 22
        assert active_resource_lease_count() == baseline_leases + 1

        # Start a second concurrent session -> holds its own independent 146-file + 22-directory lease
        snap_b = manager.run_to_completion("task_mount_b", "return 200;")
        assert snap_b.status == "completed"
        assert snap_b.value == 200
        session_b = manager.get_session("task_mount_b")
        assert session_b is not None
        assert session_b.resource_lease is not session_a.resource_lease
        assert session_b.resource_lease.pinned_handle_count == 146
        assert session_b.resource_lease.pinned_directory_count == 22
        assert active_resource_lease_count() == baseline_leases + 2

        # end_turn and cancel_task keep the persistent worker alive and keep its lease pinned
        manager.end_turn("task_mount_a")
        assert session_a.is_alive
        assert session_a.resource_lease.closed is False

        # Closing task_mount_b releases session_b's lease without disturbing session_a's lease or worker
        manager.close_task("task_mount_b")
        assert session_b.resource_lease.closed is True
        assert session_b.resource_lease.pinned_handle_count == 0
        assert session_b.resource_lease.pinned_directory_count == 0
        assert session_a.resource_lease.closed is False
        assert session_a.resource_lease.pinned_handle_count == 146
        assert session_a.resource_lease.pinned_directory_count == 22
        assert active_resource_lease_count() == baseline_leases + 1

        snap_a2 = manager.run_to_completion("task_mount_a", "return globalThis.mountValue + 23;")
        assert snap_a2.status == "completed"
        assert snap_a2.value == 123

        live_status = manager.get_status()
        assert live_status["mode"] == "source_dev"
        assert live_status["files_verified"] is True
        assert live_status["actual_process_identity_verified"] is True
        assert live_status["runtime_handshake_verified"] is True
        # source_dev never claims release_verified or installed_ready
        assert live_status["release_verified"] is False
        assert live_status["installed_ready"] is False
    finally:
        manager.close()

    assert active_resource_lease_count() == baseline_leases
    after_close_status = manager.get_status()
    assert after_close_status["active_session_count"] == 0
    assert after_close_status["actual_process_identity_verified"] is False
    assert after_close_status["runtime_handshake_verified"] is False

