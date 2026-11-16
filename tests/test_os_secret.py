"""Tests for S13-I15 Step 1 (T1): OS-protected secret wrapper (DPAPI / Keychain / file fallback)."""
from __future__ import annotations

import importlib
import sys

import pytest


def test_t1_protect_unprotect_roundtrip_and_tamper_detection() -> None:
    """T1 (covers 1): On Windows (and current platform), protect/unprotect roundtrips arbitrary payloads, produces non-plaintext ciphertext on DPAPI/Keychain, and rejects tampered blobs."""
    os_secret = importlib.import_module("rynmesh.os_secret")
    mode = os_secret.protection_mode()
    if sys.platform == "win32":
        assert mode == "dpapi"
        assert os_secret.protection == "dpapi"

    for sample in (b"nas-fernet-secret-key-bytes-1234567890", b"", "中文密码-hunter2!@#".encode("utf-8")):
        blob = os_secret.protect(sample)
        assert isinstance(blob, bytes)
        if mode != "file" and sample:
            assert sample not in blob
        assert os_secret.unprotect(blob) == sample

    if mode == "dpapi":
        valid_blob = bytearray(os_secret.protect(b"sensitive-secret"))
        valid_blob[-1] ^= 0xFF
        with pytest.raises(ValueError):
            os_secret.unprotect(bytes(valid_blob))


def test_t1_other_platform_falls_back_to_file_mode(monkeypatch: pytest.MonkeyPatch) -> None:
    """T1 (covers 1): On unsupported platforms (e.g. linux without OS wrapper), protection='file' and bytes pass through unchanged."""
    os_secret = importlib.import_module("rynmesh.os_secret")
    monkeypatch.setattr(os_secret.sys, "platform", "linux")
    assert os_secret.protection_mode() == "file"
    raw = b"raw-secret-on-file-platform"
    assert os_secret.protect(raw) == raw
    assert os_secret.unprotect(raw) == raw
