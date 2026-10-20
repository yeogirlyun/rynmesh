"""Focused tests for Ryn NativeBridge binary verification, authoritative manifest, process_guard before_resume hook, PID binding, owner/config conflicts, and pure-memory cache_status."""

from __future__ import annotations

import dataclasses
import hashlib
import json
import os
import shutil
import struct
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any

import pytest

from rynmesh.agent_runtime.computer import (
    EXPECTED_WORKER_NAME,
    EXPECTED_WORKER_VERSION,
    IMAGE_FILE_DLL,
    IMAGE_FILE_EXECUTABLE_IMAGE,
    IMAGE_FILE_MACHINE_AMD64,
    IMAGE_NT_OPTIONAL_HDR64_MAGIC,
    MANIFEST_SCHEMA_VERSION,
    MAX_MANIFEST_BYTES,
    MIN_BINARY_SIZE_BYTES,
    PACKAGED_EXECUTABLE_BASENAME,
    PACKAGE_MANIFEST_PATH,
    PROTOCOL_SEMVER,
    SUPPORTED_TARGET_TRIPLE,
    WIRE_VERSION,
    ArtifactLease,
    DevelopmentBinaryResolver,
    NativeBridge,
    NativeBridgeError,
    NativeBridgeRejected,
    NativeWorkerClient,
    ProductionBinaryResolver,
    VerifiedArtifact,
    VerifiedProcessIdentity,
    bind_node_native_bridge,
    load_authoritative_manifest,
    load_manifest,
    open_and_verify_artifact_lease,
    open_dev_executable_lease,
    parse_and_validate_manifest_bytes,
    parse_and_validate_pe_headers,
    reconfigure_node_native_bridge,
    resolve_owner_id,
    validate_lexical_windows_path,
    validate_safe_installed_filename,
    verify_lease_release_authority,
    verify_windows_x64_host,
)
from rynmesh.agent_runtime.computer import binary as binary_mod
from rynmesh.llm_package import process_guard
from tests.test_agent_native_bridge import FAKE_WORKER_SCRIPT

EXE_BASENAME = "ryn_computer_use.exe"
MANIFEST_BASENAME = "computer-use-manifest.json"


def _build_synthetic_pe64_header(
    *,
    mz_magic: bytes = b"MZ",
    e_lfanew: int = 0x80,
    pe_sig: bytes = b"PE\x00\x00",
    machine: int = IMAGE_FILE_MACHINE_AMD64,
    num_sections: int = 1,
    size_of_opt_hdr: int = 0xF0,
    characteristics: int = IMAGE_FILE_EXECUTABLE_IMAGE | 0x0020,
    opt_magic: int = IMAGE_NT_OPTIONAL_HDR64_MAGIC,
    size_of_headers: int = 0x200,
    total_size: int = 4096,
) -> bytes:
    buf = bytearray(max(total_size, 512))
    buf[0:2] = mz_magic
    struct.pack_into("<I", buf, 0x3C, e_lfanew)
    if 0 <= e_lfanew <= len(buf) - 256:
        buf[e_lfanew : e_lfanew + 4] = pe_sig
        coff_off = e_lfanew + 4
        struct.pack_into(
            "<HHIIIHH",
            buf,
            coff_off,
            machine,
            num_sections,
            0,
            0,
            0,
            size_of_opt_hdr,
            characteristics,
        )
        opt_off = coff_off + 20
        struct.pack_into("<H", buf, opt_off, opt_magic)
        struct.pack_into("<I", buf, opt_off + 60, size_of_headers)
    return bytes(buf)


def _valid_manifest_dict(
    *,
    installed_filename: str = EXE_BASENAME,
    size: int = 65536,
    sha256_hex: str = "a" * 64,
    release_id: str = "rel_20261019_01",
    machine: int | str = IMAGE_FILE_MACHINE_AMD64,
    opt_magic: int | str = IMAGE_NT_OPTIONAL_HDR64_MAGIC,
) -> dict[str, Any]:
    return {
        "schemaVersion": MANIFEST_SCHEMA_VERSION,
        "releaseId": release_id,
        "wireVersion": WIRE_VERSION,
        "protocolVersion": PROTOCOL_SEMVER,
        "workerName": EXPECTED_WORKER_NAME,
        "workerVersion": EXPECTED_WORKER_VERSION,
        "targetTriple": SUPPORTED_TARGET_TRIPLE,
        "artifact": {
            "installedFilename": installed_filename,
            "size": size,
            "sha256": sha256_hex,
            "machine": machine,
            "optionalHeaderMagic": opt_magic,
        },
    }


# ============================================================================
# 1. Authoritative Release Manifest Validation & Fail-Closed Production Lookup
# ============================================================================


def test_manifest_strict_json_and_schema_validation() -> None:
    valid_raw = json.dumps(_valid_manifest_dict()).encode("utf-8")
    spec = parse_and_validate_manifest_bytes(valid_raw)
    assert spec.schema_version == MANIFEST_SCHEMA_VERSION
    assert spec.release_id == "rel_20261019_01"
    assert spec.wire_version == WIRE_VERSION
    assert spec.worker_name == EXPECTED_WORKER_NAME
    assert spec.worker_version == EXPECTED_WORKER_VERSION
    assert spec.protocol_version == PROTOCOL_SEMVER
    assert spec.target_triple == SUPPORTED_TARGET_TRIPLE
    assert spec.installed_filename == EXE_BASENAME
    assert spec.expected_size == 65536
    assert spec.expected_sha256 == "a" * 64
    assert spec.expected_machine == IMAGE_FILE_MACHINE_AMD64
    assert spec.expected_optional_header_magic == IMAGE_NT_OPTIONAL_HDR64_MAGIC
    assert spec.manifest_sha256 == hashlib.sha256(valid_raw).hexdigest()

    # String machine ("x86_64") and optionalHeaderMagic ("PE32+") also normalize to 0x8664 and 0x20B
    spec_str = parse_and_validate_manifest_bytes(
        json.dumps(
            _valid_manifest_dict(machine="x86_64", opt_magic="PE32+")
        ).encode("utf-8")
    )
    assert spec_str.expected_machine == IMAGE_FILE_MACHINE_AMD64
    assert spec_str.expected_optional_header_magic == IMAGE_NT_OPTIONAL_HDR64_MAGIC

    # Duplicate key rejected
    dup_raw = (
        b'{"schemaVersion":1,"schemaVersion":1,"releaseId":"rel_1","wireVersion":1,'
        b'"protocolVersion":"1.0.0","workerName":"ryn_computer_use","workerVersion":"0.1.0",'
        b'"targetTriple":"x86_64-pc-windows-msvc","artifact":{"installedFilename":"ryn_computer_use.exe",'
        b'"size":65536,"sha256":"'
        + (b"a" * 64)
        + b'","machine":34404,"optionalHeaderMagic":523}}'
    )
    with pytest.raises(NativeBridgeRejected) as exc_dup:
        parse_and_validate_manifest_bytes(dup_raw)
    assert exc_dup.value.code == "agent_computer_duplicate_json_key"

    # Unknown root or nested artifact field rejected
    unk_root = {**_valid_manifest_dict(), "unexpectedExtra": True}
    with pytest.raises(NativeBridgeRejected) as exc_unk:
        parse_and_validate_manifest_bytes(json.dumps(unk_root).encode("utf-8"))
    assert exc_unk.value.code == "agent_computer_manifest_invalid"

    unk_art = _valid_manifest_dict()
    unk_art["artifact"]["extraKey"] = 1
    with pytest.raises(NativeBridgeRejected) as exc_unk_art:
        parse_and_validate_manifest_bytes(json.dumps(unk_art).encode("utf-8"))
    assert exc_unk_art.value.code == "agent_computer_manifest_invalid"

    # Non-finite number rejected
    nan_raw = json.dumps(_valid_manifest_dict()).replace("65536", "NaN").encode("utf-8")
    with pytest.raises(NativeBridgeRejected) as exc_nan:
        parse_and_validate_manifest_bytes(nan_raw)
    assert exc_nan.value.code == "agent_computer_nonfinite_number"

    # Oversized manifest (> MAX_MANIFEST_BYTES) rejected before parsing
    with pytest.raises(NativeBridgeRejected) as exc_big:
        parse_and_validate_manifest_bytes(b" " * (MAX_MANIFEST_BYTES + 1))
    assert exc_big.value.code == "agent_computer_manifest_invalid"

    # Wrong schemaVersion
    bad_schema = {**_valid_manifest_dict(), "schemaVersion": 2}
    with pytest.raises(NativeBridgeRejected) as exc_schema:
        parse_and_validate_manifest_bytes(json.dumps(bad_schema).encode("utf-8"))
    assert exc_schema.value.code == "agent_computer_manifest_invalid"

    # Wrong workerName / workerVersion / protocolVersion / wireVersion
    bad_worker = {**_valid_manifest_dict(), "workerName": "other_worker"}
    with pytest.raises(NativeBridgeRejected) as exc_wname:
        parse_and_validate_manifest_bytes(json.dumps(bad_worker).encode("utf-8"))
    assert exc_wname.value.code == "agent_computer_worker_identity_mismatch"

    bad_proto = {**_valid_manifest_dict(), "protocolVersion": "2.0.0"}
    with pytest.raises(NativeBridgeRejected) as exc_proto:
        parse_and_validate_manifest_bytes(json.dumps(bad_proto).encode("utf-8"))
    assert exc_proto.value.code == "agent_computer_version_mismatch"

    bad_wire = {**_valid_manifest_dict(), "wireVersion": 99}
    with pytest.raises(NativeBridgeRejected) as exc_wire:
        parse_and_validate_manifest_bytes(json.dumps(bad_wire).encode("utf-8"))
    assert exc_wire.value.code == "agent_computer_version_mismatch"

    # Wrong targetTriple or machine -> arch_unsupported; wrong optionalHeaderMagic -> pe_invalid
    bad_triple = {**_valid_manifest_dict(), "targetTriple": "aarch64-pc-windows-msvc"}
    with pytest.raises(NativeBridgeRejected) as exc_triple:
        parse_and_validate_manifest_bytes(json.dumps(bad_triple).encode("utf-8"))
    assert exc_triple.value.code == "agent_computer_arch_unsupported"

    bad_mach = _valid_manifest_dict(machine=0x014C)
    with pytest.raises(NativeBridgeRejected) as exc_mach:
        parse_and_validate_manifest_bytes(json.dumps(bad_mach).encode("utf-8"))
    assert exc_mach.value.code == "agent_computer_arch_unsupported"

    bad_magic = _valid_manifest_dict(opt_magic=0x10B)
    with pytest.raises(NativeBridgeRejected) as exc_magic:
        parse_and_validate_manifest_bytes(json.dumps(bad_magic).encode("utf-8"))
    assert exc_magic.value.code == "agent_computer_pe_invalid"

    # Uppercase or bad length sha256 or out-of-range size rejected
    for bad_sha in ["A" * 64, "a" * 63, "g" * 64]:
        bad_hash = _valid_manifest_dict(sha256_hex=bad_sha)
        with pytest.raises(NativeBridgeRejected) as exc_sha:
            parse_and_validate_manifest_bytes(json.dumps(bad_hash).encode("utf-8"))
        assert exc_sha.value.code == "agent_computer_manifest_invalid"

    bad_size = _valid_manifest_dict(size=MIN_BINARY_SIZE_BYTES - 1)
    with pytest.raises(NativeBridgeRejected) as exc_sz:
        parse_and_validate_manifest_bytes(json.dumps(bad_size).encode("utf-8"))
    assert exc_sz.value.code == "agent_computer_manifest_invalid"

    # Unsafe installedFilename rejected
    for bad_name in [
        "../ryn_computer_use.exe",
        "sub\\ryn_computer_use.exe",
        "ryn_computer_use.exe:ads",
        "ryn_computer_use.bat",
    ]:
        bad_fn = _valid_manifest_dict(installed_filename=bad_name)
        with pytest.raises(NativeBridgeRejected) as exc_fn:
            parse_and_validate_manifest_bytes(json.dumps(bad_fn).encode("utf-8"))
        assert exc_fn.value.code == "agent_computer_path_invalid"


@pytest.mark.skipif(os.name != "nt", reason="Windows manifest handle loading tests")
def test_missing_production_manifest_and_unbundled_source_refused(tmp_path: Path) -> None:
    missing_manifest = tmp_path / "nonexistent-manifest.json"
    with pytest.raises(NativeBridgeRejected) as exc_missing:
        load_authoritative_manifest(missing_manifest)
    assert exc_missing.value.code == "agent_computer_manifest_missing"

    # Production bridge without dev_executable when manifest is missing refuses before spawn
    bridge = NativeBridge(
        manifest_path=missing_manifest,
        bundle_root=tmp_path,
    )
    try:
        assert bridge.cache_status()["status"] == "unknown"
        assert bridge.cache_status()["verified"] is False
        assert bridge.cache_status()["releaseVerified"] is False
        with pytest.raises(NativeBridgeRejected) as exc_call:
            bridge.list_windows(task_session_id="t_prod_missing")
        assert exc_call.value.code == "agent_computer_manifest_missing"
        assert exc_call.value.pre_dispatch is True
        assert bridge.cache_status()["status"] == "unknown"
    finally:
        bridge.close()

    # Even if a valid manifest is provided, unbundled source mode without explicit bundle_root/bundled_executable
    # must NOT search PATH, target/debug, or source sidecars (require_frozen defaults to True).
    manifest_file = tmp_path / MANIFEST_BASENAME
    manifest_file.write_text(json.dumps(_valid_manifest_dict()), encoding="utf-8")
    resolver = ProductionBinaryResolver(manifest_path=manifest_file)
    with pytest.raises(NativeBridgeRejected) as exc_unbundled:
        resolver.acquire_lease()
    assert exc_unbundled.value.code == "agent_computer_executable_not_found"


# ============================================================================
# 2. Path Lexical Validation, Containment, ADS, UNC, Unicode & Similar-Prefix
# ============================================================================


@pytest.mark.skipif(os.name != "nt", reason="Windows path lexical and handle containment rules")
def test_path_validation_rejects_ads_unc_device_traversal_and_prefix_sibling(
    tmp_path: Path,
) -> None:
    for invalid_path in [
        "relative\\ryn_computer_use.exe",
        f"{tmp_path}\\..\\ryn_computer_use.exe",
        f"{tmp_path}\\ryn_computer_use.exe:Zone.Identifier",
        "\\\\server\\share\\ryn_computer_use.exe",
        "\\\\.\\pipe\\ryn_computer_use.exe",
        "\\\\?\\UNC\\server\\share\\ryn_computer_use.exe",
    ]:
        with pytest.raises(NativeBridgeRejected) as exc_path:
            validate_lexical_windows_path(invalid_path)
        assert exc_path.value.code == "agent_computer_path_invalid"

    for invalid_name in ["CON.exe", "NUL.exe", "bad:name.exe", "trailing.exe."]:
        with pytest.raises(NativeBridgeRejected) as exc_name:
            validate_safe_installed_filename(invalid_name)
        assert exc_name.value.code == "agent_computer_path_invalid"

    # Malicious similar-prefix sibling directory: bundle_root = ".../app", binary in ".../app_evil"
    base_py = Path(getattr(sys, "_base_executable", sys.executable)).resolve()
    app_dir = tmp_path / "app"
    app_evil_dir = tmp_path / "app_evil"
    app_dir.mkdir()
    app_evil_dir.mkdir()
    evil_exe = app_evil_dir / EXE_BASENAME
    shutil.copyfile(base_py, evil_exe)
    raw_bytes = evil_exe.read_bytes()
    spec = parse_and_validate_manifest_bytes(
        json.dumps(
            _valid_manifest_dict(
                size=len(raw_bytes),
                sha256_hex=hashlib.sha256(raw_bytes).hexdigest(),
            )
        ).encode("utf-8")
    )

    with pytest.raises(NativeBridgeRejected) as exc_sibling:
        open_and_verify_artifact_lease(
            spec=spec,
            bundle_root=app_dir,
            candidate_executable=evil_exe,
        )
    assert exc_sibling.value.code == "agent_computer_path_invalid"

    # Legitimate Unicode directory path containing a real PE64 binary copy must succeed
    unicode_dir = tmp_path / "子陵_安全验证_目录"
    unicode_dir.mkdir()
    good_exe = unicode_dir / EXE_BASENAME
    shutil.copyfile(base_py, good_exe)
    lease = open_and_verify_artifact_lease(
        spec=spec,
        bundle_root=unicode_dir,
    )
    try:
        assert lease.verified_artifact.verified is True
        assert lease.verified_artifact.release_verified is False
        assert lease.verified_artifact.mode == "development"
        assert lease.verified_artifact.sha256 == spec.expected_sha256
    finally:
        lease.close()


# ============================================================================
# 3. PE64 Header Boundaries (Pure Byte Parsing, Never Executing Fabricated PE)
# ============================================================================


def test_pe64_header_boundary_validation() -> None:
    good_pe = _build_synthetic_pe64_header()
    info = parse_and_validate_pe_headers(good_pe, file_size=len(good_pe))
    assert info.machine == IMAGE_FILE_MACHINE_AMD64
    assert info.optional_header_magic == IMAGE_NT_OPTIONAL_HDR64_MAGIC
    assert info.is_executable is True
    assert info.is_dll is False

    # Missing MZ
    bad_mz = _build_synthetic_pe64_header(mz_magic=b"NO")
    with pytest.raises(NativeBridgeRejected) as exc_mz:
        parse_and_validate_pe_headers(bad_mz, file_size=len(bad_mz))
    assert exc_mz.value.code == "agent_computer_pe_invalid"

    # Out-of-bounds e_lfanew
    bad_lfanew = _build_synthetic_pe64_header(e_lfanew=0x7FFF)
    with pytest.raises(NativeBridgeRejected) as exc_lfanew:
        parse_and_validate_pe_headers(bad_lfanew, file_size=len(bad_lfanew))
    assert exc_lfanew.value.code == "agent_computer_pe_invalid"

    # Corrupt PE\0\0 signature
    bad_pesig = _build_synthetic_pe64_header(pe_sig=b"NE\x00\x00")
    with pytest.raises(NativeBridgeRejected) as exc_pesig:
        parse_and_validate_pe_headers(bad_pesig, file_size=len(bad_pesig))
    assert exc_pesig.value.code == "agent_computer_pe_invalid"

    # x86 32-bit machine (0x014C) -> pe_invalid
    bad_x86 = _build_synthetic_pe64_header(machine=0x014C)
    with pytest.raises(NativeBridgeRejected) as exc_x86:
        parse_and_validate_pe_headers(bad_x86, file_size=len(bad_x86))
    assert exc_x86.value.code == "agent_computer_pe_invalid"

    # PE32 optional header magic (0x10B) -> pe_invalid
    bad_pe32 = _build_synthetic_pe64_header(opt_magic=0x10B)
    with pytest.raises(NativeBridgeRejected) as exc_pe32:
        parse_and_validate_pe_headers(bad_pe32, file_size=len(bad_pe32))
    assert exc_pe32.value.code == "agent_computer_pe_invalid"

    # DLL image characteristic (0x2000) must be rejected as worker executable
    bad_dll = _build_synthetic_pe64_header(
        characteristics=IMAGE_FILE_EXECUTABLE_IMAGE | IMAGE_FILE_DLL
    )
    with pytest.raises(NativeBridgeRejected) as exc_dll:
        parse_and_validate_pe_headers(bad_dll, file_size=len(bad_dll))
    assert exc_dll.value.code == "agent_computer_pe_invalid"


# ============================================================================
# 4. Handle Pinning, Sharing Lock, Tamper Rejection & Post-Close Cleanup
# ============================================================================


@pytest.mark.skipif(os.name != "nt", reason="Windows handle pinning and sharing lock tests")
def test_handle_pinning_blocks_concurrent_mutation_and_releases_on_close(
    tmp_path: Path,
) -> None:
    verify_windows_x64_host()
    base_py = Path(getattr(sys, "_base_executable", sys.executable)).resolve()
    bundle_dir = tmp_path / "bundle"
    bundle_dir.mkdir()
    exe_copy = bundle_dir / EXE_BASENAME
    shutil.copyfile(base_py, exe_copy)
    raw = exe_copy.read_bytes()
    sha = hashlib.sha256(raw).hexdigest()

    manifest_path = bundle_dir / MANIFEST_BASENAME
    manifest_path.write_text(
        json.dumps(_valid_manifest_dict(size=len(raw), sha256_hex=sha)),
        encoding="utf-8",
    )

    resolver = ProductionBinaryResolver(
        manifest_path=manifest_path,
        bundle_root=bundle_dir,
    )
    lease = resolver.acquire_lease()
    try:
        assert lease.verified_artifact.verified is True
        assert lease.verified_artifact.release_verified is False
        assert lease.verified_artifact.mode == "development"
        assert lease.verified_artifact.volume_serial > 0
        assert (
            lease.verified_artifact.file_id_high > 0
            or lease.verified_artifact.file_id_low > 0
        )

        # While ArtifactLease is held, opening for write or unlinking must fail with PermissionError
        with pytest.raises(PermissionError):
            with open(exe_copy, "ab") as f:
                f.write(b"tamper")
        with pytest.raises(PermissionError):
            os.unlink(exe_copy)
    finally:
        lease.close()

    # Once lease is closed, the file handle is released and the temporary copy can be modified/deleted
    os.unlink(exe_copy)
    assert not exe_copy.exists()

    # Staged DevelopmentBinaryResolver verifies PE/hash/handles identically but sets release_verified=False
    shutil.copyfile(base_py, exe_copy)
    dev_resolver = DevelopmentBinaryResolver(
        staging_root=bundle_dir,
        manifest_path=manifest_path,
    )
    dev_lease = dev_resolver.acquire_lease()
    try:
        assert dev_lease.verified_artifact.mode == "development"
        assert dev_lease.verified_artifact.verified is True
        assert dev_lease.verified_artifact.release_verified is False
    finally:
        dev_lease.close()

    # Tampered binary copy: hash mismatch fails closed AND closes the handle so file is deletable
    with open(exe_copy, "r+b") as f:
        f.seek(-1, os.SEEK_END)
        last_byte = f.read(1)
        f.seek(-1, os.SEEK_END)
        f.write(bytes([last_byte[0] ^ 0xFF]))

    with pytest.raises(NativeBridgeRejected) as exc_tamper:
        resolver.acquire_lease()
    assert exc_tamper.value.code == "agent_computer_binary_tampered"

    # Handle must not be leaked on verification failure
    os.unlink(exe_copy)
    assert not exe_copy.exists()


# ============================================================================
# 5. process_guard._windows_job before_resume Hook Ordering & Zero-Resume on Failure
# ============================================================================


@pytest.mark.skipif(os.name != "nt", reason="Windows Job Object and suspended process tests")
def test_process_guard_before_resume_hook_and_zero_execution_on_failure(
    tmp_path: Path,
) -> None:
    base_py = str(Path(getattr(sys, "_base_executable", sys.executable)).resolve())
    marker_file = tmp_path / "child_ran.txt"
    child_script = tmp_path / "child_marker.py"
    child_script.write_text(
        f"from pathlib import Path\nPath({str(marker_file)!r}).write_text('ran', encoding='utf-8')\n",
        encoding="utf-8",
    )

    # 1. Failing before_resume hook must terminate the suspended process via its Job BEFORE NtResumeProcess
    hook_called = False

    def _failing_hook(proc: Any, job: Any) -> None:
        nonlocal hook_called
        hook_called = True
        assert proc.poll() is None
        assert job
        assert not marker_file.exists()
        raise RuntimeError("abort_before_resume")

    proc_fail = subprocess.Popen(
        [base_py, str(child_script)],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        creationflags=0x4 | subprocess.CREATE_NO_WINDOW,
    )
    try:
        with pytest.raises(RuntimeError, match="abort_before_resume"):
            process_guard._windows_job(proc_fail, before_resume=_failing_hook)
        assert hook_called is True
        proc_fail.wait(timeout=3.0)
        assert proc_fail.poll() is not None
        assert not marker_file.exists()
    finally:
        for s in (proc_fail.stdin, proc_fail.stdout, proc_fail.stderr):
            if s is not None:
                try:
                    s.close()
                except Exception:
                    pass
        process_guard.terminate(proc_fail)

    # 2. Succeeding before_resume hook runs before child execution, then resumes child normally
    order: list[str] = []

    def _ok_hook(proc: Any, job: Any) -> None:
        assert not marker_file.exists()
        order.append("before_resume")

    proc_ok = subprocess.Popen(
        [base_py, str(child_script)],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        creationflags=0x4 | subprocess.CREATE_NO_WINDOW,
    )
    try:
        proc_ok._ryn_job = process_guard._windows_job(proc_ok, before_resume=_ok_hook)  # type: ignore[attr-defined]
        proc_ok.wait(timeout=5.0)
        assert order == ["before_resume"]
        assert marker_file.exists()
        assert marker_file.read_text(encoding="utf-8") == "ran"
    finally:
        for s in (proc_ok.stdin, proc_ok.stdout, proc_ok.stderr):
            if s is not None:
                try:
                    s.close()
                except Exception:
                    pass
        process_guard.terminate(proc_ok)


@pytest.mark.skipif(os.name != "nt", reason="Windows suspended process FileId verification tests")
def test_suspended_process_file_id_mismatch_aborts_before_resume(
    tmp_path: Path,
) -> None:
    base_py = Path(getattr(sys, "_base_executable", sys.executable)).resolve()
    exe_a = tmp_path / "python_a.exe"
    exe_b = tmp_path / "python_b.exe"
    shutil.copyfile(base_py, exe_a)
    shutil.copyfile(base_py, exe_b)

    marker = tmp_path / "should_never_run.txt"
    script = tmp_path / "never_run.py"
    script.write_text(
        f"from pathlib import Path\nPath({str(marker)!r}).write_text('executed', encoding='utf-8')\n",
        encoding="utf-8",
    )

    # Acquire lease on exe_a, but spawn exe_b: verify_suspended_process detects FileId mismatch before resume!
    lease_a = open_dev_executable_lease([str(exe_a), str(script)])
    proc_b = subprocess.Popen(
        [str(exe_b), str(script)],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        creationflags=0x4 | subprocess.CREATE_NO_WINDOW,
    )
    try:
        with pytest.raises(NativeBridgeRejected) as exc_mismatch:
            process_guard._windows_job(
                proc_b,
                before_resume=lease_a.verify_suspended_process,
            )
        assert exc_mismatch.value.code == "agent_computer_process_identity_mismatch"
        proc_b.wait(timeout=3.0)
        assert proc_b.poll() is not None
        assert not marker.exists()
    finally:
        for s in (proc_b.stdin, proc_b.stdout, proc_b.stderr):
            if s is not None:
                try:
                    s.close()
                except Exception:
                    pass
        process_guard.terminate(proc_b)
        lease_a.close()


# ============================================================================
# 6. Handshake PID Binding (Closes Bridge03 probe-11 Self-Reported PID Gap)
# ============================================================================


@pytest.mark.skipif(os.name != "nt", reason="Windows subprocess handshake PID verification")
def test_handshake_rejects_self_reported_pid_mismatch_and_never_caches(
    tmp_path: Path,
) -> None:
    script_path = tmp_path / "fake_worker.py"
    script_path.write_text(FAKE_WORKER_SCRIPT, encoding="utf-8")
    base_py = str(Path(getattr(sys, "_base_executable", sys.executable)).resolve())
    state_file = tmp_path / "wrong_pid_state.jsonl"

    bridge = NativeBridge(
        dev_executable=[base_py, str(script_path), "handshake_wrong_pid", str(state_file)],
    )
    try:
        with pytest.raises(NativeBridgeRejected) as exc_pid:
            bridge.list_windows(task_session_id="t_pid")
        assert exc_pid.value.code == "agent_computer_worker_identity_mismatch"
        assert exc_pid.value.pre_dispatch is True

        status = bridge.cache_status()
        assert status["status"] == "unknown"
        assert status["cached"] is False
        assert status["live"] is False
        assert status["verified"] is False
        assert status["releaseVerified"] is False
        assert status["binding"] is None
    finally:
        bridge.close()


@pytest.mark.skipif(os.name != "nt", reason="Windows subprocess transport_factory PID binding")
def test_injected_transport_factory_requires_matching_process_identity(
    tmp_path: Path,
) -> None:
    script_path = tmp_path / "fake_worker.py"
    script_path.write_text(FAKE_WORKER_SCRIPT, encoding="utf-8")
    base_py = str(Path(getattr(sys, "_base_executable", sys.executable)).resolve())
    state_file = tmp_path / "tf_state.jsonl"
    cmd = [base_py, str(script_path), "normal", str(state_file)]

    def _spawn() -> Any:
        proc = subprocess.Popen(
            cmd,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            creationflags=0x4 | subprocess.CREATE_NO_WINDOW,
        )
        proc._ryn_job = process_guard._windows_job(proc)  # type: ignore[attr-defined]
        return proc

    # 1. Providing a wrong process_identity_provider PID must fail closed during handshake
    client_bad = NativeWorkerClient(
        transport_factory=_spawn,
        process_identity_provider=lambda proc: VerifiedProcessIdentity(
            pid=proc.pid + 999,
            creation_time_ft=123456789,
            image_path=base_py,
            volume_serial=1,
            file_id_high=0,
            file_id_low=1,
            verified_by="test_identity_provider",
        ),
    )
    try:
        with pytest.raises(NativeBridgeRejected) as exc_bad:
            client_bad.ensure_session()
        assert exc_bad.value.code == "agent_computer_worker_identity_mismatch"
        assert client_bad.cache_status()["status"] == "unknown"
    finally:
        client_bad.close()

    # 2. Providing the actual process PID via process_identity_provider succeeds and marks mode='test', verified=False
    client_good = NativeWorkerClient(
        transport_factory=_spawn,
        process_identity_provider=lambda proc: VerifiedProcessIdentity(
            pid=proc.pid,
            creation_time_ft=123456789,
            image_path=base_py,
            volume_serial=1,
            file_id_high=0,
            file_id_low=1,
            verified_by="test_identity_provider",
        ),
    )
    try:
        session = client_good.ensure_session()
        assert session.worker_pid == session.process.pid
        status = client_good.cache_status()
        assert status["status"] == "ready"
        assert status["mode"] == "test"
        assert status["verified"] is False
        assert status["releaseVerified"] is False
        assert status["worker"]["pid"] == session.process.pid
    finally:
        client_good.close()


# ============================================================================
# 7. Owner & Configuration Conflicts + Explicit Reconfigure Preserving No-Replay
# ============================================================================


@pytest.mark.skipif(os.name != "nt", reason="Windows native bridge reconfigure and conflict tests")
def test_owner_and_config_conflict_detection_and_explicit_reconfigure(
    tmp_path: Path,
) -> None:
    class DummyOwner:
        pass

    owner_a = DummyOwner()
    owner_b = DummyOwner()

    script_path = tmp_path / "fake_worker.py"
    script_path.write_text(FAKE_WORKER_SCRIPT, encoding="utf-8")
    base_py = str(Path(getattr(sys, "_base_executable", sys.executable)).resolve())

    state_1 = tmp_path / "s1.jsonl"
    state_2 = tmp_path / "s2.jsonl"
    cmd_1 = [base_py, str(script_path), "commit_eof_no_reply", str(state_1)]
    cmd_2 = [base_py, str(script_path), "normal", str(state_2)]

    bridge_a = bind_node_native_bridge(owner_a, dev_executable=cmd_1)
    try:
        # Retrieve-only returns existing bridge
        assert bind_node_native_bridge(owner_a) is bridge_a
        # Idempotent identical config returns existing bridge without error
        assert bind_node_native_bridge(owner_a, dev_executable=list(cmd_1)) is bridge_a

        # Different explicit config on same owner must raise agent_computer_config_conflict
        with pytest.raises(NativeBridgeRejected) as exc_cfg:
            bind_node_native_bridge(owner_a, dev_executable=cmd_2)
        assert exc_cfg.value.code == "agent_computer_config_conflict"
        assert exc_cfg.value.pre_dispatch is True

        # Cross-owner reuse of bridge_a on owner_b must raise agent_computer_owner_conflict
        with pytest.raises(NativeBridgeRejected) as exc_own:
            bind_node_native_bridge(owner_b, bridge=bridge_a)
        assert exc_own.value.code == "agent_computer_owner_conflict"
        assert exc_own.value.pre_dispatch is True

        # Execute a write on bridge_a that fails post-commit (EOF after commit_received) so op is uncertain
        win_list = bridge_a.list_windows(task_session_id="task_reconf")
        target_win = win_list["windows"][0]["window"]
        obs = bridge_a.get_state(task_session_id="task_reconf", window=target_win)

        first_ready_calls = 0

        def _first_on_ready(_info: dict[str, Any]) -> None:
            nonlocal first_ready_calls
            first_ready_calls += 1

        with pytest.raises(NativeBridgeError) as exc_post:
            bridge_a.execute_action(
                task_session_id="task_reconf",
                operation_id="op_no_replay_across_reconf",
                window=target_win,
                expected_state_token=obs["stateToken"],
                action={"type": "key_press", "key": "Enter"},
                on_ready=_first_on_ready,
            )
        assert not isinstance(exc_post.value, NativeBridgeRejected)
        assert exc_post.value.commit_attempted is True
        assert exc_post.value.dispatched is True
        assert exc_post.value.pre_dispatch is False
        assert exc_post.value.outcome == "unknown"
        assert first_ready_calls == 1

        events_1 = [
            json.loads(line)
            for line in state_1.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        assert sum(e["event"] == "prepare_ready" for e in events_1) == 1
        assert sum(e["event"] == "commit_received" for e in events_1) == 1

        # Now explicitly reconfigure owner_a to cmd_2
        reconfigured = reconfigure_node_native_bridge(owner_a, dev_executable=cmd_2)
        assert reconfigured is bridge_a
        stale_status = bridge_a.cache_status()
        assert stale_status["status"] == "stale"
        assert stale_status["live"] is False

        # Old task window / token from before reconfigure must be invalidated
        assert bridge_a.resolve_task_window("task_reconf", hwnd=target_win["hwnd"]) is None

        # Re-observe on new worker generation
        win_list_2 = bridge_a.list_windows(task_session_id="task_reconf")
        target_win_2 = win_list_2["windows"][0]["window"]
        obs_2 = bridge_a.get_state(task_session_id="task_reconf", window=target_win_2)
        assert bridge_a.cache_status()["status"] == "ready"
        assert bridge_a.cache_status()["workerGeneration"] == 2
        assert bridge_a.resolve_task_window("task_reconf", hwnd=target_win_2["hwnd"]) == target_win_2

        post_reconf_ready_calls = 0

        def _unexpected_on_ready(_info: dict[str, Any]) -> None:
            nonlocal post_reconf_ready_calls
            post_reconf_ready_calls += 1

        # Case (1): Retrying the EXACT original (task_session_id, operation_id, window, expected_state_token, action)
        # tuple after reconfigure MUST fail closed with post-dispatch NativeBridgeError
        # `agent_computer_commit_already_attempted`, preserving original commit_attempted/unknown receipt.
        with pytest.raises(NativeBridgeError) as exc_replay_exact:
            bridge_a.execute_action(
                task_session_id="task_reconf",
                operation_id="op_no_replay_across_reconf",
                window=target_win,
                expected_state_token=obs["stateToken"],
                action={"type": "key_press", "key": "Enter"},
                on_ready=_unexpected_on_ready,
            )
        assert type(exc_replay_exact.value) is NativeBridgeError
        assert not isinstance(exc_replay_exact.value, NativeBridgeRejected)
        assert exc_replay_exact.value.code == "agent_computer_commit_already_attempted"
        assert exc_replay_exact.value.phase == "post_dispatch"
        assert exc_replay_exact.value.commit_attempted is True
        assert exc_replay_exact.value.dispatched is True
        assert exc_replay_exact.value.pre_dispatch is False
        assert exc_replay_exact.value.outcome == "unknown"
        assert post_reconf_ready_calls == 0

        # Case (2): Reusing the same operation_id with the new generation's window/state token (or changed args)
        # MUST be rejected pre-dispatch with NativeBridgeRejected `agent_operation_id_conflict` without
        # invoking on_ready or prepare/commit, and WITHOUT overwriting the preserved unknown operation record.
        with pytest.raises(NativeBridgeRejected) as exc_conflict:
            bridge_a.execute_action(
                task_session_id="task_reconf",
                operation_id="op_no_replay_across_reconf",
                window=target_win_2,
                expected_state_token=obs_2["stateToken"],
                action={"type": "key_press", "key": "Enter"},
                on_ready=_unexpected_on_ready,
            )
        assert exc_conflict.value.code == "agent_operation_id_conflict"
        assert exc_conflict.value.phase == "pre_dispatch"
        assert exc_conflict.value.pre_dispatch is True
        assert exc_conflict.value.commit_attempted is False
        assert exc_conflict.value.dispatched is False
        assert post_reconf_ready_calls == 0

        # Verify the conflict rejection did NOT erase or overwrite the original unknown operation record:
        # retrying the exact original tuple again still raises `agent_computer_commit_already_attempted`!
        with pytest.raises(NativeBridgeError) as exc_replay_again:
            bridge_a.execute_action(
                task_session_id="task_reconf",
                operation_id="op_no_replay_across_reconf",
                window=target_win,
                expected_state_token=obs["stateToken"],
                action={"type": "key_press", "key": "Enter"},
                on_ready=_unexpected_on_ready,
            )
        assert type(exc_replay_again.value) is NativeBridgeError
        assert not isinstance(exc_replay_again.value, NativeBridgeRejected)
        assert exc_replay_again.value.code == "agent_computer_commit_already_attempted"
        assert exc_replay_again.value.commit_attempted is True
        assert exc_replay_again.value.dispatched is True
        assert exc_replay_again.value.pre_dispatch is False
        assert exc_replay_again.value.outcome == "unknown"
        assert post_reconf_ready_calls == 0

        # Inspect captured fake worker 2 log: 0 action.prepare and 0 action.commit on the second worker
        events_2 = [
            json.loads(line)
            for line in state_2.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        assert sum(e["event"] == "prepare_ready" for e in events_2) == 0
        assert sum(e["event"] == "commit_received" for e in events_2) == 0
    finally:
        bridge_a.close()


# ============================================================================
# 8. Pure-Memory cache_status() Verification (Zero Syscalls / Polling / I/O)
# ============================================================================


@pytest.mark.skipif(os.name != "nt", reason="Windows pure-memory cache_status verification")
def test_cache_status_is_pure_memory_when_io_and_poll_are_monkeypatched_to_raise(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    script_path = tmp_path / "fake_worker.py"
    script_path.write_text(FAKE_WORKER_SCRIPT, encoding="utf-8")
    base_py = str(Path(getattr(sys, "_base_executable", sys.executable)).resolve())
    state_file = tmp_path / "pure_cache.jsonl"
    cmd = [base_py, str(script_path), "normal", str(state_file)]

    bridge = NativeBridge(dev_executable=cmd)
    try:
        bridge.list_windows(task_session_id="task_cache")
        session = bridge.client._session
        assert session is not None and session.process is not None

        def _forbidden(*_args: Any, **_kwargs: Any) -> Any:
            raise AssertionError("Forbidden I/O or process syscall invoked during cache_status()")

        with monkeypatch.context() as m:
            m.setattr(Path, "stat", _forbidden)
            m.setattr(Path, "resolve", _forbidden)
            m.setattr(Path, "open", _forbidden)
            m.setattr(os, "stat", _forbidden)
            m.setattr("builtins.open", _forbidden)
            m.setattr(hashlib, "sha256", _forbidden)
            m.setattr(subprocess, "Popen", _forbidden)
            m.setattr(session.process, "poll", _forbidden)

            snap1 = bridge.cache_status()
            assert snap1["status"] == "ready"
            assert snap1["cached"] is True
            assert snap1["live"] is True
            assert snap1["mode"] == "development"
            assert snap1["verified"] is False
            assert snap1["releaseVerified"] is False
            assert snap1["ownerId"] == bridge.owner_id
            assert snap1["configFingerprint"] == bridge.config_identity.config_fingerprint
            assert snap1["worker"]["pid"] == session.process.pid
            assert snap1["binding"]["process"]["pid"] == session.process.pid

            # Mutating returned snapshot must never mutate internal cached state
            snap1["worker"]["name"] = "mutated_in_test"
            snap1["binding"]["process"]["pid"] = 999999

            snap2 = bridge.cache_status()
            assert snap2["worker"]["name"] == EXPECTED_WORKER_NAME
            assert snap2["binding"]["process"]["pid"] == session.process.pid
    finally:
        bridge.close()

    post_close = bridge.cache_status()
    assert post_close["status"] == "stale"
    assert post_close["live"] is False
    assert post_close["closed"] is True


# ============================================================================
# 9. Asynchronous Worker Exit Updates Pure-Memory Liveness Automatically
# ============================================================================


@pytest.mark.skipif(os.name != "nt", reason="Windows async reader liveness update test")
def test_async_worker_exit_marks_cache_status_stale_without_poll_in_cache_status(
    tmp_path: Path,
) -> None:
    script_path = tmp_path / "fake_worker.py"
    script_path.write_text(FAKE_WORKER_SCRIPT, encoding="utf-8")
    base_py = str(Path(getattr(sys, "_base_executable", sys.executable)).resolve())
    state_file = tmp_path / "async_exit.jsonl"
    cmd = [base_py, str(script_path), "normal", str(state_file)]

    bridge = NativeBridge(dev_executable=cmd)
    try:
        bridge.list_windows(task_session_id="t_async")
        assert bridge.cache_status()["status"] == "ready"
        assert bridge.cache_status()["live"] is True

        session = bridge.client._session
        assert session is not None and session.process is not None
        process_guard.terminate(session.process)

        deadline = time.monotonic() + 2.0
        while time.monotonic() < deadline:
            if not bridge.cache_status()["live"]:
                break
            time.sleep(0.02)

        snap = bridge.cache_status()
        assert snap["status"] == "stale"
        assert snap["live"] is False
        assert snap["cached"] is True
    finally:
        bridge.close()


# ============================================================================
# 10. Reconfigure During In-Flight ensure_session Single-Flight Handover (P1)
# ============================================================================


@pytest.mark.skipif(os.name != "nt", reason="Windows subprocess single-flight reconfigure barrier tests")
def test_reconfigure_during_inflight_ensure_session_unblocks_waiters_and_retires_old_candidate(
    tmp_path: Path,
) -> None:
    class DummyOwner:
        pass

    script_path = tmp_path / "fake_worker.py"
    script_path.write_text(FAKE_WORKER_SCRIPT, encoding="utf-8")
    base_py = str(Path(getattr(sys, "_base_executable", sys.executable)).resolve())

    def _make_proc(state_name: str) -> subprocess.Popen[bytes]:
        state_file = tmp_path / state_name
        cmd = [base_py, str(script_path), "normal", str(state_file)]
        proc = subprocess.Popen(
            cmd,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            creationflags=0x4 | subprocess.CREATE_NO_WINDOW,
        )
        proc._ryn_job = process_guard._windows_job(proc)  # type: ignore[attr-defined]
        return proc

    # --- Scenario A: Reconfigure while first ensure_session leader is blocked in transport_factory ---
    owner_a = DummyOwner()
    entered_old_a = threading.Event()
    release_old_a = threading.Event()
    old_procs_a: list[subprocess.Popen[bytes]] = []
    fast_procs_a: list[subprocess.Popen[bytes]] = []
    fast_calls_a = 0

    def _slow_factory_a() -> Any:
        entered_old_a.set()
        release_old_a.wait(timeout=5.0)
        proc = _make_proc("p1_old_a.jsonl")
        old_procs_a.append(proc)
        return proc

    def _fast_factory_a() -> Any:
        nonlocal fast_calls_a
        fast_calls_a += 1
        proc = _make_proc(f"p1_fast_a_{fast_calls_a}.jsonl")
        fast_procs_a.append(proc)
        return proc

    bridge_a = bind_node_native_bridge(owner_a, transport_factory=_slow_factory_a)
    try:
        old_err_a: list[BaseException] = []
        new_res_a: list[dict[str, Any]] = []
        new_err_a: list[BaseException] = []
        new_done_a = threading.Event()

        def _run_old_a() -> None:
            try:
                bridge_a.list_windows(task_session_id="t_old_a", timeout=2.0)
            except BaseException as exc:
                old_err_a.append(exc)

        def _run_new_a() -> None:
            try:
                new_res_a.append(
                    bridge_a.list_windows(task_session_id="t_new_a", timeout=2.0)
                )
            except BaseException as exc:
                new_err_a.append(exc)
            finally:
                new_done_a.set()

        t_old_a = threading.Thread(target=_run_old_a, daemon=True)
        t_old_a.start()
        assert entered_old_a.wait(timeout=2.0)
        assert bridge_a._client._connecting is True
        assert bridge_a._client._active_connect_attempt is not None

        # Reconfigure while old leader is still blocked inside _slow_factory_a
        reconfigured_a = reconfigure_node_native_bridge(
            owner_a, transport_factory=_fast_factory_a
        )
        assert reconfigured_a is bridge_a
        assert bridge_a._client._connecting is False
        assert bridge_a._client._active_connect_attempt is None

        # New caller arrives while old leader is STILL blocked; must become new leader and succeed immediately!
        t_new_a = threading.Thread(target=_run_new_a, daemon=True)
        t_new_a.start()
        assert new_done_a.wait(timeout=3.0), "New caller hung waiting on invalidated old connect claim"
        assert not new_err_a
        assert len(new_res_a) == 1
        assert fast_calls_a == 1
        status_a = bridge_a.cache_status()
        assert status_a["status"] == "ready"
        assert status_a["live"] is True
        assert status_a["workerGeneration"] == 2
        assert status_a["worker"]["pid"] == fast_procs_a[0].pid

        # Now release old leader; it must abort its own candidate cleanly and never disturb the new session
        release_old_a.set()
        t_old_a.join(timeout=3.0)
        t_new_a.join(timeout=1.0)
        assert not t_old_a.is_alive()
        assert len(old_err_a) == 1
        assert isinstance(old_err_a[0], NativeBridgeRejected)
        assert old_err_a[0].code == "agent_computer_closed"
        assert len(old_procs_a) == 1
        old_procs_a[0].wait(timeout=3.0)
        assert old_procs_a[0].poll() is not None

        status_a_after = bridge_a.cache_status()
        assert status_a_after["status"] == "ready"
        assert status_a_after["live"] is True
        assert status_a_after["workerGeneration"] == 2
        assert status_a_after["worker"]["pid"] == fast_procs_a[0].pid
        assert bridge_a._client._connecting is False
        assert bridge_a._client._active_connect_attempt is None
    finally:
        release_old_a.set()
        bridge_a.close()
        for p in old_procs_a + fast_procs_a:
            process_guard.terminate(p)

    # --- Scenario B: Waiter already queued in _cond.wait() when reconfigure fires, and old leader finishes
    #     WHILE new leader is mid-connect (old leader must NOT clear new leader's _connecting claim!) ---
    owner_b = DummyOwner()
    entered_old_b = threading.Event()
    release_old_b = threading.Event()
    entered_new_b = threading.Event()
    release_new_b = threading.Event()
    old_procs_b: list[subprocess.Popen[bytes]] = []
    new_procs_b: list[subprocess.Popen[bytes]] = []
    new_factory_calls_b = 0

    def _slow_factory_b() -> Any:
        entered_old_b.set()
        release_old_b.wait(timeout=5.0)
        proc = _make_proc("p1_old_b.jsonl")
        old_procs_b.append(proc)
        return proc

    def _gated_new_factory_b() -> Any:
        nonlocal new_factory_calls_b
        new_factory_calls_b += 1
        entered_new_b.set()
        release_new_b.wait(timeout=5.0)
        proc = _make_proc(f"p1_new_b_{new_factory_calls_b}.jsonl")
        new_procs_b.append(proc)
        return proc

    bridge_b = bind_node_native_bridge(owner_b, transport_factory=_slow_factory_b)
    try:
        old_err_b: list[BaseException] = []
        waiter1_res: list[dict[str, Any]] = []
        waiter1_err: list[BaseException] = []
        waiter2_res: list[dict[str, Any]] = []
        waiter2_err: list[BaseException] = []

        t_old_b = threading.Thread(
            target=lambda: (
                bridge_b.list_windows(task_session_id="t_old_b", timeout=2.0)
            ),
            daemon=True,
        )

        def _run_old_b() -> None:
            try:
                bridge_b.list_windows(task_session_id="t_old_b", timeout=2.0)
            except BaseException as exc:
                old_err_b.append(exc)

        def _run_waiter1() -> None:
            try:
                waiter1_res.append(
                    bridge_b.list_windows(task_session_id="t_w1", timeout=3.0)
                )
            except BaseException as exc:
                waiter1_err.append(exc)

        def _run_waiter2() -> None:
            try:
                waiter2_res.append(
                    bridge_b.list_windows(task_session_id="t_w2", timeout=3.0)
                )
            except BaseException as exc:
                waiter2_err.append(exc)

        t_old_b = threading.Thread(target=_run_old_b, daemon=True)
        t_old_b.start()
        assert entered_old_b.wait(timeout=2.0)

        # Start waiter1 while old_b owns the initial connect attempt
        t_w1 = threading.Thread(target=_run_waiter1, daemon=True)
        t_w1.start()
        time.sleep(0.03)

        # Reconfigure wakes waiter1 -> waiter1 claims new attempt token and enters _gated_new_factory_b
        reconfigure_node_native_bridge(
            owner_b, transport_factory=_gated_new_factory_b
        )
        assert entered_new_b.wait(timeout=2.0)
        new_attempt_token = bridge_b._client._active_connect_attempt
        assert new_attempt_token is not None
        assert bridge_b._client._connecting is True

        # Release old_b WHILE waiter1 is still blocked inside _gated_new_factory_b!
        release_old_b.set()
        t_old_b.join(timeout=3.0)
        assert not t_old_b.is_alive()
        assert len(old_err_b) == 1
        assert isinstance(old_err_b[0], NativeBridgeRejected)
        assert old_err_b[0].code == "agent_computer_closed"

        # Old leader's exit MUST NOT have cleared waiter1's active connect claim!
        assert bridge_b._client._active_connect_attempt is new_attempt_token
        assert bridge_b._client._connecting is True

        # A second waiter arriving now must wait on waiter1 rather than spawning a duplicate new worker
        t_w2 = threading.Thread(target=_run_waiter2, daemon=True)
        t_w2.start()
        time.sleep(0.03)
        assert new_factory_calls_b == 1

        # Release waiter1's factory; both waiter1 and waiter2 must succeed on the single new worker
        release_new_b.set()
        t_w1.join(timeout=3.0)
        t_w2.join(timeout=3.0)
        assert not waiter1_err
        assert not waiter2_err
        assert len(waiter1_res) == 1
        assert len(waiter2_res) == 1
        assert new_factory_calls_b == 1
        assert bridge_b.cache_status()["status"] == "ready"
        assert bridge_b.cache_status()["workerGeneration"] == 2
    finally:
        release_old_b.set()
        release_new_b.set()
        bridge_b.close()
        for p in old_procs_b + new_procs_b:
            process_guard.terminate(p)


# ============================================================================
# 11. Standalone NativeBridge Pristine-Only Adoption & Owner/Cache Invariants (P2)
# ============================================================================


@pytest.mark.skipif(os.name != "nt", reason="Windows standalone bridge adoption and race tests")
def test_standalone_bridge_adoption_rules_and_owner_cache_consistency(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    class DummyOwner:
        pass

    script_path = tmp_path / "fake_worker.py"
    script_path.write_text(FAKE_WORKER_SCRIPT, encoding="utf-8")
    base_py = str(Path(getattr(sys, "_base_executable", sys.executable)).resolve())
    cmd_normal = [
        base_py,
        str(script_path),
        "normal",
        str(tmp_path / "p2_normal.jsonl"),
    ]
    cmd_eof = [
        base_py,
        str(script_path),
        "commit_eof_no_reply",
        str(tmp_path / "p2_eof.jsonl"),
    ]

    owner_a = DummyOwner()
    owner_b = DummyOwner()
    owner_c = DummyOwner()
    expected_owner_a_id = resolve_owner_id(owner_a)
    expected_owner_c_id = resolve_owner_id(owner_c)

    # 1. Pristine unowned NativeBridge can be adopted ONCE with 0 I/O; owner_id and cache_status()["ownerId"] match
    pristine = NativeBridge(dev_executable=cmd_normal)
    used_connected: NativeBridge | None = None
    used_unknown: NativeBridge | None = None
    pristine_2: NativeBridge | None = None
    try:
        assert pristine.owner_id.startswith("own_standalone_")
        assert pristine.cache_status()["ownerId"] == pristine.owner_id

        def _forbidden_io(*_args: Any, **_kwargs: Any) -> Any:
            raise AssertionError("Forbidden I/O or process spawn during bridge adoption")

        with monkeypatch.context() as m:
            m.setattr(Path, "stat", _forbidden_io)
            m.setattr(Path, "open", _forbidden_io)
            m.setattr(os, "stat", _forbidden_io)
            m.setattr("builtins.open", _forbidden_io)
            m.setattr(subprocess, "Popen", _forbidden_io)

            bound_a = bind_node_native_bridge(owner_a, bridge=pristine)
            assert bound_a is pristine
            assert pristine.owner_id == expected_owner_a_id
            assert pristine.cache_status()["ownerId"] == expected_owner_a_id
            # Idempotent re-bind before handshake
            assert bind_node_native_bridge(owner_a, bridge=pristine) is pristine
            assert reconfigure_node_native_bridge(owner_a, bridge=pristine) is pristine

        # Connect and handshake; cached binding must carry owner_a's ownerId
        pristine.list_windows(task_session_id="t_adopted")
        snap_a = pristine.cache_status()
        assert snap_a["status"] == "ready"
        assert snap_a["live"] is True
        assert snap_a["ownerId"] == expected_owner_a_id
        assert snap_a["binding"]["ownerId"] == expected_owner_a_id
        assert pristine.owner_id == expected_owner_a_id
        # Idempotent re-bind after handshake still succeeds
        assert bind_node_native_bridge(owner_a, bridge=pristine) is pristine
        assert reconfigure_node_native_bridge(owner_a, bridge=pristine) is pristine

        # 2. Existing-bridge conflict check on owner_a must NOT mutate an incoming pristine bridge
        pristine_2 = NativeBridge(dev_executable=cmd_normal)
        orig_p2_id = pristine_2.owner_id
        with pytest.raises(NativeBridgeRejected) as exc_cfg:
            bind_node_native_bridge(owner_a, bridge=pristine_2)
        assert exc_cfg.value.code == "agent_computer_config_conflict"
        assert exc_cfg.value.pre_dispatch is True
        assert pristine_2.owner_id == orig_p2_id
        assert pristine_2._bound_owner_token is None
        assert pristine_2.cache_status()["ownerId"] == orig_p2_id

        # Because pristine_2 was NOT mutated by the rejected bind on owner_a, it can still be bound to owner_c!
        bound_c = bind_node_native_bridge(owner_c, bridge=pristine_2)
        assert bound_c is pristine_2
        assert pristine_2.owner_id == expected_owner_c_id
        assert pristine_2.cache_status()["ownerId"] == expected_owner_c_id

        # 3. Already-used connected standalone NativeBridge must be rejected with agent_computer_owner_conflict
        #    by both bind_node_native_bridge and reconfigure_node_native_bridge with 0 state mutation and 0 I/O
        used_connected = NativeBridge(dev_executable=cmd_normal)
        standalone_conn_id = used_connected.owner_id
        used_connected.list_windows(task_session_id="t_standalone_conn")
        snap_conn_before = used_connected.cache_status()
        assert snap_conn_before["status"] == "ready"
        assert snap_conn_before["live"] is True
        assert snap_conn_before["ownerId"] == standalone_conn_id

        with monkeypatch.context() as m:
            m.setattr(Path, "stat", _forbidden_io)
            m.setattr(subprocess, "Popen", _forbidden_io)

            for fn, target_owner in (
                (bind_node_native_bridge, owner_b),
                (reconfigure_node_native_bridge, owner_b),
                (bind_node_native_bridge, owner_a),
                (reconfigure_node_native_bridge, owner_a),
            ):
                with pytest.raises(NativeBridgeRejected) as exc_used:
                    fn(target_owner, bridge=used_connected)
                assert exc_used.value.code == "agent_computer_owner_conflict"
                assert exc_used.value.pre_dispatch is True

        assert used_connected.owner_id == standalone_conn_id
        assert used_connected._bound_owner_token is None
        snap_conn_after = used_connected.cache_status()
        assert snap_conn_after["status"] == "ready"
        assert snap_conn_after["live"] is True
        assert snap_conn_after["ownerId"] == standalone_conn_id
        assert snap_conn_after["binding"]["ownerId"] == standalone_conn_id
        assert getattr(owner_b, "_ryn_native_bridge", None) is None
        assert bind_node_native_bridge(owner_a) is pristine

        # 4. Already-used standalone NativeBridge with an unknown post-commit operation record and retired/closed worker
        used_unknown = NativeBridge(dev_executable=cmd_eof)
        standalone_unk_id = used_unknown.owner_id
        wlist = used_unknown.list_windows(task_session_id="t_unk")
        wref = wlist["windows"][0]["window"]
        obs = used_unknown.get_state(task_session_id="t_unk", window=wref)
        with pytest.raises(NativeBridgeError) as exc_eof:
            used_unknown.execute_action(
                task_session_id="t_unk",
                operation_id="op_standalone_unknown",
                window=wref,
                expected_state_token=obs["stateToken"],
                action={"type": "key_press", "key": "Enter"},
                on_ready=lambda _info: None,
            )
        assert exc_eof.value.commit_attempted is True
        assert exc_eof.value.outcome == "unknown"
        used_unknown.close()

        for fn in (bind_node_native_bridge, reconfigure_node_native_bridge):
            with pytest.raises(NativeBridgeRejected) as exc_unk_adopt:
                fn(owner_b, bridge=used_unknown)
            assert exc_unk_adopt.value.code == "agent_computer_owner_conflict"
            assert exc_unk_adopt.value.pre_dispatch is True

        assert used_unknown.owner_id == standalone_unk_id
        assert used_unknown.cache_status()["ownerId"] == standalone_unk_id
        # Unknown operation record on used_unknown remains intact and blocks replay
        with pytest.raises(NativeBridgeError) as exc_replay_unk:
            used_unknown.execute_action(
                task_session_id="t_unk",
                operation_id="op_standalone_unknown",
                window=wref,
                expected_state_token=obs["stateToken"],
                action={"type": "key_press", "key": "Enter"},
                on_ready=lambda _info: None,
            )
        assert exc_replay_unk.value.code == "agent_computer_commit_already_attempted"
        assert exc_replay_unk.value.commit_attempted is True
        assert exc_replay_unk.value.outcome == "unknown"

        # 5. Replacing an existing bound bridge with a PRISTINE unowned bridge via reconfigure_node_native_bridge
        #    closes the old bridge outside _OWNER_BRIDGES_LOCK, adopts the pristine bridge for owner_a,
        #    and transfers post-dispatch no-replay operation records!
        wlist_a = pristine.list_windows(task_session_id="t_adopt_op")
        wref_a = wlist_a["windows"][0]["window"]
        obs_a = pristine.get_state(task_session_id="t_adopt_op", window=wref_a)
        commit_a = pristine.execute_action(
            task_session_id="t_adopt_op",
            operation_id="op_transfer_to_replacement",
            window=wref_a,
            expected_state_token=obs_a["stateToken"],
            action={"type": "key_press", "key": "Enter"},
            on_ready=lambda _info: None,
        )
        assert commit_a["status"] == "committed"

        pristine_replacement = NativeBridge(dev_executable=cmd_normal)
        try:
            replaced = reconfigure_node_native_bridge(
                owner_a, bridge=pristine_replacement
            )
            assert replaced is pristine_replacement
            assert bind_node_native_bridge(owner_a) is pristine_replacement
            assert pristine.cache_status()["closed"] is True
            assert pristine_replacement.owner_id == expected_owner_a_id
            assert pristine_replacement.cache_status()["ownerId"] == expected_owner_a_id

            pristine_replacement.list_windows(task_session_id="t_adopt_op")
            assert pristine_replacement.cache_status()["ownerId"] == expected_owner_a_id
            assert (
                pristine_replacement.cache_status()["binding"]["ownerId"]
                == expected_owner_a_id
            )

            # Transferred post-dispatch operation record must block replay on the replacement bridge!
            with pytest.raises(NativeBridgeError) as exc_transferred:
                pristine_replacement.execute_action(
                    task_session_id="t_adopt_op",
                    operation_id="op_transfer_to_replacement",
                    window=wref_a,
                    expected_state_token=obs_a["stateToken"],
                    action={"type": "key_press", "key": "Enter"},
                    on_ready=lambda _info: None,
                )
            assert exc_transferred.value.code == "agent_computer_commit_already_attempted"
            assert exc_transferred.value.commit_attempted is True
            assert exc_transferred.value.pre_dispatch is False
        finally:
            pristine_replacement.close()
    finally:
        pristine.close()
        if pristine_2 is not None:
            pristine_2.close()
        if used_connected is not None:
            used_connected.close()
        if used_unknown is not None:
            used_unknown.close()


@pytest.mark.skipif(os.name != "nt", reason="Windows concurrent bind vs list_windows barrier race test")
def test_concurrent_bind_and_standalone_start_has_single_consistent_owner_outcome(
    tmp_path: Path,
) -> None:
    class DummyOwner:
        pass

    script_path = tmp_path / "fake_worker.py"
    script_path.write_text(FAKE_WORKER_SCRIPT, encoding="utf-8")
    base_py = str(Path(getattr(sys, "_base_executable", sys.executable)).resolve())

    # Subcase A: standalone.list_windows enters transport_factory first -> bind_node_native_bridge MUST reject
    # with agent_computer_owner_conflict, and both owner_id and cache_status()["ownerId"] stay standalone!
    entered_factory = threading.Event()
    release_factory = threading.Event()
    spawned: list[subprocess.Popen[bytes]] = []

    def _gated_factory() -> Any:
        entered_factory.set()
        release_factory.wait(timeout=5.0)
        proc = subprocess.Popen(
            [base_py, str(script_path), "normal", str(tmp_path / "race_a.jsonl")],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            creationflags=0x4 | subprocess.CREATE_NO_WINDOW,
        )
        proc._ryn_job = process_guard._windows_job(proc)  # type: ignore[attr-defined]
        spawned.append(proc)
        return proc

    standalone_a = NativeBridge(transport_factory=_gated_factory)
    orig_standalone_id = standalone_a.owner_id
    owner_race_a = DummyOwner()
    try:
        list_err: list[BaseException] = []

        def _run_list() -> None:
            try:
                standalone_a.list_windows(task_session_id="t_race_a", timeout=3.0)
            except BaseException as exc:
                list_err.append(exc)

        t_list = threading.Thread(target=_run_list, daemon=True)
        t_list.start()
        assert entered_factory.wait(timeout=2.0)

        # While standalone_a is mid-connect (before CachedBinding is even published), attempt adoption:
        with pytest.raises(NativeBridgeRejected) as exc_race:
            bind_node_native_bridge(owner_race_a, bridge=standalone_a)
        assert exc_race.value.code == "agent_computer_owner_conflict"
        assert exc_race.value.pre_dispatch is True

        release_factory.set()
        t_list.join(timeout=3.0)
        assert not list_err
        snap_a = standalone_a.cache_status()
        assert snap_a["status"] == "ready"
        assert standalone_a.owner_id == orig_standalone_id
        assert snap_a["ownerId"] == orig_standalone_id
        assert snap_a["binding"]["ownerId"] == orig_standalone_id
        assert getattr(owner_race_a, "_ryn_native_bridge", None) is None
    finally:
        release_factory.set()
        standalone_a.close()
        for p in spawned:
            process_guard.terminate(p)

    # Subcase B: Simultaneous barrier race between bind_node_native_bridge and standalone.list_windows
    cmd_b = [base_py, str(script_path), "normal", str(tmp_path / "race_b.jsonl")]
    standalone_b = NativeBridge(dev_executable=cmd_b)
    orig_b_id = standalone_b.owner_id
    owner_race_b = DummyOwner()
    expected_b_owner_id = resolve_owner_id(owner_race_b)
    barrier = threading.Barrier(2)
    bind_outcome: list[Any] = []
    try:
        def _race_bind() -> None:
            barrier.wait(timeout=2.0)
            try:
                res = bind_node_native_bridge(owner_race_b, bridge=standalone_b)
                bind_outcome.append(("adopted", res))
            except BaseException as exc:
                bind_outcome.append(("rejected", exc))

        def _race_start() -> None:
            barrier.wait(timeout=2.0)
            standalone_b.list_windows(task_session_id="t_race_b", timeout=3.0)

        tb1 = threading.Thread(target=_race_bind, daemon=True)
        tb2 = threading.Thread(target=_race_start, daemon=True)
        tb1.start()
        tb2.start()
        tb1.join(timeout=4.0)
        tb2.join(timeout=4.0)

        assert len(bind_outcome) == 1
        tag, payload = bind_outcome[0]
        snap_b = standalone_b.cache_status()
        assert snap_b["status"] == "ready"
        assert snap_b["live"] is True
        # Regardless of which thread acquired the lock first, bridge.owner_id and cache_status()["ownerId"]
        # MUST be 100% consistent!
        assert standalone_b.owner_id == snap_b["ownerId"]
        assert snap_b["binding"]["ownerId"] == standalone_b.owner_id
        if tag == "adopted":
            assert standalone_b.owner_id == expected_b_owner_id
            assert getattr(owner_race_b, "_ryn_native_bridge", None) is standalone_b
        else:
            assert isinstance(payload, NativeBridgeRejected)
            assert payload.code == "agent_computer_owner_conflict"
            assert standalone_b.owner_id == orig_b_id
            assert getattr(owner_race_b, "_ryn_native_bridge", None) is None
    finally:
        standalone_b.close()


# ============================================================================
# 12. R1 Release Authority & Anti-Forging Guarantees
# ============================================================================


@pytest.mark.skipif(os.name != "nt", reason="Windows R1 release authority verification")
def test_r1_raw_mode_strings_and_self_authored_manifest_never_mint_release_authority(
    tmp_path: Path,
) -> None:
    """Verify that raw `mode='production'` / `source_mode='production'`, `load_manifest`, `load_authoritative_manifest`, and self-authored manifests + self-hashed PE copies NEVER mint `release_verified=True`."""
    verify_windows_x64_host()
    base_py = Path(getattr(sys, "_base_executable", sys.executable)).resolve()
    bundle_dir = tmp_path / "r1_self_bundle"
    bundle_dir.mkdir()
    exe_copy = bundle_dir / PACKAGED_EXECUTABLE_BASENAME
    shutil.copyfile(base_py, exe_copy)
    raw_exe = exe_copy.read_bytes()
    exe_sha = hashlib.sha256(raw_exe).hexdigest()

    manifest_dict = _valid_manifest_dict(size=len(raw_exe), sha256_hex=exe_sha)
    manifest_bytes = json.dumps(manifest_dict, indent=2).encode("utf-8")
    manifest_path = bundle_dir / MANIFEST_BASENAME
    manifest_path.write_bytes(manifest_bytes)

    # 1. Public parse_and_validate_manifest_bytes with source_mode="production" has zero release authority
    spec_from_bytes = parse_and_validate_manifest_bytes(
        manifest_bytes,
        source_mode="production",
    )
    assert spec_from_bytes.source_mode == "production"
    assert spec_from_bytes.release_authorized is False
    assert getattr(spec_from_bytes, "_release_commitment", None) is None

    # 2. Public load_manifest and load_authoritative_manifest with source_mode="production" have zero release authority
    spec_from_load = load_manifest(manifest_path, source_mode="production")
    spec_from_alias = load_authoritative_manifest(
        manifest_path, source_mode="production"
    )
    assert spec_from_load.release_authorized is False
    assert spec_from_alias.release_authorized is False
    assert getattr(spec_from_load, "_release_commitment", None) is None
    assert getattr(spec_from_alias, "_release_commitment", None) is None

    # 3. open_and_verify_artifact_lease with mode="production" on an uncommitted spec never mints release authority
    lease_raw = open_and_verify_artifact_lease(
        spec=spec_from_bytes,
        bundle_root=bundle_dir,
        candidate_executable=exe_copy,
        mode="production",
    )
    try:
        assert lease_raw.verified_artifact.verified is True
        assert lease_raw.verified_artifact.release_verified is False
        assert lease_raw.verified_artifact.mode == "development"
        assert getattr(lease_raw, "_release_proof", None) is None
        assert lease_raw.has_valid_release_proof() is False
        assert verify_lease_release_authority(lease_raw) is False
        assert lease_raw.has_pinned_verified_artifact() is True
    finally:
        lease_raw.close()

    # 4. Non-frozen ProductionBinaryResolver with explicit self-authored manifest_path + bundle_root
    # validates file/PE/hash (`verified=True`) in development mode (`release_verified=False`).
    prod_resolver = ProductionBinaryResolver(
        manifest_path=manifest_path,
        bundle_root=bundle_dir,
    )
    lease_prod_override = prod_resolver.acquire_lease()
    try:
        assert lease_prod_override.verified_artifact.verified is True
        assert lease_prod_override.verified_artifact.release_verified is False
        assert lease_prod_override.verified_artifact.mode == "development"
        assert getattr(lease_prod_override, "_release_proof", None) is None
        assert verify_lease_release_authority(lease_prod_override) is False
    finally:
        lease_prod_override.close()


@pytest.mark.skipif(os.name != "nt", reason="Windows R1 CachedBinding lease proof verification")
def test_r1_public_dto_verified_artifact_cannot_forge_cached_binding_release_authority(
    tmp_path: Path,
) -> None:
    """Verify:
    1. A custom `artifact_resolver` returning a fabricated size/FileId `VerifiedArtifact(mode='production', verified=True, release_verified=True)` with no handles/proof is rejected pre-resume by `verify_suspended_process` (`agent_computer_process_identity_mismatch`), never runs the worker, and leaves cache `unknown`.
    2. A custom `artifact_resolver` copying real OS path/size/volume/FileId/PE identity from a closed `open_dev_executable_lease` into a forged `VerifiedArtifact(mode='production', verified=True, release_verified=True)` on a new `ArtifactLease(file_handle=None, dir_handle=None, _release_proof=None)` reaches handshake, and `client` downgrades the `ready` `CachedBinding` to `mode='development'`, `verified=False`, `releaseVerified=False`, `artifact=None`, `manifestDigest=None`, `releaseId=None`.
    3. Attaching a forged `_release_proof = object()` to `ArtifactLease` is rejected before spawn with `agent_computer_binary_tampered`, leaves cache `unknown`, and never spawns the worker.
    """
    script_path = tmp_path / "fake_worker.py"
    script_path.write_text(FAKE_WORKER_SCRIPT, encoding="utf-8")
    base_py = str(Path(getattr(sys, "_base_executable", sys.executable)).resolve())

    # 1. Fabricated size/FileId on public DTO with no handles/proof -> rejected pre-resume before worker runs
    state_mismatch = tmp_path / "dto_mismatch.jsonl"
    cmd_mismatch = [base_py, str(script_path), "normal", str(state_mismatch)]

    class FabricatedIdentityDtoResolver:
        mode = "production"
        config_tag = "fabricated_identity_dto_resolver"

        def acquire_lease(self, *, phase: str = "pre_dispatch") -> ArtifactLease:
            _ = phase
            forged_art = VerifiedArtifact(
                mode="production",
                verified=True,
                release_verified=True,
                executable_path=base_py,
                bundle_root=str(Path(base_py).parent),
                size=1024,
                sha256="a" * 64,
                volume_serial=1,
                file_id_high=0,
                file_id_low=1,
                pe_machine=IMAGE_FILE_MACHINE_AMD64,
                pe_magic=IMAGE_NT_OPTIONAL_HDR64_MAGIC,
                manifest_sha256="b" * 64,
                release_id="ryn-forged-release",
                worker_name=EXPECTED_WORKER_NAME,
                worker_version=EXPECTED_WORKER_VERSION,
                protocol_version=PROTOCOL_SEMVER,
                target_triple=SUPPORTED_TARGET_TRIPLE,
            )
            return ArtifactLease(
                verified_artifact=forged_art,
                command=list(cmd_mismatch),
                file_handle=None,
                dir_handle=None,
                _release_proof=None,
            )

    bridge_mismatch = NativeBridge(artifact_resolver=FabricatedIdentityDtoResolver())
    try:
        with pytest.raises(NativeBridgeRejected) as exc_mismatch:
            bridge_mismatch.list_windows(task_session_id="t_forged_mismatch")
        assert exc_mismatch.value.code == "agent_computer_process_identity_mismatch"
        assert exc_mismatch.value.pre_dispatch is True
        assert not state_mismatch.exists()
        snap_mismatch = bridge_mismatch.cache_status()
        assert snap_mismatch["status"] == "unknown"
        assert snap_mismatch["cached"] is False
        assert snap_mismatch["live"] is False
        assert snap_mismatch["verified"] is False
        assert snap_mismatch["releaseVerified"] is False
        assert snap_mismatch["binding"] is None
    finally:
        bridge_mismatch.close()

    # 2. Real OS executable identity copied from a closed dev lease into forged public DTO flags
    # (`mode='production'`, `verified=True`, `release_verified=True`) with no handles/proof ->
    # reaches handshake, but CachedBinding downgrades to development with verified=False, releaseVerified=False.
    state_downgrade = tmp_path / "dto_downgrade.jsonl"
    cmd_downgrade = [base_py, str(script_path), "normal", str(state_downgrade)]

    real_dev_lease = open_dev_executable_lease(cmd_downgrade)
    real_ident_art = real_dev_lease.verified_artifact
    real_dev_lease.close()

    class ForgedFlagsRealIdentityDtoResolver:
        mode = "production"
        config_tag = "forged_flags_real_identity_dto_resolver"

        def acquire_lease(self, *, phase: str = "pre_dispatch") -> ArtifactLease:
            _ = phase
            forged_art = VerifiedArtifact(
                mode="production",
                verified=True,
                release_verified=True,
                executable_path=real_ident_art.executable_path,
                bundle_root=real_ident_art.bundle_root,
                size=real_ident_art.size,
                sha256="a" * 64,
                volume_serial=real_ident_art.volume_serial,
                file_id_high=real_ident_art.file_id_high,
                file_id_low=real_ident_art.file_id_low,
                pe_machine=real_ident_art.pe_machine,
                pe_magic=real_ident_art.pe_magic,
                manifest_sha256="b" * 64,
                release_id="ryn-forged-release",
                worker_name=EXPECTED_WORKER_NAME,
                worker_version=EXPECTED_WORKER_VERSION,
                protocol_version=PROTOCOL_SEMVER,
                target_triple=SUPPORTED_TARGET_TRIPLE,
            )
            return ArtifactLease(
                verified_artifact=forged_art,
                command=list(cmd_downgrade),
                file_handle=None,
                dir_handle=None,
                _release_proof=None,
            )

    bridge = NativeBridge(artifact_resolver=ForgedFlagsRealIdentityDtoResolver())
    try:
        res = bridge.list_windows(task_session_id="t_forged_dto")
        assert len(res["windows"]) >= 1
        snap = bridge.cache_status()
        assert snap["status"] == "ready"
        assert snap["live"] is True
        assert snap["releaseVerified"] is False
        assert snap["verified"] is False
        assert snap["mode"] == "development"
        assert snap["binding"]["releaseVerified"] is False
        assert snap["binding"]["verified"] is False
        assert snap["binding"]["mode"] == "development"
        assert snap["binding"]["manifestDigest"] is None
        assert snap["binding"]["releaseId"] is None
        assert snap["binding"]["artifact"] is None
    finally:
        bridge.close()

    # 3. Forging a fake `_release_proof` attribute on `ArtifactLease` must be rejected pre-dispatch before spawn
    state_bad_proof = tmp_path / "dto_bad_proof.jsonl"
    cmd_bad_proof = [base_py, str(script_path), "normal", str(state_bad_proof)]

    class ForgedProofObjectResolver:
        mode = "production"
        config_tag = "forged_proof_obj"

        def acquire_lease(self, *, phase: str = "pre_dispatch") -> ArtifactLease:
            lease = ForgedFlagsRealIdentityDtoResolver().acquire_lease(phase=phase)
            lease.command = list(cmd_bad_proof)
            lease._release_proof = object()  # type: ignore[assignment]
            return lease

    bridge_bad_proof = NativeBridge(artifact_resolver=ForgedProofObjectResolver())
    try:
        with pytest.raises(NativeBridgeRejected) as exc_forged_proof:
            bridge_bad_proof.list_windows(task_session_id="t_forged_proof")
        assert exc_forged_proof.value.code == "agent_computer_binary_tampered"
        assert exc_forged_proof.value.pre_dispatch is True
        assert not state_bad_proof.exists()
        snap_bad = bridge_bad_proof.cache_status()
        assert snap_bad["status"] == "unknown"
        assert snap_bad["cached"] is False
        assert snap_bad["live"] is False
        assert snap_bad["verified"] is False
        assert snap_bad["releaseVerified"] is False
        assert snap_bad["binding"] is None
    finally:
        bridge_bad_proof.close()


@pytest.mark.skipif(os.name != "nt", reason="Windows R1 commitment and dataclasses.replace defense")
def test_r1_dataclasses_replace_and_override_paths_reject_committed_spec_before_spawn(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Unit simulation of `_load_packaged_release_spec` verifying:
    1. `dataclasses.replace` altering ANY field on a committed `ArtifactSpec` invalidates `release_authorized` and causes `open_and_verify_artifact_lease` to reject before spawn (`agent_computer_manifest_invalid`).
    2. Using an unaltered committed `ArtifactSpec` with a different `bundle_root`, `candidate_executable`, or when `sys.frozen` is `False` is rejected before spawn.
    3. Under `sys.frozen=True`, `ProductionBinaryResolver` rejects any caller override of `manifest_path`, `bundle_root`, `bundled_executable`, `require_frozen=False`, or worker/protocol version before spawn.

    NOTE: This is a Python R1 unit simulation only. Genuine frozen installer acceptance requires the R2 package self-check with a real compiled Rust `ryn_computer_use.exe` performing `initialize` and `shutdown`.
    """
    verify_windows_x64_host()
    base_py = Path(getattr(sys, "_base_executable", sys.executable)).resolve()

    frozen_app_dir = tmp_path / "frozen_app"
    frozen_app_dir.mkdir()
    frozen_host_exe = frozen_app_dir / "ryn_desktop.exe"
    packaged_worker_exe = frozen_app_dir / PACKAGED_EXECUTABLE_BASENAME
    shutil.copyfile(base_py, frozen_host_exe)
    shutil.copyfile(base_py, packaged_worker_exe)

    raw_worker = packaged_worker_exe.read_bytes()
    worker_sha = hashlib.sha256(raw_worker).hexdigest()

    pkg_bin_dir = tmp_path / "pkg_bin"
    pkg_bin_dir.mkdir()
    pkg_manifest_path = pkg_bin_dir / MANIFEST_BASENAME
    pkg_manifest_path.write_text(
        json.dumps(_valid_manifest_dict(size=len(raw_worker), sha256_hex=worker_sha)),
        encoding="utf-8",
    )

    other_dir = tmp_path / "other_dir"
    other_dir.mkdir()
    other_worker_exe = other_dir / PACKAGED_EXECUTABLE_BASENAME
    shutil.copyfile(base_py, other_worker_exe)
    other_manifest_path = other_dir / MANIFEST_BASENAME
    other_manifest_path.write_text(
        json.dumps(_valid_manifest_dict(size=len(raw_worker), sha256_hex=worker_sha)),
        encoding="utf-8",
    )

    # Without sys.frozen=True, _load_packaged_release_spec must fail closed
    with pytest.raises(NativeBridgeRejected) as exc_not_frozen:
        binary_mod._load_packaged_release_spec()
    assert exc_not_frozen.value.code == "agent_computer_executable_not_found"

    with monkeypatch.context() as m:
        m.setattr(sys, "frozen", True, raising=False)
        m.setattr(sys, "executable", str(frozen_host_exe))
        m.setattr(binary_mod, "PACKAGE_MANIFEST_PATH", pkg_manifest_path)

        committed_spec = binary_mod._load_packaged_release_spec()
        assert committed_spec.release_authorized is True

        # 1. dataclasses.replace mutations on a committed spec MUST NOT preserve release authority
        # and MUST be rejected before spawn by open_and_verify_artifact_lease!
        mutations: list[dict[str, Any]] = [
            {"expected_sha256": "0" * 64},
            {"expected_size": committed_spec.expected_size + 4},
            {"installed_filename": "other_worker.exe"},
            {"release_id": "ryn-tampered-release"},
            {"worker_version": "9.9.9"},
            {"protocol_version": "9.9.9"},
            {"manifest_sha256": "f" * 64},
        ]
        for changes in mutations:
            altered_spec = dataclasses.replace(committed_spec, **changes)
            # dataclasses.replace copies the private _release_commitment field,
            # but full field comparison must detect the mismatch!
            assert altered_spec._release_commitment is committed_spec._release_commitment
            assert altered_spec.release_authorized is False
            with pytest.raises(NativeBridgeRejected) as exc_altered:
                open_and_verify_artifact_lease(
                    spec=altered_spec,
                    bundle_root=frozen_app_dir,
                    candidate_executable=packaged_worker_exe,
                    mode="production",
                )
            assert exc_altered.value.code == "agent_computer_manifest_invalid"

        # 2. Passing unaltered committed_spec with a different bundle_root or candidate_executable must fail closed
        with pytest.raises(NativeBridgeRejected) as exc_wrong_root:
            open_and_verify_artifact_lease(
                spec=committed_spec,
                bundle_root=other_dir,
                candidate_executable=other_worker_exe,
                mode="production",
            )
        assert exc_wrong_root.value.code == "agent_computer_path_invalid"

        # 3. Under sys.frozen=True, ProductionBinaryResolver must reject any caller override before spawn
        for bad_kwargs, expected_code in [
            ({"manifest_path": other_manifest_path}, "agent_computer_path_invalid"),
            ({"bundle_root": other_dir}, "agent_computer_path_invalid"),
            ({"bundled_executable": other_worker_exe}, "agent_computer_path_invalid"),
            ({"require_frozen": False}, "agent_computer_path_invalid"),
            ({"expected_worker_name": "other_worker"}, "agent_computer_worker_identity_mismatch"),
            ({"expected_worker_version": "9.9.9"}, "agent_computer_worker_identity_mismatch"),
            ({"expected_protocol_version": "9.9.9"}, "agent_computer_version_mismatch"),
        ]:
            bad_resolver = ProductionBinaryResolver(**bad_kwargs)
            with pytest.raises(NativeBridgeRejected) as exc_override:
                bad_resolver.acquire_lease()
            assert exc_override.value.code == expected_code

        # 4. Unit-simulated default ProductionBinaryResolver under frozen context mints valid lease proof
        sim_resolver = ProductionBinaryResolver()
        sim_lease = sim_resolver.acquire_lease()
        try:
            assert sim_lease.verified_artifact.verified is True
            assert sim_lease.verified_artifact.release_verified is True
            assert sim_lease.verified_artifact.mode == "production"
            assert sim_lease.has_valid_release_proof() is True
            assert verify_lease_release_authority(sim_lease) is True
        finally:
            sim_lease.close()
            # Closing the lease invalidates the release proof
            assert verify_lease_release_authority(sim_lease) is False

    # 5. Once sys.frozen is False again, the previously loaded committed_spec is no longer release_authorized
    # and cannot be used to open a lease
    assert committed_spec.release_authorized is False
    with pytest.raises(NativeBridgeRejected) as exc_unfrozen_use:
        open_and_verify_artifact_lease(
            spec=committed_spec,
            bundle_root=frozen_app_dir,
            candidate_executable=packaged_worker_exe,
            mode="production",
        )
    assert exc_unfrozen_use.value.code == "agent_computer_executable_not_found"


