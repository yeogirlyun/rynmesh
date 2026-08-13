import pytest

from rynmesh import release
from rynmesh.crypto import SignedPayload, public_key_from_private, sign_payload


def _keypair():
    import os
    priv = os.urandom(32)
    return priv, public_key_from_private(priv)


def test_build_manifest_shape():
    m = release.build_manifest(version="0.3.0", wheel_sha256="abc", wheel_size=10,
                               wheel_filename="rynmesh-0.3.0-py3-none-any.whl",
                               requires_python=">=3.11", published_at="2026-06-03T00:00:00Z", notes="n")
    assert m["kind"] == "rynmesh.release" and m["version"] == "0.3.0" and m["wheel_sha256"] == "abc"


def test_verify_manifest_accepts_pinned_signer():
    priv, pub = _keypair()
    m = release.build_manifest(version="0.3.0", wheel_sha256="abc", wheel_size=10,
                               wheel_filename="w.whl", requires_python=">=3.11",
                               published_at="2026-06-03T00:00:00Z", notes="")
    signed = sign_payload(m, private_key_bytes=priv)
    out = release.verify_manifest(signed, pinned_pubkeys={pub})
    assert out["version"] == "0.3.0"


def test_verify_manifest_rejects_unpinned_signer():
    priv, _pub = _keypair()
    _other_priv, other_pub = _keypair()
    m = release.build_manifest(version="0.3.0", wheel_sha256="abc", wheel_size=10,
                               wheel_filename="w.whl", requires_python=">=3.11",
                               published_at="2026-06-03T00:00:00Z", notes="")
    signed = sign_payload(m, private_key_bytes=priv)
    with pytest.raises(release.ReleaseError):
        release.verify_manifest(signed, pinned_pubkeys={other_pub})


def test_verify_manifest_rejects_forged_signature():
    priv, pub = _keypair()
    m = release.build_manifest(version="0.3.0", wheel_sha256="abc", wheel_size=10,
                               wheel_filename="w.whl", requires_python=">=3.11",
                               published_at="2026-06-03T00:00:00Z", notes="")
    signed = sign_payload(m, private_key_bytes=priv)
    tampered = SignedPayload(payload={**signed.payload, "wheel_sha256": "EVIL"},
                             signature=signed.signature, public_key=signed.public_key)
    with pytest.raises(release.ReleaseError):
        release.verify_manifest(tampered, pinned_pubkeys={pub})


def test_verify_manifest_rejects_wrong_kind():
    priv, pub = _keypair()
    bad = {"kind": "not.release", "version": "9.9.9"}
    signed = sign_payload(bad, private_key_bytes=priv)
    with pytest.raises(release.ReleaseError):
        release.verify_manifest(signed, pinned_pubkeys={pub})


@pytest.mark.parametrize("cand,cur,expected", [
    ("0.3.0", "0.2.0", True), ("0.2.0", "0.2.0", False), ("0.1.9", "0.2.0", False),
    ("1.0.0", "0.9.9", True), ("0.2.10", "0.2.2", True),
])
def test_is_newer(cand, cur, expected):
    assert release.is_newer(cand, cur) is expected


def test_verify_manifest_rejects_empty_pinned_set():
    priv, _pub = _keypair()
    m = release.build_manifest(version="0.3.0", wheel_sha256="abc", wheel_size=10,
                               wheel_filename="w.whl", requires_python=">=3.11",
                               published_at="2026-06-03T00:00:00Z", notes="")
    signed = sign_payload(m, private_key_bytes=priv)
    with pytest.raises(release.ReleaseError):
        release.verify_manifest(signed, pinned_pubkeys=set())  # fail-closed, NOT accept-all


def test_verify_manifest_rejects_alg_confusion():
    priv, pub = _keypair()
    m = release.build_manifest(version="0.3.0", wheel_sha256="abc", wheel_size=10,
                               wheel_filename="w.whl", requires_python=">=3.11",
                               published_at="2026-06-03T00:00:00Z", notes="")
    signed = sign_payload(m, private_key_bytes=priv)
    forged = SignedPayload(payload=signed.payload, signature=signed.signature,
                           public_key=signed.public_key, alg="none")
    with pytest.raises(release.ReleaseError):
        release.verify_manifest(forged, pinned_pubkeys={pub})


def test_verify_manifest_rejects_missing_version_or_hash():
    priv, pub = _keypair()
    for bad in ({"kind": "rynmesh.release", "version": "", "wheel_sha256": "abc"},
                {"kind": "rynmesh.release", "version": "0.3.0", "wheel_sha256": ""}):
        signed = sign_payload(bad, private_key_bytes=priv)
        with pytest.raises(release.ReleaseError):
            release.verify_manifest(signed, pinned_pubkeys={pub})
