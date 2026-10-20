"""Resource-profile validation, handle-pinned lease management, and trusted location for the Ryn QuickJS host runtime.

This module provides strict, deterministic validation, handle-bound file lineage verification,
and immutable resource-profile resolution for the Ryn JS runtime. It does not auto-execute
processes, modify environment variables, or query PATH/cwd/temp in production mode.
"""

from __future__ import annotations

import copy
import hashlib
import json
import math
import os
import re
import stat
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .process_identity import (
    MAX_ACTIVE_RESOURCE_LEASES,
    MAX_PINNED_DIRECTORIES_PER_LEASE,
    MAX_PINNED_HANDLES_PER_LEASE,
    JsProcessIdentityError,
    PinnedDirectoryIdentity,
    PinnedFileIdentity,
    close_pinned_raw_handle,
    open_and_verify_pinned_win32_directory,
    open_and_verify_pinned_win32_file,
    revalidate_pinned_directory_handle,
    revalidate_pinned_file_handle,
)
from .protocol import PROTOCOL_VERSION, JsProtocolError, JsRuntimeError, canonical_json_bytes

_ACTIVE_LEASES_LOCK = threading.Lock()
_ACTIVE_LEASE_IDS: set[int] = set()


@dataclass(frozen=True)
class _ProductionLeaseAuthorityRecord:
    profile_root: Path
    node_executable: Path
    worker_path: Path
    sky_shim_path: Path
    package_root: Path
    wasm_path: Path
    manifest_path: Path
    profile_digest: str
    platform: str
    arch: str
    node_version: str
    node_sha256: str
    node_bytes: int
    quickjs_version: str
    quickjs_variant: str
    wire_protocol: int
    worker_sha256: str
    sky_shim_sha256: str
    wasm_sha256: str
    total_files: int
    total_bytes: int


_PRODUCTION_LEASE_AUTHORITIES: dict[int, _ProductionLeaseAuthorityRecord] = {}

MANIFEST_SCHEMA_VERSION = 1
MANIFEST_FILENAME = "manifest.json"
BUILD_RECEIPT_FILENAME = "build-receipt.json"

PINNED_QUICKJS_VERSION = "0.32.0"
PINNED_QUICKJS_VARIANT = "RELEASE_SYNC"
PINNED_WIRE_PROTOCOL = PROTOCOL_VERSION
PINNED_RUNTIME_PACKAGE_NAME = "@rynmesh/js-runtime"
PINNED_RUNTIME_PACKAGE_VERSION = "0.1.0"

APPROVED_WIN32_X64_NODE_VERSION = "22.23.3"
APPROVED_WIN32_X64_NODE_ARCHIVE_SHA256 = "2b0ff57b049cda1bbcea2240eec20467018713c1efe1f7360c2681859b90ed71"
APPROVED_WIN32_X64_NODE_SHA256 = "9c9245166b4a8e182e0b797da9c20136117ff24368eaff1fec8343a123c8db0e"
APPROVED_WIN32_X64_NODE_BYTES = 86_973_768

ALLOWED_PLATFORMS: frozenset[str] = frozenset({"win32", "darwin", "linux"})
ALLOWED_ARCHES: frozenset[str] = frozenset({"x64", "arm64"})
ALLOWED_PROFILE_MODES: frozenset[str] = frozenset({"packaged_release", "source_dev"})

MAX_MANIFEST_BYTES = 1024 * 1024
MAX_MANIFEST_JSON_DEPTH = 16
MAX_PROFILE_FILE_COUNT = 512
MAX_SINGLE_RESOURCE_FILE_BYTES = 160 * 1024 * 1024
MAX_AGGREGATE_PROFILE_BYTES = 320 * 1024 * 1024
MAX_RELATIVE_PATH_LEN = 240
MAX_RELATIVE_PATH_DEPTH = 16
MAX_LICENSE_ENTRIES = 64

SHA256_HEX_RE = re.compile(r"^[0-9a-f]{64}$")
SEMVER_RE = re.compile(r"^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)(?:-[0-9A-Za-z.-]+)?(?:\+[0-9A-Za-z.-]+)?$")
PROFILE_ID_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{1,63}$")

_WINDOWS_RESERVED_NAMES: frozenset[str] = frozenset({
    "CON",
    "PRN",
    "AUX",
    "NUL",
    "CONIN$",
    "CONOUT$",
    *(f"COM{i}" for i in range(1, 10)),
    *(f"LPT{i}" for i in range(1, 10)),
})

REQUIRED_PRODUCTION_PACKAGES: dict[str, str] = {
    "@jitl/quickjs-ffi-types": "0.32.0",
    "@jitl/quickjs-wasmfile-debug-asyncify": "0.32.0",
    "@jitl/quickjs-wasmfile-debug-sync": "0.32.0",
    "@jitl/quickjs-wasmfile-release-asyncify": "0.32.0",
    "@jitl/quickjs-wasmfile-release-sync": "0.32.0",
    "quickjs-emscripten": "0.32.0",
    "quickjs-emscripten-core": "0.32.0",
}

QUICKJS_VARIANT_PACKAGES: tuple[str, ...] = (
    "@jitl/quickjs-wasmfile-debug-asyncify",
    "@jitl/quickjs-wasmfile-debug-sync",
    "@jitl/quickjs-wasmfile-release-asyncify",
    "@jitl/quickjs-wasmfile-release-sync",
)

NODE_EXECUTABLE_REL_PATH_WIN32 = "node/node.exe"
NODE_EXECUTABLE_REL_PATH_POSIX = "node/bin/node"
NODE_LICENSE_REL_PATH = "licenses/NODE_LICENSE"
NODE_README_REL_PATH = "licenses/NODE_README.md"
WORKER_REL_PATH = "package/src/worker.mjs"
SKY_SHIM_REL_PATH = "package/src/sky-shim.mjs"
PACKAGE_JSON_REL_PATH = "package/package.json"
PACKAGE_LOCK_REL_PATH = "package/package-lock.json"
RELEASE_SYNC_WASM_REL_PATH = (
    "package/node_modules/@jitl/quickjs-wasmfile-release-sync/dist/emscripten-module.wasm"
)
RELEASE_SYNC_PKG_REL_PATH = (
    "package/node_modules/@jitl/quickjs-wasmfile-release-sync/package.json"
)
QUICKJS_UMBRELLA_PKG_REL_PATH = "package/node_modules/quickjs-emscripten/package.json"
QUICKJS_CORE_PKG_REL_PATH = "package/node_modules/quickjs-emscripten-core/package.json"
QUICKJS_FFI_TYPES_PKG_REL_PATH = "package/node_modules/@jitl/quickjs-ffi-types/package.json"

_TOP_LEVEL_MANIFEST_KEYS: frozenset[str] = frozenset({
    "schema_version",
    "profile_id",
    "platform",
    "arch",
    "node_version",
    "node_executable_relpath",
    "node_sha256",
    "node_bytes",
    "quickjs_version",
    "quickjs_variant",
    "wire_protocol",
    "worker_relpath",
    "worker_sha256",
    "sky_shim_relpath",
    "sky_shim_sha256",
    "wasm_relpath",
    "wasm_sha256",
    "total_files",
    "total_bytes",
    "files",
    "licenses",
    "dependency_packages",
})

_FILE_ENTRY_KEYS: frozenset[str] = frozenset({
    "path",
    "size",
    "sha256",
})

_LICENSE_ENTRY_KEYS: frozenset[str] = frozenset({
    "component",
    "version",
    "license",
    "path",
    "sha256",
    "size",
})

_DEPENDENCY_PACKAGE_ENTRY_KEYS: frozenset[str] = frozenset({
    "name",
    "version",
    "integrity",
    "license",
    "package_json_path",
    "license_path",
})

_FORBIDDEN_INVOCATION_ENV_KEYS: frozenset[str] = frozenset({
    "NODE_OPTIONS",
    "NODE_PATH",
    "NODE_EXTRA_CA_CERTS",
    "NODE_REDIRECT_WARNINGS",
    "NODE_REPL_EXTERNAL_MODULE",
    "UV_THREADPOOL_SIZE",
})


class JsResourceError(JsRuntimeError):
    """Raised when a JS runtime resource profile or file fails integrity, lineage, or schema checks."""


def expected_node_executable_relpath(platform_name: str) -> str:
    """Return the canonical relative path for the Node binary inside a resource profile."""
    if platform_name == "win32":
        return NODE_EXECUTABLE_REL_PATH_WIN32
    if platform_name in {"darwin", "linux"}:
        return NODE_EXECUTABLE_REL_PATH_POSIX
    raise JsResourceError(
        "ryn_js_resource_invalid_platform",
        f"Unsupported platform for Node executable path: {platform_name!r}",
    )


def validate_sha256_hex(value: Any, field_name: str = "sha256") -> str:
    """Validate that `value` is a 64-character lowercase hexadecimal SHA-256 digest."""
    if not isinstance(value, str) or not SHA256_HEX_RE.fullmatch(value):
        raise JsResourceError(
            "ryn_js_resource_invalid_hash",
            f"{field_name} must be a 64-character lowercase hex SHA-256 digest",
        )
    return value


def validate_semver(value: Any, field_name: str = "version") -> str:
    """Validate that `value` is a strict semantic version string."""
    if not isinstance(value, str) or not SEMVER_RE.fullmatch(value):
        raise JsResourceError(
            "ryn_js_resource_invalid_version",
            f"{field_name} must be a valid semantic version string, got {value!r}",
        )
    return value


def validate_relative_resource_path(rel_path: Any, *, field_name: str = "path") -> str:
    """Validate a canonical POSIX-normalized relative path inside a resource profile.

    Rejects empty strings, backslashes, leading/trailing slashes, `.` or `..` segments,
    drive letters, UNC prefixes, NTFS Alternate Data Streams (`:`), control characters,
    NUL bytes, Windows reserved device names, and excessive depth/length.
    """
    if not isinstance(rel_path, str) or not rel_path:
        raise JsResourceError(
            "ryn_js_resource_invalid_path",
            f"{field_name} must be a non-empty relative path string",
        )
    if len(rel_path) > MAX_RELATIVE_PATH_LEN:
        raise JsResourceError(
            "ryn_js_resource_invalid_path",
            f"{field_name} exceeds maximum length of {MAX_RELATIVE_PATH_LEN} characters",
        )
    if "\x00" in rel_path or any(ord(ch) < 32 for ch in rel_path):
        raise JsResourceError(
            "ryn_js_resource_invalid_path",
            f"{field_name} contains control characters or NUL bytes",
        )
    if "\\" in rel_path:
        raise JsResourceError(
            "ryn_js_resource_invalid_path",
            f"{field_name} must use forward slashes only: {rel_path!r}",
        )
    if ":" in rel_path:
        raise JsResourceError(
            "ryn_js_resource_ads_or_drive_forbidden",
            f"{field_name} must not contain ':' (drive letters or NTFS Alternate Data Streams): {rel_path!r}",
        )
    if rel_path.startswith("/") or rel_path.endswith("/") or "//" in rel_path:
        raise JsResourceError(
            "ryn_js_resource_invalid_path",
            f"{field_name} must be a clean relative path without leading, trailing, or empty segments: {rel_path!r}",
        )
    parts = rel_path.split("/")
    if len(parts) > MAX_RELATIVE_PATH_DEPTH:
        raise JsResourceError(
            "ryn_js_resource_invalid_path",
            f"{field_name} exceeds maximum directory depth {MAX_RELATIVE_PATH_DEPTH}",
        )
    for part in parts:
        if not part or part in {".", ".."}:
            raise JsResourceError(
                "ryn_js_resource_path_traversal",
                f"{field_name} contains traversal or dot segment: {rel_path!r}",
            )
        if part.endswith(" ") or part.endswith("."):
            raise JsResourceError(
                "ryn_js_resource_invalid_path",
                f"{field_name} segment has trailing space or dot: {part!r}",
            )
        stem = part.split(".", 1)[0].upper()
        if stem in _WINDOWS_RESERVED_NAMES or part.upper() in _WINDOWS_RESERVED_NAMES:
            raise JsResourceError(
                "ryn_js_resource_reserved_device_name",
                f"{field_name} contains Windows reserved device name: {part!r}",
            )
    return rel_path


def _check_input_filesystem_path_syntax(path_value: str | Path, *, label: str = "path") -> Path:
    """Reject raw path strings with NUL, UNC device namespaces, or NTFS ADS colons before filesystem access."""
    raw = str(path_value)
    if not raw or "\x00" in raw:
        raise JsResourceError("ryn_js_resource_invalid_path", f"{label} is empty or contains NUL")
    norm_prefix = raw.replace("/", "\\")
    if norm_prefix.startswith("\\\\"):
        raise JsResourceError(
            "ryn_js_resource_unc_or_device_forbidden",
            f"{label} must not use UNC or Windows device namespace paths: {raw!r}",
        )
    # Check for NTFS Alternate Data Streams: any ':' other than a single drive letter at index 1 on Windows
    colon_positions = [idx for idx, ch in enumerate(raw) if ch == ":"]
    for idx in colon_positions:
        if not (idx == 1 and len(raw) >= 2 and raw[0].isalpha()):
            raise JsResourceError(
                "ryn_js_resource_ads_or_drive_forbidden",
                f"{label} contains forbidden colon / Alternate Data Stream syntax: {raw!r}",
            )
    for part in Path(raw).parts:
        if part in {"\\", "/"} or (len(part) in {2, 3} and part[0].isalpha() and part[1] == ":"):
            continue
        stem = part.split(".", 1)[0].upper()
        if stem in _WINDOWS_RESERVED_NAMES or part.upper() in _WINDOWS_RESERVED_NAMES:
            raise JsResourceError(
                "ryn_js_resource_reserved_device_name",
                f"{label} contains reserved device segment: {part!r}",
            )
    return Path(raw)


def is_symlink_or_reparse(path: Path) -> bool:
    """Return True if `path` is a symbolic link, junction, or Windows reparse point."""
    try:
        if path.is_symlink():
            return True
        st = os.lstat(path)
        if stat.S_ISLNK(st.st_mode):
            return True
        file_attrs = getattr(st, "st_file_attributes", 0)
        if isinstance(file_attrs, int) and (file_attrs & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)):
            return True
    except OSError:
        return True
    return False


def _resolve_opened_fd_path(fd: int) -> Path:
    if os.name == "nt":
        import ctypes
        import msvcrt

        handle = msvcrt.get_osfhandle(fd)
        kernel32 = ctypes.windll.kernel32
        get_final_path = kernel32.GetFinalPathNameByHandleW
        get_final_path.argtypes = [ctypes.c_void_p, ctypes.c_wchar_p, ctypes.c_uint32, ctypes.c_uint32]
        get_final_path.restype = ctypes.c_uint32
        buf = ctypes.create_unicode_buffer(4096)
        length = get_final_path(ctypes.c_void_p(handle), buf, 4096, 0)
        if not length or length >= 4096:
            raise JsResourceError(
                "ryn_js_resource_unreadable",
                "Opened resource file handle path could not be resolved",
            )
        raw_final = buf.value
        if raw_final.startswith("\\\\?\\UNC\\"):
            raise JsResourceError(
                "ryn_js_resource_unc_or_device_forbidden",
                "Opened resource file handle resolved to a UNC path",
            )
        if raw_final.startswith("\\\\?\\"):
            raw_final = raw_final[4:]
        return _check_input_filesystem_path_syntax(raw_final, label="opened_handle_path").resolve(strict=True)
    proc_fd = Path(f"/proc/self/fd/{fd}")
    if proc_fd.exists():
        return proc_fd.resolve(strict=True)
    raise JsResourceError(
        "ryn_js_resource_unreadable",
        "Opened resource file handle path could not be resolved",
    )


def verify_directory_lineage_no_reparse(root_path: str | Path) -> Path:
    """Verify that `root_path` and every existing ancestor directory are real directories without reparse points."""
    candidate = _check_input_filesystem_path_syntax(root_path, label="profile_root")
    if not candidate.exists():
        raise JsResourceError(
            "ryn_js_resource_root_missing",
            f"Resource root does not exist: {candidate}",
        )
    if is_symlink_or_reparse(candidate):
        raise JsResourceError(
            "ryn_js_resource_reparse_forbidden",
            f"Resource root is a symlink or reparse point: {candidate}",
        )
    # Check ancestor chain before calling resolve() so we never bless an ancestor junction/symlink
    abs_candidate = Path(os.path.abspath(candidate))
    accum: Path | None = None
    for part in abs_candidate.parts:
        accum = Path(part) if accum is None else accum / part
        # Skip bare drive root like 'D:\' if Windows reports system attributes, though check reparse on dirs
        if len(accum.parts) == 1 and os.name == "nt":
            continue
        if is_symlink_or_reparse(accum):
            raise JsResourceError(
                "ryn_js_resource_reparse_forbidden",
                f"Resource root ancestor is a symlink or reparse point: {accum}",
            )
    try:
        resolved = candidate.resolve(strict=True)
    except OSError as exc:
        raise JsResourceError(
            "ryn_js_resource_root_missing",
            f"Failed to resolve resource root {candidate}: {exc}",
        ) from exc
    if not resolved.is_dir() or is_symlink_or_reparse(resolved):
        raise JsResourceError(
            "ryn_js_resource_root_invalid",
            f"Resource root is not a regular directory: {resolved}",
        )
    if os.path.normcase(str(resolved)) != os.path.normcase(str(abs_candidate)):
        raise JsResourceError(
            "ryn_js_resource_reparse_forbidden",
            f"Resource root resolved path {resolved} differs from physical path {abs_candidate}",
        )
    return resolved


def verify_relative_lineage_under_root(root_resolved: Path, rel_path: str) -> Path:
    """Verify every segment from `root_resolved` down to `rel_path` without traversing symlinks or reparse points."""
    clean_rel = validate_relative_resource_path(rel_path)
    if is_symlink_or_reparse(root_resolved):
        raise JsResourceError(
            "ryn_js_resource_reparse_forbidden",
            "Containment root is a symlink or reparse point",
        )
    current = root_resolved
    parts = clean_rel.split("/")
    for idx, part in enumerate(parts):
        current = current / part
        if not current.exists():
            raise JsResourceError(
                "ryn_js_resource_file_missing",
                f"Required resource path is missing: {clean_rel}",
            )
        if is_symlink_or_reparse(current):
            raise JsResourceError(
                "ryn_js_resource_reparse_forbidden",
                f"Resource path segment is a symlink or reparse point: {clean_rel}",
            )
        is_last = idx == len(parts) - 1
        try:
            st = os.lstat(current)
        except OSError as exc:
            raise JsResourceError(
                "ryn_js_resource_unreadable",
                f"Failed to stat resource path {clean_rel}: {exc}",
            ) from exc
        if is_last:
            if not stat.S_ISREG(st.st_mode):
                raise JsResourceError(
                    "ryn_js_resource_not_regular_file",
                    f"Resource path is not a regular file: {clean_rel}",
                )
        else:
            if not stat.S_ISDIR(st.st_mode):
                raise JsResourceError(
                    "ryn_js_resource_invalid_path",
                    f"Intermediate resource segment is not a directory: {clean_rel}",
                )
    try:
        target_resolved = current.resolve(strict=True)
    except OSError as exc:
        raise JsResourceError(
            "ryn_js_resource_unreadable",
            f"Failed to resolve resource file {clean_rel}: {exc}",
        ) from exc
    if not target_resolved.is_relative_to(root_resolved):
        raise JsResourceError(
            "ryn_js_resource_outside_containment",
            f"Resource path resolves outside profile root: {clean_rel}",
        )
    rel_resolved = target_resolved.relative_to(root_resolved).as_posix()
    if os.name == "nt":
        if rel_resolved.lower() != clean_rel.lower():
            raise JsResourceError(
                "ryn_js_resource_outside_containment",
                f"Resolved relative path {rel_resolved!r} does not match expected {clean_rel!r}",
            )
    elif rel_resolved != clean_rel:
        raise JsResourceError(
            "ryn_js_resource_outside_containment",
            f"Resolved relative path {rel_resolved!r} does not match expected {clean_rel!r}",
        )
    return target_resolved


def read_and_verify_contained_file(
    *,
    root_resolved: Path,
    rel_path: str,
    expected_sha256: str | None = None,
    expected_size: int | None = None,
    max_bytes: int = MAX_SINGLE_RESOURCE_FILE_BYTES,
    retain_bytes: bool = False,
    keep_open_fd: bool = False,
) -> tuple[str, int, bytes | None, int | None]:
    """Perform handle-bound bounded read and SHA-256 verification of a file inside `root_resolved`.

    Returns `(actual_sha256, actual_size, raw_bytes_or_None, opened_fd_or_None)`.
    If `keep_open_fd=True`, the caller receives the open read-only file descriptor and owns closing it.
    """
    if type(max_bytes) is not int or max_bytes <= 0:
        raise JsResourceError("ryn_js_resource_invalid_limit", "max_bytes must be a positive integer")
    if expected_size is not None:
        if type(expected_size) is not int or expected_size < 0:
            raise JsResourceError("ryn_js_resource_invalid_size", "expected_size must be a non-negative integer")
        if expected_size > max_bytes:
            raise JsResourceError(
                "ryn_js_resource_file_too_large",
                f"Expected size {expected_size} for {rel_path} exceeds cap {max_bytes}",
            )
    if expected_sha256 is not None:
        expected_sha256 = validate_sha256_hex(expected_sha256, f"{rel_path}.sha256")

    target_resolved = verify_relative_lineage_under_root(root_resolved, rel_path)
    try:
        st_before = os.lstat(target_resolved)
    except OSError as exc:
        raise JsResourceError("ryn_js_resource_unreadable", f"Failed to lstat {rel_path}: {exc}") from exc

    if stat.S_ISLNK(st_before.st_mode) or not stat.S_ISREG(st_before.st_mode):
        raise JsResourceError("ryn_js_resource_not_regular_file", f"Not a regular file: {rel_path}")
    file_attrs = getattr(st_before, "st_file_attributes", 0)
    if isinstance(file_attrs, int) and (file_attrs & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)):
        raise JsResourceError("ryn_js_resource_reparse_forbidden", f"File has reparse attribute: {rel_path}")
    if st_before.st_size > max_bytes:
        raise JsResourceError(
            "ryn_js_resource_file_too_large",
            f"File {rel_path} size {st_before.st_size} exceeds maximum {max_bytes} bytes",
        )
    if expected_size is not None and st_before.st_size != expected_size:
        raise JsResourceError(
            "ryn_js_resource_size_mismatch",
            f"File {rel_path} byte length mismatch: expected {expected_size}, found {st_before.st_size}",
        )

    open_flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(target_resolved, open_flags)
    except OSError as exc:
        raise JsResourceError("ryn_js_resource_unreadable", f"Failed to open {rel_path}: {exc}") from exc

    hand_off_fd = False
    try:
        try:
            st_fd = os.fstat(fd)
            st_after = os.lstat(target_resolved)
        except OSError as exc:
            raise JsResourceError("ryn_js_resource_unreadable", f"Failed to fstat {rel_path}: {exc}") from exc

        if not stat.S_ISREG(st_fd.st_mode) or not stat.S_ISREG(st_after.st_mode):
            raise JsResourceError("ryn_js_resource_not_regular_file", f"Opened handle is not a regular file: {rel_path}")
        after_attrs = getattr(st_after, "st_file_attributes", 0)
        if isinstance(after_attrs, int) and (after_attrs & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)):
            raise JsResourceError("ryn_js_resource_reparse_forbidden", f"File became a reparse point: {rel_path}")
        if (st_before.st_ino or st_fd.st_ino) and (
            st_fd.st_ino != st_before.st_ino
            or st_fd.st_dev != st_before.st_dev
            or st_after.st_ino != st_fd.st_ino
            or st_after.st_dev != st_fd.st_dev
        ):
            raise JsResourceError("ryn_js_resource_file_changed", f"File inode/device changed during open: {rel_path}")

        handle_path = _resolve_opened_fd_path(fd)
        if not handle_path.is_relative_to(root_resolved):
            raise JsResourceError(
                "ryn_js_resource_outside_containment",
                f"Opened handle for {rel_path} resolved outside root",
            )
        if os.path.normcase(str(handle_path)) != os.path.normcase(str(target_resolved)):
            raise JsResourceError(
                "ryn_js_resource_reparse_forbidden",
                f"Opened handle path {handle_path} does not match target {target_resolved}",
            )

        read_cap = expected_size if expected_size is not None else max_bytes
        read_limit = read_cap + 1
        chunks: list[bytes] | None = [] if retain_bytes else None
        hasher = hashlib.sha256()
        total = 0

        while total < read_limit:
            try:
                chunk = os.read(fd, min(65536, read_limit - total))
            except OSError as exc:
                raise JsResourceError("ryn_js_resource_unreadable", f"Read error on {rel_path}: {exc}") from exc
            if not chunk:
                break
            if chunks is not None:
                chunks.append(chunk)
            hasher.update(chunk)
            total += len(chunk)

        try:
            st_end = os.fstat(fd)
        except OSError as exc:
            raise JsResourceError("ryn_js_resource_unreadable", f"Post-read fstat failed on {rel_path}: {exc}") from exc

        if (st_fd.st_ino or st_end.st_ino) and (
            st_end.st_ino != st_fd.st_ino or st_end.st_dev != st_fd.st_dev
        ):
            raise JsResourceError("ryn_js_resource_file_changed", f"File handle identity changed during read: {rel_path}")
        if total > max_bytes or st_fd.st_size > max_bytes or st_end.st_size > max_bytes:
            raise JsResourceError(
                "ryn_js_resource_file_too_large",
                f"File {rel_path} exceeds maximum {max_bytes} bytes",
            )
        if expected_size is not None and (
            total != expected_size or st_fd.st_size != expected_size or st_end.st_size != expected_size
        ):
            raise JsResourceError(
                "ryn_js_resource_size_mismatch",
                f"File {rel_path} byte length mismatch during read: expected {expected_size}, read {total}",
            )
        if total != st_end.st_size:
            raise JsResourceError("ryn_js_resource_file_changed", f"File size changed while reading: {rel_path}")

        actual_sha256 = hasher.hexdigest()
        if expected_sha256 is not None and actual_sha256 != expected_sha256:
            raise JsResourceError(
                "ryn_js_resource_hash_mismatch",
                f"SHA-256 mismatch for {rel_path}: expected {expected_sha256}, got {actual_sha256}",
            )

        raw_bytes = b"".join(chunks) if chunks is not None else None
        if keep_open_fd:
            try:
                os.lseek(fd, 0, os.SEEK_SET)
            except OSError:
                pass
            hand_off_fd = True
            return actual_sha256, total, raw_bytes, fd
        return actual_sha256, total, raw_bytes, None
    finally:
        if not hand_off_fd:
            try:
                os.close(fd)
            except OSError:
                pass


def parse_strict_bounded_json(
    raw_bytes: bytes,
    *,
    max_bytes: int = MAX_MANIFEST_BYTES,
    max_depth: int = MAX_MANIFEST_JSON_DEPTH,
    label: str = "manifest",
) -> dict[str, Any]:
    """Parse UTF-8 JSON rejecting BOM, duplicate keys, non-finite numbers, and excessive depth/size."""
    if not isinstance(raw_bytes, (bytes, bytearray)):
        raise JsResourceError("ryn_js_resource_invalid_json", f"{label} must be raw bytes")
    if len(raw_bytes) <= 0:
        raise JsResourceError("ryn_js_resource_invalid_json", f"{label} is empty")
    if len(raw_bytes) > max_bytes:
        raise JsResourceError(
            "ryn_js_resource_manifest_too_large",
            f"{label} size {len(raw_bytes)} exceeds limit {max_bytes} bytes",
        )
    if raw_bytes.startswith(b"\xef\xbb\xbf"):
        raise JsResourceError("ryn_js_resource_invalid_json", f"{label} must not contain a UTF-8 BOM")
    try:
        text = bytes(raw_bytes).decode("utf-8")
    except UnicodeDecodeError as exc:
        raise JsResourceError("ryn_js_resource_invalid_json", f"{label} is not valid UTF-8: {exc}") from exc

    def _reject_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        obj: dict[str, Any] = {}
        for key, value in pairs:
            if not isinstance(key, str):
                raise JsResourceError("ryn_js_resource_invalid_json", f"{label} object keys must be strings")
            if key in obj:
                raise JsResourceError(
                    "ryn_js_resource_duplicate_json_key",
                    f"{label} contains duplicate JSON key: {key!r}",
                )
            obj[key] = value
        return obj

    def _reject_constant(token: str) -> Any:
        raise JsResourceError(
            "ryn_js_resource_invalid_json",
            f"{label} contains non-finite JSON constant: {token!r}",
        )

    try:
        parsed = json.loads(
            text,
            object_pairs_hook=_reject_duplicates,
            parse_constant=_reject_constant,
        )
    except JsResourceError:
        raise
    except (json.JSONDecodeError, ValueError, RecursionError) as exc:
        raise JsResourceError("ryn_js_resource_invalid_json", f"Invalid JSON in {label}: {exc}") from exc

    if not isinstance(parsed, dict):
        raise JsResourceError("ryn_js_resource_invalid_json", f"{label} root must be a JSON object")

    def _check_depth(val: Any, depth: int) -> None:
        if depth > max_depth:
            raise JsResourceError(
                "ryn_js_resource_json_depth_exceeded",
                f"{label} JSON nesting depth exceeds maximum {max_depth}",
            )
        if val is None or isinstance(val, (bool, str)):
            return
        if type(val) is int:
            return
        if isinstance(val, float):
            if not math.isfinite(val):
                raise JsResourceError("ryn_js_resource_invalid_json", f"Non-finite float in {label}")
            return
        if isinstance(val, list):
            for item in val:
                _check_depth(item, depth + 1)
            return
        if isinstance(val, dict):
            for k, v in val.items():
                if not isinstance(k, str):
                    raise JsResourceError("ryn_js_resource_invalid_json", f"Non-string key in {label}")
                _check_depth(v, depth + 1)
            return
        raise JsResourceError("ryn_js_resource_invalid_json", f"Unsupported JSON type in {label}: {type(val).__name__}")

    _check_depth(parsed, 0)
    return parsed


def compute_manifest_digest(manifest_dict: dict[str, Any]) -> str:
    """Compute the canonical SHA-256 digest of a validated manifest dictionary."""
    try:
        canonical = canonical_json_bytes(manifest_dict, max_bytes=MAX_MANIFEST_BYTES)
    except JsProtocolError as exc:
        raise JsResourceError("ryn_js_resource_invalid_json", str(exc)) from exc
    return hashlib.sha256(canonical).hexdigest()


@dataclass(frozen=True)
class ResourceFileEntry:
    """Immutable per-file record inside a Ryn JS runtime resource profile."""

    path: str
    size: int
    sha256: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "path": self.path,
            "size": self.size,
            "sha256": self.sha256,
        }


@dataclass(frozen=True)
class ResourceLicenseEntry:
    """Immutable license and provenance entry for Node or an npm dependency."""

    component: str
    version: str
    license: str
    path: str
    sha256: str
    size: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "component": self.component,
            "version": self.version,
            "license": self.license,
            "path": self.path,
            "sha256": self.sha256,
            "size": self.size,
        }


@dataclass(frozen=True)
class ResourceDependencyPackageEntry:
    """Immutable production npm package closure entry verified against package-lock.json."""

    name: str
    version: str
    integrity: str
    license: str
    package_json_path: str
    license_path: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "version": self.version,
            "integrity": self.integrity,
            "license": self.license,
            "package_json_path": self.package_json_path,
            "license_path": self.license_path,
        }


@dataclass(frozen=True)
class ExpectedJsRuntimeProfileSpec:
    """Trusted builder/package-injected commitment for a Ryn JS runtime resource profile.

    Production resolution requires an `ExpectedJsRuntimeProfileSpec` with a pinned
    `expected_manifest_sha256` supplied by trusted build/package configuration, never
    generated dynamically from whatever files happen to be on disk or read from caller JSON.
    """

    expected_manifest_sha256: str
    platform: str = "win32"
    arch: str = "x64"
    node_version: str = APPROVED_WIN32_X64_NODE_VERSION
    node_sha256: str = APPROVED_WIN32_X64_NODE_SHA256
    node_bytes: int = APPROVED_WIN32_X64_NODE_BYTES
    quickjs_version: str = PINNED_QUICKJS_VERSION
    quickjs_variant: str = PINNED_QUICKJS_VARIANT
    wire_protocol: int = PINNED_WIRE_PROTOCOL
    worker_sha256: str | None = None
    sky_shim_sha256: str | None = None
    wasm_sha256: str | None = None

    def __post_init__(self) -> None:
        validate_sha256_hex(self.expected_manifest_sha256, "expected_manifest_sha256")
        if self.platform not in ALLOWED_PLATFORMS:
            raise JsResourceError("ryn_js_resource_invalid_platform", f"Unsupported platform: {self.platform!r}")
        if self.arch not in ALLOWED_ARCHES:
            raise JsResourceError("ryn_js_resource_invalid_arch", f"Unsupported arch: {self.arch!r}")
        validate_semver(self.node_version, "node_version")
        validate_sha256_hex(self.node_sha256, "node_sha256")
        if type(self.node_bytes) is not int or self.node_bytes <= 0 or self.node_bytes > MAX_SINGLE_RESOURCE_FILE_BYTES:
            raise JsResourceError("ryn_js_resource_invalid_size", "node_bytes must be a bounded positive integer")
        if self.quickjs_version != PINNED_QUICKJS_VERSION:
            raise JsResourceError(
                "ryn_js_resource_version_mismatch",
                f"quickjs_version must be {PINNED_QUICKJS_VERSION!r}",
            )
        if self.quickjs_variant != PINNED_QUICKJS_VARIANT:
            raise JsResourceError(
                "ryn_js_resource_variant_mismatch",
                f"quickjs_variant must be {PINNED_QUICKJS_VARIANT!r}",
            )
        if type(self.wire_protocol) is not int or self.wire_protocol != PINNED_WIRE_PROTOCOL:
            raise JsResourceError(
                "ryn_js_resource_protocol_mismatch",
                f"wire_protocol must be integer {PINNED_WIRE_PROTOCOL}",
            )
        if self.worker_sha256 is not None:
            validate_sha256_hex(self.worker_sha256, "worker_sha256")
        if self.sky_shim_sha256 is not None:
            validate_sha256_hex(self.sky_shim_sha256, "sky_shim_sha256")
        if self.wasm_sha256 is not None:
            validate_sha256_hex(self.wasm_sha256, "wasm_sha256")


@dataclass(frozen=True)
class JsRuntimeResourceManifest:
    """Validated immutable schema-1 manifest for a Ryn JS runtime resource profile."""

    schema_version: int
    profile_id: str
    platform: str
    arch: str
    node_version: str
    node_executable_relpath: str
    node_sha256: str
    node_bytes: int
    quickjs_version: str
    quickjs_variant: str
    wire_protocol: int
    worker_relpath: str
    worker_sha256: str
    sky_shim_relpath: str
    sky_shim_sha256: str
    wasm_relpath: str
    wasm_sha256: str
    total_files: int
    total_bytes: int
    files: tuple[ResourceFileEntry, ...]
    licenses: tuple[ResourceLicenseEntry, ...]
    dependency_packages: tuple[ResourceDependencyPackageEntry, ...]
    manifest_sha256: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "profile_id": self.profile_id,
            "platform": self.platform,
            "arch": self.arch,
            "node_version": self.node_version,
            "node_executable_relpath": self.node_executable_relpath,
            "node_sha256": self.node_sha256,
            "node_bytes": self.node_bytes,
            "quickjs_version": self.quickjs_version,
            "quickjs_variant": self.quickjs_variant,
            "wire_protocol": self.wire_protocol,
            "worker_relpath": self.worker_relpath,
            "worker_sha256": self.worker_sha256,
            "sky_shim_relpath": self.sky_shim_relpath,
            "sky_shim_sha256": self.sky_shim_sha256,
            "wasm_relpath": self.wasm_relpath,
            "wasm_sha256": self.wasm_sha256,
            "total_files": self.total_files,
            "total_bytes": self.total_bytes,
            "files": [entry.to_dict() for entry in self.files],
            "licenses": [entry.to_dict() for entry in self.licenses],
            "dependency_packages": [entry.to_dict() for entry in self.dependency_packages],
        }


@dataclass(frozen=True)
class JsRuntimeResourceProfile:
    """Verified immutable Ryn JS resource profile ready for later `JsRuntimeManager` consumption.

    Note: File and manifest verification (`files_verified=True`) intentionally remains distinct
    from a live Node/QuickJS subprocess handshake (`handshake_verified=False`). Existence or
    static verification of files on disk never claims that a live child handshake has passed.
    """

    profile_root: Path
    node_executable: Path
    worker_path: Path
    sky_shim_path: Path
    package_root: Path
    wasm_path: Path
    manifest_path: Path
    profile_digest: str
    mode: str
    platform: str
    arch: str
    node_version: str
    node_sha256: str
    node_bytes: int
    quickjs_version: str
    quickjs_variant: str
    wire_protocol: int
    worker_sha256: str
    sky_shim_sha256: str
    wasm_sha256: str
    manifest: JsRuntimeResourceManifest
    files_verified: bool = True
    handshake_verified: bool = False

    @property
    def is_production(self) -> bool:
        return self.mode == "packaged_release"

    @property
    def is_development(self) -> bool:
        return self.mode == "source_dev"

    def to_public_metadata(self) -> dict[str, Any]:
        """Return safe, secret-free diagnostic metadata without exposing raw host filesystem paths."""
        return {
            "profile_id": self.manifest.profile_id,
            "profile_digest": self.profile_digest,
            "mode": self.mode,
            "platform": self.platform,
            "arch": self.arch,
            "node_version": self.node_version,
            "node_sha256": self.node_sha256,
            "node_bytes": self.node_bytes,
            "quickjs_version": self.quickjs_version,
            "quickjs_variant": self.quickjs_variant,
            "wire_protocol": self.wire_protocol,
            "worker_sha256": self.worker_sha256,
            "sky_shim_sha256": self.sky_shim_sha256,
            "wasm_sha256": self.wasm_sha256,
            "total_files": self.manifest.total_files,
            "total_bytes": self.manifest.total_bytes,
            "files_verified": self.files_verified,
            "handshake_verified": self.handshake_verified,
        }


def expected_profile_directory_relpaths(manifest: JsRuntimeResourceManifest) -> tuple[str, ...]:
    """Return the canonical sorted tuple of relative directories for `manifest` (starting with '.')."""
    dirs: set[str] = set()
    for entry in manifest.files:
        parts = entry.path.split("/")
        for idx in range(1, len(parts)):
            dirs.add("/".join(parts[:idx]))
    return (".", *sorted(dirs))


def _parent_directory_relpath(rel_path: str) -> str:
    if "/" not in rel_path:
        return "."
    return rel_path.rsplit("/", 1)[0]


@dataclass
class VerifiedJsResourceLease:
    """Handle-pinned lease over all inventoried runtime files and directories held across worker spawn and lifetime.

    On Windows, holds open read-only Win32 file handles (`GENERIC_READ`, `FILE_SHARE_READ` only)
    for every file in `manifest.files` (including `node/node.exe`, `worker.mjs`, `sky-shim.mjs`,
    all `package.json`/`package-lock.json`, all 4 QuickJS `.wasm` binaries, and all dependency
    and license files) AND open directory handles (`FILE_FLAG_BACKUP_SEMANTICS | FILE_FLAG_OPEN_REPARSE_POINT`,
    `FILE_SHARE_READ | FILE_SHARE_WRITE` without `FILE_SHARE_DELETE`) for the profile root and
    every ancestor directory in the profile tree, preventing modification, deletion, or directory
    rename across the entire worker lifetime.
    """

    profile: JsRuntimeResourceProfile
    _pinned_files: dict[str, PinnedFileIdentity] = field(default_factory=dict, repr=False)
    _pinned_directories: dict[str, PinnedDirectoryIdentity] = field(default_factory=dict, repr=False)
    _pinned_fds: dict[str, int] = field(default_factory=dict, repr=False)
    _closed: bool = False
    _lease_registered: bool = field(default=False, repr=False)
    _authority_mode: str = field(default="source_dev", repr=False)
    _committed_manifest_sha256: str | None = field(default=None, repr=False)

    def __post_init__(self) -> None:
        if not self._pinned_fds and self._pinned_files:
            self._pinned_fds = {rel: pin.raw_handle for rel, pin in self._pinned_files.items()}

    @property
    def closed(self) -> bool:
        return self._closed

    @property
    def pinned_relpaths(self) -> tuple[str, ...]:
        if self._pinned_files:
            return tuple(sorted(self._pinned_files.keys()))
        return tuple(sorted(self._pinned_fds.keys()))

    @property
    def pinned_directory_relpaths(self) -> tuple[str, ...]:
        if not self._pinned_directories:
            return ()
        rest = sorted(k for k in self._pinned_directories if k != ".")
        return (".", *rest) if "." in self._pinned_directories else tuple(rest)

    @property
    def pinned_handle_count(self) -> int:
        if self._closed:
            return 0
        return len(self._pinned_files) if self._pinned_files else len(self._pinned_fds)

    @property
    def pinned_directory_count(self) -> int:
        if self._closed:
            return 0
        return len(self._pinned_directories)

    def get_pinned_file_identity(self, rel_path: str) -> PinnedFileIdentity | None:
        """Return the `PinnedFileIdentity` for `rel_path`, or None if not present/closed."""
        if self._closed:
            return None
        return self._pinned_files.get(rel_path)

    def get_pinned_directory_identity(self, rel_path: str) -> PinnedDirectoryIdentity | None:
        """Return the `PinnedDirectoryIdentity` for `rel_path` (e.g. '.' or 'package/src'), or None if closed."""
        if self._closed:
            return None
        return self._pinned_directories.get(rel_path)

    def revalidate_critical_files(self) -> None:
        """Re-verify all pinned directory and file handles and confirm critical runtime files match profile hashes."""
        if self._closed:
            raise JsResourceError("ryn_js_resource_lease_closed", "VerifiedJsResourceLease is already closed")
        file_map = {entry.path: entry for entry in self.profile.manifest.files}
        expected_dirs = expected_profile_directory_relpaths(self.profile.manifest)
        critical_set = {
            self.profile.manifest.node_executable_relpath,
            self.profile.manifest.worker_relpath,
            self.profile.manifest.sky_shim_relpath,
            self.profile.manifest.wasm_relpath,
        }
        if self._pinned_directories:
            if set(self._pinned_directories.keys()) != set(expected_dirs):
                raise JsResourceError(
                    "ryn_js_resource_directory_missing",
                    "Lease pinned directory set does not match full manifest directory tree",
                )
            for dir_rel in expected_dirs:
                dir_pin = self._pinned_directories.get(dir_rel)
                if dir_pin is None:
                    raise JsResourceError(
                        "ryn_js_resource_directory_missing",
                        f"Missing pinned directory handle for {dir_rel!r}",
                    )
                try:
                    revalidate_pinned_directory_handle(
                        dir_pin,
                        root_resolved=self.profile.profile_root,
                    )
                except JsProcessIdentityError as exc:
                    raise JsResourceError(exc.code, exc.message) from exc
                if dir_rel != ".":
                    parent_rel = _parent_directory_relpath(dir_rel)
                    parent_pin = self._pinned_directories.get(parent_rel)
                    if (
                        parent_pin is None
                        or dir_pin.volume_serial != parent_pin.volume_serial
                        or os.path.normcase(str(dir_pin.final_path.parent))
                        != os.path.normcase(str(parent_pin.final_path))
                    ):
                        raise JsResourceError(
                            "ryn_js_resource_directory_changed",
                            f"Pinned directory {dir_rel!r} parent lineage does not match pinned parent {parent_rel!r}",
                        )

        if self._pinned_files:
            if set(self._pinned_files.keys()) != set(file_map.keys()):
                raise JsResourceError(
                    "ryn_js_resource_file_missing",
                    "Lease pinned file set does not match full manifest file inventory",
                )
            for rel_path, entry in file_map.items():
                pin = self._pinned_files.get(rel_path)
                if pin is None:
                    raise JsResourceError("ryn_js_resource_file_missing", f"Missing pinned handle for {rel_path}")
                try:
                    revalidate_pinned_file_handle(
                        pin,
                        root_resolved=self.profile.profile_root,
                        verify_content_hash=(rel_path in critical_set),
                    )
                except JsProcessIdentityError as exc:
                    raise JsResourceError(exc.code, exc.message) from exc
                if self._pinned_directories:
                    parent_rel = _parent_directory_relpath(rel_path)
                    parent_pin = self._pinned_directories.get(parent_rel)
                    if (
                        parent_pin is None
                        or pin.volume_serial != parent_pin.volume_serial
                        or os.path.normcase(str(pin.final_path.parent))
                        != os.path.normcase(str(parent_pin.final_path))
                    ):
                        raise JsResourceError(
                            "ryn_js_resource_directory_changed",
                            f"Pinned file {rel_path!r} parent lineage does not match pinned parent {parent_rel!r}",
                        )
            return

        for rel_path in critical_set:
            entry = file_map.get(rel_path)
            if entry is None:
                raise JsResourceError("ryn_js_resource_file_missing", f"Missing manifest entry for {rel_path}")
            read_and_verify_contained_file(
                root_resolved=self.profile.profile_root,
                rel_path=rel_path,
                expected_sha256=entry.sha256,
                expected_size=entry.size,
                retain_bytes=False,
                keep_open_fd=False,
            )

    def close(self) -> None:
        """Release all pinned OS file and directory handles/descriptors idempotently."""
        if self._closed:
            return
        self._closed = True
        if self._lease_registered:
            self._lease_registered = False
            with _ACTIVE_LEASES_LOCK:
                _ACTIVE_LEASE_IDS.discard(id(self))
                _PRODUCTION_LEASE_AUTHORITIES.pop(id(self), None)
        pins = list(self._pinned_files.values())
        self._pinned_files.clear()
        dir_pins = list(self._pinned_directories.values())
        self._pinned_directories.clear()
        fds = list(self._pinned_fds.values())
        self._pinned_fds.clear()
        if pins:
            for pin in pins:
                close_pinned_raw_handle(pin.raw_handle, is_win32_handle=pin.is_win32_handle)
        else:
            for fd in fds:
                try:
                    os.close(fd)
                except OSError:
                    pass
        for dir_pin in dir_pins:
            close_pinned_raw_handle(dir_pin.raw_handle, is_win32_handle=dir_pin.is_win32_handle)

    def __enter__(self) -> VerifiedJsResourceLease:
        if self._closed:
            raise JsResourceError("ryn_js_resource_lease_closed", "Cannot enter a closed VerifiedJsResourceLease")
        return self

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        self.close()


def active_resource_lease_count() -> int:
    """Return the current number of open `VerifiedJsResourceLease` instances in this host process."""
    with _ACTIVE_LEASES_LOCK:
        return len(_ACTIVE_LEASE_IDS)


def is_verified_production_lease(
    lease: VerifiedJsResourceLease,
    *,
    expected_manifest_sha256: str | None = None,
    expected_profile_root: str | Path | None = None,
    expected_spec: ExpectedJsRuntimeProfileSpec | None = None,
) -> bool:
    """Return True only if `lease` is an authentic, unmutated production lease matching all bound paths and commitments."""
    if not isinstance(lease, VerifiedJsResourceLease) or lease.closed:
        return False
    if lease._authority_mode != "packaged_release" or lease.profile.mode != "packaged_release" or not lease.profile.is_production:
        return False
    if not lease._committed_manifest_sha256 or lease._committed_manifest_sha256 != lease.profile.profile_digest:
        return False
    with _ACTIVE_LEASES_LOCK:
        record = _PRODUCTION_LEASE_AUTHORITIES.get(id(lease))
    if record is None:
        return False
    prof = lease.profile
    mf = prof.manifest
    if (
        prof.profile_digest != record.profile_digest
        or mf.manifest_sha256 != record.profile_digest
        or prof.platform != record.platform
        or prof.arch != record.arch
        or prof.node_version != record.node_version
        or prof.node_sha256 != record.node_sha256
        or prof.node_bytes != record.node_bytes
        or prof.quickjs_version != record.quickjs_version
        or prof.quickjs_variant != record.quickjs_variant
        or prof.wire_protocol != record.wire_protocol
        or prof.worker_sha256 != record.worker_sha256
        or prof.sky_shim_sha256 != record.sky_shim_sha256
        or prof.wasm_sha256 != record.wasm_sha256
        or mf.total_files != record.total_files
        or mf.total_bytes != record.total_bytes
    ):
        return False
    for actual_path, bound_path in (
        (prof.profile_root, record.profile_root),
        (prof.node_executable, record.node_executable),
        (prof.worker_path, record.worker_path),
        (prof.sky_shim_path, record.sky_shim_path),
        (prof.package_root, record.package_root),
        (prof.wasm_path, record.wasm_path),
        (prof.manifest_path, record.manifest_path),
    ):
        if os.path.normcase(str(actual_path)) != os.path.normcase(str(bound_path)):
            return False

    # Verify pinned directory and file handles match the bound authority record
    root_dir_pin = lease._pinned_directories.get(".")
    pkg_dir_pin = lease._pinned_directories.get("package")
    node_pin = lease._pinned_files.get(mf.node_executable_relpath)
    worker_pin = lease._pinned_files.get(mf.worker_relpath)
    shim_pin = lease._pinned_files.get(mf.sky_shim_relpath)
    wasm_pin = lease._pinned_files.get(mf.wasm_relpath)
    if (
        root_dir_pin is None
        or pkg_dir_pin is None
        or node_pin is None
        or worker_pin is None
        or shim_pin is None
        or wasm_pin is None
    ):
        return False
    if (
        os.path.normcase(str(root_dir_pin.final_path)) != os.path.normcase(str(record.profile_root))
        or os.path.normcase(str(pkg_dir_pin.final_path)) != os.path.normcase(str(record.package_root))
        or os.path.normcase(str(node_pin.final_path)) != os.path.normcase(str(record.node_executable))
        or os.path.normcase(str(worker_pin.final_path)) != os.path.normcase(str(record.worker_path))
        or os.path.normcase(str(shim_pin.final_path)) != os.path.normcase(str(record.sky_shim_path))
        or os.path.normcase(str(wasm_pin.final_path)) != os.path.normcase(str(record.wasm_path))
        or node_pin.sha256 != record.node_sha256
        or node_pin.size != record.node_bytes
        or worker_pin.sha256 != record.worker_sha256
        or shim_pin.sha256 != record.sky_shim_sha256
        or wasm_pin.sha256 != record.wasm_sha256
    ):
        return False

    if expected_manifest_sha256 is not None and record.profile_digest != expected_manifest_sha256:
        return False
    if expected_profile_root is not None:
        try:
            norm_expected_root = os.path.normcase(str(Path(os.path.abspath(expected_profile_root))))
        except Exception:
            return False
        if os.path.normcase(str(record.profile_root)) != norm_expected_root:
            return False
    if expected_spec is not None:
        if not isinstance(expected_spec, ExpectedJsRuntimeProfileSpec):
            return False
        if (
            record.profile_digest != expected_spec.expected_manifest_sha256
            or record.platform != expected_spec.platform
            or record.arch != expected_spec.arch
            or record.node_version != expected_spec.node_version
            or record.node_sha256 != expected_spec.node_sha256
            or record.node_bytes != expected_spec.node_bytes
            or record.quickjs_version != expected_spec.quickjs_version
            or record.quickjs_variant != expected_spec.quickjs_variant
            or record.wire_protocol != expected_spec.wire_protocol
        ):
            return False
        if expected_spec.worker_sha256 is not None and record.worker_sha256 != expected_spec.worker_sha256:
            return False
        if expected_spec.sky_shim_sha256 is not None and record.sky_shim_sha256 != expected_spec.sky_shim_sha256:
            return False
        if expected_spec.wasm_sha256 is not None and record.wasm_sha256 != expected_spec.wasm_sha256:
            return False
    return True


def sanitize_invocation_environment(env: dict[str, str]) -> dict[str, str]:
    """Return a copy of `env` with `NODE_OPTIONS`, `NODE_PATH`, and Node/provider overrides stripped.

    Does not mutate `os.environ` or the input dictionary.
    """
    if not isinstance(env, dict):
        raise JsResourceError("ryn_js_resource_invalid_env", "env must be a dictionary")
    cleaned: dict[str, str] = {}
    for key, val in env.items():
        if not isinstance(key, str) or not isinstance(val, str):
            continue
        upper = key.upper()
        if upper in _FORBIDDEN_INVOCATION_ENV_KEYS or upper.startswith("NODE_"):
            if upper != "NODE_NO_WARNINGS":
                continue
        cleaned[key] = val
    cleaned["NODE_NO_WARNINGS"] = "1"
    return cleaned


def _validate_package_exports_in_manifest(
    *,
    pkg_name: str,
    pkg_dir_rel: str,
    pkg_json_obj: dict[str, Any],
    manifest_file_map: dict[str, ResourceFileEntry],
    require_wasm: bool,
) -> None:
    """Verify that `main`, `module`, `types`, and `exports` targets in `package.json` exist in the manifest."""
    if pkg_json_obj.get("name") != pkg_name:
        raise JsResourceError(
            "ryn_js_resource_package_mismatch",
            f"Package in {pkg_dir_rel}/package.json has name {pkg_json_obj.get('name')!r}, expected {pkg_name!r}",
        )
    expected_ver = REQUIRED_PRODUCTION_PACKAGES.get(pkg_name)
    if expected_ver is not None and pkg_json_obj.get("version") != expected_ver:
        raise JsResourceError(
            "ryn_js_resource_version_mismatch",
            f"Package {pkg_name} has version {pkg_json_obj.get('version')!r}, expected {expected_ver!r}",
        )

    referenced_rel_targets: list[str] = []
    for top_field in ("main", "module", "types"):
        val = pkg_json_obj.get(top_field)
        if isinstance(val, str) and val.strip():
            referenced_rel_targets.append(val.strip())

    exports_field = pkg_json_obj.get("exports")
    if not isinstance(exports_field, dict) or not exports_field:
        raise JsResourceError(
            "ryn_js_resource_export_map_invalid",
            f"Package {pkg_name} is missing a valid exports map in package.json",
        )

    def _collect_export_targets(node: Any) -> None:
        if isinstance(node, str):
            referenced_rel_targets.append(node)
        elif isinstance(node, dict):
            for sub_key, sub_val in node.items():
                if not isinstance(sub_key, str):
                    raise JsResourceError("ryn_js_resource_export_map_invalid", f"Invalid export key in {pkg_name}")
                _collect_export_targets(sub_val)
        else:
            raise JsResourceError(
                "ryn_js_resource_export_map_invalid",
                f"Unsupported export map value in {pkg_name}: {type(node).__name__}",
            )

    _collect_export_targets(exports_field)
    if require_wasm:
        if "./wasm" not in exports_field or "./emscripten-module" not in exports_field or "./ffi" not in exports_field:
            raise JsResourceError(
                "ryn_js_resource_export_map_invalid",
                f"QuickJS variant package {pkg_name} must export './wasm', './ffi', and './emscripten-module'",
            )

    saw_wasm_file = False
    for rel_target in referenced_rel_targets:
        norm_target = rel_target[2:] if rel_target.startswith("./") else rel_target
        full_rel = validate_relative_resource_path(
            f"{pkg_dir_rel}/{norm_target}",
            field_name=f"{pkg_name}.exports",
        )
        if full_rel not in manifest_file_map:
            raise JsResourceError(
                "ryn_js_resource_closure_incomplete",
                f"Exported file {full_rel!r} declared by {pkg_name} is missing from manifest files",
            )
        if full_rel.endswith(".wasm"):
            saw_wasm_file = True

    if require_wasm and not saw_wasm_file:
        raise JsResourceError(
            "ryn_js_resource_wasm_missing",
            f"QuickJS variant package {pkg_name} did not resolve a .wasm file in its export closure",
        )


def validate_manifest_dict(manifest_data: dict[str, Any]) -> JsRuntimeResourceManifest:
    """Strictly validate a parsed manifest dictionary against schema version 1 invariants."""
    if not isinstance(manifest_data, dict):
        raise JsResourceError("ryn_js_resource_invalid_manifest", "Manifest must be a dictionary")

    unknown_top = set(manifest_data.keys()) - _TOP_LEVEL_MANIFEST_KEYS
    if unknown_top:
        raise JsResourceError(
            "ryn_js_resource_unknown_field",
            f"Manifest contains unknown top-level fields: {sorted(unknown_top)}",
        )
    missing_top = _TOP_LEVEL_MANIFEST_KEYS - set(manifest_data.keys())
    if missing_top:
        raise JsResourceError(
            "ryn_js_resource_missing_field",
            f"Manifest is missing required top-level fields: {sorted(missing_top)}",
        )

    schema_version = manifest_data["schema_version"]
    if type(schema_version) is not int or schema_version != MANIFEST_SCHEMA_VERSION:
        raise JsResourceError(
            "ryn_js_resource_schema_mismatch",
            f"Unsupported manifest schema_version: {schema_version!r} (expected {MANIFEST_SCHEMA_VERSION})",
        )

    profile_id = manifest_data["profile_id"]
    if not isinstance(profile_id, str) or not PROFILE_ID_RE.fullmatch(profile_id):
        raise JsResourceError(
            "ryn_js_resource_invalid_manifest",
            f"Invalid profile_id: {profile_id!r}",
        )

    platform_name = manifest_data["platform"]
    if not isinstance(platform_name, str) or platform_name not in ALLOWED_PLATFORMS:
        raise JsResourceError(
            "ryn_js_resource_invalid_platform",
            f"Unsupported manifest platform: {platform_name!r}",
        )

    arch_name = manifest_data["arch"]
    if not isinstance(arch_name, str) or arch_name not in ALLOWED_ARCHES:
        raise JsResourceError(
            "ryn_js_resource_invalid_arch",
            f"Unsupported manifest arch: {arch_name!r}",
        )

    expected_profile_prefix = f"{platform_name}-{arch_name}"
    if not (profile_id == expected_profile_prefix or profile_id.startswith(f"{expected_profile_prefix}-")):
        raise JsResourceError(
            "ryn_js_resource_invalid_manifest",
            f"profile_id {profile_id!r} must match platform-arch prefix {expected_profile_prefix!r}",
        )

    node_version = validate_semver(manifest_data["node_version"], "node_version")
    node_exec_rel = validate_relative_resource_path(
        manifest_data["node_executable_relpath"],
        field_name="node_executable_relpath",
    )
    if node_exec_rel != expected_node_executable_relpath(platform_name):
        raise JsResourceError(
            "ryn_js_resource_invalid_path",
            f"node_executable_relpath must be {expected_node_executable_relpath(platform_name)!r} for {platform_name}",
        )
    node_sha256 = validate_sha256_hex(manifest_data["node_sha256"], "node_sha256")
    node_bytes = manifest_data["node_bytes"]
    if type(node_bytes) is not int or node_bytes <= 0 or node_bytes > MAX_SINGLE_RESOURCE_FILE_BYTES:
        raise JsResourceError("ryn_js_resource_invalid_size", f"Invalid node_bytes: {node_bytes!r}")

    quickjs_version = validate_semver(manifest_data["quickjs_version"], "quickjs_version")
    if quickjs_version != PINNED_QUICKJS_VERSION:
        raise JsResourceError(
            "ryn_js_resource_version_mismatch",
            f"quickjs_version must be {PINNED_QUICKJS_VERSION!r}, got {quickjs_version!r}",
        )

    quickjs_variant = manifest_data["quickjs_variant"]
    if quickjs_variant != PINNED_QUICKJS_VARIANT:
        raise JsResourceError(
            "ryn_js_resource_variant_mismatch",
            f"quickjs_variant must be {PINNED_QUICKJS_VARIANT!r}, got {quickjs_variant!r}",
        )

    wire_protocol = manifest_data["wire_protocol"]
    if type(wire_protocol) is not int or wire_protocol != PINNED_WIRE_PROTOCOL:
        raise JsResourceError(
            "ryn_js_resource_protocol_mismatch",
            f"wire_protocol must be integer {PINNED_WIRE_PROTOCOL}, got {wire_protocol!r}",
        )

    worker_relpath = validate_relative_resource_path(manifest_data["worker_relpath"], field_name="worker_relpath")
    if worker_relpath != WORKER_REL_PATH:
        raise JsResourceError(
            "ryn_js_resource_invalid_path",
            f"worker_relpath must be {WORKER_REL_PATH!r}, got {worker_relpath!r}",
        )
    worker_sha256 = validate_sha256_hex(manifest_data["worker_sha256"], "worker_sha256")

    sky_shim_relpath = validate_relative_resource_path(manifest_data["sky_shim_relpath"], field_name="sky_shim_relpath")
    if sky_shim_relpath != SKY_SHIM_REL_PATH:
        raise JsResourceError(
            "ryn_js_resource_invalid_path",
            f"sky_shim_relpath must be {SKY_SHIM_REL_PATH!r}, got {sky_shim_relpath!r}",
        )
    sky_shim_sha256 = validate_sha256_hex(manifest_data["sky_shim_sha256"], "sky_shim_sha256")

    wasm_relpath = validate_relative_resource_path(manifest_data["wasm_relpath"], field_name="wasm_relpath")
    if wasm_relpath != RELEASE_SYNC_WASM_REL_PATH:
        raise JsResourceError(
            "ryn_js_resource_invalid_path",
            f"wasm_relpath must be {RELEASE_SYNC_WASM_REL_PATH!r}, got {wasm_relpath!r}",
        )
    wasm_sha256 = validate_sha256_hex(manifest_data["wasm_sha256"], "wasm_sha256")

    total_files = manifest_data["total_files"]
    if type(total_files) is not int or total_files <= 0 or total_files > MAX_PROFILE_FILE_COUNT:
        raise JsResourceError(
            "ryn_js_resource_file_count_exceeded",
            f"total_files must be between 1 and {MAX_PROFILE_FILE_COUNT}, got {total_files!r}",
        )

    total_bytes = manifest_data["total_bytes"]
    if type(total_bytes) is not int or total_bytes <= 0 or total_bytes > MAX_AGGREGATE_PROFILE_BYTES:
        raise JsResourceError(
            "ryn_js_resource_aggregate_bytes_exceeded",
            f"total_bytes must be between 1 and {MAX_AGGREGATE_PROFILE_BYTES}, got {total_bytes!r}",
        )

    raw_files = manifest_data["files"]
    if not isinstance(raw_files, list) or not raw_files:
        raise JsResourceError("ryn_js_resource_invalid_manifest", "files must be a non-empty list")
    if len(raw_files) > MAX_PROFILE_FILE_COUNT:
        raise JsResourceError(
            "ryn_js_resource_file_count_exceeded",
            f"files list length {len(raw_files)} exceeds cap {MAX_PROFILE_FILE_COUNT}",
        )
    if len(raw_files) != total_files:
        raise JsResourceError(
            "ryn_js_resource_invalid_manifest",
            f"total_files ({total_files}) does not match files list length ({len(raw_files)})",
        )

    file_entries: list[ResourceFileEntry] = []
    file_map: dict[str, ResourceFileEntry] = {}
    seen_lower_paths: set[str] = set()
    computed_total_bytes = 0
    prev_path: str | None = None

    for idx, item in enumerate(raw_files):
        if not isinstance(item, dict):
            raise JsResourceError("ryn_js_resource_invalid_manifest", f"files[{idx}] must be an object")
        extra_keys = set(item.keys()) - _FILE_ENTRY_KEYS
        if extra_keys:
            raise JsResourceError(
                "ryn_js_resource_unknown_field",
                f"files[{idx}] contains unknown fields: {sorted(extra_keys)}",
            )
        missing_keys = _FILE_ENTRY_KEYS - set(item.keys())
        if missing_keys:
            raise JsResourceError(
                "ryn_js_resource_missing_field",
                f"files[{idx}] is missing required fields: {sorted(missing_keys)}",
            )
        f_path = validate_relative_resource_path(item["path"], field_name=f"files[{idx}].path")
        if f_path in {MANIFEST_FILENAME, BUILD_RECEIPT_FILENAME}:
            raise JsResourceError(
                "ryn_js_resource_invalid_path",
                f"files[{idx}] must not self-reference {f_path!r}",
            )
        lower_path = f_path.lower()
        if lower_path in seen_lower_paths:
            raise JsResourceError(
                "ryn_js_resource_duplicate_file_entry",
                f"Duplicate or case-colliding file path in manifest: {f_path!r}",
            )
        seen_lower_paths.add(lower_path)
        if prev_path is not None and f_path <= prev_path:
            raise JsResourceError(
                "ryn_js_resource_unsorted_files",
                f"Manifest files must be sorted lexicographically by path ({prev_path!r} before {f_path!r})",
            )
        prev_path = f_path

        f_size = item["size"]
        if type(f_size) is not int or f_size <= 0 or f_size > MAX_SINGLE_RESOURCE_FILE_BYTES:
            raise JsResourceError(
                "ryn_js_resource_invalid_size",
                f"files[{idx}] ({f_path}) size must be between 1 and {MAX_SINGLE_RESOURCE_FILE_BYTES}",
            )
        f_sha = validate_sha256_hex(item["sha256"], f"files[{idx}].sha256")
        computed_total_bytes += f_size
        if computed_total_bytes > MAX_AGGREGATE_PROFILE_BYTES:
            raise JsResourceError(
                "ryn_js_resource_aggregate_bytes_exceeded",
                f"Aggregate file bytes exceed limit {MAX_AGGREGATE_PROFILE_BYTES}",
            )
        entry = ResourceFileEntry(path=f_path, size=f_size, sha256=f_sha)
        file_entries.append(entry)
        file_map[f_path] = entry

    if computed_total_bytes != total_bytes:
        raise JsResourceError(
            "ryn_js_resource_invalid_manifest",
            f"total_bytes ({total_bytes}) does not equal sum of file sizes ({computed_total_bytes})",
        )

    # Cross-check pinned top-level file pointers against the files inventory
    for rel_key, expected_sha, expected_sz in (
        (node_exec_rel, node_sha256, node_bytes),
        (worker_relpath, worker_sha256, None),
        (sky_shim_relpath, sky_shim_sha256, None),
        (wasm_relpath, wasm_sha256, None),
    ):
        matched = file_map.get(rel_key)
        if matched is None:
            raise JsResourceError(
                "ryn_js_resource_file_missing",
                f"Required runtime file {rel_key!r} is missing from manifest files inventory",
            )
        if matched.sha256 != expected_sha:
            raise JsResourceError(
                "ryn_js_resource_hash_mismatch",
                f"Top-level hash for {rel_key!r} does not match manifest file entry",
            )
        if expected_sz is not None and matched.size != expected_sz:
            raise JsResourceError(
                "ryn_js_resource_size_mismatch",
                f"Top-level size for {rel_key!r} ({expected_sz}) does not match file entry ({matched.size})",
            )

    for required_meta_rel in (
        PACKAGE_JSON_REL_PATH,
        PACKAGE_LOCK_REL_PATH,
        NODE_LICENSE_REL_PATH,
    ):
        if required_meta_rel not in file_map:
            raise JsResourceError(
                "ryn_js_resource_file_missing",
                f"Required package/license file {required_meta_rel!r} is missing from manifest",
            )

    # Validate dependency_packages closure inventory
    raw_deps = manifest_data["dependency_packages"]
    if not isinstance(raw_deps, list) or len(raw_deps) != len(REQUIRED_PRODUCTION_PACKAGES):
        raise JsResourceError(
            "ryn_js_resource_closure_incomplete",
            f"dependency_packages must contain all {len(REQUIRED_PRODUCTION_PACKAGES)} pinned production packages",
        )

    dep_entries: list[ResourceDependencyPackageEntry] = []
    seen_dep_names: set[str] = set()
    prev_dep_name: str | None = None
    for idx, item in enumerate(raw_deps):
        if not isinstance(item, dict):
            raise JsResourceError("ryn_js_resource_invalid_manifest", f"dependency_packages[{idx}] must be an object")
        extra_keys = set(item.keys()) - _DEPENDENCY_PACKAGE_ENTRY_KEYS
        if extra_keys:
            raise JsResourceError(
                "ryn_js_resource_unknown_field",
                f"dependency_packages[{idx}] has unknown fields: {sorted(extra_keys)}",
            )
        missing_keys = _DEPENDENCY_PACKAGE_ENTRY_KEYS - set(item.keys())
        if missing_keys:
            raise JsResourceError(
                "ryn_js_resource_missing_field",
                f"dependency_packages[{idx}] is missing fields: {sorted(missing_keys)}",
            )
        pkg_name = item["name"]
        if not isinstance(pkg_name, str) or pkg_name not in REQUIRED_PRODUCTION_PACKAGES:
            raise JsResourceError(
                "ryn_js_resource_unexpected_package",
                f"Unexpected or unpinned dependency package in manifest: {pkg_name!r}",
            )
        if pkg_name in seen_dep_names:
            raise JsResourceError("ryn_js_resource_duplicate_package", f"Duplicate dependency package: {pkg_name!r}")
        seen_dep_names.add(pkg_name)
        if prev_dep_name is not None and pkg_name <= prev_dep_name:
            raise JsResourceError(
                "ryn_js_resource_invalid_manifest",
                "dependency_packages must be sorted lexicographically by package name",
            )
        prev_dep_name = pkg_name

        pkg_ver = validate_semver(item["version"], f"dependency_packages[{idx}].version")
        if pkg_ver != REQUIRED_PRODUCTION_PACKAGES[pkg_name]:
            raise JsResourceError(
                "ryn_js_resource_version_mismatch",
                f"Package {pkg_name} version must be {REQUIRED_PRODUCTION_PACKAGES[pkg_name]!r}, got {pkg_ver!r}",
            )
        integrity = item["integrity"]
        if not isinstance(integrity, str) or not integrity.startswith("sha512-") or len(integrity) < 24:
            raise JsResourceError(
                "ryn_js_resource_invalid_manifest",
                f"Package {pkg_name} must have a valid sha512 lockfile integrity string",
            )
        pkg_license = item["license"]
        if not isinstance(pkg_license, str) or not pkg_license.strip():
            raise JsResourceError("ryn_js_resource_license_missing", f"Package {pkg_name} license string is empty")
        pkg_json_rel = validate_relative_resource_path(
            item["package_json_path"],
            field_name=f"dependency_packages[{idx}].package_json_path",
        )
        expected_pkg_json_rel = f"package/node_modules/{pkg_name}/package.json"
        if pkg_json_rel != expected_pkg_json_rel or pkg_json_rel not in file_map:
            raise JsResourceError(
                "ryn_js_resource_closure_incomplete",
                f"Package {pkg_name} package.json path {pkg_json_rel!r} is invalid or missing from files",
            )
        lic_rel = validate_relative_resource_path(
            item["license_path"],
            field_name=f"dependency_packages[{idx}].license_path",
        )
        expected_lic_rel = f"package/node_modules/{pkg_name}/LICENSE"
        if lic_rel != expected_lic_rel or lic_rel not in file_map:
            raise JsResourceError(
                "ryn_js_resource_license_missing",
                f"Package {pkg_name} LICENSE path {lic_rel!r} is invalid or missing from files",
            )
        dep_entries.append(
            ResourceDependencyPackageEntry(
                name=pkg_name,
                version=pkg_ver,
                integrity=integrity,
                license=pkg_license.strip(),
                package_json_path=pkg_json_rel,
                license_path=lic_rel,
            )
        )

    if seen_dep_names != set(REQUIRED_PRODUCTION_PACKAGES.keys()):
        raise JsResourceError(
            "ryn_js_resource_closure_incomplete",
            "Manifest dependency_packages does not match exact pinned production closure",
        )

    # Ensure all 4 QuickJS variant WASM files + dist modules exist in manifest files
    for variant_pkg in QUICKJS_VARIANT_PACKAGES:
        for req_suffix in (
            "package.json",
            "LICENSE",
            "dist/index.mjs",
            "dist/ffi.mjs",
            "dist/emscripten-module.mjs",
            "dist/emscripten-module.wasm",
        ):
            v_rel = f"package/node_modules/{variant_pkg}/{req_suffix}"
            if v_rel not in file_map:
                raise JsResourceError(
                    "ryn_js_resource_wasm_missing" if req_suffix.endswith(".wasm") else "ryn_js_resource_closure_incomplete",
                    f"Required QuickJS variant file {v_rel!r} is missing from manifest",
                )

    # Check that no unpinned package directories exist under package/node_modules/
    allowed_nm_prefixes = tuple(f"package/node_modules/{pkg}/" for pkg in REQUIRED_PRODUCTION_PACKAGES)
    for f_path in file_map:
        if f_path.startswith("package/node_modules/"):
            if not f_path.startswith(allowed_nm_prefixes):
                raise JsResourceError(
                    "ryn_js_resource_unexpected_package",
                    f"Manifest contains file under unpinned node_modules package: {f_path!r}",
                )

    # Validate licenses inventory
    raw_licenses = manifest_data["licenses"]
    if not isinstance(raw_licenses, list) or not raw_licenses or len(raw_licenses) > MAX_LICENSE_ENTRIES:
        raise JsResourceError(
            "ryn_js_resource_license_missing",
            "licenses must be a non-empty bounded list",
        )
    license_entries: list[ResourceLicenseEntry] = []
    seen_license_paths: set[str] = set()
    saw_node_license = False
    for idx, item in enumerate(raw_licenses):
        if not isinstance(item, dict):
            raise JsResourceError("ryn_js_resource_invalid_manifest", f"licenses[{idx}] must be an object")
        extra_keys = set(item.keys()) - _LICENSE_ENTRY_KEYS
        if extra_keys:
            raise JsResourceError(
                "ryn_js_resource_unknown_field",
                f"licenses[{idx}] has unknown fields: {sorted(extra_keys)}",
            )
        missing_keys = _LICENSE_ENTRY_KEYS - set(item.keys())
        if missing_keys:
            raise JsResourceError(
                "ryn_js_resource_missing_field",
                f"licenses[{idx}] is missing fields: {sorted(missing_keys)}",
            )
        comp = item["component"]
        if not isinstance(comp, str) or not comp.strip():
            raise JsResourceError("ryn_js_resource_invalid_manifest", f"licenses[{idx}].component must be non-empty")
        ver = validate_semver(item["version"], f"licenses[{idx}].version")
        lic_name = item["license"]
        if not isinstance(lic_name, str) or not lic_name.strip():
            raise JsResourceError("ryn_js_resource_license_missing", f"licenses[{idx}].license must be non-empty")
        l_path = validate_relative_resource_path(item["path"], field_name=f"licenses[{idx}].path")
        if l_path in seen_license_paths:
            raise JsResourceError("ryn_js_resource_duplicate_file_entry", f"Duplicate license path: {l_path!r}")
        seen_license_paths.add(l_path)
        l_sha = validate_sha256_hex(item["sha256"], f"licenses[{idx}].sha256")
        l_size = item["size"]
        if type(l_size) is not int or l_size <= 0:
            raise JsResourceError("ryn_js_resource_invalid_size", f"licenses[{idx}].size must be positive")
        matched_file = file_map.get(l_path)
        if matched_file is None or matched_file.sha256 != l_sha or matched_file.size != l_size:
            raise JsResourceError(
                "ryn_js_resource_license_missing",
                f"License entry {l_path!r} does not match manifest files inventory",
            )
        if comp == "node" and l_path == NODE_LICENSE_REL_PATH:
            saw_node_license = True
        license_entries.append(
            ResourceLicenseEntry(
                component=comp.strip(),
                version=ver,
                license=lic_name.strip(),
                path=l_path,
                sha256=l_sha,
                size=l_size,
            )
        )

    if not saw_node_license:
        raise JsResourceError(
            "ryn_js_resource_license_missing",
            f"Manifest licenses must include Node license at {NODE_LICENSE_REL_PATH!r}",
        )
    for dep in dep_entries:
        if dep.license_path not in seen_license_paths:
            raise JsResourceError(
                "ryn_js_resource_license_missing",
                f"Manifest licenses is missing license file {dep.license_path!r} for {dep.name}",
            )

    canonical_dict = {
        "schema_version": schema_version,
        "profile_id": profile_id,
        "platform": platform_name,
        "arch": arch_name,
        "node_version": node_version,
        "node_executable_relpath": node_exec_rel,
        "node_sha256": node_sha256,
        "node_bytes": node_bytes,
        "quickjs_version": quickjs_version,
        "quickjs_variant": quickjs_variant,
        "wire_protocol": wire_protocol,
        "worker_relpath": worker_relpath,
        "worker_sha256": worker_sha256,
        "sky_shim_relpath": sky_shim_relpath,
        "sky_shim_sha256": sky_shim_sha256,
        "wasm_relpath": wasm_relpath,
        "wasm_sha256": wasm_sha256,
        "total_files": total_files,
        "total_bytes": total_bytes,
        "files": [e.to_dict() for e in file_entries],
        "licenses": [e.to_dict() for e in license_entries],
        "dependency_packages": [e.to_dict() for e in dep_entries],
    }
    digest = compute_manifest_digest(canonical_dict)

    return JsRuntimeResourceManifest(
        schema_version=schema_version,
        profile_id=profile_id,
        platform=platform_name,
        arch=arch_name,
        node_version=node_version,
        node_executable_relpath=node_exec_rel,
        node_sha256=node_sha256,
        node_bytes=node_bytes,
        quickjs_version=quickjs_version,
        quickjs_variant=quickjs_variant,
        wire_protocol=wire_protocol,
        worker_relpath=worker_relpath,
        worker_sha256=worker_sha256,
        sky_shim_relpath=sky_shim_relpath,
        sky_shim_sha256=sky_shim_sha256,
        wasm_relpath=wasm_relpath,
        wasm_sha256=wasm_sha256,
        total_files=total_files,
        total_bytes=total_bytes,
        files=tuple(file_entries),
        licenses=tuple(license_entries),
        dependency_packages=tuple(dep_entries),
        manifest_sha256=digest,
    )


def _verify_no_unexpected_files_in_root(
    root_resolved: Path,
    expected_rel_paths: set[str],
    expected_dir_rel_paths: set[str] | None = None,
) -> None:
    """Walk `root_resolved` without following symlinks and reject any unexpected files, directories, or reparse points."""
    allowed_Meta = {MANIFEST_FILENAME, BUILD_RECEIPT_FILENAME}
    seen_files: set[str] = set()
    seen_dirs: set[str] = set()

    for dirpath, dirnames, filenames in os.walk(root_resolved, topdown=True, followlinks=False):
        curr_dir = Path(dirpath)
        if is_symlink_or_reparse(curr_dir):
            raise JsResourceError(
                "ryn_js_resource_reparse_forbidden",
                f"Directory inside resource profile is a symlink or reparse point: {curr_dir}",
            )
        curr_dir_rel = "." if curr_dir == root_resolved else curr_dir.relative_to(root_resolved).as_posix()
        if expected_dir_rel_paths is not None and curr_dir_rel not in expected_dir_rel_paths:
            raise JsResourceError(
                "ryn_js_resource_unexpected_directory",
                f"Resource profile directory contains unexpected subdirectory: {curr_dir_rel!r}",
            )
        seen_dirs.add(curr_dir_rel)
        for dname in dirnames:
            dpath = curr_dir / dname
            if is_symlink_or_reparse(dpath):
                raise JsResourceError(
                    "ryn_js_resource_reparse_forbidden",
                    f"Subdirectory inside resource profile is a symlink or reparse point: {dpath}",
                )
        for fname in filenames:
            fpath = curr_dir / fname
            if is_symlink_or_reparse(fpath):
                raise JsResourceError(
                    "ryn_js_resource_reparse_forbidden",
                    f"File inside resource profile is a symlink or reparse point: {fpath}",
                )
            rel = fpath.relative_to(root_resolved).as_posix()
            if rel in allowed_Meta:
                continue
            if rel not in expected_rel_paths:
                raise JsResourceError(
                    "ryn_js_resource_unexpected_file",
                    f"Resource profile directory contains unlisted file: {rel!r}",
                )
            seen_files.add(rel)

    if expected_dir_rel_paths is not None:
        missing_dirs = expected_dir_rel_paths - seen_dirs
        if missing_dirs:
            raise JsResourceError(
                "ryn_js_resource_directory_missing",
                f"Resource profile is missing expected directories on disk: {sorted(missing_dirs)}",
            )

    missing = expected_rel_paths - seen_files
    if missing:
        raise JsResourceError(
            "ryn_js_resource_file_missing",
            f"Resource profile is missing expected files on disk: {sorted(missing)}",
        )


def _verify_lockfile_and_exports_from_disk_bytes(
    *,
    manifest: JsRuntimeResourceManifest,
    retained_json_bytes: dict[str, bytes],
) -> None:
    """Cross-verify `package/package.json`, `package/package-lock.json`, and each dependency `package.json`."""
    file_map = {entry.path: entry for entry in manifest.files}
    root_pkg_raw = retained_json_bytes.get(PACKAGE_JSON_REL_PATH)
    lock_raw = retained_json_bytes.get(PACKAGE_LOCK_REL_PATH)
    if root_pkg_raw is None or lock_raw is None:
        raise JsResourceError("ryn_js_resource_file_missing", "Missing package.json or package-lock.json bytes")

    root_pkg = parse_strict_bounded_json(root_pkg_raw, label=PACKAGE_JSON_REL_PATH)
    if root_pkg.get("name") != PINNED_RUNTIME_PACKAGE_NAME or root_pkg.get("type") != "module":
        raise JsResourceError(
            "ryn_js_resource_package_mismatch",
            f"{PACKAGE_JSON_REL_PATH} must be ESM package {PINNED_RUNTIME_PACKAGE_NAME!r}",
        )
    deps = root_pkg.get("dependencies")
    if not isinstance(deps, dict) or deps != {"quickjs-emscripten": PINNED_QUICKJS_VERSION}:
        raise JsResourceError(
            "ryn_js_resource_package_mismatch",
            f"{PACKAGE_JSON_REL_PATH} dependencies must strictly pin quickjs-emscripten@{PINNED_QUICKJS_VERSION}",
        )

    lock_obj = parse_strict_bounded_json(lock_raw, label=PACKAGE_LOCK_REL_PATH)
    if lock_obj.get("lockfileVersion") != 3 or not isinstance(lock_obj.get("packages"), dict):
        raise JsResourceError(
            "ryn_js_resource_lockfile_invalid",
            f"{PACKAGE_LOCK_REL_PATH} must be a valid lockfileVersion 3 file",
        )
    lock_packages: dict[str, Any] = lock_obj["packages"]
    expected_lock_keys = {""} | {f"node_modules/{name}" for name in REQUIRED_PRODUCTION_PACKAGES}
    if set(lock_packages.keys()) != expected_lock_keys:
        raise JsResourceError(
            "ryn_js_resource_lockfile_invalid",
            f"{PACKAGE_LOCK_REL_PATH} packages keys do not match exact pinned production closure",
        )

    dep_by_name = {d.name: d for d in manifest.dependency_packages}
    for pkg_name, expected_ver in REQUIRED_PRODUCTION_PACKAGES.items():
        lock_entry = lock_packages.get(f"node_modules/{pkg_name}")
        if not isinstance(lock_entry, dict):
            raise JsResourceError("ryn_js_resource_lockfile_invalid", f"Missing lock entry for {pkg_name}")
        if lock_entry.get("version") != expected_ver:
            raise JsResourceError(
                "ryn_js_resource_version_mismatch",
                f"Lockfile version for {pkg_name} is {lock_entry.get('version')!r}, expected {expected_ver!r}",
            )
        dep_record = dep_by_name[pkg_name]
        if lock_entry.get("integrity") != dep_record.integrity:
            raise JsResourceError(
                "ryn_js_resource_lockfile_invalid",
                f"Manifest integrity for {pkg_name} does not match package-lock.json",
            )
        pkg_json_raw = retained_json_bytes.get(dep_record.package_json_path)
        if pkg_json_raw is None:
            raise JsResourceError("ryn_js_resource_file_missing", f"Missing {dep_record.package_json_path}")
        pkg_json_obj = parse_strict_bounded_json(pkg_json_raw, label=dep_record.package_json_path)
        _validate_package_exports_in_manifest(
            pkg_name=pkg_name,
            pkg_dir_rel=f"package/node_modules/{pkg_name}",
            pkg_json_obj=pkg_json_obj,
            manifest_file_map=file_map,
            require_wasm=(pkg_name in QUICKJS_VARIANT_PACKAGES),
        )


def _open_and_verify_profile_directory(
    *,
    root_resolved: Path,
    rel_path: str,
    keep_open_pin: bool,
) -> PinnedDirectoryIdentity | None:
    """Verify lineage and optionally open a pinned directory handle for `rel_path` under `root_resolved`."""
    if rel_path == ".":
        target_resolved = root_resolved
    else:
        clean_rel = validate_relative_resource_path(rel_path, field_name="directory_path")
        target_resolved = root_resolved / Path(clean_rel)
    if not target_resolved.exists():
        raise JsResourceError("ryn_js_resource_directory_missing", f"Required directory is missing: {rel_path}")
    if is_symlink_or_reparse(target_resolved) or not target_resolved.is_dir():
        raise JsResourceError(
            "ryn_js_resource_reparse_forbidden",
            f"Directory is a symlink, reparse point, or not a regular directory: {rel_path}",
        )
    if os.name == "nt":
        try:
            return open_and_verify_pinned_win32_directory(
                target_resolved=target_resolved,
                root_resolved=root_resolved,
                rel_path=rel_path,
                keep_open=keep_open_pin,
            )
        except JsProcessIdentityError as exc:
            raise JsResourceError(exc.code, exc.message) from exc

    if not keep_open_pin:
        return None
    open_flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(target_resolved, open_flags)
    except OSError as exc:
        raise JsResourceError("ryn_js_resource_unreadable", f"Failed to open directory {rel_path}: {exc}") from exc
    try:
        st_fd = os.fstat(fd)
        if not stat.S_ISDIR(st_fd.st_mode):
            raise JsResourceError("ryn_js_resource_not_directory", f"Opened handle is not a directory: {rel_path}")
        return PinnedDirectoryIdentity(
            rel_path=rel_path,
            final_path=target_resolved,
            volume_serial=int(st_fd.st_dev),
            file_id=int(st_fd.st_ino),
            raw_handle=fd,
            is_win32_handle=False,
        )
    except BaseException:
        try:
            os.close(fd)
        except OSError:
            pass
        raise


def _open_and_verify_profile_entry(
    *,
    root_resolved: Path,
    rel_path: str,
    expected_sha256: str,
    expected_size: int,
    max_bytes: int,
    retain_bytes: bool,
    keep_open_pin: bool,
) -> tuple[str, int, bytes | None, PinnedFileIdentity | None, bytes]:
    """Verify lineage and hash a manifest file, optionally keeping a `PinnedFileIdentity` handle open."""
    target_resolved = verify_relative_lineage_under_root(root_resolved, rel_path)
    if os.name == "nt":
        try:
            actual_sha, actual_sz, raw_b, pin = open_and_verify_pinned_win32_file(
                target_resolved=target_resolved,
                root_resolved=root_resolved,
                rel_path=rel_path,
                expected_sha256=expected_sha256,
                expected_size=expected_size,
                max_bytes=max_bytes,
                retain_bytes=retain_bytes or rel_path.endswith(".wasm"),
                keep_open=keep_open_pin,
            )
        except JsProcessIdentityError as exc:
            raise JsResourceError(exc.code, exc.message) from exc
        head8 = (raw_b[:8] if raw_b is not None else b"")
        if not retain_bytes:
            raw_b = None
        return actual_sha, actual_sz, raw_b, pin, head8

    actual_sha, actual_sz, raw_b, opened_fd = read_and_verify_contained_file(
        root_resolved=root_resolved,
        rel_path=rel_path,
        expected_sha256=expected_sha256,
        expected_size=expected_size,
        max_bytes=max_bytes,
        retain_bytes=retain_bytes or rel_path.endswith(".wasm"),
        keep_open_fd=keep_open_pin,
    )
    head8 = (raw_b[:8] if raw_b is not None else b"")
    if not retain_bytes:
        raw_b = None
    pin = None
    if keep_open_pin and opened_fd is not None:
        st_fd = os.fstat(opened_fd)
        pin = PinnedFileIdentity(
            rel_path=rel_path,
            final_path=target_resolved,
            size=actual_sz,
            sha256=actual_sha,
            volume_serial=int(st_fd.st_dev),
            file_id=int(st_fd.st_ino),
            raw_handle=opened_fd,
            is_win32_handle=False,
        )
    return actual_sha, actual_sz, raw_b, pin, head8


def _resolve_and_verify_profile_internal(
    *,
    profile_root: str | Path,
    mode: str,
    expected_spec: ExpectedJsRuntimeProfileSpec | None,
    expected_platform: str | None,
    expected_arch: str | None,
    acquire_lease: bool,
) -> tuple[JsRuntimeResourceProfile, VerifiedJsResourceLease | None]:
    if mode not in ALLOWED_PROFILE_MODES:
        raise JsResourceError("ryn_js_resource_invalid_mode", f"Unsupported profile mode: {mode!r}")
    if mode == "packaged_release" and expected_spec is None:
        raise JsResourceError(
            "ryn_js_resource_missing_commitment",
            "Production packaged_release resolution requires a trusted ExpectedJsRuntimeProfileSpec",
        )
    if acquire_lease:
        with _ACTIVE_LEASES_LOCK:
            if len(_ACTIVE_LEASE_IDS) >= MAX_ACTIVE_RESOURCE_LEASES:
                raise JsResourceError(
                    "ryn_js_resource_lease_limit_exceeded",
                    f"Active resource lease count exceeds cap {MAX_ACTIVE_RESOURCE_LEASES}",
                )

    root_resolved = verify_directory_lineage_no_reparse(profile_root)
    _, _, manifest_bytes, _ = read_and_verify_contained_file(
        root_resolved=root_resolved,
        rel_path=MANIFEST_FILENAME,
        max_bytes=MAX_MANIFEST_BYTES,
        retain_bytes=True,
        keep_open_fd=False,
    )
    assert manifest_bytes is not None
    manifest_dict = parse_strict_bounded_json(
        manifest_bytes,
        max_bytes=MAX_MANIFEST_BYTES,
        max_depth=MAX_MANIFEST_JSON_DEPTH,
        label=MANIFEST_FILENAME,
    )
    manifest = validate_manifest_dict(manifest_dict)

    if len(manifest.files) > MAX_PINNED_HANDLES_PER_LEASE:
        raise JsResourceError(
            "ryn_js_resource_file_count_exceeded",
            f"Manifest file count {len(manifest.files)} exceeds handle pin cap {MAX_PINNED_HANDLES_PER_LEASE}",
        )
    expected_dirs = expected_profile_directory_relpaths(manifest)
    if len(expected_dirs) > MAX_PINNED_DIRECTORIES_PER_LEASE:
        raise JsResourceError(
            "ryn_js_resource_directory_count_exceeded",
            f"Profile directory count {len(expected_dirs)} exceeds directory pin cap {MAX_PINNED_DIRECTORIES_PER_LEASE}",
        )

    target_platform = expected_spec.platform if expected_spec is not None else expected_platform
    target_arch = expected_spec.arch if expected_spec is not None else expected_arch
    if target_platform is not None and manifest.platform != target_platform:
        raise JsResourceError(
            "ryn_js_resource_platform_mismatch",
            f"Profile platform {manifest.platform!r} does not match expected {target_platform!r}",
        )
    if target_arch is not None and manifest.arch != target_arch:
        raise JsResourceError(
            "ryn_js_resource_arch_mismatch",
            f"Profile arch {manifest.arch!r} does not match expected {target_arch!r}",
        )

    if expected_spec is not None:
        if manifest.manifest_sha256 != expected_spec.expected_manifest_sha256:
            raise JsResourceError(
                "ryn_js_resource_manifest_digest_mismatch",
                f"Manifest digest {manifest.manifest_sha256} does not match trusted expected digest "
                f"{expected_spec.expected_manifest_sha256}",
            )
        if manifest.node_version != expected_spec.node_version:
            raise JsResourceError(
                "ryn_js_resource_version_mismatch",
                f"Manifest node_version {manifest.node_version!r} != expected {expected_spec.node_version!r}",
            )
        if manifest.node_sha256 != expected_spec.node_sha256:
            raise JsResourceError(
                "ryn_js_resource_hash_mismatch",
                f"Manifest node_sha256 {manifest.node_sha256} != expected {expected_spec.node_sha256}",
            )
        if manifest.node_bytes != expected_spec.node_bytes:
            raise JsResourceError(
                "ryn_js_resource_size_mismatch",
                f"Manifest node_bytes {manifest.node_bytes} != expected {expected_spec.node_bytes}",
            )
        if expected_spec.worker_sha256 is not None and manifest.worker_sha256 != expected_spec.worker_sha256:
            raise JsResourceError(
                "ryn_js_resource_hash_mismatch",
                f"Manifest worker_sha256 {manifest.worker_sha256} != expected {expected_spec.worker_sha256}",
            )
        if expected_spec.sky_shim_sha256 is not None and manifest.sky_shim_sha256 != expected_spec.sky_shim_sha256:
            raise JsResourceError(
                "ryn_js_resource_hash_mismatch",
                f"Manifest sky_shim_sha256 {manifest.sky_shim_sha256} != expected {expected_spec.sky_shim_sha256}",
            )
        if expected_spec.wasm_sha256 is not None and manifest.wasm_sha256 != expected_spec.wasm_sha256:
            raise JsResourceError(
                "ryn_js_resource_hash_mismatch",
                f"Manifest wasm_sha256 {manifest.wasm_sha256} != expected {expected_spec.wasm_sha256}",
            )

    expected_rel_paths = {entry.path for entry in manifest.files}
    _verify_no_unexpected_files_in_root(root_resolved, expected_rel_paths, set(expected_dirs))

    json_paths_to_retain = {
        PACKAGE_JSON_REL_PATH,
        PACKAGE_LOCK_REL_PATH,
        *(dep.package_json_path for dep in manifest.dependency_packages),
    }
    variant_wasm_paths = {
        f"package/node_modules/{variant_pkg}/dist/emscripten-module.wasm"
        for variant_pkg in QUICKJS_VARIANT_PACKAGES
    }

    retained_json_bytes: dict[str, bytes] = {}
    pinned_directories: dict[str, PinnedDirectoryIdentity] = {}
    pinned_files: dict[str, PinnedFileIdentity] = {}
    wasm_headers: dict[str, bytes] = {}
    verified_bytes_sum = 0

    try:
        for dir_rel in expected_dirs:
            dir_pin = _open_and_verify_profile_directory(
                root_resolved=root_resolved,
                rel_path=dir_rel,
                keep_open_pin=acquire_lease,
            )
            if acquire_lease and dir_pin is not None:
                if dir_rel != ".":
                    parent_rel = _parent_directory_relpath(dir_rel)
                    parent_pin = pinned_directories.get(parent_rel)
                    if (
                        parent_pin is None
                        or dir_pin.volume_serial != parent_pin.volume_serial
                        or os.path.normcase(str(dir_pin.final_path.parent))
                        != os.path.normcase(str(parent_pin.final_path))
                    ):
                        close_pinned_raw_handle(dir_pin.raw_handle, is_win32_handle=dir_pin.is_win32_handle)
                        raise JsResourceError(
                            "ryn_js_resource_directory_changed",
                            f"Pinned directory {dir_rel!r} parent lineage does not match pinned parent {parent_rel!r}",
                        )
                pinned_directories[dir_rel] = dir_pin

        for entry in manifest.files:
            want_bytes = entry.path in json_paths_to_retain
            _, actual_sz, raw_b, pin, head8 = _open_and_verify_profile_entry(
                root_resolved=root_resolved,
                rel_path=entry.path,
                expected_sha256=entry.sha256,
                expected_size=entry.size,
                max_bytes=MAX_SINGLE_RESOURCE_FILE_BYTES,
                retain_bytes=want_bytes,
                keep_open_pin=acquire_lease,
            )
            verified_bytes_sum += actual_sz
            if verified_bytes_sum > MAX_AGGREGATE_PROFILE_BYTES:
                if pin is not None:
                    close_pinned_raw_handle(pin.raw_handle, is_win32_handle=pin.is_win32_handle)
                raise JsResourceError(
                    "ryn_js_resource_aggregate_bytes_exceeded",
                    f"Verified bytes exceed aggregate cap {MAX_AGGREGATE_PROFILE_BYTES}",
                )
            if want_bytes and raw_b is not None:
                retained_json_bytes[entry.path] = raw_b
            if entry.path in variant_wasm_paths:
                wasm_headers[entry.path] = head8
            if acquire_lease and pin is not None:
                parent_rel = _parent_directory_relpath(entry.path)
                parent_pin = pinned_directories.get(parent_rel)
                if (
                    parent_pin is None
                    or pin.volume_serial != parent_pin.volume_serial
                    or os.path.normcase(str(pin.final_path.parent))
                    != os.path.normcase(str(parent_pin.final_path))
                ):
                    close_pinned_raw_handle(pin.raw_handle, is_win32_handle=pin.is_win32_handle)
                    raise JsResourceError(
                        "ryn_js_resource_directory_changed",
                        f"Pinned file {entry.path!r} parent lineage does not match pinned parent {parent_rel!r}",
                    )
                pinned_files[entry.path] = pin

        if verified_bytes_sum != manifest.total_bytes:
            raise JsResourceError(
                "ryn_js_resource_size_mismatch",
                f"Verified total bytes ({verified_bytes_sum}) != manifest total_bytes ({manifest.total_bytes})",
            )

        _verify_lockfile_and_exports_from_disk_bytes(
            manifest=manifest,
            retained_json_bytes=retained_json_bytes,
        )

        # Verify WASM magic header (\x00asm\x01\x00\x00\x00) on all 4 QuickJS variant WASM binaries
        for variant_pkg in QUICKJS_VARIANT_PACKAGES:
            wasm_rel = f"package/node_modules/{variant_pkg}/dist/emscripten-module.wasm"
            wasm_head = wasm_headers.get(wasm_rel, b"")
            if len(wasm_head) < 8 or wasm_head[:8] != b"\x00asm\x01\x00\x00\x00":
                raise JsResourceError(
                    "ryn_js_resource_invalid_wasm",
                    f"File {wasm_rel!r} does not have a valid WebAssembly binary header",
                )

        profile = JsRuntimeResourceProfile(
            profile_root=root_resolved,
            node_executable=root_resolved / Path(manifest.node_executable_relpath),
            worker_path=root_resolved / Path(manifest.worker_relpath),
            sky_shim_path=root_resolved / Path(manifest.sky_shim_relpath),
            package_root=root_resolved / "package",
            wasm_path=root_resolved / Path(manifest.wasm_relpath),
            manifest_path=root_resolved / MANIFEST_FILENAME,
            profile_digest=manifest.manifest_sha256,
            mode=mode,
            platform=manifest.platform,
            arch=manifest.arch,
            node_version=manifest.node_version,
            node_sha256=manifest.node_sha256,
            node_bytes=manifest.node_bytes,
            quickjs_version=manifest.quickjs_version,
            quickjs_variant=manifest.quickjs_variant,
            wire_protocol=manifest.wire_protocol,
            worker_sha256=manifest.worker_sha256,
            sky_shim_sha256=manifest.sky_shim_sha256,
            wasm_sha256=manifest.wasm_sha256,
            manifest=manifest,
            files_verified=True,
            handshake_verified=False,
        )
        if not acquire_lease:
            return profile, None

        committed_digest = (
            expected_spec.expected_manifest_sha256
            if (mode == "packaged_release" and expected_spec is not None)
            else None
        )
        with _ACTIVE_LEASES_LOCK:
            if len(_ACTIVE_LEASE_IDS) >= MAX_ACTIVE_RESOURCE_LEASES:
                raise JsResourceError(
                    "ryn_js_resource_lease_limit_exceeded",
                    f"Active resource lease count exceeds cap {MAX_ACTIVE_RESOURCE_LEASES}",
                )
            lease = VerifiedJsResourceLease(
                profile=profile,
                _pinned_files=dict(pinned_files),
                _pinned_directories=dict(pinned_directories),
                _pinned_fds={rel: p.raw_handle for rel, p in pinned_files.items()},
                _lease_registered=True,
                _authority_mode=mode,
                _committed_manifest_sha256=committed_digest,
            )
            _ACTIVE_LEASE_IDS.add(id(lease))
            if mode == "packaged_release" and committed_digest is not None:
                _PRODUCTION_LEASE_AUTHORITIES[id(lease)] = _ProductionLeaseAuthorityRecord(
                    profile_root=profile.profile_root,
                    node_executable=profile.node_executable,
                    worker_path=profile.worker_path,
                    sky_shim_path=profile.sky_shim_path,
                    package_root=profile.package_root,
                    wasm_path=profile.wasm_path,
                    manifest_path=profile.manifest_path,
                    profile_digest=profile.profile_digest,
                    platform=profile.platform,
                    arch=profile.arch,
                    node_version=profile.node_version,
                    node_sha256=profile.node_sha256,
                    node_bytes=profile.node_bytes,
                    quickjs_version=profile.quickjs_version,
                    quickjs_variant=profile.quickjs_variant,
                    wire_protocol=profile.wire_protocol,
                    worker_sha256=profile.worker_sha256,
                    sky_shim_sha256=profile.sky_shim_sha256,
                    wasm_sha256=profile.wasm_sha256,
                    total_files=manifest.total_files,
                    total_bytes=manifest.total_bytes,
                )
        return profile, lease
    except BaseException:
        for pin in pinned_files.values():
            close_pinned_raw_handle(pin.raw_handle, is_win32_handle=pin.is_win32_handle)
        for dir_pin in pinned_directories.values():
            close_pinned_raw_handle(dir_pin.raw_handle, is_win32_handle=dir_pin.is_win32_handle)
        raise


def resolve_production_js_runtime_profile(
    *,
    trusted_profile_root: str | Path,
    expected_spec: ExpectedJsRuntimeProfileSpec,
) -> JsRuntimeResourceProfile:
    """Resolve and strictly verify a production packaged JS runtime resource profile.

    Never consults `PATH`, `RYN_JS_NODE_EXECUTABLE`, `NODE_PATH`, `os.getcwd()`, or temp dirs.
    Requires a builder/package-injected `ExpectedJsRuntimeProfileSpec` with `expected_manifest_sha256`.
    """
    if not isinstance(expected_spec, ExpectedJsRuntimeProfileSpec):
        raise JsResourceError(
            "ryn_js_resource_missing_commitment",
            "expected_spec must be an ExpectedJsRuntimeProfileSpec instance",
        )
    profile, _ = _resolve_and_verify_profile_internal(
        profile_root=trusted_profile_root,
        mode="packaged_release",
        expected_spec=expected_spec,
        expected_platform=expected_spec.platform,
        expected_arch=expected_spec.arch,
        acquire_lease=False,
    )
    return profile


def open_production_js_runtime_lease(
    *,
    trusted_profile_root: str | Path,
    expected_spec: ExpectedJsRuntimeProfileSpec,
) -> VerifiedJsResourceLease:
    """Resolve a production JS runtime profile and return a `VerifiedJsResourceLease` holding pinned handles."""
    if not isinstance(expected_spec, ExpectedJsRuntimeProfileSpec):
        raise JsResourceError(
            "ryn_js_resource_missing_commitment",
            "expected_spec must be an ExpectedJsRuntimeProfileSpec instance",
        )
    _, lease = _resolve_and_verify_profile_internal(
        profile_root=trusted_profile_root,
        mode="packaged_release",
        expected_spec=expected_spec,
        expected_platform=expected_spec.platform,
        expected_arch=expected_spec.arch,
        acquire_lease=True,
    )
    assert lease is not None
    return lease


def resolve_development_js_runtime_profile(
    *,
    explicit_profile_root: str | Path,
    expected_spec: ExpectedJsRuntimeProfileSpec | None = None,
    expected_platform: str | None = None,
    expected_arch: str | None = None,
) -> JsRuntimeResourceProfile:
    """Resolve an explicitly operator/test-supplied resource profile in `source_dev` mode.

    Even in `source_dev` mode, every file in `manifest.json`, its SHA-256 digest, the
    production dependency closure, and reparse-free lineage are strictly verified.
    The resulting profile is explicitly labelled `mode="source_dev"` and `is_production=False`.
    """
    profile, _ = _resolve_and_verify_profile_internal(
        profile_root=explicit_profile_root,
        mode="source_dev",
        expected_spec=expected_spec,
        expected_platform=expected_platform,
        expected_arch=expected_arch,
        acquire_lease=False,
    )
    return profile


def open_development_js_runtime_lease(
    *,
    explicit_profile_root: str | Path,
    expected_spec: ExpectedJsRuntimeProfileSpec | None = None,
    expected_platform: str | None = None,
    expected_arch: str | None = None,
) -> VerifiedJsResourceLease:
    """Resolve an explicit development JS runtime profile in `source_dev` mode and return a handle-pinned lease."""
    _, lease = _resolve_and_verify_profile_internal(
        profile_root=explicit_profile_root,
        mode="source_dev",
        expected_spec=expected_spec,
        expected_platform=expected_platform,
        expected_arch=expected_arch,
        acquire_lease=True,
    )
    assert lease is not None
    return lease


__all__ = [
    "ALLOWED_ARCHES",
    "ALLOWED_PLATFORMS",
    "ALLOWED_PROFILE_MODES",
    "APPROVED_WIN32_X64_NODE_ARCHIVE_SHA256",
    "APPROVED_WIN32_X64_NODE_BYTES",
    "APPROVED_WIN32_X64_NODE_SHA256",
    "APPROVED_WIN32_X64_NODE_VERSION",
    "BUILD_RECEIPT_FILENAME",
    "ExpectedJsRuntimeProfileSpec",
    "JsResourceError",
    "JsRuntimeResourceManifest",
    "JsRuntimeResourceProfile",
    "MANIFEST_FILENAME",
    "MANIFEST_SCHEMA_VERSION",
    "MAX_ACTIVE_RESOURCE_LEASES",
    "MAX_AGGREGATE_PROFILE_BYTES",
    "MAX_LICENSE_ENTRIES",
    "MAX_MANIFEST_BYTES",
    "MAX_MANIFEST_JSON_DEPTH",
    "MAX_PINNED_DIRECTORIES_PER_LEASE",
    "MAX_PINNED_HANDLES_PER_LEASE",
    "MAX_PROFILE_FILE_COUNT",
    "MAX_RELATIVE_PATH_DEPTH",
    "MAX_RELATIVE_PATH_LEN",
    "MAX_SINGLE_RESOURCE_FILE_BYTES",
    "NODE_EXECUTABLE_REL_PATH_POSIX",
    "NODE_EXECUTABLE_REL_PATH_WIN32",
    "NODE_LICENSE_REL_PATH",
    "NODE_README_REL_PATH",
    "PACKAGE_JSON_REL_PATH",
    "PACKAGE_LOCK_REL_PATH",
    "PINNED_QUICKJS_VARIANT",
    "PINNED_QUICKJS_VERSION",
    "PINNED_RUNTIME_PACKAGE_NAME",
    "PINNED_RUNTIME_PACKAGE_VERSION",
    "PINNED_WIRE_PROTOCOL",
    "PinnedDirectoryIdentity",
    "PinnedFileIdentity",
    "QUICKJS_CORE_PKG_REL_PATH",
    "QUICKJS_FFI_TYPES_PKG_REL_PATH",
    "QUICKJS_UMBRELLA_PKG_REL_PATH",
    "QUICKJS_VARIANT_PACKAGES",
    "RELEASE_SYNC_PKG_REL_PATH",
    "RELEASE_SYNC_WASM_REL_PATH",
    "REQUIRED_PRODUCTION_PACKAGES",
    "ResourceDependencyPackageEntry",
    "ResourceFileEntry",
    "ResourceLicenseEntry",
    "SKY_SHIM_REL_PATH",
    "VerifiedJsResourceLease",
    "WORKER_REL_PATH",
    "active_resource_lease_count",
    "compute_manifest_digest",
    "expected_node_executable_relpath",
    "expected_profile_directory_relpaths",
    "is_symlink_or_reparse",
    "is_verified_production_lease",
    "open_development_js_runtime_lease",
    "open_production_js_runtime_lease",
    "parse_strict_bounded_json",
    "read_and_verify_contained_file",
    "resolve_development_js_runtime_profile",
    "resolve_production_js_runtime_profile",
    "sanitize_invocation_environment",
    "validate_manifest_dict",
    "validate_relative_resource_path",
    "validate_semver",
    "validate_sha256_hex",
    "verify_directory_lineage_no_reparse",
    "verify_relative_lineage_under_root",
]


