"""Owner-installed first-party plugins: independent packages, activation, rollback and local pages."""
from __future__ import annotations

import importlib.abc
import importlib.util
import json
import secrets
import sys
import threading
import time
from pathlib import Path

from ..atomic_io import atomic_write_json
from ..node_sites.protocol import SiteError, identity_headers
from .packages import (
    MAX_CATALOG,
    MAX_PACKAGE,
    PluginError,
    download,
    unpack,
    verify_catalog,
    verify_tree,
)


class SourceLoader(importlib.abc.SourceLoader):
    """Load verified sources without accepting or writing bytecode in immutable packages."""
    def __init__(self, path):
        self.path = path

    def get_filename(self, fullname):
        return str(self.path)

    def get_data(self, path):
        return Path(path).read_bytes()

    def get_code(self, fullname):
        return self.source_to_code(self.path.read_bytes(), str(self.path))


class PackageFinder(importlib.abc.MetaPathFinder):
    def __init__(self, namespace, root):
        self.namespace, self.root = namespace, root

    def find_spec(self, fullname, path=None, target=None):
        if not fullname.startswith(self.namespace + "."):
            return None
        relative = fullname[len(self.namespace) + 1:].replace(".", "/")
        source = self.root / (relative + ".py")
        if source.is_file():
            return importlib.util.spec_from_loader(fullname, SourceLoader(source))
        source = self.root / relative / "__init__.py"
        if source.is_file():
            return importlib.util.spec_from_file_location(fullname, source, loader=SourceLoader(source), submodule_search_locations=[str(source.parent)])
        return None


class PublicPlugins:
    def __init__(self, home, sites, *, source=None, fetch=download):
        self.home, self.sites, self.fetch = Path(home), sites, fetch
        self.root = self.home / "public-plugins"
        self.source = source or json.loads(Path(__file__).with_name("source.json").read_text(encoding="utf-8"))
        self.lock = threading.RLock()
        self.operation = threading.Lock()
        self.running = {}
        self.errors = {}
        self.catalog_error = ""
        self.checked_at = None
        self.raw_catalog = None
        self.catalog = []
        self.state = {"schema": 1, "plugins": {}}
        if (self.root / "state.json").exists():
            try:
                self.state = json.loads((self.root / "state.json").read_text(encoding="utf-8"))
                assert self.state["schema"] == 1 and isinstance(self.state["plugins"], dict)
            except Exception:
                raise PluginError("plugin_state_invalid") from None
        if (self.root / "catalog.json").exists():
            try:
                self.raw_catalog = json.loads((self.root / "catalog.json").read_text(encoding="utf-8"))
                self.catalog = verify_catalog(self.raw_catalog, self.source["public_key"])
            except Exception:
                self.catalog_error = "plugin_signature_invalid"
        # The old bundled studio is now optional. Preserve all data and audience,
        # but require an explicit share action after the first independent install.
        if not self.state.get("migrated"):
            if sites.store.site("image-studio"):
                sites.store.update("image-studio", {"enabled": False})
            self.state["migrated"] = True
            self.save()
        sites.client.local_fetch = self.local_fetch
        for slug, record in self.state["plugins"].items():
            if record.get("enabled"):
                try:
                    self.activate(slug, record)
                except Exception as exc:
                    self.errors[slug] = str(exc) if isinstance(exc, PluginError) else "plugin_start_failed"

    def save(self):
        self.root.mkdir(parents=True, exist_ok=True)
        atomic_write_json(self.root / "state.json", self.state)

    def row(self, slug, record):
        rows = verify_catalog(record["catalog"], self.source["public_key"])
        match = next((r for r in rows if r["id"] == slug), None)
        if match is None:
            raise PluginError("plugin_signature_invalid")
        return match

    def package_path(self, row):
        return self.root / "packages" / row["id"] / row["sha256"]

    def activate(self, slug, record):
        row = self.row(slug, record)
        root = self.package_path(row)
        verify_tree(root, row)
        entry = root / row["entry"]
        namespace = "_ryn_public_" + slug.replace("-", "_") + "_" + secrets.token_hex(6)
        finder = PackageFinder(namespace, entry.parent)
        spec = importlib.util.spec_from_file_location(namespace, entry, loader=SourceLoader(entry), submodule_search_locations=[str(entry.parent)])
        module = importlib.util.module_from_spec(spec)
        sys.meta_path.insert(0, finder)
        sys.modules[namespace] = module
        runtime = None
        try:
            spec.loader.exec_module(module)
            runtime = module.create(home=self.home, sites=self.sites)
            runtime.start()
            summary = getattr(runtime, "summary", None)
            if callable(summary):
                self.sites.host.summaries[row["site_id"]] = summary
            self.running[slug] = (runtime, finder, namespace)
            self.sites.host.builtins[row["site_id"]] = lambda method, path, headers, body: self.site_request(slug, method, path, headers, body)
            self.errors.pop(slug, None)
        except Exception:
            self.sites.host.builtins.pop(row["site_id"], None)
            if runtime:
                runtime.close()
            self.forget(finder, namespace)
            raise PluginError("plugin_start_failed") from None

    @staticmethod
    def forget(finder, namespace):
        if finder in sys.meta_path:
            sys.meta_path.remove(finder)
        for key in list(sys.modules):
            if key == namespace or key.startswith(namespace + "."):
                sys.modules.pop(key, None)

    def deactivate(self, slug, *, force=False):
        active = self.running.get(slug)
        if not active:
            return
        runtime, finder, namespace = active
        if not force and runtime.busy():
            raise PluginError("plugin_busy")
        self.sites.host.summaries.pop(slug, None)
        self.sites.host.builtins.pop(slug, None)
        self.running.pop(slug, None)
        runtime.close()
        self.forget(finder, namespace)
        self.sites.host.revoke_cache()

    def overview(self):
        with self.lock:
            rows = {r["id"]: r for r in self.catalog}
            for slug, record in self.state["plugins"].items():
                try:
                    rows.setdefault(slug, self.row(slug, record))
                except PluginError:
                    self.errors[slug] = "plugin_signature_invalid"
            items = []
            for slug, row in rows.items():
                record = self.state["plugins"].get(slug)
                installed = self.row(slug, record) if record else None
                newer = installed is not None and tuple(map(int, row["version"].split("."))) > tuple(map(int, installed["version"].split(".")))
                items.append({k: row[k] for k in ("id", "name", "description", "version", "notes", "source", "permissions")} | {
                    "installed_version": installed["version"] if installed else None,
                    "enabled": bool(record and record.get("enabled")), "running": slug in self.running,
                    "update_available": newer, "rollback_available": bool(record and record.get("previous")),
                    "error": self.errors.get(slug, ""),
                })
            return {"plugins": items, "checked_at": self.checked_at, "error": self.catalog_error,
                    "source": self.source["url"], "legacy_data": (self.home / "image-studio/studio.db").exists()}

    def refresh(self):
        try:
            raw = json.loads(self.fetch(self.source["url"], MAX_CATALOG))
            rows = verify_catalog(raw, self.source["public_key"])
            with self.lock:
                atomic_write_json(self.root / "catalog.json", raw)
                self.raw_catalog, self.catalog = raw, rows
                self.checked_at, self.catalog_error = time.time(), ""
        except Exception as exc:
            with self.lock:
                self.catalog_error = str(exc) if isinstance(exc, PluginError) else "plugin_download_failed"
            raise PluginError(self.catalog_error) from None
        return self.overview()

    def install(self, slug):
        if not self.operation.acquire(blocking=False):
            raise PluginError("plugin_busy")
        try:
            with self.lock:
                row = next((r for r in self.catalog if r["id"] == slug), None)
                if row is None:
                    raise PluginError("plugin_not_found")
                candidate = {"catalog": self.raw_catalog, "enabled": True}
                old = self.state["plugins"].get(slug)
                if old:
                    current = self.row(slug, old)
                    if tuple(map(int, row["version"].split("."))) <= tuple(map(int, current["version"].split("."))):
                        raise PluginError("plugin_no_update")
                    candidate["enabled"] = old["enabled"]
                    candidate["previous"] = {k: v for k, v in old.items() if k != "previous"}
            data = self.fetch(row["url"], MAX_PACKAGE)
            staging = self.root / "staging" / secrets.token_hex(12)
            unpack(data, staging, row)
            destination = self.package_path(row)
            destination.parent.mkdir(parents=True, exist_ok=True)
            if not destination.exists():
                staging.rename(destination)
            else:
                verify_tree(destination, row)
            with self.lock:
                self.switch(slug, candidate)
            return self.overview()
        finally:
            self.operation.release()

    def switch(self, slug, candidate):
        old = self.state["plugins"].get(slug)
        self.deactivate(slug)
        try:
            if candidate.get("enabled"):
                self.activate(slug, candidate)
            self.state["plugins"][slug] = candidate
            self.save()
        except Exception:
            self.deactivate(slug, force=True)
            if old:
                self.state["plugins"][slug] = old
                if old.get("enabled"):
                    self.activate(slug, old)
            else:
                self.state["plugins"].pop(slug, None)
            raise

    def action(self, slug, action):
        if not self.operation.acquire(blocking=False):
            raise PluginError("plugin_busy")
        try:
            with self.lock:
                old = self.state["plugins"].get(slug)
                if not old:
                    raise PluginError("plugin_not_installed")
                if action == "rollback":
                    if not old.get("previous"):
                        raise PluginError("plugin_no_previous")
                    self.switch(slug, {**old["previous"], "enabled": old["enabled"]})
                elif action in {"enable", "disable"}:
                    self.switch(slug, {**old, "enabled": action == "enable"})
                elif action == "uninstall":
                    self.deactivate(slug)
                    if self.sites.store.site(slug):
                        self.sites.store.update(slug, {"enabled": False})
                    del self.state["plugins"][slug]
                    self.save()
                else:
                    raise PluginError("plugin_action_invalid")
            return self.overview()
        finally:
            self.operation.release()

    def site_request(self, slug, method, path, headers, body):
        with self.lock:
            if slug not in self.running:
                raise SiteError("site_denied", 403)
            return self.running[slug][0].site(method, path, headers, body)

    def local_fetch(self, sid, method, path, body):
        headers = identity_headers(token="", caller=self.sites.engine.me, member={"member_id": "owner", "name": "我"}, resource="local")
        return self.site_request(sid, method, path, headers, body)

    def open(self, slug):
        with self.lock:
            if slug not in self.running:
                raise PluginError("plugin_not_enabled")
            key = self.sites.store.key_for("local", self.sites.engine.me, slug)
            return {"url": self.sites.gateway.url(key), "name": self.row(slug, self.state["plugins"][slug])["name"]}

    def owner(self, slug, method, path, body):
        with self.lock:
            if slug not in self.running:
                raise PluginError("plugin_not_enabled")
            return self.running[slug][0].owner(method, path, body)

    def close(self):
        with self.lock:
            for slug in list(self.running):
                self.deactivate(slug, force=True)
