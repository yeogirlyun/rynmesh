"""Compatibility builds, independently pinned beside unchanged releases.

An archive and its canonical file manifest must both match pins in this module,
whether it comes from a local file or from Ryn's own release download.
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
    "deckcraft": {
        "id": "ryn-cjk-1",
        "base_version": "v0.6.1",
        "base_web_sha256": "dd76659f643fa98ff79c86bf439bcaeeaaf6bd3a4037a1dc6628c97366862ba1",
        "archive_sha256": "b084a901521fff06acbea617d0eff0ab3fd390c3a0baae163f2f400a50e60db6",
        "files_manifest_sha256": "ea1b5c9bc8d4d4b40fdaee21cd00a3ab54bf560f584580b2f8b5781b2ee3d302",
        "release_sha256": "01ef3bf81754c4d3049fc8f3ca912c71d7c8f22cef755ffee7b0387eb4818505",
    },
    "gridcraft": {
        "id": "ryn-cjk-blank-2",
        "base_version": "v0.6.1",
        "base_web_sha256": "86048ba175689ff852c141d6115e5491f0856f891300c3e1f91274b9c2709a42",
        "archive_sha256": "8db12ff172d14c18c784f965da4aa86eae6388ece2d559eba1cf9d15878e36ef",
        "files_manifest_sha256": "e15340cd0046a6852652a02170b561733f33ca9a6ede3a22d6ebfa204601738c",
        "release_sha256": "460cf42ca6ada606582b1af92ec014bc894481078bf021e1dc13862945682351",
    },
    "wordcraft": {
        "id": "ryn-cjk-1",
        "base_version": "v0.6.1",
        "base_web_sha256": "85e458cb5adaa75afa871a9858c735ef7fd1e05741c6bf5d444dc09a1eb29e0a",
        "archive_sha256": "53364159ed1d5d1af984a849ed9ab339fe7657e1fa6941c027aa786fe654dc47",
        "files_manifest_sha256": "84125c56ecebdff5cee7b1c88d00f09c4aa4dae9f3aedfe599bbdbe245605816",
        "release_sha256": "190f7dbb3d86c74cbe4f37dd80c11191e9754e3d3da88869a48b7e401c95b13f",
    },
    "pdfcraft": {
        "id": "ryn-cjk-1",
        "base_version": "v0.7.1",
        "base_web_sha256": "2e58067125b2c14d92d50888a6a03351302065ff4b99c0914892a078f05dfd66",
        "archive_sha256": "7acf50249aea23569c9dfeba1d765ff193477b3cdb0543d9b0a1ae46647e2cd3",
        "files_manifest_sha256": "2798d7945f8114e942b04124d302c2bb967fbf69845640952fad8d64fc0ab1e0",
        "release_sha256": "bf7267ff689a884aa4831f2a863ee6bf0c615a4ff6b669e6b1cff8f00a3f21a5",
    }
}


# A core-only update must not discard a verified existing Chinese font fallback.
# Full installers explicitly require the current compatibility build instead.
PREVIOUS_COMPAT = {
    "gridcraft": {
        "id": "ryn-cjk-1",
        "base_version": "v0.6.1",
        "base_web_sha256": "86048ba175689ff852c141d6115e5491f0856f891300c3e1f91274b9c2709a42",
        "archive_sha256": "beebcc8c9467845f4c2e404d64cf33c2cf2e08cec3d59f5bfdb43d36baced3c0",
        "files_manifest_sha256": "8a82fd16ada4265ba906dfdb17f5dee78de5a3751d0492127093eff5b0ce783c"
    }
}


# Upstream web builds embed no Chinese font, so Ryn publishes these builds as assets of
# its own release. The assets were repacked from verified installs (the original build
# archives were not kept), so each has its own release_sha256 pin; both archives extract
# to exactly the pinned file manifest. The pins, not this host, establish trust.
RELEASE_URL = "https://github.com/yeogirlyun/rynmesh/releases/download/office-cjk-1/"


def asset_name(slug, spec=None):
    spec = spec or COMPAT[slug]
    return f"{slug}-web-{spec['base_version'].removeprefix('v')}-{spec['id']}.zip"


def applies(slug, upstream=None):
    """True when the pinned build was made for the upstream release being installed."""
    spec = COMPAT.get(slug)
    upstream = upstream or host.specification(slug)
    return spec is not None and (spec["base_version"], spec["base_web_sha256"]) == (
        upstream.get("tag"), upstream.get("web_sha256"))


def download_install(cache, slug, *, download=host.download_to):
    """Fetch the pinned archive from Ryn's release and install it exactly like a local one."""
    spec = COMPAT.get(slug)
    if spec is None:
        raise host.CraftError("craft_compat_unsupported")
    _check_spec(slug, spec)
    host._upstream_installed(cache, slug)
    if _location(cache, slug, spec).exists():
        web = installed(cache, slug, current_only=True)
        return {"app": slug, "version": spec["base_version"], "compatibility": spec["id"], "state": "ready", "web_dir": str(web)}
    stage =Path(tempfile.mkdtemp(prefix=".download-compat-", dir=host._location(cache, slug).parent))
    try:
        archive = stage / asset_name(slug, spec)
        host.report_progress(phase="downloading", downloaded_bytes=0, total_bytes=None, percent=0)
        download(RELEASE_URL + asset_name(slug, spec), archive)
        return install(cache, slug, archive)
    finally:
        shutil.rmtree(stage, ignore_errors=True)


def _archive_pins(spec):
    """The original build archive and, when published, Ryn's release asset."""
    return {value for value in (spec["archive_sha256"], spec.get("release_sha256")) if value}


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
    for key in ("archive_sha256", "files_manifest_sha256", "release_sha256"):
        if key == "release_sha256" and key not in spec:
            continue
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
        }.items()) or receipt["archive_sha256"] not in _archive_pins(spec):
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


def installed(cache, slug, *, current_only=False):
    """No compatibility package means upstream behavior; a broken one fails closed."""
    spec = COMPAT.get(slug)
    if spec is None:
        return None
    root = _location(cache, slug, spec)
    if not root.exists():
        previous = PREVIOUS_COMPAT.get(slug)
        if (not current_only and previous and previous["id"] != spec["id"]
                and previous["base_web_sha256"] == spec["base_web_sha256"]):
            previous_root = _location(cache, slug, previous)
            if previous_root.exists():
                _check_spec(slug, previous)
                return _verified(previous_root, slug, previous)
        return None
    _check_spec(slug, spec)
    return _verified(root, slug, spec)


def install(cache, slug, archive_path):
    """Install only the source-pinned archive; never modify upstream."""
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
                archive_sha256 = host.digest(archive)
                if archive_sha256 not in _archive_pins(spec):
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
                    "archive_sha256": archive_sha256, **manifest,
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
