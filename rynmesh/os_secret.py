"""OS-backed secret protection for local node credentials (DPAPI / macOS Keychain / file fallback).

- Windows: CryptProtectData / CryptUnprotectData via ctypes (no extra dependencies).
- macOS: random Fernet master key stored in login Keychain via `/usr/bin/security`.
- Other platforms: pass-through bytes with ``protection == "file"``.
"""

from __future__ import annotations

import ctypes as c
import subprocess
import sys
import threading
from ctypes import wintypes as w
from functools import lru_cache

from cryptography.fernet import Fernet, InvalidToken

_PREFIX_DPAPI = b"RYNSEC1:dpapi:"
_PREFIX_KEYCHAIN = b"RYNSEC1:keychain:"

_CRYPTPROTECT_UI_FORBIDDEN = 0x1
_KEYCHAIN_SERVICE = "com.rynmesh.os-secret"
_KEYCHAIN_ACCOUNT = "master-key-v1"
_MACOS_LOCK = threading.Lock()
_MACOS_CACHED_KEY: bytes | None = None


class OSSecretError(ValueError):
    """Failed to protect or unprotect a local secret."""


def _platform_mode(plat: str) -> str:
    if plat == "win32":
        return "dpapi"
    if plat == "darwin":
        return "keychain"
    return "file"


_BOOT_PLATFORM = sys.platform
protection: str = _platform_mode(_BOOT_PLATFORM)


def protection_mode() -> str:
    """Return active protection backend: ``'dpapi'``, ``'keychain'``, or ``'file'``."""
    if sys.platform != _BOOT_PLATFORM:
        return _platform_mode(sys.platform)
    return protection


if _BOOT_PLATFORM == "win32":

    class _DataBlob(c.Structure):
        _fields_ = [
            ("cbData", w.DWORD),
            ("pbData", c.POINTER(c.c_char)),
        ]

    @lru_cache(maxsize=1)
    def _win32_crypt_api():
        crypt32 = c.WinDLL("crypt32", use_last_error=True)
        kernel32 = c.WinDLL("kernel32", use_last_error=True)
        blob_p = c.POINTER(_DataBlob)
        crypt32.CryptProtectData.argtypes = [
            blob_p,
            w.LPCWSTR,
            blob_p,
            c.c_void_p,
            c.c_void_p,
            w.DWORD,
            blob_p,
        ]
        crypt32.CryptProtectData.restype = w.BOOL
        crypt32.CryptUnprotectData.argtypes = [
            blob_p,
            c.POINTER(w.LPWSTR),
            blob_p,
            c.c_void_p,
            c.c_void_p,
            w.DWORD,
            blob_p,
        ]
        crypt32.CryptUnprotectData.restype = w.BOOL
        kernel32.LocalFree.argtypes = [c.c_void_p]
        kernel32.LocalFree.restype = c.c_void_p
        return crypt32, kernel32


def _dpapi_protect(raw: bytes) -> bytes:
    crypt32, kernel32 = _win32_crypt_api()
    buf = c.create_string_buffer(raw, len(raw)) if raw else c.create_string_buffer(1)
    in_blob = _DataBlob(len(raw), buf)
    out_blob = _DataBlob()
    ok = crypt32.CryptProtectData(
        c.byref(in_blob),
        None,
        None,
        None,
        None,
        _CRYPTPROTECT_UI_FORBIDDEN,
        c.byref(out_blob),
    )
    if not ok:
        raise OSSecretError("os_secret_protect_failed")
    try:
        return _PREFIX_DPAPI + c.string_at(out_blob.pbData, out_blob.cbData)
    finally:
        if out_blob.pbData:
            kernel32.LocalFree(c.cast(out_blob.pbData, c.c_void_p))


def _dpapi_unprotect(payload: bytes) -> bytes:
    if not payload:
        raise OSSecretError("os_secret_unprotect_failed")
    crypt32, kernel32 = _win32_crypt_api()
    buf = c.create_string_buffer(payload, len(payload))
    in_blob = _DataBlob(len(payload), buf)
    out_blob = _DataBlob()
    ok = crypt32.CryptUnprotectData(
        c.byref(in_blob),
        None,
        None,
        None,
        None,
        _CRYPTPROTECT_UI_FORBIDDEN,
        c.byref(out_blob),
    )
    if not ok:
        raise OSSecretError("os_secret_unprotect_failed")
    try:
        return c.string_at(out_blob.pbData, out_blob.cbData)
    finally:
        if out_blob.pbData:
            kernel32.LocalFree(c.cast(out_blob.pbData, c.c_void_p))


def _macos_master_key() -> bytes:
    global _MACOS_CACHED_KEY
    with _MACOS_LOCK:
        if _MACOS_CACHED_KEY is not None:
            return _MACOS_CACHED_KEY
        try:
            proc = subprocess.run(
                [
                    "/usr/bin/security",
                    "find-generic-password",
                    "-s",
                    _KEYCHAIN_SERVICE,
                    "-a",
                    _KEYCHAIN_ACCOUNT,
                    "-w",
                ],
                capture_output=True,
                text=True,
                timeout=5,
                check=False,
            )
        except OSError as exc:
            raise OSSecretError("os_secret_keychain_unavailable") from exc
        if proc.returncode == 0:
            candidate = proc.stdout.strip().encode("ascii", errors="strict")
            try:
                Fernet(candidate)
                _MACOS_CACHED_KEY = candidate
                return candidate
            except ValueError:
                pass
        generated = Fernet.generate_key()
        try:
            subprocess.run(
                [
                    "/usr/bin/security",
                    "add-generic-password",
                    "-U",
                    "-s",
                    _KEYCHAIN_SERVICE,
                    "-a",
                    _KEYCHAIN_ACCOUNT,
                    "-w",
                    generated.decode("ascii"),
                ],
                capture_output=True,
                text=True,
                timeout=5,
                check=True,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            raise OSSecretError("os_secret_keychain_write_failed") from exc
        _MACOS_CACHED_KEY = generated
        return generated


def protect(data: bytes) -> bytes:
    """Wrap ``data`` using the host OS secret protection mechanism (or pass through in ``file`` mode)."""
    if not isinstance(data, (bytes, bytearray, memoryview)):
        raise TypeError("data must be bytes-like")
    raw = bytes(data)
    mode = protection_mode()
    if mode == "dpapi":
        return _dpapi_protect(raw)
    if mode == "keychain":
        token = Fernet(_macos_master_key()).encrypt(raw)
        return _PREFIX_KEYCHAIN + token
    return raw


def unprotect(blob: bytes) -> bytes:
    """Unwrap ``blob`` protected by :func:`protect`."""
    if not isinstance(blob, (bytes, bytearray, memoryview)):
        raise TypeError("blob must be bytes-like")
    raw_blob = bytes(blob)
    mode = protection_mode()
    if raw_blob.startswith(_PREFIX_DPAPI):
        if mode != "dpapi":
            raise OSSecretError("os_secret_platform_mismatch")
        return _dpapi_unprotect(raw_blob[len(_PREFIX_DPAPI) :])
    if raw_blob.startswith(_PREFIX_KEYCHAIN):
        if mode != "keychain":
            raise OSSecretError("os_secret_platform_mismatch")
        try:
            return Fernet(_macos_master_key()).decrypt(raw_blob[len(_PREFIX_KEYCHAIN) :])
        except InvalidToken as exc:
            raise OSSecretError("os_secret_unprotect_failed") from exc
    if mode == "dpapi":
        return _dpapi_unprotect(raw_blob)
    if mode == "keychain":
        try:
            return Fernet(_macos_master_key()).decrypt(raw_blob)
        except InvalidToken as exc:
            raise OSSecretError("os_secret_unprotect_failed") from exc
    return raw_blob
