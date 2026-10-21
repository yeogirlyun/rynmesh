"""Ordinary Windows and POSIX file-handle containment for host native screenshot artifacts.

Pins open Win32 directory handles along `.ryn/computer/<safe_task>/<safe_op>`, creates
`image_N.png` exclusively (`CREATE_NEW`), and verifies file and parent directory handle
identities (`volume_serial`, `file_id_high`, `file_id_low`, `final_path`) before writing
the first byte and after flushing.
"""
from __future__ import annotations

import ctypes
import os
import stat
from ctypes import wintypes
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .computer.binary import (
    _FILE_ATTRIBUTE_DIRECTORY,
    _FILE_ATTRIBUTE_INVALID,
    _FILE_ATTRIBUTE_REPARSE_POINT,
    _FILE_FLAG_BACKUP_SEMANTICS,
    _FILE_FLAG_OPEN_REPARSE_POINT,
    _FILE_READ_ATTRIBUTES,
    _FILE_SHARE_READ,
    _FILE_SHARE_WRITE,
    _GENERIC_READ,
    _INVALID_HANDLE_VALUES,
    _OPEN_EXISTING,
    _close_win32_handle,
    _get_kernel32,
    _HandleIdentity,
    _query_handle_identity,
    validate_lexical_windows_path,
)
from .computer.protocol import NativeBridgeError
from .image_capabilities import _is_symlink_or_reparse

_GENERIC_WRITE = 0x40000000
_DELETE_ACCESS = 0x00010000
_CREATE_NEW = 1


@dataclass(frozen=True)
class VerifiedFileIdentity:
    """Immutable OS file identity recorded when a screenshot PNG is exclusively written."""

    final_path: str
    size: int
    volume_serial: int
    file_id_high: int
    file_id_low: int


def _raise_screenshot_invalid() -> Any:
    raise NativeBridgeError(
        "agent_computer_screenshot_invalid",
        phase="read",
        reason="malformed_json",
    )


def _configure_write_kernel32(k32: Any) -> Any:
    if not getattr(k32, "_ryn_write_configured", False):
        k32.CreateDirectoryW.restype = wintypes.BOOL
        k32.CreateDirectoryW.argtypes = [wintypes.LPCWSTR, ctypes.c_void_p]
        k32.WriteFile.restype = wintypes.BOOL
        k32.WriteFile.argtypes = [
            wintypes.HANDLE,
            ctypes.c_void_p,
            wintypes.DWORD,
            ctypes.POINTER(wintypes.DWORD),
            ctypes.c_void_p,
        ]
        k32.FlushFileBuffers.restype = wintypes.BOOL
        k32.FlushFileBuffers.argtypes = [wintypes.HANDLE]
        k32.DeleteFileW.restype = wintypes.BOOL
        k32.DeleteFileW.argtypes = [wintypes.LPCWSTR]
        k32.RemoveDirectoryW.restype = wintypes.BOOL
        k32.RemoveDirectoryW.argtypes = [wintypes.LPCWSTR]
        k32._ryn_write_configured = True
    return k32


def _same_handle_id(a: _HandleIdentity | VerifiedFileIdentity, b: _HandleIdentity | VerifiedFileIdentity) -> bool:
    return (
        a.volume_serial == b.volume_serial
        and a.file_id_high == b.file_id_high
        and a.file_id_low == b.file_id_low
        and os.path.normcase(a.final_path) == os.path.normcase(b.final_path)
    )


def _open_pinned_dir_handle(k32: Any, dir_lexical: str) -> tuple[int, _HandleIdentity]:
    """Open a directory handle without `FILE_SHARE_DELETE` so it cannot be renamed or unlinked while pinned."""
    attrs = k32.GetFileAttributesW(dir_lexical)
    if attrs == _FILE_ATTRIBUTE_INVALID or (attrs & _FILE_ATTRIBUTE_REPARSE_POINT) or not (attrs & _FILE_ATTRIBUTE_DIRECTORY):
        _raise_screenshot_invalid()
    raw_handle = k32.CreateFileW(
        dir_lexical,
        _FILE_READ_ATTRIBUTES,
        _FILE_SHARE_READ | _FILE_SHARE_WRITE,
        None,
        _OPEN_EXISTING,
        _FILE_FLAG_BACKUP_SEMANTICS | _FILE_FLAG_OPEN_REPARSE_POINT,
        None,
    )
    if raw_handle in _INVALID_HANDLE_VALUES or not raw_handle:
        _raise_screenshot_invalid()
    handle = int(raw_handle)
    try:
        ident = _query_handle_identity(handle, phase="read")
    except Exception:
        _close_win32_handle(handle)
        _raise_screenshot_invalid()
    if (
        not ident.is_directory
        or ident.is_reparse
        or os.path.normcase(ident.final_path) != os.path.normcase(dir_lexical)
    ):
        _close_win32_handle(handle)
        _raise_screenshot_invalid()
    return handle, ident


def safe_unlink_verified_file(
    target_path: Path,
    expected_identity: VerifiedFileIdentity | _HandleIdentity | None,
    *,
    ws_resolved: Path | None = None,
) -> bool:
    """Unlink `target_path` only if its handle identity still matches `expected_identity` and is not a reparse point."""
    if expected_identity is None:
        return False
    if os.name == "nt":
        try:
            k32 = _configure_write_kernel32(_get_kernel32())
            lexical = validate_lexical_windows_path(target_path, phase="read")
        except Exception:
            return False
        if os.path.normcase(lexical) != os.path.normcase(expected_identity.final_path):
            return False
        if ws_resolved is not None:
            try:
                ws_lex = validate_lexical_windows_path(ws_resolved, phase="read")
            except Exception:
                return False
            prefix = os.path.normcase(ws_lex.rstrip("\\") + "\\")
            if not os.path.normcase(lexical).startswith(prefix):
                return False
        attrs = k32.GetFileAttributesW(lexical)
        if (
            attrs == _FILE_ATTRIBUTE_INVALID
            or (attrs & _FILE_ATTRIBUTE_REPARSE_POINT)
            or (attrs & _FILE_ATTRIBUTE_DIRECTORY)
        ):
            return False
        raw_handle = k32.CreateFileW(
            lexical,
            _FILE_READ_ATTRIBUTES,
            _FILE_SHARE_READ | _FILE_SHARE_WRITE,
            None,
            _OPEN_EXISTING,
            _FILE_FLAG_OPEN_REPARSE_POINT,
            None,
        )
        if raw_handle in _INVALID_HANDLE_VALUES or not raw_handle:
            return False
        handle = int(raw_handle)
        try:
            current_ident = _query_handle_identity(handle, phase="read")
        except Exception:
            _close_win32_handle(handle)
            return False
        _close_win32_handle(handle)
        if (
            current_ident.is_directory
            or current_ident.is_reparse
            or not _same_handle_id(current_ident, expected_identity)
        ):
            return False
        return bool(k32.DeleteFileW(current_ident.final_path))

    try:
        st = os.lstat(target_path)
        if not stat.S_ISREG(st.st_mode) or stat.S_ISLNK(st.st_mode):
            return False
        if (
            int(st.st_dev) != expected_identity.volume_serial
            or int(st.st_ino) != expected_identity.file_id_low
        ):
            return False
        os.unlink(target_path)
        return True
    except OSError:
        return False


def exclusive_write_verified_png(
    target_path: Path,
    raw_bytes: bytes,
    *,
    ws_resolved: Path,
) -> VerifiedFileIdentity:
    """Create `<workspace>/.ryn/computer/<safe_task>/<safe_op>/image_N.png` with handle-verified parent containment."""
    try:
        rel_parts = target_path.relative_to(ws_resolved).parts
    except ValueError:
        _raise_screenshot_invalid()

    if len(rel_parts) != 5 or rel_parts[0] != ".ryn" or rel_parts[1] != "computer":
        _raise_screenshot_invalid()

    if os.name == "nt":
        try:
            k32 = _configure_write_kernel32(_get_kernel32())
            ws_lex = validate_lexical_windows_path(ws_resolved, phase="read")
            target_lex = validate_lexical_windows_path(target_path, phase="read")
        except Exception:
            _raise_screenshot_invalid()

        # Verify no ancestor from drive root to workspace root is a reparse point
        drive = ws_lex[:3]
        rest = ws_lex[3:]
        cur_anc = drive.rstrip("\\")
        for seg in (s for s in rest.split("\\") if s):
            cur_anc = f"{cur_anc}\\{seg}"
            attrs = k32.GetFileAttributesW(cur_anc)
            if (
                attrs == _FILE_ATTRIBUTE_INVALID
                or (attrs & _FILE_ATTRIBUTE_REPARSE_POINT)
                or not (attrs & _FILE_ATTRIBUTE_DIRECTORY)
            ):
                _raise_screenshot_invalid()

        pinned_handles: list[int] = []
        created_file_ident: _HandleIdentity | None = None
        try:
            ws_handle, ws_ident = _open_pinned_dir_handle(k32, ws_lex)
            pinned_handles.append(ws_handle)
            prev_ident = ws_ident

            for dir_part in rel_parts[:-1]:
                if (
                    not dir_part
                    or dir_part in {".", ".."}
                    or "/" in dir_part
                    or "\\" in dir_part
                    or ":" in dir_part
                ):
                    _raise_screenshot_invalid()
                child_lex = f"{prev_ident.final_path}\\{dir_part}"
                attrs = k32.GetFileAttributesW(child_lex)
                if attrs == _FILE_ATTRIBUTE_INVALID:
                    if not k32.CreateDirectoryW(child_lex, None):
                        err = ctypes.get_last_error()
                        if err != 183:  # ERROR_ALREADY_EXISTS
                            _raise_screenshot_invalid()

                child_handle, child_ident = _open_pinned_dir_handle(k32, child_lex)
                pinned_handles.append(child_handle)
                if (
                    child_ident.volume_serial != ws_ident.volume_serial
                    or os.path.normcase(child_ident.final_path) != os.path.normcase(child_lex)
                ):
                    _raise_screenshot_invalid()
                # Re-query parent handle to ensure parent remained fixed during child open
                recheck_parent = _query_handle_identity(pinned_handles[-2], phase="read")
                if (
                    recheck_parent.is_reparse
                    or not recheck_parent.is_directory
                    or not _same_handle_id(recheck_parent, prev_ident)
                ):
                    _raise_screenshot_invalid()
                prev_ident = child_ident

            file_name = rel_parts[-1]
            expected_file_lex = f"{prev_ident.final_path}\\{file_name}"
            if os.path.normcase(expected_file_lex) != os.path.normcase(target_lex):
                _raise_screenshot_invalid()

            existing_attrs = k32.GetFileAttributesW(expected_file_lex)
            if existing_attrs != _FILE_ATTRIBUTE_INVALID:
                _raise_screenshot_invalid()

            raw_file_handle = k32.CreateFileW(
                expected_file_lex,
                _GENERIC_READ | _GENERIC_WRITE,
                _FILE_SHARE_READ,
                None,
                _CREATE_NEW,
                _FILE_FLAG_OPEN_REPARSE_POINT,
                None,
            )
            if raw_file_handle in _INVALID_HANDLE_VALUES or not raw_file_handle:
                _raise_screenshot_invalid()
            file_handle = int(raw_file_handle)
            try:
                # Verify opened file handle identity and parent directory handle BEFORE writing first byte
                ident_before = _query_handle_identity(file_handle, phase="read")
                created_file_ident = ident_before
                parent_now = _query_handle_identity(pinned_handles[-1], phase="read")
                ws_now = _query_handle_identity(pinned_handles[0], phase="read")
                if (
                    ident_before.is_directory
                    or ident_before.is_reparse
                    or ident_before.size != 0
                    or ident_before.volume_serial != parent_now.volume_serial
                    or os.path.normcase(ident_before.final_path)
                    != os.path.normcase(expected_file_lex)
                    or not _same_handle_id(parent_now, prev_ident)
                    or not _same_handle_id(ws_now, ws_ident)
                ):
                    _raise_screenshot_invalid()

                total = len(raw_bytes)
                offset = 0
                while offset < total:
                    chunk = raw_bytes[offset : offset + 65536]
                    buf = ctypes.create_string_buffer(chunk, len(chunk))
                    written = wintypes.DWORD(0)
                    ok = k32.WriteFile(
                        wintypes.HANDLE(file_handle),
                        buf,
                        len(chunk),
                        ctypes.byref(written),
                        None,
                    )
                    if not ok or int(written.value) <= 0:
                        _raise_screenshot_invalid()
                    offset += int(written.value)

                if not k32.FlushFileBuffers(wintypes.HANDLE(file_handle)):
                    _raise_screenshot_invalid()

                ident_after = _query_handle_identity(file_handle, phase="read")
                parent_after = _query_handle_identity(pinned_handles[-1], phase="read")
                if (
                    ident_after.is_directory
                    or ident_after.is_reparse
                    or ident_after.size != total
                    or not _same_handle_id(ident_after, ident_before)
                    or not _same_handle_id(parent_after, prev_ident)
                ):
                    _raise_screenshot_invalid()
                created_file_ident = ident_after
            finally:
                _close_win32_handle(file_handle)

            return VerifiedFileIdentity(
                final_path=created_file_ident.final_path,
                size=created_file_ident.size,
                volume_serial=created_file_ident.volume_serial,
                file_id_high=created_file_ident.file_id_high,
                file_id_low=created_file_ident.file_id_low,
            )
        except BaseException:
            if created_file_ident is not None:
                safe_unlink_verified_file(
                    Path(created_file_ident.final_path),
                    created_file_ident,
                    ws_resolved=ws_resolved,
                )
            raise
        finally:
            for h in reversed(pinned_handles):
                _close_win32_handle(h)

    # POSIX fallback
    curr = ws_resolved
    for part in rel_parts[:-1]:
        curr = curr / part
        if curr.exists():
            if _is_symlink_or_reparse(curr) or not curr.is_dir():
                _raise_screenshot_invalid()
        else:
            curr.mkdir(parents=False, exist_ok=True)
            if _is_symlink_or_reparse(curr) or not curr.is_dir():
                _raise_screenshot_invalid()

    parent_resolved = curr.resolve(strict=True)
    if not parent_resolved.is_relative_to(ws_resolved):
        _raise_screenshot_invalid()

    final_path = parent_resolved / rel_parts[-1]
    if final_path.exists() or _is_symlink_or_reparse(final_path):
        _raise_screenshot_invalid()

    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0)
    fd = os.open(str(final_path), flags, 0o600)
    st_after = None
    try:
        st_before = os.fstat(fd)
        if not stat.S_ISREG(st_before.st_mode) or stat.S_ISLNK(st_before.st_mode):
            _raise_screenshot_invalid()
        view = memoryview(raw_bytes)
        written_bytes = 0
        while written_bytes < len(raw_bytes):
            n = os.write(fd, view[written_bytes:])
            if n <= 0:
                raise OSError("short_write")
            written_bytes += n
        os.fsync(fd)
        st_after = os.fstat(fd)
    except BaseException:
        try:
            os.close(fd)
        except OSError:
            pass
        try:
            os.unlink(final_path)
        except OSError:
            pass
        _raise_screenshot_invalid()
    else:
        os.close(fd)

    if _is_symlink_or_reparse(final_path):
        try:
            os.unlink(final_path)
        except OSError:
            pass
        _raise_screenshot_invalid()
    resolved_file = final_path.resolve(strict=True)
    if resolved_file.parent != parent_resolved or not resolved_file.is_relative_to(ws_resolved):
        try:
            os.unlink(final_path)
        except OSError:
            pass
        _raise_screenshot_invalid()

    return VerifiedFileIdentity(
        final_path=str(resolved_file),
        size=len(raw_bytes),
        volume_serial=int(st_after.st_dev) if st_after is not None else 0,
        file_id_high=0,
        file_id_low=int(st_after.st_ino) if st_after is not None else 0,
    )


def cleanup_task_verified_artifacts(
    entries: list[ tuple[Path, VerifiedFileIdentity | None, Path] ],
    *,
    safe_task: str,
    ws_roots: set[Path],
) -> None:
    """Safely remove only verified task-owned screenshot files and empty non-reparse directories."""
    op_dirs: set[ tuple[Path, Path] ] = set()
    for target_path, file_ident, ws_root in entries:
        safe_unlink_verified_file(target_path, file_ident, ws_resolved=ws_root)
        op_dirs.add((target_path.parent, ws_root))

    # Remove empty <safe_op> directories only if non-reparse and inside <ws_root>/.ryn/computer/<safe_task>
    for op_dir, ws_root in sorted(op_dirs, key=lambda item: str(item[0]), reverse=True):
        try:
            if not op_dir.exists() or _is_symlink_or_reparse(op_dir) or not op_dir.is_dir():
                continue
            resolved_op = op_dir.resolve(strict=True)
            expected_task_dir = (ws_root / ".ryn" / "computer" / safe_task).resolve(strict=True)
            if resolved_op.parent != expected_task_dir or not resolved_op.is_relative_to(ws_root):
                continue
            resolved_op.rmdir()
        except OSError:
            pass

    for ws_root in ws_roots:
        try:
            task_dir = ws_root / ".ryn" / "computer" / safe_task
            if not task_dir.exists() or _is_symlink_or_reparse(task_dir) or not task_dir.is_dir():
                continue
            resolved_task = task_dir.resolve(strict=True)
            expected_comp_dir = (ws_root / ".ryn" / "computer").resolve(strict=True)
            if resolved_task.parent != expected_comp_dir or not resolved_task.is_relative_to(ws_root):
                continue
            resolved_task.rmdir()
        except OSError:
            pass
