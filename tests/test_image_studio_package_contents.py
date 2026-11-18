"""The independent image studio package contains the complete runnable plugin."""
import hashlib
import io
import json
import zipfile
from pathlib import Path

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from rynmesh.public_plugins.packages import verify_catalog
from scripts.build_public_plugin import build


def test_signed_package_contains_image_studio_runtime_and_web_assets():
    root = Path(__file__).resolve().parents[1] / "plugins/image-studio"
    key = Ed25519PrivateKey.generate()
    name, raw, catalog = build(root, revision="a" * 40, key=key.private_bytes_raw())
    row, = verify_catalog(catalog, catalog["public_key"])
    expected = {"plugin.json", "README.md"}
    expected.update(p.relative_to(root).as_posix() for p in (root / "ryn_image_studio").glob("*.py"))
    expected.update(p.relative_to(root).as_posix() for p in (root / "ryn_image_studio/web").iterdir() if p.is_file())

    with zipfile.ZipFile(io.BytesIO(raw)) as archive:
        assert set(archive.namelist()) == set(row["files"]) == expected
        manifest = json.loads(archive.read("plugin.json"))
        assert name == f"image-studio-{manifest['version']}.zip"
        assert row["version"] == manifest["version"] and row["entry"] in expected
        for path in expected:
            assert hashlib.sha256(archive.read(path)).hexdigest() == row["files"][path]
