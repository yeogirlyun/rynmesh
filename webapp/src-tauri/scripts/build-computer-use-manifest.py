"""Authoritative R2 Windows packaging helper for `ryn_computer_use.exe`, `computer-use-manifest.json`, PyInstaller CArchive single-manifest extraction, and `computer-release-receipt.json` pair/source validation.

This module provides pure, deterministic validation and generation functions used by
`webapp/src-tauri/scripts/build-windows.ps1` and unit-tested in `tests/test_computer_use_packaging.py`.
It NEVER imports private release-authority tokens from `rynmesh.agent_runtime.computer.binary`
and NEVER executes target PE binaries.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import struct
import subprocess
import sys
import zlib
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Sequence

try:
    import tomllib  # Python 3.11+
except ModuleNotFoundError:  # pragma: no cover
    import tomli as tomllib  # type: ignore[no-redef]


MANIFEST_SCHEMA_VERSION = 1
RECEIPT_SCHEMA_VERSION = 1

SUPPORTED_TARGET_TRIPLE = "x86_64-pc-windows-msvc"
INSTALLED_EXECUTABLE_FILENAME = "ryn_computer_use.exe"
TAURI_STAGED_NATIVE_FILENAME = f"ryn_computer_use-{SUPPORTED_TARGET_TRIPLE}.exe"
TAURI_STAGED_PEER_FILENAME = f"rynmesh-peer-{SUPPORTED_TARGET_TRIPLE}.exe"
EXPECTED_TAURI_EXTERNAL_BINS = (
    "binaries/rynmesh-peer",
    "binaries/ryn_computer_use",
)

MANIFEST_FILENAME = "computer-use-manifest.json"
RECEIPT_FILENAME = "computer-release-receipt.json"
MANIFEST_PACKAGE_REL_POSIX = "rynmesh/bin/computer-use-manifest.json"

IMAGE_FILE_MACHINE_AMD64 = 0x8664
IMAGE_NT_OPTIONAL_HDR64_MAGIC = 0x020B

PYINSTALLER_COOKIE_MAGIC = b"MEI\x0c\x0b\x0a\x0b\x0e"
PYINSTALLER_COOKIE_FORMAT = "!8sIIii64s"
PYINSTALLER_COOKIE_SIZE = struct.calcsize(PYINSTALLER_COOKIE_FORMAT)  # 88 bytes
PYINSTALLER_TOC_ENTRY_FORMAT = "!iIIIBB"
PYINSTALLER_TOC_ENTRY_HEADER_SIZE = struct.calcsize(PYINSTALLER_TOC_ENTRY_FORMAT)  # 18 bytes
MAX_CARCHIVE_SEARCH_WINDOW_BYTES = 16 * 1024 * 1024
MAX_CARCHIVE_TOC_BYTES = 32 * 1024 * 1024
MAX_CARCHIVE_TOC_ENTRIES = 100_000
MAX_MANIFEST_ARCHIVE_ENTRY_BYTES = 64 * 1024

REVIEWED_EXPLICIT_RELATIVE_FILES: tuple[str, ...] = (
    "pyproject.toml",
    "native/ryn_computer_use/Cargo.toml",
    "native/ryn_computer_use/Cargo.lock",
    "rynmesh/llm_package/process_guard.py",
    "rynmesh/agent_runtime/extensions/adapter.py",
    "rynmesh/agent_runtime/extensions/store.py",
    "rynmesh/agent_runtime/native_computer.py",
    "rynmesh/agent_runtime/tools.py",
    "rynmesh/agent_runtime/routes.py",
    "rynmesh/agent_runtime/local_desktop.py",
    "rynmesh/agent_runtime/local_tools.py",
    "rynmesh/agent_runtime/runtime.py",
    "webapp/src-tauri/sidecar/rynmesh_peer_entry.py",
    "webapp/src-tauri/scripts/build-windows.ps1",
    "webapp/src-tauri/scripts/build-computer-use-manifest.py",
    "webapp/src-tauri/tauri.conf.json",
    "webapp/src-tauri/tauri.windows.conf.json",
)

REVIEWED_OPTIONAL_RELATIVE_FILES: tuple[str, ...] = (
    "rynmesh/agent_runtime/extensions/computer_use_server.py",
)

_RUST_WIRE_VERSION_RE = re.compile(
    r"^\s*pub\s+const\s+WIRE_VERSION\s*:\s*u32\s*=\s*(\d+)\s*;",
    re.MULTILINE,
)
_RUST_PROTOCOL_SEMVER_RE = re.compile(
    r'^\s*pub\s+const\s+PROTOCOL_SEMVER\s*:\s*&str\s*=\s*"([^"]+)"\s*;',
    re.MULTILINE,
)
_RUST_ENGINE_WORKER_NAME_RE = re.compile(
    r'name\s*:\s*"([^"]+)"\.to_string\(\)',
)
_RUST_ENGINE_WORKER_VER_MACRO_RE = re.compile(
    r'version\s*:\s*env!\(\s*"CARGO_PKG_VERSION"\s*\)\.to_string\(\)',
)

_PY_WIRE_VERSION_RE = re.compile(
    r"^\s*WIRE_VERSION\s*(?::\s*int\s*)?=\s*(\d+)\s*$",
    re.MULTILINE,
)
_PY_PROTOCOL_SEMVER_RE = re.compile(
    r'^\s*PROTOCOL_SEMVER\s*(?::\s*str\s*)?=\s*"([^"]+)"\s*$',
    re.MULTILINE,
)
_PY_WORKER_NAME_RE = re.compile(
    r'^\s*EXPECTED_WORKER_NAME\s*(?::\s*str\s*)?=\s*"([^"]+)"\s*$',
    re.MULTILINE,
)
_PY_WORKER_VERSION_RE = re.compile(
    r'^\s*EXPECTED_WORKER_VERSION\s*(?::\s*str\s*)?=\s*"([^"]+)"\s*$',
    re.MULTILINE,
)


class PackagingContractError(ValueError):
    """Raised when any R2 build, manifest, archive, Tauri config, or receipt check fails."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code
        self.detail = message


@dataclass(frozen=True)
class RepoContractVersions:
    """Authoritative version and identity constants cross-checked across Rust and Python sources."""

    rynmesh_package_version: str
    worker_name: str
    worker_version: str
    protocol_version: str
    wire_version: int
    target_triple: str
    installed_filename: str


@dataclass(frozen=True)
class PyInstallerArchiveEntry:
    """Single entry parsed from a PyInstaller onefile CArchive (`MEI\\x0c\\x0b\\x0a\\x0b\\x0e`)."""

    name: str
    data_offset: int
    compressed_size: int
    uncompressed_size: int
    compression_flag: int
    typecode: str
    raw_bytes: bytes


def _ensure_repo_on_sys_path(repo_root: Path) -> None:
    repo_str = str(repo_root.resolve())
    if repo_str not in sys.path:
        sys.path.insert(0, repo_str)


def _import_binary_validators(repo_root: Path) -> Any:
    _ensure_repo_on_sys_path(repo_root)
    from rynmesh.agent_runtime.computer import binary as binary_mod

    return binary_mod


def _import_protocol_validators(repo_root: Path) -> Any:
    _ensure_repo_on_sys_path(repo_root)
    from rynmesh.agent_runtime.computer import protocol as protocol_mod

    return protocol_mod


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _read_file_bytes_and_sha256(path: Path, *, missing_code: str) -> tuple[bytes, str]:
    try:
        if not path.exists() or not path.is_file() or path.is_symlink():
            raise PackagingContractError(
                missing_code,
                f"Required regular file missing or symlink: {path}",
            )
        raw = path.read_bytes()
    except PackagingContractError:
        raise
    except OSError as exc:
        raise PackagingContractError(
            missing_code,
            f"Unable to read file {path}: {exc}",
        ) from exc
    return raw, _sha256_bytes(raw)


def _norm_abs(path: str | Path) -> str:
    return os.path.normcase(os.path.abspath(os.path.normpath(str(path))))


def _is_relative_to_norm(child: str | Path, parent: str | Path) -> bool:
    child_n = _norm_abs(child)
    parent_n = _norm_abs(parent).rstrip("\\/")
    return child_n == parent_n or child_n.startswith(parent_n + os.sep)


def extract_repo_contract_versions(repo_root: str | Path) -> RepoContractVersions:
    """Extract and cross-verify worker/protocol/package versions across Rust and Python sources.

    Never guesses defaults or permits `None`/mismatched constants between Cargo/Rust and Python.
    """
    root = Path(repo_root)

    pyproject_raw, _ = _read_file_bytes_and_sha256(
        root / "pyproject.toml",
        missing_code="packaging_pyproject_missing",
    )
    try:
        pyproject_data = tomllib.loads(pyproject_raw.decode("utf-8"))
    except Exception as exc:
        raise PackagingContractError(
            "packaging_pyproject_invalid",
            f"Invalid pyproject.toml: {exc}",
        ) from exc

    project_tbl = pyproject_data.get("project")
    if not isinstance(project_tbl, dict):
        raise PackagingContractError(
            "packaging_pyproject_invalid",
            "Missing [project] table in pyproject.toml",
        )
    rynmesh_ver = project_tbl.get("version")
    if not isinstance(rynmesh_ver, str) or not rynmesh_ver.strip():
        raise PackagingContractError(
            "packaging_pyproject_invalid",
            "Missing non-empty project.version in pyproject.toml",
        )

    pkg_data = (
        pyproject_data.get("tool", {})
        .get("setuptools", {})
        .get("package-data", {})
        .get("rynmesh", [])
    )
    if not isinstance(pkg_data, list) or "bin/*" not in pkg_data:
        raise PackagingContractError(
            "packaging_pyproject_invalid",
            "pyproject.toml [tool.setuptools.package-data].rynmesh must include 'bin/*'",
        )

    cargo_raw, _ = _read_file_bytes_and_sha256(
        root / "native" / "ryn_computer_use" / "Cargo.toml",
        missing_code="packaging_cargo_manifest_missing",
    )
    try:
        cargo_data = tomllib.loads(cargo_raw.decode("utf-8"))
    except Exception as exc:
        raise PackagingContractError(
            "packaging_cargo_manifest_invalid",
            f"Invalid native/ryn_computer_use/Cargo.toml: {exc}",
        ) from exc

    cargo_pkg = cargo_data.get("package")
    if not isinstance(cargo_pkg, dict):
        raise PackagingContractError(
            "packaging_cargo_manifest_invalid",
            "Missing [package] table in native/ryn_computer_use/Cargo.toml",
        )
    cargo_name = cargo_pkg.get("name")
    cargo_version = cargo_pkg.get("version")
    if cargo_name != "ryn_computer_use":
        raise PackagingContractError(
            "packaging_worker_identity_mismatch",
            f"Cargo package name must be 'ryn_computer_use', got {cargo_name!r}",
        )
    if not isinstance(cargo_version, str) or not cargo_version.strip():
        raise PackagingContractError(
            "packaging_cargo_manifest_invalid",
            "Missing non-empty package.version in native/ryn_computer_use/Cargo.toml",
        )

    cargo_bins = cargo_data.get("bin")
    if not isinstance(cargo_bins, list) or not any(
        isinstance(b, dict)
        and b.get("name") == "ryn_computer_use"
        and b.get("path") == "src/main.rs"
        for b in cargo_bins
    ):
        raise PackagingContractError(
            "packaging_cargo_manifest_invalid",
            "Cargo.toml must declare [[bin]] name='ryn_computer_use' path='src/main.rs'",
        )

    rs_proto_raw, _ = _read_file_bytes_and_sha256(
        root / "native" / "ryn_computer_use" / "src" / "protocol.rs",
        missing_code="packaging_rust_protocol_missing",
    )
    rs_proto_text = rs_proto_raw.decode("utf-8")
    m_rs_wire = _RUST_WIRE_VERSION_RE.search(rs_proto_text)
    m_rs_semver = _RUST_PROTOCOL_SEMVER_RE.search(rs_proto_text)
    if m_rs_wire is None or m_rs_semver is None:
        raise PackagingContractError(
            "packaging_rust_protocol_invalid",
            "Unable to extract WIRE_VERSION and PROTOCOL_SEMVER from native/ryn_computer_use/src/protocol.rs",
        )
    rs_wire_version = int(m_rs_wire.group(1))
    rs_proto_semver = m_rs_semver.group(1).strip()

    rs_engine_raw, _ = _read_file_bytes_and_sha256(
        root / "native" / "ryn_computer_use" / "src" / "engine.rs",
        missing_code="packaging_rust_engine_missing",
    )
    rs_engine_text = rs_engine_raw.decode("utf-8")
    m_rs_worker_name = _RUST_ENGINE_WORKER_NAME_RE.search(rs_engine_text)
    m_rs_worker_ver = _RUST_ENGINE_WORKER_VER_MACRO_RE.search(rs_engine_text)
    if m_rs_worker_name is None or m_rs_worker_ver is None:
        raise PackagingContractError(
            "packaging_rust_engine_invalid",
            "Unable to verify WorkerInfo name and CARGO_PKG_VERSION macro in native/ryn_computer_use/src/engine.rs",
        )
    rs_worker_name = m_rs_worker_name.group(1).strip()

    py_proto_raw, _ = _read_file_bytes_and_sha256(
        root / "rynmesh" / "agent_runtime" / "computer" / "protocol.py",
        missing_code="packaging_python_protocol_missing",
    )
    py_proto_text = py_proto_raw.decode("utf-8")
    m_py_wire = _PY_WIRE_VERSION_RE.search(py_proto_text)
    m_py_semver = _PY_PROTOCOL_SEMVER_RE.search(py_proto_text)
    m_py_wname = _PY_WORKER_NAME_RE.search(py_proto_text)
    m_py_wver = _PY_WORKER_VERSION_RE.search(py_proto_text)
    if (
        m_py_wire is None
        or m_py_semver is None
        or m_py_wname is None
        or m_py_wver is None
    ):
        raise PackagingContractError(
            "packaging_python_protocol_invalid",
            "Unable to extract protocol/worker constants from rynmesh/agent_runtime/computer/protocol.py",
        )
    py_wire_version = int(m_py_wire.group(1))
    py_proto_semver = m_py_semver.group(1).strip()
    py_worker_name = m_py_wname.group(1).strip()
    py_worker_version = m_py_wver.group(1).strip()

    if rs_wire_version != py_wire_version:
        raise PackagingContractError(
            "packaging_version_mismatch",
            f"Rust WIRE_VERSION ({rs_wire_version}) != Python WIRE_VERSION ({py_wire_version})",
        )
    if rs_proto_semver != py_proto_semver:
        raise PackagingContractError(
            "packaging_version_mismatch",
            f"Rust PROTOCOL_SEMVER ({rs_proto_semver!r}) != Python PROTOCOL_SEMVER ({py_proto_semver!r})",
        )
    if rs_worker_name != py_worker_name or rs_worker_name != cargo_name:
        raise PackagingContractError(
            "packaging_worker_identity_mismatch",
            f"Worker name mismatch across Cargo ({cargo_name!r}), Rust engine ({rs_worker_name!r}), Python ({py_worker_name!r})",
        )
    if cargo_version != py_worker_version:
        raise PackagingContractError(
            "packaging_version_mismatch",
            f"Cargo package version ({cargo_version!r}) != Python EXPECTED_WORKER_VERSION ({py_worker_version!r})",
        )

    binary_mod = _import_binary_validators(root)
    if (
        getattr(binary_mod, "MANIFEST_SCHEMA_VERSION", None) != MANIFEST_SCHEMA_VERSION
        or getattr(binary_mod, "SUPPORTED_TARGET_TRIPLE", None)
        != SUPPORTED_TARGET_TRIPLE
        or getattr(binary_mod, "PACKAGED_EXECUTABLE_BASENAME", None)
        != INSTALLED_EXECUTABLE_FILENAME
    ):
        raise PackagingContractError(
            "packaging_binary_contract_mismatch",
            "binary.py schema/target/basename constants do not match packaging contract",
        )

    return RepoContractVersions(
        rynmesh_package_version=rynmesh_ver.strip(),
        worker_name=rs_worker_name,
        worker_version=cargo_version.strip(),
        protocol_version=rs_proto_semver,
        wire_version=rs_wire_version,
        target_triple=SUPPORTED_TARGET_TRIPLE,
        installed_filename=INSTALLED_EXECUTABLE_FILENAME,
    )


def validate_native_binary_build_origin(
    executable_path: str | Path,
    *,
    expected_target_dir: str | Path | None = None,
    repo_root: str | Path | None = None,
) -> Path:
    """Verify the native binary path is the isolated release build artifact (`.../x86_64-pc-windows-msvc/release/ryn_computer_use.exe`).

    Rejects debug builds, wrong basenames, symlinks, and source-checkout target directories.
    """
    exe_path = Path(executable_path)
    if exe_path.name != INSTALLED_EXECUTABLE_FILENAME:
        raise PackagingContractError(
            "packaging_native_origin_invalid",
            f"Native binary basename must be {INSTALLED_EXECUTABLE_FILENAME!r}, got {exe_path.name!r}",
        )

    norm_parts = [p.lower() for p in exe_path.parts]
    if "debug" in norm_parts:
        raise PackagingContractError(
            "packaging_native_origin_invalid",
            f"Debug native binary is forbidden for release packaging: {exe_path}",
        )
    if len(norm_parts) < 3 or norm_parts[-3:-1] != [
        SUPPORTED_TARGET_TRIPLE.lower(),
        "release",
    ]:
        raise PackagingContractError(
            "packaging_native_origin_invalid",
            f"Native binary must reside under <targetDir>/{SUPPORTED_TARGET_TRIPLE}/release/{INSTALLED_EXECUTABLE_FILENAME}: {exe_path}",
        )

    if repo_root is not None:
        source_crate_target = (
            Path(repo_root) / "native" / "ryn_computer_use" / "target"
        )
        if _is_relative_to_norm(exe_path, source_crate_target):
            raise PackagingContractError(
                "packaging_native_origin_invalid",
                f"Native binary must be built in isolated buildRoot target dir, not source crate target dir: {exe_path}",
            )

    if expected_target_dir is not None:
        expected_exe = (
            Path(expected_target_dir)
            / SUPPORTED_TARGET_TRIPLE
            / "release"
            / INSTALLED_EXECUTABLE_FILENAME
        )
        if _norm_abs(exe_path) != _norm_abs(expected_exe):
            raise PackagingContractError(
                "packaging_native_origin_invalid",
                f"Native binary path {exe_path} does not match expected isolated release output {expected_exe}",
            )

    if not exe_path.exists() or not exe_path.is_file() or exe_path.is_symlink():
        raise PackagingContractError(
            "packaging_native_binary_missing",
            f"Native release binary does not exist or is a symlink: {exe_path}",
        )
    return exe_path


def inspect_and_validate_pe64_executable_bytes(
    exe_bytes: bytes,
    *,
    repo_root: str | Path,
    label: str = "executable",
) -> dict[str, Any]:
    """Validate that `exe_bytes` is a bounded, non-DLL Windows x86_64 PE32+ executable and return its digest/metadata."""
    binary_mod = _import_binary_validators(Path(repo_root))
    if not isinstance(exe_bytes, (bytes, bytearray)):
        raise PackagingContractError(
            "packaging_pe_invalid",
            f"{label} payload must be bytes",
        )
    size = len(exe_bytes)
    if (
        size < binary_mod.MIN_BINARY_SIZE_BYTES
        or size > binary_mod.MAX_BINARY_SIZE_BYTES
    ):
        raise PackagingContractError(
            "packaging_binary_size_invalid",
            f"{label} size {size} is outside allowed bounds [{binary_mod.MIN_BINARY_SIZE_BYTES}, {binary_mod.MAX_BINARY_SIZE_BYTES}]",
        )
    try:
        pe_info = binary_mod.parse_and_validate_pe_headers(
            bytes(exe_bytes[:65536]),
            file_size=size,
            expected_machine=IMAGE_FILE_MACHINE_AMD64,
            expected_magic=IMAGE_NT_OPTIONAL_HDR64_MAGIC,
        )
    except Exception as exc:
        raise PackagingContractError(
            "packaging_pe_invalid",
            f"{label} failed PE32+ x86_64 header validation: {exc}",
        ) from exc

    if (
        not pe_info.is_executable
        or pe_info.is_dll
        or pe_info.machine != IMAGE_FILE_MACHINE_AMD64
        or pe_info.optional_header_magic != IMAGE_NT_OPTIONAL_HDR64_MAGIC
    ):
        raise PackagingContractError(
            "packaging_pe_invalid",
            f"{label} is not a non-DLL x86_64 PE32+ executable",
        )

    return {
        "size": size,
        "sha256": _sha256_bytes(bytes(exe_bytes)),
        "machine": pe_info.machine,
        "optionalHeaderMagic": pe_info.optional_header_magic,
    }


def compute_deterministic_release_id(
    *,
    worker_version: str,
    wire_version: int,
    protocol_version: str,
    native_sha256: str,
) -> str:
    """Compute a deterministic releaseId from version constants and native binary SHA-256 prefix (zero timestamps or paths)."""
    return (
        f"ryn-computer-use-v{worker_version}"
        f"-w{wire_version}"
        f"-p{protocol_version}"
        f"-{native_sha256[:16]}"
    )


def generate_manifest_from_executable_bytes(
    exe_bytes: bytes,
    *,
    versions: RepoContractVersions,
    repo_root: str | Path,
) -> tuple[dict[str, Any], bytes, str]:
    """Generate a deterministic UTF-8 `computer-use-manifest.json` from exact native executable bytes and verified repo versions.

    Returns `(manifest_dict, manifest_utf8_bytes, manifest_sha256)`.
    """
    if versions.target_triple != SUPPORTED_TARGET_TRIPLE:
        raise PackagingContractError(
            "packaging_arch_unsupported",
            f"Unsupported target triple: {versions.target_triple!r}",
        )
    if versions.installed_filename != INSTALLED_EXECUTABLE_FILENAME:
        raise PackagingContractError(
            "packaging_filename_invalid",
            f"Installed filename must be {INSTALLED_EXECUTABLE_FILENAME!r}, got {versions.installed_filename!r}",
        )

    pe_meta = inspect_and_validate_pe64_executable_bytes(
        exe_bytes,
        repo_root=repo_root,
        label=INSTALLED_EXECUTABLE_FILENAME,
    )
    release_id = compute_deterministic_release_id(
        worker_version=versions.worker_version,
        wire_version=versions.wire_version,
        protocol_version=versions.protocol_version,
        native_sha256=pe_meta["sha256"],
    )

    manifest_dict: dict[str, Any] = {
        "schemaVersion": MANIFEST_SCHEMA_VERSION,
        "releaseId": release_id,
        "wireVersion": versions.wire_version,
        "protocolVersion": versions.protocol_version,
        "workerName": versions.worker_name,
        "workerVersion": versions.worker_version,
        "targetTriple": versions.target_triple,
        "artifact": {
            "installedFilename": versions.installed_filename,
            "size": pe_meta["size"],
            "sha256": pe_meta["sha256"],
            "machine": pe_meta["machine"],
            "optionalHeaderMagic": pe_meta["optionalHeaderMagic"],
        },
    }

    manifest_text = (
        json.dumps(manifest_dict, indent=2, sort_keys=False, ensure_ascii=False) + "\n"
    )
    manifest_bytes = manifest_text.encode("utf-8")

    # Round-trip validate through binary.py's strict manifest parser
    binary_mod = _import_binary_validators(Path(repo_root))
    try:
        spec = binary_mod.parse_and_validate_manifest_bytes(
            manifest_bytes,
            expected_worker_name=versions.worker_name,
            expected_worker_version=versions.worker_version,
            expected_protocol_version=versions.protocol_version,
            source_mode="development",
        )
    except Exception as exc:
        raise PackagingContractError(
            "packaging_manifest_invalid",
            f"Generated manifest failed binary.py strict validation: {exc}",
        ) from exc

    # Public manifest parser must NEVER mint release authority
    if getattr(spec, "release_authorized", False) is not False:
        raise PackagingContractError(
            "packaging_authority_violation",
            "Public manifest validation unexpectedly set release_authorized=True",
        )

    return manifest_dict, manifest_bytes, spec.manifest_sha256


def generate_manifest_from_executable(
    executable_path: str | Path,
    *,
    repo_root: str | Path,
    expected_target_dir: str | Path | None = None,
) -> tuple[dict[str, Any], bytes, str]:
    """Validate `executable_path` origin, read its bytes once, cross-check repo versions, and generate deterministic manifest bytes."""
    exe_path = validate_native_binary_build_origin(
        executable_path,
        expected_target_dir=expected_target_dir,
        repo_root=repo_root,
    )
    exe_bytes, _ = _read_file_bytes_and_sha256(
        exe_path,
        missing_code="packaging_native_binary_missing",
    )
    versions = extract_repo_contract_versions(repo_root)
    return generate_manifest_from_executable_bytes(
        exe_bytes,
        versions=versions,
        repo_root=repo_root,
    )


def verify_manifest_matches_executable_bytes(
    manifest_bytes: bytes,
    exe_bytes: bytes,
    *,
    repo_root: str | Path,
    versions: RepoContractVersions | None = None,
) -> dict[str, Any]:
    """Strictly validate `manifest_bytes` (including duplicate/unknown keys) and verify it matches `exe_bytes` and repo versions."""
    root = Path(repo_root)
    eff_versions = (
        versions if versions is not None else extract_repo_contract_versions(root)
    )
    binary_mod = _import_binary_validators(root)
    protocol_mod = _import_protocol_validators(root)

    try:
        spec = binary_mod.parse_and_validate_manifest_bytes(
            manifest_bytes,
            expected_worker_name=eff_versions.worker_name,
            expected_worker_version=eff_versions.worker_version,
            expected_protocol_version=eff_versions.protocol_version,
            source_mode="development",
        )
        parsed_raw = protocol_mod.parse_strict_json(manifest_bytes.decode("utf-8"))
    except Exception as exc:
        raise PackagingContractError(
            "packaging_manifest_invalid",
            f"Manifest failed strict validation: {exc}",
        ) from exc

    if spec.installed_filename != INSTALLED_EXECUTABLE_FILENAME:
        raise PackagingContractError(
            "packaging_manifest_invalid",
            f"Manifest installedFilename must be {INSTALLED_EXECUTABLE_FILENAME!r}, got {spec.installed_filename!r}",
        )

    pe_meta = inspect_and_validate_pe64_executable_bytes(
        exe_bytes,
        repo_root=root,
        label=INSTALLED_EXECUTABLE_FILENAME,
    )
    if spec.expected_size != pe_meta["size"]:
        raise PackagingContractError(
            "packaging_binary_size_mismatch",
            f"Manifest size ({spec.expected_size}) != executable size ({pe_meta['size']})",
        )
    if spec.expected_sha256 != pe_meta["sha256"]:
        raise PackagingContractError(
            "packaging_binary_sha256_mismatch",
            f"Manifest sha256 ({spec.expected_sha256}) != executable sha256 ({pe_meta['sha256']})",
        )

    expected_release_id = compute_deterministic_release_id(
        worker_version=eff_versions.worker_version,
        wire_version=eff_versions.wire_version,
        protocol_version=eff_versions.protocol_version,
        native_sha256=pe_meta["sha256"],
    )
    if spec.release_id != expected_release_id:
        raise PackagingContractError(
            "packaging_release_id_mismatch",
            f"Manifest releaseId ({spec.release_id!r}) != deterministic expected releaseId ({expected_release_id!r})",
        )

    assert isinstance(parsed_raw, dict)
    return parsed_raw


def verify_and_resolve_venv_rynmesh_package_dir(
    *,
    venv_root: str | Path,
    build_root: str | Path,
    repo_root: str | Path,
    imported_rynmesh_init: str | Path | None = None,
) -> Path:
    """Verify that the target `rynmesh` package directory is inside `venv_root` (under `build_root`) and never inside `repo_root/rynmesh`."""
    venv_p = Path(venv_root)
    build_p = Path(build_root)
    repo_p = Path(repo_root)

    if not _is_relative_to_norm(venv_p, build_p):
        raise PackagingContractError(
            "packaging_venv_location_invalid",
            f"venv_root ({venv_p}) must be located inside build_root ({build_p})",
        )

    if imported_rynmesh_init is None:
        python_exe = venv_p / "Scripts" / "python.exe"
        if not python_exe.exists():
            raise PackagingContractError(
                "packaging_venv_python_missing",
                f"Isolated venv python executable not found: {python_exe}",
            )
        proc = subprocess.run(
            [
                str(python_exe),
                "-I",
                "-c",
                "import pathlib, rynmesh; print(pathlib.Path(rynmesh.__file__).resolve())",
            ],
            cwd=str(build_p),
            capture_output=True,
            text=True,
            check=False,
        )
        if proc.returncode != 0:
            raise PackagingContractError(
                "packaging_venv_import_failed",
                f"Failed to import rynmesh from isolated venv: {proc.stderr.strip()}",
            )
        lines = [ln.strip() for ln in proc.stdout.splitlines() if ln.strip()]
        if len(lines) != 1:
            raise PackagingContractError(
                "packaging_venv_import_failed",
                f"Unexpected output resolving rynmesh.__file__: {proc.stdout!r}",
            )
        init_path = Path(lines[0])
    else:
        init_path = Path(imported_rynmesh_init)

    if init_path.name != "__init__.py" or init_path.parent.name != "rynmesh":
        raise PackagingContractError(
            "packaging_venv_location_invalid",
            f"Resolved path is not rynmesh/__init__.py: {init_path}",
        )

    if not _is_relative_to_norm(init_path, venv_p):
        raise PackagingContractError(
            "packaging_venv_shadowed",
            f"Imported rynmesh ({init_path}) is not inside isolated build venv ({venv_p})",
        )

    repo_rynmesh_dir = repo_p / "rynmesh"
    if _is_relative_to_norm(init_path, repo_rynmesh_dir):
        raise PackagingContractError(
            "packaging_venv_shadowed",
            f"Imported rynmesh ({init_path}) resolved to source checkout ({repo_rynmesh_dir})",
        )

    return init_path.parent


def stage_manifest_into_venv(
    *,
    manifest_bytes: bytes,
    venv_root: str | Path,
    build_root: str | Path,
    repo_root: str | Path,
    staging_manifest_copy: str | Path | None = None,
    imported_rynmesh_init: str | Path | None = None,
) -> Path:
    """Write `manifest_bytes` into the verified isolated venv `rynmesh/bin/computer-use-manifest.json` and ensure source checkout has no conflicting manifest."""
    repo_p = Path(repo_root)
    source_manifest = repo_p / "rynmesh" / "bin" / MANIFEST_FILENAME
    if source_manifest.exists():
        raise PackagingContractError(
            "packaging_source_manifest_collision",
            f"Source checkout must not contain {source_manifest}; manifest must only be staged in isolated build venv",
        )

    pkg_dir = verify_and_resolve_venv_rynmesh_package_dir(
        venv_root=venv_root,
        build_root=build_root,
        repo_root=repo_root,
        imported_rynmesh_init=imported_rynmesh_init,
    )
    bin_dir = pkg_dir / "bin"
    bin_dir.mkdir(parents=True, exist_ok=True)
    target_manifest = bin_dir / MANIFEST_FILENAME
    target_manifest.write_bytes(manifest_bytes)

    read_back, read_sha = _read_file_bytes_and_sha256(
        target_manifest,
        missing_code="packaging_manifest_write_failed",
    )
    if read_back != manifest_bytes or read_sha != _sha256_bytes(manifest_bytes):
        raise PackagingContractError(
            "packaging_manifest_write_failed",
            f"Staged venv manifest read-back mismatch at {target_manifest}",
        )

    if staging_manifest_copy is not None:
        copy_path = Path(staging_manifest_copy)
        copy_path.parent.mkdir(parents=True, exist_ok=True)
        copy_path.write_bytes(manifest_bytes)

    return target_manifest


def read_pyinstaller_carchive_entries_from_bytes(
    peer_bytes: bytes,
) -> list[PyInstallerArchiveEntry]:
    """Read and parse the PyInstaller `CArchive` (`MEI\\x0c\\x0b\\x0a\\x0b\\x0e`) directly from onefile PE bytes.

    Bounded, read-only binary parser that never executes the target binary.
    """
    file_len = len(peer_bytes)
    if file_len < PYINSTALLER_COOKIE_SIZE:
        raise PackagingContractError(
            "packaging_peer_archive_invalid",
            f"Peer binary size ({file_len}) is smaller than PyInstaller CArchive cookie ({PYINSTALLER_COOKIE_SIZE})",
        )

    search_start = max(0, file_len - MAX_CARCHIVE_SEARCH_WINDOW_BYTES)
    search_buf = peer_bytes[search_start:]

    cookie_pos = -1
    idx = len(search_buf) - PYINSTALLER_COOKIE_SIZE
    while idx >= 0:
        found = search_buf.rfind(PYINSTALLER_COOKIE_MAGIC, 0, idx + len(PYINSTALLER_COOKIE_MAGIC))
        if found < 0:
            break
        abs_pos = search_start + found
        if abs_pos + PYINSTALLER_COOKIE_SIZE <= file_len:
            magic, pkg_len, toc_off, toc_len, _pyvers, _pylib = struct.unpack(
                PYINSTALLER_COOKIE_FORMAT,
                peer_bytes[abs_pos : abs_pos + PYINSTALLER_COOKIE_SIZE],
            )
            cookie_end = abs_pos + PYINSTALLER_COOKIE_SIZE
            archive_start = cookie_end - pkg_len
            if (
                magic == PYINSTALLER_COOKIE_MAGIC
                and pkg_len >= PYINSTALLER_COOKIE_SIZE
                and archive_start >= 0
                and toc_len >= 0
                and toc_len <= MAX_CARCHIVE_TOC_BYTES
                and toc_off + toc_len <= pkg_len - PYINSTALLER_COOKIE_SIZE
            ):
                cookie_pos = abs_pos
                break
        idx = found - 1

    if cookie_pos < 0:
        raise PackagingContractError(
            "packaging_peer_archive_invalid",
            "Peer binary does not contain a valid PyInstaller CArchive cookie (MEI\\x0c\\x0b\\x0a\\x0b\\x0e)",
        )

    _magic, pkg_len, toc_off, toc_len, _pyvers, _pylib = struct.unpack(
        PYINSTALLER_COOKIE_FORMAT,
        peer_bytes[cookie_pos : cookie_pos + PYINSTALLER_COOKIE_SIZE],
    )
    cookie_end = cookie_pos + PYINSTALLER_COOKIE_SIZE
    archive_start = cookie_end - pkg_len
    toc_abs_start = archive_start + toc_off
    toc_abs_end = toc_abs_start + toc_len
    if toc_abs_start < archive_start or toc_abs_end > cookie_pos:
        raise PackagingContractError(
            "packaging_peer_archive_invalid",
            f"PyInstaller CArchive TOC bounds [{toc_abs_start}, {toc_abs_end}] outside archive [{archive_start}, {cookie_pos}]",
        )

    toc_buf = peer_bytes[toc_abs_start:toc_abs_end]
    pos = 0
    entries: list[PyInstallerArchiveEntry] = []
    while pos < len(toc_buf):
        if len(entries) >= MAX_CARCHIVE_TOC_ENTRIES:
            raise PackagingContractError(
                "packaging_peer_archive_invalid",
                f"PyInstaller CArchive TOC exceeds {MAX_CARCHIVE_TOC_ENTRIES} entries",
            )
        if pos + PYINSTALLER_TOC_ENTRY_HEADER_SIZE > len(toc_buf):
            raise PackagingContractError(
                "packaging_peer_archive_invalid",
                "Truncated PyInstaller CArchive TOC entry header",
            )
        (
            entry_size,
            data_offset,
            compressed_size,
            uncompressed_size,
            compression_flag,
            typecode_byte,
        ) = struct.unpack(
            PYINSTALLER_TOC_ENTRY_FORMAT,
            toc_buf[pos : pos + PYINSTALLER_TOC_ENTRY_HEADER_SIZE],
        )
        if entry_size < PYINSTALLER_TOC_ENTRY_HEADER_SIZE or pos + entry_size > len(toc_buf):
            raise PackagingContractError(
                "packaging_peer_archive_invalid",
                f"Invalid PyInstaller CArchive TOC entry_size={entry_size} at offset {pos}",
            )
        raw_name = toc_buf[pos + PYINSTALLER_TOC_ENTRY_HEADER_SIZE : pos + entry_size].rstrip(b"\x00")
        try:
            name = raw_name.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise PackagingContractError(
                "packaging_peer_archive_invalid",
                f"Invalid UTF-8 entry name in PyInstaller CArchive TOC: {exc}",
            ) from exc

        data_abs_start = archive_start + data_offset
        data_abs_end = data_abs_start + compressed_size
        if data_abs_start < archive_start or data_abs_end > toc_abs_start:
            raise PackagingContractError(
                "packaging_peer_archive_invalid",
                f"PyInstaller CArchive entry {name!r} data bounds [{data_abs_start}, {data_abs_end}] outside [{archive_start}, {toc_abs_start}]",
            )
        entries.append(
            PyInstallerArchiveEntry(
                name=name,
                data_offset=data_offset,
                compressed_size=compressed_size,
                uncompressed_size=uncompressed_size,
                compression_flag=compression_flag,
                typecode=chr(typecode_byte),
                raw_bytes=peer_bytes[data_abs_start:data_abs_end],
            )
        )
        pos += entry_size

    return entries


def _decompress_manifest_archive_entry(
    entry: PyInstallerArchiveEntry,
    *,
    decompressobj_factory: Callable[[], Any] | None = None,
) -> bytes:
    """Decompress a single manifest entry from a PyInstaller CArchive with strict size bounds.

    Uses a single bounded `decompress(..., MAX_MANIFEST_ARCHIVE_ENTRY_BYTES + 1)` call and never
    calls unbounded `flush()`. Immediately rejects any excess output, `unconsumed_tail`,
    incomplete stream (`not eof`), trailing `unused_data`, or `uncompressed_size` mismatch.
    """
    if (
        entry.uncompressed_size < 0
        or entry.compressed_size < 0
        or entry.uncompressed_size > MAX_MANIFEST_ARCHIVE_ENTRY_BYTES
        or entry.compressed_size > MAX_MANIFEST_ARCHIVE_ENTRY_BYTES
    ):
        raise PackagingContractError(
            "packaging_peer_archive_invalid",
            f"Embedded manifest entry {entry.name!r} exceeds maximum allowed size ({MAX_MANIFEST_ARCHIVE_ENTRY_BYTES} bytes)",
        )
    if entry.compression_flag == 1:
        try:
            factory = zlib.decompressobj if decompressobj_factory is None else decompressobj_factory
            decomp = factory()
            out = decomp.decompress(
                entry.raw_bytes,
                MAX_MANIFEST_ARCHIVE_ENTRY_BYTES + 1,
            )
            if decomp.unconsumed_tail or len(out) > MAX_MANIFEST_ARCHIVE_ENTRY_BYTES:
                raise PackagingContractError(
                    "packaging_peer_archive_invalid",
                    f"Decompressed manifest entry {entry.name!r} exceeded size cap",
                )
            if not decomp.eof:
                raise PackagingContractError(
                    "packaging_peer_archive_invalid",
                    f"Truncated or incomplete zlib stream for embedded manifest entry {entry.name!r}",
                )
            if decomp.unused_data:
                raise PackagingContractError(
                    "packaging_peer_archive_invalid",
                    f"Trailing unused data after zlib stream in embedded manifest entry {entry.name!r}",
                )
        except PackagingContractError:
            raise
        except Exception as exc:
            raise PackagingContractError(
                "packaging_peer_archive_invalid",
                f"Failed to decompress embedded manifest entry {entry.name!r}: {exc}",
            ) from exc
    elif entry.compression_flag == 0:
        out = bytes(entry.raw_bytes)
    else:
        raise PackagingContractError(
            "packaging_peer_archive_invalid",
            f"Unsupported PyInstaller CArchive compression_flag={entry.compression_flag} for {entry.name!r}",
        )

    if len(out) != entry.uncompressed_size:
        raise PackagingContractError(
            "packaging_peer_archive_invalid",
            f"Embedded manifest entry {entry.name!r} uncompressed length ({len(out)}) != TOC uncompressed_size ({entry.uncompressed_size})",
        )
    return out


def _cross_check_with_installed_pyinstaller_reader(
    peer_exe_path: Path,
    expected_entry_name: str,
    expected_extracted_bytes: bytes,
) -> None:
    """When `PyInstaller.archive.readers.CArchiveReader` is installed in the current Python environment, cross-verify extraction."""
    try:
        from PyInstaller.archive.readers import CArchiveReader  # type: ignore[import-not-found]
    except Exception:
        return

    try:
        reader = CArchiveReader(str(peer_exe_path))
    except Exception as exc:
        raise PackagingContractError(
            "packaging_peer_archive_invalid",
            f"PyInstaller CArchiveReader rejected {peer_exe_path}: {exc}",
        ) from exc

    matched_keys: list[str] = []
    for key in reader.toc.keys():
        norm = str(key).replace("\\", "/").strip("/")
        norm_lower = norm.lower()
        if (
            norm_lower == MANIFEST_PACKAGE_REL_POSIX
            or norm_lower.endswith("/" + MANIFEST_FILENAME)
            or norm_lower == MANIFEST_FILENAME
        ):
            matched_keys.append(str(key))

    if len(matched_keys) == 0:
        raise PackagingContractError(
            "packaging_archive_manifest_missing",
            f"PyInstaller CArchiveReader found 0 manifest entries in {peer_exe_path}",
        )
    if len(matched_keys) > 1:
        raise PackagingContractError(
            "packaging_archive_manifest_duplicate",
            f"PyInstaller CArchiveReader found {len(matched_keys)} manifest entries in {peer_exe_path}: {matched_keys}",
        )
    raw_extracted = reader.extract(matched_keys[0])
    if raw_extracted != expected_extracted_bytes or matched_keys[0] != expected_entry_name:
        raise PackagingContractError(
            "packaging_archive_manifest_sha256_mismatch",
            f"PyInstaller CArchiveReader extraction mismatch for {peer_exe_path}",
        )


def inspect_peer_embedded_manifest_bytes(
    peer_bytes: bytes,
    *,
    expected_manifest_bytes: bytes,
    peer_exe_path: str | Path | None = None,
    archive_reader: Callable[[bytes], Sequence[PyInstallerArchiveEntry]] | None = None,
) -> dict[str, Any]:
    """Inspect the final frozen peer binary's PyInstaller CArchive and verify it embeds single authoritative `rynmesh/bin/computer-use-manifest.json` matching `expected_manifest_bytes`.

    Fails closed when the peer binary lacks a CArchive, has 0 or >1 manifest entries, uses an invalid
    package-relative destination path, or embeds stale/mismatched manifest bytes.
    """
    reader_fn = (
        read_pyinstaller_carchive_entries_from_bytes
        if archive_reader is None
        else archive_reader
    )
    entries = list(reader_fn(peer_bytes))

    matching: list[ tuple[str, PyInstallerArchiveEntry] ] = []
    for entry in entries:
        dest_norm = entry.name.replace("\\", "/").strip("/")
        dest_lower = dest_norm.lower()
        if (
            dest_lower == MANIFEST_PACKAGE_REL_POSIX
            or dest_lower.endswith("/" + MANIFEST_FILENAME)
            or dest_lower == MANIFEST_FILENAME
        ):
            matching.append((dest_norm, entry))

    if len(matching) == 0:
        raise PackagingContractError(
            "packaging_archive_manifest_missing",
            f"Final peer CArchive contains 0 entries for {MANIFEST_PACKAGE_REL_POSIX}",
        )
    if len(matching) > 1:
        raise PackagingContractError(
            "packaging_archive_manifest_duplicate",
            f"Final peer CArchive contains {len(matching)} manifest entries (expected 1): {[m[0] for m in matching]}",
        )

    dest_norm, matched_entry = matching[0]
    if dest_norm.lower() != MANIFEST_PACKAGE_REL_POSIX:
        raise PackagingContractError(
            "packaging_archive_manifest_dest_invalid",
            f"Embedded manifest destination must be {MANIFEST_PACKAGE_REL_POSIX!r}, got {dest_norm!r}",
        )

    extracted_bytes = _decompress_manifest_archive_entry(matched_entry)
    extracted_sha = _sha256_bytes(extracted_bytes)
    expected_sha = _sha256_bytes(expected_manifest_bytes)
    if extracted_bytes != expected_manifest_bytes or extracted_sha != expected_sha:
        raise PackagingContractError(
            "packaging_archive_manifest_sha256_mismatch",
            f"Embedded peer manifest sha256 ({extracted_sha}) != expected manifest sha256 ({expected_sha})",
        )

    if peer_exe_path is not None and archive_reader is None:
        _cross_check_with_installed_pyinstaller_reader(
            Path(peer_exe_path),
            matched_entry.name,
            extracted_bytes,
        )

    return {
        "packageRelativePath": MANIFEST_PACKAGE_REL_POSIX,
        "sha256": extracted_sha,
        "size": len(extracted_bytes),
        "embeddedArchiveCount": 1,
        "boundPeerSha256": _sha256_bytes(peer_bytes),
        "boundPeerSize": len(peer_bytes),
    }


def verify_pyinstaller_manifest_entries(
    entries: Sequence[tuple[str, str]],
    *,
    venv_root: str | Path,
    repo_root: str | Path,
    expected_manifest_sha256: str,
) -> Path:
    """Supplementary provenance check: verify from work-dir TOC `(dest_rel_path, source_abs_path)` entries that PyInstaller sourced `rynmesh/bin/computer-use-manifest.json` from `venv_root` and never `repo_root/rynmesh`."""
    matching: list[tuple[str, Path]] = []
    for dest_name, src_path_str in entries:
        dest_norm = dest_name.replace("\\", "/").strip("/")
        dest_lower = dest_norm.lower()
        if (
            dest_lower == MANIFEST_PACKAGE_REL_POSIX
            or dest_lower.endswith("/" + MANIFEST_FILENAME)
            or dest_lower == MANIFEST_FILENAME
        ):
            matching.append((dest_norm, Path(src_path_str)))

    if len(matching) == 0:
        raise PackagingContractError(
            "packaging_archive_manifest_missing",
            f"PyInstaller TOC collected 0 entries for {MANIFEST_PACKAGE_REL_POSIX}",
        )
    if len(matching) > 1:
        raise PackagingContractError(
            "packaging_archive_manifest_duplicate",
            f"PyInstaller TOC collected {len(matching)} manifest entries (expected exactly 1): {[m[0] for m in matching]}",
        )

    dest_norm, src_path = matching[0]
    if dest_norm.lower() != MANIFEST_PACKAGE_REL_POSIX:
        raise PackagingContractError(
            "packaging_archive_manifest_dest_invalid",
            f"Manifest archive destination must be {MANIFEST_PACKAGE_REL_POSIX!r}, got {dest_norm!r}",
        )

    if not _is_relative_to_norm(src_path, venv_root):
        raise PackagingContractError(
            "packaging_archive_manifest_source_invalid",
            f"Archived manifest source ({src_path}) did not come from isolated venv ({venv_root})",
        )
    if _is_relative_to_norm(src_path, Path(repo_root) / "rynmesh"):
        raise PackagingContractError(
            "packaging_archive_manifest_source_invalid",
            f"Archived manifest source ({src_path}) came from repo source tree instead of isolated venv",
        )

    _, src_sha = _read_file_bytes_and_sha256(
        src_path,
        missing_code="packaging_archive_manifest_missing",
    )
    if src_sha != expected_manifest_sha256:
        raise PackagingContractError(
            "packaging_archive_manifest_sha256_mismatch",
            f"Archived manifest source sha256 ({src_sha}) != expected manifest sha256 ({expected_manifest_sha256})",
        )
    return src_path


def extract_toc_data_entries_from_pyinstaller_work(
    work_dir: str | Path,
) -> list[tuple[str, str]]:
    """Extract `(dest_name, src_path)` tuples for `computer-use-manifest.json` from PyInstaller's `PKG-00.toc` or `Analysis-00.toc` in `work_dir`."""
    work_p = Path(work_dir)
    peer_work = (
        work_p / "rynmesh-peer" if (work_p / "rynmesh-peer").is_dir() else work_p
    )
    toc_candidates = [
        peer_work / "PKG-00.toc",
        peer_work / "Analysis-00.toc",
    ]
    toc_file = next((p for p in toc_candidates if p.is_file()), None)
    if toc_file is None:
        raise PackagingContractError(
            "packaging_pyinstaller_toc_missing",
            f"PyInstaller TOC file (PKG-00.toc / Analysis-00.toc) not found under {peer_work}",
        )

    text = toc_file.read_text(encoding="utf-8", errors="replace")
    tuple_re = re.compile(
        r"\(\s*(['\"])(?P<dest>[^'\"]*computer-use-manifest\.json[^'\"]*)\1\s*,\s*(['\"])(?P<src>[^'\"]+)\3\s*,\s*['\"]DATA['\"]\s*\)",
        re.IGNORECASE,
    )
    entries: list[tuple[str, str]] = []
    for match in tuple_re.finditer(text):
        dest_raw = match.group("dest").encode("utf-8").decode("unicode_escape")
        src_raw = match.group("src").encode("utf-8").decode("unicode_escape")
        entries.append((dest_raw, src_raw))
    return entries


def validate_tauri_windows_config(
    tauri_windows_conf_path: str | Path,
    *,
    repo_root: str | Path,
) -> tuple[dict[str, Any], str]:
    """Validate `webapp/src-tauri/tauri.windows.conf.json` preserves `binaries/rynmesh-peer` and adds `binaries/ryn_computer_use` without mounting unapproved resources."""
    conf_p = Path(tauri_windows_conf_path)
    raw, sha = _read_file_bytes_and_sha256(
        conf_p,
        missing_code="packaging_tauri_config_missing",
    )
    protocol_mod = _import_protocol_validators(Path(repo_root))
    try:
        parsed = protocol_mod.parse_strict_json(raw.decode("utf-8"))
    except Exception as exc:
        raise PackagingContractError(
            "packaging_tauri_config_invalid",
            f"Invalid JSON in {conf_p}: {exc}",
        ) from exc

    if not isinstance(parsed, dict):
        raise PackagingContractError(
            "packaging_tauri_config_invalid",
            f"{conf_p} root must be a JSON object",
        )
    bundle = parsed.get("bundle")
    if not isinstance(bundle, dict):
        raise PackagingContractError(
            "packaging_tauri_config_invalid",
            f"{conf_p} must define a 'bundle' object",
        )
    external_bin = bundle.get("externalBin")
    if (
        not isinstance(external_bin, list)
        or tuple(external_bin) != EXPECTED_TAURI_EXTERNAL_BINS
    ):
        raise PackagingContractError(
            "packaging_tauri_external_bin_invalid",
            f"{conf_p} bundle.externalBin must be {list(EXPECTED_TAURI_EXTERNAL_BINS)!r}, got {external_bin!r}",
        )
    resources = bundle.get("resources")
    if resources != []:
        raise PackagingContractError(
            "packaging_tauri_resources_invalid",
            f"{conf_p} bundle.resources must remain [] in R2, got {resources!r}",
        )
    windows_cfg = bundle.get("windows")
    if not isinstance(windows_cfg, dict) or not isinstance(
        windows_cfg.get("nsis"), dict
    ):
        raise PackagingContractError(
            "packaging_tauri_config_invalid",
            f"{conf_p} bundle.windows.nsis configuration missing",
        )
    nsis_cfg = windows_cfg["nsis"]
    if nsis_cfg.get("installMode") != "currentUser":
        raise PackagingContractError(
            "packaging_tauri_config_invalid",
            f"{conf_p} bundle.windows.nsis.installMode must remain 'currentUser'",
        )

    return parsed, sha


def collect_reviewed_source_files(repo_root: str | Path) -> list[Path]:
    """Collect the deterministic set of reviewed Rust, Python, Tauri, and packaging source files."""
    root = Path(repo_root)
    rel_paths: set[str] = set()

    for rel in REVIEWED_EXPLICIT_RELATIVE_FILES:
        p = root / rel
        if not p.exists() or not p.is_file() or p.is_symlink():
            raise PackagingContractError(
                "packaging_reviewed_source_missing",
                f"Required reviewed source file missing or symlink: {p}",
            )
        rel_paths.add(rel.replace("\\", "/"))

    for rel in REVIEWED_OPTIONAL_RELATIVE_FILES:
        p = root / rel
        if p.exists():
            if not p.is_file() or p.is_symlink():
                raise PackagingContractError(
                    "packaging_reviewed_source_missing",
                    f"Reviewed optional file is not a regular file: {p}",
                )
            rel_paths.add(rel.replace("\\", "/"))

    rust_src_dir = root / "native" / "ryn_computer_use" / "src"
    if not rust_src_dir.is_dir():
        raise PackagingContractError(
            "packaging_reviewed_source_missing",
            f"Rust source directory missing: {rust_src_dir}",
        )
    for rs_file in sorted(rust_src_dir.rglob("*.rs")):
        if rs_file.is_symlink() or not rs_file.is_file():
            raise PackagingContractError(
                "packaging_reviewed_source_missing",
                f"Invalid Rust source file: {rs_file}",
            )
        rel_paths.add(rs_file.relative_to(root).as_posix())

    py_comp_dir = root / "rynmesh" / "agent_runtime" / "computer"
    if not py_comp_dir.is_dir():
        raise PackagingContractError(
            "packaging_reviewed_source_missing",
            f"Python computer bridge directory missing: {py_comp_dir}",
        )
    for py_file in sorted(py_comp_dir.rglob("*.py")):
        if py_file.is_symlink() or not py_file.is_file():
            raise PackagingContractError(
                "packaging_reviewed_source_missing",
                f"Invalid Python computer bridge source file: {py_file}",
            )
        rel_paths.add(py_file.relative_to(root).as_posix())

    return [root / r for r in sorted(rel_paths)]


def compute_reviewed_source_commitment(repo_root: str | Path) -> dict[str, Any]:
    """Compute a deterministic SHA-256 manifest of all reviewed Rust, Python, Tauri, and packaging source files."""
    root = Path(repo_root)
    files_list: list[dict[str, Any]] = []
    for abs_path in collect_reviewed_source_files(root):
        raw, sha = _read_file_bytes_and_sha256(
            abs_path,
            missing_code="packaging_reviewed_source_missing",
        )
        files_list.append(
            {
                "path": abs_path.relative_to(root).as_posix(),
                "sha256": sha,
                "size": len(raw),
            }
        )
    canonical = json.dumps(files_list, sort_keys=True, separators=(",", ":")).encode(
        "utf-8"
    )
    return {
        "treeSha256": _sha256_bytes(canonical),
        "fileCount": len(files_list),
        "files": files_list,
    }


def create_build_receipt(
    *,
    repo_root: str | Path,
    manifest_bytes: bytes,
    native_release_exe: str | Path,
    staged_native_exe: str | Path,
    dist_peer_exe: str | Path,
    staged_peer_exe: str | Path,
    tauri_windows_conf_path: str | Path,
    expected_target_dir: str | Path | None = None,
    archive_manifest_entries: Sequence[tuple[str, str]] | None = None,
    venv_root: str | Path | None = None,
    archive_reader: Callable[[bytes], Sequence[PyInstallerArchiveEntry]] | None = None,
) -> tuple[dict[str, Any], bytes]:
    """Validate the complete staged native + peer + embedded CArchive manifest + Tauri config + source set and build `computer-release-receipt.json`."""
    root = Path(repo_root)
    versions = extract_repo_contract_versions(root)

    validate_native_binary_build_origin(
        native_release_exe,
        expected_target_dir=expected_target_dir,
        repo_root=root,
    )
    native_rel_bytes, _ = _read_file_bytes_and_sha256(
        Path(native_release_exe),
        missing_code="packaging_native_binary_missing",
    )
    staged_nat_p = Path(staged_native_exe)
    if staged_nat_p.name != TAURI_STAGED_NATIVE_FILENAME:
        raise PackagingContractError(
            "packaging_staged_native_invalid",
            f"Staged Tauri native binary must be named {TAURI_STAGED_NATIVE_FILENAME!r}, got {staged_nat_p.name!r}",
        )
    staged_nat_bytes, _ = _read_file_bytes_and_sha256(
        staged_nat_p,
        missing_code="packaging_staged_native_missing",
    )
    if native_rel_bytes != staged_nat_bytes:
        raise PackagingContractError(
            "packaging_staged_native_mismatch",
            "Staged Tauri native binary bytes do not match release native binary bytes",
        )
    native_pe = inspect_and_validate_pe64_executable_bytes(
        native_rel_bytes,
        repo_root=root,
        label=INSTALLED_EXECUTABLE_FILENAME,
    )

    manifest_dict = verify_manifest_matches_executable_bytes(
        manifest_bytes,
        native_rel_bytes,
        repo_root=root,
        versions=versions,
    )
    manifest_sha = _sha256_bytes(manifest_bytes)

    if archive_manifest_entries is not None:
        if venv_root is None:
            raise PackagingContractError(
                "packaging_venv_location_invalid",
                "venv_root is required when verifying archive_manifest_entries",
            )
        verify_pyinstaller_manifest_entries(
            archive_manifest_entries,
            venv_root=venv_root,
            repo_root=root,
            expected_manifest_sha256=manifest_sha,
        )

    dist_peer_p = Path(dist_peer_exe)
    dist_peer_bytes, _ = _read_file_bytes_and_sha256(
        dist_peer_p,
        missing_code="packaging_peer_binary_missing",
    )
    staged_peer_p = Path(staged_peer_exe)
    if staged_peer_p.name != TAURI_STAGED_PEER_FILENAME:
        raise PackagingContractError(
            "packaging_staged_peer_invalid",
            f"Staged Tauri peer binary must be named {TAURI_STAGED_PEER_FILENAME!r}, got {staged_peer_p.name!r}",
        )
    staged_peer_bytes, _ = _read_file_bytes_and_sha256(
        staged_peer_p,
        missing_code="packaging_staged_peer_missing",
    )
    if dist_peer_bytes != staged_peer_bytes:
        raise PackagingContractError(
            "packaging_staged_peer_mismatch",
            "Staged Tauri peer binary bytes do not match dist/rynmesh-peer.exe bytes",
        )
    peer_pe = inspect_and_validate_pe64_executable_bytes(
        staged_peer_bytes,
        repo_root=root,
        label=TAURI_STAGED_PEER_FILENAME,
    )

    # Mandatory real final-peer CArchive manifest extraction and binding
    archive_evidence = inspect_peer_embedded_manifest_bytes(
        staged_peer_bytes,
        expected_manifest_bytes=manifest_bytes,
        peer_exe_path=staged_peer_p,
        archive_reader=archive_reader,
    )

    _, tauri_conf_sha = validate_tauri_windows_config(
        tauri_windows_conf_path,
        repo_root=root,
    )
    source_commitment = compute_reviewed_source_commitment(root)

    receipt_dict: dict[str, Any] = {
        "schemaVersion": RECEIPT_SCHEMA_VERSION,
        "releaseId": manifest_dict["releaseId"],
        "targetTriple": SUPPORTED_TARGET_TRIPLE,
        "versions": {
            "rynmeshPackageVersion": versions.rynmesh_package_version,
            "workerName": versions.worker_name,
            "workerVersion": versions.worker_version,
            "protocolVersion": versions.protocol_version,
            "wireVersion": versions.wire_version,
        },
        "manifest": {
            "packageRelativePath": MANIFEST_PACKAGE_REL_POSIX,
            "sha256": manifest_sha,
            "size": len(manifest_bytes),
            "embeddedArchiveCount": archive_evidence["embeddedArchiveCount"],
            "boundPeerSha256": archive_evidence["boundPeerSha256"],
            "boundPeerSize": archive_evidence["boundPeerSize"],
        },
        "nativeExecutable": {
            "installedFilename": INSTALLED_EXECUTABLE_FILENAME,
            "stagedFilename": TAURI_STAGED_NATIVE_FILENAME,
            "size": native_pe["size"],
            "sha256": native_pe["sha256"],
            "machine": native_pe["machine"],
            "optionalHeaderMagic": native_pe["optionalHeaderMagic"],
        },
        "peerExecutable": {
            "stagedFilename": TAURI_STAGED_PEER_FILENAME,
            "size": peer_pe["size"],
            "sha256": peer_pe["sha256"],
            "machine": peer_pe["machine"],
            "optionalHeaderMagic": peer_pe["optionalHeaderMagic"],
        },
        "tauriWindowsConfig": {
            "externalBin": list(EXPECTED_TAURI_EXTERNAL_BINS),
            "sha256": tauri_conf_sha,
        },
        "sourceCommitment": source_commitment,
    }
    receipt_bytes = (
        json.dumps(receipt_dict, indent=2, sort_keys=False, ensure_ascii=False) + "\n"
    ).encode("utf-8")
    return receipt_dict, receipt_bytes


def verify_build_receipt_and_pair(
    *,
    receipt_bytes: bytes,
    manifest_bytes: bytes,
    repo_root: str | Path,
    native_release_exe: str | Path,
    staged_native_exe: str | Path,
    dist_peer_exe: str | Path,
    staged_peer_exe: str | Path,
    tauri_windows_conf_path: str | Path,
    expected_target_dir: str | Path | None = None,
    archive_manifest_entries: Sequence[tuple[str, str]] | None = None,
    venv_root: str | Path | None = None,
    archive_reader: Callable[[bytes], Sequence[PyInstallerArchiveEntry]] | None = None,
) -> dict[str, Any]:
    """Validate an existing `computer-release-receipt.json` against current binaries, embedded CArchive manifest, Tauri config, and reviewed sources (`-SkipSidecar` gate)."""
    root = Path(repo_root)
    protocol_mod = _import_protocol_validators(root)
    try:
        receipt = protocol_mod.parse_strict_json(receipt_bytes.decode("utf-8"))
    except Exception as exc:
        raise PackagingContractError(
            "packaging_receipt_invalid",
            f"Invalid receipt JSON: {exc}",
        ) from exc

    if not isinstance(receipt, dict):
        raise PackagingContractError(
            "packaging_receipt_invalid",
            "Receipt root must be a JSON object",
        )
    required_keys = {
        "schemaVersion",
        "releaseId",
        "targetTriple",
        "versions",
        "manifest",
        "nativeExecutable",
        "peerExecutable",
        "tauriWindowsConfig",
        "sourceCommitment",
    }
    if set(receipt.keys()) != required_keys:
        raise PackagingContractError(
            "packaging_receipt_invalid",
            f"Receipt keys mismatch: got {sorted(receipt.keys())}",
        )
    if (
        type(receipt["schemaVersion"]) is not int
        or receipt["schemaVersion"] != RECEIPT_SCHEMA_VERSION
    ):
        raise PackagingContractError(
            "packaging_receipt_invalid",
            f"Unsupported receipt schemaVersion: {receipt['schemaVersion']!r}",
        )
    if receipt["targetTriple"] != SUPPORTED_TARGET_TRIPLE:
        raise PackagingContractError(
            "packaging_receipt_target_mismatch",
            f"Receipt targetTriple ({receipt['targetTriple']!r}) != {SUPPORTED_TARGET_TRIPLE!r}",
        )

    expected_receipt, _ = create_build_receipt(
        repo_root=root,
        manifest_bytes=manifest_bytes,
        native_release_exe=native_release_exe,
        staged_native_exe=staged_native_exe,
        dist_peer_exe=dist_peer_exe,
        staged_peer_exe=staged_peer_exe,
        tauri_windows_conf_path=tauri_windows_conf_path,
        expected_target_dir=expected_target_dir,
        archive_manifest_entries=archive_manifest_entries,
        venv_root=venv_root,
        archive_reader=archive_reader,
    )

    if receipt["versions"] != expected_receipt["versions"]:
        raise PackagingContractError(
            "packaging_receipt_version_stale",
            "Receipt versions do not match current repository contract versions",
        )
    if receipt["sourceCommitment"] != expected_receipt["sourceCommitment"]:
        raise PackagingContractError(
            "packaging_receipt_source_stale",
            "Reviewed Rust/Python/Tauri/build source files have changed since the sidecar build receipt was recorded",
        )
    if receipt["tauriWindowsConfig"] != expected_receipt["tauriWindowsConfig"]:
        raise PackagingContractError(
            "packaging_receipt_tauri_config_stale",
            "tauri.windows.conf.json has changed since the sidecar build receipt was recorded",
        )
    if receipt["nativeExecutable"] != expected_receipt["nativeExecutable"]:
        raise PackagingContractError(
            "packaging_receipt_native_mismatch",
            "Native computer-use binary does not match computer-release-receipt.json",
        )
    if receipt["peerExecutable"] != expected_receipt["peerExecutable"]:
        raise PackagingContractError(
            "packaging_receipt_peer_mismatch",
            "Frozen rynmesh-peer binary does not match computer-release-receipt.json",
        )
    if (
        receipt["manifest"] != expected_receipt["manifest"]
        or receipt["releaseId"] != expected_receipt["releaseId"]
    ):
        raise PackagingContractError(
            "packaging_receipt_manifest_mismatch",
            "Staged or embedded manifest does not match computer-release-receipt.json",
        )

    return receipt


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Ryn R2 native computer-use manifest & release receipt tool",
    )
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_stage = sub.add_parser("stage-manifest")
    p_stage.add_argument("--repo-root", required=True)
    p_stage.add_argument("--build-root", required=True)
    p_stage.add_argument("--venv-root", required=True)
    p_stage.add_argument("--native-target-dir", required=True)
    p_stage.add_argument("--native-exe", required=True)
    p_stage.add_argument("--staging-manifest", required=True)

    p_receipt = sub.add_parser("write-receipt")
    p_receipt.add_argument("--repo-root", required=True)
    p_receipt.add_argument("--build-root", required=True)
    p_receipt.add_argument("--venv-root", required=True)
    p_receipt.add_argument("--work-dir", required=True)
    p_receipt.add_argument("--native-target-dir", required=True)
    p_receipt.add_argument("--native-exe", required=True)
    p_receipt.add_argument("--staged-native-exe", required=True)
    p_receipt.add_argument("--dist-peer-exe", required=True)
    p_receipt.add_argument("--staged-peer-exe", required=True)
    p_receipt.add_argument("--staging-manifest", required=True)
    p_receipt.add_argument("--tauri-windows-conf", required=True)
    p_receipt.add_argument("--receipt-out", required=True)

    p_skip = sub.add_parser("verify-skip-sidecar")
    p_skip.add_argument("--repo-root", required=True)
    p_skip.add_argument("--build-root", required=True)
    p_skip.add_argument("--venv-root", required=True)
    p_skip.add_argument("--work-dir", required=True)
    p_skip.add_argument("--native-target-dir", required=True)
    p_skip.add_argument("--native-exe", required=True)
    p_skip.add_argument("--staged-native-exe", required=True)
    p_skip.add_argument("--dist-peer-exe", required=True)
    p_skip.add_argument("--staged-peer-exe", required=True)
    p_skip.add_argument("--staging-manifest", required=True)
    p_skip.add_argument("--tauri-windows-conf", required=True)
    p_skip.add_argument("--receipt-path", required=True)

    args = parser.parse_args(list(argv) if argv is not None else None)
    try:
        if args.cmd == "stage-manifest":
            _, manifest_bytes, _ = generate_manifest_from_executable(
                args.native_exe,
                repo_root=args.repo_root,
                expected_target_dir=args.native_target_dir,
            )
            venv_manifest_path = stage_manifest_into_venv(
                manifest_bytes=manifest_bytes,
                venv_root=args.venv_root,
                build_root=args.build_root,
                repo_root=args.repo_root,
                staging_manifest_copy=args.staging_manifest,
            )
            print(
                json.dumps(
                    {
                        "ok": True,
                        "step": "stage-manifest",
                        "venvManifestPath": str(venv_manifest_path),
                        "stagingManifestPath": str(Path(args.staging_manifest).resolve()),
                        "manifestSha256": _sha256_bytes(manifest_bytes),
                    }
                )
            )
            return 0

        if args.cmd == "write-receipt":
            manifest_bytes, _ = _read_file_bytes_and_sha256(
                Path(args.staging_manifest),
                missing_code="packaging_manifest_missing",
            )
            toc_entries = extract_toc_data_entries_from_pyinstaller_work(args.work_dir)
            receipt_dict, receipt_bytes = create_build_receipt(
                repo_root=args.repo_root,
                manifest_bytes=manifest_bytes,
                native_release_exe=args.native_exe,
                staged_native_exe=args.staged_native_exe,
                dist_peer_exe=args.dist_peer_exe,
                staged_peer_exe=args.staged_peer_exe,
                tauri_windows_conf_path=args.tauri_windows_conf,
                expected_target_dir=args.native_target_dir,
                archive_manifest_entries=toc_entries,
                venv_root=args.venv_root,
            )
            receipt_out = Path(args.receipt_out)
            receipt_out.parent.mkdir(parents=True, exist_ok=True)
            receipt_out.write_bytes(receipt_bytes)
            print(
                json.dumps(
                    {
                        "ok": True,
                        "step": "write-receipt",
                        "releaseId": receipt_dict["releaseId"],
                        "receiptPath": str(receipt_out.resolve()),
                        "manifestSha256": receipt_dict["manifest"]["sha256"],
                        "nativeSha256": receipt_dict["nativeExecutable"]["sha256"],
                        "peerSha256": receipt_dict["peerExecutable"]["sha256"],
                        "sourceTreeSha256": receipt_dict["sourceCommitment"][
                            "treeSha256"
                        ],
                    }
                )
            )
            return 0

        if args.cmd == "verify-skip-sidecar":
            receipt_bytes, _ = _read_file_bytes_and_sha256(
                Path(args.receipt_path),
                missing_code="packaging_receipt_missing",
            )
            manifest_bytes, _ = _read_file_bytes_and_sha256(
                Path(args.staging_manifest),
                missing_code="packaging_manifest_missing",
            )
            toc_entries = extract_toc_data_entries_from_pyinstaller_work(args.work_dir)
            receipt_dict = verify_build_receipt_and_pair(
                receipt_bytes=receipt_bytes,
                manifest_bytes=manifest_bytes,
                repo_root=args.repo_root,
                native_release_exe=args.native_exe,
                staged_native_exe=args.staged_native_exe,
                dist_peer_exe=args.dist_peer_exe,
                staged_peer_exe=args.staged_peer_exe,
                tauri_windows_conf_path=args.tauri_windows_conf,
                expected_target_dir=args.native_target_dir,
                archive_manifest_entries=toc_entries,
                venv_root=args.venv_root,
            )
            print(
                json.dumps(
                    {
                        "ok": True,
                        "step": "verify-skip-sidecar",
                        "releaseId": receipt_dict["releaseId"],
                        "manifestSha256": receipt_dict["manifest"]["sha256"],
                        "nativeSha256": receipt_dict["nativeExecutable"]["sha256"],
                        "peerSha256": receipt_dict["peerExecutable"]["sha256"],
                    }
                )
            )
            return 0

        raise PackagingContractError("packaging_unknown_command", f"Unknown command: {args.cmd}")
    except PackagingContractError as exc:
        print(
            json.dumps(
                {
                    "ok": False,
                    "errorCode": exc.code,
                    "detail": exc.detail,
                }
            ),
            file=sys.stderr,
        )
        return 1


if __name__ == "__main__":
    sys.exit(main())
