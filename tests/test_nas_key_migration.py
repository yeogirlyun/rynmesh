"""Tests for S13-I15 Step 2 (T2): NAS key.protected creation and legacy key -> key.protected migration."""
from __future__ import annotations

import importlib
import json
from pathlib import Path

from cryptography.fernet import Fernet

from rynmesh.nas import NasPlugin


def test_t2_legacy_nas_key_migrates_to_key_protected_and_preserves_credentials(
    tmp_path: Path,
) -> None:
    """T2 (covers 2): Starting NasPlugin when only legacy 'key' + 'state.enc' exist decrypts credentials, writes 'key.protected', and renames 'key' to 'key.bak-<timestamp>'."""
    os_secret = importlib.import_module("rynmesh.os_secret")
    nas_dir = tmp_path / "plugins" / "nas"
    nas_dir.mkdir(parents=True, exist_ok=True)

    legacy_key = Fernet.generate_key()
    legacy_state = {
        "enabled": True,
        "sources": [
            {
                "id": "src-1",
                "name": "Office NAS",
                "system": "synology",
                "protocol": "smb",
                "host": "192.0.2.10",
                "share": "docs",
                "username": "alice",
                "password": "super-secret-smb-password",
                "writable": False,
                "allow_ai": False,
            }
        ],
    }
    (nas_dir / "key").write_bytes(legacy_key)
    (nas_dir / "state.enc").write_bytes(
        Fernet(legacy_key).encrypt(json.dumps(legacy_state).encode("utf-8"))
    )

    plugin = NasPlugin(tmp_path)
    assert plugin.state["enabled"] is True
    assert len(plugin.state["sources"]) == 1
    assert plugin.state["sources"][0]["password"] == "super-secret-smb-password"

    assert not (nas_dir / "key").exists()
    protected_path = nas_dir / "key.protected"
    assert protected_path.is_file()
    protected_bytes = protected_path.read_bytes()
    if os_secret.protection_mode() != "file":
        assert legacy_key not in protected_bytes
    assert os_secret.unprotect(protected_bytes) == legacy_key

    backups = list(nas_dir.glob("key.bak-*"))
    assert len(backups) == 1
    assert backups[0].read_bytes() == legacy_key

    # Subsequent restart reads key.protected cleanly
    reloaded = NasPlugin(tmp_path)
    assert reloaded.state["sources"][0]["password"] == "super-secret-smb-password"


def test_t2_fresh_nas_save_writes_key_protected_only(tmp_path: Path) -> None:
    """T2 (covers 2): Fresh NasPlugin._save writes key.protected and never creates plaintext 'key'."""
    os_secret = importlib.import_module("rynmesh.os_secret")
    home = tmp_path / "fresh"
    plugin = NasPlugin(home)
    plugin.enable(True)

    nas_dir = home / "plugins" / "nas"
    assert (nas_dir / "key.protected").is_file()
    assert not (nas_dir / "key").exists()
    raw_key = os_secret.unprotect((nas_dir / "key.protected").read_bytes())
    Fernet(raw_key)
