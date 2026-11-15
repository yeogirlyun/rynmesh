from __future__ import annotations

import json
import logging

from cryptography.fernet import Fernet

from rynmesh.nas import NasPlugin


class _StubSMBConnector:
    def __init__(self, config):
        self.config = config

    def list(self, path):
        return []

    def close(self):
        return None


def _sample_smb_body(password: str = "top-secret-nas-pw") -> dict:
    return {
        "name": "Home NAS",
        "system": "fnos",
        "protocol": "smb",
        "host": "nas.local",
        "share": "documents",
        "username": "tester",
        "password": password,
        "writable": True,
        "allow_ai": False,
    }


def _make_configured_plugin(home, password: str = "top-secret-nas-pw") -> NasPlugin:
    plugin = NasPlugin(home)
    plugin.connectors["smb"] = _StubSMBConnector
    plugin.enable(True)
    plugin.configure(_sample_smb_body(password=password))
    return plugin


def test_missing_key_with_existing_state_isolates_corrupt_state_and_recovers(tmp_path, caplog):
    _make_configured_plugin(tmp_path, password="secret-password-t1")
    nas_dir = tmp_path / "plugins" / "nas"
    original_ciphertext = (nas_dir / "state.enc").read_bytes()
    assert b"secret-password-t1" not in original_ciphertext
    (nas_dir / "key").unlink()

    with caplog.at_level(logging.WARNING, logger="rynmesh.nas"):
        recovered = NasPlugin(tmp_path)

    assert recovered.cipher is None
    assert not (nas_dir / "state.enc").exists()
    corrupt_files = list(nas_dir.glob("state.enc.corrupt-*"))
    assert len(corrupt_files) == 1
    suffix = corrupt_files[0].name.split("state.enc.corrupt-", 1)[1]
    assert suffix.isdigit()
    assert corrupt_files[0].read_bytes() == original_ciphertext
    for item in nas_dir.iterdir():
        assert b"secret-password-t1" not in item.read_bytes()

    assert any(rec.levelno == logging.WARNING for rec in caplog.records)

    status = recovered.status()
    assert status["enabled"] is False
    assert status["sources"] == []
    assert "recovered_from_corruption" in status
    info = status["recovered_from_corruption"]
    assert set(info.keys()) == {"at", "reason"}
    assert isinstance(info["at"], int)
    assert info["reason"] == "FileNotFoundError"
    serialized_status = json.dumps(status, ensure_ascii=False)
    assert str(tmp_path) not in serialized_status
    assert "corrupt-" not in serialized_status
    assert "secret-password-t1" not in serialized_status

    recovered.enable(True)
    assert (nas_dir / "state.enc").exists()
    assert (nas_dir / "key").exists()
    assert corrupt_files[0].exists()

    reloaded = NasPlugin(tmp_path)
    reloaded_status = reloaded.status()
    assert reloaded_status["enabled"] is True
    assert "recovered_from_corruption" not in reloaded_status
    assert corrupt_files[0].exists()


def test_garbage_or_corrupt_state_enc_isolates_corrupt_file_and_recovers(tmp_path, caplog):
    home_garbage = tmp_path / "garbage_state"
    _make_configured_plugin(home_garbage, password="secret-password-t2")
    nas_dir = home_garbage / "plugins" / "nas"
    existing_key = (nas_dir / "key").read_bytes()
    (nas_dir / "state.enc").write_bytes(b"garbage-not-fernet-token")

    with caplog.at_level(logging.WARNING, logger="rynmesh.nas"):
        recovered = NasPlugin(home_garbage)

    assert recovered.cipher is None
    corrupt_states = list(nas_dir.glob("state.enc.corrupt-*"))
    assert len(corrupt_states) == 1
    assert corrupt_states[0].read_bytes() == b"garbage-not-fernet-token"
    assert any(rec.levelno == logging.WARNING for rec in caplog.records)

    status = recovered.status()
    assert status["enabled"] is False
    assert status["sources"] == []
    assert status["recovered_from_corruption"]["reason"] == "InvalidToken"
    assert isinstance(status["recovered_from_corruption"]["at"], int)
    assert "corrupt-" not in json.dumps(status)

    recovered.save()
    assert (nas_dir / "key").read_bytes() == existing_key
    assert (nas_dir / "state.enc").exists()
    assert "recovered_from_corruption" not in NasPlugin(home_garbage).status()

    home_bad_json = tmp_path / "bad_json_state"
    bad_json_dir = home_bad_json / "plugins" / "nas"
    bad_json_dir.mkdir(parents=True)
    valid_key = Fernet.generate_key()
    (bad_json_dir / "key").write_bytes(valid_key)
    (bad_json_dir / "state.enc").write_bytes(Fernet(valid_key).encrypt(b"{not-valid-json"))
    recovered_json = NasPlugin(home_bad_json)
    assert recovered_json.status()["recovered_from_corruption"]["reason"] == "JSONDecodeError"
    assert len(list(bad_json_dir.glob("state.enc.corrupt-*"))) == 1
    recovered_json.save()

    home_bad_key = tmp_path / "bad_key_with_state"
    _make_configured_plugin(home_bad_key, password="secret-password-t2b")
    bad_key_dir = home_bad_key / "plugins" / "nas"
    (bad_key_dir / "key").write_bytes(b"broken-key-bytes")
    recovered_both = NasPlugin(home_bad_key)
    assert recovered_both.status()["recovered_from_corruption"]["reason"] == "ValueError"
    assert len(list(bad_key_dir.glob("state.enc.corrupt-*"))) == 1
    assert len(list(bad_key_dir.glob("key.corrupt-*"))) == 1
    recovered_both.save()
    assert (bad_key_dir / "key").exists()
    assert (bad_key_dir / "state.enc").exists()


def test_existing_valid_key_without_state_reuses_key_on_first_save(tmp_path):
    nas_dir = tmp_path / "plugins" / "nas"
    nas_dir.mkdir(parents=True)
    original_key = Fernet.generate_key()
    (nas_dir / "key").write_bytes(original_key)

    plugin = NasPlugin(tmp_path)
    plugin.connectors["smb"] = _StubSMBConnector
    assert "recovered_from_corruption" not in plugin.status()

    plugin.enable(True)
    plugin.configure(_sample_smb_body(password="reused-key-secret"))

    assert (nas_dir / "key").read_bytes() == original_key
    assert list(nas_dir.glob("key.corrupt-*")) == []
    assert (nas_dir / "state.enc").exists()

    restarted = NasPlugin(tmp_path)
    restarted_status = restarted.status()
    assert "recovered_from_corruption" not in restarted_status
    assert restarted_status["enabled"] is True
    assert len(restarted_status["sources"]) == 1
    assert restarted_status["sources"][0]["name"] == "Home NAS"


def test_corrupt_key_without_state_isolates_corrupt_key_and_creates_new_key(tmp_path):
    nas_dir = tmp_path / "plugins" / "nas"
    nas_dir.mkdir(parents=True)
    (nas_dir / "key").write_bytes(b"corrupt-key-not-base64-fernet")

    plugin = NasPlugin(tmp_path)
    plugin.connectors["smb"] = _StubSMBConnector
    plugin.enable(True)
    plugin.configure(_sample_smb_body(password="new-key-secret"))

    corrupt_keys = list(nas_dir.glob("key.corrupt-*"))
    assert len(corrupt_keys) == 1
    assert corrupt_keys[0].read_bytes() == b"corrupt-key-not-base64-fernet"
    new_key = (nas_dir / "key").read_bytes()
    Fernet(new_key)

    restarted = NasPlugin(tmp_path)
    restarted_status = restarted.status()
    assert "recovered_from_corruption" not in restarted_status
    assert restarted_status["enabled"] is True
    assert len(restarted_status["sources"]) == 1
    assert corrupt_keys[0].exists()


def test_normal_state_roundtrip_unchanged(tmp_path):
    plugin = _make_configured_plugin(tmp_path, password="normal-secret-value")
    nas_dir = tmp_path / "plugins" / "nas"

    first_status = plugin.status()
    assert "recovered_from_corruption" not in first_status
    assert first_status["enabled"] is True
    assert len(first_status["sources"]) == 1
    assert b"normal-secret-value" not in (nas_dir / "state.enc").read_bytes()
    assert list(nas_dir.glob("*.corrupt-*")) == []

    restored = NasPlugin(tmp_path)
    restored_status = restored.status()
    assert "recovered_from_corruption" not in restored_status
    assert restored_status["enabled"] is True
    assert len(restored_status["sources"]) == 1
    assert restored_status["sources"][0]["name"] == "Home NAS"
    assert restored_status["sources"][0]["share"] == "documents"
