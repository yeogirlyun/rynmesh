"""Trusted, separately installed node-site plugins. No application-specific imports.

Only packages explicitly activated in <home>/site-plugins/enabled.json load.
Plugins execute with the node's privileges; installation is a local owner action.
"""
from __future__ import annotations

import importlib
import importlib.metadata
import logging
import sys
from pathlib import Path
from typing import Any

from ..atomic_io import read_json
from .protocol import site_id

log = logging.getLogger(__name__)
ENTRY_GROUP = "rynmesh.node_sites"


def install_site_plugins(app: Any, *, home: Path, sites: Any, local_control: Any) -> dict[str, Any]:
    root = (Path(home) / "site-plugins").resolve()
    config = read_json(root / "enabled.json", default={"plugins": {}})
    configured = config.get("plugins", {}) if isinstance(config, dict) else {}
    results: dict[str, Any] = {}
    app.state.site_plugins = results
    if not isinstance(configured, dict):
        return results
    for name, directory in sorted(configured.items()):
        previous_routes = list(app.router.routes)
        previous_shutdown = list(app.router.on_shutdown)
        previous_handlers = dict(sites.host.builtins)
        try:
            site_id(name)
            if not isinstance(directory, str):
                raise ValueError("invalid plugin directory")
            path = (root / directory).resolve()
            if path == root or not path.is_relative_to(root) or not path.is_dir():
                raise ValueError("plugin directory must be inside site-plugins")
            entries = [(dist, ep) for dist in importlib.metadata.distributions(path=[str(path)])
                       for ep in dist.entry_points if ep.group == ENTRY_GROUP and ep.name == name]
            if len(entries) != 1:
                raise ValueError("expected one matching node-site entry point")
            dist, entry = entries[0]
            # Append: a plugin must not shadow the host's libraries.
            if str(path) not in sys.path:
                sys.path.append(str(path))
            importlib.invalidate_caches()
            plugin = entry.load()(app, home=home, sites=sites, local_control=local_control)
            owner_path = getattr(plugin, "owner_path", None)
            if owner_path:
                prefix = f"/api/local/site-plugins/{name}/"
                if not isinstance(owner_path, str) or not owner_path.startswith(prefix) or ".." in owner_path:
                    raise ValueError("invalid owner page path")
                sites.owner_paths[name] = owner_path
            results[name] = {"status": "ready", "version": dist.version}
        except Exception:
            # A broken plugin must not prevent Ryn or other sites from starting.
            for close in app.router.on_shutdown[len(previous_shutdown):]:
                try:
                    close()
                except Exception:
                    pass
            app.router.routes[:] = previous_routes
            app.router.on_shutdown[:] = previous_shutdown
            sites.host.builtins.clear()
            sites.host.builtins.update(previous_handlers)
            sites.owner_paths.pop(name, None)
            results[name] = {"status": "failed", "code": "site_plugin_load_failed"}
            log.exception("Node-site plugin %s failed to load", name)
    return results
