"""Safe import preview and atomic commit for Plugins, Skills, and MCP server configs."""

from __future__ import annotations

import base64
import binascii
import copy
import io
import json
import os
import re
import shutil
import stat
import time
import uuid
import zipfile
from pathlib import Path
from threading import RLock
from typing import Any

from ..data_policy import normalize_policy
from ..store import AgentError
from .store import (
    COMPUTER_USE_PLUGIN_ID,
    COMPUTER_USE_SERVER_ID,
    ENV_KEY_RE,
    EXTENSION_ID_RE,
    HEADER_KEY_RE,
    MAX_SKILL_FILE_BYTES,
    MAX_SKILL_PACKAGE_DEPTH,
    MAX_SKILL_PACKAGE_FILES,
    MAX_SKILL_TOTAL_BYTES,
    ExtensionStore,
    _slugify,
    compute_directory_manifest,
    normalize_mcp_auth,
    validate_peer_provider,
)
from .views import normalize_views

try:
    import tomllib  # type: ignore[import-not-found]
except ImportError:  # pragma: no cover - Python 3.10 fallback
    import tomli as tomllib  # type: ignore[import-not-found, no-redef]

MAX_ARCHIVE_BYTES = 6 * 1024 * 1024
MAX_STAGED_PREVIEWS = 32
PREVIEW_TTL_SECONDS = 1800
PLUGIN_ROOT_TOKEN = "__RYN_PLUGIN_ROOT__"

_FRONTMATTER_RE = re.compile(r"\A---\r?\n(.*?)\r?\n---\r?\n?(.*)\Z", re.DOTALL)
_MD_LINK_RE = re.compile(r"\[[^\]]+\]\(([^)]+)\)")
_REL_REF_RE = re.compile(
    r"(?:^|[\s`\"'(])((?:references|refs|scripts|assets)/[A-Za-z0-9_./-]+)",
    re.MULTILINE,
)

KNOWN_UNSUPPORTED_MANIFEST_KEYS = {
    "hooks": "Lifecycle hooks are not executed by Ryn.",
    "commands": "Slash commands from external host packages are not executed by Ryn.",
    "agents": "Sub-agent definitions from external host packages are not executed by Ryn.",
    "apps": "External host app connectors (.app.json) are not supported in Ryn.",
    "connectors": "External cloud connectors are not supported in Ryn.",
    "outputStyles": "Output style overrides are not supported in Ryn.",
    "output_styles": "Output style overrides are not supported in Ryn.",
    "lspServers": "LSP server definitions in plugin manifests are not supported in Ryn.",
    "monitors": "Background monitor definitions are not supported in Ryn.",
    "oauth": "Top-level OAuth settings are not imported. Peer-provider login uses ryn.peerProvider.oauth.",
}

KNOWN_UNSUPPORTED_DIRS = {
    "hooks": ("hooks", "Directory 'hooks/' is ignored; Ryn does not run external lifecycle hooks."),
    "commands": ("commands", "Directory 'commands/' is ignored; Ryn does not import external slash commands."),
    "agents": ("agents", "Directory 'agents/' is ignored; Ryn does not import external sub-agents."),
    "apps": ("apps", "Directory 'apps/' is ignored; Ryn does not run external app connectors."),
    "output-styles": ("outputStyles", "Directory 'output-styles/' is ignored."),
}


def _parse_simple_toml(text: str) -> dict[str, Any]:
    """Parse TOML using tomllib (3.11+) or tomli (<3.11)."""
    loader = tomllib
    if loader is None:
        try:
            import tomli as loader  # type: ignore[import-not-found, no-redef]
        except ImportError:
            import importlib

            loader = importlib.import_module("tomllib")
    try:
        data = loader.loads(text)
    except Exception as exc:
        raise AgentError("agent_invalid_mcp_config") from exc
    if not isinstance(data, dict):
        raise AgentError("agent_invalid_mcp_config")
    return data


def parse_skill_markdown(text: str, fallback_id: str = "skill") -> dict[str, Any]:
    """Parse SKILL.md YAML frontmatter and markdown body."""
    if not isinstance(text, str) or not text.strip():
        raise AgentError("agent_invalid_skill_package")
    if len(text.encode("utf-8")) > MAX_SKILL_FILE_BYTES:
        raise AgentError("agent_skill_file_too_large")

    meta: dict[str, str] = {}
    body = text
    match = _FRONTMATTER_RE.match(text)
    if match:
        fm_raw, body = match.group(1), match.group(2)
        for line in fm_raw.splitlines():
            stripped = line.strip()
            if not stripped or stripped.startswith("#") or ":" not in stripped:
                continue
            k, v = stripped.split(":", 1)
            key = k.strip().lower()
            val = v.strip().strip('"').strip("'")
            if key and val:
                meta[key] = val

    body_stripped = body.strip()
    if not body_stripped and not meta.get("description"):
        raise AgentError("agent_invalid_skill_package")

    heading_title = ""
    first_para = ""
    for line in body_stripped.splitlines():
        s = line.strip()
        if not s:
            continue
        if s.startswith("#") and not heading_title:
            heading_title = s.lstrip("#").strip()
            continue
        if not s.startswith("#") and not first_para:
            first_para = s
            break

    raw_name = meta.get("name") or meta.get("id") or fallback_id
    skill_id = _slugify(raw_name, fallback=_slugify(fallback_id, "skill"))
    if not EXTENSION_ID_RE.match(skill_id):
        raise AgentError("agent_invalid_skill_package")

    title = meta.get("title") or heading_title or meta.get("name") or skill_id
    description = meta.get("description") or meta.get("summary") or first_para or title
    version = meta.get("version") or "1.0.0"

    referenced_paths: set[str] = set()
    for link_target in _MD_LINK_RE.findall(body):
        cleaned = link_target.split("#", 1)[0].split("?", 1)[0].strip()
        if (
            cleaned
            and not cleaned.startswith(("http://", "https://", "mailto:", "/"))
            and "://" not in cleaned
        ):
            referenced_paths.add(cleaned.replace("\\", "/").lstrip("./"))
    for rel_match in _REL_REF_RE.findall(body):
        cleaned = rel_match.strip().rstrip(".,;:)")
        if cleaned:
            referenced_paths.add(cleaned.replace("\\", "/"))

    return {
        "id": skill_id,
        "name": meta.get("name") or skill_id,
        "title": title[:160],
        "description": description[:600],
        "summary": description[:600],
        "version": str(version)[:64],
        "content": text,
        "referenced_paths": sorted(referenced_paths),
    }


def _validate_archive_member_name(name: str) -> tuple[str, ...]:
    if not name or "\x00" in name:
        raise AgentError("agent_extension_archive_traversal")
    normalized = name.replace("\\", "/")
    if normalized.startswith("/") or re.match(r"^[A-Za-z]:", normalized):
        raise AgentError("agent_extension_archive_traversal")
    parts = [p for p in normalized.split("/") if p and p != "."]
    if any(p == ".." for p in parts):
        raise AgentError("agent_extension_archive_traversal")
    if len(parts) > MAX_SKILL_PACKAGE_DEPTH + 1:
        raise AgentError("agent_skill_package_limit")
    return tuple(parts)


def extract_zip_safely(raw_zip: bytes, dest_dir: Path) -> Path:
    """Extract a zip archive into dest_dir with strict traversal, symlink, and size checks."""
    if len(raw_zip) > MAX_ARCHIVE_BYTES:
        raise AgentError("agent_skill_file_too_large")
    try:
        zf = zipfile.ZipFile(io.BytesIO(raw_zip))
    except Exception as exc:
        raise AgentError("agent_invalid_extension_archive") from exc

    dest_dir.mkdir(parents=True, exist_ok=True)
    dest_resolved = dest_dir.resolve()
    infos = zf.infolist()
    file_count = 0
    total_bytes = 0

    with zf:
        for info in infos:
            parts = _validate_archive_member_name(info.filename)
            if not parts:
                continue
            mode = (info.external_attr >> 16) & 0o170000
            if mode == 0o120000:
                raise AgentError("agent_extension_symlink_rejected")
            if info.is_dir() or info.filename.endswith(("/", "\\")):
                target_dir = (dest_resolved / Path(*parts)).resolve()
                if target_dir != dest_resolved and dest_resolved not in target_dir.parents:
                    raise AgentError("agent_extension_archive_traversal")
                target_dir.mkdir(parents=True, exist_ok=True)
                continue

            file_count += 1
            if file_count > MAX_SKILL_PACKAGE_FILES:
                raise AgentError("agent_skill_package_limit")
            if info.file_size > MAX_SKILL_FILE_BYTES:
                raise AgentError("agent_skill_file_too_large")
            if total_bytes + info.file_size > MAX_SKILL_TOTAL_BYTES:
                raise AgentError("agent_skill_package_limit")

            target_file = (dest_resolved / Path(*parts)).resolve()
            if dest_resolved not in target_file.parents:
                raise AgentError("agent_extension_archive_traversal")
            target_file.parent.mkdir(parents=True, exist_ok=True)

            written = 0
            with zf.open(info, "r") as src, target_file.open("wb") as dst:
                while True:
                    chunk = src.read(65536)
                    if not chunk:
                        break
                    written += len(chunk)
                    total_bytes += len(chunk)
                    if written > MAX_SKILL_FILE_BYTES or total_bytes > MAX_SKILL_TOTAL_BYTES:
                        raise AgentError("agent_skill_file_too_large")
                    dst.write(chunk)

    entries = [e for e in dest_resolved.iterdir() if e.name != "__MACOSX"]
    if len(entries) == 1 and entries[0].is_dir():
        return entries[0]
    return dest_resolved


def copy_directory_safely(src_dir: Path, dest_dir: Path) -> Path:
    """Copy a local directory into staging while enforcing symlink, size, and depth bounds."""
    try:
        st = os.lstat(src_dir)
    except OSError as exc:
        raise AgentError("agent_extension_source_not_found") from exc
    if stat.S_ISLNK(st.st_mode) or not stat.S_ISDIR(st.st_mode):
        raise AgentError("agent_extension_symlink_rejected")

    manifest = compute_directory_manifest(src_dir)
    dest_dir.mkdir(parents=True, exist_ok=True)
    for item in manifest["files"]:
        rel = item["path"]
        src_file = src_dir / Path(*rel.split("/"))
        dst_file = dest_dir / Path(*rel.split("/"))
        dst_file.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(src_file, dst_file)
    return dest_dir


def _substitute_plugin_root_token(value: str, package_root: Path | None) -> str:
    """Replace ${CLAUDE_PLUGIN_ROOT} and ${CODEX_PLUGIN_ROOT} safely within package root."""
    out = value.replace("${CLAUDE_PLUGIN_ROOT}", PLUGIN_ROOT_TOKEN).replace(
        "${CODEX_PLUGIN_ROOT}", PLUGIN_ROOT_TOKEN
    )
    if PLUGIN_ROOT_TOKEN in out:
        norm = out.replace("\\", "/")
        if f"{PLUGIN_ROOT_TOKEN}/.." in norm or "/../" in norm or norm.endswith("/.."):
            raise AgentError("agent_extension_archive_traversal")
        if package_root is not None:
            root_str = str(package_root.resolve())
            resolved_str = out.replace(PLUGIN_ROOT_TOKEN, root_str)
            candidate = Path(resolved_str).resolve()
            pkg_res = package_root.resolve()
            if candidate != pkg_res and pkg_res not in candidate.parents:
                raise AgentError("agent_extension_archive_traversal")
            return str(candidate)
    return out


def _load_plugin_manifest_json(path: Path) -> dict[str, Any]:
    """Read and validate a plugin.json manifest dictionary."""
    try:
        st = os.lstat(path)
    except OSError as exc:
        raise AgentError("agent_invalid_plugin_package") from exc
    if stat.S_ISLNK(st.st_mode):
        raise AgentError("agent_extension_symlink_rejected")
    if not stat.S_ISREG(st.st_mode):
        raise AgentError("agent_invalid_plugin_package")
    if st.st_size > MAX_SKILL_FILE_BYTES:
        raise AgentError("agent_skill_file_too_large")
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        raise AgentError("agent_invalid_plugin_package") from exc
    if not isinstance(data, dict):
        raise AgentError("agent_invalid_plugin_package")
    for key in ("id", "name", "version"):
        if key in data and data[key] is not None:
            if not isinstance(data[key], str) or not data[key].strip():
                raise AgentError("agent_invalid_plugin_package")
    for key in ("title", "description"):
        if key in data and data[key] is not None and not isinstance(data[key], str):
            raise AgentError("agent_invalid_plugin_package")
    if (
        "interface" in data
        and data["interface"] is not None
        and not isinstance(data["interface"], dict)
    ):
        raise AgentError("agent_invalid_plugin_package")
    return data


def _is_recognized_portable_manifest(data: dict[str, Any]) -> bool:
    """Return True if a root plugin.json dictionary represents a recognized portable manifest."""
    if not isinstance(data, dict):
        return False
    raw_identity = data.get("id") or data.get("name")
    if not isinstance(raw_identity, str) or not raw_identity.strip():
        return False
    schema = data.get("$schema")
    if schema is not None and (not isinstance(schema, str) or not schema.strip()):
        raise AgentError("agent_invalid_plugin_package")
    ext = data.get("extensions")
    if ext is not None and not isinstance(ext, dict):
        raise AgentError("agent_invalid_plugin_package")
    return True


def _parse_manifest_path_list(raw_value: Any) -> list[str]:
    """Validate and normalize a manifest path field accepting a relative string or string array."""
    if raw_value is None:
        return []
    if isinstance(raw_value, str):
        cleaned = raw_value.strip()
        if not cleaned:
            raise AgentError("agent_invalid_plugin_package")
        return [cleaned]
    if isinstance(raw_value, list):
        if not raw_value:
            raise AgentError("agent_invalid_plugin_package")
        out: list[str] = []
        for item in raw_value:
            if not isinstance(item, str) or not item.strip():
                raise AgentError("agent_invalid_plugin_package")
            out.append(item.strip())
        return out
    raise AgentError("agent_invalid_plugin_package")


def _resolve_manifest_relative_path(pkg_root: Path, raw_rel: str) -> Path:
    """Resolve a manifest-declared relative path safely inside pkg_root."""
    if not isinstance(raw_rel, str) or "\x00" in raw_rel:
        raise AgentError("agent_extension_archive_traversal")
    norm = raw_rel.strip().replace("\\", "/")
    if (
        not norm
        or norm.startswith("/")
        or norm.startswith("~")
        or re.match(r"^[A-Za-z]:", norm)
    ):
        raise AgentError("agent_extension_archive_traversal")
    parts = [p for p in norm.split("/") if p]
    if any(p == ".." for p in parts):
        raise AgentError("agent_extension_archive_traversal")
    clean_parts = [p for p in parts if p != "."]
    if len(clean_parts) > MAX_SKILL_PACKAGE_DEPTH:
        raise AgentError("agent_skill_package_limit")
    pkg_resolved = pkg_root.resolve()
    cur = pkg_resolved
    for part in clean_parts:
        cur = cur / part
        try:
            st = os.lstat(cur)
        except OSError:
            continue
        if stat.S_ISLNK(st.st_mode):
            raise AgentError("agent_extension_symlink_rejected")
    target = (pkg_resolved / Path(*clean_parts)).resolve() if clean_parts else pkg_resolved
    if target != pkg_resolved and pkg_resolved not in target.parents:
        raise AgentError("agent_extension_archive_traversal")
    return target


def _collect_skills_from_path(
    target_path: Path,
    pkg_resolved: Path,
    seen_skill_dirs: set[Path],
    skill_md_locations: list[tuple[Path, str]],
) -> int:
    """Scan a resolved skill path for SKILL.md and append unique skill directories."""
    matched = 0
    if target_path.is_file() and target_path.name == "SKILL.md":
        s_dir = target_path.parent.resolve()
        if s_dir != pkg_resolved and pkg_resolved not in s_dir.parents:
            raise AgentError("agent_extension_archive_traversal")
        matched += 1
        if s_dir not in seen_skill_dirs:
            seen_skill_dirs.add(s_dir)
            rel = "" if s_dir == pkg_resolved else s_dir.relative_to(pkg_resolved).as_posix()
            skill_md_locations.append((s_dir, rel))
        return matched

    if not target_path.is_dir():
        return 0

    if (target_path / "SKILL.md").is_file():
        s_dir = target_path.resolve()
        matched += 1
        if s_dir not in seen_skill_dirs:
            seen_skill_dirs.add(s_dir)
            rel = "" if s_dir == pkg_resolved else s_dir.relative_to(pkg_resolved).as_posix()
            skill_md_locations.append((s_dir, rel))

    for child in sorted(target_path.iterdir(), key=lambda p: p.name):
        try:
            child_st = os.lstat(child)
        except OSError as exc:
            raise AgentError("agent_skill_package_missing") from exc
        if stat.S_ISLNK(child_st.st_mode):
            raise AgentError("agent_extension_symlink_rejected")
        if stat.S_ISDIR(child_st.st_mode) and (child / "SKILL.md").is_file():
            s_dir = child.resolve()
            if s_dir != pkg_resolved and pkg_resolved not in s_dir.parents:
                raise AgentError("agent_extension_archive_traversal")
            matched += 1
            if s_dir not in seen_skill_dirs:
                seen_skill_dirs.add(s_dir)
                rel = "" if s_dir == pkg_resolved else s_dir.relative_to(pkg_resolved).as_posix()
                skill_md_locations.append((s_dir, rel))
    return matched


class ExtensionImportManager:
    """Manages bounded import previews and atomic commits into ExtensionStore."""

    def __init__(self, store: ExtensionStore) -> None:
        self.store = store
        self.staging_root = self.store.root / "import_staging"
        self.staging_root.mkdir(parents=True, exist_ok=True)
        self._lock = RLock()
        self._previews: dict[str, dict[str, Any]] = {}

    def _cleanup_expired(self) -> None:
        now = time.time()
        expired = [
            pid
            for pid, item in self._previews.items()
            if now - item.get("created_at", 0) > PREVIEW_TTL_SECONDS
        ]
        for pid in expired:
            entry = self._previews.pop(pid, None)
            if entry and entry.get("staging_dir"):
                shutil.rmtree(entry["staging_dir"], ignore_errors=True)
        while len(self._previews) >= MAX_STAGED_PREVIEWS:
            oldest_id = next(iter(self._previews))
            entry = self._previews.pop(oldest_id, None)
            if entry and entry.get("staging_dir"):
                shutil.rmtree(entry["staging_dir"], ignore_errors=True)

    def preview(self, payload: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(payload, dict):
            raise AgentError("agent_invalid_request")
        kind = str(payload.get("kind") or "").strip().lower()
        if kind not in ("plugin", "skill", "mcp"):
            raise AgentError("agent_invalid_extension_kind")

        sources = [
            k
            for k in ("path", "config_text", "skill_text", "zip_base64")
            if payload.get(k) is not None and str(payload.get(k)).strip() != ""
        ]
        if len(sources) != 1:
            raise AgentError("agent_invalid_import_source")
        source_type = sources[0]
        filename = str(payload.get("filename") or "").strip()

        with self._lock:
            self._cleanup_expired()
            preview_id = uuid.uuid4().hex
            staging_dir = (self.staging_root / preview_id).resolve()
            try:
                if kind == "mcp":
                    parsed = self._preview_mcp(payload, source_type, filename, staging_dir)
                elif kind == "skill":
                    parsed = self._preview_skill(payload, source_type, filename, staging_dir)
                else:
                    parsed = self._preview_plugin(payload, source_type, filename, staging_dir)

                current_overview = self.store.overview()
                existing_plugins = {p["id"] for p in current_overview["plugins"]}
                existing_skills = {s["id"] for s in current_overview["skills"]}
                existing_builtin_skills = {
                    s["id"] for s in current_overview["skills"] if s.get("builtin")
                }
                existing_servers = {m["id"] for m in current_overview["mcp_servers"]}

                warnings: list[str] = list(parsed.get("warnings", []))
                can_import = bool(parsed.get("can_import", True))

                for p in parsed["plugins"]:
                    if p["id"] == COMPUTER_USE_PLUGIN_ID:
                        warnings.append(f"Plugin ID '{p['id']}' conflicts with a built-in plugin.")
                        can_import = False
                    elif p["id"] in existing_plugins:
                        warnings.append(
                            f"Plugin '{p['id']}' is already installed and will be updated on commit."
                        )

                for s in parsed["skills"]:
                    if s["id"] in existing_builtin_skills:
                        warnings.append(f"Skill ID '{s['id']}' conflicts with a built-in skill.")
                        can_import = False
                    elif s["id"] in existing_skills:
                        warnings.append(
                            f"Skill '{s['id']}' is already installed and will be updated to a new generation on commit."
                        )

                for m in parsed["mcp_servers"]:
                    if m["id"] == COMPUTER_USE_SERVER_ID:
                        warnings.append(f"MCP server ID '{m['id']}' conflicts with a built-in server.")
                        can_import = False
                    elif m["id"] in existing_servers:
                        warnings.append(
                            f"MCP server '{m['id']}' is already configured and will be replaced on commit."
                        )

                if not (parsed["plugins"] or parsed["skills"] or parsed["mcp_servers"]):
                    can_import = False
                    warnings.append("No importable plugins, skills, or MCP servers were found.")

                if (
                    len({p["id"] for p in parsed["plugins"]}) != len(parsed["plugins"])
                    or len({s["id"] for s in parsed["skills"]}) != len(parsed["skills"])
                    or len({m["id"] for m in parsed["mcp_servers"]}) != len(parsed["mcp_servers"])
                ):
                    raise AgentError("agent_import_duplicate_id")

                public_plugins = [
                    {
                        "id": p["id"],
                        "name": p["name"],
                        "title": p["title"],
                        "description": p["description"],
                        "version": p["version"],
                        "source_format": p["source_format"],
                        "skill_ids": p["skill_ids"],
                        "mcp_server_ids": p["mcp_server_ids"],
                        "peer_provider": copy.deepcopy(p.get("peer_provider")),
                        "data_policy": normalize_policy(p.get("data_policy")),
                        "views": copy.deepcopy(p.get("views", [])),
                    }
                    for p in parsed["plugins"]
                ]
                public_skills = [
                    {
                        "id": s["id"],
                        "name": s["name"],
                        "title": s["title"],
                        "description": s["description"],
                        "version": s["version"],
                        "files": [{"path": f["path"], "size": f.get("size", 0)} for f in s.get("files", [])],
                    }
                    for s in parsed["skills"]
                ]
                public_servers = [
                    {
                        "id": m["id"],
                        "name": m["name"],
                        "title": m["name"],
                        "description": m.get("description", ""),
                        "transport": m["transport"],
                        "command": m.get("command") or "",
                        "args": list(m.get("args") or []),
                        "url": m.get("url") or "",
                        "bearer_token_env": m.get("bearer_token_env") or "",
                        "auth": normalize_mcp_auth(m.get("auth")),
                        "data_policy": normalize_policy(m.get("data_policy")),
                        "login_required": m.get("auth", {}).get("mode") == "oauth",
                        "oauth_issuer": None,
                        "enabled_tools": m.get("enabled_tools"),
                        "disabled_tools": list(m.get("disabled_tools") or []),
                        "env_keys": sorted((m.get("secret_env") or {}).keys()),
                        "env_vars": list(m.get("env_vars") or []),
                        "header_keys": sorted((m.get("secret_headers") or {}).keys()),
                        "env_http_headers": dict(m.get("env_http_headers") or {}),
                        "has_secrets": bool(m.get("secret_env") or m.get("secret_headers")),
                    }
                    for m in parsed["mcp_servers"]
                ]

                staged_record = {
                    "preview_id": preview_id,
                    "created_at": time.time(),
                    "staging_dir": staging_dir if staging_dir.exists() else None,
                    "can_import": can_import,
                    "internal_plugins": parsed["plugins"],
                    "internal_skills": parsed["skills"],
                    "internal_mcp_servers": parsed["mcp_servers"],
                }
                self._previews[preview_id] = staged_record

                return {
                    "preview_id": preview_id,
                    "name": parsed["name"],
                    "version": parsed["version"],
                    "source_format": parsed["source_format"],
                    "plugins": public_plugins,
                    "skills": public_skills,
                    "mcp_servers": public_servers,
                    "warnings": warnings,
                    "unsupported_components": sorted(set(parsed.get("unsupported_components", []))),
                    "can_import": can_import,
                    "expected_revision": current_overview["revision"],
                }
            except Exception:
                if staging_dir.exists():
                    shutil.rmtree(staging_dir, ignore_errors=True)
                raise

    def commit(
        self,
        preview_id: str,
        *,
        expected_revision: int,
        desktop_access: bool = False,
    ) -> dict[str, Any]:
        if not isinstance(preview_id, str) or not preview_id:
            raise AgentError("agent_import_preview_not_found")
        if type(expected_revision) is not int or expected_revision < 1:
            raise AgentError("agent_invalid_revision")

        with self._lock:
            self._cleanup_expired()
            staged = self._previews.get(preview_id)
            if not staged:
                raise AgentError("agent_import_preview_not_found")
            if not staged.get("can_import"):
                raise AgentError("agent_import_not_allowed")

            # Check expected_revision before moving staged directories
            if self.store.revision() != expected_revision:
                raise AgentError("agent_extensions_revision_conflict")

            plugins = copy.deepcopy(staged["internal_plugins"])
            skills = copy.deepcopy(staged["internal_skills"])
            servers = copy.deepcopy(staged["internal_mcp_servers"])

            if (
                len({p["id"] for p in plugins}) != len(plugins)
                or len({s["id"] for s in skills}) != len(skills)
                or len({m["id"] for m in servers}) != len(servers)
            ):
                raise AgentError("agent_import_duplicate_id")

            # 1. Verify all staged package hashes match the exact preview snapshot before copying
            for plugin in plugins:
                staged_pkg = plugin.get("staged_package_dir")
                expected_hash = plugin.get("manifest_sha256")
                if staged_pkg:
                    current_manifest = compute_directory_manifest(Path(staged_pkg))
                    if not expected_hash or current_manifest["manifest_sha256"] != expected_hash:
                        self._previews.pop(preview_id, None)
                        if staged.get("staging_dir"):
                            shutil.rmtree(staged["staging_dir"], ignore_errors=True)
                        raise AgentError("agent_extension_package_integrity_failed")

            for skill in skills:
                staged_pkg = skill.get("staged_package_dir")
                p_id = skill.get("plugin_id")
                rel_in_plugin = skill.get("plugin_rel_dir")
                expected_hash = skill.get("manifest_sha256")
                expected_files = skill.get("files")
                check_dir: Path | None = None
                if staged_pkg:
                    check_dir = Path(staged_pkg)
                elif p_id and rel_in_plugin is not None:
                    parent_plugin = next((p for p in plugins if p["id"] == p_id), None)
                    if parent_plugin and parent_plugin.get("staged_package_dir"):
                        base_dir = Path(parent_plugin["staged_package_dir"])
                        check_dir = (base_dir / rel_in_plugin).resolve() if rel_in_plugin else base_dir
                if check_dir is not None:
                    current_manifest = compute_directory_manifest(check_dir)
                    if (
                        not expected_hash
                        or current_manifest["manifest_sha256"] != expected_hash
                        or (expected_files is not None and current_manifest["files"] != expected_files)
                    ):
                        self._previews.pop(preview_id, None)
                        if staged.get("staging_dir"):
                            shutil.rmtree(staged["staging_dir"], ignore_errors=True)
                        raise AgentError("agent_extension_package_integrity_failed")

            plugin_pkg_map: dict[str, Path] = {}
            allocated_dirs: list[Path] = []
            try:
                for plugin in plugins:
                    staged_pkg = plugin.pop("staged_package_dir", None)
                    expected_hash = plugin.get("manifest_sha256")
                    if staged_pkg:
                        dest_pkg = self.store.allocate_package_dir("plugin", plugin["id"])
                        allocated_dirs.append(dest_pkg)
                        shutil.copytree(Path(staged_pkg), dest_pkg, dirs_exist_ok=True)
                        manifest = compute_directory_manifest(dest_pkg)
                        if not expected_hash or manifest["manifest_sha256"] != expected_hash:
                            raise AgentError("agent_extension_package_integrity_failed")
                        plugin["package_dir"] = str(dest_pkg)
                        plugin["manifest_sha256"] = expected_hash
                        plugin_pkg_map[plugin["id"]] = dest_pkg

                for skill in skills:
                    staged_pkg = skill.pop("staged_package_dir", None)
                    p_id = skill.get("plugin_id")
                    rel_in_plugin = skill.pop("plugin_rel_dir", None)
                    expected_hash = skill.get("manifest_sha256")
                    expected_files = skill.get("files")
                    if p_id and p_id in plugin_pkg_map and rel_in_plugin is not None:
                        skill_pkg = (
                            (plugin_pkg_map[p_id] / rel_in_plugin).resolve()
                            if rel_in_plugin
                            else plugin_pkg_map[p_id]
                        )
                        manifest = compute_directory_manifest(skill_pkg)
                        if (
                            not expected_hash
                            or manifest["manifest_sha256"] != expected_hash
                            or (expected_files is not None and manifest["files"] != expected_files)
                        ):
                            raise AgentError("agent_extension_package_integrity_failed")
                        skill["package_dir"] = str(skill_pkg)
                        skill["manifest_sha256"] = expected_hash
                        skill["files"] = manifest["files"]
                    elif staged_pkg:
                        dest_pkg = self.store.allocate_package_dir("skill", skill["id"])
                        allocated_dirs.append(dest_pkg)
                        shutil.copytree(Path(staged_pkg), dest_pkg, dirs_exist_ok=True)
                        manifest = compute_directory_manifest(dest_pkg)
                        if (
                            not expected_hash
                            or manifest["manifest_sha256"] != expected_hash
                            or (expected_files is not None and manifest["files"] != expected_files)
                        ):
                            raise AgentError("agent_extension_package_integrity_failed")
                        skill["package_dir"] = str(dest_pkg)
                        skill["manifest_sha256"] = expected_hash
                        skill["files"] = manifest["files"]

                for srv in servers:
                    p_id = srv.get("plugin_id")
                    pkg_root = plugin_pkg_map.get(p_id) if p_id else None
                    if srv.get("command"):
                        srv["command"] = _substitute_plugin_root_token(srv["command"], pkg_root)
                    if srv.get("args"):
                        srv["args"] = [
                            _substitute_plugin_root_token(str(a), pkg_root) for a in srv["args"]
                        ]
                    if srv.get("cwd"):
                        srv["cwd"] = _substitute_plugin_root_token(srv["cwd"], pkg_root)
                    if srv.get("secret_env"):
                        srv["secret_env"] = {
                            k: _substitute_plugin_root_token(str(v), pkg_root)
                            for k, v in srv["secret_env"].items()
                        }

                result = self.store.commit_imported_items(
                    plugins=plugins,
                    skills=skills,
                    mcp_servers=servers,
                    expected_revision=expected_revision,
                    desktop_access=desktop_access,
                )
            except Exception:
                for allocated in allocated_dirs:
                    shutil.rmtree(allocated, ignore_errors=True)
                raise

            self._previews.pop(preview_id, None)
            if staged.get("staging_dir"):
                shutil.rmtree(staged["staging_dir"], ignore_errors=True)
            return result["overview"]

    # ------------------------------------------------------------------
    # Internal Preview Parsers
    # ------------------------------------------------------------------

    def _preview_mcp(
        self,
        payload: dict[str, Any],
        source_type: str,
        filename: str,
        staging_dir: Path,
    ) -> dict[str, Any]:
        text = ""
        if source_type == "config_text":
            text = str(payload["config_text"])
        elif source_type == "path":
            src_path = Path(str(payload["path"])).expanduser()
            try:
                st = os.lstat(src_path)
            except OSError as exc:
                raise AgentError("agent_extension_source_not_found") from exc
            if stat.S_ISLNK(st.st_mode) or not stat.S_ISREG(st.st_mode):
                raise AgentError("agent_extension_symlink_rejected")
            if st.st_size > MAX_SKILL_FILE_BYTES:
                raise AgentError("agent_skill_file_too_large")
            text = src_path.read_text(encoding="utf-8")
            filename = filename or src_path.name
        elif source_type == "zip_base64":
            raw_zip = self._decode_base64(str(payload["zip_base64"]))
            root = extract_zip_safely(raw_zip, staging_dir)
            for candidate in (".mcp.json", "mcp.json", "config.toml"):
                c_path = root / candidate
                if c_path.is_file():
                    text = c_path.read_text(encoding="utf-8")
                    filename = candidate
                    break
            if not text:
                raise AgentError("agent_invalid_mcp_config")
        else:
            raise AgentError("agent_invalid_import_source")

        servers, source_format, warnings, unsupported = self._parse_mcp_config_text(
            text, filename=filename
        )
        title = servers[0]["name"] if len(servers) == 1 else "MCP Configuration"
        return {
            "name": title,
            "version": "1.0.0",
            "source_format": source_format,
            "plugins": [],
            "skills": [],
            "mcp_servers": servers,
            "warnings": warnings,
            "unsupported_components": unsupported,
            "can_import": len(servers) > 0 and "sse" not in unsupported,
        }

    def _parse_mcp_config_text(
        self,
        text: str,
        *,
        filename: str = "",
        plugin_id: str | None = None,
        package_root: Path | None = None,
        default_data_policy: dict[str, Any] | None = None,
    ) -> tuple[list[dict[str, Any]], str, list[str], list[str]]:
        stripped = text.strip()
        if not stripped:
            raise AgentError("agent_invalid_mcp_config")

        warnings: list[str] = []
        unsupported: list[str] = []
        raw_servers: dict[str, Any] = {}
        source_format = "mcp_json"

        is_toml = filename.lower().endswith(".toml") or (
            not stripped.startswith("{") and "[mcp_servers" in stripped
        )
        if is_toml:
            source_format = "codex_toml"
            data = _parse_simple_toml(stripped)
            mcp_tbl = data.get("mcp_servers") or data.get("mcpServers")
            if not isinstance(mcp_tbl, dict):
                raise AgentError("agent_invalid_mcp_config")
            raw_servers = mcp_tbl
        else:
            try:
                data = json.loads(stripped)
            except Exception as exc:
                raise AgentError("agent_invalid_mcp_config") from exc
            if not isinstance(data, dict):
                raise AgentError("agent_invalid_mcp_config")
            if isinstance(data.get("mcpServers"), dict):
                raw_servers = data["mcpServers"]
            elif isinstance(data.get("mcp_servers"), dict):
                raw_servers = data["mcp_servers"]
            elif any(k in data for k in ("command", "url", "httpUrl", "sseUrl", "type", "transport")):
                sid = str(data.get("id") or data.get("name") or "mcp-server")
                raw_servers = {sid: data}
            else:
                raise AgentError("agent_invalid_mcp_config")

        servers: list[dict[str, Any]] = []
        seen_server_ids: set[str] = set()
        for raw_key, cfg in raw_servers.items():
            if not isinstance(cfg, dict):
                raise AgentError("agent_invalid_mcp_config")
            sid = _slugify(f"{plugin_id}-{raw_key}" if plugin_id else str(raw_key), "mcp-server")
            if not EXTENSION_ID_RE.match(sid):
                raise AgentError("agent_mcp_invalid_id")
            if sid in seen_server_ids:
                raise AgentError("agent_import_duplicate_id")
            seen_server_ids.add(sid)

            ryn = cfg.get("ryn", {})
            if not isinstance(ryn, dict):
                raise AgentError("agent_invalid_data_policy")
            data_policy = normalize_policy(ryn.get("dataPolicy", default_data_policy))
            auth = normalize_mcp_auth(cfg.get("auth"))
            if "client_secret" in cfg or "clientSecret" in cfg:
                raise AgentError("agent_mcp_invalid_auth")
            if "oauth" in cfg or "oauthClientId" in cfg:
                if "auth" in cfg:
                    raise AgentError("agent_mcp_invalid_auth")
                oauth = cfg.get("oauth", {})
                if not isinstance(oauth, dict) or set(oauth) - {"clientId", "client_id", "scopes", "callbackPort", "callback_port"}:
                    raise AgentError("agent_mcp_invalid_auth")
                client_ids = [oauth[key] for key in ("clientId", "client_id") if key in oauth]
                if "oauthClientId" in cfg:
                    client_ids.append(cfg["oauthClientId"])
                ports = [oauth[key] for key in ("callbackPort", "callback_port") if key in oauth]
                if (client_ids and any(value != client_ids[0] for value in client_ids)) or (ports and any(value != ports[0] for value in ports)):
                    raise AgentError("agent_mcp_invalid_auth")
                auth = normalize_mcp_auth({"mode": "oauth", "client_id": client_ids[0] if client_ids else None,
                    "scopes": oauth.get("scopes", []), "callback_port": ports[0] if ports else None})
            if "auth_url" in cfg:
                warnings.append(
                    f"MCP server '{sid}' auth_url is ignored; authorization endpoints are discovered when signing in."
                )

            url = str(cfg.get("url") or cfg.get("httpUrl") or "").strip()
            command = str(cfg.get("command") or "").strip()
            type_field = cfg.get("type")
            transport_field = cfg.get("transport")
            if (type_field is not None and not isinstance(type_field, str)) or (
                transport_field is not None and not isinstance(transport_field, str)
            ):
                raise AgentError("agent_invalid_mcp_config")
            raw_transport = str(type_field or transport_field or "").strip().lower()
            if raw_transport in ("sse", "http+sse", "legacy-sse", "legacy_sse") or (
                "sseUrl" in cfg and not url and not command
            ):
                unsupported.append("sse")
                warnings.append(
                    f"MCP server '{sid}' declares legacy SSE transport, which is not supported in v1; only 'stdio' and 'streamable_http' are supported."
                )
                continue
            if raw_transport in ("http", "streamable-http", "streamable_http"):
                transport = "streamable_http"
            elif raw_transport == "stdio":
                transport = "stdio"
            elif raw_transport == "":
                transport = "streamable_http" if (url and not command) else "stdio"
            else:
                raise AgentError("agent_mcp_invalid_transport")

            if transport == "stdio":
                if not command:
                    raise AgentError("agent_mcp_command_required")
                command = _substitute_plugin_root_token(command, package_root)
                raw_args = cfg.get("args") or []
                if not isinstance(raw_args, list) or any(not isinstance(a, str) for a in raw_args):
                    raise AgentError("agent_mcp_invalid_args")
                args = [_substitute_plugin_root_token(str(a), package_root) for a in raw_args[:64]]
                cwd_raw = cfg.get("cwd")
                cwd = (
                    _substitute_plugin_root_token(str(cwd_raw).strip(), package_root)
                    if cwd_raw
                    else None
                )
            else:
                if not url or not (url.startswith("http://") or url.startswith("https://")):
                    raise AgentError("agent_mcp_url_required")
                args = []
                cwd = None

            secret_env: dict[str, str] = {}
            env_dict = cfg.get("env")
            if isinstance(env_dict, dict):
                for k, v in env_dict.items():
                    k_str = str(k).strip()
                    if not ENV_KEY_RE.match(k_str):
                        raise AgentError("agent_mcp_invalid_env_key")
                    if v is not None:
                        secret_env[k_str] = _substitute_plugin_root_token(str(v), package_root)

            raw_env_vars = cfg.get("env_vars") if "env_vars" in cfg else cfg.get("envVars")
            env_vars: list[str] = []
            if raw_env_vars is not None:
                if not isinstance(raw_env_vars, list) or any(not isinstance(item, str) for item in raw_env_vars):
                    raise AgentError("agent_mcp_invalid_env_key")
                for item in raw_env_vars[:64]:
                    var_name = str(item).strip()
                    if not var_name:
                        continue
                    if not ENV_KEY_RE.match(var_name):
                        raise AgentError("agent_mcp_invalid_env_key")
                    if var_name not in env_vars:
                        env_vars.append(var_name)

            secret_headers: dict[str, str] = {}
            hdr_dict = cfg.get("headers") or cfg.get("http_headers")
            if isinstance(hdr_dict, dict):
                for k, v in hdr_dict.items():
                    k_str = str(k).strip()
                    if not HEADER_KEY_RE.match(k_str):
                        raise AgentError("agent_mcp_invalid_header_key")
                    if v is not None:
                        secret_headers[k_str] = str(v)

            raw_env_http_headers = (
                cfg.get("env_http_headers") if "env_http_headers" in cfg else cfg.get("envHttpHeaders")
            )
            env_http_headers: dict[str, str] = {}
            if raw_env_http_headers is not None:
                if not isinstance(raw_env_http_headers, dict):
                    raise AgentError("agent_mcp_invalid_header_key")
                for k, v in raw_env_http_headers.items():
                    k_str = str(k).strip()
                    if not k_str:
                        continue
                    if not HEADER_KEY_RE.match(k_str):
                        raise AgentError("agent_mcp_invalid_header_key")
                    if v is not None and str(v).strip():
                        env_name = str(v).strip()
                        if not ENV_KEY_RE.match(env_name):
                            raise AgentError("agent_mcp_invalid_env_key")
                        env_http_headers[k_str] = env_name

            bearer_token_env = str(
                cfg.get("bearer_token_env") or cfg.get("bearer_token_env_var") or ""
            ).strip() or None
            if bearer_token_env and not ENV_KEY_RE.match(bearer_token_env):
                raise AgentError("agent_mcp_invalid_bearer_token_env")

            startup_timeout_ms = int(
                cfg.get("startup_timeout_ms")
                or (int(cfg["startup_timeout_sec"]) * 1000 if "startup_timeout_sec" in cfg else 10000)
            )
            tool_timeout_ms = int(
                cfg.get("tool_timeout_ms")
                or (int(cfg["tool_timeout_sec"]) * 1000 if "tool_timeout_sec" in cfg else 30000)
            )

            raw_enabled_tools = cfg.get("enabled_tools")
            if raw_enabled_tools is None:
                raw_enabled_tools = cfg.get("enabledTools")
            enabled_tools = (
                sorted({str(x).strip() for x in raw_enabled_tools if str(x).strip()})
                if isinstance(raw_enabled_tools, list)
                else None
            )
            raw_disabled_tools = cfg.get("disabled_tools")
            if raw_disabled_tools is None:
                raw_disabled_tools = cfg.get("disabledTools")
            disabled_tools = (
                sorted({str(x).strip() for x in raw_disabled_tools if str(x).strip()})
                if isinstance(raw_disabled_tools, list)
                else []
            )
            if cfg.get("read_only_tools") or cfg.get("readOnlyTools"):
                warnings.append(
                    f"MCP server '{sid}' declares read_only_tools in imported config; imported tools remain write-classified until explicitly configured by the local owner."
                )

            servers.append(
                {
                    "id": sid,
                    "import_key": str(raw_key),
                    "name": str(cfg.get("name") or raw_key),
                    "description": str(cfg.get("description") or "")[:1000],
                    "transport": transport,
                    "plugin_id": plugin_id,
                    "computer_access": bool(cfg.get("computer_access", False)),
                    "command": command if transport == "stdio" else None,
                    "args": args,
                    "cwd": cwd,
                    "secret_env": secret_env,
                    "env_vars": env_vars if transport == "stdio" else [],
                    "url": url if transport == "streamable_http" else None,
                    "bearer_token_env": bearer_token_env if transport == "streamable_http" else None,
                    "auth": auth,
                    "data_policy": data_policy,
                    "oauth_client_registration": str(cfg.get("oauth_client_registration") or cfg.get("oauthClientRegistration") or "auto"),
                    "secret_headers": secret_headers,
                    "env_http_headers": env_http_headers if transport == "streamable_http" else {},
                    "startup_timeout_ms": max(1000, min(60000, startup_timeout_ms)),
                    "tool_timeout_ms": max(1000, min(120000, tool_timeout_ms)),
                    "enabled_tools": enabled_tools,
                    "disabled_tools": disabled_tools,
                    "read_only_tools": [],
                }
            )
        return servers, source_format, warnings, unsupported

    def _preview_skill(
        self,
        payload: dict[str, Any],
        source_type: str,
        filename: str,
        staging_dir: Path,
    ) -> dict[str, Any]:
        warnings: list[str] = []
        fallback_id = Path(filename).stem if filename else "imported-skill"
        if fallback_id.upper() == "SKILL":
            fallback_id = "imported-skill"

        if source_type == "skill_text":
            parsed_md = parse_skill_markdown(str(payload["skill_text"]), fallback_id=fallback_id)
            staging_dir.mkdir(parents=True, exist_ok=True)
            (staging_dir / "SKILL.md").write_text(parsed_md["content"], encoding="utf-8")
            pkg_root = staging_dir
        elif source_type == "zip_base64":
            raw_zip = self._decode_base64(str(payload["zip_base64"]))
            pkg_root = extract_zip_safely(raw_zip, staging_dir)
            if fallback_id == "imported-skill" and pkg_root != staging_dir:
                fallback_id = pkg_root.name
        elif source_type == "path":
            src_path = Path(str(payload["path"])).expanduser()
            try:
                st = os.lstat(src_path)
            except OSError as exc:
                raise AgentError("agent_extension_source_not_found") from exc
            if stat.S_ISLNK(st.st_mode):
                raise AgentError("agent_extension_symlink_rejected")
            if stat.S_ISREG(st.st_mode):
                if src_path.suffix.lower() == ".zip":
                    if st.st_size > MAX_ARCHIVE_BYTES:
                        raise AgentError("agent_skill_file_too_large")
                    pkg_root = extract_zip_safely(src_path.read_bytes(), staging_dir)
                else:
                    if st.st_size > MAX_SKILL_FILE_BYTES:
                        raise AgentError("agent_skill_file_too_large")
                    text = src_path.read_text(encoding="utf-8")
                    staging_dir.mkdir(parents=True, exist_ok=True)
                    (staging_dir / "SKILL.md").write_text(text, encoding="utf-8")
                    pkg_root = staging_dir
                    if src_path.stem.upper() != "SKILL":
                        fallback_id = src_path.stem
                    elif src_path.parent.name:
                        fallback_id = src_path.parent.name
            elif stat.S_ISDIR(st.st_mode):
                pkg_root = copy_directory_safely(src_path, staging_dir)
                fallback_id = src_path.name or fallback_id
            else:
                raise AgentError("agent_invalid_skill_package")
        else:
            raise AgentError("agent_invalid_import_source")

        skill_md_path = pkg_root / "SKILL.md"
        if not skill_md_path.is_file():
            raise AgentError("agent_invalid_skill_package")
        manifest = compute_directory_manifest(pkg_root)
        raw_md_text = skill_md_path.read_text(encoding="utf-8")
        parsed_md = parse_skill_markdown(raw_md_text, fallback_id=fallback_id)
        from .codex_skills import extract_codex_skill_metadata

        codex_meta = extract_codex_skill_metadata(pkg_root, raw_md_text, fallback_id=fallback_id)
        present_files = {f["path"] for f in manifest["files"]}
        for ref in parsed_md["referenced_paths"]:
            if ref not in present_files:
                warnings.append(f"Skill '{parsed_md['id']}' references missing file '{ref}'.")

        skill_entry = {
            "id": parsed_md["id"],
            "name": parsed_md["name"],
            "title": parsed_md["title"],
            "description": parsed_md["description"],
            "short_description": codex_meta.get("short_description") or parsed_md["summary"],
            "summary": parsed_md["summary"],
            "version": parsed_md["version"],
            "content": parsed_md["content"],
            "files": manifest["files"],
            "manifest_sha256": manifest["manifest_sha256"],
            "staged_package_dir": str(pkg_root),
            "source_format": "skill_md",
            "scope": "user",
            "interface": codex_meta.get("interface") or {},
            "policy": codex_meta.get("policy") or {"allow_implicit_invocation": True},
            "warnings": warnings,
        }
        return {
            "name": parsed_md["title"],
            "version": parsed_md["version"],
            "source_format": "skill_md",
            "plugins": [],
            "skills": [skill_entry],
            "mcp_servers": [],
            "warnings": warnings,
            "unsupported_components": [],
            "can_import": True,
        }

    def _preview_plugin(
        self,
        payload: dict[str, Any],
        source_type: str,
        filename: str,
        staging_dir: Path,
    ) -> dict[str, Any]:
        if source_type == "zip_base64":
            raw_zip = self._decode_base64(str(payload["zip_base64"]))
            pkg_root = extract_zip_safely(raw_zip, staging_dir)
        elif source_type == "path":
            src_path = Path(str(payload["path"])).expanduser()
            try:
                st = os.lstat(src_path)
            except OSError as exc:
                raise AgentError("agent_extension_source_not_found") from exc
            if stat.S_ISLNK(st.st_mode):
                raise AgentError("agent_extension_symlink_rejected")
            if stat.S_ISREG(st.st_mode) and src_path.suffix.lower() == ".zip":
                if st.st_size > MAX_ARCHIVE_BYTES:
                    raise AgentError("agent_skill_file_too_large")
                pkg_root = extract_zip_safely(src_path.read_bytes(), staging_dir)
            elif stat.S_ISDIR(st.st_mode):
                pkg_root = copy_directory_safely(src_path, staging_dir)
            else:
                raise AgentError("agent_invalid_plugin_package")
        else:
            raise AgentError("agent_invalid_import_source")

        manifest_info = compute_directory_manifest(pkg_root)
        codex_manifest = pkg_root / ".codex-plugin" / "plugin.json"
        claude_manifest = pkg_root / ".claude-plugin" / "plugin.json"
        portable_manifest = pkg_root / "plugin.json"

        codex_overlay: dict[str, Any] | None = None
        is_portable_root = False
        if portable_manifest.is_file():
            portable_data = _load_plugin_manifest_json(portable_manifest)
            if codex_manifest.is_file():
                codex_overlay = _load_plugin_manifest_json(codex_manifest)
            if _is_recognized_portable_manifest(portable_data) or (
                codex_overlay is None and not claude_manifest.is_file()
            ):
                source_format = "ryn_plugin"
                raw_manifest = portable_data
                is_portable_root = True
            elif codex_overlay is not None:
                source_format = "codex_plugin"
                raw_manifest = codex_overlay
                codex_overlay = None
            else:
                source_format = "claude_plugin"
                raw_manifest = _load_plugin_manifest_json(claude_manifest)
        elif codex_manifest.is_file():
            source_format = "codex_plugin"
            raw_manifest = _load_plugin_manifest_json(codex_manifest)
        elif claude_manifest.is_file():
            source_format = "claude_plugin"
            raw_manifest = _load_plugin_manifest_json(claude_manifest)
        else:
            raise AgentError("agent_invalid_plugin_package")

        fallback_name = pkg_root.name if pkg_root != staging_dir else (Path(filename).stem or "plugin")
        plugin_id = _slugify(
            str(raw_manifest.get("id") or raw_manifest.get("name") or fallback_name),
            fallback="plugin",
        )
        if not EXTENSION_ID_RE.match(plugin_id):
            raise AgentError("agent_invalid_plugin_package")

        openai_ext: dict[str, Any] | None = None
        extensions_obj = raw_manifest.get("extensions")
        if extensions_obj is not None:
            if not isinstance(extensions_obj, dict):
                raise AgentError("agent_invalid_plugin_package")
            if "com.openai" in extensions_obj:
                if not isinstance(extensions_obj["com.openai"], dict):
                    raise AgentError("agent_invalid_plugin_package")
                openai_ext = extensions_obj["com.openai"]
                if (
                    "interface" in openai_ext
                    and openai_ext["interface"] is not None
                    and not isinstance(openai_ext["interface"], dict)
                ):
                    raise AgentError("agent_invalid_plugin_package")

        root_interface = (
            raw_manifest["interface"] if isinstance(raw_manifest.get("interface"), dict) else {}
        )
        interface_from_overlay = False
        if openai_ext is not None:
            openai_interface = (
                openai_ext["interface"] if isinstance(openai_ext.get("interface"), dict) else {}
            )
            interface = openai_interface or root_interface
        elif root_interface:
            interface = root_interface
        elif codex_overlay is not None and isinstance(codex_overlay.get("interface"), dict):
            interface = codex_overlay["interface"]
            interface_from_overlay = True
        else:
            interface = {}

        for iface_key in ("displayName", "shortDescription"):
            if iface_key in interface and interface[iface_key] is not None and not isinstance(interface[iface_key], str):
                raise AgentError("agent_invalid_plugin_package")

        title = str(
            interface.get("displayName")
            or raw_manifest.get("title")
            or raw_manifest.get("name")
            or plugin_id
        ).strip()[:160]
        if interface_from_overlay:
            description = str(
                raw_manifest.get("description")
                or interface.get("shortDescription")
                or ""
            ).strip()[:1000]
        else:
            description = str(
                interface.get("shortDescription")
                or raw_manifest.get("description")
                or ""
            ).strip()[:1000]
        version = str(raw_manifest.get("version") or "1.0.0").strip()[:64]

        ryn = raw_manifest.get("ryn", {})
        if not isinstance(ryn, dict):
            raise AgentError("agent_invalid_plugin_package")
        peer_provider = validate_peer_provider(ryn["peerProvider"]) if "peerProvider" in ryn else None
        data_policy = normalize_policy(ryn.get("dataPolicy"))

        warnings: list[str] = []
        unsupported: list[str] = []

        for key, note in KNOWN_UNSUPPORTED_MANIFEST_KEYS.items():
            if raw_manifest.get(key) or (openai_ext is not None and openai_ext.get(key)):
                unsupported.append(key)
                warnings.append(note)

        if (pkg_root / ".app.json").exists():
            unsupported.append("apps")
            warnings.append("File '.app.json' is ignored; external app connectors are not supported in Ryn.")
        if (pkg_root / "hooks.json").exists() or (pkg_root / "hooks" / "hooks.json").exists():
            unsupported.append("hooks")
            warnings.append("Lifecycle hooks configuration is ignored; Ryn does not run external hooks.")

        for dir_name, (comp_name, warn_msg) in KNOWN_UNSUPPORTED_DIRS.items():
            if (pkg_root / dir_name).is_dir():
                unsupported.append(comp_name)
                warnings.append(warn_msg)

        # Discover skills inside default skills/ directory and/or manifest skills paths
        skills: list[dict[str, Any]] = []
        explicit_skill_paths = _parse_manifest_path_list(raw_manifest.get("skills"))
        pkg_resolved = pkg_root.resolve()
        seen_skill_dirs: set[Path] = set()
        skill_md_locations: list[tuple[Path, str]] = []
        can_import = True

        default_skills_dir = pkg_resolved / "skills"
        if (
            source_format == "claude_plugin" or not explicit_skill_paths
        ) and default_skills_dir.is_dir():
            _collect_skills_from_path(
                default_skills_dir, pkg_resolved, seen_skill_dirs, skill_md_locations
            )

        for raw_skill_path in explicit_skill_paths:
            skills_target = _resolve_manifest_relative_path(pkg_resolved, raw_skill_path)
            if not skills_target.exists():
                warnings.append(
                    f"Declared skills path '{raw_skill_path}' was not found in the plugin package."
                )
                can_import = False
                continue
            matched_count = _collect_skills_from_path(
                skills_target, pkg_resolved, seen_skill_dirs, skill_md_locations
            )
            if matched_count == 0:
                warnings.append(
                    f"Declared skills path '{raw_skill_path}' did not contain any SKILL.md."
                )
                can_import = False

        if (
            not explicit_skill_paths
            and not skill_md_locations
            and (pkg_resolved / "SKILL.md").is_file()
        ):
            skill_md_locations.append((pkg_resolved, ""))

        seen_skill_ids: set[str] = set()
        for s_dir, rel_dir in skill_md_locations:
            s_manifest = compute_directory_manifest(s_dir)
            s_md_text = (s_dir / "SKILL.md").read_text(encoding="utf-8")
            s_parsed = parse_skill_markdown(
                s_md_text,
                fallback_id=s_dir.name or plugin_id,
            )
            from .codex_skills import extract_codex_skill_metadata

            s_codex_meta = extract_codex_skill_metadata(
                s_dir,
                s_md_text,
                fallback_id=s_dir.name or plugin_id,
            )
            orig_sid = s_parsed["id"]
            sid = orig_sid
            if sid in seen_skill_ids:
                candidate = _slugify(f"{plugin_id}-{s_dir.name}-{orig_sid}")
                counter = 2
                while candidate in seen_skill_ids:
                    candidate = _slugify(f"{plugin_id}-{s_dir.name}-{orig_sid}-{counter}")
                    counter += 1
                sid = candidate
                warnings.append(
                    f"Duplicate skill ID '{orig_sid}' in '{rel_dir or s_dir.name}' was remapped to '{sid}'."
                )
            seen_skill_ids.add(sid)
            present_files = {f["path"] for f in s_manifest["files"]}
            for ref in s_parsed["referenced_paths"]:
                if ref not in present_files:
                    warnings.append(f"Skill '{sid}' references missing file '{ref}'.")
            skills.append(
                {
                    "id": sid,
                    "name": s_parsed["name"],
                    "title": s_parsed["title"],
                    "description": s_parsed["description"],
                    "short_description": s_codex_meta.get("short_description") or s_parsed["summary"],
                    "summary": s_parsed["summary"],
                    "version": s_parsed["version"] or version,
                    "content": s_parsed["content"],
                    "files": s_manifest["files"],
                    "manifest_sha256": s_manifest["manifest_sha256"],
                    "plugin_id": plugin_id,
                    "plugin_rel_dir": rel_dir,
                    "source_format": source_format,
                    "scope": "plugin",
                    "interface": s_codex_meta.get("interface") or {},
                    "policy": s_codex_meta.get("policy") or {"allow_implicit_invocation": True},
                    "warnings": [],
                }
            )

        # Discover MCP servers inside canonical mcp.json (portable root) or .mcp.json/mcp.json (legacy)
        mcp_servers: list[dict[str, Any]] = []
        seen_plugin_server_ids: set[str] = set()
        mcp_candidates: list[Path] = []
        raw_mcp_field = raw_manifest.get("mcpServers")
        if raw_mcp_field is not None and not isinstance(raw_mcp_field, dict):
            for raw_mcp_path in _parse_manifest_path_list(raw_mcp_field):
                c_path = _resolve_manifest_relative_path(pkg_resolved, raw_mcp_path)
                if c_path.is_file():
                    if c_path not in mcp_candidates:
                        mcp_candidates.append(c_path)
                else:
                    warnings.append(
                        f"Declared mcpServers path '{raw_mcp_path}' was not found in the plugin package."
                    )
                    can_import = False
        default_mcp_names = ("mcp.json",) if is_portable_root else (".mcp.json", "mcp.json")
        for name in default_mcp_names:
            p = (pkg_resolved / name).resolve()
            if p.is_file() and p not in mcp_candidates:
                mcp_candidates.append(p)

        for mcp_file in mcp_candidates:
            parsed_srvs, _, mcp_warns, mcp_unsup = self._parse_mcp_config_text(
                mcp_file.read_text(encoding="utf-8"),
                filename=mcp_file.name,
                plugin_id=plugin_id,
                package_root=None,
                default_data_policy=data_policy,
            )
            for srv in parsed_srvs:
                if srv["id"] in seen_plugin_server_ids:
                    raise AgentError("agent_import_duplicate_id")
                seen_plugin_server_ids.add(srv["id"])
            mcp_servers.extend(parsed_srvs)
            warnings.extend(mcp_warns)
            unsupported.extend(mcp_unsup)

        if isinstance(raw_mcp_field, dict):
            parsed_srvs, _, mcp_warns, mcp_unsup = self._parse_mcp_config_text(
                json.dumps({"mcpServers": raw_mcp_field}),
                filename="plugin.json",
                plugin_id=plugin_id,
                package_root=None,
                default_data_policy=data_policy,
            )
            for srv in parsed_srvs:
                if srv["id"] in seen_plugin_server_ids:
                    raise AgentError("agent_import_duplicate_id")
                seen_plugin_server_ids.add(srv["id"])
            mcp_servers.extend(parsed_srvs)
            warnings.extend(mcp_warns)
            unsupported.extend(mcp_unsup)

        views = normalize_views(ryn.get("views", []), {srv["import_key"]: srv["id"] for srv in mcp_servers})
        if "sse" in unsupported:
            can_import = False
        if not skills and not mcp_servers and peer_provider is None:
            can_import = False
            if not warnings:
                warnings.append("Plugin package contains no importable skills or MCP servers.")

        plugin_entry = {
            "id": plugin_id,
            "name": str(raw_manifest.get("name") or plugin_id),
            "title": title,
            "description": description,
            "version": version,
            "source_format": source_format,
            "staged_package_dir": str(pkg_root),
            "manifest_sha256": manifest_info["manifest_sha256"],
            "skill_ids": [s["id"] for s in skills],
            "mcp_server_ids": [m["id"] for m in mcp_servers],
            "peer_provider": peer_provider,
            "data_policy": data_policy,
            "views": views,
            "unsupported_components": sorted(set(unsupported)),
            "warnings": warnings,
        }
        return {
            "name": title,
            "version": version,
            "source_format": source_format,
            "plugins": [plugin_entry],
            "skills": skills,
            "mcp_servers": mcp_servers,
            "warnings": warnings,
            "unsupported_components": sorted(set(unsupported)),
            "can_import": can_import,
        }

    @staticmethod
    def _decode_base64(value: str) -> bytes:
        cleaned = value.strip()
        if cleaned.startswith("data:") and "," in cleaned:
            cleaned = cleaned.split(",", 1)[1]
        try:
            return base64.b64decode(cleaned, validate=True)
        except (binascii.Error, ValueError) as exc:
            raise AgentError("agent_invalid_extension_archive") from exc


def preview_extension_import(store: ExtensionStore, payload: dict[str, Any]) -> dict[str, Any]:
    return ExtensionImportManager(store).preview(payload)
