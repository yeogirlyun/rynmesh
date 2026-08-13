"""rynmesh-publish: build a wheel, sign a release manifest, upload to a registry.

Usage:
  rynmesh-publish --registry http://203.0.113.10:8790 --key publisher.key [--notes "..."]
The --key file holds the 32-byte Ed25519 private key (raw or base64).
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import json
import subprocess
import sys
import urllib.request
from datetime import UTC, datetime
from pathlib import Path

from rynmesh import release
from rynmesh.crypto import SignedPayload, sign_payload


def build_signed_manifest(wheel_path: str, *, version: str, private_key_bytes: bytes,
                          requires_python: str, published_at: str, notes: str) -> SignedPayload:
    data = Path(wheel_path).read_bytes()
    manifest = release.build_manifest(
        version=version, wheel_sha256=hashlib.sha256(data).hexdigest(), wheel_size=len(data),
        wheel_filename=Path(wheel_path).name, requires_python=requires_python,
        published_at=published_at, notes=notes,
    )
    return sign_payload(manifest, private_key_bytes=private_key_bytes)


def _load_key(path: str) -> bytes:
    raw = Path(path).read_bytes().strip()
    if len(raw) == 32:
        return raw
    try:
        dec = base64.b64decode(raw)
        if len(dec) == 32:
            return dec
    except Exception:
        pass
    raise SystemExit("publisher key must be 32 raw bytes or base64 of 32 bytes")


def _post(url: str, data: bytes, content_type: str) -> bytes:
    req = urllib.request.Request(url, data=data, headers={"content-type": content_type}, method="POST")
    with urllib.request.urlopen(req, timeout=60) as r:
        return r.read()


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Build + sign + upload a rynmesh release")
    ap.add_argument("--registry", required=True)
    ap.add_argument("--key", required=True)
    ap.add_argument("--notes", default="")
    ap.add_argument("--requires-python", default=">=3.11")
    args = ap.parse_args(argv)

    priv = _load_key(args.key)
    print("Building wheel...")
    subprocess.run([sys.executable, "-m", "build", "--wheel", "--outdir", "dist"], check=True)
    wheels = sorted(Path("dist").glob("rynmesh-*.whl"), key=lambda p: p.stat().st_mtime)
    wheel = wheels[-1]
    version = wheel.name.split("-")[1]
    signed = build_signed_manifest(str(wheel), version=version, private_key_bytes=priv,
                                   requires_python=args.requires_python,
                                   published_at=datetime.now(UTC).isoformat(), notes=args.notes)
    print(f"Uploading wheel {wheel.name} ({signed.payload['wheel_size']} bytes) to relay...")
    _post(f"{args.registry}/api/v1/relay/blobs", wheel.read_bytes(), "application/octet-stream")
    print("Posting signed release manifest...")
    _post(f"{args.registry}/api/v1/releases", json.dumps(signed.to_dict()).encode(), "application/json")
    print(f"Published rynmesh {version} (sha256={signed.payload['wheel_sha256'][:12]}...)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
