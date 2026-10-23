"""Create a signed plugin manifest for Git Release or a company HTTPS endpoint.

Private key: raw 32-byte Ed25519 file, outside the source repository.
"""
import argparse
import hashlib
import json
from pathlib import Path

from rynmesh.crypto import sign_payload
from rynmesh.node_apps.craft_catalog import CRAFT
from rynmesh.node_apps.plugin_updates import validate_source, verify_release


def sign_release(*, slug, version, archive, url, manifest_url, kind, key, min_host, max_host):
    archive = Path(archive)
    payload = {"kind": "ryn.plugin.release", "schema": 1, "id": slug, "version": version,
               "runtime": "static-web", "permissions": [], "min_host_version": min_host,
               "max_host_version": max_host, "url": url, "size": archive.stat().st_size,
               "sha256": hashlib.sha256(archive.read_bytes()).hexdigest()}
    signed = sign_payload(payload, private_key_bytes=Path(key).read_bytes()).to_dict()
    source = validate_source({"kind": kind, "url": manifest_url, "public_key": signed["public_key"]})
    verify_release(signed, source, slug)
    return signed


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--slug", required=True, choices=sorted(CRAFT))
    for name in ["version", "archive", "url", "manifest-url", "key", "min-host", "max-host", "output"]:
        parser.add_argument("--" + name, required=True)
    parser.add_argument("--kind", choices=["public", "company"], default="public")
    values = vars(parser.parse_args())
    output = Path(values.pop("output"))
    signed = sign_release(**values)
    output.write_text(json.dumps(signed, indent=2), encoding="utf-8")
    print("Manifest written. Publisher public key: " + signed["public_key"])
