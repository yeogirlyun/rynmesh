"""Reviewed built-in adapters, enabled by a tiny local plugin registration.

Implementations are trusted Ryn code. No code, entry point or command is loaded
from a user-writable plugin manifest. Removing a registration keeps app data.
"""

import hashlib
import json
import os
import shutil
import tempfile
from pathlib import Path

from .catalog_data import CATALOG
from .craft_host import CraftError

BUSINESS = ("resume", "recruit", "desk")
BUILTINS = (*BUSINESS, "nas")
ALL_BUILTINS = BUILTINS
VERSION = "1.0.0"
NAS_TOOLS = ("ryn_list_resource_roots", "ryn_list_resources", "ryn_read_resource", "ryn_search_resource",
             "ryn_create_text_file", "ryn_create_directory")


def normalize_slug(slug):
    return slug


def manifest(slug):
    slug = normalize_slug(slug)
    if slug not in ALL_BUILTINS:
        raise CraftError("craft_unknown_app")
    item = CATALOG.get(slug)
    return {
        "schema": 1, "id": slug, "version": VERSION, "runtime": "trusted_builtin_adapter",
        "operations": list(item["operations"]) if item else list(NAS_TOOLS),
        "contract_sha256": hashlib.sha256(encoded(item["operations"])).hexdigest() if item else None,
        "source": item["source"] if item else {"repository": "Rynmesh", "module": "rynmesh.nas"},
        "data": "retained_in_original_encrypted_store",
    }


def encoded(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode()


def checksum(slug):
    return hashlib.sha256(encoded(manifest(slug))).hexdigest()


def location(cache, slug):
    slug = normalize_slug(slug)
    if slug not in ALL_BUILTINS:
        raise CraftError("craft_unknown_app")
    return Path(cache) / slug / VERSION


def verify(cache, slug):
    slug = normalize_slug(slug)
    root = location(cache, slug)
    try:
        if root.is_symlink() or root.parent.is_symlink():
            raise ValueError
        files = {p.name for p in root.iterdir()}
        if files != {"manifest.json", "receipt.json"}:
            raise ValueError
        for path in root.iterdir():
            if path.is_symlink() or not path.is_file() or path.stat().st_size > 65536:
                raise ValueError
        raw = (root / "manifest.json").read_bytes()
        receipt = json.loads((root / "receipt.json").read_bytes())
        if raw != encoded(manifest(slug)) or receipt != {"id": slug, "version": VERSION, "sha256": checksum(slug)}:
            raise ValueError
        return root
    except (OSError, ValueError, TypeError):
        raise CraftError("craft_build_not_verified") from None


def install(cache, slug):
    slug = normalize_slug(slug)
    root = location(cache, slug)
    if root.exists():
        return verify(cache, slug)
    root.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if root.parent.is_symlink() or not root.resolve().is_relative_to(Path(cache).resolve()):
        raise CraftError("plugin_unsafe_path")
    stage = Path(tempfile.mkdtemp(prefix=".install-", dir=root.parent))
    os.chmod(stage, 0o700)
    try:
        (stage / "manifest.json").write_bytes(encoded(manifest(slug)))
        (stage / "receipt.json").write_bytes(encoded({"id": slug, "version": VERSION, "sha256": checksum(slug)}))
        stage.rename(root)
    finally:
        if stage.exists():
            shutil.rmtree(stage)
    return verify(cache, slug)
