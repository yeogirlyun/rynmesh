"""Explicit offline compatibility builds, independently pinned beside unchanged releases.

An archive and its canonical file manifest must both match pins in this module.
Receipts record installation results; their contents never establish trust.
"""

import hashlib
import json
import os
import shutil
import tempfile
import zipfile
from pathlib import Path, PurePosixPath

from ..file_transactions import file_transaction
from . import craft_host as host

COMPAT = {
    "gridcraft": {
        "id": "ryn-cjk-1",
        "base_version": "v0.6.1",
        "base_web_sha256": "86048ba175689ff852c141d6115e5491f0856f891300c3e1f91274b9c2709a42",
        "archive_sha256": "beebcc8c9467845f4c2e404d64cf33c2cf2e08cec3d59f5bfdb43d36baced3c0",
        "files_manifest_sha256": "8a82fd16ada4265ba906dfdb17f5dee78de5a3751d0492127093eff5b0ce783c",
    },
    "wordcraft": {
        "id": "ryn-cjk-1",
        "base_version": "v0.6.1",
        "base_web_sha256": "85e458cb5adaa75afa871a9858c735ef7fd1e05741c6bf5d444dc09a1eb29e0a",
        "archive_sha256": "53364159ed1d5d1af984a849ed9ab339fe7657e1fa6941c027aa786fe654dc47",
        "files_manifest_sha256": "84125c56ecebdff5cee7b1c88d00f09c4aa4dae9f3aedfe599bbdbe245605816",
    },
    "pdfcraft": {
        "id": "ryn-cjk-1",
        "base_version": "v0.7.1",
        "base_web_sha256": "2e58067125b2c14d92d50888a6a03351302065ff4b99c0914892a078f05dfd66",
        "archive_sha256": "7acf50249aea23569c9dfeba1d765ff193477b3cdb0543d9b0a1ae46647e2cd3",
        "files_manifest_sha256": "2798d7945f8114e942b04124d302c2bb967fbf69845640952fad8d64fc0ab1e0",
    }
}


def manifest_digest(manifest):
    """Stable build-time pin for {entry, files}, including notices and provenance."""
    raw = json.dumps(manifest, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _linked(path):
    return path.is_symlink() or getattr(path, "is_junction", lambda: False)()


def _location(cache, slug, spec):
    base = host._location(cache, slug)
    identifier = spec["id"]
    if (
        not isinstance(identifier, str)
        or identifier.startswith(".")
        or any(c not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._-" for c in identifier)
        or not identifier
    ):
        raise host.CraftError("craft_compat_not_verified")
    parent = base / "compatibility"
    root = parent / identifier
    if (
        any(_linked(path) for path in (base.parent, base, parent, root))
        or not root.resolve().is_relative_to(base.resolve())
    ):
        raise host.CraftError("craft_compat_not_verified")
    return root


def _check_spec(slug, spec):
    upstream = host.specification(slug)
    if (spec["base_version"], spec["base_web_sha256"]) != (
        upstream["tag"], upstream["web_sha256"]
    ):
        raise host.CraftError("craft_compat_base_mismatch")
    for key in ("archive_sha256", "files_manifest_sha256"):
        value = spec[key]
        if not isinstance(value, str) or len(value) != 64 or any(c not in "0123456789abcdef" for c in value):
            raise host.CraftError("craft_compat_not_pinned")


def _verified(root, slug, spec):
    try:
        receipt_path = root / "receipt.json"
        payload = root / "payload"
        if _linked(receipt_path) or _linked(payload) or receipt_path.stat().st_size > 2 * 1024 * 1024:
            raise ValueError
        receipt = json.loads(receipt_path.read_bytes())
        if any(receipt[key] != value for key, value in {
            "app": slug, "version": spec["base_version"], "compatibility": spec["id"],
            "archive_sha256": spec["archive_sha256"],
        }.items()):
            raise ValueError
        manifest = {"entry": receipt["entry"], "files": receipt["files"]}
        if manifest_digest(manifest) != spec["files_manifest_sha256"]:
            raise ValueError
        files = manifest["files"]
        if not isinstance(files, dict) or not files or len(files) > host.MAX_FILES:
            raise ValueError
        paths = list(payload.rglob("*"))
        if any(_linked(path) for path in paths):
            raise ValueError
        if {p.relative_to(payload).as_posix() for p in paths if p.is_file()} != set(files):
            raise ValueError
        for name, checksum in files.items():
            target = (payload / name).resolve()
            if not target.is_relative_to(payload.resolve()) or host.digest(target) != checksum:
                raise ValueError
        entry = PurePosixPath(manifest["entry"])
        web = (payload / entry).resolve()
        if entry.is_absolute() or ".." in entry.parts or not web.is_relative_to(payload.resolve()) or not (web / "index.html").is_file():
            raise ValueError
        return web
    except (OSError, ValueError, KeyError, TypeError):
        raise host.CraftError("craft_compat_not_verified") from None


def installed(cache, slug):
    """No compatibility package means upstream behavior; a broken one fails closed."""
    spec = COMPAT.get(slug)
    if spec is None:
        return None
    root = _location(cache, slug, spec)
    if not root.exists():
        return None
    _check_spec(slug, spec)
    return _verified(root, slug, spec)


def install(cache, slug, archive_path):
    """Install only the source-pinned local archive; never fetch or modify upstream."""
    spec = COMPAT.get(slug)
    if spec is None:
        raise host.CraftError("craft_compat_unsupported")
    _check_spec(slug, spec)
    root = _location(cache, slug, spec)
    with file_transaction(host._location(cache, slug).parent / ".install.lock"):
        host._upstream_installed(cache, slug)
        if root.exists():
            web = _verified(root, slug, spec)
        else:
            root.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            stage = Path(tempfile.mkdtemp(prefix=".install-", dir=root.parent))
            os.chmod(stage, 0o700)
            try:
                archive = stage / "compat.zip"
                with Path(archive_path).open("rb") as source, archive.open("xb") as out:
                    size = 0
                    while chunk := source.read(256 * 1024):
                        size += len(chunk)
                        if size > host.MAX_ARCHIVE:
                            raise host.CraftError("craft_archive_limit")
                        out.write(chunk)
                if host.digest(archive) != spec["archive_sha256"]:
                    raise host.CraftError("craft_compat_checksum_mismatch")
                payload = stage / "payload"
                web = host.extract(archive, payload)
                manifest = {
                    "entry": web.relative_to(payload).as_posix(),
                    "files": {p.relative_to(payload).as_posix(): host.digest(p) for p in payload.rglob("*") if p.is_file()},
                }
                if manifest_digest(manifest) != spec["files_manifest_sha256"]:
                    raise host.CraftError("craft_compat_not_verified")
                receipt = {
                    "app": slug, "version": spec["base_version"], "compatibility": spec["id"],
                    "archive_sha256": spec["archive_sha256"], **manifest,
                }
                (stage / "receipt.json").write_text(json.dumps(receipt, indent=2), encoding="utf-8")
                archive.unlink()
                _verified(stage, slug, spec)
                stage.rename(root)
                web = _verified(root, slug, spec)
            except zipfile.BadZipFile:
                raise host.CraftError("craft_compat_not_verified") from None
            finally:
                if stage.exists():
                    if _linked(stage) or not stage.resolve().is_relative_to(root.parent.resolve()):
                        raise host.CraftError("craft_compat_not_verified")
                    shutil.rmtree(stage)
    return {"app": slug, "version": spec["base_version"], "compatibility": spec["id"], "state": "ready", "web_dir": str(web)}
