"""Pure unit and static contract tests for R2 Windows native computer-use packaging (`build-computer-use-manifest.py`, `build-windows.ps1`, `tauri.windows.conf.json`, and `rynmesh_peer_entry.py`).

These tests validate deterministic manifest generation, PE64/size/hash/version/schema rejection,
isolated buildRoot/venv staging, real PyInstaller CArchive single-manifest extraction, external build receipt
and `-SkipSidecar` staleness detection, Tauri Windows externalBin preservation, and the sidecar
`--ryn-computer-use-package-check` entry branch WITHOUT executing synthetic PEs or running installer builds.
"""

from __future__ import annotations

import ast
import copy
import dataclasses
import hashlib
import importlib.util
import io
import json
import re
import shutil
import struct
import sys
import zlib
from pathlib import Path
from typing import Any, Sequence

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = REPO_ROOT / "webapp" / "src-tauri" / "scripts"
MANIFEST_SCRIPT_PATH = SCRIPTS_DIR / "build-computer-use-manifest.py"
BUILD_PS1_PATH = SCRIPTS_DIR / "build-windows.ps1"
TAURI_BASE_CONF_PATH = REPO_ROOT / "webapp" / "src-tauri" / "tauri.conf.json"
TAURI_WIN_CONF_PATH = REPO_ROOT / "webapp" / "src-tauri" / "tauri.windows.conf.json"
SIDECAR_ENTRY_PATH = (
    REPO_ROOT / "webapp" / "src-tauri" / "sidecar" / "rynmesh_peer_entry.py"
)


def _load_packaging_module() -> Any:
    spec = importlib.util.spec_from_file_location(
        "build_computer_use_manifest",
        MANIFEST_SCRIPT_PATH,
    )
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


def _load_sidecar_entry_module() -> Any:
    spec = importlib.util.spec_from_file_location(
        "rynmesh_peer_entry_test_mod",
        SIDECAR_ENTRY_PATH,
    )
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


pkg_mod = _load_packaging_module()
PackagingContractError = pkg_mod.PackagingContractError


def _build_synthetic_pe64_bytes(
    *,
    mz_magic: bytes = b"MZ",
    e_lfanew: int = 0x80,
    pe_sig: bytes = b"PE\x00\x00",
    machine: int = 0x8664,
    num_sections: int = 1,
    size_of_opt_header: int = 240,
    characteristics: int = 0x0002,
    opt_magic: int = 0x020B,
    size_of_headers: int = 0x200,
    total_size: int = 4096,
    payload_fill: bytes = b"\x90",
) -> bytes:
    buf = bytearray((payload_fill * total_size)[:total_size])
    for i in range(min(size_of_headers, len(buf))):
        buf[i] = 0
    buf[0:2] = mz_magic
    struct.pack_into("<I", buf, 0x3C, e_lfanew)
    if e_lfanew + 24 + 2 <= len(buf):
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
            size_of_opt_header,
            characteristics,
        )
        opt_off = coff_off + 20
        struct.pack_into("<H", buf, opt_off, opt_magic)
        if opt_off + 64 <= len(buf):
            struct.pack_into("<I", buf, opt_off + 60, size_of_headers)
    return bytes(buf)


def _build_synthetic_pe64_with_carchive(
    archive_entries: Sequence[tuple[str, bytes, bool, str]],
    *,
    total_size: int = 16384,
    payload_fill: bytes = b"\x22",
) -> bytes:
    """Build a synthetic PE64 executable with a spec-valid PyInstaller `MEI\\x0c\\x0b\\x0a\\x0b\\x0e` CArchive appended.

    Each tuple in `archive_entries` is `(entry_name, raw_bytes, compress_bool, typecode_char)`.
    """
    base_pe = _build_synthetic_pe64_bytes(
        total_size=total_size,
        payload_fill=payload_fill,
    )
    data_buf = bytearray()
    toc_buf = bytearray()

    for name, raw_bytes, compress, typecode in archive_entries:
        uncomp_len = len(raw_bytes)
        if compress:
            comp_bytes = zlib.compress(raw_bytes)
            comp_flag = 1
        else:
            comp_bytes = bytes(raw_bytes)
            comp_flag = 0
        comp_len = len(comp_bytes)
        data_offset = len(data_buf)
        data_buf.extend(comp_bytes)

        name_utf8 = name.encode("utf-8") + b"\x00"
        raw_entry_len = pkg_mod.PYINSTALLER_TOC_ENTRY_HEADER_SIZE + len(name_utf8)
        pad_len = (4 - (raw_entry_len % 4)) % 4
        entry_size = raw_entry_len + pad_len
        header = struct.pack(
            pkg_mod.PYINSTALLER_TOC_ENTRY_FORMAT,
            entry_size,
            data_offset,
            comp_len,
            uncomp_len,
            comp_flag,
            ord(typecode[0]),
        )
        toc_buf.extend(header)
        toc_buf.extend(name_utf8)
        toc_buf.extend(b"\x00" * pad_len)

    toc_offset = len(data_buf)
    toc_length = len(toc_buf)
    pkg_length = len(data_buf) + toc_length + pkg_mod.PYINSTALLER_COOKIE_SIZE
    cookie = struct.pack(
        pkg_mod.PYINSTALLER_COOKIE_FORMAT,
        pkg_mod.PYINSTALLER_COOKIE_MAGIC,
        pkg_length,
        toc_offset,
        toc_length,
        311,
        b"python311.dll".ljust(64, b"\x00"),
    )
    return base_pe + bytes(data_buf) + bytes(toc_buf) + cookie


# ============================================================================
# 1. Repository Version & Contract Synchronization
# ============================================================================


def test_extract_repo_contract_versions_matches_actual_rust_and_python_sources_and_rejects_drift(
    tmp_path: Path,
) -> None:
    versions = pkg_mod.extract_repo_contract_versions(REPO_ROOT)
    assert versions.worker_name == "ryn_computer_use"
    assert versions.worker_version == "0.1.0"
    assert versions.protocol_version == "1.0.0"
    assert versions.wire_version == 1
    assert versions.target_triple == "x86_64-pc-windows-msvc"
    assert versions.installed_filename == "ryn_computer_use.exe"
    assert versions.rynmesh_package_version == "0.7.0"

    # Build a minimal mirror of the contract files in tmp_path to test version/constant drift rejection
    fake_repo = tmp_path / "repo"
    for rel in (
        "pyproject.toml",
        "native/ryn_computer_use/Cargo.toml",
        "native/ryn_computer_use/src/protocol.rs",
        "native/ryn_computer_use/src/engine.rs",
        "rynmesh/agent_runtime/computer/protocol.py",
        "rynmesh/agent_runtime/computer/binary.py",
        "rynmesh/agent_runtime/computer/__init__.py",
    ):
        src = REPO_ROOT / rel
        dst = fake_repo / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(src, dst)

    assert pkg_mod.extract_repo_contract_versions(fake_repo) == versions

    # 1. Drift in Cargo.toml package.version vs Python EXPECTED_WORKER_VERSION
    cargo_toml_path = fake_repo / "native/ryn_computer_use/Cargo.toml"
    orig_cargo = cargo_toml_path.read_text(encoding="utf-8")
    cargo_toml_path.write_text(
        orig_cargo.replace('version = "0.1.0"', 'version = "0.2.0"', 1),
        encoding="utf-8",
    )
    with pytest.raises(PackagingContractError) as exc_ver:
        pkg_mod.extract_repo_contract_versions(fake_repo)
    assert exc_ver.value.code == "packaging_version_mismatch"
    cargo_toml_path.write_text(orig_cargo, encoding="utf-8")

    # 2. Drift in Rust PROTOCOL_SEMVER vs Python PROTOCOL_SEMVER
    rs_proto_path = fake_repo / "native/ryn_computer_use/src/protocol.rs"
    orig_rs_proto = rs_proto_path.read_text(encoding="utf-8")
    rs_proto_path.write_text(
        orig_rs_proto.replace(
            'pub const PROTOCOL_SEMVER: &str = "1.0.0";',
            'pub const PROTOCOL_SEMVER: &str = "2.0.0";',
            1,
        ),
        encoding="utf-8",
    )
    with pytest.raises(PackagingContractError) as exc_proto:
        pkg_mod.extract_repo_contract_versions(fake_repo)
    assert exc_proto.value.code == "packaging_version_mismatch"

    # 3. Drift in Rust WIRE_VERSION vs Python WIRE_VERSION
    rs_proto_path.write_text(
        orig_rs_proto.replace(
            "pub const WIRE_VERSION: u32 = 1;",
            "pub const WIRE_VERSION: u32 = 2;",
            1,
        ),
        encoding="utf-8",
    )
    with pytest.raises(PackagingContractError) as exc_wire:
        pkg_mod.extract_repo_contract_versions(fake_repo)
    assert exc_wire.value.code == "packaging_version_mismatch"
    rs_proto_path.write_text(orig_rs_proto, encoding="utf-8")

    # 4. Missing 'bin/*' in pyproject.toml package-data
    pyproject_path = fake_repo / "pyproject.toml"
    orig_pyproject = pyproject_path.read_text(encoding="utf-8")
    pyproject_path.write_text(
        orig_pyproject.replace(', "bin/*"', ""),
        encoding="utf-8",
    )
    with pytest.raises(PackagingContractError) as exc_pkgdata:
        pkg_mod.extract_repo_contract_versions(fake_repo)
    assert exc_pkgdata.value.code == "packaging_pyproject_invalid"
    pyproject_path.write_text(orig_pyproject, encoding="utf-8")


# ============================================================================
# 2. Deterministic Manifest Generation & Schema Validation
# ============================================================================


def test_manifest_generator_determinism_and_strict_schema_validation() -> None:
    versions = pkg_mod.extract_repo_contract_versions(REPO_ROOT)
    pe_bytes_a = _build_synthetic_pe64_bytes(total_size=8192, payload_fill=b"\x41")
    pe_bytes_b = _build_synthetic_pe64_bytes(total_size=8192, payload_fill=b"\x42")

    dict_1, bytes_1, sha_1 = pkg_mod.generate_manifest_from_executable_bytes(
        pe_bytes_a,
        versions=versions,
        repo_root=REPO_ROOT,
    )
    dict_2, bytes_2, sha_2 = pkg_mod.generate_manifest_from_executable_bytes(
        pe_bytes_a,
        versions=versions,
        repo_root=REPO_ROOT,
    )

    # Identical binary + version inputs must produce byte-for-byte identical UTF-8 JSON
    assert bytes_1 == bytes_2
    assert sha_1 == sha_2
    assert dict_1 == dict_2
    assert bytes_1.endswith(b"\n")

    # No timestamps, absolute developer paths, or peer hashes in manifest
    text_1 = bytes_1.decode("utf-8")
    assert "timestamp" not in text_1.lower()
    assert "rynmesh-peer" not in text_1.lower()
    assert ":" not in dict_1["artifact"]["installedFilename"]
    assert "\\" not in dict_1["artifact"]["installedFilename"]
    assert dict_1["artifact"]["installedFilename"] == "ryn_computer_use.exe"
    assert dict_1["artifact"]["size"] == len(pe_bytes_a)
    assert dict_1["artifact"]["sha256"] == hashlib.sha256(pe_bytes_a).hexdigest()
    assert dict_1["artifact"]["machine"] == 0x8664
    assert dict_1["artifact"]["optionalHeaderMagic"] == 0x020B

    # Different executable bytes must produce a different sha256 and different deterministic releaseId
    dict_b, bytes_b, sha_b = pkg_mod.generate_manifest_from_executable_bytes(
        pe_bytes_b,
        versions=versions,
        repo_root=REPO_ROOT,
    )
    assert bytes_b != bytes_1
    assert sha_b != sha_1
    assert dict_b["releaseId"] != dict_1["releaseId"]
    assert dict_b["artifact"]["sha256"] != dict_1["artifact"]["sha256"]

    # Ensure build-computer-use-manifest.py never imports private issuer tokens from binary.py
    script_source = MANIFEST_SCRIPT_PATH.read_text(encoding="utf-8")
    assert "_COMMITMENT_ISSUER_TOKEN" not in script_source
    assert "_LEASE_PROOF_ISSUER_TOKEN" not in script_source
    assert "_PackagedReleaseCommitment" not in script_source
    assert "_ReleaseLeaseProof" not in script_source
    assert "_load_packaged_release_spec" not in script_source


def test_manifest_generator_and_verifier_reject_wrong_pe_size_hash_version_and_duplicate_or_unknown_keys() -> None:
    versions = pkg_mod.extract_repo_contract_versions(REPO_ROOT)
    good_pe = _build_synthetic_pe64_bytes(total_size=4096)
    manifest_dict, manifest_bytes, _ = pkg_mod.generate_manifest_from_executable_bytes(
        good_pe,
        versions=versions,
        repo_root=REPO_ROOT,
    )

    # Positive verification of matching manifest and executable bytes
    verified_dict = pkg_mod.verify_manifest_matches_executable_bytes(
        manifest_bytes,
        good_pe,
        repo_root=REPO_ROOT,
        versions=versions,
    )
    assert verified_dict == manifest_dict

    # 1. Reject undersized binary (< 512 bytes)
    tiny_pe = _build_synthetic_pe64_bytes(total_size=256, size_of_headers=128)
    with pytest.raises(PackagingContractError) as exc_tiny:
        pkg_mod.generate_manifest_from_executable_bytes(
            tiny_pe,
            versions=versions,
            repo_root=REPO_ROOT,
        )
    assert exc_tiny.value.code == "packaging_binary_size_invalid"

    # 2. Reject 32-bit x86 machine (0x014C)
    x86_pe = _build_synthetic_pe64_bytes(machine=0x014C)
    with pytest.raises(PackagingContractError) as exc_x86:
        pkg_mod.generate_manifest_from_executable_bytes(
            x86_pe,
            versions=versions,
            repo_root=REPO_ROOT,
        )
    assert exc_x86.value.code == "packaging_pe_invalid"

    # 3. Reject PE32 optional header magic (0x010B)
    pe32_bytes = _build_synthetic_pe64_bytes(opt_magic=0x010B)
    with pytest.raises(PackagingContractError) as exc_pe32:
        pkg_mod.generate_manifest_from_executable_bytes(
            pe32_bytes,
            versions=versions,
            repo_root=REPO_ROOT,
        )
    assert exc_pe32.value.code == "packaging_pe_invalid"

    # 4. Reject DLL image (IMAGE_FILE_DLL = 0x2000)
    dll_bytes = _build_synthetic_pe64_bytes(characteristics=0x0002 | 0x2000)
    with pytest.raises(PackagingContractError) as exc_dll:
        pkg_mod.generate_manifest_from_executable_bytes(
            dll_bytes,
            versions=versions,
            repo_root=REPO_ROOT,
        )
    assert exc_dll.value.code == "packaging_pe_invalid"

    # 5. Reject SHA-256 mismatch against executable bytes
    tampered_pe = _build_synthetic_pe64_bytes(total_size=4096, payload_fill=b"\xCC")
    with pytest.raises(PackagingContractError) as exc_sha:
        pkg_mod.verify_manifest_matches_executable_bytes(
            manifest_bytes,
            tampered_pe,
            repo_root=REPO_ROOT,
            versions=versions,
        )
    assert exc_sha.value.code == "packaging_binary_sha256_mismatch"

    # 6. Reject size mismatch against executable bytes
    larger_pe = _build_synthetic_pe64_bytes(total_size=8192)
    with pytest.raises(PackagingContractError) as exc_size:
        pkg_mod.verify_manifest_matches_executable_bytes(
            manifest_bytes,
            larger_pe,
            repo_root=REPO_ROOT,
            versions=versions,
        )
    assert exc_size.value.code == "packaging_binary_size_mismatch"

    # 7. Reject workerVersion / protocolVersion / wireVersion mismatch
    wrong_ver = dataclasses.replace(versions, worker_version="9.9.9")
    with pytest.raises(PackagingContractError) as exc_wver:
        pkg_mod.verify_manifest_matches_executable_bytes(
            manifest_bytes,
            good_pe,
            repo_root=REPO_ROOT,
            versions=wrong_ver,
        )
    assert exc_wver.value.code == "packaging_manifest_invalid"

    # 8. Reject duplicate JSON keys in manifest
    dup_key_json = (
        manifest_bytes.decode("utf-8").rstrip()[:-1]
        + ',\n  "workerName": "ryn_computer_use"\n}\n'
    ).encode("utf-8")
    with pytest.raises(PackagingContractError) as exc_dup:
        pkg_mod.verify_manifest_matches_executable_bytes(
            dup_key_json,
            good_pe,
            repo_root=REPO_ROOT,
            versions=versions,
        )
    assert exc_dup.value.code == "packaging_manifest_invalid"

    # 9. Reject unknown extra fields in manifest root or artifact
    extra_root = copy.deepcopy(manifest_dict)
    extra_root["unexpectedField"] = True
    with pytest.raises(PackagingContractError) as exc_extra_root:
        pkg_mod.verify_manifest_matches_executable_bytes(
            json.dumps(extra_root).encode("utf-8"),
            good_pe,
            repo_root=REPO_ROOT,
            versions=versions,
        )
    assert exc_extra_root.value.code == "packaging_manifest_invalid"

    extra_art = copy.deepcopy(manifest_dict)
    extra_art["artifact"]["peerSha256"] = "a" * 64
    with pytest.raises(PackagingContractError) as exc_extra_art:
        pkg_mod.verify_manifest_matches_executable_bytes(
            json.dumps(extra_art).encode("utf-8"),
            good_pe,
            repo_root=REPO_ROOT,
            versions=versions,
        )
    assert exc_extra_art.value.code == "packaging_manifest_invalid"


# ============================================================================
# 3. Native Build Origin, Venv Staging, and PyInstaller Single-Manifest TOC
# ============================================================================


def test_native_origin_and_isolated_venv_staging_prevent_debug_or_repo_shadowing(
    tmp_path: Path,
) -> None:
    build_root = tmp_path / ".codex-tmp" / "windows-package"
    target_dir = build_root / "native-target"
    release_exe = (
        target_dir
        / "x86_64-pc-windows-msvc"
        / "release"
        / "ryn_computer_use.exe"
    )
    release_exe.parent.mkdir(parents=True, exist_ok=True)
    pe_bytes = _build_synthetic_pe64_bytes(total_size=4096)
    release_exe.write_bytes(pe_bytes)

    # Valid isolated release build origin succeeds
    validated_exe = pkg_mod.validate_native_binary_build_origin(
        release_exe,
        expected_target_dir=target_dir,
        repo_root=REPO_ROOT,
    )
    assert validated_exe == release_exe

    # Reject debug binary path
    debug_exe = (
        target_dir / "x86_64-pc-windows-msvc" / "debug" / "ryn_computer_use.exe"
    )
    debug_exe.parent.mkdir(parents=True, exist_ok=True)
    debug_exe.write_bytes(pe_bytes)
    with pytest.raises(PackagingContractError) as exc_debug:
        pkg_mod.validate_native_binary_build_origin(
            debug_exe,
            expected_target_dir=target_dir,
            repo_root=REPO_ROOT,
        )
    assert exc_debug.value.code == "packaging_native_origin_invalid"

    # Reject binary inside repo source crate target directory
    fake_repo = tmp_path / "fake_repo"
    repo_target_exe = (
        fake_repo
        / "native"
        / "ryn_computer_use"
        / "target"
        / "x86_64-pc-windows-msvc"
        / "release"
        / "ryn_computer_use.exe"
    )
    repo_target_exe.parent.mkdir(parents=True, exist_ok=True)
    repo_target_exe.write_bytes(pe_bytes)
    with pytest.raises(PackagingContractError) as exc_repo_target:
        pkg_mod.validate_native_binary_build_origin(
            repo_target_exe,
            repo_root=fake_repo,
        )
    assert exc_repo_target.value.code == "packaging_native_origin_invalid"

    # Test isolated venv staging and repo shadow detection
    venv_root = build_root / "venv"
    venv_rynmesh_dir = venv_root / "Lib" / "site-packages" / "rynmesh"
    venv_rynmesh_dir.mkdir(parents=True, exist_ok=True)
    venv_init = venv_rynmesh_dir / "__init__.py"
    venv_init.write_text("# venv rynmesh\n", encoding="utf-8")

    fake_repo_rynmesh = fake_repo / "rynmesh"
    fake_repo_rynmesh.mkdir(parents=True, exist_ok=True)
    repo_init = fake_repo_rynmesh / "__init__.py"
    repo_init.write_text("# repo rynmesh\n", encoding="utf-8")

    _, manifest_bytes, manifest_sha = pkg_mod.generate_manifest_from_executable(
        release_exe,
        repo_root=REPO_ROOT,
        expected_target_dir=target_dir,
    )

    # Reject if imported rynmesh.__file__ resolves to the repo checkout instead of venv_root
    with pytest.raises(PackagingContractError) as exc_shadow:
        pkg_mod.stage_manifest_into_venv(
            manifest_bytes=manifest_bytes,
            venv_root=venv_root,
            build_root=build_root,
            repo_root=fake_repo,
            imported_rynmesh_init=repo_init,
        )
    assert exc_shadow.value.code == "packaging_venv_shadowed"

    # Succeeds when imported rynmesh.__file__ resolves inside venv_root and repo has no stale manifest
    staging_copy = build_root / "staging" / "computer-use-manifest.json"
    staged_venv_manifest = pkg_mod.stage_manifest_into_venv(
        manifest_bytes=manifest_bytes,
        venv_root=venv_root,
        build_root=build_root,
        repo_root=fake_repo,
        staging_manifest_copy=staging_copy,
        imported_rynmesh_init=venv_init,
    )
    assert staged_venv_manifest.read_bytes() == manifest_bytes
    assert staging_copy.read_bytes() == manifest_bytes

    # Reject if a stale source manifest exists in repo_root/rynmesh/bin/computer-use-manifest.json
    stale_repo_manifest = fake_repo_rynmesh / "bin" / "computer-use-manifest.json"
    stale_repo_manifest.parent.mkdir(parents=True, exist_ok=True)
    stale_repo_manifest.write_bytes(manifest_bytes)
    with pytest.raises(PackagingContractError) as exc_collision:
        pkg_mod.stage_manifest_into_venv(
            manifest_bytes=manifest_bytes,
            venv_root=venv_root,
            build_root=build_root,
            repo_root=fake_repo,
            imported_rynmesh_init=venv_init,
        )
    assert exc_collision.value.code == "packaging_source_manifest_collision"
    stale_repo_manifest.unlink()

    # Verify PyInstaller TOC provenance parsing and single-manifest enforcement
    work_dir = build_root / "work" / "rynmesh-peer"
    work_dir.mkdir(parents=True, exist_ok=True)
    toc_file = work_dir / "PKG-00.toc"
    toc_file.write_text(
        "[\n"
        f"  ('rynmesh\\\\bin\\\\computer-use-manifest.json', {str(staged_venv_manifest)!r}, 'DATA'),\n"
        "]\n",
        encoding="utf-8",
    )
    entries = pkg_mod.extract_toc_data_entries_from_pyinstaller_work(
        build_root / "work"
    )
    assert len(entries) == 1
    pkg_mod.verify_pyinstaller_manifest_entries(
        entries,
        venv_root=venv_root,
        repo_root=fake_repo,
        expected_manifest_sha256=manifest_sha,
    )

    # Reject duplicate manifest entries in PyInstaller TOC
    with pytest.raises(PackagingContractError) as exc_dup_toc:
        pkg_mod.verify_pyinstaller_manifest_entries(
            [
                ("rynmesh/bin/computer-use-manifest.json", str(staged_venv_manifest)),
                ("rynmesh/bin/computer-use-manifest.json", str(staged_venv_manifest)),
            ],
            venv_root=venv_root,
            repo_root=fake_repo,
            expected_manifest_sha256=manifest_sha,
        )
    assert exc_dup_toc.value.code == "packaging_archive_manifest_duplicate"

    # Reject 0 manifest entries in PyInstaller TOC
    with pytest.raises(PackagingContractError) as exc_zero_toc:
        pkg_mod.verify_pyinstaller_manifest_entries(
            [],
            venv_root=venv_root,
            repo_root=fake_repo,
            expected_manifest_sha256=manifest_sha,
        )
    assert exc_zero_toc.value.code == "packaging_archive_manifest_missing"


# ============================================================================
# 4. Real PyInstaller CArchive Extraction, Build Receipt & -SkipSidecar
# ============================================================================


def test_build_receipt_and_skip_sidecar_detect_stale_native_peer_manifest_and_sources(
    tmp_path: Path,
) -> None:
    # Verify actual repo source commitment covers Rust crate, Python bridge, extensions/adapter.py + H1 files, and packaging scripts
    actual_commitment = pkg_mod.compute_reviewed_source_commitment(REPO_ROOT)
    committed_paths = {entry["path"] for entry in actual_commitment["files"]}
    assert "rynmesh/agent_runtime/adapter.py" not in committed_paths
    for expected_rel in (
        "pyproject.toml",
        "native/ryn_computer_use/Cargo.toml",
        "native/ryn_computer_use/src/main.rs",
        "native/ryn_computer_use/src/engine.rs",
        "native/ryn_computer_use/src/protocol.rs",
        "rynmesh/agent_runtime/computer/binary.py",
        "rynmesh/agent_runtime/computer/client.py",
        "rynmesh/agent_runtime/computer/coordinator.py",
        "rynmesh/agent_runtime/computer/protocol.py",
        "rynmesh/agent_runtime/extensions/adapter.py",
        "rynmesh/agent_runtime/extensions/store.py",
        "rynmesh/agent_runtime/native_computer.py",
        "rynmesh/agent_runtime/tools.py",
        "rynmesh/agent_runtime/routes.py",
        "rynmesh/agent_runtime/local_desktop.py",
        "rynmesh/agent_runtime/local_tools.py",
        "rynmesh/agent_runtime/runtime.py",
        "rynmesh/llm_package/process_guard.py",
        "webapp/src-tauri/sidecar/rynmesh_peer_entry.py",
        "webapp/src-tauri/scripts/build-windows.ps1",
        "webapp/src-tauri/scripts/build-computer-use-manifest.py",
        "webapp/src-tauri/tauri.conf.json",
        "webapp/src-tauri/tauri.windows.conf.json",
    ):
        assert expected_rel in committed_paths

    # Construct a complete staged build environment in tmp_path
    build_root = tmp_path / ".codex-tmp" / "windows-package"
    venv_root = build_root / "venv"
    venv_rynmesh_dir = venv_root / "Lib" / "site-packages" / "rynmesh"
    venv_rynmesh_dir.mkdir(parents=True, exist_ok=True)
    venv_init = venv_rynmesh_dir / "__init__.py"
    venv_init.write_text("# venv\n", encoding="utf-8")

    native_target_dir = build_root / "native-target"
    native_release_exe = (
        native_target_dir
        / "x86_64-pc-windows-msvc"
        / "release"
        / "ryn_computer_use.exe"
    )
    native_release_exe.parent.mkdir(parents=True, exist_ok=True)
    native_bytes = _build_synthetic_pe64_bytes(total_size=8192, payload_fill=b"\x11")
    native_release_exe.write_bytes(native_bytes)

    staging_manifest = build_root / "staging" / "computer-use-manifest.json"
    _, manifest_bytes, _ = pkg_mod.generate_manifest_from_executable(
        native_release_exe,
        repo_root=REPO_ROOT,
        expected_target_dir=native_target_dir,
    )
    venv_manifest = pkg_mod.stage_manifest_into_venv(
        manifest_bytes=manifest_bytes,
        venv_root=venv_root,
        build_root=build_root,
        repo_root=REPO_ROOT,
        staging_manifest_copy=staging_manifest,
        imported_rynmesh_init=venv_init,
    )

    work_dir = build_root / "work" / "rynmesh-peer"
    work_dir.mkdir(parents=True, exist_ok=True)
    (work_dir / "PKG-00.toc").write_text(
        f"[('rynmesh\\\\bin\\\\computer-use-manifest.json', {str(venv_manifest)!r}, 'DATA')]\n",
        encoding="utf-8",
    )
    toc_entries = pkg_mod.extract_toc_data_entries_from_pyinstaller_work(
        build_root / "work"
    )

    dist_peer_exe = build_root / "dist" / "rynmesh-peer.exe"
    dist_peer_exe.parent.mkdir(parents=True, exist_ok=True)
    binaries_dir = tmp_path / "binaries"
    binaries_dir.mkdir(parents=True, exist_ok=True)
    staged_native_exe = (
        binaries_dir / "ryn_computer_use-x86_64-pc-windows-msvc.exe"
    )
    staged_peer_exe = binaries_dir / "rynmesh-peer-x86_64-pc-windows-msvc.exe"
    staged_native_exe.write_bytes(native_bytes)

    # P1-5 Regression: A bare synthetic PE64 peer WITHOUT a PyInstaller CArchive MUST fail closed even when PKG-00.toc is valid
    bare_peer_bytes = _build_synthetic_pe64_bytes(total_size=16384, payload_fill=b"\x22")
    dist_peer_exe.write_bytes(bare_peer_bytes)
    staged_peer_exe.write_bytes(bare_peer_bytes)
    with pytest.raises(PackagingContractError) as exc_no_carchive:
        pkg_mod.create_build_receipt(
            repo_root=REPO_ROOT,
            manifest_bytes=manifest_bytes,
            native_release_exe=native_release_exe,
            staged_native_exe=staged_native_exe,
            dist_peer_exe=dist_peer_exe,
            staged_peer_exe=staged_peer_exe,
            tauri_windows_conf_path=TAURI_WIN_CONF_PATH,
            expected_target_dir=native_target_dir,
            archive_manifest_entries=toc_entries,
            venv_root=venv_root,
        )
    assert exc_no_carchive.value.code == "packaging_peer_archive_invalid"

    # Build a peer PE64 with a real, spec-valid PyInstaller CArchive embedding the exact manifest bytes
    peer_bytes = _build_synthetic_pe64_with_carchive(
        [
            ("rynmesh/bin/computer-use-manifest.json", manifest_bytes, True, "x"),
        ],
        total_size=16384,
        payload_fill=b"\x22",
    )
    dist_peer_exe.write_bytes(peer_bytes)
    staged_peer_exe.write_bytes(peer_bytes)

    receipt_dict, receipt_bytes = pkg_mod.create_build_receipt(
        repo_root=REPO_ROOT,
        manifest_bytes=manifest_bytes,
        native_release_exe=native_release_exe,
        staged_native_exe=staged_native_exe,
        dist_peer_exe=dist_peer_exe,
        staged_peer_exe=staged_peer_exe,
        tauri_windows_conf_path=TAURI_WIN_CONF_PATH,
        expected_target_dir=native_target_dir,
        archive_manifest_entries=toc_entries,
        venv_root=venv_root,
    )
    assert receipt_dict["manifest"]["embeddedArchiveCount"] == 1
    assert receipt_dict["manifest"]["boundPeerSha256"] == receipt_dict["peerExecutable"]["sha256"]
    assert receipt_dict["manifest"]["boundPeerSize"] == receipt_dict["peerExecutable"]["size"]

    receipt_path = build_root / "staging" / "computer-release-receipt.json"
    receipt_path.write_bytes(receipt_bytes)

    # Valid pair with real CArchive passes verify-skip-sidecar CLI and helper without modifying manifest
    assert (
        pkg_mod.main(
            [
                "verify-skip-sidecar",
                "--repo-root",
                str(REPO_ROOT),
                "--build-root",
                str(build_root),
                "--venv-root",
                str(venv_root),
                "--work-dir",
                str(build_root / "work"),
                "--native-target-dir",
                str(native_target_dir),
                "--native-exe",
                str(native_release_exe),
                "--staged-native-exe",
                str(staged_native_exe),
                "--dist-peer-exe",
                str(dist_peer_exe),
                "--staged-peer-exe",
                str(staged_peer_exe),
                "--staging-manifest",
                str(staging_manifest),
                "--tauri-windows-conf",
                str(TAURI_WIN_CONF_PATH),
                "--receipt-path",
                str(receipt_path),
            ]
        )
        == 0
    )

    # 1. Tampering with staged peer binary fails -SkipSidecar
    staged_peer_exe.write_bytes(
        _build_synthetic_pe64_with_carchive(
            [("rynmesh/bin/computer-use-manifest.json", manifest_bytes, True, "x")],
            total_size=16384,
            payload_fill=b"\x33",
        )
    )
    with pytest.raises(PackagingContractError) as exc_peer_tamper:
        pkg_mod.verify_build_receipt_and_pair(
            receipt_bytes=receipt_bytes,
            manifest_bytes=manifest_bytes,
            repo_root=REPO_ROOT,
            native_release_exe=native_release_exe,
            staged_native_exe=staged_native_exe,
            dist_peer_exe=dist_peer_exe,
            staged_peer_exe=staged_peer_exe,
            tauri_windows_conf_path=TAURI_WIN_CONF_PATH,
            expected_target_dir=native_target_dir,
            archive_manifest_entries=toc_entries,
            venv_root=venv_root,
        )
    assert exc_peer_tamper.value.code == "packaging_staged_peer_mismatch"
    staged_peer_exe.write_bytes(peer_bytes)

    # 2. Replacing both dist and staged peer with a different peer build fails receipt peer hash check
    other_peer = _build_synthetic_pe64_with_carchive(
        [("rynmesh/bin/computer-use-manifest.json", manifest_bytes, True, "x")],
        total_size=16384,
        payload_fill=b"\x44",
    )
    dist_peer_exe.write_bytes(other_peer)
    staged_peer_exe.write_bytes(other_peer)
    with pytest.raises(PackagingContractError) as exc_peer_stale:
        pkg_mod.verify_build_receipt_and_pair(
            receipt_bytes=receipt_bytes,
            manifest_bytes=manifest_bytes,
            repo_root=REPO_ROOT,
            native_release_exe=native_release_exe,
            staged_native_exe=staged_native_exe,
            dist_peer_exe=dist_peer_exe,
            staged_peer_exe=staged_peer_exe,
            tauri_windows_conf_path=TAURI_WIN_CONF_PATH,
            expected_target_dir=native_target_dir,
            archive_manifest_entries=toc_entries,
            venv_root=venv_root,
        )
    assert exc_peer_stale.value.code == "packaging_receipt_peer_mismatch"
    dist_peer_exe.write_bytes(peer_bytes)
    staged_peer_exe.write_bytes(peer_bytes)

    # 3. Stale source commitment in receipt (e.g., Rust or Python source changed since sidecar build) fails -SkipSidecar
    stale_src_receipt = copy.deepcopy(receipt_dict)
    stale_src_receipt["sourceCommitment"]["treeSha256"] = "0" * 64
    with pytest.raises(PackagingContractError) as exc_src_stale:
        pkg_mod.verify_build_receipt_and_pair(
            receipt_bytes=json.dumps(stale_src_receipt).encode("utf-8"),
            manifest_bytes=manifest_bytes,
            repo_root=REPO_ROOT,
            native_release_exe=native_release_exe,
            staged_native_exe=staged_native_exe,
            dist_peer_exe=dist_peer_exe,
            staged_peer_exe=staged_peer_exe,
            tauri_windows_conf_path=TAURI_WIN_CONF_PATH,
            expected_target_dir=native_target_dir,
            archive_manifest_entries=toc_entries,
            venv_root=venv_root,
        )
    assert exc_src_stale.value.code == "packaging_receipt_source_stale"


def test_real_pyinstaller_carchive_reader_distinguishes_absent_stale_duplicate_and_valid_embedded_manifests() -> None:
    versions = pkg_mod.extract_repo_contract_versions(REPO_ROOT)
    native_a = _build_synthetic_pe64_bytes(total_size=8192, payload_fill=b"\x51")
    native_b = _build_synthetic_pe64_bytes(total_size=8192, payload_fill=b"\x52")
    _, manifest_a, sha_a = pkg_mod.generate_manifest_from_executable_bytes(
        native_a,
        versions=versions,
        repo_root=REPO_ROOT,
    )
    _, manifest_b, _ = pkg_mod.generate_manifest_from_executable_bytes(
        native_b,
        versions=versions,
        repo_root=REPO_ROOT,
    )

    # 1. Valid compressed CArchive entry extracts exact manifest bytes and binds peer SHA-256/size
    peer_valid = _build_synthetic_pe64_with_carchive(
        [
            ("rynmesh\\bin\\computer-use-manifest.json", manifest_a, True, "x"),
            ("rynmesh\\__init__.pyc", b"\x00\x01\x02\x03", False, "m"),
        ],
        total_size=12288,
    )
    evidence = pkg_mod.inspect_peer_embedded_manifest_bytes(
        peer_valid,
        expected_manifest_bytes=manifest_a,
    )
    assert evidence == {
        "packageRelativePath": "rynmesh/bin/computer-use-manifest.json",
        "sha256": sha_a,
        "size": len(manifest_a),
        "embeddedArchiveCount": 1,
        "boundPeerSha256": hashlib.sha256(peer_valid).hexdigest(),
        "boundPeerSize": len(peer_valid),
    }

    # 2. Absent manifest entry in valid CArchive -> packaging_archive_manifest_missing
    peer_no_manifest = _build_synthetic_pe64_with_carchive(
        [("rynmesh\\__init__.pyc", b"\x00\x01\x02\x03", False, "m")],
        total_size=12288,
    )
    with pytest.raises(PackagingContractError) as exc_missing:
        pkg_mod.inspect_peer_embedded_manifest_bytes(
            peer_no_manifest,
            expected_manifest_bytes=manifest_a,
        )
    assert exc_missing.value.code == "packaging_archive_manifest_missing"

    # 3. Duplicate manifest entries in CArchive -> packaging_archive_manifest_duplicate
    peer_dup_manifest = _build_synthetic_pe64_with_carchive(
        [
            ("rynmesh/bin/computer-use-manifest.json", manifest_a, True, "x"),
            ("rynmesh\\bin\\computer-use-manifest.json", manifest_a, False, "x"),
        ],
        total_size=12288,
    )
    with pytest.raises(PackagingContractError) as exc_dup:
        pkg_mod.inspect_peer_embedded_manifest_bytes(
            peer_dup_manifest,
            expected_manifest_bytes=manifest_a,
        )
    assert exc_dup.value.code == "packaging_archive_manifest_duplicate"

    # 4. Wrong archive destination path -> packaging_archive_manifest_dest_invalid
    peer_bad_dest = _build_synthetic_pe64_with_carchive(
        [("bin/computer-use-manifest.json", manifest_a, True, "x")],
        total_size=12288,
    )
    with pytest.raises(PackagingContractError) as exc_dest:
        pkg_mod.inspect_peer_embedded_manifest_bytes(
            peer_bad_dest,
            expected_manifest_bytes=manifest_a,
        )
    assert exc_dest.value.code == "packaging_archive_manifest_dest_invalid"

    # 5. Stale/mismatched embedded manifest bytes -> packaging_archive_manifest_sha256_mismatch
    peer_stale_manifest = _build_synthetic_pe64_with_carchive(
        [("rynmesh/bin/computer-use-manifest.json", manifest_b, True, "x")],
        total_size=12288,
    )
    with pytest.raises(PackagingContractError) as exc_stale:
        pkg_mod.inspect_peer_embedded_manifest_bytes(
            peer_stale_manifest,
            expected_manifest_bytes=manifest_a,
        )
    assert exc_stale.value.code == "packaging_archive_manifest_sha256_mismatch"

    # 6. Bounded decompression sentinel: flush() is NEVER invoked on either valid or refusal paths,
    # and truncated/trailing-data/oversized streams fail closed before any official CArchiveReader extraction.
    flush_calls = 0

    class _NoFlushDecompressSentinel:
        def __init__(self) -> None:
            self._inner = zlib.decompressobj()

        def decompress(self, data: bytes, max_length: int = 0) -> bytes:
            assert max_length == pkg_mod.MAX_MANIFEST_ARCHIVE_ENTRY_BYTES + 1
            return self._inner.decompress(data, max_length)

        def flush(self, *args: Any, **kwargs: Any) -> bytes:
            nonlocal flush_calls
            flush_calls += 1
            raise AssertionError("Unbounded flush() must never be called")

        @property
        def unconsumed_tail(self) -> bytes:
            return self._inner.unconsumed_tail

        @property
        def eof(self) -> bool:
            return self._inner.eof

        @property
        def unused_data(self) -> bytes:
            return self._inner.unused_data

    valid_entry = pkg_mod.PyInstallerArchiveEntry(
        name="rynmesh/bin/computer-use-manifest.json",
        data_offset=0,
        compressed_size=len(zlib.compress(manifest_a)),
        uncompressed_size=len(manifest_a),
        compression_flag=1,
        typecode="x",
        raw_bytes=zlib.compress(manifest_a),
    )
    assert (
        pkg_mod._decompress_manifest_archive_entry(
            valid_entry,
            decompressobj_factory=_NoFlushDecompressSentinel,
        )
        == manifest_a
    )
    assert flush_calls == 0

    # Refusal path: stream whose actual uncompressed output exceeds MAX_MANIFEST_ARCHIVE_ENTRY_BYTES
    # while lying about uncompressed_size in TOC is stopped by bounded decompress(MAX + 1) without flush()
    over_cap_compressed = zlib.compress(
        b"A" * (pkg_mod.MAX_MANIFEST_ARCHIVE_ENTRY_BYTES + 32)
    )
    lying_over_cap_entry = pkg_mod.PyInstallerArchiveEntry(
        name="rynmesh/bin/computer-use-manifest.json",
        data_offset=0,
        compressed_size=len(over_cap_compressed),
        uncompressed_size=len(manifest_a),
        compression_flag=1,
        typecode="x",
        raw_bytes=over_cap_compressed,
    )
    with pytest.raises(PackagingContractError) as exc_over_cap:
        pkg_mod._decompress_manifest_archive_entry(
            lying_over_cap_entry,
            decompressobj_factory=_NoFlushDecompressSentinel,
        )
    assert exc_over_cap.value.code == "packaging_peer_archive_invalid"
    assert flush_calls == 0

    # Refusal path: truncated zlib stream (not eof) -> packaging_peer_archive_invalid without flush()
    truncated_raw = zlib.compress(manifest_a)[:-2]
    truncated_entry = pkg_mod.PyInstallerArchiveEntry(
        name="rynmesh/bin/computer-use-manifest.json",
        data_offset=0,
        compressed_size=len(truncated_raw),
        uncompressed_size=len(manifest_a),
        compression_flag=1,
        typecode="x",
        raw_bytes=truncated_raw,
    )
    with pytest.raises(PackagingContractError) as exc_trunc:
        pkg_mod._decompress_manifest_archive_entry(
            truncated_entry,
            decompressobj_factory=_NoFlushDecompressSentinel,
        )
    assert exc_trunc.value.code == "packaging_peer_archive_invalid"
    assert flush_calls == 0

    # Refusal path: trailing unused bytes after complete zlib stream -> packaging_peer_archive_invalid without flush()
    trailing_raw = zlib.compress(manifest_a) + b"trailing"
    trailing_entry = pkg_mod.PyInstallerArchiveEntry(
        name="rynmesh/bin/computer-use-manifest.json",
        data_offset=0,
        compressed_size=len(trailing_raw),
        uncompressed_size=len(manifest_a),
        compression_flag=1,
        typecode="x",
        raw_bytes=trailing_raw,
    )
    with pytest.raises(PackagingContractError) as exc_trailing:
        pkg_mod._decompress_manifest_archive_entry(
            trailing_entry,
            decompressobj_factory=_NoFlushDecompressSentinel,
        )
    assert exc_trailing.value.code == "packaging_peer_archive_invalid"
    assert flush_calls == 0


# ============================================================================
# 5. Actual Tauri Windows Config, build-windows.ps1, and Sidecar Entry Contracts
# ============================================================================


def _assert_powershell_variables_declared_before_use(ps1_source: str) -> None:
    """Verify every `$variable` referenced in `ps1_source` is declared via `param(...)`, `function Name(...)`, or `$var = ...` before use."""
    declared_vars: set[str] = {
        "PSScriptRoot",
        "LASTEXITCODE",
        "ErrorActionPreference",
        "null",
    }
    assign_re = re.compile(r"\$(?P<name>[A-Za-z_][A-Za-z0-9_]*)\s*=")
    typed_param_re = re.compile(
        r"\[[A-Za-z_][A-Za-z0-9_.\[\]]*\]\s*\$(?P<name>[A-Za-z_][A-Za-z0-9_]*)"
    )
    ref_re = re.compile(
        r"\$(?:\{(?P<braced>[A-Za-z_][A-Za-z0-9_]*)\}|(?P<bare>[A-Za-z_][A-Za-z0-9_:]*))"
    )

    for line_no, raw_line in enumerate(ps1_source.splitlines(), start=1):
        code_line = raw_line.split("#", 1)[0]
        for m_param in typed_param_re.finditer(code_line):
            declared_vars.add(m_param.group("name"))
        lhs_names = [m.group("name") for m in assign_re.finditer(code_line)]
        for m_ref in ref_re.finditer(code_line):
            token = m_ref.group("braced") or m_ref.group("bare")
            assert token is not None
            if token.lower().startswith("env:"):
                continue
            start_pos = m_ref.start()
            if any(
                code_line[start_pos:].startswith(f"${lhs}")
                and re.match(rf"^\${re.escape(lhs)}\s*=", code_line[start_pos:])
                for lhs in lhs_names
            ):
                continue
            assert token in declared_vars, (
                f"Undeclared PowerShell variable ${token} referenced at build-windows.ps1:{line_no}"
            )
        declared_vars.update(lhs_names)


def test_actual_tauri_windows_config_build_script_and_sidecar_entry_contracts(
    tmp_path: Path,
) -> None:
    # 1. Validate actual webapp/src-tauri/tauri.windows.conf.json and base tauri.conf.json
    win_conf, _ = pkg_mod.validate_tauri_windows_config(
        TAURI_WIN_CONF_PATH,
        repo_root=REPO_ROOT,
    )
    assert win_conf["bundle"]["externalBin"] == [
        "binaries/rynmesh-peer",
        "binaries/ryn_computer_use",
    ]
    assert win_conf["bundle"]["resources"] == []
    assert win_conf["bundle"]["windows"]["nsis"]["installMode"] == "currentUser"

    base_conf = json.loads(TAURI_BASE_CONF_PATH.read_text(encoding="utf-8"))
    assert base_conf["bundle"]["externalBin"] == ["binaries/rynmesh-peer"]

    # Reject a broken tauri.windows.conf.json that drops binaries/rynmesh-peer or mounts resources
    bad_conf_path = tmp_path / "bad.tauri.windows.conf.json"
    bad_conf = copy.deepcopy(win_conf)
    bad_conf["bundle"]["externalBin"] = ["binaries/ryn_computer_use"]
    bad_conf_path.write_text(json.dumps(bad_conf), encoding="utf-8")
    with pytest.raises(PackagingContractError) as exc_missing_peer:
        pkg_mod.validate_tauri_windows_config(bad_conf_path, repo_root=REPO_ROOT)
    assert exc_missing_peer.value.code == "packaging_tauri_external_bin_invalid"

    # 2. Inspect actual webapp/src-tauri/scripts/build-windows.ps1 and verify every referenced $variable is declared before use
    ps1_text = BUILD_PS1_PATH.read_text(encoding="utf-8")
    assert "$nativeCargoManifest" not in ps1_text
    _assert_powershell_variables_declared_before_use(ps1_text)

    # Typo-negative regression: reintroducing undeclared $nativeCargoManifest is caught by the scanner
    with pytest.raises(AssertionError, match=r"nativeCargoManifest"):
        _assert_powershell_variables_declared_before_use(
            ps1_text.replace(
                "--manifest-path $nativeCrateManifest",
                "--manifest-path $nativeCargoManifest",
                1,
            )
        )

    # Verify cargo build --manifest-path argument matches the declared Cargo.toml variable
    m_cargo_manifest_arg = re.search(
        r"cargo\s+build\s+--manifest-path\s+\$(?P<var>[A-Za-z_][A-Za-z0-9_]*)\s+--release\s+--locked\s+--target\s+x86_64-pc-windows-msvc\s+--bin\s+ryn_computer_use",
        ps1_text,
    )
    assert m_cargo_manifest_arg is not None
    cargo_manifest_var = m_cargo_manifest_arg.group("var")
    assert cargo_manifest_var == "nativeCrateManifest"
    assert (
        f"${cargo_manifest_var} = Join-Path $repoRoot 'native/ryn_computer_use/Cargo.toml'"
        in ps1_text
    )

    assert "$env:CARGO_TARGET_DIR = $nativeTargetDir" in ps1_text
    assert "stage-manifest" in ps1_text
    assert "Push-Location $buildRoot" in ps1_text
    assert "--collect-data rynmesh" in ps1_text
    assert "rynmesh-peer-x86_64-pc-windows-msvc.exe" in ps1_text
    assert "ryn_computer_use-x86_64-pc-windows-msvc.exe" in ps1_text
    assert "write-receipt" in ps1_text
    assert "verify-skip-sidecar" in ps1_text

    # Verify ordering in build-windows.ps1: cargo build -> pip install -> stage-manifest -> Push-Location $buildRoot + PyInstaller -> write-receipt
    idx_cargo = ps1_text.index(f"cargo build --manifest-path ${cargo_manifest_var}")
    idx_pip = ps1_text.index("-m pip install")
    idx_stage = ps1_text.index("stage-manifest")
    idx_push_build = ps1_text.index("Push-Location $buildRoot")
    idx_pyinstaller = ps1_text.index("-m PyInstaller --onefile")
    idx_write_receipt = ps1_text.index("write-receipt")
    assert (
        idx_cargo
        < idx_pip
        < idx_stage
        < idx_push_build
        < idx_pyinstaller
        < idx_write_receipt
    )

    # 3. Inspect actual webapp/src-tauri/sidecar/rynmesh_peer_entry.py AST & ordering
    entry_text = SIDECAR_ENTRY_PATH.read_text(encoding="utf-8")
    entry_tree = ast.parse(entry_text, filename=str(SIDECAR_ENTRY_PATH))

    # Top-level imports must remain only `sys` and `multiprocessing`
    top_imports = [
        node
        for node in entry_tree.body
        if isinstance(node, (ast.Import, ast.ImportFrom))
    ]
    imported_names = [
        alias.name for node in top_imports for alias in node.names
    ]
    assert imported_names == ["sys", "multiprocessing"]

    idx_pkg_check = entry_text.index('"--ryn-computer-use-package-check"')
    idx_peer_http = entry_text.index("from rynmesh.peer_http import main")
    assert idx_pkg_check < idx_peer_http

    # Verify all existing entry flags remain intact
    for existing_flag in (
        '"--ryn-document-worker"',
        '"--ryn-native-worker"',
        '"--ryn-computer-use-worker"',
        '"--ryn-private-cli-worker"',
        '"--restore-space"',
    ):
        assert existing_flag in entry_text

    # Verify package-check helper uses zero-arg NativeWorkerClient(), verify_lease_release_authority,
    # session.request("shutdown", ...) + validate_shutdown_result, and never calls UI/window/action methods or touches 8791
    assert "NativeWorkerClient()" in entry_text
    assert "verify_lease_release_authority(" in entry_text
    assert "client.ensure_session()" in entry_text
    assert 'session.request(\n            "shutdown",' in entry_text
    assert "validate_shutdown_result(" in entry_text
    assert '.get("shuttingDown") is True' in entry_text
    assert "client.close()" in entry_text
    assert "except BaseException:" not in entry_text
    for forbidden in (
        "list_windows",
        "get_window",
        "get_state",
        "list_apps",
        "execute_action",
        "action.prepare",
        "action.commit",
        "8791",
    ):
        assert forbidden not in entry_text


def test_sidecar_package_check_validates_shutdown_ack_and_emits_single_json_line_without_catching_system_exit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sidecar_mod = _load_sidecar_entry_module()
    import rynmesh.agent_runtime.computer as comp_pkg

    class _FakeProc:
        def poll(self) -> int | None:
            return 0

    class _FakeLease:
        def __init__(self) -> None:
            self.closed = False

        def close(self) -> None:
            self.closed = True

    class _FakeSession:
        def __init__(self, shutdown_reply: Any) -> None:
            self._artifact_lease = _FakeLease()
            self.process_identity = object()
            self.handshake = {"protocolVersion": "1.0.0"}
            self.process = _FakeProc()
            self._shutdown_reply = shutdown_reply
            self.requests: list[tuple[str, dict[str, Any], dict[str, Any]]] = []

        def request(self, method: str, params: dict[str, Any], **kwargs: Any) -> tuple[Any, int]:
            self.requests.append((method, params, kwargs))
            return self._shutdown_reply, 1

    class _FakeClient:
        def __init__(self, shutdown_reply: Any) -> None:
            self.session = _FakeSession(shutdown_reply)
            self.closed = False

        def ensure_session(self) -> _FakeSession:
            return self.session

        def cache_status(self) -> dict[str, Any]:
            if self.closed:
                return {
                    "status": "closed",
                    "mode": "production",
                    "closed": True,
                    "live": False,
                    "verified": False,
                    "releaseVerified": False,
                    "binding": None,
                }
            return {
                "status": "ready",
                "mode": "production",
                "closed": False,
                "live": True,
                "verified": True,
                "releaseVerified": True,
                "protocolVersion": "1.0.0",
                "worker": {"name": "ryn_computer_use", "version": "0.1.0", "pid": 4242},
                "binding": {
                    "releaseVerified": True,
                    "releaseId": "ryn-computer-use-v0.1.0-w1-p1.0.0-0123456789abcdef",
                    "manifestDigest": "a" * 64,
                    "artifact": {
                        "sha256": "b" * 64,
                        "size": 8192,
                        "targetTriple": "x86_64-pc-windows-msvc",
                        "machine": 0x8664,
                        "optionalHeaderMagic": 0x020B,
                    },
                    "process": {
                        "pid": 4242,
                        "creationTime": 133700000000000000,
                        "fileId": "1:2",
                        "verifiedBy": "win32_process_handle",
                    },
                },
            }

        def close(self) -> None:
            self.closed = True

    monkeypatch.setattr(
        comp_pkg,
        "verify_lease_release_authority",
        lambda lease, *, process_identity=None, handshake=None: True,
    )

    # 1. Valid {"shuttingDown": True} acknowledgement -> exit code 0, strictly 1 JSON line, SystemExit NOT caught
    out_ok = io.StringIO()
    client_ok = _FakeClient({"shuttingDown": True})
    with pytest.raises(SystemExit) as exc_ok:
        sidecar_mod._emit_and_exit_computer_use_package_check(
            ["rynmesh-peer.exe", "--ryn-computer-use-package-check"],
            stream=out_ok,
            client_factory=lambda: client_ok,
            is_frozen=True,
        )
    assert exc_ok.value.code == 0
    lines_ok = out_ok.getvalue().splitlines()
    assert len(lines_ok) == 1
    parsed_ok = json.loads(lines_ok[0])
    assert parsed_ok["ok"] is True
    assert parsed_ok["releaseVerified"] is True
    assert parsed_ok["cleanup"] == {
        "shutdownClean": True,
        "leaseClosed": True,
        "clientClosed": True,
        "workerTerminated": True,
    }
    assert len(client_ok.session.requests) == 1
    assert client_ok.session.requests[0][0] == "shutdown"

    # 2. Old wrong shutdown shape {"stopping": True} is rejected by real validate_shutdown_result -> 1 JSON error line, exit 1
    out_bad_shape = io.StringIO()
    client_bad_shape = _FakeClient({"stopping": True})
    with pytest.raises(SystemExit) as exc_bad_shape:
        sidecar_mod._emit_and_exit_computer_use_package_check(
            ["rynmesh-peer.exe", "--ryn-computer-use-package-check"],
            stream=out_bad_shape,
            client_factory=lambda: client_bad_shape,
            is_frozen=True,
        )
    assert exc_bad_shape.value.code == 1
    lines_bad = out_bad_shape.getvalue().splitlines()
    assert len(lines_bad) == 1
    parsed_bad = json.loads(lines_bad[0])
    assert parsed_bad["ok"] is False
    assert parsed_bad["errorCode"] == "agent_computer_protocol_invalid"
    assert parsed_bad["phase"] == "shutdown"
    assert client_bad_shape.closed is True
    assert client_bad_shape.session._artifact_lease.closed is True

    # 3. {"shuttingDown": False} -> shutdownClean=False, ok=False, exit 1, strictly 1 JSON line
    out_false_ack = io.StringIO()
    client_false_ack = _FakeClient({"shuttingDown": False})
    with pytest.raises(SystemExit) as exc_false_ack:
        sidecar_mod._emit_and_exit_computer_use_package_check(
            ["rynmesh-peer.exe", "--ryn-computer-use-package-check"],
            stream=out_false_ack,
            client_factory=lambda: client_false_ack,
            is_frozen=True,
        )
    assert exc_false_ack.value.code == 1
    lines_false = out_false_ack.getvalue().splitlines()
    assert len(lines_false) == 1
    parsed_false = json.loads(lines_false[0])
    assert parsed_false["ok"] is False
    assert parsed_false["cleanup"]["shutdownClean"] is False
    assert client_false_ack.closed is True
    assert client_false_ack.session._artifact_lease.closed is True

    # 4. Non-frozen rejection -> strictly 1 JSON line, exit 1
    out_unfrozen = io.StringIO()
    with pytest.raises(SystemExit) as exc_unfrozen:
        sidecar_mod._emit_and_exit_computer_use_package_check(
            ["rynmesh-peer.exe", "--ryn-computer-use-package-check"],
            stream=out_unfrozen,
            is_frozen=False,
        )
    assert exc_unfrozen.value.code == 1
    lines_unfrozen = out_unfrozen.getvalue().splitlines()
    assert len(lines_unfrozen) == 1
    assert json.loads(lines_unfrozen[0])["errorCode"] == "package_check_frozen_required"
