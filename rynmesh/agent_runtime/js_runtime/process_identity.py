"""Bounded Windows FFI for handle-pinned JS runtime files, directories, and suspended child process verification.

This module provides:
1. Windows `CreateFileW` read-only file pinning (`GENERIC_READ`, `FILE_SHARE_READ` only —
   denying write/delete/rename while allowing image execution of `node.exe`).
2. Windows `CreateFileW` directory handle pinning (`FILE_LIST_DIRECTORY | FILE_TRAVERSE | FILE_READ_ATTRIBUTES`,
   `FILE_SHARE_READ | FILE_SHARE_WRITE` without `FILE_SHARE_DELETE`, `FILE_FLAG_BACKUP_SEMANTICS | FILE_FLAG_OPEN_REPARSE_POINT` —
   preventing rename, deletion, or reparse replacement of the profile root and every directory in the
   runtime package closure while allowing ordinary directory traversal and file creation in parent temp roots).
3. Handle-based file and directory identity inspection (`BY_HANDLE_FILE_INFORMATION` volume serial +
   64-bit file index, file size, attributes, and `GetFinalPathNameByHandleW`).
4. Suspended child process pre-resume OS identity verification (`GetProcessId`,
   `GetProcessTimes`, `QueryFullProcessImageNameW`) before `NtResumeProcess`.
"""

from __future__ import annotations

import hashlib
import os
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .protocol import JsRuntimeError


class JsProcessIdentityError(JsRuntimeError):
    """Raised when a pinned file/directory handle or suspended child process fails OS identity verification."""


MAX_ACTIVE_RESOURCE_LEASES = 32
MAX_PINNED_HANDLES_PER_LEASE = 512
MAX_PINNED_DIRECTORIES_PER_LEASE = 128

GENERIC_READ = 0x80000000
FILE_LIST_DIRECTORY = 0x00000001
FILE_TRAVERSE = 0x00000020
FILE_READ_ATTRIBUTES = 0x00000080
DIRECTORY_PIN_ACCESS = FILE_LIST_DIRECTORY | FILE_TRAVERSE | FILE_READ_ATTRIBUTES

FILE_SHARE_READ = 0x00000001
FILE_SHARE_WRITE = 0x00000002
DIRECTORY_PIN_SHARE = FILE_SHARE_READ | FILE_SHARE_WRITE

OPEN_EXISTING = 3
FILE_ATTRIBUTE_NORMAL = 0x00000080
FILE_ATTRIBUTE_DIRECTORY = 0x00000010
FILE_ATTRIBUTE_REPARSE_POINT = 0x00000400
FILE_FLAG_BACKUP_SEMANTICS = 0x02000000
FILE_FLAG_OPEN_REPARSE_POINT = 0x00200000
INVALID_HANDLE_VALUE = -1
CREATE_SUSPENDED = 0x00000004


@dataclass(frozen=True)
class PinnedFileIdentity:
    """Immutable identity of a handle-pinned regular runtime file."""

    rel_path: str
    final_path: Path
    size: int
    sha256: str
    volume_serial: int
    file_id: int
    raw_handle: int
    is_win32_handle: bool


@dataclass(frozen=True)
class PinnedDirectoryIdentity:
    """Immutable identity of a handle-pinned runtime directory."""

    rel_path: str
    final_path: Path
    volume_serial: int
    file_id: int
    raw_handle: int
    is_win32_handle: bool


@dataclass(frozen=True)
class VerifiedProcessIdentity:
    """Immutable OS-verified identity of a spawned Node worker process inspected while suspended."""

    pid: int
    creation_time_filetime: int
    image_path: Path
    node_volume_serial: int
    node_file_id: int
    node_sha256: str
    node_bytes: int
    profile_digest: str
    mode: str
    session_id: str
    generation: int

    def to_public_metadata(self) -> dict[str, Any]:
        """Return safe, path-free diagnostic metadata for public status reporting."""
        return {
            "pid": self.pid,
            "creation_time_filetime": self.creation_time_filetime,
            "node_volume_serial": self.node_volume_serial,
            "node_file_id": self.node_file_id,
            "node_sha256": self.node_sha256,
            "node_bytes": self.node_bytes,
            "profile_digest": self.profile_digest,
            "mode": self.mode,
            "session_id": self.session_id,
            "generation": self.generation,
        }


if os.name == "nt":
    import ctypes
    from ctypes import wintypes

    class _FILETIME(ctypes.Structure):
        _fields_ = [
            ("dwLowDateTime", wintypes.DWORD),
            ("dwHighDateTime", wintypes.DWORD),
        ]

    class _BY_HANDLE_FILE_INFORMATION(ctypes.Structure):  # noqa: N801 - matches the Win32 structure name.
        _fields_ = [
            ("dwFileAttributes", wintypes.DWORD),
            ("ftCreationTime", _FILETIME),
            ("ftLastAccessTime", _FILETIME),
            ("ftLastWriteTime", _FILETIME),
            ("dwVolumeSerialNumber", wintypes.DWORD),
            ("nFileSizeHigh", wintypes.DWORD),
            ("nFileSizeLow", wintypes.DWORD),
            ("nNumberOfLinks", wintypes.DWORD),
            ("nFileIndexHigh", wintypes.DWORD),
            ("nFileIndexLow", wintypes.DWORD),
        ]

    _kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

    _CreateFileW = _kernel32.CreateFileW
    _CreateFileW.argtypes = [
        wintypes.LPCWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.LPVOID,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.HANDLE,
    ]
    _CreateFileW.restype = wintypes.HANDLE

    _ReadFile = _kernel32.ReadFile
    _ReadFile.argtypes = [
        wintypes.HANDLE,
        wintypes.LPVOID,
        wintypes.DWORD,
        ctypes.POINTER(wintypes.DWORD),
        wintypes.LPVOID,
    ]
    _ReadFile.restype = wintypes.BOOL

    _SetFilePointerEx = _kernel32.SetFilePointerEx
    _SetFilePointerEx.argtypes = [
        wintypes.HANDLE,
        ctypes.c_int64,
        ctypes.POINTER(ctypes.c_int64),
        wintypes.DWORD,
    ]
    _SetFilePointerEx.restype = wintypes.BOOL

    _GetFileInformationByHandle = _kernel32.GetFileInformationByHandle
    _GetFileInformationByHandle.argtypes = [
        wintypes.HANDLE,
        ctypes.POINTER(_BY_HANDLE_FILE_INFORMATION),
    ]
    _GetFileInformationByHandle.restype = wintypes.BOOL

    _GetFinalPathNameByHandleW = _kernel32.GetFinalPathNameByHandleW
    _GetFinalPathNameByHandleW.argtypes = [
        wintypes.HANDLE,
        wintypes.LPWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
    ]
    _GetFinalPathNameByHandleW.restype = wintypes.DWORD

    _CloseHandle = _kernel32.CloseHandle
    _CloseHandle.argtypes = [wintypes.HANDLE]
    _CloseHandle.restype = wintypes.BOOL

    _GetProcessId = _kernel32.GetProcessId
    _GetProcessId.argtypes = [wintypes.HANDLE]
    _GetProcessId.restype = wintypes.DWORD

    _GetProcessTimes = _kernel32.GetProcessTimes
    _GetProcessTimes.argtypes = [
        wintypes.HANDLE,
        ctypes.POINTER(_FILETIME),
        ctypes.POINTER(_FILETIME),
        ctypes.POINTER(_FILETIME),
        ctypes.POINTER(_FILETIME),
    ]
    _GetProcessTimes.restype = wintypes.BOOL

    _QueryFullProcessImageNameW = _kernel32.QueryFullProcessImageNameW
    _QueryFullProcessImageNameW.argtypes = [
        wintypes.HANDLE,
        wintypes.DWORD,
        wintypes.LPWSTR,
        ctypes.POINTER(wintypes.DWORD),
    ]
    _QueryFullProcessImageNameW.restype = wintypes.BOOL


def _is_invalid_win32_handle(handle_val: int | None) -> bool:
    if handle_val is None or handle_val == 0:
        return True
    ptr_mask = (1 << (ctypes.sizeof(ctypes.c_void_p) * 8)) - 1
    return (handle_val & ptr_mask) == (INVALID_HANDLE_VALUE & ptr_mask)


def close_pinned_raw_handle(raw_handle: int, *, is_win32_handle: bool) -> None:
    """Close a raw OS handle or POSIX file descriptor idempotently."""
    if raw_handle is None or (raw_handle < 0 and not is_win32_handle):
        return
    if os.name == "nt" and is_win32_handle:
        if not _is_invalid_win32_handle(raw_handle):
            try:
                _CloseHandle(wintypes.HANDLE(raw_handle))
            except Exception:
                pass
        return
    try:
        os.close(raw_handle)
    except OSError:
        pass


def _query_win32_handle_common(
    raw_handle: int,
    *,
    label: str,
    expect_directory: bool,
) -> tuple[int, int, int, int, Path]:
    """Query `(attributes, file_size, volume_serial, file_id_64, final_path)` for an open Win32 file or directory handle."""
    if os.name != "nt":
        raise JsProcessIdentityError("ryn_js_platform_guard_unsupported", "Win32 handle query requires Windows")
    info = _BY_HANDLE_FILE_INFORMATION()
    ok = _GetFileInformationByHandle(wintypes.HANDLE(raw_handle), ctypes.byref(info))
    if not ok:
        err = ctypes.get_last_error()
        raise JsProcessIdentityError(
            "ryn_js_resource_unreadable",
            f"GetFileInformationByHandle failed for {label} (winerror={err})",
        )
    attrs = int(info.dwFileAttributes)
    if attrs & FILE_ATTRIBUTE_REPARSE_POINT:
        raise JsProcessIdentityError(
            "ryn_js_resource_reparse_forbidden",
            f"Handle for {label} has FILE_ATTRIBUTE_REPARSE_POINT",
        )
    is_dir = bool(attrs & FILE_ATTRIBUTE_DIRECTORY)
    if expect_directory and not is_dir:
        raise JsProcessIdentityError(
            "ryn_js_resource_root_invalid",
            f"Handle for directory {label} does not have FILE_ATTRIBUTE_DIRECTORY",
        )
    if not expect_directory and is_dir:
        raise JsProcessIdentityError(
            "ryn_js_resource_not_regular_file",
            f"Handle for {label} refers to a directory, not a regular file",
        )
    file_size = (int(info.nFileSizeHigh) << 32) | int(info.nFileSizeLow)
    vol_serial = int(info.dwVolumeSerialNumber)
    file_id = (int(info.nFileIndexHigh) << 32) | int(info.nFileIndexLow)

    buf = ctypes.create_unicode_buffer(4096)
    length = _GetFinalPathNameByHandleW(wintypes.HANDLE(raw_handle), buf, 4096, 0)
    if not length or length >= 4096:
        err = ctypes.get_last_error()
        raise JsProcessIdentityError(
            "ryn_js_resource_unreadable",
            f"GetFinalPathNameByHandleW failed for {label} (winerror={err})",
        )
    raw_final = buf.value
    if raw_final.startswith("\\\\?\\UNC\\"):
        raise JsProcessIdentityError(
            "ryn_js_resource_unc_or_device_forbidden",
            f"Handle for {label} resolved to a UNC path",
        )
    if raw_final.startswith("\\\\?\\"):
        raw_final = raw_final[4:]
    if not raw_final or "\x00" in raw_final:
        raise JsProcessIdentityError("ryn_js_resource_invalid_path", f"Invalid final path for {label}")
    colon_positions = [idx for idx, ch in enumerate(raw_final) if ch == ":"]
    for idx in colon_positions:
        if not (idx == 1 and len(raw_final) >= 2 and raw_final[0].isalpha()):
            raise JsProcessIdentityError(
                "ryn_js_resource_ads_or_drive_forbidden",
                f"Handle for {label} resolved to an Alternate Data Stream path",
            )
    try:
        resolved_final = Path(raw_final).resolve(strict=True)
    except OSError as exc:
        raise JsProcessIdentityError(
            "ryn_js_resource_unreadable",
            f"Failed to resolve final path for {label}: {exc}",
        ) from exc
    return attrs, file_size, vol_serial, file_id, resolved_final


def query_win32_handle_info(raw_handle: int, *, label: str = "handle") -> tuple[int, int, int, int, Path]:
    """Return `(attributes, file_size, volume_serial, file_id_64, final_path)` for an open Win32 regular-file handle."""
    return _query_win32_handle_common(raw_handle, label=label, expect_directory=False)


def query_win32_directory_handle_info(raw_handle: int, *, label: str = "dir_handle") -> tuple[int, int, int, Path]:
    """Return `(attributes, volume_serial, file_id_64, final_path)` for an open Win32 directory handle."""
    attrs, _size, vol_serial, file_id, resolved_final = _query_win32_handle_common(
        raw_handle,
        label=label,
        expect_directory=True,
    )
    return attrs, vol_serial, file_id, resolved_final


def open_and_verify_pinned_win32_directory(
    *,
    target_resolved: Path,
    root_resolved: Path,
    rel_path: str,
    keep_open: bool,
) -> PinnedDirectoryIdentity | None:
    """Open a directory on Windows with `FILE_FLAG_BACKUP_SEMANTICS | FILE_FLAG_OPEN_REPARSE_POINT` and no delete sharing."""
    if os.name != "nt":
        raise JsProcessIdentityError(
            "ryn_js_platform_guard_unsupported",
            "Windows directory handle pinning requires Windows",
        )
    flags = FILE_FLAG_BACKUP_SEMANTICS | FILE_FLAG_OPEN_REPARSE_POINT
    raw_h = _CreateFileW(
        str(target_resolved),
        DIRECTORY_PIN_ACCESS,
        DIRECTORY_PIN_SHARE,
        None,
        OPEN_EXISTING,
        flags,
        None,
    )
    handle_int = int(ctypes.cast(raw_h, ctypes.c_void_p).value or 0)
    if _is_invalid_win32_handle(handle_int):
        err = ctypes.get_last_error()
        raise JsProcessIdentityError(
            "ryn_js_resource_unreadable",
            f"CreateFileW failed to open directory {rel_path!r} (winerror={err})",
        )

    handed_off = False
    try:
        _, vol_serial, file_id, final_path = query_win32_directory_handle_info(
            handle_int,
            label=rel_path,
        )
        if rel_path != "." and not final_path.is_relative_to(root_resolved):
            raise JsProcessIdentityError(
                "ryn_js_resource_outside_containment",
                f"Opened directory handle for {rel_path!r} resolved outside profile root",
            )
        if os.path.normcase(str(final_path)) != os.path.normcase(str(target_resolved)):
            raise JsProcessIdentityError(
                "ryn_js_resource_reparse_forbidden",
                f"Opened directory handle path {final_path} does not match expected directory {target_resolved}",
            )
        try:
            st_after = os.lstat(target_resolved)
        except OSError as exc:
            raise JsProcessIdentityError(
                "ryn_js_resource_unreadable",
                f"Failed to lstat directory {rel_path!r}: {exc}",
            ) from exc
        if stat.S_ISLNK(st_after.st_mode) or not stat.S_ISDIR(st_after.st_mode):
            raise JsProcessIdentityError(
                "ryn_js_resource_root_invalid",
                f"Path is not a regular directory: {rel_path!r}",
            )
        after_attrs = getattr(st_after, "st_file_attributes", 0)
        if isinstance(after_attrs, int) and (after_attrs & FILE_ATTRIBUTE_REPARSE_POINT):
            raise JsProcessIdentityError(
                "ryn_js_resource_reparse_forbidden",
                f"Directory became a reparse point: {rel_path!r}",
            )

        if keep_open:
            pin = PinnedDirectoryIdentity(
                rel_path=rel_path,
                final_path=final_path,
                volume_serial=vol_serial,
                file_id=file_id,
                raw_handle=handle_int,
                is_win32_handle=True,
            )
            handed_off = True
            return pin
        return None
    finally:
        if not handed_off:
            close_pinned_raw_handle(handle_int, is_win32_handle=True)


def revalidate_pinned_directory_handle(
    pin: PinnedDirectoryIdentity,
    *,
    root_resolved: Path,
) -> None:
    """Re-verify a pinned directory handle and confirm the directory path still resolves to the exact same directory ID."""
    target_path = root_resolved if pin.rel_path == "." else (root_resolved / Path(pin.rel_path))
    if os.name == "nt" and pin.is_win32_handle:
        _, curr_vol, curr_fid, curr_final = query_win32_directory_handle_info(
            pin.raw_handle,
            label=pin.rel_path,
        )
        if curr_vol != pin.volume_serial or curr_fid != pin.file_id:
            raise JsProcessIdentityError(
                "ryn_js_resource_file_changed",
                f"Pinned directory handle volume/file_id changed for {pin.rel_path!r}",
            )
        if pin.rel_path != "." and not curr_final.is_relative_to(root_resolved):
            raise JsProcessIdentityError(
                "ryn_js_resource_outside_containment",
                f"Pinned directory handle for {pin.rel_path!r} resolved outside profile root",
            )
        if os.path.normcase(str(curr_final)) != os.path.normcase(str(pin.final_path)):
            raise JsProcessIdentityError(
                "ryn_js_resource_file_changed",
                f"Pinned directory final path changed for {pin.rel_path!r}",
            )

        probe_h = _CreateFileW(
            str(target_path),
            DIRECTORY_PIN_ACCESS,
            DIRECTORY_PIN_SHARE,
            None,
            OPEN_EXISTING,
            FILE_FLAG_BACKUP_SEMANTICS | FILE_FLAG_OPEN_REPARSE_POINT,
            None,
        )
        probe_int = int(ctypes.cast(probe_h, ctypes.c_void_p).value or 0)
        if _is_invalid_win32_handle(probe_int):
            err = ctypes.get_last_error()
            raise JsProcessIdentityError(
                "ryn_js_resource_root_missing",
                f"Directory path re-open check failed for {pin.rel_path!r} (winerror={err})",
            )
        try:
            _, probe_vol, probe_fid, probe_final = query_win32_directory_handle_info(
                probe_int,
                label=pin.rel_path,
            )
            if (
                probe_vol != pin.volume_serial
                or probe_fid != pin.file_id
                or os.path.normcase(str(probe_final)) != os.path.normcase(str(pin.final_path))
            ):
                raise JsProcessIdentityError(
                    "ryn_js_resource_file_changed",
                    f"Directory path {pin.rel_path!r} no longer points to the pinned directory identity",
                )
        finally:
            close_pinned_raw_handle(probe_int, is_win32_handle=True)
        return

    try:
        st_fd = os.fstat(pin.raw_handle)
        st_path = os.lstat(target_path)
    except OSError as exc:
        raise JsProcessIdentityError(
            "ryn_js_resource_unreadable",
            f"Failed to stat pinned directory {pin.rel_path!r}: {exc}",
        ) from exc
    if not stat.S_ISDIR(st_fd.st_mode) or not stat.S_ISDIR(st_path.st_mode) or stat.S_ISLNK(st_path.st_mode):
        raise JsProcessIdentityError(
            "ryn_js_resource_root_invalid",
            f"Pinned directory {pin.rel_path!r} is no longer a regular directory",
        )
    if st_fd.st_ino != pin.file_id or st_fd.st_dev != pin.volume_serial:
        raise JsProcessIdentityError(
            "ryn_js_resource_file_changed",
            f"Pinned directory FD inode/dev changed for {pin.rel_path!r}",
        )
    if st_path.st_ino != pin.file_id or st_path.st_dev != pin.volume_serial:
        raise JsProcessIdentityError(
            "ryn_js_resource_file_changed",
            f"Directory path {pin.rel_path!r} no longer matches pinned directory FD identity",
        )


def open_and_verify_pinned_win32_file(
    *,
    target_resolved: Path,
    root_resolved: Path,
    rel_path: str,
    expected_sha256: str | None,
    expected_size: int | None,
    max_bytes: int,
    retain_bytes: bool,
    keep_open: bool,
) -> tuple[str, int, bytes | None, PinnedFileIdentity | None]:
    """Open `target_resolved` on Windows with `FILE_SHARE_READ` only, hash from the same handle, and optionally keep it pinned."""
    flags = FILE_ATTRIBUTE_NORMAL | FILE_FLAG_OPEN_REPARSE_POINT
    raw_h = _CreateFileW(
        str(target_resolved),
        GENERIC_READ,
        FILE_SHARE_READ,
        None,
        OPEN_EXISTING,
        flags,
        None,
    )
    handle_int = int(ctypes.cast(raw_h, ctypes.c_void_p).value or 0)
    if _is_invalid_win32_handle(handle_int):
        err = ctypes.get_last_error()
        raise JsProcessIdentityError(
            "ryn_js_resource_unreadable",
            f"CreateFileW failed to open {rel_path} with FILE_SHARE_READ (winerror={err})",
        )

    handed_off = False
    try:
        _, size_before, vol_before, fid_before, final_before = query_win32_handle_info(
            handle_int,
            label=rel_path,
        )
        if not final_before.is_relative_to(root_resolved):
            raise JsProcessIdentityError(
                "ryn_js_resource_outside_containment",
                f"Opened handle for {rel_path} resolved outside profile root",
            )
        if os.path.normcase(str(final_before)) != os.path.normcase(str(target_resolved)):
            raise JsProcessIdentityError(
                "ryn_js_resource_reparse_forbidden",
                f"Opened handle path {final_before} does not match expected path {target_resolved}",
            )
        if size_before > max_bytes:
            raise JsProcessIdentityError(
                "ryn_js_resource_file_too_large",
                f"File {rel_path} size {size_before} exceeds maximum {max_bytes} bytes",
            )
        if expected_size is not None and size_before != expected_size:
            raise JsProcessIdentityError(
                "ryn_js_resource_size_mismatch",
                f"File {rel_path} byte length mismatch: expected {expected_size}, found {size_before}",
            )

        try:
            st_after = os.lstat(target_resolved)
        except OSError as exc:
            raise JsProcessIdentityError("ryn_js_resource_unreadable", f"Failed to lstat {rel_path}: {exc}") from exc
        if stat.S_ISLNK(st_after.st_mode) or not stat.S_ISREG(st_after.st_mode):
            raise JsProcessIdentityError("ryn_js_resource_not_regular_file", f"Not a regular file: {rel_path}")
        after_attrs = getattr(st_after, "st_file_attributes", 0)
        if isinstance(after_attrs, int) and (after_attrs & FILE_ATTRIBUTE_REPARSE_POINT):
            raise JsProcessIdentityError("ryn_js_resource_reparse_forbidden", f"File became reparse point: {rel_path}")

        read_cap = expected_size if expected_size is not None else max_bytes
        read_limit = read_cap + 1
        buf_size = 65536
        buf = ctypes.create_string_buffer(buf_size)
        bytes_read = wintypes.DWORD(0)
        chunks: list[bytes] | None = [] if retain_bytes else None
        hasher = hashlib.sha256()
        total = 0

        while total < read_limit:
            to_read = min(buf_size, read_limit - total)
            ok = _ReadFile(
                wintypes.HANDLE(handle_int),
                buf,
                wintypes.DWORD(to_read),
                ctypes.byref(bytes_read),
                None,
            )
            if not ok:
                err = ctypes.get_last_error()
                raise JsProcessIdentityError(
                    "ryn_js_resource_unreadable",
                    f"ReadFile failed on {rel_path} (winerror={err})",
                )
            n = int(bytes_read.value)
            if n <= 0:
                break
            chunk = buf.raw[:n]
            if chunks is not None:
                chunks.append(chunk)
            hasher.update(chunk)
            total += n

        _, size_after, vol_after, fid_after, final_after = query_win32_handle_info(
            handle_int,
            label=rel_path,
        )
        if vol_after != vol_before or fid_after != fid_before:
            raise JsProcessIdentityError(
                "ryn_js_resource_file_changed",
                f"File handle identity changed during read: {rel_path}",
            )
        if os.path.normcase(str(final_after)) != os.path.normcase(str(final_before)):
            raise JsProcessIdentityError(
                "ryn_js_resource_file_changed",
                f"File handle path changed during read: {rel_path}",
            )
        if total > max_bytes or size_before > max_bytes or size_after > max_bytes:
            raise JsProcessIdentityError(
                "ryn_js_resource_file_too_large",
                f"File {rel_path} exceeds maximum {max_bytes} bytes",
            )
        if expected_size is not None and (
            total != expected_size or size_before != expected_size or size_after != expected_size
        ):
            raise JsProcessIdentityError(
                "ryn_js_resource_size_mismatch",
                f"File {rel_path} byte length mismatch during read: expected {expected_size}, read {total}",
            )
        if total != size_after:
            raise JsProcessIdentityError(
                "ryn_js_resource_file_changed",
                f"File size changed while reading: {rel_path}",
            )

        actual_sha256 = hasher.hexdigest()
        if expected_sha256 is not None and actual_sha256 != expected_sha256:
            raise JsProcessIdentityError(
                "ryn_js_resource_hash_mismatch",
                f"SHA-256 mismatch for {rel_path}: expected {expected_sha256}, got {actual_sha256}",
            )

        raw_bytes = b"".join(chunks) if chunks is not None else None
        if keep_open:
            new_pos = ctypes.c_int64(0)
            _SetFilePointerEx(wintypes.HANDLE(handle_int), ctypes.c_int64(0), ctypes.byref(new_pos), 0)
            pin = PinnedFileIdentity(
                rel_path=rel_path,
                final_path=final_after,
                size=total,
                sha256=actual_sha256,
                volume_serial=vol_after,
                file_id=fid_after,
                raw_handle=handle_int,
                is_win32_handle=True,
            )
            handed_off = True
            return actual_sha256, total, raw_bytes, pin
        return actual_sha256, total, raw_bytes, None
    finally:
        if not handed_off:
            close_pinned_raw_handle(handle_int, is_win32_handle=True)


def revalidate_pinned_file_handle(
    pin: PinnedFileIdentity,
    *,
    root_resolved: Path,
    verify_content_hash: bool = False,
) -> None:
    """Re-verify an already-pinned file handle and confirm the profile path still resolves to the exact same file ID."""
    target_path = root_resolved / Path(pin.rel_path)
    if os.name == "nt" and pin.is_win32_handle:
        _, curr_size, curr_vol, curr_fid, curr_final = query_win32_handle_info(
            pin.raw_handle,
            label=pin.rel_path,
        )
        if curr_vol != pin.volume_serial or curr_fid != pin.file_id:
            raise JsProcessIdentityError(
                "ryn_js_resource_file_changed",
                f"Pinned handle volume/file_id changed for {pin.rel_path}",
            )
        if curr_size != pin.size:
            raise JsProcessIdentityError(
                "ryn_js_resource_size_mismatch",
                f"Pinned handle size changed for {pin.rel_path}: expected {pin.size}, got {curr_size}",
            )
        if not curr_final.is_relative_to(root_resolved):
            raise JsProcessIdentityError(
                "ryn_js_resource_outside_containment",
                f"Pinned handle for {pin.rel_path} resolved outside profile root",
            )
        if os.path.normcase(str(curr_final)) != os.path.normcase(str(pin.final_path)):
            raise JsProcessIdentityError(
                "ryn_js_resource_file_changed",
                f"Pinned handle final path changed for {pin.rel_path}",
            )

        probe_h = _CreateFileW(
            str(target_path),
            GENERIC_READ,
            FILE_SHARE_READ,
            None,
            OPEN_EXISTING,
            FILE_ATTRIBUTE_NORMAL | FILE_FLAG_OPEN_REPARSE_POINT,
            None,
        )
        probe_int = int(ctypes.cast(probe_h, ctypes.c_void_p).value or 0)
        if _is_invalid_win32_handle(probe_int):
            err = ctypes.get_last_error()
            raise JsProcessIdentityError(
                "ryn_js_resource_file_missing",
                f"Path re-open check failed for {pin.rel_path} (winerror={err})",
            )
        try:
            _, probe_size, probe_vol, probe_fid, probe_final = query_win32_handle_info(
                probe_int,
                label=pin.rel_path,
            )
            if (
                probe_vol != pin.volume_serial
                or probe_fid != pin.file_id
                or probe_size != pin.size
                or os.path.normcase(str(probe_final)) != os.path.normcase(str(pin.final_path))
            ):
                raise JsProcessIdentityError(
                    "ryn_js_resource_file_changed",
                    f"Path {pin.rel_path} no longer points to the pinned file identity",
                )
        finally:
            close_pinned_raw_handle(probe_int, is_win32_handle=True)

        if verify_content_hash:
            new_pos = ctypes.c_int64(0)
            if not _SetFilePointerEx(wintypes.HANDLE(pin.raw_handle), ctypes.c_int64(0), ctypes.byref(new_pos), 0):
                raise JsProcessIdentityError(
                    "ryn_js_resource_unreadable",
                    f"SetFilePointerEx failed on {pin.rel_path}",
                )
            buf_size = 65536
            buf = ctypes.create_string_buffer(buf_size)
            bytes_read = wintypes.DWORD(0)
            hasher = hashlib.sha256()
            total = 0
            limit = pin.size + 1
            while total < limit:
                ok = _ReadFile(
                    wintypes.HANDLE(pin.raw_handle),
                    buf,
                    wintypes.DWORD(min(buf_size, limit - total)),
                    ctypes.byref(bytes_read),
                    None,
                )
                if not ok:
                    raise JsProcessIdentityError(
                        "ryn_js_resource_unreadable",
                        f"ReadFile failed during revalidation of {pin.rel_path}",
                    )
                n = int(bytes_read.value)
                if n <= 0:
                    break
                hasher.update(buf.raw[:n])
                total += n
            _SetFilePointerEx(wintypes.HANDLE(pin.raw_handle), ctypes.c_int64(0), ctypes.byref(new_pos), 0)
            if total != pin.size or hasher.hexdigest() != pin.sha256:
                raise JsProcessIdentityError(
                    "ryn_js_resource_hash_mismatch",
                    f"Pinned handle content hash mismatch during revalidation of {pin.rel_path}",
                )
        return

    try:
        st_fd = os.fstat(pin.raw_handle)
        st_path = os.lstat(target_path)
    except OSError as exc:
        raise JsProcessIdentityError(
            "ryn_js_resource_unreadable",
            f"Failed to stat pinned file {pin.rel_path}: {exc}",
        ) from exc
    if not stat.S_ISREG(st_fd.st_mode) or not stat.S_ISREG(st_path.st_mode) or stat.S_ISLNK(st_path.st_mode):
        raise JsProcessIdentityError(
            "ryn_js_resource_not_regular_file",
            f"Pinned file {pin.rel_path} is no longer a regular file",
        )
    if st_fd.st_ino != pin.file_id or st_fd.st_dev != pin.volume_serial:
        raise JsProcessIdentityError(
            "ryn_js_resource_file_changed",
            f"Pinned FD inode/dev changed for {pin.rel_path}",
        )
    if st_path.st_ino != pin.file_id or st_path.st_dev != pin.volume_serial or st_fd.st_size != pin.size:
        raise JsProcessIdentityError(
            "ryn_js_resource_file_changed",
            f"Path {pin.rel_path} no longer matches pinned FD identity",
        )


def query_win32_suspended_process_os_info(process_handle: int) -> tuple[int, int, str]:
    """Query `(os_pid, creation_time_filetime, raw_image_path)` directly from a Win32 process HANDLE."""
    if os.name != "nt":
        raise JsProcessIdentityError(
            "ryn_js_platform_guard_unsupported",
            "Win32 suspended process inspection is only supported on Windows",
        )
    if _is_invalid_win32_handle(process_handle):
        raise JsProcessIdentityError(
            "ryn_js_process_identity_mismatch",
            "Process handle is invalid or NULL",
        )

    h_proc = wintypes.HANDLE(process_handle)
    os_pid = int(_GetProcessId(h_proc))
    if os_pid <= 0:
        err = ctypes.get_last_error()
        raise JsProcessIdentityError(
            "ryn_js_process_identity_mismatch",
            f"GetProcessId failed on suspended child process (winerror={err})",
        )

    ft_create = _FILETIME()
    ft_exit = _FILETIME()
    ft_kernel = _FILETIME()
    ft_user = _FILETIME()
    if not _GetProcessTimes(
        h_proc,
        ctypes.byref(ft_create),
        ctypes.byref(ft_exit),
        ctypes.byref(ft_kernel),
        ctypes.byref(ft_user),
    ):
        err = ctypes.get_last_error()
        raise JsProcessIdentityError(
            "ryn_js_process_identity_mismatch",
            f"GetProcessTimes failed on suspended child process (winerror={err})",
        )
    creation_time = (int(ft_create.dwHighDateTime) << 32) | int(ft_create.dwLowDateTime)
    if creation_time <= 0:
        raise JsProcessIdentityError(
            "ryn_js_process_identity_mismatch",
            f"Suspended child process reported non-positive creation time: {creation_time}",
        )

    buf_len = wintypes.DWORD(4096)
    img_buf = ctypes.create_unicode_buffer(4096)
    if not _QueryFullProcessImageNameW(h_proc, 0, img_buf, ctypes.byref(buf_len)):
        err = ctypes.get_last_error()
        raise JsProcessIdentityError(
            "ryn_js_process_identity_mismatch",
            f"QueryFullProcessImageNameW failed on suspended child process (winerror={err})",
        )
    raw_img = img_buf.value
    if not raw_img or "\x00" in raw_img:
        raise JsProcessIdentityError(
            "ryn_js_process_identity_mismatch",
            "Suspended child process reported empty image path",
        )
    return os_pid, creation_time, raw_img


def verify_suspended_process_identity_on_windows(
    *,
    process: Any,
    lease: Any,
    session_id: str,
    generation: int,
) -> VerifiedProcessIdentity:
    """Verify a suspended child `subprocess.Popen` against `lease` before `NtResumeProcess` is called."""
    if os.name != "nt":
        raise JsProcessIdentityError(
            "ryn_js_platform_guard_unsupported",
            "Windows suspended-process identity verification is required before resuming worker",
        )
    if lease is None or getattr(lease, "closed", True):
        raise JsProcessIdentityError(
            "ryn_js_resource_lease_closed",
            "Cannot verify suspended process against a missing or closed resource lease",
        )

    raw_proc_handle = getattr(process, "_handle", None)
    if raw_proc_handle is None:
        raise JsProcessIdentityError(
            "ryn_js_process_identity_mismatch",
            "Child Popen instance is missing Windows _handle",
        )
    os_pid, creation_time, raw_image_path = query_win32_suspended_process_os_info(int(raw_proc_handle))
    if creation_time <= 0:
        raise JsProcessIdentityError(
            "ryn_js_process_identity_mismatch",
            f"Suspended child process reported non-positive creation time: {creation_time}",
        )
    popen_pid = getattr(process, "pid", None)
    if type(popen_pid) is not int or popen_pid <= 0 or os_pid != popen_pid:
        raise JsProcessIdentityError(
            "ryn_js_process_identity_mismatch",
            f"OS process ID ({os_pid}) does not match Popen.pid ({popen_pid!r})",
        )

    norm_img = raw_image_path
    if norm_img.startswith("\\\\?\\UNC\\"):
        raise JsProcessIdentityError(
            "ryn_js_resource_unc_or_device_forbidden",
            "Child process image resolved to a UNC path",
        )
    if norm_img.startswith("\\\\?\\"):
        norm_img = norm_img[4:]

    node_rel = lease.profile.manifest.node_executable_relpath
    pinned_node = lease.get_pinned_file_identity(node_rel)
    if pinned_node is None:
        raise JsProcessIdentityError(
            "ryn_js_process_identity_mismatch",
            f"Resource lease is missing a pinned handle for Node executable {node_rel!r}",
        )

    img_h = _CreateFileW(
        str(norm_img),
        GENERIC_READ,
        FILE_SHARE_READ,
        None,
        OPEN_EXISTING,
        FILE_ATTRIBUTE_NORMAL | FILE_FLAG_OPEN_REPARSE_POINT,
        None,
    )
    img_h_int = int(ctypes.cast(img_h, ctypes.c_void_p).value or 0)
    if _is_invalid_win32_handle(img_h_int):
        err = ctypes.get_last_error()
        raise JsProcessIdentityError(
            "ryn_js_process_identity_mismatch",
            f"Failed to open child process image path {norm_img!r} for identity check (winerror={err})",
        )
    try:
        _, img_size, img_vol, img_fid, img_final = query_win32_handle_info(
            img_h_int,
            label="child_process_image",
        )
    finally:
        close_pinned_raw_handle(img_h_int, is_win32_handle=True)

    if (
        img_vol != pinned_node.volume_serial
        or img_fid != pinned_node.file_id
        or img_size != pinned_node.size
        or os.path.normcase(str(img_final)) != os.path.normcase(str(pinned_node.final_path))
        or os.path.normcase(str(img_final)) != os.path.normcase(str(lease.profile.node_executable))
    ):
        raise JsProcessIdentityError(
            "ryn_js_process_identity_mismatch",
            "Suspended child process executable image does not match the pinned Node binary in the resource lease",
        )

    # Revalidate all pinned directories and files in the lease while the child is still suspended at 0 user instructions
    lease.revalidate_critical_files()

    return VerifiedProcessIdentity(
        pid=os_pid,
        creation_time_filetime=creation_time,
        image_path=img_final,
        node_volume_serial=pinned_node.volume_serial,
        node_file_id=pinned_node.file_id,
        node_sha256=pinned_node.sha256,
        node_bytes=pinned_node.size,
        profile_digest=lease.profile.profile_digest,
        mode=lease.profile.mode,
        session_id=session_id,
        generation=generation,
    )


__all__ = [
    "CREATE_SUSPENDED",
    "DIRECTORY_PIN_ACCESS",
    "DIRECTORY_PIN_SHARE",
    "FILE_ATTRIBUTE_DIRECTORY",
    "FILE_ATTRIBUTE_NORMAL",
    "FILE_ATTRIBUTE_REPARSE_POINT",
    "FILE_FLAG_BACKUP_SEMANTICS",
    "FILE_FLAG_OPEN_REPARSE_POINT",
    "FILE_LIST_DIRECTORY",
    "FILE_READ_ATTRIBUTES",
    "FILE_SHARE_READ",
    "FILE_SHARE_WRITE",
    "FILE_TRAVERSE",
    "GENERIC_READ",
    "INVALID_HANDLE_VALUE",
    "MAX_ACTIVE_RESOURCE_LEASES",
    "MAX_PINNED_DIRECTORIES_PER_LEASE",
    "MAX_PINNED_HANDLES_PER_LEASE",
    "OPEN_EXISTING",
    "JsProcessIdentityError",
    "PinnedDirectoryIdentity",
    "PinnedFileIdentity",
    "VerifiedProcessIdentity",
    "close_pinned_raw_handle",
    "open_and_verify_pinned_win32_directory",
    "open_and_verify_pinned_win32_file",
    "query_win32_directory_handle_info",
    "query_win32_handle_info",
    "query_win32_suspended_process_os_info",
    "revalidate_pinned_directory_handle",
    "revalidate_pinned_file_handle",
    "verify_suspended_process_identity_on_windows",
]
