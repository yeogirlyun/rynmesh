"""Read-only Computer Use status projection and endpoint handler."""
from __future__ import annotations

import asyncio
import hashlib
import json
import re
import sys
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from cryptography.exceptions import InvalidTag
from fastapi import Request
from fastapi.responses import JSONResponse

from ..atomic_io import read_json
from ..services import peer_box
from . import image_capabilities
from .computer.coordinator import NativeBridge
from .computer.protocol import EXPECTED_WORKER_NAME, PROTOCOL_SEMVER
from .diagnostic_log import failure
from .execution_policy import MODES as EXECUTION_PERMISSION_MODES
from .extensions.mcp_transport import StdioMcpSession, StreamableHttpMcpSession
from .extensions.store import (
    COMPUTER_USE_PLUGIN_ID,
    COMPUTER_USE_SERVER_ID,
    EXTENSIONS_CHANNEL,
    EXTENSIONS_VERSION,
)
from .extensions.store import (
    MAX_BYTES as EXT_MAX_BYTES,
)
from .extensions.store import (
    MAX_PLAINTEXT as EXT_MAX_PLAINTEXT,
)
from .store import AgentError

SCHEMA_VERSION = 1

EVIDENCE_SOURCES = frozenset({
    "none",
    "extension_store",
    "agent_store",
    "native_probe_cache",
    "live_session",
    "model_route_contract",
    "model_image_probe",
    "runtime_receipt",
    "client_render_receipt",
})

REASON_CODES = frozenset({
    "plugin_missing",
    "plugin_disabled",
    "agent_disabled",
    "native_not_verified",
    "native_missing",
    "native_platform_unsupported",
    "native_protocol_incompatible",
    "native_evidence_stale",
    "native_failed",
    "global_desktop_access_disabled",
    "task_local_execution_disabled",
    "task_desktop_access_disabled",
    "session_not_started",
    "session_lost",
    "model_unavailable",
    "image_route_unsupported",
    "image_capability_unknown",
    "image_verification_pending",
    "image_verification_failed",
    "image_evidence_stale",
    "status_unavailable",
})

PERMISSION_MODES = frozenset(EXECUTION_PERMISSION_MODES | {"unknown"})
ADAPTER_KINDS = frozenset({
    "codex_cli",
    "antigravity_cli",
    "openai_chat",
    "openai_responses",
    "anthropic_messages",
    "other",
    "unknown",
})
IMAGE_CARRIERS = frozenset({"none", "codex_turn_input", "api_image_blocks", "unknown"})
IMAGE_STATUSES = frozenset({"unsupported", "carrier_available", "verified", "unknown"})

_EVENT_SOURCES_REQUIRING_TIMESTAMP = frozenset({
    "native_probe_cache",
    "live_session",
    "model_image_probe",
    "runtime_receipt",
    "client_render_receipt",
})

_SAFE_ID_RE = re.compile(r"^[A-Za-z0-9._:@/-]{1,160}$")
_SAFE_MODEL_ID_RE = re.compile(r"^[A-Za-z0-9._:@/-]{1,1024}$")
_SAFE_TASK_ID_RE = re.compile(r"^[A-Za-z0-9._:@-]{1,160}$")
_SAFE_VERSION_RE = re.compile(r"^[A-Za-z0-9._+-]{1,64}$")
_HEX_SHA256_RE = re.compile(r"^[0-9a-fA-F]{64}$")
_WINDOWS_DRIVE_RE = re.compile(r"^[A-Za-z]:[/\\]")
_UNIX_ROOT_RE = re.compile(
    r"^/(?:Users|home|root|tmp|var|etc|opt|mnt|proc|dev|private|Volumes|Applications|System|Library)\b",
    re.IGNORECASE,
)
_UNC_PATH_RE = re.compile(r"^\\\\[^\\]+\\[^\\]+")
_URL_RE = re.compile(r"^[a-zA-Z][a-zA-Z0-9+.-]*://")
_SECRET_TOKEN_RE = re.compile(
    r"(?:^|[^A-Za-z0-9])(?:sk-[A-Za-z0-9_-]{8,}|rk-[A-Za-z0-9_-]{8,}|AIza[0-9A-Za-z_-]{16,}|gh[pousr]_[A-Za-z0-9]{16,}|xox[baprs]-[A-Za-z0-9-]{10,}|Bearer\s+[A-Za-z0-9._~+/=-]{8,})",
    re.IGNORECASE,
)
_EXECUTABLE_SUFFIX_RE = re.compile(r"\.(?:exe|dll|bat|cmd|ps1|sh|bin|app|msi|so|dylib)$", re.IGNORECASE)
_TRACEBACK_RE = re.compile(
    r"(?:Traceback \(most recent call last\)|Exception:|Error:|\bat line \d+|\bFile \"[^\"]+\")",
    re.IGNORECASE,
)


def _no_store_headers() -> dict[str, str]:
    return {"Cache-Control": "no-store"}


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def looks_like_unsafe_diagnostic_or_secret(value: Any, *, max_len: int = 200) -> bool:
    if not isinstance(value, str):
        return True
    trimmed = value.strip()
    if not trimmed or len(trimmed) > max_len:
        return True
    if any(ord(ch) < 32 or ord(ch) == 127 for ch in trimmed):
        return True
    if _WINDOWS_DRIVE_RE.search(trimmed) or _UNC_PATH_RE.search(trimmed) or _UNIX_ROOT_RE.search(trimmed):
        return True
    if _URL_RE.search(trimmed) or _SECRET_TOKEN_RE.search(trimmed):
        return True
    if _EXECUTABLE_SUFFIX_RE.search(trimmed) or _TRACEBACK_RE.search(trimmed):
        return True
    if "\\" in trimmed or "/.ryn/" in trimmed or ".ryn\\" in trimmed or ".." in trimmed:
        return True
    return False


def _normalize_safe_id(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    trimmed = value.strip()
    if not trimmed or looks_like_unsafe_diagnostic_or_secret(trimmed, max_len=160):
        return None
    if not _SAFE_ID_RE.match(trimmed):
        return None
    return trimmed


def _normalize_safe_model_id(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    trimmed = value.strip()
    if not trimmed or looks_like_unsafe_diagnostic_or_secret(trimmed, max_len=1024):
        return None
    if not _SAFE_MODEL_ID_RE.match(trimmed):
        return None
    return trimmed


def _normalize_safe_display_name(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    trimmed = value.strip()
    if not trimmed or len(trimmed) > 160:
        return None
    if looks_like_unsafe_diagnostic_or_secret(trimmed, max_len=160):
        return None
    if trimmed.startswith("/"):
        return None
    return trimmed


def _normalize_safe_version(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    trimmed = value.strip()
    if not trimmed or looks_like_unsafe_diagnostic_or_secret(trimmed, max_len=64):
        return None
    if not _SAFE_VERSION_RE.match(trimmed):
        return None
    return trimmed


def _normalize_non_negative_int(value: Any) -> int | None:
    if type(value) is int and value >= 0:
        return value
    return None


def _normalize_positive_int(value: Any) -> int | None:
    if type(value) is int and value > 0:
        return value
    return None


def _normalize_tri_bool(value: Any) -> bool | None:
    if type(value) is bool:
        return value
    return None


def _normalize_reason_code(value: Any) -> str | None:
    if isinstance(value, str) and value in REASON_CODES:
        return value
    return None


def _normalize_permission_mode(value: Any) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        return "unknown"
    trimmed = value.strip()
    if not trimmed:
        return None
    if trimmed in PERMISSION_MODES:
        return trimmed
    return "unknown"


def _normalize_iso_timestamp(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    trimmed = value.strip()
    if not trimmed or len(trimmed) > 64:
        return None
    if "T" not in trimmed and "t" not in trimmed:
        return None
    candidate = trimmed[:-1] + "+00:00" if trimmed.endswith(("Z", "z")) else trimmed
    try:
        dt = datetime.fromisoformat(candidate)
    except ValueError:
        return None
    if dt.tzinfo is None:
        return None
    utc_dt = dt.astimezone(timezone.utc)
    iso_str = utc_dt.isoformat()
    if iso_str.endswith("+00:00"):
        iso_str = iso_str[:-6] + "Z"
    return iso_str


def _parse_iso_datetime(value: Any) -> datetime | None:
    norm = _normalize_iso_timestamp(value)
    if norm is None:
        return None
    try:
        return datetime.fromisoformat(norm[:-1] + "+00:00" if norm.endswith("Z") else norm)
    except ValueError:
        return None


def _make_evidence(source: Any, observed_at: Any = None) -> dict[str, str | None]:
    src = source if isinstance(source, str) and source in EVIDENCE_SOURCES else "none"
    if src == "none":
        return {"source": "none", "observed_at": None}
    if observed_at is not None:
        ts = _normalize_iso_timestamp(observed_at)
        if ts is None:
            return {"source": "none", "observed_at": None}
        return {"source": src, "observed_at": ts}
    if src in _EVENT_SOURCES_REQUIRING_TIMESTAMP:
        return {"source": "none", "observed_at": None}
    return {"source": src, "observed_at": None}


def validate_query_params(request: Request) -> str | None:
    raw_items = list(request.query_params.multi_items())
    task_id_values: list[str] = []
    for key, val in raw_items:
        if key != "task_id":
            raise AgentError("agent_invalid_request")
        task_id_values.append(val)
    if len(task_id_values) > 1:
        raise AgentError("agent_invalid_request")
    if not task_id_values:
        return None
    return validate_requested_task_id(task_id_values[0])


def validate_requested_task_id(raw_task_id: Any) -> str:
    if not isinstance(raw_task_id, str):
        raise AgentError("agent_invalid_task_id")
    trimmed = raw_task_id.strip()
    if not trimmed or len(trimmed) > 160:
        raise AgentError("agent_invalid_task_id")
    if "/" in trimmed or "\\" in trimmed or ".." in trimmed:
        raise AgentError("agent_invalid_task_id")
    if looks_like_unsafe_diagnostic_or_secret(trimmed, max_len=160):
        raise AgentError("agent_invalid_task_id")
    if not _SAFE_TASK_ID_RE.match(trimmed):
        raise AgentError("agent_invalid_task_id")
    return trimmed


def _extract_agent_view(data: dict[str, Any], requested_task_id: str | None) -> dict[str, Any]:
    if not isinstance(data, dict):
        raise AgentError("agent_state_unreadable")
    settings = data.get("settings") if isinstance(data.get("settings"), dict) else {}
    settings_rev = data.get("settings_revision", 0)
    deleted_tasks = data.get("deleted_tasks") if isinstance(data.get("deleted_tasks"), dict) else {}
    tasks = data.get("tasks") if isinstance(data.get("tasks"), dict) else {}

    task_view: dict[str, Any] | None = None
    is_deleted = False
    if requested_task_id is not None:
        if requested_task_id in deleted_tasks:
            is_deleted = True
        else:
            raw_task = tasks.get(requested_task_id)
            if isinstance(raw_task, dict):
                if raw_task.get("deleted") is True or raw_task.get("status") == "deleted":
                    is_deleted = True
                else:
                    goal_obj = raw_task.get("goal")
                    goal_rev = (
                        goal_obj.get("revision")
                        if isinstance(goal_obj, dict) and type(goal_obj.get("revision")) is int
                        else raw_task.get("goal_revision")
                    )
                    task_view = {
                        "id": raw_task.get("id", requested_task_id),
                        "status": raw_task.get("status"),
                        "revision": raw_task.get("revision"),
                        "control_revision": raw_task.get("control_revision"),
                        "scope_revision": raw_task.get("scope_revision"),
                        "goal_revision": goal_rev,
                        "context_epoch": raw_task.get("context_epoch", 0),
                        "model_id": raw_task.get("model_id"),
                        "permission_mode": raw_task.get("permission_mode"),
                        "local_execution": raw_task.get("local_execution"),
                        "scope": raw_task.get("scope"),
                        "model_destination": raw_task.get("model_destination"),
                        "pending_approval": raw_task.get("pending_approval"),
                        "waiting_approval": raw_task.get("waiting_approval"),
                        "in_flight": raw_task.get("in_flight"),
                        "run_state": raw_task.get("run_state"),
                        "reconciliation": raw_task.get("reconciliation"),
                        "outcome": raw_task.get("outcome"),
                        "session_generation": raw_task.get("session_generation"),
                        "updated_at": raw_task.get("updated_at"),
                        "created_at": raw_task.get("created_at"),
                        "receipts": raw_task.get("receipts"),
                        "image_request_links": raw_task.get("image_request_links"),
                        "client_render_receipts": raw_task.get("client_render_receipts"),
                        "events": raw_task.get("events"),
                    }

    return {
        "settings": dict(settings),
        "settings_revision": settings_rev,
        "default_model_destination": data.get("default_model_destination"),
        "task_deleted": is_deleted,
        "task": task_view,
    }


def _read_agent_view_readonly(runtime: Any, requested_task_id: str | None) -> dict[str, Any]:
    runtime_dict = getattr(runtime, "__dict__", {})
    store = runtime_dict.get("store")
    if store is None:
        raise AgentError("agent_computer_status_unavailable")
    inspect_fn = getattr(store, "inspect", None)
    if callable(inspect_fn):
        return inspect_fn(lambda data: _extract_agent_view(data, requested_task_id))
    read_fn = getattr(store, "read", None)
    if callable(read_fn):
        return _extract_agent_view(read_fn(), requested_task_id)
    raise AgentError("agent_computer_status_unavailable")


def _get_extension_adapter_readonly(runtime: Any) -> Any:
    runtime_dict = getattr(runtime, "__dict__", {})
    ext = runtime_dict.get("extensions")
    if ext is not None:
        return ext
    tools = runtime_dict.get("tools")
    tools_dict = getattr(tools, "__dict__", {}) if tools is not None else {}
    return tools_dict.get("extensions")


def _read_extension_state_readonly(runtime: Any) -> dict[str, Any] | None:
    ext_adapter = _get_extension_adapter_readonly(runtime)
    ext_store = getattr(ext_adapter, "__dict__", {}).get("_store") if ext_adapter is not None else None

    ext_path: Path | None = None
    key: Any = None
    pub: Any = None

    if ext_store is not None:
        st_dict = getattr(ext_store, "__dict__", {})
        ext_path = st_dict.get("path")
        key = st_dict.get("key")
        pub = st_dict.get("pub")
    else:
        agent_store = getattr(runtime, "__dict__", {}).get("store")
        ag_dict = getattr(agent_store, "__dict__", {}) if agent_store is not None else {}
        ag_root = ag_dict.get("root")
        key = ag_dict.get("key")
        pub = ag_dict.get("pub")
        if isinstance(ag_root, Path):
            ext_path = ag_root / "extensions.json"

    if not isinstance(ext_path, Path) or not ext_path.is_file() or key is None or not isinstance(pub, str):
        return None

    try:
        envelope = read_json(ext_path, max_bytes=EXT_MAX_BYTES)
        if not isinstance(envelope, dict) or envelope.get("version") != EXTENSIONS_VERSION:
            return None
        plaintext = peer_box.open_sealed(
            key,
            pub,
            envelope["nonce"],
            envelope["ciphertext"],
            info=EXTENSIONS_CHANNEL,
        )
        if len(plaintext) > EXT_MAX_PLAINTEXT:
            return None
        state = json.loads(plaintext)
        return state if isinstance(state, dict) else None
    except (OSError, ValueError, KeyError, TypeError, InvalidTag, AgentError):
        return None


def _project_plugin_section(ext_state: dict[str, Any] | None) -> dict[str, Any]:
    if not isinstance(ext_state, dict):
        return {
            "installed": None,
            "enabled": None,
            "revision": None,
            "evidence": _make_evidence("none", None),
            "reason_code": None,
        }

    revision = _normalize_non_negative_int(ext_state.get("revision"))
    plugins = ext_state.get("plugins") if isinstance(ext_state.get("plugins"), dict) else {}
    servers = ext_state.get("mcp_servers") if isinstance(ext_state.get("mcp_servers"), dict) else {}

    plugin_rec = plugins.get(COMPUTER_USE_PLUGIN_ID) if isinstance(plugins.get(COMPUTER_USE_PLUGIN_ID), dict) else None
    server_rec = servers.get(COMPUTER_USE_SERVER_ID) if isinstance(servers.get(COMPUTER_USE_SERVER_ID), dict) else None

    observed_at = (
        (plugin_rec or {}).get("updated_at")
        or (server_rec or {}).get("updated_at")
        or ext_state.get("updated_at")
    )
    if observed_at is not None and _normalize_iso_timestamp(observed_at) is None:
        observed_at = None
    evidence = _make_evidence("extension_store", observed_at)

    if plugin_rec is None and server_rec is None:
        return {
            "installed": False,
            "enabled": False,
            "revision": revision,
            "evidence": evidence,
            "reason_code": "plugin_missing",
        }

    if plugin_rec is not None and "installed" in plugin_rec:
        plugin_installed = _normalize_tri_bool(plugin_rec.get("installed"))
    elif plugin_rec is not None:
        plugin_installed = True
    else:
        plugin_installed = None

    if server_rec is not None and "installed" in server_rec:
        server_installed = _normalize_tri_bool(server_rec.get("installed"))
    else:
        server_installed = None

    if plugin_installed is False or server_installed is False:
        installed: bool | None = False
    elif plugin_installed is True and (server_installed is True or server_installed is None):
        installed = True
    elif plugin_installed is None and server_installed is True:
        installed = True
    else:
        installed = None

    plugin_enabled = _normalize_tri_bool(plugin_rec.get("enabled")) if plugin_rec is not None else None
    server_enabled = _normalize_tri_bool(server_rec.get("enabled")) if server_rec is not None else None

    if installed is False:
        enabled: bool | None = False
        reason_code: str | None = "plugin_missing"
    elif plugin_enabled is False or server_enabled is False:
        enabled = False
        reason_code = "plugin_disabled"
    elif plugin_enabled is True and (server_rec is None or "enabled" not in server_rec or server_enabled is True):
        enabled = True
        reason_code = None
    else:
        enabled = None
        reason_code = None

    return {
        "installed": installed,
        "enabled": enabled,
        "revision": revision,
        "evidence": evidence,
        "reason_code": reason_code,
    }


def _project_native_section(runtime: Any) -> dict[str, Any]:
    if sys.platform != "win32":
        return {
            "platform": "unsupported",
            "executable_presence": "unknown",
            "status": "error",
            "version": None,
            "protocol_version": None,
            "evidence": _make_evidence("none", None),
            "reason_code": "native_platform_unsupported",
        }

    runtime_dict = getattr(runtime, "__dict__", {})
    store = runtime_dict.get("store")
    store_dict = getattr(store, "__dict__", {}) if store is not None else {}
    bridge = store_dict.get("_ryn_native_bridge")

    if not isinstance(bridge, NativeBridge):
        return {
            "platform": "windows",
            "executable_presence": "unknown",
            "status": "unknown",
            "version": None,
            "protocol_version": None,
            "evidence": _make_evidence("none", None),
            "reason_code": "native_not_verified",
        }

    try:
        raw = bridge.cache_status()
    except Exception:
        raw = None

    if not isinstance(raw, dict) or raw.get("cached") is not True:
        return {
            "platform": "windows",
            "executable_presence": "unknown",
            "status": "unknown",
            "version": None,
            "protocol_version": None,
            "evidence": _make_evidence("none", None),
            "reason_code": "native_not_verified",
        }

    worker_info = raw.get("worker") if isinstance(raw.get("worker"), dict) else {}
    version = _normalize_safe_version(worker_info.get("version"))
    protocol_version = _normalize_safe_version(raw.get("protocolVersion"))
    worker_name = worker_info.get("name")
    live = raw.get("live") is True
    handshake_ts = _normalize_iso_timestamp(raw.get("handshakeTimestamp"))
    evidence = _make_evidence("live_session" if live else "native_probe_cache", handshake_ts)
    presence = "present" if (live and evidence["observed_at"] is not None) else "unknown"

    if (protocol_version is not None and protocol_version != PROTOCOL_SEMVER) or (
        isinstance(worker_name, str) and worker_name != EXPECTED_WORKER_NAME
    ):
        return {
            "platform": "windows",
            "executable_presence": presence,
            "status": "incompatible",
            "version": version,
            "protocol_version": protocol_version,
            "evidence": evidence,
            "reason_code": "native_protocol_incompatible",
        }

    if not live or raw.get("status") == "stale" or evidence["observed_at"] is None:
        return {
            "platform": "windows",
            "executable_presence": presence,
            "status": "unverified",
            "version": version,
            "protocol_version": protocol_version,
            "evidence": evidence,
            "reason_code": "native_evidence_stale",
        }

    # NativeBridge.cache_status() currently lacks authoritative current binary/config
    # verification binding, so it must not independently qualify native.status="ready".
    return {
        "platform": "windows",
        "executable_presence": presence,
        "status": "unverified",
        "version": version,
        "protocol_version": protocol_version,
        "evidence": evidence,
        "reason_code": "native_not_verified",
    }


def _project_permissions_section(
    settings: dict[str, Any],
    task_id: str | None,
    task_row: dict[str, Any] | None,
) -> dict[str, Any]:
    agent_enabled = _normalize_tri_bool(settings.get("enabled"))
    global_desktop_access = _normalize_tri_bool(settings.get("desktop_access"))
    global_permission_mode = _normalize_permission_mode(settings.get("permission_mode"))

    observed_at = settings.get("updated_at")
    if task_row is not None and isinstance(task_row.get("updated_at"), str):
        observed_at = task_row.get("updated_at")
    if observed_at is not None and _normalize_iso_timestamp(observed_at) is None:
        observed_at = None
    evidence = _make_evidence("agent_store", observed_at)

    if task_id is None or task_row is None:
        return {
            "agent_enabled": agent_enabled,
            "global_desktop_access": global_desktop_access,
            "task_scope": "not_requested",
            "task_local_execution": None,
            "task_desktop_access": None,
            "global_permission_mode": global_permission_mode,
            "task_permission_mode": None,
            "evidence": evidence,
        }

    task_local_execution: bool | None = None
    task_desktop_access: bool | None = None

    le = task_row.get("local_execution")
    if isinstance(le, bool):
        task_local_execution = le
    elif isinstance(le, dict):
        if "enabled" in le and type(le.get("enabled")) is bool:
            task_local_execution = le["enabled"]
        elif "workspace_root" in le:
            ws = le.get("workspace_root")
            task_local_execution = bool(isinstance(ws, str) and ws.strip())
        elif le:
            task_local_execution = True
        else:
            task_local_execution = False

        if "desktop_access" in le and type(le.get("desktop_access")) is bool:
            task_desktop_access = le["desktop_access"]
        elif "allow_desktop" in le and type(le.get("allow_desktop")) is bool:
            task_desktop_access = le["allow_desktop"]
        elif not le:
            task_desktop_access = False

    scope = task_row.get("scope")
    if isinstance(scope, dict):
        if task_local_execution is None and "allow_local_execution" in scope:
            task_local_execution = _normalize_tri_bool(scope.get("allow_local_execution"))
        if task_desktop_access is None:
            for k in ("desktop_access", "allow_desktop"):
                if k in scope and type(scope.get(k)) is bool:
                    task_desktop_access = scope[k]
                    break

    task_permission_mode = _normalize_permission_mode(task_row.get("permission_mode"))
    if task_permission_mode is None and isinstance(scope, dict) and "permission_mode" in scope:
        task_permission_mode = _normalize_permission_mode(scope.get("permission_mode"))

    return {
        "agent_enabled": agent_enabled,
        "global_desktop_access": global_desktop_access,
        "task_scope": "present",
        "task_local_execution": task_local_execution,
        "task_desktop_access": task_desktop_access,
        "global_permission_mode": global_permission_mode,
        "task_permission_mode": task_permission_mode,
        "evidence": evidence,
    }


def _is_computer_use_tool_ref(record: Any) -> bool:
    if not isinstance(record, dict):
        return False
    if record.get("computer_access") is True:
        return True
    ext_bind = record.get("extension_binding")
    if isinstance(ext_bind, dict) and (
        ext_bind.get("computer_access") is True or ext_bind.get("server_id") == COMPUTER_USE_SERVER_ID
    ):
        return True
    server_id = record.get("server_id") or record.get("plugin_id") or record.get("extension_id")
    if server_id == COMPUTER_USE_SERVER_ID:
        return True
    tool_name = str(record.get("tool") or record.get("name") or record.get("action") or "")
    if tool_name.startswith((
        "ryn_computer_",
        "mcp__plugin_computer-use_",
        "mcp__plugin_computer_use_",
        "mcp__computer-use__",
        "mcp__computer_use__",
        "mcp.computer-use.",
        "mcp.computer_use.",
        "computer_use.",
        "computer.",
        "computer_",
    )):
        return True
    return False


def _is_closed_flag_set(raw_closed: Any) -> bool:
    if type(raw_closed) is bool:
        return raw_closed
    if isinstance(raw_closed, threading.Event):
        return raw_closed.is_set()
    return False


def _is_process_exited_readonly(proc_obj: Any) -> bool:
    if proc_obj is None:
        return False
    if isinstance(proc_obj, dict):
        return proc_obj.get("returncode") is not None
    proc_dict = getattr(proc_obj, "__dict__", {})
    if isinstance(proc_dict, dict) and proc_dict.get("returncode") is not None:
        return True
    return False


def _inspect_unknown_computer_history(task_row: dict[str, Any]) -> tuple[bool, str | None]:
    has_unknown = False
    latest_ts: str | None = None
    receipts = task_row.get("receipts")
    computer_op_ids: set[str] = set()
    if isinstance(receipts, list):
        for r in receipts:
            if not isinstance(r, dict) or not _is_computer_use_tool_ref(r):
                continue
            op_id = r.get("operation_id")
            if isinstance(op_id, str) and op_id:
                computer_op_ids.add(op_id)
            if r.get("status") == "unknown":
                has_unknown = True
                r_ts = _normalize_iso_timestamp(r.get("confirmed_at") or r.get("started_at"))
                if r_ts is not None:
                    latest_ts = r_ts
    reconciliation = task_row.get("reconciliation")
    if isinstance(reconciliation, dict) and reconciliation.get("state") == "unknown":
        rec_op = reconciliation.get("operation_id")
        if _is_computer_use_tool_ref(reconciliation) or (
            isinstance(rec_op, str) and rec_op in computer_op_ids
        ):
            has_unknown = True
    return has_unknown, latest_ts


def _project_session_section(
    runtime: Any,
    task_id: str | None,
    task_row: dict[str, Any] | None,
) -> dict[str, Any]:
    if task_id is None or task_row is None:
        return {
            "state": "not_applicable",
            "generation": None,
            "evidence": _make_evidence("none", None),
            "reason_code": None,
        }

    task_status = str(task_row.get("status") or "")
    task_updated_at = _normalize_iso_timestamp(task_row.get("updated_at") or task_row.get("created_at"))
    task_evidence = _make_evidence("agent_store", task_updated_at)

    for wait_key in ("pending_approval", "waiting_approval"):
        waiting = task_row.get(wait_key)
        if isinstance(waiting, dict) and _is_computer_use_tool_ref(waiting):
            gen = _normalize_non_negative_int(waiting.get("generation") or task_row.get("session_generation"))
            wait_ts = _normalize_iso_timestamp(waiting.get("requested_at") or waiting.get("created_at")) or task_updated_at
            return {
                "state": "waiting_approval",
                "generation": gen,
                "evidence": _make_evidence("agent_store", wait_ts),
                "reason_code": None,
            }

    in_flight = task_row.get("in_flight")
    in_flight_computer = isinstance(in_flight, dict) and _is_computer_use_tool_ref(in_flight)

    ext_adapter = _get_extension_adapter_readonly(runtime)
    manager = getattr(ext_adapter, "__dict__", {}).get("_manager") if ext_adapter is not None else None
    if manager is not None:
        mgr_dict = getattr(manager, "__dict__", {})
        sessions = mgr_dict.get("_sessions")
        pending = mgr_dict.get("_pending")
        key = (COMPUTER_USE_SERVER_ID, task_id)
        is_pending = (isinstance(pending, (set, frozenset)) and key in pending) or (
            isinstance(pending, dict) and key in pending and bool(pending.get(key))
        )
        if is_pending:
            return {
                "state": "starting",
                "generation": None,
                "evidence": _make_evidence("none", None),
                "reason_code": None,
            }
        if isinstance(sessions, dict) and key in sessions:
            sess = sessions.get(key)
            if isinstance(sess, (StdioMcpSession, StreamableHttpMcpSession)):
                sess_dict = getattr(sess, "__dict__", {})
                if not isinstance(sess_dict, dict):
                    sess_dict = {}
                closed_set = _is_closed_flag_set(sess_dict.get("closed"))
                failure_val = sess_dict.get("failure")
                has_failure = isinstance(failure_val, str) and bool(failure_val.strip())
                proc_dead = _is_process_exited_readonly(sess_dict.get("process"))
                if closed_set or has_failure or proc_dead:
                    return {
                        "state": "lost",
                        "generation": None,
                        "evidence": _make_evidence("none", None),
                        "reason_code": "session_lost",
                    }
                return {
                    "state": "unknown",
                    "generation": None,
                    "evidence": _make_evidence("none", None),
                    "reason_code": None,
                }
            # Unsupported legacy/mock cache entry in _sessions[key]:
            # Never call poll(), never trust fictional generation/started_at, and never override
            # unknown historical flight with busy/connected.
            has_unknown_history, _ = _inspect_unknown_computer_history(task_row)
            if not (in_flight_computer or has_unknown_history):
                sess_dict = sess if isinstance(sess, dict) else getattr(sess, "__dict__", {})
                if not isinstance(sess_dict, dict):
                    sess_dict = {}
                closed_set = _is_closed_flag_set(sess_dict.get("closed"))
                failure_val = sess_dict.get("failure")
                has_failure = isinstance(failure_val, str) and bool(failure_val.strip())
                proc_obj = sess_dict.get("process") if "process" in sess_dict else sess_dict.get("proc")
                proc_dead = _is_process_exited_readonly(proc_obj)
                if closed_set or has_failure or proc_dead:
                    return {
                        "state": "lost",
                        "generation": None,
                        "evidence": _make_evidence("none", None),
                        "reason_code": "session_lost",
                    }
                return {
                    "state": "unknown",
                    "generation": None,
                    "evidence": _make_evidence("none", None),
                    "reason_code": None,
                }

    if task_status == "cancelled":
        return {
            "state": "cancelled",
            "generation": _normalize_non_negative_int(task_row.get("session_generation")),
            "evidence": task_evidence,
            "reason_code": None,
        }

    has_unknown_history, unknown_receipt_ts = _inspect_unknown_computer_history(task_row)
    if in_flight_computer or has_unknown_history:
        gen = _normalize_non_negative_int(
            (in_flight.get("generation") if isinstance(in_flight, dict) else None)
            or task_row.get("session_generation")
        )
        inf_ts = (
            (_normalize_iso_timestamp(in_flight.get("started_at")) if isinstance(in_flight, dict) else None)
            or unknown_receipt_ts
            or task_updated_at
        )
        return {
            "state": "unknown",
            "generation": gen,
            "evidence": _make_evidence("agent_store", inf_ts) if inf_ts is not None else _make_evidence("none", None),
            "reason_code": None,
        }

    return {
        "state": "none",
        "generation": _normalize_non_negative_int(task_row.get("session_generation")),
        "evidence": task_evidence,
        "reason_code": "session_not_started",
    }


def _extract_upstream_model_id(selected_model_id: str, row: dict[str, Any] | None = None) -> str:
    if isinstance(row, dict):
        for key in ("model_id", "cli_model"):
            val = row.get(key)
            if isinstance(val, str) and val.strip():
                return val.strip()
    if "/model/" in selected_model_id:
        return selected_model_id.split("/model/", 1)[1].strip()
    if selected_model_id.startswith("local/"):
        return selected_model_id.removeprefix("local/").strip()
    return selected_model_id


def _map_adapter_kind(raw_adapter: Any) -> str | None:
    if raw_adapter is None:
        return None
    if not isinstance(raw_adapter, str):
        return "unknown"
    cleaned = raw_adapter.strip()
    if not cleaned:
        return None
    if cleaned == "openai_compatible":
        return "openai_chat"
    if cleaned in ADAPTER_KINDS:
        return cleaned
    return "other"


def _map_image_carrier(raw_transport: Any) -> str:
    if not isinstance(raw_transport, str):
        return "unknown"
    cleaned = raw_transport.strip()
    if cleaned in {"codex_cli_turn_input", "codex_turn_input"}:
        return "codex_turn_input"
    if cleaned in {"api_multimodal_content", "api_image_blocks"}:
        return "api_image_blocks"
    if cleaned == "none":
        return "none"
    return "unknown"


def _read_source_store_records_readonly(source_store: Any) -> list[dict[str, Any]]:
    if source_store is None:
        return []
    path = getattr(source_store, "path", None)
    if isinstance(path, Path) and not path.exists():
        return []
    read_fn = getattr(source_store, "read", None)
    if callable(read_fn):
        try:
            raw = read_fn()
            if isinstance(raw, dict):
                return [r for r in raw.values() if isinstance(r, dict)]
            if isinstance(raw, list):
                return [r for r in raw if isinstance(r, dict)]
        except Exception:
            return []
    return []


def _resolve_cached_model_info(
    runtime: Any,
    selected_model_id: str,
) -> tuple[dict[str, Any] | None, dict[str, Any] | None, dict[str, Any] | None]:
    """Return (cached_model_row, source_record, cli_model_row) without probing or spawning."""
    runtime_dict = getattr(runtime, "__dict__", {})
    cached_entry: dict[str, Any] | None = None
    source_record: dict[str, Any] | None = None
    cli_model_row: dict[str, Any] | None = None

    snap_hook = runtime_dict.get("computer_status_model_snapshot")
    if isinstance(snap_hook, dict) and isinstance(snap_hook.get(selected_model_id), dict):
        cached_entry = dict(snap_hook[selected_model_id])

    catalog = runtime_dict.get("catalog")
    cat_dict = getattr(catalog, "__dict__", {}) if catalog is not None else {}
    cat_store = cat_dict.get("store")
    runtime_store = runtime_dict.get("store")
    store_peer_id = str(
        getattr(cat_store, "peer_id", None)
        or getattr(runtime_store, "peer_id", None)
        or runtime_dict.get("consumer_node_id")
        or ""
    ).strip()

    # 1. Inspect catalog._cli_metadata for local/<service_id> or local/<service_id>/model/<id>
    cli_meta = cat_dict.get("_cli_metadata")
    if isinstance(cli_meta, dict):
        lock = cat_dict.get("_cli_metadata_lock")
        if lock is not None and hasattr(lock, "__enter__"):
            with lock:
                cli_items = list(cli_meta.items())
        else:
            cli_items = list(cli_meta.items())

        for base_ref, snap in cli_items:
            if not isinstance(base_ref, str) or not isinstance(snap, dict):
                continue
            if selected_model_id != base_ref and not selected_model_id.startswith(base_ref + "/model/"):
                continue
            manager = snap.get("manager")
            models_list = snap.get("models") if isinstance(snap.get("models"), list) else []
            manifest = getattr(manager, "manifest", None)
            if not isinstance(manifest, dict):
                manifest = {}
            adapter_obj = getattr(manager, "adapter", None)
            adapter_kind = str(manifest.get("adapter") or getattr(adapter_obj, "kind", "") or "").strip()
            service_name = str(manifest.get("name") or base_ref.removeprefix("local/")).strip()

            upstream_id = ""
            if selected_model_id.startswith(base_ref + "/model/"):
                upstream_id = selected_model_id.split("/model/", 1)[1].strip()
                for item in models_list:
                    if isinstance(item, dict) and str(item.get("id") or "").strip() == upstream_id:
                        cli_model_row = dict(item)
                        break
            else:
                for item in models_list:
                    if isinstance(item, dict) and item.get("default"):
                        cli_model_row = dict(item)
                        upstream_id = str(item.get("id") or "").strip()
                        break

            if cached_entry is None:
                model_label = str((cli_model_row or {}).get("name") or upstream_id or "CLI default").strip()
                display_name = f"{service_name} · {model_label}" if service_name else model_label
                cached_entry = {
                    "id": selected_model_id,
                    "name": display_name,
                    "source": "local_service",
                    "adapter": adapter_kind,
                    "model_id": upstream_id,
                    "cli_model": upstream_id,
                    "peer_id": store_peer_id,
                    "source_id": "",
                    "available": True,
                }
                if isinstance(cli_model_row, dict):
                    for k in (
                        "supports_image_input",
                        "image_input",
                        "input_modalities",
                        "modalities",
                        "capabilities",
                        "vision",
                        "multimodal",
                    ):
                        if k in cli_model_row and k not in cached_entry:
                            cached_entry[k] = cli_model_row[k]
            break

    # 2. Inspect SourceStore for local/<service_id> API models (match ONLY by exact selected service ID)
    services = cat_dict.get("services") if isinstance(cat_dict.get("services"), dict) else {}
    source_store = services.get("source_store")
    if source_store is not None and selected_model_id.startswith("local/"):
        target_sid = selected_model_id.removeprefix("local/")
        records = _read_source_store_records_readonly(source_store)
        model_service_id_fn = getattr(source_store, "model_service_id", None)
        for rec in records:
            models = rec.get("models") if isinstance(rec.get("models"), list) else []
            for raw_m in models:
                m_name = str(raw_m or "").strip()
                if not m_name:
                    continue
                sid = None
                if callable(model_service_id_fn):
                    try:
                        sid = model_service_id_fn(rec, m_name)
                    except Exception:
                        sid = None
                else:
                    rec_id = str(rec.get("id") or "").strip()
                    if rec_id:
                        sid = f"{rec_id}-{hashlib.sha256(m_name.encode()).hexdigest()[:12]}"
                if sid == target_sid:
                    source_record = rec
                    protocol = str(rec.get("protocol") or "openai_chat").strip()
                    adapter_raw = protocol if protocol in {"openai_responses", "anthropic_messages"} else "openai_chat"
                    rec_name = str(rec.get("name") or rec.get("id") or "Local API").strip()
                    model_settings_map = rec.get("model_settings") if isinstance(rec.get("model_settings"), dict) else {}
                    per_model_settings = (
                        model_settings_map.get(m_name) if isinstance(model_settings_map.get(m_name), dict) else {}
                    )
                    dest_rev = per_model_settings.get("destination_revision") or rec.get("destination_revision")
                    source_rev = rec.get("source_revision")
                    source_id_val = str(rec.get("id") or "").strip()
                    if cached_entry is None:
                        cached_entry = {
                            "id": selected_model_id,
                            "name": f"{rec_name} / {m_name}",
                            "source": "local_api",
                            "adapter": adapter_raw,
                            "protocol": protocol,
                            "model_id": m_name,
                            "source_id": source_id_val,
                            "peer_id": store_peer_id,
                            "available": bool(rec.get("enabled", True)) and rec.get("status") != "needs_key",
                            "destination_revision": dest_rev,
                            "source_revision": source_rev,
                            "updated_at": rec.get("updated_at") or rec.get("created_at"),
                        }
                    else:
                        cached_entry.setdefault("source", "local_api")
                        cached_entry["source_id"] = source_id_val
                        if store_peer_id and not cached_entry.get("peer_id"):
                            cached_entry["peer_id"] = store_peer_id
                        if dest_rev is not None:
                            cached_entry["destination_revision"] = dest_rev
                        if source_rev is not None:
                            cached_entry["source_revision"] = source_rev
                    break
            if source_record is not None:
                break

    return cached_entry, source_record, cli_model_row


def _project_model_section(
    runtime: Any,
    settings: dict[str, Any],
    task_id: str | None,
    task_row: dict[str, Any] | None,
) -> tuple[dict[str, Any], dict[str, Any] | None]:
    if task_id is not None:
        raw_model_id = task_row.get("model_id") if isinstance(task_row, dict) else None
    else:
        raw_model_id = settings.get("model_id") or settings.get("default_model_id")

    selected_model_id = _normalize_safe_model_id(raw_model_id)
    if selected_model_id is None:
        return (
            {
                "id": None,
                "display_name": None,
                "adapter": None,
                "availability": "unknown",
                "image_input": {
                    "status": "unknown",
                    "carrier": "unknown",
                    "evidence": _make_evidence("none", None),
                    "reason_code": "image_capability_unknown",
                },
            },
            None,
        )

    cached_entry, source_record, cli_model_row = _resolve_cached_model_info(runtime, selected_model_id)
    if cached_entry is None and source_record is None:
        return (
            {
                "id": selected_model_id,
                "display_name": None,
                "adapter": "unknown",
                "availability": "unknown",
                "image_input": {
                    "status": "unknown",
                    "carrier": "unknown",
                    "evidence": _make_evidence("none", None),
                    "reason_code": "image_capability_unknown",
                },
            },
            None,
        )

    eval_input = dict(cached_entry or {})
    eval_input["id"] = selected_model_id
    eval_input["model_id"] = _extract_upstream_model_id(selected_model_id, eval_input)

    if cli_model_row is not None:
        cap = image_capabilities.evaluate_image_capability(
            eval_input,
            source_record=source_record,
            cli_model_row=cli_model_row,
        )
    else:
        cap = image_capabilities.evaluate_image_capability(
            eval_input,
            source_record=source_record,
        )

    display_name = _normalize_safe_display_name(
        eval_input.get("display_name") or eval_input.get("name") or eval_input.get("label")
    )
    adapter = _map_adapter_kind(cap.get("adapter") or eval_input.get("adapter")) or "unknown"
    carrier = _map_image_carrier(
        cap.get("image_transport") or eval_input.get("image_transport") or eval_input.get("carrier")
    )

    raw_avail = eval_input.get("availability")
    if raw_avail in {"available", "unavailable", "unknown"}:
        availability = raw_avail
    elif isinstance(eval_input.get("available"), bool):
        availability = "available" if eval_input["available"] else "unavailable"
    elif isinstance(source_record, dict) and isinstance(source_record.get("enabled"), bool):
        availability = "available" if source_record["enabled"] else "unavailable"
    else:
        availability = "unknown"

    route_updated_at = _normalize_iso_timestamp(
        eval_input.get("updated_at")
        or eval_input.get("observed_at")
        or (source_record or {}).get("updated_at")
        or (source_record or {}).get("created_at")
        or settings.get("updated_at")
    )
    route_revision = (
        eval_input.get("destination_revision")
        or (source_record or {}).get("destination_revision")
        or eval_input.get("source_revision")
        or (source_record or {}).get("source_revision")
    )

    supports_img = cap.get("supports_image_input")
    carrier_avail = cap.get("image_carrier_available") is True

    # Route capability metadata and loose hooks/snapshots may map only unsupported /
    # carrier_available / unknown. Promoting model.image_input.status to "verified"
    # requires an authoritative host-owned pixel-recognition probe cache bound to the
    # exact model/source/destination revision, which is not yet produced in this repo.
    if (
        supports_img is False
        or cap.get("image_support") == "unsupported"
        or cap.get("model_image_support") == "unsupported"
    ):
        img_status = "unsupported"
        if cap.get("image_transport") == "none" or adapter == "antigravity_cli":
            carrier = "none"
        img_reason: str | None = "image_route_unsupported"
        img_evidence = _make_evidence("model_route_contract", route_updated_at)
    elif supports_img is True and carrier_avail and carrier in {"codex_turn_input", "api_image_blocks"}:
        img_status = "carrier_available"
        img_reason = "model_unavailable" if availability == "unavailable" else "image_verification_pending"
        img_evidence = _make_evidence("model_route_contract", route_updated_at)
    else:
        img_status = "unknown"
        img_reason = "model_unavailable" if availability == "unavailable" else "image_capability_unknown"
        img_evidence = _make_evidence("model_route_contract" if route_updated_at else "none", route_updated_at)

    provider_peer_id = str(
        eval_input.get("provider_peer_id")
        or eval_input.get("peer_id")
        or ""
    ).strip()
    source_id_val = str(
        (source_record or {}).get("id")
        or eval_input.get("source_id")
        or ""
    ).strip()

    destination_meta = {
        "catalog_model_id": selected_model_id,
        "provider_peer_id": provider_peer_id,
        "source_id": source_id_val,
        "destination_revision": str(route_revision or "").strip(),
        "source": str(cap.get("source") or eval_input.get("source") or "").strip(),
        "adapter": adapter if adapter != "unknown" else "",
        "image_transport": str(cap.get("image_transport") or "").strip(),
    }
    return (
        {
            "id": selected_model_id,
            "display_name": display_name,
            "adapter": adapter,
            "availability": availability,
            "image_input": {
                "status": img_status,
                "carrier": carrier,
                "evidence": img_evidence,
                "reason_code": img_reason,
            },
        },
        destination_meta,
    )


def _compute_selection_epoch(
    *,
    selection_source: str,
    task_id: str | None,
    settings_revision: int | None,
    control_revision: int | None,
    scope_revision: int | None,
    model_id: str | None,
    task_destination: dict[str, Any] | None = None,
    route_destination: dict[str, Any] | None = None,
) -> str:
    def _clean_dest(d: Any) -> dict[str, Any]:
        if not isinstance(d, dict):
            return {}
        out: dict[str, Any] = {}
        for k in (
            "catalog_model_id",
            "provider_peer_id",
            "source_id",
            "destination_revision",
            "source_revision",
            "source",
            "adapter",
            "image_transport",
        ):
            val = d.get(k)
            if k == "adapter" and isinstance(val, str) and val.strip():
                mapped = _map_adapter_kind(val)
                val = mapped if mapped and mapped != "unknown" else val.strip()
            if val is not None and val != "":
                out[k] = val
        return out

    if selection_source == "task":
        payload = {
            "selection_source": "task",
            "task_id": task_id,
            "model_id": model_id,
            "control_revision": control_revision if type(control_revision) is int else 0,
            "scope_revision": scope_revision if type(scope_revision) is int else 0,
            "task_destination": _clean_dest(task_destination),
            "route_destination": _clean_dest(route_destination),
        }
    else:
        payload = {
            "selection_source": "settings",
            "settings_revision": settings_revision if type(settings_revision) is int else 0,
            "model_id": model_id,
            "route_destination": _clean_dest(route_destination),
        }
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:24]
    return f"sel_{digest}"


_ARTIFACT_FILENAME_RE = re.compile(r"^image_[0-9]+\.[a-z0-9]+$")


def _extract_verified_receipt_artifact(
    task_id: str,
    receipt: dict[str, Any],
) -> dict[str, Any] | None:
    if receipt.get("status") != "confirmed":
        return None
    op_id = _normalize_safe_id(receipt.get("operation_id"))
    if op_id is None:
        return None
    if not (
        type(receipt.get("goal_revision")) is int
        and type(receipt.get("scope_revision")) is int
        and type(receipt.get("context_epoch")) is int
    ):
        return None

    # Derive receipt timestamp strictly from canonical verification.record fields.
    receipt_ts = _normalize_iso_timestamp(receipt.get("confirmed_at") or receipt.get("started_at"))
    if receipt_ts is None:
        return None

    result = receipt.get("result")
    if not isinstance(result, dict):
        return None

    candidates: list[dict[str, Any]] = []
    for key in ("image_artifacts", "mcp_artifacts", "images", "artifacts"):
        items = result.get(key)
        if isinstance(items, list):
            for item in items:
                if isinstance(item, dict):
                    candidates.append(item)
    if isinstance(result.get("screenshot"), dict):
        candidates.append(result["screenshot"])
    if isinstance(result.get("artifact"), dict):
        candidates.append(result["artifact"])

    for art in candidates:
        sha256 = str(art.get("sha256") or "").strip().lower()
        if not _HEX_SHA256_RE.match(sha256):
            continue
        mime = str(art.get("mime_type") or art.get("media_type") or "").strip().lower()
        if mime not in image_capabilities.ALLOWED_IMAGE_MIMES:
            continue
        byte_len = art.get("bytes") if type(art.get("bytes")) is int else art.get("size_bytes")
        if type(byte_len) is not int or byte_len <= 0 or byte_len > image_capabilities.MAX_IMAGE_SOURCE_BYTES:
            continue
        rel_path = str(art.get("path") or art.get("relative_path") or "").strip()
        if not rel_path or Path(rel_path).is_absolute() or ".." in rel_path or "\\" in rel_path:
            continue
        try:
            safe_task = image_capabilities.safe_segment(task_id, "task")
            safe_op = image_capabilities.safe_segment(op_id, "op")
        except ValueError:
            continue
        parts = rel_path.split("/")
        if len(parts) != 5 or parts[:4] != [".ryn", "mcp", safe_task, safe_op]:
            continue
        filename = parts[4]
        expected_ext = image_capabilities.ALLOWED_IMAGE_MIMES[mime][0]
        if not _ARTIFACT_FILENAME_RE.match(filename) or not filename.endswith(expected_ext):
            continue

        width = _normalize_positive_int(art.get("width"))
        height = _normalize_positive_int(art.get("height"))
        obs_id = _normalize_safe_id(art.get("id") or art.get("artifact_id")) or f"obs_{op_id}_{sha256[:12]}"
        return {
            "id": obs_id,
            "operation_id": op_id,
            "path": rel_path,
            "sha256": sha256,
            "bytes": byte_len,
            "mime_type": mime,
            "width": width,
            "height": height,
            "observed_at": receipt_ts,
            "task_revision": _normalize_non_negative_int(receipt.get("task_revision") or receipt.get("revision")),
            "goal_revision": receipt["goal_revision"],
            "scope_revision": receipt["scope_revision"],
            "control_revision": receipt.get("control_revision") if type(receipt.get("control_revision")) is int else None,
            "context_epoch": receipt["context_epoch"],
            "model_id": _normalize_safe_model_id(receipt.get("model_id")),
            "client_render_receipt": art.get("client_render_receipt") or receipt.get("client_render_receipt"),
        }
    return None


def _find_correlated_render_receipt(
    task_id: str,
    artifact_info: dict[str, Any],
    task_row: dict[str, Any],
) -> dict[str, Any]:
    # No canonical authenticated client-render receipt producer exists in the repository yet.
    # Self-asserted or partial dictionaries must never promote stages.displayed to "recorded".
    _ = (task_id, artifact_info, task_row)
    return {"state": "unknown", "evidence": _make_evidence("none", None)}


def _find_event_timestamp_for_request(
    task_row: dict[str, Any],
    request_id: str,
    *,
    stage: str,
) -> str | None:
    events = task_row.get("events")
    if not isinstance(events, list):
        return None
    target_kinds = (
        {"model_response_completed", "assistant"}
        if stage == "semantic"
        else {"model_started", "model_input_prepared", "model_response_completed"}
    )
    for ev in reversed(events):
        if not isinstance(ev, dict):
            continue
        if str(ev.get("kind") or ev.get("type") or "") not in target_kinds:
            continue
        data = ev.get("data") if isinstance(ev.get("data"), dict) else ev
        if data.get("request_id") == request_id:
            ts = _normalize_iso_timestamp(ev.get("at") or data.get("observed_at"))
            if ts is not None:
                return ts
    return None


def _project_latest_observation(
    task_id: str | None,
    task_row: dict[str, Any] | None,
    context: dict[str, Any],
    current_route_dest: dict[str, Any] | None,
) -> dict[str, Any] | None:
    if task_id is None or not isinstance(task_row, dict):
        return None

    receipts = task_row.get("receipts")
    if not isinstance(receipts, list) or not receipts:
        return None

    verified_artifacts: list[dict[str, Any]] = []
    for rec in receipts:
        if not isinstance(rec, dict):
            continue
        art = _extract_verified_receipt_artifact(task_id, rec)
        if art is not None:
            verified_artifacts.append(art)

    if not verified_artifacts:
        return None

    latest_art = verified_artifacts[-1]
    links = task_row.get("image_request_links")
    matched_link: dict[str, Any] | None = None
    if isinstance(links, list):
        for link in reversed(links):
            if not isinstance(link, dict):
                continue
            src_arts = link.get("source_artifacts")
            if not isinstance(src_arts, list):
                continue
            correlated_src = None
            for sa in src_arts:
                if not isinstance(sa, dict):
                    continue
                if (
                    sa.get("operation_id") == latest_art["operation_id"]
                    and str(sa.get("sha256") or "").strip().lower() == latest_art["sha256"]
                    and (not sa.get("path") or str(sa.get("path")).strip() == latest_art["path"])
                ):
                    correlated_src = sa
                    break
            if correlated_src is None:
                continue

            # Once we find the latest link referencing this artifact, validate all bindings strictly;
            # do not fall back to an earlier link if the latest link for this artifact is invalid/stale.
            if link.get("binding_current") is not True:
                break
            if link.get("task_id") != task_id:
                break
            if link.get("operation_id") is not None and link.get("operation_id") != latest_art["operation_id"]:
                break
            req_id = _normalize_safe_id(link.get("request_id"))
            link_model_id = _normalize_safe_model_id(link.get("model_id"))
            dest = link.get("model_destination")
            if req_id is None or link_model_id is None or not isinstance(dest, dict):
                break
            if _normalize_safe_model_id(dest.get("catalog_model_id")) != link_model_id:
                break
            dest_rev = str(dest.get("destination_revision") or "").strip()
            dest_peer_id = str(dest.get("provider_peer_id") or "").strip()
            dest_source_id = str(dest.get("source_id") or "").strip()
            link_source = str(link.get("source") or "").strip()
            link_adapter = _map_adapter_kind(link.get("adapter"))
            link_transport = str(link.get("image_transport") or "").strip()
            if (
                not dest_rev
                or not dest_peer_id
                or not link_source
                or link_adapter is None
                or link_adapter == "unknown"
                or not link_transport
                or _map_image_carrier(link_transport) not in {"codex_turn_input", "api_image_blocks"}
            ):
                break
            if link_source == "local_api" and not dest_source_id:
                break
            if not (
                type(link.get("goal_revision")) is int
                and type(link.get("scope_revision")) is int
                and type(link.get("control_revision")) is int
                and type(link.get("context_epoch")) is int
            ):
                break
            # Compare original receipt revisions with linkage revisions; never overwrite mismatching receipt state.
            if (
                link["goal_revision"] != latest_art["goal_revision"]
                or link["scope_revision"] != latest_art["scope_revision"]
                or link["context_epoch"] != latest_art["context_epoch"]
            ):
                break
            if latest_art.get("control_revision") is not None and link["control_revision"] != latest_art["control_revision"]:
                break
            if latest_art.get("model_id") is not None and link_model_id != latest_art["model_id"]:
                break

            matched_link = dict(link)
            matched_link["_correlated_source_artifact"] = correlated_src
            break

    saved_stage = {
        "state": "recorded",
        "evidence": _make_evidence("runtime_receipt", latest_art["observed_at"]),
    }
    displayed_stage = _find_correlated_render_receipt(task_id, latest_art, task_row)
    submitted_stage = {"state": "not_recorded", "evidence": _make_evidence("none", None)}
    semantic_stage = {"state": "not_recorded", "evidence": _make_evidence("none", None)}

    obs_model_id = latest_art.get("model_id")
    obs_goal_rev = latest_art["goal_revision"]
    obs_scope_rev = latest_art["scope_revision"]
    obs_control_rev = latest_art.get("control_revision")
    obs_context_epoch = latest_art["context_epoch"]
    obs_task_rev = latest_art.get("task_revision")
    obs_destination: dict[str, Any] | None = None
    obs_route_dest: dict[str, Any] | None = None

    if matched_link is not None:
        obs_model_id = _normalize_safe_model_id(matched_link.get("model_id"))
        obs_control_rev = matched_link["control_revision"]
        if obs_task_rev is None and type(matched_link.get("task_revision")) is int:
            obs_task_rev = matched_link["task_revision"]
        raw_dest = matched_link.get("model_destination")
        if isinstance(raw_dest, dict):
            obs_destination = {
                "catalog_model_id": str(raw_dest.get("catalog_model_id") or "").strip(),
                "provider_peer_id": str(raw_dest.get("provider_peer_id") or "").strip(),
                "source_id": str(raw_dest.get("source_id") or "").strip(),
                "destination_revision": str(raw_dest.get("destination_revision") or "").strip(),
            }
            obs_route_dest = {
                "catalog_model_id": obs_destination["catalog_model_id"],
                "provider_peer_id": obs_destination["provider_peer_id"],
                "source_id": obs_destination["source_id"],
                "destination_revision": obs_destination["destination_revision"],
                "source": str(matched_link.get("source") or "").strip(),
                "adapter": _map_adapter_kind(matched_link.get("adapter")) or "",
                "image_transport": str(matched_link.get("image_transport") or "").strip(),
            }

        req_id = str(matched_link["request_id"])
        corr_src = matched_link["_correlated_source_artifact"]
        raw_payload_sha = corr_src.get("payload_sha256")
        if raw_payload_sha is not None:
            payload_sha = str(raw_payload_sha).strip().lower()
        else:
            payload_sha = latest_art["sha256"]
        sub_imgs = matched_link.get("submitted_images") if isinstance(matched_link.get("submitted_images"), list) else []
        payload_matched = bool(
            _HEX_SHA256_RE.match(payload_sha)
            and any(
                isinstance(si, dict)
                and _HEX_SHA256_RE.match(str(si.get("payload_sha256") or ""))
                and str(si.get("payload_sha256") or "").strip().lower() == payload_sha
                and type(si.get("payload_bytes")) is int
                and si.get("payload_bytes") > 0
                for si in sub_imgs
            )
        )

        dispatch_status = matched_link.get("dispatch_status")
        if dispatch_status == "submitted" and matched_link.get("image_submitted") is True and payload_matched:
            sub_ts = (
                _normalize_iso_timestamp(matched_link.get("submitted_at") or matched_link.get("observed_at"))
                or _find_event_timestamp_for_request(task_row, req_id, stage="submitted")
                or latest_art["observed_at"]
            )
            submitted_stage = {
                "state": "recorded",
                "evidence": _make_evidence("runtime_receipt", sub_ts),
            }
            if matched_link.get("semantic_response_received") is True:
                sem_ts = (
                    _normalize_iso_timestamp(matched_link.get("completed_at") or matched_link.get("semantic_observed_at"))
                    or _find_event_timestamp_for_request(task_row, req_id, stage="semantic")
                    or sub_ts
                )
                semantic_stage = {
                    "state": "recorded",
                    "evidence": _make_evidence("runtime_receipt", sem_ts),
                }
            elif matched_link.get("semantic_response_failed") is True:
                semantic_stage = {"state": "failed", "evidence": _make_evidence("none", None)}
        elif dispatch_status in {"failed", "error"}:
            submitted_stage = {"state": "failed", "evidence": _make_evidence("none", None)}
            semantic_stage = {"state": "failed", "evidence": _make_evidence("none", None)}

    cur_model_id = context.get("model_id")
    cur_goal_rev = task_row.get("goal_revision")
    cur_scope_rev = task_row.get("scope_revision")
    cur_control_rev = task_row.get("control_revision")
    cur_context_epoch = task_row.get("context_epoch", 0)
    cur_task_dest = task_row.get("model_destination") if isinstance(task_row.get("model_destination"), dict) else None

    revisions_match = bool(
        matched_link is not None
        and cur_model_id is not None
        and obs_model_id is not None
        and obs_model_id == cur_model_id
        and type(obs_control_rev) is int
        and obs_control_rev == cur_control_rev
        and obs_goal_rev == cur_goal_rev
        and obs_scope_rev == cur_scope_rev
        and obs_context_epoch == cur_context_epoch
    )

    dest_matches = False
    if (
        revisions_match
        and isinstance(obs_destination, dict)
        and isinstance(obs_route_dest, dict)
        and isinstance(cur_task_dest, dict)
        and isinstance(current_route_dest, dict)
    ):
        cur_task_cat = str(cur_task_dest.get("catalog_model_id") or "").strip()
        cur_route_cat = str(current_route_dest.get("catalog_model_id") or "").strip()
        obs_cat = obs_destination["catalog_model_id"]

        cur_task_rev_id = str(cur_task_dest.get("destination_revision") or "").strip()
        cur_route_rev_id = str(current_route_dest.get("destination_revision") or "").strip()
        obs_rev_id = obs_destination["destination_revision"]

        cur_task_peer = str(cur_task_dest.get("provider_peer_id") or "").strip()
        cur_route_peer = str(current_route_dest.get("provider_peer_id") or "").strip()
        obs_peer = obs_destination["provider_peer_id"]

        cur_task_src_id = str(cur_task_dest.get("source_id") or "").strip()
        cur_route_src_id = str(current_route_dest.get("source_id") or "").strip()
        obs_src_id = obs_destination["source_id"]

        cur_source = str(current_route_dest.get("source") or "").strip()
        obs_source = obs_route_dest["source"]
        cur_adapter = _map_adapter_kind(current_route_dest.get("adapter")) or ""
        obs_adapter = obs_route_dest["adapter"]
        cur_transport = str(current_route_dest.get("image_transport") or "").strip()
        obs_transport = obs_route_dest["image_transport"]

        require_source_id = bool(
            cur_source == "local_api"
            or obs_source == "local_api"
            or cur_task_src_id
            or cur_route_src_id
            or obs_src_id
        )
        source_id_ok = (
            bool(obs_src_id and cur_task_src_id == obs_src_id and cur_route_src_id == obs_src_id)
            if require_source_id
            else (cur_task_src_id == obs_src_id == cur_route_src_id == "")
        )

        dest_matches = bool(
            obs_cat
            and obs_cat == cur_model_id == cur_task_cat == cur_route_cat
            and obs_rev_id
            and obs_rev_id == cur_task_rev_id == cur_route_rev_id
            and obs_peer
            and obs_peer == cur_task_peer == cur_route_peer
            and source_id_ok
            and obs_source
            and obs_source == cur_source
            and obs_adapter
            and obs_adapter == cur_adapter
            and obs_transport
            and obs_transport == cur_transport
        )

    if obs_task_rev is None:
        obs_task_rev = (
            context.get("task_revision")
            if (revisions_match and dest_matches)
            else _normalize_non_negative_int(obs_control_rev)
        )

    # Always compute obs_epoch strictly from observed fields; never backfill current task/route destination.
    obs_epoch = _compute_selection_epoch(
        selection_source="task",
        task_id=task_id,
        settings_revision=None,
        control_revision=obs_control_rev,
        scope_revision=obs_scope_rev,
        model_id=obs_model_id,
        task_destination=obs_destination,
        route_destination=obs_route_dest,
    )

    matches_current = bool(
        revisions_match
        and dest_matches
        and obs_task_rev == context.get("task_revision")
        and obs_epoch == context.get("selection_epoch")
    )

    return {
        "id": latest_art["id"],
        "task_revision": obs_task_rev,
        "selection_epoch": obs_epoch,
        "model_id": obs_model_id,
        "matches_current_selection": matches_current,
        "width": latest_art["width"],
        "height": latest_art["height"],
        "stages": {
            "saved": saved_stage,
            "displayed": displayed_stage,
            "submitted": submitted_stage,
            "semantic_observed": semantic_stage,
        },
    }


def build_computer_status_dto(app: Any, runtime: Any, requested_task_id: str | None = None) -> dict[str, Any]:
    startup_error = getattr(getattr(app, "state", None), "agent_startup_error", None)
    if startup_error is not None:
        raise AgentError("agent_computer_status_unavailable")
    if runtime is None:
        raise AgentError("agent_computer_status_unavailable")

    task_id = validate_requested_task_id(requested_task_id) if requested_task_id is not None else None
    agent_view = _read_agent_view_readonly(runtime, task_id)

    task_row: dict[str, Any] | None = None
    if task_id is not None:
        if agent_view.get("task_deleted") is True:
            raise AgentError("agent_task_deleted")
        raw_task = agent_view.get("task")
        if not isinstance(raw_task, dict):
            raise AgentError("agent_task_not_found")
        task_row = raw_task

    settings = agent_view.get("settings") if isinstance(agent_view.get("settings"), dict) else {}
    settings_revision = _normalize_non_negative_int(agent_view.get("settings_revision"))
    ext_state = _read_extension_state_readonly(runtime)

    plugin_sec = _project_plugin_section(ext_state)
    native_sec = _project_native_section(runtime)
    permissions_sec = _project_permissions_section(settings, task_id, task_row)
    session_sec = _project_session_section(runtime, task_id, task_row)
    model_sec, current_route_dest = _project_model_section(runtime, settings, task_id, task_row)

    if task_row is not None:
        selection_source = "task"
        task_revision = _normalize_non_negative_int(task_row.get("revision"))
        if task_revision is None:
            task_revision = _normalize_non_negative_int(task_row.get("control_revision"))
        control_rev = _normalize_non_negative_int(task_row.get("control_revision"))
        scope_rev = _normalize_non_negative_int(task_row.get("scope_revision"))
        task_dest = task_row.get("model_destination") if isinstance(task_row.get("model_destination"), dict) else None
    else:
        selection_source = "settings"
        task_revision = None
        control_rev = None
        scope_rev = None
        task_dest = (
            agent_view.get("default_model_destination")
            if isinstance(agent_view.get("default_model_destination"), dict)
            else None
        )

    selection_epoch = _compute_selection_epoch(
        selection_source=selection_source,
        task_id=task_id,
        settings_revision=settings_revision,
        control_revision=control_rev,
        scope_revision=scope_rev,
        model_id=model_sec.get("id"),
        task_destination=task_dest,
        route_destination=current_route_dest,
    )

    context_sec = {
        "task_id": task_id,
        "task_revision": task_revision,
        "settings_revision": settings_revision,
        "selection_source": selection_source,
        "model_id": model_sec.get("id"),
        "selection_epoch": selection_epoch,
    }

    latest_observation = _project_latest_observation(task_id, task_row, context_sec, current_route_dest)

    return {
        "schema_version": 1,
        "snapshot_id": f"snap_{uuid.uuid4().hex[:24]}",
        "generated_at": _utc_now_iso(),
        "context": context_sec,
        "plugin": plugin_sec,
        "native": native_sec,
        "permissions": permissions_sec,
        "session": session_sec,
        "model": model_sec,
        "latest_observation": latest_observation,
    }


def _error_status_and_code(exc: Exception) -> tuple[int, str]:
    if isinstance(exc, AgentError):
        code = str(exc)
        if code == "agent_task_deleted":
            return 410, "agent_task_deleted"
        if code == "agent_task_not_found":
            return 404, "agent_task_not_found"
        if code == "agent_invalid_task_id":
            return 400, "agent_invalid_task_id"
        if code.startswith("agent_invalid_"):
            return 400, "agent_invalid_request"
    return 503, "agent_computer_status_unavailable"


async def get_computer_status_response(app: Any, runtime: Any, request: Request) -> JSONResponse:
    try:
        requested_task_id = validate_query_params(request)
        payload = await asyncio.to_thread(build_computer_status_dto, app, runtime, requested_task_id)
        return JSONResponse(payload, status_code=200, headers=_no_store_headers())
    except Exception as exc:
        status_code, detail_code = _error_status_and_code(exc)
        failure("api_computer_status", exc)
        return JSONResponse({"detail": detail_code}, status_code=status_code, headers=_no_store_headers())
