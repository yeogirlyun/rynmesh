"""Signed first-party executable packages. Private/company sources are never read here."""
from __future__ import annotations

import hashlib
import io
import json
import re
import ssl
import stat
import urllib.request
import zipfile
from pathlib import PurePosixPath
from urllib.parse import urlsplit

import certifi

from ..crypto import SignedPayload, verify_signed_payload

MAX_PACKAGE = 20 * 1024 * 1024
MAX_CATALOG = 1024 * 1024
PERMISSIONS = ["local-code", "codex-image", "plugin-data", "node-site"]
ID = re.compile(r"[a-z][a-z0-9-]{0,63}\Z")
VERSION = re.compile(r"[0-9]+\.[0-9]+\.[0-9]+\Z")


class PluginError(Exception):
    pass


def safe_path(value):
    if not isinstance(value, str) or not value or "\\" in value or ":" in value:
        raise PluginError("plugin_package_invalid")
    p = PurePosixPath(value)
    if p.is_absolute() or any(x in {"", ".", ".."} for x in value.split("/")):
        raise PluginError("plugin_package_invalid")
    return value


def download(url, limit):
    """HTTPS only, no credentials or cookies; redirects remain on GitHub asset hosts."""
    hosts = {"raw.githubusercontent.com", "github.com", "release-assets.githubusercontent.com", "objects.githubusercontent.com"}

    def check(target):
        u = urlsplit(target)
        if u.scheme != "https" or u.hostname not in hosts or u.username or u.password or u.port not in (None, 443):
            raise PluginError("plugin_source_invalid")

    class Redirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, req, fp, code, msg, headers, newurl):
            check(newurl)
            return super().redirect_request(req, fp, code, msg, headers, newurl)

    check(url)
    opener = urllib.request.build_opener(Redirect(), urllib.request.HTTPSHandler(context=ssl.create_default_context(cafile=certifi.where())))
    try:
        with opener.open(urllib.request.Request(url, headers={"User-Agent": "Ryn-Public-Plugins/1"}), timeout=30) as r:
            data = r.read(limit + 1)
        if len(data) > limit:
            raise PluginError("plugin_package_too_large")
        return data
    except PluginError:
        raise
    except (OSError, ValueError):
        raise PluginError("plugin_download_failed") from None


def verify_catalog(raw, public_key):
    try:
        signed = SignedPayload.from_dict(raw)
        if signed.public_key != public_key:
            raise ValueError()
        verify_signed_payload(signed)
        value = signed.payload
        if value["kind"] != "ryn.public-plugin-catalog" or value["schema"] != 1:
            raise ValueError()
        rows = value["plugins"]
        if not isinstance(rows, list) or len(rows) > 100:
            raise ValueError()
        ids = set()
        for row in rows:
            slug = row["id"]
            if not ID.fullmatch(slug) or slug in ids or not VERSION.fullmatch(row["version"]):
                raise ValueError()
            ids.add(slug)
            if row["runtime"] != "first-party-python" or row["host_api"] != 1 or row["permissions"] != PERMISSIONS:
                raise ValueError()
            if row["site_id"] != slug or not row["entry"].endswith("/__init__.py"):
                raise ValueError()
            safe_path(row["entry"])
            if not re.fullmatch(r"https://github.com/yeogirlyun/rynmesh/releases/download/[A-Za-z0-9._-]+/[A-Za-z0-9._-]+\.zip", row["url"]):
                raise ValueError()
            if type(row["size"]) is not int or not 0 < row["size"] <= MAX_PACKAGE:
                raise ValueError()
            if not re.fullmatch(r"[0-9a-f]{64}", row["sha256"]):
                raise ValueError()
            files = row["files"]
            if not isinstance(files, dict) or not 1 <= len(files) <= 200 or row["entry"] not in files or "plugin.json" not in files:
                raise ValueError()
            lowered = set()
            for name, digest in files.items():
                safe_path(name)
                if name.lower() in lowered or not re.fullmatch(r"[0-9a-f]{64}", digest):
                    raise ValueError()
                lowered.add(name.lower())
            for field in ("name", "description", "notes", "source"):
                if not isinstance(row[field], str) or len(row[field]) > 4000:
                    raise ValueError()
        return rows
    except Exception:
        raise PluginError("plugin_signature_invalid") from None


def verify_tree(root, row):
    actual = set()
    for p in root.rglob("*"):
        if p.is_symlink():
            raise PluginError("plugin_integrity_failed")
        if p.is_file():
            name = p.relative_to(root).as_posix()
            # Python bytecode is never trusted or shipped.
            if name not in row["files"] or p.stat().st_size > MAX_PACKAGE:
                raise PluginError("plugin_integrity_failed")
            if hashlib.sha256(p.read_bytes()).hexdigest() != row["files"][name]:
                raise PluginError("plugin_integrity_failed")
            actual.add(name)
    if actual != set(row["files"]):
        raise PluginError("plugin_integrity_failed")


def unpack(data, root, row):
    if len(data) != row["size"] or hashlib.sha256(data).hexdigest() != row["sha256"]:
        raise PluginError("plugin_integrity_failed")
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            entries = archive.infolist()
            names = [safe_path(i.filename) for i in entries]
            if len(names) != len(set(names)) or set(names) != set(row["files"]) or sum(i.file_size for i in entries) > MAX_PACKAGE:
                raise PluginError("plugin_package_invalid")
            for entry in entries:
                mode = entry.external_attr >> 16
                if entry.is_dir() or stat.S_ISLNK(mode):
                    raise PluginError("plugin_package_invalid")
                path = root / entry.filename
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(archive.read(entry))
        verify_tree(root, row)
        manifest = json.loads((root / "plugin.json").read_text(encoding="utf-8"))
        if any(manifest.get(k) != row[k] for k in manifest):
            raise PluginError("plugin_package_invalid")
    except (OSError, ValueError, KeyError, zipfile.BadZipFile):
        raise PluginError("plugin_package_invalid") from None
