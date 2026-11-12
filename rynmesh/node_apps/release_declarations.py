"""Bring a signed release's plugin declarations into the extension store.

Shared by the offline installer and the background service updater, so a
release switched in by either path ends in the same state:

* publisher metadata is merged three-way (``migrate_release_defaults``), never
  overriding what the owner changed;
* skills whose files ship with the release (``<skills_root>/<skill id>/``) keep
  their package in step with the release, as long as the owner has not changed
  the skill or its files;
* declared OAuth client ids migrate cleanly without touching customized auth;
* declared local endpoints point at the service's port;
* the release's own remote connections keep their read-only declarations
  trusted, as they were when the release was installed.
"""
from __future__ import annotations

import shutil
import uuid
from pathlib import Path


def configure_local_mcp_services(store, declarations, service_id, port):
    """Point declared stock endpoints of a local runtime at its port; never override
    customized URLs or auth. Returns the managed server ids."""
    managed = []
    for declaration in declarations.get("mcp_servers", []):
        local = declaration.get("local_service") or {}
        path = local.get("path", "")
        if local.get("id") != service_id or not path.startswith("/") or any(c in path for c in "?#\\"):
            continue
        endpoint = f"http://127.0.0.1:{port}{path}"
        try:
            server = store.get_mcp_server_internal(declaration["id"], require_enabled=False)
        except TypeError:
            try:
                server = store.get_mcp_server_internal(declaration["id"])
            except Exception:
                continue
        except Exception:
            continue
        if server.get("plugin_id") != declaration.get("plugin_id"):
            continue
        current = server.get("url")
        # Any static bearer/auth customization is retained for owner review.
        headers = server.get("secret_headers") or {}
        if any(k.lower() == "authorization" for k in headers) or server.get("bearer_token_env"):
            continue
        stock = set(local.get("legacy_urls", [])) | {declaration.get("url")}
        if current != endpoint:
            if current not in stock:
                continue
            store.upsert_mcp_server({"url": endpoint, "auth": declaration.get("auth") or {}},
                                    server_id=server["id"], expected_revision=server["revision"])
        managed.append(server["id"])
    return managed


def migrate_declared_oauth_clients(store, declarations, plugin_ids):
    """Fill in a declared OAuth client_id when the stored server matches the stock
    remote connection except for ``client_id: None``."""
    from ..agent_runtime.extensions.mcp_oauth import has_static_authorization
    allowed_plugins = set(plugin_ids)
    desired = {
        s["id"]: s
        for s in declarations.get("mcp_servers", [])
        if s.get("plugin_id") in allowed_plugins and not s.get("local_service")
    }
    if not desired or not hasattr(store, "list_mcp_servers_internal"):
        return []
    migrated = []
    for server in store.list_mcp_servers_internal(include_tools=False):
        sid = server["id"]
        declared = desired.get(sid)
        if not declared or server.get("plugin_id") != declared.get("plugin_id"):
            continue
        auth = declared.get("auth") or {}
        if (auth.get("client_id") and declared.get("url") and declared["url"] == server.get("url")
                and server.get("auth") == {**auth, "client_id": None}
                and not has_static_authorization(server)):
            store.upsert_mcp_server({"auth": auth}, server_id=sid, expected_revision=server["revision"])
            migrated.append(sid)
    return migrated


def _copy_package(store, source: Path, skill_id: str) -> Path:
    target = store.allocate_package_dir("skill", skill_id)
    stage = target.parent / (".release-" + uuid.uuid4().hex)
    try:
        shutil.copytree(source, stage)
        stage.rename(target)
    finally:
        if stage.exists():
            shutil.rmtree(stage, ignore_errors=True)
    return target


def _skill_source(skills_root, skill_id):
    if skills_root is None:
        return None
    root = Path(skills_root)
    if not root.exists():
        return None
    source = root / skill_id
    if not source.resolve().is_relative_to(root.resolve()):
        raise ValueError("Invalid skill resource path")
    return source if source.is_dir() else None


def _restore_enabled(store, skill_id, previous):
    if previous.get("enabled") or previous.get("explicitly_disabled"):
        store.set_skill_enabled(skill_id, bool(previous.get("enabled")))


def refresh_released_skill_packages(store, declarations, skills_root):
    """Move packaged skills that still match the release they came from to this
    release's files. A skill the owner edited, or whose files changed, is kept."""
    from ..agent_runtime.extensions.store import compute_directory_manifest
    state = store._read()
    released = (state.get("release_defaults") or {}).get("skills") or {}
    refreshed = []
    for declared in declarations.get("skills", []):
        sid = declared["id"]
        skill = state["skills"].get(sid)
        if (not skill or not skill.get("package_dir") or skill.get("plugin_id") != declared.get("plugin_id")
                or sid not in released or skill.get("content") != released[sid].get("content")):
            continue
        old = Path(skill["package_dir"])
        try:
            if compute_directory_manifest(old.resolve())["manifest_sha256"] != skill.get("manifest_sha256"):
                continue
        except Exception:
            continue
        source = _skill_source(skills_root, sid)
        if source is not None and declared.get("content") == skill.get("content"):
            try:
                if compute_directory_manifest(source.resolve())["manifest_sha256"] == skill.get("manifest_sha256"):
                    continue
            except Exception:
                continue
        record = {**declared}
        if source is not None:
            record["package_dir"] = str(_copy_package(store, source, sid))
        store.commit_imported_items(plugins=[], mcp_servers=[], skills=[record])
        _restore_enabled(store, sid, skill)
        store._safe_rmtree(old)
        refreshed.append(sid)
    return refreshed


def bind_skill_resources(store, declarations, skills_root):
    """Give release defaults that have no package yet the files of this release;
    imported packages are kept."""
    current = store._read()["skills"]
    bound = []
    for declared in declarations.get("skills", []):
        sid = declared["id"]
        source = _skill_source(skills_root, sid)
        skill = current.get(sid)
        if (source is None or not skill or skill.get("package_dir")
                or skill.get("plugin_id") != declared.get("plugin_id")
                or skill.get("content") != declared.get("content")):
            continue
        target = _copy_package(store, source, sid)
        store.commit_imported_items(plugins=[], mcp_servers=[], skills=[{**declared, "package_dir": str(target)}])
        _restore_enabled(store, sid, skill)
        bound.append(sid)
    return bound


def apply_release_declarations(store, declarations, *, skills_root, service_id, port, plugin_ids):
    """Apply one release's declarations; returns what changed for the record."""
    refreshed = refresh_released_skill_packages(store, declarations, skills_root)
    migration = store.migrate_release_defaults(declarations)
    bound = bind_skill_resources(store, declarations, skills_root)
    oauth_migrated = migrate_declared_oauth_clients(store, declarations, plugin_ids)
    managed = configure_local_mcp_services(store, declarations, service_id, port)
    trusted = store.trust_installer_connections(
        [s for s in declarations.get("mcp_servers", []) if s.get("plugin_id") in set(plugin_ids)]
    )
    return {
        "migration": migration,
        "managed_servers": managed,
        "oauth_migrated": oauth_migrated,
        "trusted_connections": trusted,
        "skills_refreshed": refreshed,
        "skills_bound": bound,
    }
