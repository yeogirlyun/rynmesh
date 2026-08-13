import hashlib
import os

from rynmesh import release
from rynmesh.crypto import public_key_from_private
from rynmesh.tools.publish_release import build_signed_manifest


def test_build_signed_manifest_verifies(tmp_path):
    wheel = tmp_path / "rynmesh-0.3.0-py3-none-any.whl"
    wheel.write_bytes(b"PK\x03\x04 fake wheel bytes")
    priv = os.urandom(32)
    pub = public_key_from_private(priv)
    signed = build_signed_manifest(str(wheel), version="0.3.0", private_key_bytes=priv,
                                   requires_python=">=3.11", published_at="2026-06-03T00:00:00Z", notes="x")
    payload = release.verify_manifest(signed, {pub})
    assert payload["version"] == "0.3.0"
    assert payload["wheel_sha256"] == hashlib.sha256(wheel.read_bytes()).hexdigest()
    assert payload["wheel_size"] == wheel.stat().st_size
