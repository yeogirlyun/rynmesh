"""Windows discretionary ACLs for private node records (chmod is insufficient).

Only the running user, SYSTEM and local administrators receive access.
The caller must own the specific file/directory; errors are fail-closed and
contain no filesystem paths or account names. No shell commands are used.
"""

from __future__ import annotations

import ctypes as c
import logging
import os
import re
import sys
import threading
import uuid
from ctypes import wintypes as w
from functools import lru_cache
from pathlib import Path

_logger = logging.getLogger(__name__)

_DACL = 4
_PROTECTED_DACL = 0x80000000
_ACE_RE = re.compile(r"\(([^)]+)\)")

_WEAK_LOCK = threading.Lock()
_WEAK_ACL_PATHS: set[str] = set()


def _norm_key_path(path: str | Path) -> str:
    return os.path.normcase(os.path.abspath(os.fspath(path)))


def mark_key_protection(path: str | Path, *, weak: bool) -> None:
    norm = _norm_key_path(path)
    with _WEAK_LOCK:
        if weak:
            _WEAK_ACL_PATHS.add(norm)
        else:
            _WEAK_ACL_PATHS.discard(norm)


def key_protection_status(home: str | Path | None = None) -> str:
    """Return ``'weak'`` if any tracked private key under ``home`` failed ACL hardening, else ``'ok'``."""
    with _WEAK_LOCK:
        if not _WEAK_ACL_PATHS:
            return "ok"
        if home is None:
            return "weak"
        prefix = _norm_key_path(home)
        sep = prefix if prefix.endswith(os.sep) else (prefix + os.sep)
        for item in _WEAK_ACL_PATHS:
            if item == prefix or item.startswith(sep):
                return "weak"
    return "ok"


@lru_cache(maxsize=1)
def _api():
    adv = c.WinDLL("advapi32", use_last_error=True)
    kernel = c.WinDLL("kernel32", use_last_error=True)
    pointer = c.c_void_p
    pp = c.POINTER(pointer)
    signatures = {
        "OpenProcessToken": ([w.HANDLE, w.DWORD, c.POINTER(w.HANDLE)], w.BOOL),
        "GetTokenInformation": ([w.HANDLE, c.c_int, pointer, w.DWORD, c.POINTER(w.DWORD)], w.BOOL),
        "ConvertSidToStringSidW": ([pointer, c.POINTER(w.LPWSTR)], w.BOOL),
        "ConvertStringSecurityDescriptorToSecurityDescriptorW": ([w.LPCWSTR, w.DWORD, pp, pointer], w.BOOL),
        "GetSecurityDescriptorDacl": ([pointer, c.POINTER(w.BOOL), pp, c.POINTER(w.BOOL)], w.BOOL),
        "SetNamedSecurityInfoW": ([w.LPWSTR, c.c_int, w.DWORD, pointer, pointer, pointer, pointer], w.DWORD),
        "GetNamedSecurityInfoW": ([w.LPCWSTR, c.c_int, w.DWORD, pp, pp, pp, pp, pp], w.DWORD),
        "ConvertSecurityDescriptorToStringSecurityDescriptorW": ([pointer, w.DWORD, w.DWORD, c.POINTER(w.LPWSTR), pointer], w.BOOL),
    }
    for name, (args, result) in signatures.items():
        fn = getattr(adv, name)
        fn.argtypes, fn.restype = args, result
    kernel.GetCurrentProcess.argtypes, kernel.GetCurrentProcess.restype = [], w.HANDLE
    kernel.CloseHandle.argtypes, kernel.CloseHandle.restype = [w.HANDLE], w.BOOL
    kernel.LocalFree.argtypes, kernel.LocalFree.restype = [pointer], pointer
    return adv, kernel


@lru_cache(maxsize=1)
def current_user_sid() -> str:
    adv, kernel = _api()
    token = w.HANDLE()
    if not adv.OpenProcessToken(kernel.GetCurrentProcess(), 8, c.byref(token)):
        raise OSError("private_acl_token_unavailable")
    try:
        size = w.DWORD()
        adv.GetTokenInformation(token, 1, None, 0, c.byref(size))
        buffer = c.create_string_buffer(size.value)
        if not adv.GetTokenInformation(token, 1, buffer, size, c.byref(size)):
            raise OSError("private_acl_token_unavailable")
        sid = c.cast(buffer, c.POINTER(c.c_void_p))[0]  # TOKEN_USER starts with SID_AND_ATTRIBUTES.
        string = w.LPWSTR()
        if not adv.ConvertSidToStringSidW(sid, c.byref(string)):
            raise OSError("private_acl_sid_unavailable")
        try:
            return string.value
        finally:
            kernel.LocalFree(string)
    finally:
        kernel.CloseHandle(token)


def read_windows_dacl(path: str | Path) -> str:
    adv, kernel = _api()
    descriptor = c.c_void_p()
    if adv.GetNamedSecurityInfoW(str(path), 1, _DACL, None, None, None, None, c.byref(descriptor)):
        raise OSError("private_acl_read_failed")
    string = w.LPWSTR()
    try:
        if not adv.ConvertSecurityDescriptorToStringSecurityDescriptorW(
            descriptor, 1, _DACL, c.byref(string), None
        ):
            raise OSError("private_acl_read_failed")
        return string.value
    finally:
        if string:
            kernel.LocalFree(string)
        kernel.LocalFree(descriptor)


def is_windows_dacl_restricted(dacl: str, *, directory: bool = False) -> bool:
    """Check whether ``dacl`` is protected and grants access only to SYSTEM, Administrators, and current user."""
    if not isinstance(dacl, str) or "(" not in dacl:
        return False
    header = dacl.split("(", 1)[0]
    if not header.startswith("D:") or "P" not in header[2:]:
        return False
    entries = _ACE_RE.findall(dacl)
    if not entries:
        return False
    expected_flags = "OICI" if directory else ""
    allowed_sids = {"SY", "BA", current_user_sid()}
    seen_sids: set[str] = set()
    for entry in entries:
        parts = entry.split(";")
        if len(parts) != 6:
            return False
        ace_type, ace_flags, rights, _obj_guid, _inh_guid, account_sid = parts
        if ace_type != "A" or ace_flags != expected_flags or rights != "FA":
            return False
        if account_sid not in allowed_sids:
            return False
        seen_sids.add(account_sid)
    return seen_sids == allowed_sids


def restrict_windows_acl(path: str | Path, *, directory: bool = False) -> None:
    adv, kernel = _api()
    inheritance = "OICI" if directory else ""
    sddl = "D:P" + "".join(f"(A;{inheritance};FA;;;{sid})" for sid in (
        "SY", "BA", current_user_sid()
    ))
    # Avoid repeatedly propagating identical directory ACLs through the spool.
    if directory and is_windows_dacl_restricted(read_windows_dacl(path), directory=True):
        return
    descriptor = c.c_void_p()
    if not adv.ConvertStringSecurityDescriptorToSecurityDescriptorW(sddl, 1, c.byref(descriptor), None):
        raise OSError("private_acl_invalid")
    try:
        present, defaulted, acl = w.BOOL(), w.BOOL(), c.c_void_p()
        if not adv.GetSecurityDescriptorDacl(descriptor, c.byref(present), c.byref(acl), c.byref(defaulted)) or not present or not acl:
            raise OSError("private_acl_invalid")
        if adv.SetNamedSecurityInfoW(str(path), 1, _DACL | _PROTECTED_DACL, None, None, acl, None):
            raise OSError("private_acl_write_failed")
    finally:
        kernel.LocalFree(descriptor)


_DEFAULT_RESTRICT_ACL = restrict_windows_acl
_DEFAULT_READ_DACL = read_windows_dacl


def ensure_private_file_acl(
    path: str | Path,
    *,
    track_key_status: bool = True,
    acl_fn=None,
    dacl_fn=None,
    logger: logging.Logger | None = None,
) -> bool:
    """Verify and tighten file permissions; log warning (no path) and mark weak on failure."""
    target = Path(path)
    log = logger or _logger
    try:
        os.chmod(target, 0o600)
    except OSError:
        pass
    if sys.platform != "win32":
        if track_key_status:
            mark_key_protection(target, weak=False)
        return True
    effective_restrict = acl_fn if (acl_fn is not None and acl_fn is not _DEFAULT_RESTRICT_ACL) else restrict_windows_acl
    effective_read = dacl_fn if (dacl_fn is not None and dacl_fn is not _DEFAULT_READ_DACL) else read_windows_dacl
    try:
        if effective_restrict is not _DEFAULT_RESTRICT_ACL:
            # Caller or test explicitly overrode restrict_windows_acl: invoke it directly.
            effective_restrict(target)
        else:
            dacl = effective_read(target)
            if not is_windows_dacl_restricted(dacl, directory=False):
                effective_restrict(target)
        if track_key_status:
            mark_key_protection(target, weak=False)
        return True
    except OSError as exc:
        log.warning("Failed to restrict Windows ACL on private key record (%s)", type(exc).__name__)
        if track_key_status:
            mark_key_protection(target, weak=True)
        return False


def write_private_bytes_exclusive(
    path: str | Path,
    data: bytes,
    *,
    acl_fn=None,
    dacl_fn=None,
    logger: logging.Logger | None = None,
) -> bool:
    """Create ``path`` via ``atomic_write_bytes`` without overwriting an existing file.

    Returns ``True`` if this call created ``path``, or raises ``FileExistsError`` if
    another thread/process created ``path`` first. If Windows ACL enforcement fails
    (e.g. FAT/shared volume), writes durably with a warning and marks ``key_protection='weak'``.
    """
    from .atomic_io import AtomicIOError, atomic_write_bytes

    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists():
        raise FileExistsError("private key already exists")
    staging = destination.with_name(f".{destination.name[:20]}.{uuid.uuid4().hex[:12]}.keyinit")
    log = logger or _logger
    acl_ok = True
    try:
        effective_restrict = acl_fn if (acl_fn is not None and acl_fn is not _DEFAULT_RESTRICT_ACL) else restrict_windows_acl
        if sys.platform == "win32" and effective_restrict is not _DEFAULT_RESTRICT_ACL:
            # Honor caller/test override of restrict_windows_acl while still writing via atomic_write_bytes.
            atomic_write_bytes(staging, data, mode=0o644)
            try:
                effective_restrict(staging)
            except OSError as exc:
                acl_ok = False
                log.warning("Failed to restrict Windows ACL on new private key (%s)", type(exc).__name__)
        else:
            try:
                atomic_write_bytes(staging, data)
            except AtomicIOError as exc:
                cause = exc.__cause__
                if sys.platform == "win32" and isinstance(cause, OSError):
                    atomic_write_bytes(staging, data, mode=0o644)
                    acl_ok = False
                    log.warning("Failed to set Windows ACL on new private key (%s); falling back to weak ACL", type(cause).__name__)
                else:
                    raise
        try:
            os.chmod(staging, 0o600)
        except OSError:
            pass
        if os.name == "nt":
            # MoveFileW fails atomically with FileExistsError if destination already exists and preserves NTFS DACL.
            os.rename(staging, destination)
        else:
            os.link(staging, destination)
            staging.unlink(missing_ok=True)
        staging = None
        mark_key_protection(destination, weak=not acl_ok)
        return True
    finally:
        if staging is not None:
            staging.unlink(missing_ok=True)
