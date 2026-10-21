"""Transactional, sealed persistence for Ryn Agent extensions (Plugins, Skills, MCP servers)."""

from __future__ import annotations

import copy
import hashlib
import json
import os
import re
import shutil
import stat
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from cryptography.exceptions import InvalidTag

from ...atomic_io import atomic_write_json, read_json
from ...file_transactions import file_transaction
from ...services import peer_box
from ..knowledge import SKILLS as BUILTIN_SKILLS
from ..store import AgentError, AgentToolRejected, encoded

EXTENSIONS_VERSION = "ryn.agent.extensions.v1"
EXTENSIONS_CHANNEL = b"rynmesh-agent-extensions-v1"
MAX_PLAINTEXT = 8 * 1024 * 1024
MAX_BYTES = 12 * 1024 * 1024

MAX_SKILL_PACKAGE_FILES = 128
MAX_SKILL_FILE_BYTES = 512 * 1024
MAX_SKILL_TOTAL_BYTES = 4 * 1024 * 1024
MAX_SKILL_PACKAGE_DEPTH = 8
STREAM_CHUNK_BYTES = 64 * 1024

EXTENSION_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")
ENV_KEY_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,127}$")
HEADER_KEY_RE = re.compile(r"^[A-Za-z0-9-]{1,128}$")
MASKED_VALUE = "••••••••"

COMPUTER_USE_PLUGIN_ID = "computer-use"
COMPUTER_USE_SERVER_ID = "computer-use"
COMPUTER_USE_SERVER_INSTANCE_ID = "builtin-computer-use"
COMPUTER_USE_PLUGIN_INSTANCE_ID = "builtin-computer-use-plugin"

COMPUTER_USE_BUILTIN_TOOLS = [
    {
        "name": "computer_observe_windows",
        "title": "Observe Windows desktop windows",
        "description": (
            "List visible top-level windows and foreground window metadata on this computer. "
            "After finding the target hwnd, call computer_read_window directly (works even on background windows without focusing first)."
        ),
        "input_schema": {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "query": {"type": "string", "maxLength": 160},
                "limit": {"type": "integer", "minimum": 1, "maximum": 50},
            },
        },
        "annotations": {
            "readOnlyHint": True,
            "destructiveHint": False,
            "idempotentHint": True,
            "openWorldHint": False,
        },
    },
    {
        "name": "computer_read_window",
        "title": "Read window controls and capture screenshot",
        "description": (
            "Inspect UI Automation controls, text, and screenshot of a specific window (works on background windows; "
            "do NOT call computer_act_window after an action just to verify, as computer_act_window already returns a fresh screenshot)."
        ),
        "input_schema": {
            "type": "object",
            "additionalProperties": False,
            "required": ["hwnd"],
            "properties": {
                "hwnd": {"type": "integer", "minimum": 1},
                "include_screenshot": {"type": "boolean"},
            },
        },
        "annotations": {
            "readOnlyHint": True,
            "destructiveHint": False,
            "idempotentHint": True,
            "openWorldHint": False,
        },
    },
    {
        "name": "computer_act_window",
        "title": "Perform action on a window",
        "description": (
            "Focus, click, type text, press keys, invoke, or set value on a target window. "
            "Every click/type/key action automatically brings the window to the foreground (do NOT call action='focus' first). "
            "Supports combo execution in ONE call: pass action='type' (or 'click') with x, y (or element_id), text='...', and optional key='Enter' "
            "to click the input, type text, and submit in a single step. Returns a fresh post-action screenshot and state."
        ),
        "input_schema": {
            "type": "object",
            "additionalProperties": False,
            "required": ["hwnd", "action"],
            "properties": {
                "hwnd": {"type": "integer", "minimum": 1},
                "action": {
                    "type": "string",
                    "enum": ["focus", "click", "type", "key", "invoke", "set_value", "close"],
                },
                "element_id": {"type": "string", "maxLength": 64},
                "x": {"type": "integer", "minimum": -10000, "maximum": 20000},
                "y": {"type": "integer", "minimum": -10000, "maximum": 20000},
                "text": {"type": "string", "maxLength": 4000},
                "key": {"type": "string", "maxLength": 32},
                "clear": {"type": "boolean"},
            },
        },
        "annotations": {
            "readOnlyHint": False,
            "destructiveHint": False,
            "idempotentHint": False,
            "openWorldHint": False,
        },
    },
]


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _canon_sha256(value: Any) -> str:
    return hashlib.sha256(encoded(value)).hexdigest()


COMPUTER_USE_BUILTIN_SCHEMA_HASHES: dict[str, str] = {
    str(item["name"]): _canon_sha256(item["input_schema"])
    for item in COMPUTER_USE_BUILTIN_TOOLS
}


def _slugify(value: str, fallback: str = "ext") -> str:
    cleaned = re.sub(r"[^A-Za-z0-9_.-]+", "-", (value or "").strip()).strip("-._").lower()
    if not cleaned:
        cleaned = fallback
    if not cleaned[0].isalnum():
        cleaned = f"x-{cleaned}"
    return cleaned[:64]


def compute_model_tool_name(server_id: str, tool_name: str) -> str:
    """Build a deterministic, collision-resistant model tool name <= 64 chars.

    Unconditionally embeds an 8-hex-char SHA-256 digest of the raw (un-slugified)
    server_id and tool_name so distinct servers like 'a-b' and 'a_b' never collide.
    """
    raw_server = str(server_id or "")
    raw_tool = str(tool_name or "")
    digest = hashlib.sha256(f"{raw_server}\x00{raw_tool}".encode("utf-8")).hexdigest()[:8]
    s_slug = re.sub(r"[^A-Za-z0-9]+", "_", raw_server.strip()).strip("_").lower() or "srv"
    t_slug = re.sub(r"[^A-Za-z0-9]+", "_", raw_tool.strip()).strip("_").lower() or "tool"
    return f"mcp__{s_slug[:18]}__{t_slug[:24]}__{digest}"


def _read_bounded_file_bytes(path: Path, *, max_bytes: int) -> bytes:
    """Stat and stream-read a regular file, rejecting symlinks and oversize content before allocation."""
    try:
        st = os.lstat(path)
    except OSError:
        raise AgentError("agent_skill_file_not_found") from None
    if stat.S_ISLNK(st.st_mode):
        raise AgentError("agent_extension_symlink_rejected")
    if not stat.S_ISREG(st.st_mode):
        raise AgentError("agent_skill_file_not_found")
    if st.st_size > max_bytes:
        raise AgentError("agent_skill_file_too_large")
    chunks: list[bytes] = []
    total = 0
    try:
        with path.open("rb") as handle:
            while True:
                chunk = handle.read(min(STREAM_CHUNK_BYTES, max_bytes - total + 1))
                if not chunk:
                    break
                total += len(chunk)
                if total > max_bytes:
                    raise AgentError("agent_skill_file_too_large")
                chunks.append(chunk)
    except AgentError:
        raise
    except OSError:
        raise AgentError("agent_skill_file_not_found") from None
    return b"".join(chunks)


def compute_directory_manifest(
    root: Path,
    *,
    max_files: int = MAX_SKILL_PACKAGE_FILES,
    max_file_bytes: int = MAX_SKILL_FILE_BYTES,
    max_total_bytes: int = MAX_SKILL_TOTAL_BYTES,
    max_depth: int = MAX_SKILL_PACKAGE_DEPTH,
) -> dict[str, Any]:
    """Compute a deterministic SHA-256 manifest over a package directory with strict bounds."""
    try:
        root_st = os.lstat(root)
    except OSError:
        raise AgentError("agent_skill_package_missing") from None
    if stat.S_ISLNK(root_st.st_mode) or not stat.S_ISDIR(root_st.st_mode):
        raise AgentError("agent_extension_symlink_rejected")

    files: list[dict[str, Any]] = []
    total_bytes = 0

    def _walk(current: Path, rel_parts: tuple[str, ...]) -> None:
        nonlocal total_bytes
        if len(rel_parts) > max_depth:
            raise AgentError("agent_skill_package_limit")
        try:
            entries = sorted(os.scandir(current), key=lambda e: e.name)
        except OSError:
            raise AgentError("agent_skill_package_missing") from None
        for entry in entries:
            entry_path = current / entry.name
            try:
                entry_st = os.lstat(entry_path)
            except OSError:
                raise AgentError("agent_skill_package_missing") from None
            if stat.S_ISLNK(entry_st.st_mode):
                raise AgentError("agent_extension_symlink_rejected")
            next_parts = rel_parts + (entry.name,)
            if stat.S_ISDIR(entry_st.st_mode):
                _walk(entry_path, next_parts)
            elif stat.S_ISREG(entry_st.st_mode):
                if len(files) + 1 > max_files:
                    raise AgentError("agent_skill_package_limit")
                if entry_st.st_size > max_file_bytes:
                    raise AgentError("agent_skill_file_too_large")
                if total_bytes + entry_st.st_size > max_total_bytes:
                    raise AgentError("agent_skill_package_limit")
                data = _read_bounded_file_bytes(entry_path, max_bytes=max_file_bytes)
                total_bytes += len(data)
                if total_bytes > max_total_bytes:
                    raise AgentError("agent_skill_package_limit")
                files.append(
                    {
                        "path": "/".join(next_parts),
                        "size": len(data),
                        "sha256": hashlib.sha256(data).hexdigest(),
                    }
                )

    _walk(root, ())
    files.sort(key=lambda item: item["path"])
    return {
        "files": files,
        "total_bytes": total_bytes,
        "manifest_sha256": _canon_sha256(files),
    }


def _mcp_config_fingerprint(server: dict[str, Any]) -> str:
    """Digest of transport/endpoint/command/auth configuration to detect stale probes."""
    relevant = {
        "transport": server.get("transport"),
        "command": server.get("command"),
        "args": server.get("args") or [],
        "cwd": server.get("cwd"),
        "secret_env": server.get("secret_env") or {},
        "env_vars": server.get("env_vars") or [],
        "url": server.get("url"),
        "bearer_token_env": server.get("bearer_token_env"),
        "secret_headers": server.get("secret_headers") or {},
        "env_http_headers": server.get("env_http_headers") or {},
        "oauth_client_registration": server.get("oauth_client_registration") or "auto",
        "computer_access": bool(server.get("computer_access", False)),
    }
    return _canon_sha256(relevant)


def _alloc_generation(state: dict[str, Any]) -> int:
    """Allocate a strictly monotonic generation/revision across the entire store (prevents ABA)."""
    nxt = max(int(state.get("next_generation", 2)), int(state.get("revision", 1)) + 1)
    state["next_generation"] = nxt + 1
    state["revision"] = nxt
    return nxt


class ExtensionStore:
    """Thread-safe, sealed local configuration store for Ryn Agent extensions."""

    def __init__(self, root: str | Path, messaging_key: Any = None) -> None:
        base = Path(root)
        self.root = base if base.name == "agent" else base / "agent"
        self.root.mkdir(parents=True, exist_ok=True)
        self.path = self.root / "extensions.json"
        self.lock = self.root / ".extensions.lock"
        self.pkgs_dir = self.root / "extensions_pkgs"
        self.pkgs_dir.mkdir(parents=True, exist_ok=True)
        self.key = (
            messaging_key
            if messaging_key is not None
            else peer_box.load_or_create_messaging_key(self.root / "messaging.key")
        )
        self.pub = peer_box.public_key_b64(self.key)
        self._cached_stamp: tuple[int, int] | None = None
        self._cached_state: dict[str, Any] | None = None
        with file_transaction(self.lock):
            if not self.path.exists():
                self._write(self._default_state())

    def _default_computer_use_tools(self) -> list[dict[str, Any]]:
        tools: list[dict[str, Any]] = []
        for item in COMPUTER_USE_BUILTIN_TOOLS:
            schema = item["input_schema"]
            tools.append(
                {
                    "name": item["name"],
                    "title": item["title"],
                    "description": item["description"],
                    "input_schema": copy.deepcopy(schema),
                    "schema_sha256": _canon_sha256(schema),
                    "annotations": copy.deepcopy(item["annotations"]),
                    "model_tool_name": compute_model_tool_name(
                        COMPUTER_USE_SERVER_ID, item["name"]
                    ),
                }
            )
        return tools

    @staticmethod
    def _computer_use_default_args() -> list[str]:
        if getattr(sys, "frozen", False):
            return ["--ryn-computer-use-worker"]
        return ["-m", "rynmesh.agent_runtime.extensions.computer_use_server"]

    def _default_state(self) -> dict[str, Any]:
        now = _now()
        cu_server = {
            "id": COMPUTER_USE_SERVER_ID,
            "instance_id": "builtin-computer-use",
            "name": "Computer Use",
            "description": (
                "First-party Windows UI Automation and screenshot MCP provider integrated "
                "with Ryn desktop_access permissions and approval policies."
            ),
            "transport": "stdio",
            "builtin": True,
            "deletable": False,
            "plugin_id": COMPUTER_USE_PLUGIN_ID,
            "computer_access": True,
            "enabled": True,
            "command": sys.executable,
            "args": self._computer_use_default_args(),
            "cwd": None,
            "secret_env": {},
            "url": None,
            "bearer_token_env": None,
            "secret_headers": {},
            "startup_timeout_ms": 10000,
            "tool_timeout_ms": 30000,
            "enabled_tools": None,
            "disabled_tools": [],
            "read_only_tools": ["computer_observe_windows", "computer_read_window"],
            "discovered_tools": self._default_computer_use_tools(),
            "status": "disabled",
            "status_message": "",
            "last_tested_at": None,
            "config_generation": 1,
            "revision": 1,
            "updated_at": now,
        }
        cu_server["config_sha256"] = _mcp_config_fingerprint(cu_server)
        return {
            "version": EXTENSIONS_VERSION,
            "revision": 1,
            "next_generation": 2,
            "updated_at": now,
            "builtin_skills_enabled": dict.fromkeys(BUILTIN_SKILLS, True),
            "builtin_skills_revisions": dict.fromkeys(BUILTIN_SKILLS, 1),
            "plugins": {
                COMPUTER_USE_PLUGIN_ID: {
                    "id": COMPUTER_USE_PLUGIN_ID,
                    "instance_id": "builtin-computer-use-plugin",
                    "name": "Computer Use",
                    "title": "Computer Use",
                    "description": (
                        "Inspect Windows desktop windows, capture window screenshots for visual grounding, "
                        "and operate local desktop window controls under desktop_access policy."
                    ),
                    "version": "1.0.0",
                    "kind": "builtin",
                    "source_format": "builtin",
                    "builtin": True,
                    "deletable": False,
                    "enabled": False,
                    "computer_access": True,
                    "skill_ids": [],
                    "mcp_server_ids": [COMPUTER_USE_SERVER_ID],
                    "unsupported_components": [],
                    "warnings": [],
                    "revision": 1,
                    "updated_at": now,
                }
            },
            "skills": {},
            "mcp_servers": {
                COMPUTER_USE_SERVER_ID: cu_server,
            },
        }

    def _read(self) -> dict[str, Any]:
        if not self.path.exists():
            self._cached_stamp = None
            self._cached_state = None
            state = self._default_state()
            self._write(state)
            return state
        try:
            st = self.path.stat()
            stamp = (st.st_mtime_ns, st.st_size)
            if self._cached_stamp == stamp and self._cached_state is not None:
                return copy.deepcopy(self._cached_state)
            envelope = read_json(self.path, max_bytes=MAX_BYTES)
            if not isinstance(envelope, dict) or envelope.get("version") != EXTENSIONS_VERSION:
                raise AgentError("agent_state_unreadable")
            plaintext = peer_box.open_sealed(
                self.key,
                self.pub,
                envelope["nonce"],
                envelope["ciphertext"],
                info=EXTENSIONS_CHANNEL,
            )
            if len(plaintext) > MAX_PLAINTEXT:
                raise AgentError("agent_storage_limit")
            state = json.loads(plaintext)
            if not isinstance(state, dict):
                raise AgentError("agent_state_unreadable")
        except AgentError:
            raise
        except (OSError, ValueError, KeyError, TypeError, InvalidTag):
            raise AgentError("agent_state_unreadable") from None
        normalized = self._normalize_state(state)
        self._cached_stamp = stamp
        self._cached_state = copy.deepcopy(normalized)
        return normalized

    def _normalize_state(self, state: dict[str, Any]) -> dict[str, Any]:
        defaults = self._default_state()
        state["version"] = EXTENSIONS_VERSION
        state.setdefault("revision", 1)
        state.setdefault("next_generation", int(state["revision"]) + 1)
        state.setdefault("updated_at", _now())
        builtin_enabled = state.setdefault("builtin_skills_enabled", {})
        builtin_revs = state.setdefault("builtin_skills_revisions", {})
        for skill_id in BUILTIN_SKILLS:
            builtin_enabled.setdefault(skill_id, True)
            builtin_revs.setdefault(skill_id, 1)
        plugins = state.setdefault("plugins", {})
        if COMPUTER_USE_PLUGIN_ID not in plugins:
            plugins[COMPUTER_USE_PLUGIN_ID] = defaults["plugins"][COMPUTER_USE_PLUGIN_ID]
        else:
            plugins[COMPUTER_USE_PLUGIN_ID].setdefault("instance_id", "builtin-computer-use-plugin")
            plugins[COMPUTER_USE_PLUGIN_ID]["description"] = defaults["plugins"][
                COMPUTER_USE_PLUGIN_ID
            ]["description"]
        state.setdefault("skills", {})
        for skill in state["skills"].values():
            skill.setdefault("instance_id", uuid.uuid4().hex)
            skill.setdefault("install_generation", 1)
        servers = state.setdefault("mcp_servers", {})
        if COMPUTER_USE_SERVER_ID not in servers:
            servers[COMPUTER_USE_SERVER_ID] = defaults["mcp_servers"][COMPUTER_USE_SERVER_ID]
        else:
            cu_srv = servers[COMPUTER_USE_SERVER_ID]
            cu_srv.setdefault("instance_id", "builtin-computer-use")
            cu_srv["command"] = sys.executable
            cu_srv["args"] = self._computer_use_default_args()
            if not cu_srv.get("discovered_tools"):
                cu_srv["discovered_tools"] = self._default_computer_use_tools()
            else:
                default_tools_by_name = {item["name"]: item for item in COMPUTER_USE_BUILTIN_TOOLS}
                for tool_entry in cu_srv.get("discovered_tools") or []:
                    if isinstance(tool_entry, dict) and tool_entry.get("name") in default_tools_by_name:
                        src_tool = default_tools_by_name[tool_entry["name"]]
                        tool_entry["title"] = src_tool["title"]
                        tool_entry["description"] = src_tool["description"]
            cu_srv.setdefault("config_generation", 1)
            cu_srv["config_sha256"] = _mcp_config_fingerprint(cu_srv)
        for srv in servers.values():
            srv.setdefault("instance_id", uuid.uuid4().hex)
            srv.setdefault("config_generation", 1)
            srv.setdefault("env_vars", [])
            srv.setdefault("env_http_headers", {})
            srv.setdefault("oauth_client_registration", "auto")
        return state

    def _write(self, state: dict[str, Any]) -> dict[str, Any]:
        state["version"] = EXTENSIONS_VERSION
        raw = encoded(state)
        if len(raw) > MAX_PLAINTEXT:
            raise AgentError("agent_storage_limit")
        nonce, ciphertext = peer_box.seal(self.key, self.pub, raw, info=EXTENSIONS_CHANNEL)
        atomic_write_json(
            self.path,
            {"version": EXTENSIONS_VERSION, "nonce": nonce, "ciphertext": ciphertext},
            max_bytes=MAX_BYTES,
        )
        try:
            st = self.path.stat()
            self._cached_stamp = (st.st_mtime_ns, st.st_size)
            self._cached_state = copy.deepcopy(state)
        except OSError:
            self._cached_stamp = None
            self._cached_state = None
        return state

    def revision(self) -> int:
        with file_transaction(self.lock):
            return int(self._read().get("revision", 1))

    # ------------------------------------------------------------------
    # Effective Parent + Child Enablement Checks
    # ------------------------------------------------------------------

    def _is_parent_plugin_enabled(self, plugin_id: str | None, state: dict[str, Any]) -> bool:
        if not plugin_id:
            return True
        parent = state.get("plugins", {}).get(plugin_id)
        return bool(parent and parent.get("enabled", False))

    def _is_skill_effectively_enabled(self, skill: dict[str, Any], state: dict[str, Any]) -> bool:
        if not skill.get("enabled", False):
            return False
        return self._is_parent_plugin_enabled(skill.get("plugin_id"), state)

    def _is_server_effectively_enabled(self, server: dict[str, Any], state: dict[str, Any]) -> bool:
        if not server.get("enabled", False):
            return False
        return self._is_parent_plugin_enabled(server.get("plugin_id"), state)

    def _check_expected_revision(
        self,
        expected_revision: Any,
        actual_revision: int,
    ) -> None:
        if expected_revision is None:
            return
        if type(expected_revision) is not int or expected_revision < 1:
            raise AgentError("agent_invalid_revision")
        if expected_revision != int(actual_revision):
            raise AgentError("agent_extensions_revision_conflict")

    # ------------------------------------------------------------------
    # Public View Formatting (Masking secrets & enriching metadata)
    # ------------------------------------------------------------------

    def _public_plugin(
        self,
        plugin: dict[str, Any],
        state: dict[str, Any],
        *,
        desktop_access: bool = False,
    ) -> dict[str, Any]:
        is_computer_use = plugin["id"] == COMPUTER_USE_PLUGIN_ID
        win_supported = os.name == "nt"
        bundled_skills = [
            self._public_imported_skill(state["skills"][sid], state)
            for sid in plugin.get("skill_ids", [])
            if sid in state["skills"]
        ]
        bundled_servers = [
            self._public_mcp_server(state["mcp_servers"][mid], state, desktop_access=desktop_access)
            for mid in plugin.get("mcp_server_ids", [])
            if mid in state["mcp_servers"]
        ]
        status = "enabled" if plugin.get("enabled") else "disabled"
        status_reason = ""
        if is_computer_use:
            if not win_supported:
                status = "unavailable"
                status_reason = "windows_only"
            elif plugin.get("enabled") and not desktop_access:
                status = "needs_permission"
                status_reason = "desktop_access_disabled"
        out: dict[str, Any] = {
            "id": plugin["id"],
            "instance_id": plugin.get("instance_id", ""),
            "name": plugin.get("name") or plugin["id"],
            "title": plugin.get("title") or plugin.get("name") or plugin["id"],
            "description": plugin.get("description", ""),
            "version": str(plugin.get("version", "1.0.0")),
            "kind": plugin.get("kind", "imported"),
            "source_format": plugin.get("source_format", "ryn_plugin"),
            "builtin": bool(plugin.get("builtin", False)),
            "deletable": bool(plugin.get("deletable", not plugin.get("builtin", False))),
            "enabled": bool(plugin.get("enabled", False)),
            "component_enabled": bool(plugin.get("enabled", False)),
            "parent_enabled": True,
            "status": status,
            "status_reason": status_reason,
            "computer_access": bool(plugin.get("computer_access", False)),
            "skill_ids": list(plugin.get("skill_ids", [])),
            "mcp_server_ids": list(plugin.get("mcp_server_ids", [])),
            "skills_count": len(bundled_skills),
            "mcp_servers_count": len(bundled_servers),
            "skills": bundled_skills,
            "mcp_servers": bundled_servers,
            "unsupported_components": list(plugin.get("unsupported_components", [])),
            "warnings": list(plugin.get("warnings", [])),
            "revision": int(plugin.get("revision", 1)),
            "updated_at": plugin.get("updated_at"),
        }
        if is_computer_use:
            out["capabilities"] = {
                "platform": sys.platform,
                "windows_supported": win_supported,
                "desktop_access_enabled": bool(desktop_access),
                "observable": [
                    "Visible top-level Windows desktop windows and foreground window state",
                    "Window UI Automation control tree, bounds, labels, and text values",
                    "Window PNG screenshots saved to the task workspace for visual inspection",
                ],
                "controllable": [
                    "Window focus, coordinate or element click, text entry, and key presses",
                    "UI Automation InvokePattern and ValuePattern actions on target controls",
                ],
                "permission_note": (
                    "Enabling this plugin authorizes its three native Windows desktop MCP tools "
                    "(computer_observe_windows, computer_read_window, computer_act_window) when "
                    "'Allow computer and browser operations' (desktop_access) is enabled in Agent "
                    "settings; write actions follow the task's confirmation mode. Browser automation "
                    "remains available through Ryn's separate built-in browser tools under desktop_access."
                ),
            }
        return out

    def _public_builtin_skill(
        self,
        skill: dict[str, Any],
        enabled: bool,
        revision: int = 1,
    ) -> dict[str, Any]:
        content = str(skill.get("content", ""))
        description = str(skill.get("description", ""))
        short_description = str(skill.get("summary") or description)
        return {
            "id": skill["id"],
            "name": skill["id"],
            "title": skill["title"],
            "description": description,
            "short_description": short_description,
            "summary": description,
            "version": str(skill["version"]),
            "content": content,
            "builtin": True,
            "deletable": False,
            "enabled": bool(enabled),
            "component_enabled": bool(enabled),
            "parent_enabled": True,
            "source_format": "builtin",
            "scope": "system",
            "path": f"builtin://{skill['id']}/SKILL.md",
            "interface": {
                "display_name": skill["title"],
                "short_description": short_description,
                "icon_small": None,
                "icon_large": None,
                "brand_color": None,
                "default_prompt": f"Use ${skill['id']} to help with this task.",
            },
            "policy": {"allow_implicit_invocation": True},
            "plugin_id": None,
            "files": [{"path": "SKILL.md", "size": len(content.encode("utf-8"))}],
            "warnings": [],
            "revision": int(revision),
        }

    def _public_imported_skill(self, skill: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
        parent_enabled = self._is_parent_plugin_enabled(skill.get("plugin_id"), state)
        effective_enabled = self._is_skill_effectively_enabled(skill, state)
        description = str(skill.get("description") or skill.get("summary") or "")
        raw_iface = skill.get("interface") if isinstance(skill.get("interface"), dict) else {}
        short_description = str(
            skill.get("short_description")
            or raw_iface.get("short_description")
            or skill.get("summary")
            or description
        )
        title = skill.get("title") or skill.get("name") or skill["id"]
        scope = str(skill.get("scope") or ("plugin" if skill.get("plugin_id") else "user"))
        pkg_dir = skill.get("package_dir")
        skill_path = str(
            skill.get("skill_md_path")
            or (str(Path(pkg_dir) / "SKILL.md") if pkg_dir else "")
        )
        raw_policy = skill.get("policy") if isinstance(skill.get("policy"), dict) else {}
        return {
            "id": skill["id"],
            "instance_id": skill.get("instance_id", ""),
            "name": skill.get("name") or skill["id"],
            "title": title,
            "description": description,
            "short_description": short_description,
            "summary": str(skill.get("summary") or description),
            "version": str(skill.get("version", "1")),
            "install_generation": int(skill.get("install_generation", 1)),
            "content": str(skill.get("content", "")),
            "manifest_sha256": skill.get("manifest_sha256"),
            "builtin": False,
            "deletable": bool(skill.get("deletable", scope != "system")),
            "enabled": effective_enabled,
            "component_enabled": bool(skill.get("enabled", False)),
            "parent_enabled": parent_enabled,
            "source_format": skill.get("source_format", "skill_md"),
            "scope": scope,
            "path": skill_path,
            "package_dir": str(pkg_dir or ""),
            "interface": {
                "display_name": raw_iface.get("display_name") or title,
                "short_description": raw_iface.get("short_description") or short_description,
                "icon_small": raw_iface.get("icon_small"),
                "icon_large": raw_iface.get("icon_large"),
                "brand_color": raw_iface.get("brand_color"),
                "default_prompt": raw_iface.get("default_prompt"),
            },
            "policy": {
                "allow_implicit_invocation": bool(raw_policy.get("allow_implicit_invocation", True)),
            },
            "plugin_id": skill.get("plugin_id"),
            "files": [
                {"path": f["path"], "size": f.get("size", 0)}
                for f in skill.get("files", [])
                if isinstance(f, dict) and "path" in f
            ],
            "warnings": list(skill.get("warnings", [])),
            "revision": int(skill.get("revision", 1)),
            "updated_at": skill.get("updated_at"),
        }

    def _public_mcp_server(
        self,
        server: dict[str, Any],
        state: dict[str, Any],
        *,
        desktop_access: bool = False,
    ) -> dict[str, Any]:
        secret_env = server.get("secret_env") or {}
        secret_headers = server.get("secret_headers") or {}
        masked_env = {
            k: {"has_value": True, "masked": MASKED_VALUE}
            for k in sorted(secret_env.keys())
            if secret_env.get(k)
        }
        masked_headers = {
            k: {"has_value": True, "masked": MASKED_VALUE}
            for k in sorted(secret_headers.keys())
            if secret_headers.get(k)
        }
        parent_enabled = self._is_parent_plugin_enabled(server.get("plugin_id"), state)
        effective_enabled = self._is_server_effectively_enabled(server, state)

        enabled_tools = server.get("enabled_tools")
        disabled_tools = set(server.get("disabled_tools") or [])
        read_only_tools = set(server.get("read_only_tools") or [])
        tools_view: list[dict[str, Any]] = []
        for tool in server.get("discovered_tools") or []:
            t_name = str(tool.get("name", ""))
            if not t_name:
                continue
            comp_enabled = (
                (enabled_tools is None or t_name in enabled_tools)
                and t_name not in disabled_tools
            )
            t_enabled = effective_enabled and comp_enabled
            annotations = tool.get("annotations") if isinstance(tool.get("annotations"), dict) else {}
            owner_read_only = t_name in read_only_tools
            model_name = compute_model_tool_name(server["id"], t_name)
            tools_view.append(
                {
                    "name": t_name,
                    "model_tool_name": model_name,
                    "title": tool.get("title") or t_name,
                    "description": tool.get("description") or "",
                    "enabled": bool(t_enabled),
                    "component_enabled": bool(comp_enabled),
                    "owner_read_only": bool(owner_read_only),
                    "effective_effect": "read" if owner_read_only else "write",
                    "effective_risk": "low" if owner_read_only else "high",
                    "annotations": {
                        "readOnlyHint": bool(annotations.get("readOnlyHint", False)),
                        "destructiveHint": bool(annotations.get("destructiveHint", False)),
                        "idempotentHint": bool(annotations.get("idempotentHint", False)),
                        "openWorldHint": bool(annotations.get("openWorldHint", False)),
                    },
                    "schema_sha256": tool.get("schema_sha256")
                    or _canon_sha256(tool.get("input_schema") or {}),
                    "input_schema": tool.get("input_schema") or {"type": "object"},
                }
            )
        bearer_env = server.get("bearer_token_env")
        bearer_env_present = bool(bearer_env and os.environ.get(bearer_env))
        env_vars = [str(v) for v in (server.get("env_vars") or []) if str(v).strip()]
        env_http_headers = {
            str(k): str(v)
            for k, v in (server.get("env_http_headers") or {}).items()
            if str(k).strip() and str(v).strip()
        }
        oauth_client_registration = str(server.get("oauth_client_registration") or "auto").lower()
        if oauth_client_registration not in ("auto", "cimd", "dcr"):
            oauth_client_registration = "auto"
        auth_from_env_headers = bool(
            env_http_headers.get("Authorization")
            and os.environ.get(env_http_headers["Authorization"])
        )
        status = server.get("status") or ("ready" if effective_enabled else "disabled")
        status_message = server.get("status_message") or ""
        if not effective_enabled:
            status = "disabled"
            if not parent_enabled and server.get("enabled"):
                status_message = "Parent plugin is disabled."
        elif (
            bearer_env
            and not bearer_env_present
            and not secret_headers.get("Authorization")
            and not auth_from_env_headers
        ):
            status = "NeedsAuth"
            status_message = f"Environment variable {bearer_env} is not set."
        elif server.get("computer_access") and not desktop_access:
            status = "NeedsPermission"
            status_message = "Requires desktop_access in Agent settings."
        return {
            "id": server["id"],
            "instance_id": server.get("instance_id", ""),
            "name": server.get("name") or server["id"],
            "title": server.get("name") or server["id"],
            "description": server.get("description") or "",
            "transport": server.get("transport", "stdio"),
            "builtin": bool(server.get("builtin", False)),
            "deletable": bool(server.get("deletable", not server.get("builtin", False))),
            "plugin_id": server.get("plugin_id"),
            "computer_access": bool(server.get("computer_access", False)),
            "enabled": effective_enabled,
            "component_enabled": bool(server.get("enabled", False)),
            "parent_enabled": parent_enabled,
            "command": server.get("command") or "",
            "args": list(server.get("args") or []),
            "cwd": server.get("cwd") or "",
            "env_keys": sorted(masked_env.keys()),
            "masked_env": masked_env,
            "env_vars": env_vars,
            "url": server.get("url") or "",
            "bearer_token_env": bearer_env or "",
            "bearer_token_env_present": bearer_env_present,
            "header_keys": sorted(masked_headers.keys()),
            "masked_headers": masked_headers,
            "env_http_headers": env_http_headers,
            "oauth_client_registration": oauth_client_registration,
            "has_secrets": bool(masked_env or masked_headers),
            "startup_timeout_ms": int(server.get("startup_timeout_ms", 10000)),
            "tool_timeout_ms": int(server.get("tool_timeout_ms", 30000)),
            "enabled_tools": list(enabled_tools) if isinstance(enabled_tools, list) else None,
            "disabled_tools": sorted(disabled_tools),
            "read_only_tools": sorted(read_only_tools),
            "tools": tools_view,
            "tools_count": len(tools_view),
            "enabled_tools_count": sum(1 for item in tools_view if item["enabled"]),
            "status": status,
            "status_message": status_message,
            "last_tested_at": server.get("last_tested_at"),
            "config_generation": int(server.get("config_generation", 1)),
            "revision": int(server.get("revision", 1)),
            "updated_at": server.get("updated_at"),
        }

    def overview(self, *, desktop_access: bool = False) -> dict[str, Any]:
        with file_transaction(self.lock):
            state = self._read()
            plugins = [
                self._public_plugin(plugin, state, desktop_access=desktop_access)
                for plugin in state["plugins"].values()
            ]
            plugins.sort(key=lambda item: (not item["builtin"], item["title"].lower(), item["id"]))

            builtin_enabled = state.get("builtin_skills_enabled", {})
            builtin_revs = state.get("builtin_skills_revisions", {})
            skills: list[dict[str, Any]] = [
                self._public_builtin_skill(
                    skill,
                    builtin_enabled.get(skill_id, True),
                    int(builtin_revs.get(skill_id, 1)),
                )
                for skill_id, skill in BUILTIN_SKILLS.items()
            ]
            for skill in state["skills"].values():
                skills.append(self._public_imported_skill(skill, state))
            skills.sort(key=lambda item: (not item["builtin"], item["title"].lower(), item["id"]))

            servers = [
                self._public_mcp_server(srv, state, desktop_access=desktop_access)
                for srv in state["mcp_servers"].values()
            ]
            servers.sort(key=lambda item: (not item["builtin"], item["name"].lower(), item["id"]))

            return {
                "revision": int(state.get("revision", 1)),
                "updated_at": state.get("updated_at"),
                "desktop_access": bool(desktop_access),
                "windows_supported": os.name == "nt",
                "plugins": plugins,
                "skills": skills,
                "mcp_servers": servers,
            }

    # ------------------------------------------------------------------
    # Runtime Queries (Skills, Active MCP Servers, Invocation Boundaries)
    # ------------------------------------------------------------------

    def enabled_skills_catalog(self) -> list[dict[str, Any]]:
        """Return enabled built-in and imported skills matching knowledge.catalog() shape."""
        with file_transaction(self.lock):
            state = self._read()
            builtin_enabled = state.get("builtin_skills_enabled", {})
            out: list[dict[str, Any]] = []
            for skill_id, skill in BUILTIN_SKILLS.items():
                if builtin_enabled.get(skill_id, True):
                    out.append({key: value for key, value in skill.items() if key != "content"})
            for skill in state["skills"].values():
                if self._is_skill_effectively_enabled(skill, state):
                    out.append(
                        {
                            "id": skill["id"],
                            "version": str(skill.get("version", "1")),
                            "title": skill.get("title") or skill.get("name") or skill["id"],
                            "description": str(skill.get("description") or skill.get("summary") or ""),
                        }
                    )
            return out

    def select_skills(self, skill_ids: list[str] | None) -> list[dict[str, Any]]:
        """Select and snapshot enabled built-in or imported skills for a new task."""
        if skill_ids is None:
            return []
        if (
            not isinstance(skill_ids, list)
            or any(not isinstance(item, str) or not item for item in skill_ids)
            or len(set(skill_ids)) != len(skill_ids)
        ):
            raise AgentError("agent_invalid_skills")
        with file_transaction(self.lock):
            state = self._read()
            builtin_enabled = state.get("builtin_skills_enabled", {})
            max_allowed = len(BUILTIN_SKILLS) + len(state["skills"])
            if len(skill_ids) > max(max_allowed, 1):
                raise AgentError("agent_invalid_skills")
            selected: list[dict[str, Any]] = []
            for item in skill_ids:
                if item in BUILTIN_SKILLS:
                    if not builtin_enabled.get(item, True):
                        raise AgentError("agent_invalid_skills")
                    selected.append(copy.deepcopy(BUILTIN_SKILLS[item]))
                    continue
                skill = state["skills"].get(item)
                if not skill or not self._is_skill_effectively_enabled(skill, state):
                    raise AgentError("agent_invalid_skills")
                pkg_dir_str = skill.get("package_dir")
                if pkg_dir_str:
                    pkg_dir = Path(pkg_dir_str).resolve()
                    manifest = compute_directory_manifest(pkg_dir)
                    if skill.get("manifest_sha256") and manifest["manifest_sha256"] != skill["manifest_sha256"]:
                        raise AgentError("agent_skill_integrity_failed")
                snapshot: dict[str, Any] = {
                    "id": skill["id"],
                    "version": str(skill.get("version", "1")),
                    "install_generation": int(skill.get("install_generation", 1)),
                    "title": skill.get("title") or skill.get("name") or skill["id"],
                    "description": str(skill.get("description") or skill.get("summary") or ""),
                    "content": str(skill.get("content", "")),
                }
                if skill.get("manifest_sha256"):
                    snapshot["manifest_sha256"] = skill["manifest_sha256"]
                if skill.get("files"):
                    snapshot["files"] = [
                        f["path"] for f in skill["files"] if isinstance(f, dict) and "path" in f
                    ]
                selected.append(snapshot)
            return selected

    def get_skill_for_runtime(self, skill_id: str) -> dict[str, Any] | None:
        """Return an effectively enabled skill for runtime discovery/invocation, or None."""
        with file_transaction(self.lock):
            state = self._read()
            builtin_enabled = state.get("builtin_skills_enabled", {})
            if skill_id in BUILTIN_SKILLS:
                if not builtin_enabled.get(skill_id, True):
                    return None
                return copy.deepcopy(BUILTIN_SKILLS[skill_id])
            imported = state["skills"].get(skill_id)
            if not imported or not self._is_skill_effectively_enabled(imported, state):
                return None
            return copy.deepcopy(imported)

    def read_skill_file(
        self,
        skill_id: str,
        relative_path: str = "SKILL.md",
        *,
        expected_version: str | int | None = None,
        expected_install_generation: int | None = None,
        expected_manifest_sha256: str | None = None,
        snapshot: dict[str, Any] | None = None,
        max_chars: int = 24000,
        allow_disabled: bool = False,
    ) -> dict[str, Any]:
        """Read SKILL.md or a referenced file inside an enabled skill package.

        Enforces parent+child enablement, version/generation/manifest snapshot binding,
        directory manifest integrity, and stat/streamed byte bounds before allocation.
        """
        if isinstance(snapshot, dict):
            if expected_version is None and snapshot.get("version") is not None:
                expected_version = snapshot.get("version")
            if expected_install_generation is None and snapshot.get("install_generation") is not None:
                expected_install_generation = snapshot.get("install_generation")
            if expected_manifest_sha256 is None and snapshot.get("manifest_sha256") is not None:
                expected_manifest_sha256 = snapshot.get("manifest_sha256")

        with file_transaction(self.lock):
            state = self._read()
            builtin_enabled = state.get("builtin_skills_enabled", {})
            if skill_id in BUILTIN_SKILLS:
                if not allow_disabled and not builtin_enabled.get(skill_id, True):
                    raise AgentError("agent_skill_disabled")
                b_skill = BUILTIN_SKILLS[skill_id]
                if expected_version is not None and str(expected_version) != str(b_skill["version"]):
                    raise AgentError("agent_skill_version_mismatch")
                norm_rel = (relative_path or "SKILL.md").strip().replace("\\", "/").lstrip("/")
                if norm_rel != "SKILL.md":
                    raise AgentError("agent_skill_file_not_found")
                content = str(b_skill["content"])
                return {
                    "skill_id": skill_id,
                    "version": str(b_skill["version"]),
                    "path": "SKILL.md",
                    "content": content[:max_chars],
                    "truncated": len(content) > max_chars,
                    "files": ["SKILL.md"],
                }

            skill = state["skills"].get(skill_id)
            if not skill:
                raise AgentError("agent_skill_not_found")
            if not allow_disabled and not self._is_skill_effectively_enabled(skill, state):
                raise AgentError("agent_skill_disabled")
            if expected_version is not None and str(expected_version) != str(skill.get("version", "1")):
                raise AgentError("agent_skill_version_mismatch")
            if (
                expected_install_generation is not None
                and int(expected_install_generation) != int(skill.get("install_generation", 1))
            ):
                raise AgentError("agent_skill_version_mismatch")
            if (
                expected_manifest_sha256 is not None
                and expected_manifest_sha256 != skill.get("manifest_sha256")
            ):
                raise AgentError("agent_skill_version_mismatch")

            pkg_dir_str = skill.get("package_dir")
            if not pkg_dir_str:
                norm_rel = (relative_path or "SKILL.md").strip().replace("\\", "/").lstrip("/")
                if norm_rel != "SKILL.md":
                    raise AgentError("agent_skill_file_not_found")
                text = str(skill.get("content", ""))
                if len(text.encode("utf-8")) > MAX_SKILL_FILE_BYTES:
                    raise AgentError("agent_skill_file_too_large")
                return {
                    "skill_id": skill_id,
                    "version": str(skill.get("version", "1")),
                    "install_generation": int(skill.get("install_generation", 1)),
                    "manifest_sha256": skill.get("manifest_sha256"),
                    "path": "SKILL.md",
                    "content": text[:max_chars],
                    "truncated": len(text) > max_chars,
                    "files": ["SKILL.md"],
                }

            pkg_dir = Path(pkg_dir_str).resolve()
            norm_rel = (relative_path or "SKILL.md").strip().replace("\\", "/")
            if not norm_rel or norm_rel.startswith("/") or ".." in norm_rel.split("/"):
                raise AgentError("agent_skill_invalid_path")
            rel_parts = [part for part in norm_rel.split("/") if part and part != "."]
            if not rel_parts or len(rel_parts) > MAX_SKILL_PACKAGE_DEPTH:
                raise AgentError("agent_skill_invalid_path")

            cursor = pkg_dir
            for part in rel_parts:
                cursor = cursor / part
                try:
                    st = os.lstat(cursor)
                except OSError:
                    raise AgentError("agent_skill_file_not_found") from None
                if stat.S_ISLNK(st.st_mode):
                    raise AgentError("agent_extension_symlink_rejected")
            target = cursor.resolve()
            if target == pkg_dir or pkg_dir not in target.parents:
                raise AgentError("agent_skill_invalid_path")

            raw = _read_bounded_file_bytes(target, max_bytes=MAX_SKILL_FILE_BYTES)
            manifest = compute_directory_manifest(pkg_dir)
            if skill.get("manifest_sha256") and manifest["manifest_sha256"] != skill["manifest_sha256"]:
                raise AgentError("agent_skill_integrity_failed")
            if b"\x00" in raw[:4096]:
                raise AgentError("agent_skill_binary_file_unsupported")
            text = raw.decode("utf-8", errors="replace")
            return {
                "skill_id": skill_id,
                "version": str(skill.get("version", "1")),
                "install_generation": int(skill.get("install_generation", 1)),
                "manifest_sha256": manifest["manifest_sha256"],
                "path": target.relative_to(pkg_dir).as_posix(),
                "content": text[:max_chars],
                "truncated": len(text) > max_chars,
                "files": [f["path"] for f in manifest["files"]],
            }

    def get_mcp_server_internal(
        self,
        server_id: str,
        *,
        require_enabled: bool = False,
        expected_revision: int | None = None,
        expected_config_generation: int | None = None,
        expected_instance_id: str | None = None,
    ) -> dict[str, Any]:
        """Return internal MCP server record (with decrypted secrets) for transport/invocation."""
        with file_transaction(self.lock):
            state = self._read()
            server = state["mcp_servers"].get(server_id)
            if not server:
                raise AgentError("agent_mcp_server_not_found")
            if expected_instance_id is not None and expected_instance_id != server.get("instance_id"):
                raise AgentError("agent_extensions_revision_conflict")
            effective_enabled = self._is_server_effectively_enabled(server, state)
            if require_enabled and not effective_enabled:
                raise AgentError("agent_mcp_server_disabled")
            self._check_expected_revision(expected_revision, int(server.get("revision", 1)))
            if expected_config_generation is not None:
                if (
                    type(expected_config_generation) is not int
                    or expected_config_generation != int(server.get("config_generation", 1))
                ):
                    raise AgentError("agent_extensions_revision_conflict")
            out = copy.deepcopy(server)
            pid = server.get("plugin_id")
            out["effective_enabled"] = effective_enabled
            out["parent_enabled"] = self._is_parent_plugin_enabled(pid, state)
            out["plugin_revision"] = (
                int(state["plugins"][pid].get("revision", 1))
                if pid and pid in state["plugins"]
                else None
            )
            return out

    def list_enabled_mcp_servers_internal(self) -> list[dict[str, Any]]:
        """Return only MCP servers where both the server and any parent plugin are enabled."""
        with file_transaction(self.lock):
            state = self._read()
            result: list[dict[str, Any]] = []
            for srv in state["mcp_servers"].values():
                if self._is_server_effectively_enabled(srv, state):
                    item = copy.deepcopy(srv)
                    pid = srv.get("plugin_id")
                    item["effective_enabled"] = True
                    item["parent_enabled"] = True
                    item["plugin_revision"] = (
                        int(state["plugins"][pid].get("revision", 1))
                        if pid and pid in state["plugins"]
                        else None
                    )
                    result.append(item)
            return result

    def verify_builtin_computer_use_authority(
        self,
        binding: dict[str, Any],
    ) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]] | None:
        """Verify whether `binding` belongs to the authentic builtin `computer-use` provider.

        Returns `(server, plugin, tool_row)` when the binding is the authentic builtin
        `computer-use` provider, `None` when the binding is a third-party MCP server
        (even if `computer_access=True`), or raises `AgentToolRejected` if any part of
        the builtin authority tuple is spoofed, disabled, or mutated.
        """
        if not isinstance(binding, dict):
            raise AgentToolRejected("agent_tool_definition_changed")
        with file_transaction(self.lock):
            state = self._read()
            b_srv_id = str(binding.get("server_id") or "")
            b_inst_id = str(binding.get("instance_id") or "")
            b_plug_id = binding.get("plugin_id")
            stored_srv = (state.get("mcp_servers") or {}).get(b_srv_id)
            claims_builtin = (
                b_srv_id == COMPUTER_USE_SERVER_ID
                or b_inst_id == COMPUTER_USE_SERVER_INSTANCE_ID
                or b_plug_id == COMPUTER_USE_PLUGIN_ID
                or (
                    isinstance(stored_srv, dict)
                    and (
                        bool(stored_srv.get("builtin"))
                        or stored_srv.get("id") == COMPUTER_USE_SERVER_ID
                        or stored_srv.get("instance_id") == COMPUTER_USE_SERVER_INSTANCE_ID
                        or stored_srv.get("plugin_id") == COMPUTER_USE_PLUGIN_ID
                    )
                )
            )
            if not claims_builtin:
                return None

            if (
                b_srv_id != COMPUTER_USE_SERVER_ID
                or b_inst_id != COMPUTER_USE_SERVER_INSTANCE_ID
                or b_plug_id != COMPUTER_USE_PLUGIN_ID
                or binding.get("computer_access") is not True
            ):
                raise AgentToolRejected("agent_tool_definition_changed")

            server = (state.get("mcp_servers") or {}).get(COMPUTER_USE_SERVER_ID)
            plugin = (state.get("plugins") or {}).get(COMPUTER_USE_PLUGIN_ID)
            if not isinstance(server, dict) or not isinstance(plugin, dict):
                raise AgentToolRejected("agent_tool_definition_changed")

            if (
                server.get("id") != COMPUTER_USE_SERVER_ID
                or server.get("builtin") is not True
                or server.get("deletable") is not False
                or server.get("instance_id") != COMPUTER_USE_SERVER_INSTANCE_ID
                or server.get("plugin_id") != COMPUTER_USE_PLUGIN_ID
                or server.get("computer_access") is not True
                or plugin.get("id") != COMPUTER_USE_PLUGIN_ID
                or plugin.get("builtin") is not True
                or plugin.get("deletable") is not False
                or plugin.get("instance_id") != COMPUTER_USE_PLUGIN_INSTANCE_ID
                or plugin.get("computer_access") is not True
                or COMPUTER_USE_SERVER_ID not in (plugin.get("mcp_server_ids") or [])
            ):
                raise AgentToolRejected("agent_tool_definition_changed")

            if not (
                bool(server.get("enabled"))
                and bool(plugin.get("enabled"))
                and self._is_server_effectively_enabled(server, state)
            ):
                raise AgentToolRejected("agent_mcp_server_disabled")

            if (
                type(binding.get("revision")) is not int
                or int(server.get("revision", 0)) != int(binding["revision"])
                or type(binding.get("config_generation")) is not int
                or int(server.get("config_generation", 0)) != int(binding["config_generation"])
                or type(binding.get("plugin_revision")) is not int
                or int(plugin.get("revision", 0)) != int(binding["plugin_revision"])
            ):
                raise AgentToolRejected("agent_tool_definition_changed")

            orig_name = str(binding.get("original_tool_name") or "")
            expected_hash = COMPUTER_USE_BUILTIN_SCHEMA_HASHES.get(orig_name)
            if (
                not expected_hash
                or binding.get("model_tool_name")
                != compute_model_tool_name(COMPUTER_USE_SERVER_ID, orig_name)
                or binding.get("schema_sha256") != expected_hash
            ):
                raise AgentToolRejected("agent_tool_definition_changed")

            matched_tool: dict[str, Any] | None = None
            for tool_row in server.get("discovered_tools") or []:
                if isinstance(tool_row, dict) and str(tool_row.get("name") or "") == orig_name:
                    matched_tool = tool_row
                    break
            if (
                matched_tool is None
                or not isinstance(matched_tool.get("input_schema"), dict)
                or _canon_sha256(matched_tool["input_schema"]) != expected_hash
                or str(matched_tool.get("schema_sha256") or "") != expected_hash
            ):
                raise AgentToolRejected("agent_tool_definition_changed")

            enabled_tools = server.get("enabled_tools")
            disabled_tools = set(server.get("disabled_tools") or [])
            if (
                (enabled_tools is not None and orig_name not in enabled_tools)
                or orig_name in disabled_tools
                or not matched_tool.get("enabled", True)
            ):
                raise AgentToolRejected("agent_tool_definition_changed")

            read_only_tools = set(server.get("read_only_tools") or [])
            if orig_name == "computer_act_window" and orig_name in read_only_tools:
                raise AgentToolRejected("agent_tool_definition_changed")
            expected_effect = "read" if orig_name in read_only_tools else "write"
            if orig_name == "computer_act_window" and expected_effect != "write":
                raise AgentToolRejected("agent_tool_definition_changed")
            if binding.get("effective_effect") != expected_effect:
                raise AgentToolRejected("agent_tool_definition_changed")

            srv_copy = copy.deepcopy(server)
            srv_copy["effective_enabled"] = True
            srv_copy["parent_enabled"] = True
            srv_copy["plugin_revision"] = int(plugin.get("revision", 1))
            return srv_copy, copy.deepcopy(plugin), copy.deepcopy(matched_tool)

    # ------------------------------------------------------------------
    # Mutations: Plugins, Skills, MCP Servers (with transactional revision checks)
    # ------------------------------------------------------------------

    def set_plugin_enabled(
        self,
        plugin_id: str,
        enabled: bool,
        *,
        expected_revision: int | None = None,
        desktop_access: bool = False,
    ) -> dict[str, Any]:
        with file_transaction(self.lock):
            state = self._read()
            plugin = state["plugins"].get(plugin_id)
            if not plugin:
                raise AgentError("agent_plugin_not_found")
            self._check_expected_revision(expected_revision, int(plugin.get("revision", 1)))
            now = _now()
            gen = _alloc_generation(state)
            plugin["enabled"] = bool(enabled)
            plugin["revision"] = gen
            plugin["updated_at"] = now
            for sid in plugin.get("skill_ids", []):
                if sid in state["skills"]:
                    child_skill = state["skills"][sid]
                    if enabled and not child_skill.get("explicitly_disabled", False):
                        child_skill["enabled"] = True
                    child_skill["revision"] = _alloc_generation(state)
                    child_skill["updated_at"] = now
            for mid in plugin.get("mcp_server_ids", []):
                if mid in state["mcp_servers"]:
                    srv = state["mcp_servers"][mid]
                    if enabled and not srv.get("explicitly_disabled", False):
                        srv["enabled"] = True
                    srv["revision"] = _alloc_generation(state)
                    srv["updated_at"] = now
            state["updated_at"] = now
            self._write(state)
            return self._public_plugin(plugin, state, desktop_access=desktop_access)

    def delete_plugin(
        self,
        plugin_id: str,
        *,
        expected_revision: int | None = None,
    ) -> dict[str, Any]:
        with file_transaction(self.lock):
            state = self._read()
            plugin = state["plugins"].get(plugin_id)
            if not plugin:
                raise AgentError("agent_plugin_not_found")
            if plugin.get("builtin") or not plugin.get("deletable", True):
                raise AgentError("agent_extension_builtin_protected")
            self._check_expected_revision(expected_revision, int(plugin.get("revision", 1)))
            removed_servers = list(plugin.get("mcp_server_ids", []))
            for sid in plugin.get("skill_ids", []):
                state["skills"].pop(sid, None)
            for mid in removed_servers:
                state["mcp_servers"].pop(mid, None)
            pkg_dir = plugin.get("package_dir")
            del state["plugins"][plugin_id]
            _alloc_generation(state)
            state["updated_at"] = _now()
            self._write(state)
            if pkg_dir:
                self._safe_rmtree(Path(pkg_dir))
            return {
                "deleted": True,
                "id": plugin_id,
                "removed_mcp_server_ids": removed_servers,
                "revision": state["revision"],
            }

    def set_skill_enabled(
        self,
        skill_id: str,
        enabled: bool,
        *,
        expected_revision: int | None = None,
    ) -> dict[str, Any]:
        with file_transaction(self.lock):
            state = self._read()
            if skill_id in BUILTIN_SKILLS:
                builtin_revs = state.setdefault("builtin_skills_revisions", {})
                cur_rev = int(builtin_revs.get(skill_id, 1))
                self._check_expected_revision(expected_revision, cur_rev)
                new_rev = _alloc_generation(state)
                state.setdefault("builtin_skills_enabled", {})[skill_id] = bool(enabled)
                builtin_revs[skill_id] = new_rev
                state["updated_at"] = _now()
                self._write(state)
                return self._public_builtin_skill(BUILTIN_SKILLS[skill_id], bool(enabled), new_rev)
            skill = state["skills"].get(skill_id)
            if not skill:
                raise AgentError("agent_skill_not_found")
            self._check_expected_revision(expected_revision, int(skill.get("revision", 1)))
            skill["enabled"] = bool(enabled)
            skill["explicitly_disabled"] = not bool(enabled)
            skill["revision"] = _alloc_generation(state)
            skill["updated_at"] = _now()
            state["updated_at"] = skill["updated_at"]
            self._write(state)
            return self._public_imported_skill(skill, state)

    def delete_skill(
        self,
        skill_id: str,
        *,
        expected_revision: int | None = None,
    ) -> dict[str, Any]:
        with file_transaction(self.lock):
            state = self._read()
            if skill_id in BUILTIN_SKILLS:
                raise AgentError("agent_extension_builtin_protected")
            skill = state["skills"].get(skill_id)
            if not skill:
                raise AgentError("agent_skill_not_found")
            if skill.get("deletable") is False or skill.get("scope") == "system":
                raise AgentError("agent_extension_builtin_protected")
            self._check_expected_revision(expected_revision, int(skill.get("revision", 1)))
            plugin_id = skill.get("plugin_id")
            if plugin_id and plugin_id in state["plugins"]:
                p_skills = state["plugins"][plugin_id].get("skill_ids", [])
                if skill_id in p_skills:
                    p_skills.remove(skill_id)
                state["plugins"][plugin_id]["revision"] = _alloc_generation(state)
            if skill.get("source_format") == "codex_local":
                uninstalled = state.setdefault("uninstalled_discovered_skills", [])
                if skill_id not in uninstalled:
                    uninstalled.append(skill_id)
            pkg_dir = skill.get("package_dir")
            del state["skills"][skill_id]
            _alloc_generation(state)
            state["updated_at"] = _now()
            self._write(state)
            if pkg_dir and not plugin_id:
                self._safe_rmtree(Path(pkg_dir))
            return {"deleted": True, "id": skill_id, "revision": state["revision"]}

    def sync_discovered_skills(
        self,
        *,
        roots: list[tuple[Path, str]] | None = None,
        workspace_roots: list[Path] | None = None,
        desktop_access: bool = False,
        reset_uninstalled: bool = False,
    ) -> dict[str, Any]:
        """Discover local Codex/Agent skills (.codex/skills, .agents/skills) and sync into store."""
        from .codex_skills import copy_codex_skill_bounded, discover_local_skills

        discovered = discover_local_skills(roots=roots, workspace_roots=workspace_roots)
        with file_transaction(self.lock):
            state = self._read()
            if reset_uninstalled:
                state["uninstalled_discovered_skills"] = []
            uninstalled = set(state.get("uninstalled_discovered_skills") or [])
            now = _now()
            mutated = False
            synced_ids: list[str] = []

            for disc in discovered:
                sid = disc["id"]
                if sid in BUILTIN_SKILLS or sid in uninstalled:
                    continue
                existing = state["skills"].get(sid)
                if existing and existing.get("source_format") not in ("codex_local", None):
                    if not existing.get("interface") and disc.get("interface"):
                        existing["interface"] = disc["interface"]
                        existing["short_description"] = disc.get("short_description") or existing.get("summary")
                        existing["skill_md_path"] = disc.get("skill_md_path")
                        mutated = True
                    continue

                ext_sha = disc.get("manifest_sha256")
                existing_pkg = existing.get("package_dir") if existing else None
                if (
                    existing
                    and existing.get("external_manifest_sha256") == ext_sha
                    and existing_pkg
                    and Path(existing_pkg).is_dir()
                ):
                    if existing.get("interface") != disc.get("interface"):
                        existing["interface"] = disc.get("interface")
                        existing["short_description"] = disc.get("short_description")
                        existing["title"] = disc.get("title") or existing.get("title")
                        mutated = True
                    synced_ids.append(sid)
                    continue

                dest_pkg = self.allocate_package_dir("skill", sid)
                copy_codex_skill_bounded(Path(disc["package_dir"]), dest_pkg)
                pkg_manifest = compute_directory_manifest(dest_pkg)
                gen = _alloc_generation(state)
                install_gen = (int(existing.get("install_generation", 1)) + 1) if existing else 1
                is_enabled = bool(existing.get("enabled", True)) if existing else True
                explicitly_disabled = bool(existing.get("explicitly_disabled", False)) if existing else False

                state["skills"][sid] = {
                    "id": sid,
                    "instance_id": existing.get("instance_id") if existing else uuid.uuid4().hex,
                    "name": disc.get("name") or sid,
                    "title": disc.get("title") or disc.get("name") or sid,
                    "description": disc.get("description") or "",
                    "short_description": disc.get("short_description") or disc.get("summary") or "",
                    "summary": disc.get("summary") or disc.get("description") or "",
                    "version": str(disc.get("version") or "1.0.0"),
                    "install_generation": install_gen,
                    "content": str(disc.get("content") or ""),
                    "package_dir": str(dest_pkg),
                    "skill_md_path": disc.get("skill_md_path") or str(dest_pkg / "SKILL.md"),
                    "external_source_dir": disc.get("package_dir"),
                    "external_manifest_sha256": ext_sha,
                    "manifest_sha256": pkg_manifest["manifest_sha256"],
                    "files": pkg_manifest["files"],
                    "plugin_id": None,
                    "scope": disc.get("scope") or "user",
                    "deletable": (disc.get("scope") or "user") != "system",
                    "source_format": "codex_local",
                    "interface": disc.get("interface") or {},
                    "policy": disc.get("policy") or {"allow_implicit_invocation": True},
                    "warnings": [],
                    "enabled": is_enabled,
                    "explicitly_disabled": explicitly_disabled,
                    "revision": gen,
                    "updated_at": now,
                }
                if existing_pkg and existing_pkg != str(dest_pkg):
                    self._safe_rmtree(Path(existing_pkg))
                mutated = True
                synced_ids.append(sid)

            if mutated:
                _alloc_generation(state)
                state["updated_at"] = now
                self._write(state)

        return {
            "synced_skill_ids": synced_ids,
            "overview": self.overview(desktop_access=desktop_access),
        }

    def list_recommended_skills(
        self,
        *,
        refresh: bool = False,
        curated_roots: list[Path] | None = None,
    ) -> dict[str, Any]:
        """Return curated recommended skills from Codex vendor_imports or fallback catalog."""
        from .codex_skills import discover_recommended_skills

        curated = discover_recommended_skills(curated_roots=curated_roots)
        with file_transaction(self.lock):
            state = self._read()
            installed_ids = set(BUILTIN_SKILLS.keys()) | set(state["skills"].keys())

        items: list[dict[str, Any]] = []
        for item in curated:
            row = copy.deepcopy(item)
            row["installed"] = row["id"] in installed_ids
            items.append(row)
        return {
            "skills": items,
            "count": len(items),
            "refreshed": bool(refresh),
        }

    def install_recommended_skill(
        self,
        payload: dict[str, Any],
        *,
        curated_roots: list[Path] | None = None,
        install_to_codex_home: bool = False,
        desktop_access: bool = False,
    ) -> dict[str, Any]:
        """Install a recommended curated skill into ExtensionStore (and optionally ~/.codex/skills)."""
        from .codex_skills import (
            copy_codex_skill_bounded,
            default_codex_home,
            discover_recommended_skills,
        )

        if not isinstance(payload, dict):
            raise AgentError("agent_invalid_request")
        raw_id = str(payload.get("skill_id") or payload.get("id") or "").strip()
        repo_path = str(payload.get("repo_path") or payload.get("repoPath") or "").strip()
        if not raw_id and not repo_path:
            raise AgentError("agent_invalid_request")

        curated = discover_recommended_skills(curated_roots=curated_roots)
        matched = next(
            (
                item
                for item in curated
                if (raw_id and item["id"] == raw_id)
                or (repo_path and item.get("repo_path") == repo_path)
            ),
            None,
        )
        if not matched:
            raise AgentError("agent_skill_not_found")

        sid = matched["id"]
        if sid in BUILTIN_SKILLS:
            raise AgentError("agent_skill_builtin_conflict")

        dest_pkg = self.allocate_package_dir("skill", sid)
        src_dir_str = matched.get("source_dir")
        if src_dir_str and Path(src_dir_str).is_dir():
            copy_codex_skill_bounded(Path(src_dir_str), dest_pkg)
            if install_to_codex_home:
                try:
                    codex_target = default_codex_home() / "skills" / sid
                    if not codex_target.exists():
                        copy_codex_skill_bounded(Path(src_dir_str), codex_target)
                except Exception:
                    pass
        else:
            dest_pkg.mkdir(parents=True, exist_ok=True)
            (dest_pkg / "SKILL.md").write_text(str(matched.get("content") or ""), encoding="utf-8")

        pkg_manifest = compute_directory_manifest(dest_pkg)
        with file_transaction(self.lock):
            state = self._read()
            existing = state["skills"].get(sid)
            old_pkg = existing.get("package_dir") if existing else None
            install_gen = (int(existing.get("install_generation", 1)) + 1) if existing else 1
            now = _now()
            gen = _alloc_generation(state)
            uninstalled = state.get("uninstalled_discovered_skills")
            if isinstance(uninstalled, list) and sid in uninstalled:
                uninstalled.remove(sid)

            record = {
                "id": sid,
                "instance_id": existing.get("instance_id") if existing else uuid.uuid4().hex,
                "name": matched.get("name") or sid,
                "title": matched.get("title") or matched.get("name") or sid,
                "description": matched.get("description") or "",
                "short_description": matched.get("short_description") or matched.get("summary") or "",
                "summary": matched.get("summary") or matched.get("description") or "",
                "version": str(matched.get("version") or "1.0.0"),
                "install_generation": install_gen,
                "content": str(matched.get("content") or ""),
                "package_dir": str(dest_pkg),
                "skill_md_path": str((dest_pkg / "SKILL.md").resolve()),
                "manifest_sha256": pkg_manifest["manifest_sha256"],
                "files": pkg_manifest["files"],
                "plugin_id": None,
                "scope": "user",
                "deletable": True,
                "source_format": "codex_curated",
                "interface": matched.get("interface") or {},
                "policy": matched.get("policy") or {"allow_implicit_invocation": True},
                "warnings": [],
                "enabled": True,
                "explicitly_disabled": False,
                "revision": gen,
                "updated_at": now,
            }
            state["skills"][sid] = record
            _alloc_generation(state)
            state["updated_at"] = now
            self._write(state)
            if old_pkg and old_pkg != str(dest_pkg):
                self._safe_rmtree(Path(old_pkg))
            skill_view = self._public_imported_skill(record, state)

        return {
            "skill": skill_view,
            "overview": self.overview(desktop_access=desktop_access),
        }

    def open_skill_path(self, skill_id: str, *, action: str = "open") -> dict[str, Any]:
        """Open SKILL.md in editor or reveal in File Explorer for a builtin or installed skill."""
        from .codex_skills import open_or_reveal_skill_path

        with file_transaction(self.lock):
            state = self._read()
            if skill_id in BUILTIN_SKILLS:
                b_skill = BUILTIN_SKILLS[skill_id]
                b_dir = (self.pkgs_dir / "builtin" / _slugify(skill_id)).resolve()
                b_dir.mkdir(parents=True, exist_ok=True)
                target = b_dir / "SKILL.md"
                target.write_text(str(b_skill.get("content") or ""), encoding="utf-8")
            else:
                skill = state["skills"].get(skill_id)
                if not skill:
                    raise AgentError("agent_skill_not_found")
                raw_path = skill.get("skill_md_path")
                pkg_dir = skill.get("package_dir")
                if raw_path and Path(raw_path).exists():
                    target = Path(raw_path).resolve()
                elif pkg_dir and (Path(pkg_dir) / "SKILL.md").exists():
                    target = (Path(pkg_dir) / "SKILL.md").resolve()
                else:
                    fallback_dir = (self.pkgs_dir / "skill" / _slugify(skill_id) / "preview").resolve()
                    fallback_dir.mkdir(parents=True, exist_ok=True)
                    target = fallback_dir / "SKILL.md"
                    target.write_text(str(skill.get("content") or ""), encoding="utf-8")

        return open_or_reveal_skill_path(target, action=action)

    def upsert_mcp_server(
        self,
        payload: dict[str, Any],
        *,
        server_id: str | None = None,
        expected_revision: int | None = None,
        desktop_access: bool = False,
    ) -> dict[str, Any]:
        if not isinstance(payload, dict):
            raise AgentError("agent_invalid_extension")
        if expected_revision is None and "expected_revision" in payload:
            expected_revision = payload["expected_revision"]

        with file_transaction(self.lock):
            state = self._read()
            now = _now()
            existing = state["mcp_servers"].get(server_id) if server_id else None
            if server_id and not existing:
                raise AgentError("agent_mcp_server_not_found")

            if existing:
                self._check_expected_revision(expected_revision, int(existing.get("revision", 1)))
            else:
                self._check_expected_revision(expected_revision, int(state.get("revision", 1)))

            if existing and existing.get("builtin"):
                if "enabled" in payload:
                    existing["enabled"] = bool(payload["enabled"])
                    existing["explicitly_disabled"] = not bool(payload["enabled"])
                self._apply_mcp_policy_fields(existing, payload)
                gen = _alloc_generation(state)
                existing["config_generation"] = gen
                existing["config_sha256"] = _mcp_config_fingerprint(existing)
                existing["revision"] = gen
                existing["updated_at"] = now
                state["updated_at"] = now
                self._write(state)
                return self._public_mcp_server(existing, state, desktop_access=desktop_access)

            raw_id = server_id or str(payload.get("id") or payload.get("name") or "").strip()
            target_id = _slugify(raw_id, fallback="mcp-server")
            if not EXTENSION_ID_RE.match(target_id):
                raise AgentError("agent_mcp_invalid_id")
            if not existing and target_id in state["mcp_servers"]:
                raise AgentError("agent_mcp_server_already_exists")

            transport = str(
                payload.get("transport")
                or (existing.get("transport") if existing else "stdio")
            ).strip().lower()
            if transport in ("http", "streamable-http", "sse"):
                transport = "streamable_http"
            if transport not in ("stdio", "streamable_http"):
                raise AgentError("agent_mcp_invalid_transport")

            name = str(
                payload.get("name")
                if "name" in payload
                else (existing.get("name") if existing else target_id)
            ).strip() or target_id
            description = str(
                payload.get("description")
                if "description" in payload
                else (existing.get("description") if existing else "")
            ).strip()[:1000]

            command = str(
                payload.get("command")
                if "command" in payload
                else (existing.get("command") if existing else "")
            ).strip()
            raw_args = (
                payload.get("args")
                if "args" in payload
                else (existing.get("args") if existing else [])
            )
            if not isinstance(raw_args, list) or any(not isinstance(a, str) for a in raw_args):
                raise AgentError("agent_mcp_invalid_args")
            args = [str(a) for a in raw_args[:64]]
            cwd = (
                payload.get("cwd")
                if "cwd" in payload
                else (existing.get("cwd") if existing else None)
            )
            cwd_str = str(cwd).strip() if cwd else None

            url = str(
                payload.get("url")
                if "url" in payload
                else (existing.get("url") if existing else "")
            ).strip()
            raw_bearer_env = (
                payload.get("bearer_token_env")
                if "bearer_token_env" in payload
                else (
                    payload.get("bearer_token_env_var")
                    if "bearer_token_env_var" in payload
                    else (existing.get("bearer_token_env") if existing else "")
                )
            )
            bearer_token_env = str(raw_bearer_env or "").strip() or None
            if bearer_token_env and not ENV_KEY_RE.match(bearer_token_env):
                raise AgentError("agent_mcp_invalid_bearer_token_env")

            if transport == "stdio":
                if not command:
                    raise AgentError("agent_mcp_command_required")
            else:
                if not url or not (url.startswith("http://") or url.startswith("https://")):
                    raise AgentError("agent_mcp_url_required")

            raw_env_vars = (
                payload.get("env_vars")
                if "env_vars" in payload
                else (
                    payload.get("envVars")
                    if "envVars" in payload
                    else (existing.get("env_vars") if existing else [])
                )
            )
            if raw_env_vars is None:
                raw_env_vars = []
            if not isinstance(raw_env_vars, list) or any(not isinstance(item, str) for item in raw_env_vars):
                raise AgentError("agent_mcp_invalid_env_key")
            env_vars: list[str] = []
            for item in raw_env_vars[:64]:
                var_name = str(item).strip()
                if not var_name:
                    continue
                if not ENV_KEY_RE.match(var_name):
                    raise AgentError("agent_mcp_invalid_env_key")
                if var_name not in env_vars:
                    env_vars.append(var_name)

            secret_env = copy.deepcopy(existing.get("secret_env") or {}) if existing else {}
            for remove_key in payload.get("remove_env_keys") or []:
                secret_env.pop(str(remove_key), None)
            incoming_env = payload.get("env") if "env" in payload else payload.get("secret_env")
            if isinstance(incoming_env, dict):
                if payload.get("replace_env") is True:
                    secret_env = {}
                for k, v in incoming_env.items():
                    key_str = str(k).strip()
                    if not ENV_KEY_RE.match(key_str):
                        raise AgentError("agent_mcp_invalid_env_key")
                    if v is None or v == "":
                        secret_env.pop(key_str, None)
                    elif v != MASKED_VALUE and v != "__KEEP__":
                        secret_env[key_str] = str(v)

            secret_headers = copy.deepcopy(existing.get("secret_headers") or {}) if existing else {}
            for remove_key in payload.get("remove_header_keys") or []:
                secret_headers.pop(str(remove_key), None)
            incoming_headers = (
                payload.get("headers")
                if "headers" in payload
                else (
                    payload.get("http_headers")
                    if "http_headers" in payload
                    else payload.get("secret_headers")
                )
            )
            if isinstance(incoming_headers, dict):
                if payload.get("replace_headers") is True:
                    secret_headers = {}
                for k, v in incoming_headers.items():
                    key_str = str(k).strip()
                    if not HEADER_KEY_RE.match(key_str):
                        raise AgentError("agent_mcp_invalid_header_key")
                    if v is None or v == "":
                        secret_headers.pop(key_str, None)
                    elif v != MASKED_VALUE and v != "__KEEP__":
                        secret_headers[key_str] = str(v)

            has_incoming_env_headers = (
                "env_http_headers" in payload or "envHttpHeaders" in payload
            )
            raw_env_http_headers = (
                payload.get("env_http_headers")
                if "env_http_headers" in payload
                else (
                    payload.get("envHttpHeaders")
                    if "envHttpHeaders" in payload
                    else (existing.get("env_http_headers") if existing else {})
                )
            )
            if raw_env_http_headers is None:
                raw_env_http_headers = {}
            if not isinstance(raw_env_http_headers, dict):
                raise AgentError("agent_mcp_invalid_header_key")
            env_http_headers: dict[str, str] = (
                {}
                if has_incoming_env_headers
                else copy.deepcopy(existing.get("env_http_headers") or {}) if existing else {}
            )
            if has_incoming_env_headers:
                for k, v in raw_env_http_headers.items():
                    key_str = str(k).strip()
                    if not key_str:
                        continue
                    if not HEADER_KEY_RE.match(key_str):
                        raise AgentError("agent_mcp_invalid_header_key")
                    if v is None or str(v).strip() == "":
                        env_http_headers.pop(key_str, None)
                    else:
                        env_var_str = str(v).strip()
                        if not ENV_KEY_RE.match(env_var_str):
                            raise AgentError("agent_mcp_invalid_env_key")
                        env_http_headers[key_str] = env_var_str

            raw_oauth_reg = (
                payload.get("oauth_client_registration")
                if "oauth_client_registration" in payload
                else (
                    payload.get("oauthClientRegistration")
                    if "oauthClientRegistration" in payload
                    else (existing.get("oauth_client_registration") if existing else "auto")
                )
            )
            oauth_client_registration = str(raw_oauth_reg or "auto").strip().lower()
            if oauth_client_registration not in ("auto", "cimd", "dcr"):
                oauth_client_registration = "auto"

            enabled = bool(
                payload["enabled"]
                if "enabled" in payload
                else (existing.get("enabled", False) if existing else False)
            )
            computer_access = bool(
                payload["computer_access"]
                if "computer_access" in payload
                else (existing.get("computer_access", False) if existing else False)
            )

            gen = _alloc_generation(state)
            record: dict[str, Any] = {
                "id": target_id,
                "instance_id": existing.get("instance_id") if existing else uuid.uuid4().hex,
                "name": name,
                "description": description,
                "transport": transport,
                "builtin": False,
                "deletable": True,
                "plugin_id": existing.get("plugin_id") if existing else payload.get("plugin_id"),
                "computer_access": computer_access,
                "enabled": enabled,
                "explicitly_disabled": not enabled if "enabled" in payload else (
                    existing.get("explicitly_disabled", False) if existing else False
                ),
                "command": command if transport == "stdio" else None,
                "args": args if transport == "stdio" else [],
                "cwd": cwd_str if transport == "stdio" else None,
                "secret_env": secret_env,
                "env_vars": env_vars if transport == "stdio" else [],
                "url": url if transport == "streamable_http" else None,
                "bearer_token_env": bearer_token_env if transport == "streamable_http" else None,
                "secret_headers": secret_headers,
                "env_http_headers": env_http_headers if transport == "streamable_http" else {},
                "oauth_client_registration": oauth_client_registration,
                "startup_timeout_ms": int(existing.get("startup_timeout_ms", 10000) if existing else 10000),
                "tool_timeout_ms": int(existing.get("tool_timeout_ms", 30000) if existing else 30000),
                "enabled_tools": existing.get("enabled_tools") if existing else None,
                "disabled_tools": list(existing.get("disabled_tools") or []) if existing else [],
                "read_only_tools": list(existing.get("read_only_tools") or []) if existing else [],
                "discovered_tools": list(existing.get("discovered_tools") or []) if existing else [],
                "status": existing.get("status", "NeedsTest") if existing else ("NeedsTest" if enabled else "disabled"),
                "status_message": existing.get("status_message", "") if existing else "",
                "last_tested_at": existing.get("last_tested_at") if existing else None,
                "config_generation": int(existing.get("config_generation", gen)) if existing else gen,
                "revision": gen,
                "updated_at": now,
            }
            self._apply_mcp_policy_fields(record, payload)
            new_fp = _mcp_config_fingerprint(record)
            old_fp = existing.get("config_sha256") if existing else None
            if not existing or new_fp != old_fp:
                record["config_generation"] = gen
                if existing:
                    record["discovered_tools"] = []
                    record["status"] = "NeedsTest" if record["enabled"] else "disabled"
                    record["status_message"] = ""
            record["config_sha256"] = new_fp

            if not self._is_server_effectively_enabled(record, state):
                record["status"] = "disabled"
            elif record["status"] == "disabled":
                record["status"] = "ready" if record["discovered_tools"] else "NeedsTest"

            state["mcp_servers"][target_id] = record
            state["updated_at"] = now
            self._write(state)
            return self._public_mcp_server(record, state, desktop_access=desktop_access)

    def _apply_mcp_policy_fields(self, record: dict[str, Any], payload: dict[str, Any]) -> None:
        if "startup_timeout_ms" in payload and payload["startup_timeout_ms"] is not None:
            val = int(payload["startup_timeout_ms"])
            record["startup_timeout_ms"] = max(1000, min(60000, val))
        if "tool_timeout_ms" in payload and payload["tool_timeout_ms"] is not None:
            val = int(payload["tool_timeout_ms"])
            record["tool_timeout_ms"] = max(1000, min(120000, val))
        if "enabled_tools" in payload:
            et = payload["enabled_tools"]
            record["enabled_tools"] = [str(x) for x in et] if isinstance(et, list) else None
        if "disabled_tools" in payload and isinstance(payload["disabled_tools"], list):
            record["disabled_tools"] = sorted({str(x) for x in payload["disabled_tools"] if str(x)})
        if "read_only_tools" in payload and isinstance(payload["read_only_tools"], list):
            record["read_only_tools"] = sorted({str(x) for x in payload["read_only_tools"] if str(x)})
        if "computer_access" in payload and not record.get("builtin"):
            record["computer_access"] = bool(payload["computer_access"])

    def record_mcp_discovery(
        self,
        server_id: str,
        *,
        expected_revision: int,
        expected_config_generation: int | None = None,
        expected_instance_id: str | None = None,
        expected_enabled: bool | None = None,
        allow_disabled: bool = False,
        discovered_tools: list[dict[str, Any]] | None = None,
        status: str,
        status_message: str = "",
        desktop_access: bool = False,
    ) -> dict[str, Any]:
        """Persist discovered tools and status for an MCP server if its identity & generation match.

        Rejects stale probes when the server was deleted, recreated (ABA), disabled, or had its
        configuration modified since the probe started. Explicit testing while disabled is
        allowed with allow_disabled=True without enabling the entry.
        """
        if type(expected_revision) is not int or expected_revision < 1:
            raise AgentError("agent_invalid_revision")

        with file_transaction(self.lock):
            state = self._read()
            server = state["mcp_servers"].get(server_id)
            if not server:
                raise AgentError("agent_mcp_server_not_found")
            if expected_instance_id is not None and expected_instance_id != server.get("instance_id"):
                raise AgentError("agent_extensions_revision_conflict")
            self._check_expected_revision(expected_revision, int(server.get("revision", 1)))
            if expected_config_generation is not None:
                if (
                    type(expected_config_generation) is not int
                    or expected_config_generation != int(server.get("config_generation", 1))
                ):
                    raise AgentError("agent_extensions_revision_conflict")
            effective_enabled = self._is_server_effectively_enabled(server, state)
            if expected_enabled is not None and effective_enabled != bool(expected_enabled):
                raise AgentError("agent_extensions_revision_conflict")
            if not allow_disabled and not effective_enabled:
                raise AgentError("agent_mcp_server_disabled")

            now = _now()
            old_tools_hash = _canon_sha256(server.get("discovered_tools") or [])
            if discovered_tools is not None:
                normalized: list[dict[str, Any]] = []
                for item in discovered_tools:
                    name = str(item.get("name") or "").strip()
                    if not name:
                        continue
                    schema = item.get("inputSchema") or item.get("input_schema") or {"type": "object"}
                    if not isinstance(schema, dict):
                        schema = {"type": "object"}
                    annotations = item.get("annotations") if isinstance(item.get("annotations"), dict) else {}
                    normalized.append(
                        {
                            "name": name,
                            "title": str(item.get("title") or name),
                            "description": str(item.get("description") or "")[:1000],
                            "input_schema": schema,
                            "schema_sha256": _canon_sha256(schema),
                            "annotations": {
                                "readOnlyHint": bool(annotations.get("readOnlyHint", False)),
                                "destructiveHint": bool(annotations.get("destructiveHint", False)),
                                "idempotentHint": bool(annotations.get("idempotentHint", False)),
                                "openWorldHint": bool(annotations.get("openWorldHint", False)),
                            },
                            "model_tool_name": compute_model_tool_name(server_id, name),
                        }
                    )
                server["discovered_tools"] = normalized
                new_tools_hash = _canon_sha256(normalized)
                if new_tools_hash != old_tools_hash:
                    server["revision"] = _alloc_generation(state)

            server["status"] = status if effective_enabled else "disabled"
            server["status_message"] = str(status_message or "")[:500]
            server["last_tested_at"] = now
            server["updated_at"] = now
            state["updated_at"] = now
            self._write(state)
            return self._public_mcp_server(server, state, desktop_access=desktop_access)

    def delete_mcp_server(
        self,
        server_id: str,
        *,
        expected_revision: int | None = None,
    ) -> dict[str, Any]:
        with file_transaction(self.lock):
            state = self._read()
            server = state["mcp_servers"].get(server_id)
            if not server:
                raise AgentError("agent_mcp_server_not_found")
            if server.get("builtin") or not server.get("deletable", True):
                raise AgentError("agent_extension_builtin_protected")
            self._check_expected_revision(expected_revision, int(server.get("revision", 1)))
            plugin_id = server.get("plugin_id")
            if plugin_id and plugin_id in state["plugins"]:
                p_servers = state["plugins"][plugin_id].get("mcp_server_ids", [])
                if server_id in p_servers:
                    p_servers.remove(server_id)
                state["plugins"][plugin_id]["revision"] = _alloc_generation(state)
            del state["mcp_servers"][server_id]
            _alloc_generation(state)
            state["updated_at"] = _now()
            self._write(state)
            return {"deleted": True, "id": server_id, "revision": state["revision"]}

    def clear_mcp_app_data(
        self,
        *,
        desktop_access: bool = False,
    ) -> dict[str, Any]:
        """Clear cached MCP app session metadata without removing configured servers."""
        with file_transaction(self.lock):
            state = self._read()
            now = _now()
            state["mcp_app_data_cleared_at"] = now
            state["updated_at"] = now
            self._write(state)
        return {
            "cleared": True,
            "cleared_at": now,
            "overview": self.overview(desktop_access=desktop_access),
        }

    # ------------------------------------------------------------------
    # Import Commit Persistence (creates disabled entries)
    # ------------------------------------------------------------------

    def commit_imported_items(
        self,
        *,
        plugins: list[dict[str, Any]],
        skills: list[dict[str, Any]],
        mcp_servers: list[dict[str, Any]],
        expected_revision: int | None = None,
        desktop_access: bool = False,
    ) -> dict[str, Any]:
        """Atomically persist imported plugins, skills, and MCP servers in disabled state."""
        if (
            len({p["id"] for p in plugins}) != len(plugins)
            or len({s["id"] for s in skills}) != len(skills)
            or len({m["id"] for m in mcp_servers}) != len(mcp_servers)
        ):
            raise AgentError("agent_import_duplicate_id")

        with file_transaction(self.lock):
            state = self._read()
            self._check_expected_revision(expected_revision, int(state.get("revision", 1)))
            now = _now()
            committed_plugins: list[str] = []
            committed_skills: list[str] = []
            committed_servers: list[str] = []

            for skill in skills:
                sid = skill["id"]
                if sid in BUILTIN_SKILLS:
                    raise AgentError("agent_skill_builtin_conflict")
                existing_skill = state["skills"].get(sid)
                install_gen = (int(existing_skill.get("install_generation", 1)) + 1) if existing_skill else 1
                raw_ver = skill.get("version")
                version_str = str(raw_ver).strip() if raw_ver is not None and str(raw_ver).strip() else str(install_gen)
                gen = _alloc_generation(state)
                pkg_dir = skill.get("package_dir")
                expected_manifest_sha256 = skill.get("manifest_sha256")
                files_list = list(skill.get("files", []))
                if pkg_dir:
                    manifest = compute_directory_manifest(Path(pkg_dir).resolve())
                    if (
                        expected_manifest_sha256
                        and manifest["manifest_sha256"] != expected_manifest_sha256
                    ):
                        raise AgentError("agent_extension_package_integrity_failed")
                    manifest_sha256 = manifest["manifest_sha256"]
                    files_list = manifest["files"]
                elif not expected_manifest_sha256:
                    manifest_sha256 = _canon_sha256(
                        {
                            "version": version_str,
                            "install_generation": install_gen,
                            "content": str(skill.get("content", "")),
                        }
                    )
                else:
                    manifest_sha256 = expected_manifest_sha256
                description = str(skill.get("description") or skill.get("summary") or "")
                uninstalled = state.get("uninstalled_discovered_skills")
                if isinstance(uninstalled, list) and sid in uninstalled:
                    uninstalled.remove(sid)
                state["skills"][sid] = {
                    "id": sid,
                    "instance_id": uuid.uuid4().hex,
                    "name": skill.get("name") or sid,
                    "title": skill.get("title") or skill.get("name") or sid,
                    "description": description,
                    "short_description": str(
                        skill.get("short_description")
                        or (skill.get("interface") or {}).get("short_description")
                        or skill.get("summary")
                        or description
                    ),
                    "summary": str(skill.get("summary") or description),
                    "version": version_str,
                    "install_generation": install_gen,
                    "content": str(skill.get("content", "")),
                    "package_dir": pkg_dir,
                    "skill_md_path": skill.get("skill_md_path")
                    or (str(Path(pkg_dir) / "SKILL.md") if pkg_dir else None),
                    "manifest_sha256": manifest_sha256,
                    "files": files_list,
                    "plugin_id": skill.get("plugin_id"),
                    "scope": skill.get("scope") or ("plugin" if skill.get("plugin_id") else "user"),
                    "deletable": bool(skill.get("deletable", True)),
                    "source_format": skill.get("source_format", "skill_md"),
                    "interface": dict(skill.get("interface") or {}),
                    "policy": dict(skill.get("policy") or {"allow_implicit_invocation": True}),
                    "warnings": list(skill.get("warnings", [])),
                    "enabled": False,
                    "explicitly_disabled": False,
                    "revision": gen,
                    "updated_at": now,
                }
                committed_skills.append(sid)

            for srv in mcp_servers:
                mid = srv["id"]
                if mid == COMPUTER_USE_SERVER_ID:
                    raise AgentError("agent_mcp_builtin_conflict")
                gen = _alloc_generation(state)
                raw_enabled = srv.get("enabled_tools")
                enabled_tools = (
                    sorted({str(x).strip() for x in raw_enabled if str(x).strip()})
                    if isinstance(raw_enabled, list)
                    else None
                )
                raw_disabled = srv.get("disabled_tools")
                disabled_tools = (
                    sorted({str(x).strip() for x in raw_disabled if str(x).strip()})
                    if isinstance(raw_disabled, list)
                    else []
                )
                record = {
                    "id": mid,
                    "instance_id": uuid.uuid4().hex,
                    "name": srv.get("name") or mid,
                    "description": srv.get("description", ""),
                    "transport": srv.get("transport", "stdio"),
                    "builtin": False,
                    "deletable": True,
                    "plugin_id": srv.get("plugin_id"),
                    "computer_access": bool(srv.get("computer_access", False)),
                    "enabled": False,
                    "explicitly_disabled": False,
                    "command": srv.get("command"),
                    "args": list(srv.get("args") or []),
                    "cwd": srv.get("cwd"),
                    "secret_env": dict(srv.get("secret_env") or srv.get("env") or {}),
                    "env_vars": list(srv.get("env_vars") or []),
                    "url": srv.get("url"),
                    "bearer_token_env": srv.get("bearer_token_env") or srv.get("bearer_token_env_var"),
                    "secret_headers": dict(
                        srv.get("secret_headers") or srv.get("headers") or srv.get("http_headers") or {}
                    ),
                    "env_http_headers": dict(srv.get("env_http_headers") or {}),
                    "oauth_client_registration": str(srv.get("oauth_client_registration") or "auto"),
                    "startup_timeout_ms": int(srv.get("startup_timeout_ms", 10000)),
                    "tool_timeout_ms": int(srv.get("tool_timeout_ms", 30000)),
                    "enabled_tools": enabled_tools,
                    "disabled_tools": disabled_tools,
                    "read_only_tools": [],
                    "discovered_tools": [],
                    "status": "disabled",
                    "status_message": "",
                    "last_tested_at": None,
                    "config_generation": gen,
                    "revision": gen,
                    "updated_at": now,
                }
                record["config_sha256"] = _mcp_config_fingerprint(record)
                state["mcp_servers"][mid] = record
                committed_servers.append(mid)

            for plugin in plugins:
                pid = plugin["id"]
                if pid == COMPUTER_USE_PLUGIN_ID:
                    raise AgentError("agent_plugin_builtin_conflict")
                gen = _alloc_generation(state)
                pkg_dir = plugin.get("package_dir")
                expected_manifest_sha256 = plugin.get("manifest_sha256")
                if pkg_dir:
                    manifest = compute_directory_manifest(Path(pkg_dir).resolve())
                    if (
                        expected_manifest_sha256
                        and manifest["manifest_sha256"] != expected_manifest_sha256
                    ):
                        raise AgentError("agent_extension_package_integrity_failed")
                    manifest_sha256 = manifest["manifest_sha256"]
                else:
                    manifest_sha256 = expected_manifest_sha256
                state["plugins"][pid] = {
                    "id": pid,
                    "instance_id": uuid.uuid4().hex,
                    "name": plugin.get("name") or pid,
                    "title": plugin.get("title") or plugin.get("name") or pid,
                    "description": plugin.get("description", ""),
                    "version": str(plugin.get("version", "1.0.0")),
                    "kind": "imported",
                    "source_format": plugin.get("source_format", "ryn_plugin"),
                    "builtin": False,
                    "deletable": True,
                    "enabled": False,
                    "computer_access": False,
                    "package_dir": pkg_dir,
                    "manifest_sha256": manifest_sha256,
                    "skill_ids": list(plugin.get("skill_ids", [])),
                    "mcp_server_ids": list(plugin.get("mcp_server_ids", [])),
                    "unsupported_components": list(plugin.get("unsupported_components", [])),
                    "warnings": list(plugin.get("warnings", [])),
                    "revision": gen,
                    "updated_at": now,
                }
                committed_plugins.append(pid)

            _alloc_generation(state)
            state["updated_at"] = now
            self._write(state)
            view = self.overview(desktop_access=desktop_access)
            return {
                "committed": {
                    "plugins": committed_plugins,
                    "skills": committed_skills,
                    "mcp_servers": committed_servers,
                },
                "overview": view,
            }

    def allocate_package_dir(self, category: str, item_id: str) -> Path:
        safe_cat = _slugify(category, "pkg")
        safe_id = _slugify(item_id, "item")
        stamp = f"{int(time.time() * 1000)}-{os.urandom(4).hex()}"
        dest = (self.pkgs_dir / safe_cat / safe_id / stamp).resolve()
        if self.pkgs_dir.resolve() not in dest.parents:
            raise AgentError("agent_extension_invalid_package_path")
        dest.parent.mkdir(parents=True, exist_ok=True)
        return dest

    def _safe_rmtree(self, target: Path) -> None:
        try:
            resolved = target.resolve()
            if self.pkgs_dir.resolve() in resolved.parents and resolved.exists():
                shutil.rmtree(resolved, ignore_errors=True)
        except Exception:
            pass
