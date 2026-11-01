"""Runtime tool adapter bridging ExtensionStore and McpSessionManager into RynTools."""

from __future__ import annotations

import base64
import binascii
import copy
import hashlib
import io
import re
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Callable

from jsonschema import ValidationError
from jsonschema import validate as jsonschema_validate

from ...atomic_io import atomic_write_bytes
from ...llm_package.chat import validate_tool_arguments
from ...services import peer_box
from .. import data_policy
from ..native_computer import NativeComputerFacade, bind_owner_native_computer
from ..store import AgentError, AgentToolRejected, encoded
from .imports import ExtensionImportManager
from .mcp_transport import (
    ALLOWLISTED_MCP_RPC_ERROR_CODES,
    McpSessionManager,
    McpTransportError,
)
from .store import (
    COMPUTER_USE_SERVER_ID,
    ExtensionStore,
    _canon_sha256,
    compute_model_tool_name,
)

ALLOWLISTED_DIAGNOSTIC_ERROR_CODES = ALLOWLISTED_MCP_RPC_ERROR_CODES | frozenset({
    "agent_mcp_command_required",
    "agent_mcp_executable_not_found",
    "agent_mcp_invalid_cwd",
    "agent_mcp_spawn_failed",
    "agent_mcp_output_limit",
    "agent_mcp_protocol_invalid",
    "agent_mcp_timeout",
    "agent_mcp_closed",
    "agent_mcp_request_too_large",
    "agent_mcp_write_failed",
    "agent_mcp_process_disconnected",
    "agent_mcp_notification_limit",
    "agent_mcp_protocol_unsupported",
    "agent_mcp_tools_limit",
    "agent_mcp_url_required",
    "agent_mcp_auth_required",
    "agent_mcp_http_error",
    "agent_mcp_transport_error",
    "agent_mcp_server_not_found",
    "agent_mcp_tool_returned_error",
    "agent_mcp_operation_not_executed",
    "agent_stale_revision",
    "agent_tool_definition_changed",
    "agent_disabled",
    "agent_extension_model_not_allowed",
    "agent_approval_mismatch",
    "agent_operation_id_conflict",
})

READ_SKILL_FILE_TOOL = "ryn_read_skill_file"

READ_SKILL_FILE_SPEC: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": READ_SKILL_FILE_TOOL,
        "description": (
            "Read SKILL.md or a package-relative reference, script, or asset text file "
            "from an enabled Ryn Skill package."
        ),
        "parameters": {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "skill_id": {
                    "type": "string",
                    "minLength": 1,
                    "maxLength": 64,
                    "description": "ID of the selected or enabled skill.",
                },
                "path": {
                    "type": "string",
                    "minLength": 1,
                    "maxLength": 240,
                    "description": "Package-relative file path to read (defaults to SKILL.md).",
                },
            },
        },
    },
}

ALLOWED_IMAGE_MIMES: dict[str, tuple[str, str]] = {
    "image/png": (".png", "PNG"),
    "image/jpeg": (".jpg", "JPEG"),
    "image/webp": (".webp", "WEBP"),
    "image/gif": (".gif", "GIF"),
}
MAX_MCP_IMAGE_BYTES = 4 * 1024 * 1024
MAX_MCP_IMAGE_PIXELS = 16_000_000
MAX_TEXT_BLOCK_CHARS = 32_000

_SAFE_PATH_SEGMENT_RE = re.compile(r"[^A-Za-z0-9_.-]+")


def _safe_segment(value: str, fallback: str = "item") -> str:
    cleaned = _SAFE_PATH_SEGMENT_RE.sub("_", str(value or "").strip()).strip("._")
    return (cleaned or fallback)[:64]


_IMAGE_DATA_KEYS = frozenset({"data", "base64", "image_base64", "bytes_base64", "raw_data"})


def _looks_like_image_base64(value: str, known_b64: set[str]) -> bool:
    s = value.strip()
    if not s:
        return False
    if s in known_b64 or s.lower().startswith("data:image/"):
        return True
    if len(s) < 16:
        return False
    # Check magic bytes of the base64 prefix (PNG, JPEG, GIF, WEBP)
    prefix = s[:64]
    pad = (-len(prefix)) % 4
    try:
        head = base64.b64decode(prefix + ("=" * pad), validate=False)
    except Exception:
        return False
    return (
        head.startswith(b"\x89PNG\r\n\x1a\n")
        or head.startswith(b"\xff\xd8\xff")
        or head.startswith((b"GIF87a", b"GIF89a"))
        or (len(head) >= 12 and head[:4] == b"RIFF" and head[8:12] == b"WEBP")
    )


def _strip_raw_images(
    value: Any,
    *,
    known_b64: set[str] | None = None,
    artifacts_by_b64: dict[str, dict[str, Any]] | None = None,
    depth: int = 0,
) -> Any:
    if depth > 12:
        return None
    known = known_b64 or set()
    by_b64 = artifacts_by_b64 or {}
    if isinstance(value, dict):
        mime = str(
            value.get("mimeType")
            or value.get("mime_type")
            or value.get("contentType")
            or value.get("content_type")
            or ""
        ).strip().lower()
        is_image_dict = value.get("type") == "image" or mime.startswith("image/")
        out: dict[str, Any] = {}
        matched_art: dict[str, Any] | None = None
        for k, v in value.items():
            ks = str(k)
            if isinstance(v, str):
                vs = v.strip()
                if (
                    (is_image_dict and ks in _IMAGE_DATA_KEYS)
                    or (ks in _IMAGE_DATA_KEYS and _looks_like_image_base64(vs, known))
                    or vs in known
                    or vs.lower().startswith("data:image/")
                ):
                    if vs in by_b64:
                        matched_art = by_b64[vs]
                    out[ks] = None
                    continue
                if ks in _IMAGE_DATA_KEYS and len(v) > MAX_TEXT_BLOCK_CHARS:
                    out[ks] = None
                    continue
            out[ks] = _strip_raw_images(
                v, known_b64=known, artifacts_by_b64=by_b64, depth=depth + 1
            )
        if matched_art is not None:
            for ak in ("path", "sha256", "bytes", "mime_type", "width", "height"):
                out.setdefault(ak, matched_art[ak])
        return out
    if isinstance(value, list):
        return [
            _strip_raw_images(item, known_b64=known, artifacts_by_b64=by_b64, depth=depth + 1)
            for item in value[:200]
        ]
    if isinstance(value, str):
        vs = value.strip()
        if vs in known or vs.lower().startswith("data:image/"):
            return None
        if len(value) > MAX_TEXT_BLOCK_CHARS:
            return value[:MAX_TEXT_BLOCK_CHARS] + "...[truncated]"
    return value


class ExtensionToolAdapter:
    """Exposes enabled MCP server tools and ryn_read_skill_file through RynTools."""

    def __init__(
        self,
        local_tools: Any,
        ledger_factory: Callable[[], Any],
        *,
        node_store: Any = None,
        extension_store: ExtensionStore | None = None,
        import_manager: ExtensionImportManager | None = None,
        session_manager: McpSessionManager | None = None,
        native_computer: NativeComputerFacade | None = None,
    ) -> None:
        self.local = local_tools
        self.ledger_factory = ledger_factory
        self._node_store = node_store
        self._store = extension_store
        self._imports = import_manager
        self._manager = session_manager
        self._custom_session_manager = session_manager is not None
        owner_init = (
            getattr(local_tools, "store", None)
            or node_store
            or local_tools
            or extension_store
            or self
        )
        if isinstance(native_computer, NativeComputerFacade) and native_computer._owner is owner_init:
            self._native_computer: NativeComputerFacade | None = native_computer
        else:
            self._native_computer = None
        self._bound_root: Path | None = extension_store.root.resolve() if extension_store else None
        self._lock = threading.RLock()
        self._op_guard = threading.Lock()
        self._op_locks: dict[str, list[Any]] = {}

    def _resolve_owner(self) -> Any:
        return (
            getattr(self.local, "store", None)
            or self._node_store
            or self.local
            or self._store
            or self
        )

    @property
    def native_computer(self) -> NativeComputerFacade:
        with self._lock:
            owner = self._resolve_owner()
            if self._native_computer is None or self._native_computer._owner is not owner:
                self._native_computer = bind_owner_native_computer(
                    owner, local_tools=self.local
                )
            return self._native_computer

    def _should_route_builtin_to_native_computer(self) -> bool:
        return True

    def bind_agent_store(self, agent_store: Any) -> None:
        """Bind or rebind to the authoritative AgentStore root and messaging key."""
        if agent_store is None:
            return
        root = getattr(agent_store, "root", None)
        key = getattr(agent_store, "key", None) or getattr(agent_store, "messaging_key", None)
        if root is None:
            home = getattr(agent_store, "home", None)
            if home is None:
                return
            home_path = Path(home).resolve()
            root = home_path / "agent"
            if key is None:
                key = peer_box.load_or_create_messaging_key(home_path / "messaging.x25519")
        resolved_root = Path(root).resolve()
        if key is None:
            key = peer_box.load_or_create_messaging_key(resolved_root.parent / "messaging.x25519")
        target_pub = getattr(agent_store, "pub", None) or peer_box.public_key_b64(key)
        with self._lock:
            self._native_computer = bind_owner_native_computer(
                agent_store, local_tools=self.local
            )
            if (
                self._store is not None
                and self._bound_root == resolved_root
                and self._store.pub == target_pub
            ):
                return
            old_manager = self._manager
            self._store = ExtensionStore(resolved_root, key)
            self._imports = ExtensionImportManager(self._store)
            self._manager = McpSessionManager(self._store)
            self._custom_session_manager = False
            self._bound_root = resolved_root
            if old_manager is not None:
                try:
                    old_manager.close()
                except Exception:
                    pass

    def _ensure_initialized(self) -> None:
        with self._lock:
            local_store = getattr(self.local, "store", None)
            if local_store is not None and getattr(local_store, "root", None) is not None:
                self.bind_agent_store(local_store)
                return
            if self._store is not None:
                return
            if local_store is not None:
                self.bind_agent_store(local_store)
                if self._store is not None:
                    return
            if self._node_store is not None:
                self.bind_agent_store(self._node_store)

    @property
    def store(self) -> ExtensionStore:
        self._ensure_initialized()
        assert self._store is not None
        return self._store

    @property
    def imports(self) -> ExtensionImportManager:
        self._ensure_initialized()
        if self._imports is None:
            self._imports = ExtensionImportManager(self.store)
        return self._imports

    @property
    def manager(self) -> McpSessionManager:
        self._ensure_initialized()
        if self._manager is None:
            self._manager = McpSessionManager(self.store)
        return self._manager

    def overview(self, *, desktop_access: bool = False) -> dict[str, Any]:
        value = self.store.overview(desktop_access=desktop_access)
        oauth = getattr(self.manager, "oauth", None)
        if oauth is None:
            return value
        statuses = {}
        for server in value.get("mcp_servers", []):
            state = oauth.public_status(str(server.get("instance_id") or ""))
            server["oauth"] = state
            if server.get("enabled") and state.get("state") in {"logged_out", "pending", "error"}:
                server.update(status="NeedsAuth", status_message="agent_mcp_auth_required")
            statuses[server["id"]] = {key: server.get(key) for key in ("oauth", "status", "status_message")}
        for plugin in value.get("plugins", []):
            for server in plugin.get("mcp_servers", []):
                if server["id"] in statuses:
                    server.update(statuses[server["id"]])
        return value

    def close(self) -> None:
        with self._lock:
            mgr = self._manager
            nc = self._native_computer or getattr(self.local, "native_computer", None)
        if mgr is not None:
            mgr.close()
        if nc is not None:
            nc.close()

    def forget_task(self, task_id: str, *, operation_ids: Any = ()) -> None:
        with self._lock:
            mgr = self._manager
            nc = self._native_computer or getattr(self.local, "native_computer", None)
        if mgr is not None:
            mgr.forget_task(task_id)
        if nc is not None:
            nc.forget_task(task_id)
        self.ledger_factory().forget_task(task_id)

    @contextmanager
    def _operation_lock(self, operation_id: str):
        with self._op_guard:
            entry = self._op_locks.setdefault(operation_id, [threading.Lock(), 0])
            entry[1] += 1
        try:
            with entry[0]:
                yield
        finally:
            with self._op_guard:
                entry[1] -= 1
                if not entry[1]:
                    self._op_locks.pop(operation_id, None)

    def handles(self, name: str) -> bool:
        return isinstance(name, str) and (name == READ_SKILL_FILE_TOOL or name.startswith("mcp__"))

    def _active_mcp_tools(self) -> dict[str, tuple[dict[str, Any], dict[str, Any], dict[str, Any]]]:
        """Return mapping of model_tool_name -> (server, tool, extension_binding) for enabled tools."""
        out: dict[str, tuple[dict[str, Any], dict[str, Any], dict[str, Any]]] = {}
        for server in self.store.list_enabled_mcp_servers_internal():
            oauth = getattr(self.manager, "oauth", None)
            if oauth is not None and oauth.public_status(str(server.get("instance_id") or "")).get("state") in {"logged_out", "pending", "error"}:
                continue
            enabled_tools = server.get("enabled_tools")
            disabled_tools = set(server.get("disabled_tools") or [])
            read_only_tools = set(server.get("read_only_tools") or [])
            for tool in server.get("discovered_tools") or []:
                if not isinstance(tool, dict):
                    continue
                t_name = str(tool.get("name") or "").strip()
                if not t_name:
                    continue
                if enabled_tools is not None and t_name not in enabled_tools:
                    continue
                if t_name in disabled_tools:
                    continue
                if not tool.get("enabled", True):
                    continue
                model_name = str(
                    tool.get("model_tool_name") or compute_model_tool_name(server["id"], t_name)
                )
                owner_read_only = t_name in read_only_tools
                effective_effect = "read" if owner_read_only else "write"
                effective_risk = "low" if owner_read_only else "high"
                schema = (
                    tool.get("input_schema")
                    if isinstance(tool.get("input_schema"), dict)
                    else {"type": "object"}
                )
                schema_sha256 = str(tool.get("schema_sha256") or _canon_sha256(schema))
                binding: dict[str, Any] = {
                    "server_id": str(server["id"]),
                    "server_name": str(server.get("name") or server["id"]),
                    "original_tool_name": t_name,
                    "model_tool_name": model_name,
                    "instance_id": str(server.get("instance_id") or ""),
                    "revision": int(server.get("revision", 1)),
                    "config_generation": int(server.get("config_generation", 1)),
                    "schema_sha256": schema_sha256,
                    "effective_effect": effective_effect,
                    "effective_risk": effective_risk,
                    "plugin_id": server.get("plugin_id"),
                    "plugin_revision": server.get("plugin_revision"),
                    "computer_access": bool(server.get("computer_access", False)),
                }
                out[model_name] = (server, tool, binding)
        return out

    def specs(self, context: dict[str, Any] | None = None) -> list[dict[str, Any]]:
        result: list[dict[str, Any]] = [copy.deepcopy(READ_SKILL_FILE_SPEC)]
        active = self._active_mcp_tools()
        for model_name in sorted(active.keys()):
            server, tool, _binding = active[model_name]
            if not data_policy.allows(server.get("data_policy"), context):
                continue
            schema = (
                copy.deepcopy(tool["input_schema"])
                if isinstance(tool.get("input_schema"), dict)
                else {"type": "object"}
            )
            prefix = f"[{server.get('name') or server['id']}] "
            raw_desc = str(tool.get("description") or tool.get("title") or tool.get("name") or "").strip()
            result.append(
                {
                    "type": "function",
                    "function": {
                        "name": model_name,
                        "description": (prefix + raw_desc)[:600],
                        "parameters": schema,
                    },
                }
            )
        return result

    def _check_data_policy(self, binding, context, *, persist=False):
        server = self.store.get_mcp_server_internal(
            binding["server_id"], require_enabled=True,
            expected_instance_id=binding["instance_id"],
            expected_config_generation=binding["config_generation"])
        policy = data_policy.normalize_policy(server.get("data_policy"))
        if not data_policy.allows(policy, context):
            raise AgentToolRejected("agent_extension_model_not_allowed")
        if policy["models"]["mode"] == "any" and policy["memory"] == "allow":
            return
        admit = context.get("_admit_extension_policy") if isinstance(context, dict) else None
        if not callable(admit):
            raise AgentToolRejected("agent_extension_model_not_allowed")
        admit(data_policy.snapshot_source(server), persist=persist)

    def metadata(self, name: str) -> dict[str, Any]:
        if name == READ_SKILL_FILE_TOOL:
            return {
                "effect": "read",
                "title": "Read skill file",
                "risk": "low",
                "scope_context_supported": True,
            }
        entry = self._active_mcp_tools().get(name)
        if entry is None:
            return {
                "effect": "write",
                "title": name,
                "risk": "high",
                "scope_context_supported": True,
                "supports_preview": True,
                "preview_precondition": True,
                "unavailable": True,
            }
        server, tool, binding = entry
        eff = binding["effective_effect"]
        # Only the verified bundled provider inherits workstation permission.
        # An external MCP server's computer_access annotation is not authority.
        workstation_tool = None
        if binding["server_id"] == COMPUTER_USE_SERVER_ID:
            if self.store.verify_builtin_computer_use_authority(binding) is not None:
                workstation_tool = "ryn_computer_action"
        return {
            "effect": eff,
            "title": f"{server.get('name') or server['id']}: {tool.get('title') or tool.get('name')}",
            "risk": binding["effective_risk"],
            "scope_context_supported": True,
            "supports_preview": eff == "write",
            "preview_precondition": eff == "write",
            "computer_access": bool(binding["computer_access"]),
            "extension_binding": copy.deepcopy(binding),
            "workstation_permission_tool": workstation_tool,
        }

    def validate(self, name: str, arguments: Any) -> None:
        if not isinstance(arguments, dict):
            raise ValueError("Tool arguments must be a JSON object.")
        if name == READ_SKILL_FILE_TOOL:
            jsonschema_validate(arguments, READ_SKILL_FILE_SPEC["function"]["parameters"])
            return
        entry = self._active_mcp_tools().get(name)
        if entry is None:
            raise AgentToolRejected("agent_unknown_tool")
        _server, tool, _binding = entry
        schema = (
            tool.get("input_schema")
            if isinstance(tool.get("input_schema"), dict)
            else {"type": "object"}
        )
        validate_tool_arguments(schema, arguments)
        jsonschema_validate(arguments, schema)

    def verify_binding(self, call_or_binding: dict[str, Any]) -> dict[str, Any]:
        """Verify that a queued MCP tool call's extension_binding still matches current state."""
        if not isinstance(call_or_binding, dict):
            raise AgentToolRejected("agent_tool_definition_changed")
        if call_or_binding.get("name") == READ_SKILL_FILE_TOOL:
            return {}
        expected = (
            call_or_binding.get("extension_binding")
            if "extension_binding" in call_or_binding
            else call_or_binding
        )
        if (
            not isinstance(expected, dict)
            or not expected.get("server_id")
            or not expected.get("model_tool_name")
        ):
            raise AgentToolRejected("agent_tool_definition_changed")
        self.store.verify_builtin_computer_use_authority(expected)
        entry = self._active_mcp_tools().get(str(expected["model_tool_name"]))
        if entry is None:
            try:
                self.store.get_mcp_server_internal(
                    str(expected["server_id"]),
                    require_enabled=True,
                    expected_revision=expected.get("revision"),
                    expected_config_generation=expected.get("config_generation"),
                    expected_instance_id=expected.get("instance_id"),
                )
            except AgentError as exc:
                if str(exc) == "agent_mcp_server_disabled":
                    raise AgentToolRejected("agent_mcp_server_disabled") from None
            raise AgentToolRejected("agent_tool_definition_changed")

        _server, _tool, current_binding = entry
        for key in (
            "server_id",
            "original_tool_name",
            "model_tool_name",
            "instance_id",
            "revision",
            "config_generation",
            "schema_sha256",
            "effective_effect",
            "plugin_id",
            "plugin_revision",
            "computer_access",
        ):
            if expected.get(key) != current_binding.get(key):
                raise AgentToolRejected("agent_tool_definition_changed")
        self.store.verify_builtin_computer_use_authority(current_binding)
        return copy.deepcopy(current_binding)

    def _check_computer_access(self, context: dict[str, Any]) -> dict[str, Any]:
        policy = self.local.policy(context)
        if not policy.get("desktop_access"):
            raise AgentToolRejected("agent_desktop_access_required")
        store = getattr(self.local, "store", None)
        task_id = context.get("task_id")
        if store is not None and hasattr(store, "read_control") and task_id:
            ctrl = store.read_control(task_id)
            settings = ctrl.get("settings") or {}
            row = (ctrl.get("tasks") or {}).get(task_id) or {}
            if not settings.get("desktop_access") or not (row.get("local_execution") or {}).get(
                "desktop_access"
            ):
                raise AgentToolRejected("agent_desktop_access_required")
        return policy

    def preview(
        self,
        name: str,
        arguments: dict[str, Any],
        context: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        if not isinstance(context, dict) or not context.get("task_id"):
            raise AgentError("agent_scope_context_required")
        entry = self._active_mcp_tools().get(name)
        if entry is None:
            if context.get("extension_binding"):
                self.verify_binding(context["extension_binding"])
            raise AgentToolRejected("agent_unknown_tool")
        server, tool, binding = entry
        if context.get("extension_binding"):
            binding = self.verify_binding(context["extension_binding"])
        self._check_data_policy(binding, context)
        builtin_auth = self.store.verify_builtin_computer_use_authority(binding)
        try:
            self.validate(name, arguments)
        except (ValidationError, ValueError):
            raise AgentToolRejected("agent_tool_arguments_invalid") from None

        policy = (
            self._check_computer_access(context)
            if binding["computer_access"]
            else self.local.policy(context)
        )
        if (
            builtin_auth is not None
            and self._should_route_builtin_to_native_computer()
            and binding["effective_effect"] == "write"
        ):
            auth_server, _auth_plugin, auth_tool = builtin_auth
            return self.native_computer.preview_mcp_write(
                binding["original_tool_name"],
                arguments,
                binding=binding,
                policy=policy,
                server=auth_server,
                tool=auth_tool,
                context=context,
            )
        return {
            "title": f"{server.get('name') or server['id']}: {tool.get('title') or tool.get('name')}",
            "server_id": binding["server_id"],
            "server_name": binding["server_name"],
            "tool_name": binding["original_tool_name"],
            "model_tool_name": binding["model_tool_name"],
            "workspace_root": policy.get("workspace_root", ""),
            "desktop_access": bool(policy.get("desktop_access", False)),
            "computer_access": bool(binding["computer_access"]),
            "operation": copy.deepcopy(arguments),
            "extension_binding": copy.deepcopy(binding),
        }

    def _normalize_mcp_result(
        self,
        raw_result: dict[str, Any],
        *,
        binding: dict[str, Any],
        context: dict[str, Any],
        operation_id: str | None,
        allow_image_rejection: bool = True,
    ) -> dict[str, Any]:
        is_error = bool(raw_result.get("isError", False))
        raw_blocks = raw_result.get("content")
        blocks = raw_blocks if isinstance(raw_blocks, list) else []
        text_parts: list[str] = []
        normalized_blocks: list[dict[str, Any]] = []
        artifacts: list[dict[str, Any]] = []
        seen_b64: set[str] = set()
        artifacts_by_b64: dict[str, dict[str, Any]] = {}

        safe_task = _safe_segment(str(context.get("task_id") or "task"), "task")
        safe_op = _safe_segment(str(operation_id or "op"), "op")
        image_idx = 0
        img_cap = context.get("_image_capability") if isinstance(context, dict) else None
        route_supports_images = bool(isinstance(img_cap, dict) and img_cap.get("supports_image_input"))

        for block in blocks[:64]:
            if not isinstance(block, dict):
                continue
            b_type = str(block.get("type") or "").strip()
            if b_type == "text":
                txt = str(block.get("text") or "")[:MAX_TEXT_BLOCK_CHARS]
                text_parts.append(txt)
                normalized_blocks.append({"type": "text", "text": txt})
            elif b_type == "image":
                mime = str(block.get("mimeType") or block.get("mime_type") or "").strip().lower()
                b64_data = block.get("data")
                if isinstance(b64_data, str) and b64_data.strip():
                    seen_b64.add(b64_data.strip())
                try:
                    if mime not in ALLOWED_IMAGE_MIMES:
                        raise ValueError("unsupported_mime")
                    if not isinstance(b64_data, str) or not b64_data.strip():
                        raise ValueError("empty_image")
                    cleaned = b64_data.strip()
                    if len(cleaned) > (MAX_MCP_IMAGE_BYTES * 4) // 3 + 256:
                        raise ValueError("image_too_large")
                    raw_bytes = base64.b64decode(cleaned, validate=True)
                    if not raw_bytes or len(raw_bytes) > MAX_MCP_IMAGE_BYTES:
                        raise ValueError("image_too_large")

                    from PIL import Image, UnidentifiedImageError

                    try:
                        with Image.open(io.BytesIO(raw_bytes)) as img:
                            img.verify()
                        with Image.open(io.BytesIO(raw_bytes)) as img:
                            width, height = img.size
                            fmt = (img.format or "").upper()
                    except (OSError, UnidentifiedImageError, Image.DecompressionBombError) as exc:
                        raise ValueError("invalid_image") from exc

                    ext, expected_fmt = ALLOWED_IMAGE_MIMES[mime]
                    if (
                        width <= 0
                        or height <= 0
                        or width * height > MAX_MCP_IMAGE_PIXELS
                        or fmt != expected_fmt
                    ):
                        raise ValueError("invalid_image_dimensions_or_format")

                    rel_path = f".ryn/mcp/{safe_task}/{safe_op}/image_{image_idx}{ext}"
                    image_idx += 1
                    target_path = self.local.path(rel_path, context)
                    target_path.parent.mkdir(parents=True, exist_ok=True)
                    atomic_write_bytes(target_path, raw_bytes)
                    file_sha = hashlib.sha256(raw_bytes).hexdigest()
                    if hasattr(self.local, "_record_observed_files"):
                        self.local._record_observed_files(context, {target_path: file_sha})

                    art_entry = {
                        "path": rel_path,
                        "sha256": file_sha,
                        "bytes": len(raw_bytes),
                        "mime_type": mime,
                        "width": width,
                        "height": height,
                    }
                    artifacts.append(art_entry)
                    artifacts_by_b64[cleaned] = art_entry
                    normalized_blocks.append(
                        {
                            "type": "image",
                            **art_entry,
                            "note": (
                                "Saved to workspace file; verified operation screenshot pixels are attached to the active turn on image-capable routes."
                                if route_supports_images
                                else "Saved to workspace file; pixels have not been visually inspected by the model yet. Call ryn_image_inspect on path if visual inspection is needed."
                            ),
                        }
                    )
                except (binascii.Error, ValueError, AgentError):
                    if allow_image_rejection:
                        raise AgentError("agent_mcp_image_invalid") from None
                    normalized_blocks.append(
                        {
                            "type": "image",
                            "error": "agent_mcp_image_invalid",
                            "note": "Image payload was invalid or exceeded safety limits and was omitted.",
                        }
                    )
            elif b_type == "resource":
                # Never dereference local file URIs from external MCP servers.
                res_obj = block.get("resource") if isinstance(block.get("resource"), dict) else {}
                inline_text = res_obj.get("text")
                if isinstance(inline_text, str) and inline_text:
                    clipped = inline_text[:MAX_TEXT_BLOCK_CHARS]
                    text_parts.append(clipped)
                    normalized_blocks.append({"type": "text", "text": clipped})

        clean_structured = _strip_raw_images(
            raw_result.get("structuredContent"),
            known_b64=seen_b64,
            artifacts_by_b64=artifacts_by_b64,
        )
        normalized: dict[str, Any] = {
            "server_id": binding["server_id"],
            "tool": binding["original_tool_name"],
            "model_tool_name": binding["model_tool_name"],
            "is_error": is_error,
            "content": normalized_blocks,
            "text": "\n".join(text_parts),
        }
        if is_error:
            normalized["error"] = "agent_mcp_tool_returned_error"
        if isinstance(clean_structured, dict):
            normalized["structuredContent"] = clean_structured
        if artifacts:
            normalized["mcp_artifacts"] = artifacts
            normalized["images"] = copy.deepcopy(artifacts)
            normalized["image_path"] = artifacts[0]["path"]
            normalized["image_note"] = (
                "Image bytes were saved to the workspace and verified operation screenshot pixels are attached to the active turn on image-capable routes."
                if route_supports_images
                else "Image bytes were saved to the workspace and have not been visually inspected yet. Use ryn_image_inspect with the saved path if the selected model supports image input."
            )
        return normalized

    def execute(
        self,
        name: str,
        arguments: dict[str, Any],
        *,
        operation_id: str | None = None,
        context: dict[str, Any] | None = None,
        expected_preview: dict[str, Any] | None = None,
        cancelled: Callable[[], bool] = lambda: False,
    ) -> dict[str, Any]:
        if cancelled():
            raise AgentToolRejected(getattr(cancelled, "reason", None) or "agent_cancelled")
        if not isinstance(context, dict) or not context.get("task_id"):
            raise AgentError("agent_scope_context_required")

        if name == READ_SKILL_FILE_TOOL:
            self.local.policy(context)
            try:
                self.validate(name, arguments)
            except (ValidationError, ValueError):
                raise AgentToolRejected("agent_tool_arguments_invalid") from None
            skill_id = str(arguments.get("skill_id") or "").strip()
            selected_skills = context.get("selected_skills") or []
            if not skill_id:
                if (
                    len(selected_skills) == 1
                    and isinstance(selected_skills[0], dict)
                    and selected_skills[0].get("id")
                ):
                    skill_id = str(selected_skills[0]["id"])
                else:
                    raise AgentToolRejected("agent_skill_id_required")
            rel_path = str(arguments.get("path") or "SKILL.md").strip() or "SKILL.md"
            matched_snapshot = next(
                (s for s in selected_skills if isinstance(s, dict) and s.get("id") == skill_id),
                None,
            )
            try:
                return self.store.read_skill_file(skill_id, rel_path, snapshot=matched_snapshot)
            except AgentError as exc:
                raise AgentToolRejected(str(exc)) from None

        if context.get("extension_binding"):
            binding = self.verify_binding(context["extension_binding"])
        else:
            entry = self._active_mcp_tools().get(name)
            if entry is None:
                raise AgentToolRejected("agent_unknown_tool")
            binding = entry[2]

        self._check_data_policy(binding, context)

        builtin_auth = self.store.verify_builtin_computer_use_authority(binding)
        route_to_native = (
            builtin_auth is not None and self._should_route_builtin_to_native_computer()
        )

        try:
            self.validate(name, arguments)
        except (ValidationError, ValueError):
            raise AgentToolRejected("agent_tool_arguments_invalid") from None

        if binding["computer_access"]:
            self._check_computer_access(context)
        else:
            self.local.policy(context)

        session_scope = (
            str(context["task_id"])
            if (binding["server_id"] == COMPUTER_USE_SERVER_ID or binding["computer_access"])
            else None
        )

        if binding["effective_effect"] == "read":
            def _before_read_dispatch() -> None:
                if cancelled():
                    raise AgentToolRejected(getattr(cancelled, "reason", None) or "agent_cancelled")
                self.verify_binding(binding)
                self._check_data_policy(binding, context, persist=True)
                self.store.verify_builtin_computer_use_authority(binding)
                if binding["computer_access"]:
                    self._check_computer_access(context)
                else:
                    self.local.policy(context)

            if route_to_native:
                _before_read_dispatch()
                return self.native_computer.execute_mcp_read(
                    binding["original_tool_name"],
                    arguments,
                    binding=binding,
                    operation_id=operation_id,
                    context=context,
                    cancelled=cancelled,
                )

            try:
                raw_res = self.manager.call_tool(
                    binding["server_id"],
                    binding["original_tool_name"],
                    arguments,
                    expected_revision=binding["revision"],
                    expected_config_generation=binding["config_generation"],
                    expected_instance_id=binding["instance_id"],
                    expected_schema_hash=binding["schema_sha256"],
                    session_scope=session_scope,
                    before_dispatch=_before_read_dispatch,
                    cancelled=cancelled,
                )
            except AgentToolRejected:
                raise
            except (McpTransportError, AgentError) as exc:
                raw_code = str(exc)
                code = (
                    raw_code
                    if raw_code in ALLOWLISTED_DIAGNOSTIC_ERROR_CODES
                    else "agent_mcp_transport_error"
                )
                if getattr(exc, "dispatched", None) is False:
                    if code == "agent_stale_revision":
                        code = "agent_tool_definition_changed"
                    raise AgentToolRejected(code) from None
                raise AgentError(code) from None

            return self._normalize_mcp_result(
                raw_res,
                binding=binding,
                context=context,
                operation_id=operation_id,
                allow_image_rejection=True,
            )

        # Write tool path: requires preview match and durable OperationLedger transitions
        if not operation_id:
            raise AgentError("agent_operation_id_required")

        args_sha = hashlib.sha256(encoded(arguments)).hexdigest()
        ledger_binding = {
            "operation_id": operation_id,
            "tool": name,
            "task_id": context["task_id"],
            "arguments_sha256": args_sha,
            "local_execution": copy.deepcopy(context.get("local_execution") or {}),
            "extension_binding": copy.deepcopy(binding),
        }

        with self._operation_lock(operation_id):
            ledger = self.ledger_factory()
            prior = ledger.get(operation_id)
            if prior is not None:
                if prior.get("binding") != ledger_binding:
                    raise AgentError("agent_operation_id_conflict")
                prior_res = prior.get("result")
                if (
                    prior.get("status") == "confirmed"
                    and isinstance(prior_res, dict)
                    and not (prior_res.get("is_error") or prior_res.get("error"))
                ):
                    return copy.deepcopy(prior_res)
                if prior.get("status") == "not_executed":
                    raise AgentToolRejected(
                        str(prior.get("error") or "agent_mcp_operation_not_executed")
                    )
                unknown_err = AgentError("agent_write_outcome_unknown")
                if isinstance(prior.get("error_result"), dict):
                    unknown_err.diagnostics = copy.deepcopy(prior["error_result"])  # type: ignore[attr-defined]
                raise unknown_err

            current_preview = self.preview(
                name,
                arguments,
                context={**context, "extension_binding": binding},
            )
            if expected_preview is not None and current_preview != expected_preview:
                raise AgentToolRejected("agent_approval_preview_changed")

            prior, created = ledger.begin(ledger_binding, current_preview, receipt_reserve=32768)
            if not created:
                if prior.get("binding") != ledger_binding:
                    raise AgentError("agent_operation_id_conflict")
                prior_res = prior.get("result")
                if (
                    prior.get("status") == "confirmed"
                    and isinstance(prior_res, dict)
                    and not (prior_res.get("is_error") or prior_res.get("error"))
                ):
                    return copy.deepcopy(prior_res)
                if prior.get("status") == "not_executed":
                    raise AgentToolRejected(
                        str(prior.get("error") or "agent_mcp_operation_not_executed")
                    )
                unknown_err = AgentError("agent_write_outcome_unknown")
                if isinstance(prior.get("error_result"), dict):
                    unknown_err.diagnostics = copy.deepcopy(prior["error_result"])  # type: ignore[attr-defined]
                raise unknown_err

            if route_to_native:
                def _verify_native_binding(expected_binding: dict[str, Any]) -> None:
                    self.verify_binding(expected_binding)
                    self.store.verify_builtin_computer_use_authority(expected_binding)
                    self._check_computer_access(context)

                def _on_ready_native_gate(_validated_ready: dict[str, Any]) -> None:
                    self._check_data_policy(binding, context, persist=True)
                    ledger.update(operation_id, "dispatched", receipt_reserve=32768)

                try:
                    normalized = self.native_computer.execute_mcp_write(
                        binding["original_tool_name"],
                        arguments,
                        binding=binding,
                        operation_id=operation_id,
                        approved_preview=current_preview,
                        context=context,
                        cancelled=cancelled,
                        on_ready_host_gate=_on_ready_native_gate,
                        verify_binding_fn=_verify_native_binding,
                    )
                except AgentToolRejected as exc:
                    cur_row = ledger.get(operation_id) or {}
                    if getattr(exc, "input_not_delivered", False) or (
                        cur_row.get("status") == "intent"
                        and getattr(exc, "pre_dispatch", True)
                        and not getattr(exc, "dispatched", False)
                        and not getattr(exc, "commit_attempted", False)
                    ):
                        ledger.update(
                            operation_id,
                            "not_executed",
                            error=str(exc),
                            error_result=getattr(exc, "diagnostics", None)
                            if isinstance(getattr(exc, "diagnostics", None), dict)
                            else None,
                            receipt_reserve=0,
                        )
                        raise
                    diag = (
                        copy.deepcopy(getattr(exc, "diagnostics", None))
                        if isinstance(getattr(exc, "diagnostics", None), dict)
                        else {"error": str(exc), "phase": "post_dispatch", "dispatched": True}
                    )
                    ledger.update(
                        operation_id,
                        "dispatched",
                        error=str(exc),
                        error_result=diag,
                        receipt_reserve=0,
                    )
                    unknown_err = AgentError("agent_write_outcome_unknown")
                    unknown_err.diagnostics = copy.deepcopy(diag)  # type: ignore[attr-defined]
                    raise unknown_err from None
                except AgentError as exc:
                    diag = (
                        copy.deepcopy(getattr(exc, "diagnostics", None))
                        if isinstance(getattr(exc, "diagnostics", None), dict)
                        else {
                            "error": str(getattr(exc, "cause_code", None) or exc),
                            "phase": "post_dispatch",
                            "dispatched": True,
                        }
                    )
                    cause_code = str(getattr(exc, "cause_code", None) or diag.get("error") or exc)
                    if not getattr(exc, "callback_uncertain", False):
                        ledger.update(
                            operation_id,
                            "dispatched",
                            error=cause_code,
                            error_result=diag,
                            receipt_reserve=0,
                        )
                    else:
                        cur_row = ledger.get(operation_id) or {}
                        if cur_row.get("status") in {"intent", "dispatched"}:
                            ledger.update(
                                operation_id,
                                str(cur_row["status"]),
                                error=cause_code,
                                error_result=diag,
                                receipt_reserve=0,
                            )
                    unknown_err = AgentError("agent_write_outcome_unknown")
                    unknown_err.diagnostics = copy.deepcopy(diag)  # type: ignore[attr-defined]
                    raise unknown_err from None
                except BaseException as exc:
                    cur_row = ledger.get(operation_id) or {}
                    if cur_row.get("status") == "intent":
                        ledger.update(
                            operation_id,
                            "not_executed",
                            error="agent_mcp_operation_not_executed",
                            receipt_reserve=0,
                        )
                        if isinstance(exc, (KeyboardInterrupt, SystemExit)):
                            raise
                        raise AgentToolRejected("agent_mcp_operation_not_executed") from None
                    diag = {
                        "error": "agent_computer_bridge_protocol_error",
                        "exception_type": type(exc).__name__[:64],
                        "phase": "post_dispatch",
                        "dispatched": True,
                    }
                    ledger.update(
                        operation_id,
                        "dispatched",
                        error="agent_computer_bridge_protocol_error",
                        error_result=diag,
                        receipt_reserve=0,
                    )
                    if isinstance(exc, (KeyboardInterrupt, SystemExit)):
                        raise
                    unknown_err = AgentError("agent_write_outcome_unknown")
                    unknown_err.diagnostics = copy.deepcopy(diag)  # type: ignore[attr-defined]
                    raise unknown_err from None

                ledger.update(operation_id, "confirmed", result=normalized)
                return normalized

            passed_pre_dispatch = False

            def _before_write_dispatch() -> None:
                nonlocal passed_pre_dispatch
                if cancelled():
                    raise AgentToolRejected(getattr(cancelled, "reason", None) or "agent_cancelled")
                self.verify_binding(binding)
                self._check_data_policy(binding, context, persist=True)
                if binding["computer_access"]:
                    self._check_computer_access(context)
                else:
                    self.local.policy(context)
                ledger.update(operation_id, "dispatched", receipt_reserve=32768)
                passed_pre_dispatch = True

            try:
                raw_res = self.manager.call_tool(
                    binding["server_id"],
                    binding["original_tool_name"],
                    arguments,
                    expected_revision=binding["revision"],
                    expected_config_generation=binding["config_generation"],
                    expected_instance_id=binding["instance_id"],
                    expected_schema_hash=binding["schema_sha256"],
                    session_scope=session_scope,
                    before_dispatch=_before_write_dispatch,
                    cancelled=cancelled,
                )
            except BaseException as exc:
                if not passed_pre_dispatch:
                    if isinstance(exc, (KeyboardInterrupt, SystemExit)):
                        ledger.update(
                            operation_id,
                            "not_executed",
                            error="agent_mcp_operation_not_executed",
                        )
                        raise
                    raw_code = str(exc) if isinstance(exc, AgentError) else ""
                    code = (
                        raw_code
                        if raw_code in ALLOWLISTED_DIAGNOSTIC_ERROR_CODES
                        else "agent_mcp_operation_not_executed"
                    )
                    if code == "agent_stale_revision":
                        code = "agent_tool_definition_changed"
                    ledger.update(operation_id, "not_executed", error=code)
                    raise AgentToolRejected(code) from None

                raw_code = str(exc) if isinstance(exc, AgentError) else ""
                safe_code = (
                    raw_code
                    if raw_code in ALLOWLISTED_DIAGNOSTIC_ERROR_CODES
                    else "agent_mcp_transport_error"
                )
                raw_stage = getattr(exc, "phase", None)
                safe_stage = (
                    str(raw_stage)
                    if raw_stage
                    in {
                        "pre_dispatch",
                        "send",
                        "await_response",
                        "rpc_error",
                        "http_status",
                        "response_parse",
                        "initialize",
                    }
                    else "post_dispatch"
                )
                diagnostic: dict[str, Any] = {
                    "error": safe_code,
                    "exception_type": type(exc).__name__[:64],
                    "phase": "post_dispatch",
                    "stage": safe_stage,
                    "method": "tools/call",
                    "dispatched": bool(getattr(exc, "dispatched", True)),
                    "operation_id": str(operation_id)[:128],
                    "server_id": str(binding["server_id"])[:64],
                    "tool": str(binding["original_tool_name"])[:64],
                    "model_tool_name": str(binding["model_tool_name"])[:64],
                    "instance_id": str(binding["instance_id"])[:64],
                    "revision": int(binding["revision"]),
                    "config_generation": int(binding["config_generation"]),
                }
                if session_scope:
                    diagnostic["session_scope"] = str(session_scope)[:128]
                transport = getattr(exc, "transport", None)
                if transport in ("stdio", "streamable_http"):
                    diagnostic["transport"] = transport
                for int_key in ("request_id", "rpc_code", "http_status", "session_pid"):
                    val = getattr(exc, int_key, None)
                    if type(val) is int:
                        diagnostic[int_key] = val
                ledger.update(
                    operation_id,
                    "dispatched",
                    error=safe_code,
                    error_result=diagnostic,
                    receipt_reserve=0,
                )
                if isinstance(exc, (KeyboardInterrupt, SystemExit)):
                    raise
                unknown_err = AgentError("agent_write_outcome_unknown")
                unknown_err.diagnostics = copy.deepcopy(diagnostic)  # type: ignore[attr-defined]
                raise unknown_err from None

            normalized = self._normalize_mcp_result(
                raw_res,
                binding=binding,
                context=context,
                operation_id=operation_id,
                allow_image_rejection=False,
            )
            if normalized.get("is_error") or normalized.get("error"):
                # MCP tool-level isError=true does not prove rollback or absence of side effects.
                # Preserve dispatched/unknown status with bounded sanitized diagnostics and halt retries.
                ledger.update(
                    operation_id,
                    "dispatched",
                    error=str(normalized.get("error") or "agent_mcp_tool_returned_error"),
                    error_result=normalized,
                    receipt_reserve=0,
                )
                unknown_err = AgentError("agent_write_outcome_unknown")
                unknown_err.diagnostics = copy.deepcopy(normalized)  # type: ignore[attr-defined]
                raise unknown_err
            ledger.update(operation_id, "confirmed", result=normalized)
            return normalized

    def reconcile(self, operation_id: str, context: dict[str, Any]) -> dict[str, Any]:
        operation = context.get("operation") or {}
        row = self.ledger_factory().get(operation_id)
        if not row:
            return {
                "outcome": "unknown",
                "reason": "No durable completion receipt; MCP write operations are never automatically replayed.",
            }
        binding = row.get("binding") or {}
        if (
            binding.get("task_id") != context.get("task_id")
            or binding.get("arguments_sha256") != operation.get("arguments_sha256")
            or binding.get("tool") != operation.get("tool")
        ):
            return {"outcome": "unknown"}
        if (
            operation.get("extension_binding") is not None
            and binding.get("extension_binding") != operation.get("extension_binding")
        ):
            return {"outcome": "unknown"}
        row_result = row.get("result")
        if (
            row.get("status") == "confirmed"
            and isinstance(row_result, dict)
            and not (row_result.get("is_error") or row_result.get("error"))
        ):
            return {
                "outcome": "executed",
                "receipt": {
                    "operation_id": operation_id,
                    "tool": operation["tool"],
                    "arguments_sha256": operation["arguments_sha256"],
                    "result": row_result,
                },
            }
        if row.get("status") == "not_executed":
            return {
                "outcome": "not_executed",
                "receipt": {
                    "operation_id": operation_id,
                    "tool": operation["tool"],
                    "arguments_sha256": operation["arguments_sha256"],
                    "result": {
                        "error": row.get("error", "agent_mcp_operation_not_executed"),
                        "not_executed": True,
                        "no_mutation": True,
                    },
                },
            }
        err_res = row.get("error_result")
        safe_err = str(row.get("error") or "agent_mcp_tool_returned_error")
        if isinstance(err_res, dict) and err_res.get("phase") == "post_dispatch":
            reason = f"MCP write operation failed after dispatch ({safe_err}); target side effects are uncertain and no automatic retry is permitted."
        elif isinstance(err_res, dict):
            reason = "MCP write operation returned an error result after dispatch; target side effects are uncertain and no automatic retry is permitted."
        else:
            reason = "No durable completion receipt; MCP write operations are never automatically replayed."
        unknown_out: dict[str, Any] = {
            "outcome": "unknown",
            "reason": reason,
        }
        if isinstance(err_res, dict):
            unknown_out["error"] = safe_err
            unknown_out["diagnostic"] = copy.deepcopy(err_res)
        return unknown_out
