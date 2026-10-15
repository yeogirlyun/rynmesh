"""Owner-managed plugins. No downloads, listeners or updates on startup.

Packages are immutable, pinned and stored outside the application distribution.
Only this manager can turn a verified package into a loopback static origin.
Agent adapters use a lease so uninstall/disable cannot race an active invocation.
"""

from __future__ import annotations

import hashlib
import json
import os
import secrets
import shutil
import socket
import sys
import threading
import time
from contextlib import asynccontextmanager, contextmanager
from pathlib import Path

from fastapi import HTTPException, Request
from fastapi.responses import JSONResponse

from ..atomic_io import atomic_write_json
from ..file_transactions import file_transaction
from . import builtin_plugins as builtin
from . import craft_host
from .craft_catalog import CRAFT

DEFAULT_PLUGINS = ("pdfcraft", "wordcraft", "gridcraft", "deckcraft")
WARNINGS = {"photocraft": "fonts_fallback", "wordcraft": "cjk_font_coverage"}
GROUPS = {
    **dict.fromkeys(DEFAULT_PLUGINS, "office"),
    **dict.fromkeys(("cadcraft", "figcraft", "designcraft", "photocraft", "lightcraft", "vectorcraft"), "design"),
}


def installer_selection():
    """Optional installer choices; a preference, never permission to download."""
    if sys.platform == "win32":
        try:
            import winreg

            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Ryn\Craft") as key:
                value, kind = winreg.QueryValueEx(key, "SelectedPlugins")
            if kind == winreg.REG_SZ and isinstance(value, str) and len(value) <= 512:
                choices = value.split(";") if value else []
                if all((slug in CRAFT and CRAFT[slug]["web"]) or slug in builtin.BUILTINS for slug in choices):
                    return list(dict.fromkeys(choices))
        except (OSError, ImportError):
            pass
    return None


def package_root(peer_id):
    node = hashlib.sha256(peer_id.encode()).hexdigest()[:24]
    if sys.platform == "win32":
        base = Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData/Local")
    elif sys.platform == "darwin":
        base = Path.home() / "Library/Application Support"
    else:
        base = Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local/share")
    return base / "Ryn/plugins" / node


class PluginManager:
    def __init__(self, home, peer_id, *, cache=None, download=None, nas=None):
        self.peer_id = peer_id
        self.cache = Path(cache) if cache is not None else package_root(peer_id)
        self.cache = self.cache.expanduser().resolve()
        self.state_path = Path(home) / "craft-plugins.json"
        self.lock_path = Path(home) / "craft-plugins.lock"
        self.lock = threading.RLock()
        self.download = download
        self.servers = {}
        self.active = {}
        self.mutating = set()
        self.tickets = {}
        self.closed = False
        self.nas = nas
        self.migration_error = None

    @staticmethod
    def specification(slug):
        return builtin.manifest(slug) if slug in builtin.BUILTINS else craft_host.specification(slug)

    @staticmethod
    def version(slug):
        return builtin.VERSION if slug in builtin.BUILTINS else craft_host.specification(slug)["tag"]

    def verify(self, slug):
        if slug in builtin.BUILTINS:
            return builtin.verify(self.cache, slug)
        return craft_host.installed(self.cache, slug)

    def adopt_legacy(self, service):
        self.migration_error = None
        try:
            self._adopt_legacy(service)
        except (craft_host.CraftError, OSError):
            # A broken registration/cache must not prevent the original node
            # from starting. Keep legacy stores intact and report the failure.
            self.migration_error = "plugin_migration_failed"

    def _adopt_legacy(self, service):
        """Preserve explicit prior app use/grants. Fresh nodes install only by choice."""
        with self.lock:
            if self._read().get("unified_migrated"):
                return
        try:
            data = service.store.read()
            used = {receipt.get("app") for w in data["workspaces"].values() for receipt in w.get("operations", {}).values()}
            used.update(row["command"]["app"] for row in data["intents"].values() if row["provider_peer"] == self.peer_id)
            relationships = service.friends().store._read(service.friends().store.state_path, service.friends().store._empty())["relationships"]
            actors = {self.peer_id, *(row["peer_id"] for row in relationships.values())}
            for slug in builtin.BUSINESS:
                if any(service._slot(actor, slug) in data["workspaces"] or data["grants"].get(service._slot(actor, slug), {}).get("allowed") for actor in actors):
                    used.add(slug)
        except Exception:
            self.migration_error = "plugin_migration_failed"
            return
        with self.lock, file_transaction(self.lock_path):
            state = self._read()
            if state.get("unified_migrated"):
                return
            for slug in builtin.BUSINESS:
                if slug in used and slug not in state["plugins"]:
                    builtin.install(self.cache, slug)
                    state["plugins"][slug] = {"version": builtin.VERSION, "enabled": True, "generation": secrets.token_hex(16)}
            if self.nas and (self.nas.state["enabled"] or (self.nas.directory / "state.enc").is_file()):
                if "nas" not in state["plugins"]:
                    builtin.install(self.cache, "nas")
                    state["plugins"]["nas"] = {"version": builtin.VERSION, "enabled": bool(self.nas.state["enabled"]), "generation": secrets.token_hex(16)}
            state["unified_migrated"] = True
            self._write(state)

    def generation(self, slug):
        with self.lock:
            row = self._row(slug)
            if self.closed or slug in self.mutating or not row.get("enabled"):
                raise craft_host.CraftError("plugin_disabled")
            if row.get("version") != self.version(slug):
                raise craft_host.CraftError("plugin_not_installed")
            if slug in builtin.BUILTINS:
                builtin.verify(self.cache, slug)
            if slug == "nas" and (not self.nas or not self.nas.state["enabled"]):
                raise craft_host.CraftError("plugin_disabled")
            return row.get("generation", "legacy")

    def _read(self):
        if not self.state_path.exists():
            return {"schema": 1, "setup_completed": False, "plugins": {}}
        try:
            if self.state_path.stat().st_size > 65536:
                raise ValueError
            state = json.loads(self.state_path.read_text(encoding="utf-8"))
            if state["schema"] != 1 or not isinstance(state["plugins"], dict):
                raise ValueError
            return state
        except (OSError, ValueError, KeyError, TypeError):
            raise craft_host.CraftError("plugin_state_invalid") from None

    def _write(self, state):
        atomic_write_json(self.state_path, state, max_bytes=65536)

    def _row(self, slug):
        self.specification(slug)
        return self._read()["plugins"].get(slug, {})

    def catalog(self):
        with self.lock, file_transaction(self.lock_path):
            state = self._read()
            selection = installer_selection()
            items = []
            for slug, spec in CRAFT.items():
                row = state["plugins"].get(slug, {})
                present = row.get("version") == spec["tag"] and (self.cache / slug / spec["tag"] / "receipt.json").is_file()
                enabled = bool(present and row.get("enabled"))
                items.append({
                    "id": slug, "name": spec["name"], "version": spec["tag"],
                    "web": spec["web"], "group": GROUPS.get(slug, "media"),
                    "formats": spec["formats"], "sha256": spec["web_sha256"],
                    "source_url": f"https://github.com/storytold/{slug}/tree/{spec['tag']}",
                    "installed": bool(present), "enabled": enabled,
                    "running": slug in self.servers and self.servers[slug][1].is_alive(),
                    "busy": slug in self.mutating or bool(self.active.get(slug)),
                    # Actual upstream editing APIs have not been integrated. Never
                    # advertise mockup merge/generation examples as working tools.
                    "agent_tools": ["editor_info"] if enabled else [],
                    "editing_supported": False,
                    "warning": WARNINGS.get(slug),
                    "kind": "web_editor", "installable": spec["web"], "launch_path": None,
                })
            for slug in builtin.BUILTINS:
                row = state["plugins"].get(slug, {})
                present = False
                if row.get("version") == builtin.VERSION:
                    try:
                        builtin.verify(self.cache, slug)
                        present = True
                    except craft_host.CraftError:
                        pass
                available = slug != "nas" or self.nas is not None
                enabled = bool(present and available and row.get("enabled") and (slug != "nas" or self.nas.state["enabled"]))
                items.append({
                    "id": slug, "name": CRAFT.get(slug, {}).get("name", slug.title() if slug != "nas" else "NAS"),
                    "version": builtin.VERSION, "web": False, "kind": "builtin_adapter",
                    "installable": available, "group": "storage" if slug == "nas" else "workflow",
                    "formats": [], "sha256": builtin.checksum(slug),
                    "source_url": "https://github.com/yeogirlyun/rynmesh",
                    "installed": present, "enabled": enabled, "running": False,
                    "busy": slug in self.mutating or bool(self.active.get(slug)),
                    "agent_tools": builtin.manifest(slug)["operations"] if enabled else [],
                    "editing_supported": False,
                    "warning": None, "launch_path": "/nas" if slug == "nas" else f"/apps/workspaces?app={slug}",
                })
            items.sort(key=lambda item: DEFAULT_PLUGINS.index(item["id"]) if item["id"] in DEFAULT_PLUGINS else len(DEFAULT_PLUGINS))
            return {"node_id": self.peer_id, "plugins": items,
                    "setup_completed": bool(state.get("setup_completed")),
                    "defaults": list(DEFAULT_PLUGINS),
                    "selection": selection if selection is not None else list(DEFAULT_PLUGINS),
                    "setup_requested": selection is not None,
                    "storage_path": str(self.cache), "migration_error": self.migration_error}

    @contextmanager
    def _mutation(self, slug):
        self.specification(slug)
        with self.lock:
            if self.closed or slug in self.mutating or self.active.get(slug):
                raise craft_host.CraftError("plugin_busy")
            self.mutating.add(slug)
        try:
            yield
        finally:
            with self.lock:
                self.mutating.discard(slug)

    def install(self, slug):
        with self._mutation(slug):
            if slug in builtin.BUILTINS:
                if slug == "nas" and not self.nas:
                    raise craft_host.CraftError("plugin_unavailable")
                builtin.install(self.cache, slug)
            else:
                kwargs = {"download": self.download} if self.download else {}
                craft_host.install(self.cache, slug, **kwargs)
            with self.lock, file_transaction(self.lock_path):
                state = self._read()
                state["plugins"][slug] = {"version": self.version(slug), "enabled": True, "generation": secrets.token_hex(16)}
                if slug == "nas":
                    self.nas._enable(True)
                self._write(state)
        return self.catalog()

    def finish_setup(self):
        with self.lock, file_transaction(self.lock_path):
            state = self._read()
            state["setup_completed"] = True
            self._write(state)
        return self.catalog()

    def configure_nas(self, enabled):
        """The older NAS settings endpoint delegates to the same lifecycle."""
        if not isinstance(enabled, bool) or not self.nas:
            raise craft_host.CraftError("plugin_invalid_request")
        with self.lock:
            installed = "nas" in self._read()["plugins"]
        if enabled and not installed:
            return self.install("nas")
        if installed:
            return self.set_enabled("nas", enabled)
        self.nas._enable(False)
        return self.catalog()

    def _stop(self, slug):
        item = self.servers.get(slug)
        if item:
            server, thread, listener = item
            server.should_exit = True
            thread.join(timeout=5)
            if thread.is_alive():
                raise craft_host.CraftError("plugin_busy")
            listener.close()
            self.servers.pop(slug, None)
        self.tickets = {key: value for key, value in self.tickets.items() if value[0] != slug}

    def set_enabled(self, slug, enabled):
        if not isinstance(enabled, bool):
            raise craft_host.CraftError("plugin_invalid_request")
        with self._mutation(slug), self.lock, file_transaction(self.lock_path):
            state = self._read()
            if slug not in state["plugins"]:
                raise craft_host.CraftError("plugin_not_installed")
            if enabled:
                self.verify(slug)
            else:
                self._stop(slug)
            state["plugins"][slug]["enabled"] = enabled
            state["plugins"][slug]["generation"] = secrets.token_hex(16)
            if slug == "nas":
                if not self.nas:
                    raise craft_host.CraftError("plugin_unavailable")
                self.nas._enable(enabled)
            self._write(state)
        return self.catalog()

    def uninstall(self, slug, clear_preferences=False):
        with self._mutation(slug), self.lock, file_transaction(self.lock_path):
            state = self._read()
            self._stop(slug)
            if slug == "nas" and self.nas:
                self.nas._enable(False)
            # Never accept a path from the caller or receipt. Verify the resolved
            # absolute target before recursive removal, including on Windows.
            parent = self.cache / slug
            target = parent / self.version(slug)
            if parent.is_symlink() or target.is_symlink() or not target.resolve().is_relative_to(self.cache):
                raise craft_host.CraftError("plugin_unsafe_path")
            if target.exists():
                shutil.rmtree(target)
            state["plugins"].pop(slug, None)
            if clear_preferences:
                state.setdefault("preferences", {}).pop(slug, None)
            self._write(state)
        return self.catalog()

    @contextmanager
    def lease(self, slug):
        with self.lock:
            row = self._row(slug)
            if self.closed or slug in self.mutating:
                raise craft_host.CraftError("plugin_busy")
            if not row.get("enabled"):
                raise craft_host.CraftError("plugin_disabled")
            if row.get("version") != self.version(slug):
                raise craft_host.CraftError("plugin_not_installed")
            self.verify(slug)
            if slug == "nas" and (not self.nas or not self.nas.state["enabled"]):
                raise craft_host.CraftError("plugin_disabled")
            self.active[slug] = self.active.get(slug, 0) + 1
        try:
            yield self.specification(slug)
        finally:
            with self.lock:
                self.active[slug] -= 1

    @staticmethod
    def port(slug):
        craft_host.specification(slug)
        return 8920 + sorted(CRAFT).index(slug)

    def preview_launch(self, slug):
        if slug in builtin.BUILTINS:
            raise craft_host.CraftError("plugin_internal_entry")
        with self.lease(slug), self.lock:
            self.tickets = {key: value for key, value in self.tickets.items() if value[1] > time.monotonic()}
            if len(self.tickets) >= 64:
                raise craft_host.CraftError("plugin_busy")
            ticket = secrets.token_urlsafe(32)
            self.tickets[ticket] = (slug, time.monotonic() + 120)
            return {"app": slug, "version": CRAFT[slug]["tag"], "node_id": self.peer_id,
                    "url": f"http://127.0.0.1:{self.port(slug)}/", "ticket": ticket}

    def launch(self, slug, ticket):
        if slug in builtin.BUILTINS:
            raise craft_host.CraftError("plugin_internal_entry")
        with self.lease(slug), self.lock:
            approved = self.tickets.pop(ticket, None)
            if not approved or approved[0] != slug or approved[1] <= time.monotonic():
                raise craft_host.CraftError("plugin_launch_expired")
            item = self.servers.get(slug)
            if item and not item[1].is_alive():
                item[2].close()
                self.servers.pop(slug)
            if slug not in self.servers:
                import uvicorn

                listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                try:
                    if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
                        listener.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
                    listener.bind(("127.0.0.1", self.port(slug)))
                    listener.listen(128)
                except OSError:
                    listener.close()
                    raise craft_host.CraftError("plugin_port_busy") from None
                editor = craft_host.create_editor(self.cache, slug)
                server = uvicorn.Server(uvicorn.Config(editor, access_log=False, log_level="error", lifespan="off"))
                thread = threading.Thread(target=server.run, kwargs={"sockets": [listener]}, daemon=True,
                                          name=f"ryn-craft-{slug}")
                self.servers[slug] = (server, thread, listener)
                thread.start()
                deadline = time.monotonic() + 5
                while thread.is_alive() and not server.started and time.monotonic() < deadline:
                    time.sleep(.02)
                if not server.started:
                    self._stop(slug)
                    raise craft_host.CraftError("plugin_start_failed")
            return {"app": slug, "node_id": self.peer_id, "version": CRAFT[slug]["tag"],
                    "url": f"http://127.0.0.1:{self.port(slug)}/", "state": "ready"}

    def close(self):
        with self.lock:
            self.closed = True
            for slug in list(self.servers):
                self._stop(slug)


def install_plugin_routes(app, *, home, peer_id, local_control):
    import asyncio

    from .routes import body

    manager = PluginManager(home, peer_id, nas=getattr(app.state, "nas_plugin", None))
    app.state.craft_plugins = manager
    app.state.plugins = manager
    if manager.nas:
        manager.nas.plugin_manager = manager
    previous_lifespan = app.router.lifespan_context

    @asynccontextmanager
    async def lifespan(instance):
        async with previous_lifespan(instance) as state:
            try:
                yield state
            finally:
                await asyncio.to_thread(manager.close)

    app.router.lifespan_context = lifespan

    async def run(request, fn, *args):
        local_control(request)
        try:
            value = await asyncio.to_thread(fn, *args)
            return JSONResponse(value, headers={"Cache-Control": "no-store"})
        except craft_host.CraftError as exc:
            raise HTTPException(409, detail=str(exc)) from None
        except Exception:
            raise HTTPException(503, detail="plugin_operation_failed") from None

    @app.get("/api/local/plugins")
    @app.get("/api/local/craft-plugins")
    async def catalog(request: Request):
        return await run(request, manager.catalog)

    @app.post("/api/local/plugins/setup")
    @app.post("/api/local/craft-plugins/setup")
    async def setup(request: Request):
        local_control(request)
        if await body(request) != {}:
            raise HTTPException(400, detail="plugin_invalid_request")
        return await run(request, manager.finish_setup)

    @app.post("/api/local/plugins/{slug}/{action}")
    @app.post("/api/local/craft-plugins/{slug}/{action}")
    async def change(request: Request, slug: str, action: str):
        local_control(request)
        value = await body(request, 2048)
        methods = {
            "install": (manager.install, set(), []),
            "enable": (manager.set_enabled, {"enabled"}, [value.get("enabled")]),
            "uninstall": (manager.uninstall, {"clear_preferences"}, [value.get("clear_preferences")]),
            "preview-launch": (manager.preview_launch, set(), []),
            "launch": (manager.launch, {"ticket"}, [value.get("ticket")]),
        }
        if action not in methods or set(value) != methods[action][1]:
            raise HTTPException(400, detail="plugin_invalid_request")
        if action == "uninstall" and not isinstance(value.get("clear_preferences"), bool):
            raise HTTPException(400, detail="plugin_invalid_request")
        if action == "launch" and (not isinstance(value.get("ticket"), str) or len(value["ticket"]) > 128):
            raise HTTPException(400, detail="plugin_invalid_request")
        fn, _, args = methods[action]
        return await run(request, fn, slug, *args)

    return manager
