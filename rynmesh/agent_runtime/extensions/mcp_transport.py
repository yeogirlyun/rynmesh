"""Bounded STDIO and Streamable HTTP MCP client sessions and session manager."""

from __future__ import annotations

import ipaddress
import json
import os
import queue
import re
import shutil
import sys
import tempfile
import threading
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlsplit

import httpx

from ...llm_package import process_guard
from ..store import AgentError, AgentToolRejected
from .mcp_oauth import (
    USER_AGENT,
    McpOAuthError,
    McpOAuthManager,
    has_static_authorization,
    parse_auth_challenge,
)
from .store import (
    COMPUTER_USE_PLUGIN_ID,
    COMPUTER_USE_SERVER_ID,
    DEFAULT_STARTUP_TIMEOUT_MS,
    DEFAULT_TOOL_TIMEOUT_MS,
    EXTENSION_ID_RE,
    MAX_STARTUP_TIMEOUT_MS,
    MAX_TOOL_TIMEOUT_MS,
    ExtensionStore,
    _canon_sha256,
    _slugify,
)

# A remote plugin can add tools at any time (for example a new company data
# query). Re-read its catalog in the background so the Agent never believes a
# tool "does not exist" only because the list was read days ago.
CATALOG_REFRESH_SECONDS = 300


def _catalog_signature(tools: list[dict[str, Any]], *, stored: bool) -> frozenset:
    rows = []
    for tool in tools or []:
        if not isinstance(tool, dict) or not str(tool.get("name") or "").strip():
            continue
        if stored:
            schema_sha = tool.get("schema_sha256")
            read_only = (tool.get("annotations") or {}).get("readOnlyHint") is True
        else:
            schema = tool.get("inputSchema") or tool.get("input_schema") or {"type": "object"}
            schema_sha = _canon_sha256(schema if isinstance(schema, dict) else {"type": "object"})
            read_only = isinstance(tool.get("annotations"), dict) and tool["annotations"].get("readOnlyHint") is True
        rows.append((str(tool["name"]).strip(), schema_sha, str(tool.get("description") or "")[:1000], read_only))
    return frozenset(rows)

PROTOCOL_VERSION = "2025-03-26"
STREAMABLE_HTTP_PROTOCOL_VERSION = "2025-06-18"
SUPPORTED_PROTOCOL_VERSIONS = {
    "2025-11-25",
    "2025-06-18",
    "2025-03-26",
    "2024-11-05",
}
MAX_REQUEST_BYTES = 512 * 1024
MAX_OUTPUT_BYTES = 4 * 1024 * 1024
MAX_TOOL_PAGES = 16
MAX_DISCOVERED_TOOLS = 256
SESSION_LIFETIME_SECONDS = 300.0

_SSE_EVENT_SEP_RE = re.compile(rb"\r?\n\r?\n")

ALLOWLISTED_MCP_RPC_ERROR_CODES = frozenset({
    "agent_cancelled",
    "agent_computer_windows_required",
    "agent_computer_accessibility_unavailable",
    "agent_computer_window_changed_read_again",
    "agent_computer_click_outside_window",
    "agent_computer_text_required",
    "agent_computer_element_required",
    "agent_computer_keys_required",
    "agent_computer_key_unsupported",
    "agent_computer_input_blocked",
    "agent_computer_focus_blocked",
    "agent_computer_password_field",
    "agent_computer_action_failed",
    "agent_screenshot_requires_new_png",
    "agent_mcp_rpc_rejected",
    "agent_mcp_rpc_error",
    "agent_mcp_tool_disabled",
    "agent_mcp_server_disabled",
    "agent_permission_denied",
    "agent_invalid_tool_arguments",
    "agent_unknown_tool",
})


_CONTROL_CHARS_RE = re.compile(r"[\x00-\x1f\x7f]+")
_AUTH_SCHEME_RE = re.compile(r"(?i)\b(Bearer|Basic)\s+[^\s,;\"'<>]+")
_AUTH_KV_RE = re.compile(r"(?i)(authorization\s*[:=]\s*)[^\s,;\"'<>]+")
_LONG_TOKEN_RE = re.compile(r"[A-Za-z0-9+/=_-]{40,}(?:\.[A-Za-z0-9+/=_-]+)*")
_NON_SECRET_REQUEST_HEADERS = frozenset({"accept", "content-type", "user-agent"})


def _http_timeout(seconds: float) -> httpx.Timeout:
    bounded = max(0.05, float(seconds))
    return httpx.Timeout(bounded, connect=bounded, read=bounded, write=bounded, pool=bounded)


def _extract_request_redact_values(
    headers: dict[str, str] | None,
    extra_values: list[str] | None = None,
) -> list[str]:
    vals: list[str] = []
    if isinstance(headers, dict):
        for key, val in headers.items():
            if isinstance(key, str) and key.strip().lower() in _NON_SECRET_REQUEST_HEADERS:
                continue
            if isinstance(val, str) and len(val.strip()) >= 4:
                vals.append(val)
    for item in extra_values or []:
        if isinstance(item, str) and len(item.strip()) >= 4:
            vals.append(item)
    return vals


def _redact_secret_values(text: str, redact_values: list[str] | None) -> str:
    if not redact_values:
        return text
    cleaned = text
    candidates: list[str] = []
    for secret in redact_values:
        if isinstance(secret, str) and len(secret.strip()) >= 4:
            s_trim = secret.strip()
            candidates.append(s_trim)
            parts = s_trim.split(None, 1)
            if len(parts) == 2 and len(parts[1].strip()) >= 4:
                candidates.append(parts[1].strip())
    for tok in sorted(set(candidates), key=len, reverse=True):
        cleaned = cleaned.replace(tok, "[REDACTED]")
    return cleaned


def _redact_long_tokens(text: str) -> str:
    def _sub(match: re.Match[str]) -> str:
        tok = match.group(0)
        if len(set(tok)) >= 6 and (
            any(ch.isdigit() for ch in tok) or any(ch in "+/=." for ch in tok)
        ):
            return "[REDACTED]"
        return tok

    return _LONG_TOKEN_RE.sub(_sub, text)


def _clean_mcp_error_message(
    raw: Any,
    *,
    redact_values: list[str] | None = None,
    limit: int = 500,
) -> str:
    if not isinstance(raw, str):
        return ""
    cleaned = _CONTROL_CHARS_RE.sub(" ", raw).strip()
    if not cleaned:
        return ""
    cleaned = _redact_secret_values(cleaned, redact_values)
    cleaned = _AUTH_SCHEME_RE.sub(r"\1 [REDACTED]", cleaned)
    cleaned = _AUTH_KV_RE.sub(r"\1[REDACTED]", cleaned)
    cleaned = _redact_long_tokens(cleaned)
    return cleaned[:limit]


def _sanitize_http_error_body(
    raw_bytes: bytes,
    *,
    redact_values: list[str] | None = None,
    limit: int = 1000,
) -> str:
    if not raw_bytes:
        return ""
    text = raw_bytes[:2048].decode("utf-8", errors="replace")
    cleaned = _CONTROL_CHARS_RE.sub(" ", text).strip()
    if not cleaned:
        return ""
    cleaned = _redact_secret_values(cleaned, redact_values)
    cleaned = _AUTH_SCHEME_RE.sub(r"\1 [REDACTED]", cleaned)
    cleaned = _AUTH_KV_RE.sub(r"\1[REDACTED]", cleaned)
    cleaned = _redact_long_tokens(cleaned)
    return cleaned[:limit]


class McpTransportError(AgentError):
    """MCP transport or protocol failure with explicit dispatch tracking."""

    def __init__(
        self,
        code: str,
        *,
        dispatched: bool = False,
        phase: str | None = None,
        request_id: int | None = None,
        rpc_code: int | None = None,
        http_status: int | None = None,
        transport: str | None = None,
        server_id: str | None = None,
        session_scope: str | None = None,
        session_pid: int | None = None,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(code)
        self.dispatched = bool(dispatched)
        self.phase = phase
        self.request_id = request_id
        self.rpc_code = rpc_code
        self.http_status = http_status
        self.transport = transport
        self.server_id = server_id
        self.session_scope = session_scope
        self.session_pid = session_pid
        self.details: dict[str, Any] | None = (
            dict(details) if isinstance(details, dict) and details else None
        )


_STDIO_CA_FILE_VARS = frozenset(
    {"SSL_CERT_FILE", "REQUESTS_CA_BUNDLE", "NODE_EXTRA_CA_CERTS"}
)


def _host_env_case_insensitive(name: str) -> str | None:
    target = name.upper()
    for key, val in os.environ.items():
        if key.upper() == target and val:
            return val
    return None


def _stdio_environment(
    private_dir: str,
    secret_env: dict[str, str] | None,
    env_vars: list[str] | None = None,
    *,
    home_dir: str | None = None,
    cache_dir: str | None = None,
    inherit_user_home: bool = False,
    plugin_root: str | None = None,
    plugin_data: str | None = None,
) -> dict[str, str]:
    keep = {
        "PATH",
        "SYSTEMROOT",
        "WINDIR",
        "COMSPEC",
        "PATHEXT",
        "LANG",
        "LC_ALL",
        "PYTHONPATH",
        "VIRTUAL_ENV",
        "SYSTEMDRIVE",
        "USERNAME",
        "USER",
        "LOGNAME",
        "SHELL",
        "TERM",
        "TZ",
        "PROGRAMFILES",
        "PROGRAMFILES(X86)",
        "PROGRAMDATA",
        "ALLUSERSPROFILE",
        "NUMBER_OF_PROCESSORS",
        "PROCESSOR_ARCHITECTURE",
        "HTTP_PROXY",
        "HTTPS_PROXY",
        "NO_PROXY",
        "ALL_PROXY",
        "NODE_PATH",
    }
    env: dict[str, str] = {}
    for key, value in os.environ.items():
        upper = key.upper()
        if upper in keep:
            env[key] = value
        elif upper in _STDIO_CA_FILE_VARS and value.strip():
            env[key] = os.path.abspath(os.path.expanduser(value.strip()))
        elif upper == "SSL_CERT_DIR" and value.strip():
            parts = [
                os.path.abspath(os.path.expanduser(p.strip()))
                for p in value.split(os.pathsep)
                if p.strip()
            ]
            if parts:
                env[key] = os.pathsep.join(parts)

    if inherit_user_home:
        real_home = (
            _host_env_case_insensitive("HOME")
            or _host_env_case_insensitive("USERPROFILE")
            or str(Path.home())
        )
        real_profile = _host_env_case_insensitive("USERPROFILE") or real_home
        real_appdata = _host_env_case_insensitive("APPDATA") or real_home
        real_localappdata = _host_env_case_insensitive("LOCALAPPDATA") or real_home
        env.update(
            HOME=real_home,
            USERPROFILE=real_profile,
            APPDATA=real_appdata,
            LOCALAPPDATA=real_localappdata,
        )
    else:
        effective_home = home_dir or private_dir
        env.update(
            HOME=effective_home,
            USERPROFILE=effective_home,
            APPDATA=effective_home,
            LOCALAPPDATA=effective_home,
        )

    env.update(
        TMP=private_dir,
        TEMP=private_dir,
        TMPDIR=private_dir,
        PYTHONIOENCODING="utf-8",
        PYTHONUTF8="1",
    )
    if cache_dir:
        env["npm_config_cache"] = cache_dir
        env["UV_CACHE_DIR"] = cache_dir
        env["PIP_CACHE_DIR"] = cache_dir

    if isinstance(env_vars, list):
        for var_name in env_vars:
            if isinstance(var_name, str) and var_name in os.environ:
                env[var_name] = os.environ[var_name]
    if isinstance(secret_env, dict):
        for k, v in secret_env.items():
            if isinstance(k, str) and k and v is not None:
                env[k] = str(v)

    if plugin_root is not None or plugin_data is not None:
        protected = {
            *(["PLUGIN_ROOT"] if plugin_root is not None else []),
            *(["PLUGIN_DATA"] if plugin_data is not None else []),
        }
        for existing_key in [ek for ek in env if ek.upper() in protected]:
            env.pop(existing_key, None)
        if plugin_root is not None:
            env["PLUGIN_ROOT"] = plugin_root
        if plugin_data is not None:
            env["PLUGIN_DATA"] = plugin_data
    return env


def _resolve_executable(command: str, cwd: str | None) -> str:
    cmd = (command or "").strip()
    if not cmd:
        raise McpTransportError("agent_mcp_command_required", dispatched=False)
    cmd_path = Path(cmd)
    if cmd_path.is_absolute():
        if cmd_path.is_file():
            return str(cmd_path)
        raise McpTransportError("agent_mcp_executable_not_found", dispatched=False)
    if "/" in cmd or "\\" in cmd:
        base = Path(cwd) if cwd else Path.cwd()
        candidate = (base / cmd_path).resolve()
        if candidate.is_file():
            return str(candidate)
    found = shutil.which(cmd)
    if found:
        return found
    raise McpTransportError("agent_mcp_executable_not_found", dispatched=False)


# A server may say its tool list changed while it answers another request. The
# manager re-reads that server's list; such notices do not count toward the
# per-session notification limit because each only schedules one refresh.
TOOLS_LIST_CHANGED = "notifications/tools/list_changed"


def _seconds_since_listed(server: dict[str, Any]) -> float:
    """Age of the stored tool list; unknown counts as old."""
    try:
        listed = datetime.fromisoformat(str(server.get("last_tested_at") or ""))
        if listed.tzinfo is None:
            listed = listed.replace(tzinfo=timezone.utc)
        return max(0.0, (datetime.now(timezone.utc) - listed).total_seconds())
    except (TypeError, ValueError):
        return float("inf")


def _announce_tools_changed(session: Any) -> None:
    callback = getattr(session, "on_tools_changed", None)
    if callable(callback):
        try:
            callback()
        except Exception:
            pass


class StdioMcpSession:
    """Serial, bounded STDIO MCP client session using process_guard without shell=True."""

    def __init__(
        self,
        server: dict[str, Any],
        *,
        fallback_cwd: str | Path,
        store_root: str | Path | None = None,
        cancelled: Callable[[], bool] = lambda: False,
    ) -> None:
        self.server_id = str(server["id"])
        self.instance_id = str(server.get("instance_id", ""))
        self.revision = int(server.get("revision", 1))
        self.config_generation = int(server.get("config_generation", 1))
        self.startup_timeout = max(
            1.0,
            min(
                MAX_STARTUP_TIMEOUT_MS / 1000.0,
                int(server.get("startup_timeout_ms", DEFAULT_STARTUP_TIMEOUT_MS)) / 1000.0,
            ),
        )
        self.tool_timeout = max(
            1.0,
            min(
                MAX_TOOL_TIMEOUT_MS / 1000.0,
                int(server.get("tool_timeout_ms", DEFAULT_TOOL_TIMEOUT_MS)) / 1000.0,
            ),
        )
        self.cancelled = cancelled
        self._request_cancelled: Callable[[], bool] | None = None
        session_lifetime = max(
            SESSION_LIFETIME_SECONDS,
            self.tool_timeout,
            11.0 * self.startup_timeout,
        )
        self.deadline = time.monotonic() + session_lifetime
        self.sequence = 0
        self.output_bytes = 0
        self.notifications = 0
        self.initialized = False
        self.server_info: dict[str, Any] = {}
        self.failure: str | None = None
        self.closed = threading.Event()
        self.lock = threading.RLock()
        self.termination_lock = threading.Lock()
        self.messages: queue.Queue[dict[str, Any] | None] = queue.Queue(maxsize=64)

        if self._is_cancelled():
            raise McpTransportError("agent_cancelled", dispatched=False)

        fallback_path = Path(fallback_cwd).resolve()
        if store_root is not None:
            root_path = Path(store_root).resolve()
        elif fallback_path.name == "mcp_runtime":
            root_path = fallback_path.parent
        else:
            root_path = fallback_path

        runtime_dir = (root_path / "mcp_runtime").resolve()
        runtime_dir.mkdir(parents=True, exist_ok=True)

        safe_sid = (
            self.server_id
            if EXTENSION_ID_RE.match(self.server_id)
            else _slugify(self.server_id, "mcp-server")
        )
        cache_dir = (root_path / "mcp_cache" / safe_sid).resolve()
        cache_dir.mkdir(parents=True, exist_ok=True)

        inherit_user_home = bool(server.get("inherit_user_home", False))
        home_dir: Path | None = None
        if not inherit_user_home:
            home_dir = (root_path / "mcp_home" / safe_sid).resolve()
            home_dir.mkdir(parents=True, exist_ok=True)

        plugin_id = str(server.get("plugin_id") or "").strip()
        is_builtin_plugin = bool(server.get("builtin", False)) or plugin_id == COMPUTER_USE_PLUGIN_ID
        raw_pkg_dir = server.get("plugin_package_dir") or server.get("package_dir")
        plugin_root_path = Path(str(raw_pkg_dir)).resolve() if raw_pkg_dir else None
        plugin_data_path: Path | None = None
        if plugin_id and not is_builtin_plugin:
            raw_data_dir = server.get("plugin_data_dir")
            safe_pid = (
                plugin_id
                if EXTENSION_ID_RE.match(plugin_id)
                else _slugify(plugin_id, "plugin")
            )
            plugin_data_path = (
                Path(str(raw_data_dir)).resolve()
                if raw_data_dir
                else (root_path / "plugin_data" / safe_pid).resolve()
            )
            plugin_data_path.mkdir(parents=True, exist_ok=True)
            if plugin_root_path is None and server.get("cwd"):
                plugin_root_path = Path(str(server["cwd"])).resolve()

        cwd_raw = server.get("cwd")
        if cwd_raw and str(cwd_raw).strip():
            cwd_path = Path(str(cwd_raw)).resolve()
        elif plugin_root_path is not None:
            cwd_path = plugin_root_path
        else:
            cwd_path = fallback_path
        cwd_path.mkdir(parents=True, exist_ok=True)
        exe = _resolve_executable(str(server.get("command") or ""), str(cwd_path))
        args = [exe, *[str(a) for a in (server.get("args") or [])]]

        self.private_dir = tempfile.TemporaryDirectory(
            prefix=".ryn-mcp-", dir=str(runtime_dir), ignore_cleanup_errors=True
        )
        env = _stdio_environment(
            self.private_dir.name,
            server.get("secret_env"),
            server.get("env_vars"),
            home_dir=str(home_dir) if home_dir is not None else None,
            cache_dir=str(cache_dir),
            inherit_user_home=inherit_user_home,
            plugin_root=str(plugin_root_path) if plugin_root_path is not None else None,
            plugin_data=str(plugin_data_path) if plugin_data_path is not None else None,
        )
        stdio_secrets: list[str] = []
        for s_key, s_val in (server.get("secret_env") or {}).items():
            if (
                plugin_id
                and not is_builtin_plugin
                and str(s_key).upper() in {"PLUGIN_ROOT", "PLUGIN_DATA"}
            ):
                continue
            if isinstance(s_val, str) and len(s_val.strip()) >= 4:
                stdio_secrets.append(s_val)
        for env_k in server.get("env_vars") or []:
            env_v = env.get(str(env_k))
            if isinstance(env_v, str) and len(env_v.strip()) >= 4:
                stdio_secrets.append(env_v)
        self._redact_env_values = stdio_secrets
        if (
            getattr(sys, "frozen", False)
            and self.server_id == COMPUTER_USE_SERVER_ID
            and Path(exe).resolve() == Path(sys.executable).resolve()
            and "--ryn-computer-use-worker" in args[1:]
        ):
            env = process_guard._frozen_worker_env(env)
        try:
            self.process = process_guard.spawn(
                args,
                cwd=str(cwd_path),
                env=env,
                timeout=session_lifetime,
            )
        except (OSError, ValueError) as exc:
            self.private_dir.cleanup()
            raise McpTransportError("agent_mcp_start_failed", dispatched=False) from exc

        self.reader = threading.Thread(
            target=self._read_loop, daemon=True, name=f"ryn-mcp-stdio-{self.server_id}"
        )
        self.watcher = threading.Thread(
            target=self._watch_loop, daemon=True, name=f"ryn-mcp-watch-{self.server_id}"
        )
        self.reader.start()
        self.watcher.start()

    def _is_cancelled(self) -> bool:
        try:
            if self.cancelled():
                return True
            active = self._request_cancelled
            return bool(active and active() and self._request_cancelled is active)
        except Exception:
            return True

    @contextmanager
    def _cancel_scope(self, cancelled: Callable[[], bool] | None):
        self._request_cancelled = cancelled
        try:
            yield
        finally:
            self._request_cancelled = None

    def _read_loop(self) -> None:
        try:
            while not self.closed.is_set():
                line = self.process.stdout.readline(MAX_OUTPUT_BYTES + 1)
                if not line:
                    break
                self.output_bytes += len(line)
                if self.output_bytes > MAX_OUTPUT_BYTES or not line.endswith(b"\n"):
                    self.failure = "agent_mcp_output_limit"
                    break
                stripped = line.strip()
                if not stripped:
                    continue
                value = json.loads(stripped)
                if not isinstance(value, dict) or value.get("jsonrpc") != "2.0":
                    self.failure = "agent_mcp_protocol_invalid"
                    break
                self.messages.put(value, timeout=0.2)
        except (ValueError, UnicodeError, OSError, queue.Full):
            self.failure = self.failure or "agent_mcp_protocol_invalid"
        finally:
            try:
                self.messages.put_nowait(None)
            except queue.Full:
                pass

    def _watch_loop(self) -> None:
        while not self.closed.wait(0.05):
            cancelled = self._is_cancelled()
            if cancelled or time.monotonic() >= self.deadline or self.failure:
                self.failure = self.failure or (
                    "agent_cancelled" if cancelled else "agent_mcp_timeout"
                )
                self._terminate()
                return

    def _terminate(self) -> None:
        with self.termination_lock:
            try:
                process_guard.terminate(self.process)
            except Exception:
                pass

    def is_usable(self) -> bool:
        return (
            not self.closed.is_set()
            and self.failure is None
            and time.monotonic() < self.deadline
            and self.process.poll() is None
        )

    def _check(
        self,
        deadline: float,
        *,
        dispatched: bool,
        phase: str | None = None,
        timeout_seconds: float | None = None,
        method: str | None = None,
    ) -> None:
        if self._is_cancelled():
            self.failure = self.failure or "agent_cancelled"
        elif not dispatched and self.process.poll() is not None:
            self.failure = self.failure or "agent_mcp_process_disconnected"
        if self.failure:
            if self.failure == "agent_mcp_timeout":
                eff_phase = phase or ("await_response" if dispatched else "pre_dispatch")
                t_details: dict[str, Any] = {"phase": eff_phase}
                if timeout_seconds is not None:
                    t_details["timeout_seconds"] = round(float(timeout_seconds), 3)
                if method:
                    t_details["method"] = str(method)
                raise McpTransportError(
                    "agent_mcp_timeout",
                    dispatched=dispatched,
                    phase=eff_phase,
                    details=t_details,
                )
            raise McpTransportError(self.failure, dispatched=dispatched)
        if self.closed.is_set():
            raise McpTransportError("agent_mcp_closed", dispatched=dispatched)
        if time.monotonic() >= min(deadline, self.deadline):
            eff_phase = phase or ("await_response" if dispatched else "pre_dispatch")
            t_details = {"phase": eff_phase}
            if timeout_seconds is not None:
                t_details["timeout_seconds"] = round(float(timeout_seconds), 3)
            if method:
                t_details["method"] = str(method)
            raise McpTransportError(
                "agent_mcp_timeout",
                dispatched=dispatched,
                phase=eff_phase,
                details=t_details,
            )

    def _send(
        self,
        payload: dict[str, Any],
        deadline: float,
        *,
        timeout_seconds: float | None = None,
    ) -> None:
        method = str(payload.get("method") or "") or None
        raw = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8") + b"\n"
        if len(raw) > MAX_REQUEST_BYTES:
            raise McpTransportError(
                "agent_mcp_request_too_large", dispatched=False, phase="pre_dispatch"
            )
        self._check(
            deadline,
            dispatched=False,
            phase="pre_dispatch",
            timeout_seconds=timeout_seconds,
            method=method,
        )
        errors: list[bool] = []
        done = threading.Event()

        def _write() -> None:
            try:
                self.process.stdin.write(raw)
                self.process.stdin.flush()
            except (OSError, ValueError):
                errors.append(True)
            finally:
                done.set()

        threading.Thread(target=_write, daemon=True, name="ryn-mcp-stdin").start()
        while not done.wait(0.02):
            self._check(
                deadline,
                dispatched=True,
                phase="send",
                timeout_seconds=timeout_seconds,
                method=method,
            )
        if errors:
            raise McpTransportError("agent_mcp_write_failed", dispatched=True, phase="send")

    def _exchange(
        self,
        method: str,
        params: dict[str, Any],
        timeout: float,
        *,
        before_dispatch: Callable[[], None] | None = None,
    ) -> dict[str, Any]:
        deadline = time.monotonic() + timeout
        self._check(
            deadline,
            dispatched=False,
            phase="pre_dispatch",
            timeout_seconds=timeout,
            method=method,
        )
        if before_dispatch is not None:
            try:
                before_dispatch()
            except BaseException as exc:
                if not hasattr(exc, "dispatched"):
                    exc.dispatched = False  # type: ignore[attr-defined]
                if getattr(exc, "phase", None) is None:
                    exc.phase = "pre_dispatch"  # type: ignore[attr-defined]
                self.close()
                raise
        self._check(
            deadline,
            dispatched=False,
            phase="pre_dispatch",
            timeout_seconds=timeout,
            method=method,
        )
        self.sequence += 1
        ident = self.sequence
        try:
            self._send(
                {"jsonrpc": "2.0", "id": ident, "method": method, "params": params},
                deadline,
                timeout_seconds=timeout,
            )
            while True:
                self._check(
                    deadline,
                    dispatched=True,
                    phase="await_response",
                    timeout_seconds=timeout,
                    method=method,
                )
                try:
                    message = self.messages.get(timeout=0.05)
                except queue.Empty:
                    continue
                if message is None:
                    raise McpTransportError(
                        self.failure or "agent_mcp_process_disconnected",
                        dispatched=True,
                        phase="await_response",
                        request_id=ident,
                    )
                if "id" not in message and str(message.get("method", "")).startswith("notifications/"):
                    if message.get("method") == TOOLS_LIST_CHANGED:
                        _announce_tools_changed(self)
                        continue
                    self.notifications += 1
                    if self.notifications > 64:
                        raise McpTransportError(
                            "agent_mcp_notification_limit",
                            dispatched=True,
                            phase="await_response",
                            request_id=ident,
                        )
                    continue
                if message.get("id") != ident:
                    raise McpTransportError(
                        "agent_mcp_protocol_invalid",
                        dispatched=True,
                        phase="response_parse",
                        request_id=ident,
                    )
                if "error" in message:
                    err_obj = message.get("error")
                    code = err_obj.get("code") if isinstance(err_obj, dict) else None
                    rpc_code = code if type(code) is int else None
                    raw_msg = err_obj.get("message") if isinstance(err_obj, dict) else None
                    cleaned_msg = _clean_mcp_error_message(
                        raw_msg,
                        redact_values=self._redact_env_values,
                        limit=500,
                    )
                    rpc_details: dict[str, Any] = {}
                    if cleaned_msg:
                        rpc_details["message"] = cleaned_msg
                    if rpc_code is not None:
                        rpc_details["rpc_code"] = rpc_code
                    if code in (-32602, -32601):
                        err_code = (
                            raw_msg
                            if isinstance(raw_msg, str) and raw_msg in ALLOWLISTED_MCP_RPC_ERROR_CODES
                            else "agent_mcp_rpc_rejected"
                        )
                        raise McpTransportError(
                            err_code,
                            dispatched=True,
                            phase="rpc_error",
                            request_id=ident,
                            rpc_code=rpc_code,
                            details=rpc_details or None,
                        )
                    raise McpTransportError(
                        "agent_mcp_rpc_error",
                        dispatched=True,
                        phase="rpc_error",
                        request_id=ident,
                        rpc_code=rpc_code,
                        details=rpc_details or None,
                    )
                result = message.get("result")
                if not isinstance(result, dict):
                    raise McpTransportError(
                        "agent_mcp_protocol_invalid",
                        dispatched=True,
                        phase="response_parse",
                        request_id=ident,
                    )
                return result
        except AgentToolRejected:
            raise
        except BaseException as exc:
            if getattr(exc, "request_id", None) is None:
                exc.request_id = ident  # type: ignore[attr-defined]
            if getattr(exc, "phase", None) is None:
                exc.phase = "await_response" if getattr(exc, "dispatched", False) else "pre_dispatch"  # type: ignore[attr-defined]
            if getattr(exc, "transport", None) is None:
                exc.transport = "stdio"  # type: ignore[attr-defined]
            if getattr(exc, "server_id", None) is None:
                exc.server_id = self.server_id  # type: ignore[attr-defined]
            pid = getattr(self.process, "pid", None)
            if getattr(exc, "session_pid", None) is None and type(pid) is int:
                exc.session_pid = pid  # type: ignore[attr-defined]
            if isinstance(exc, McpTransportError) and str(exc) == "agent_mcp_timeout":
                d = dict(exc.details or {})
                d.setdefault("phase", exc.phase or "await_response")
                d.setdefault("timeout_seconds", round(float(timeout), 3))
                d.setdefault("method", method)
                exc.details = d
            self.close()
            raise

    def initialize(self, *, cancelled: Callable[[], bool] | None = None) -> dict[str, Any]:
        with self.lock:
            if self.initialized:
                self._check(
                    self.deadline,
                    dispatched=False,
                    phase="initialize",
                    timeout_seconds=self.startup_timeout,
                    method="initialize",
                )
                if cancelled is not None and cancelled():
                    raise McpTransportError(
                        "agent_cancelled",
                        dispatched=False,
                        phase="pre_dispatch",
                        transport="stdio",
                        server_id=self.server_id,
                    )
                return self.server_info
            with self._cancel_scope(cancelled):
                result = self._exchange(
                    "initialize",
                    {
                        "protocolVersion": PROTOCOL_VERSION,
                        "capabilities": {},
                        "clientInfo": {"name": "Ryn", "version": "1.0"},
                    },
                    self.startup_timeout,
                )
                if (
                    result.get("protocolVersion") not in SUPPORTED_PROTOCOL_VERSIONS
                    or not isinstance(result.get("capabilities"), dict)
                ):
                    self.close()
                    raise McpTransportError(
                        "agent_mcp_protocol_unsupported",
                        dispatched=False,
                        phase="initialize",
                    )
                self._send(
                    {"jsonrpc": "2.0", "method": "notifications/initialized"},
                    time.monotonic() + self.startup_timeout,
                    timeout_seconds=self.startup_timeout,
                )
                self.initialized = True
                self.server_info = result
                return result

    def list_all_tools(self, *, cancelled: Callable[[], bool] | None = None) -> list[dict[str, Any]]:
        if not self.initialized:
            self.initialize(cancelled=cancelled)
        with self.lock:
            self._check(
                self.deadline,
                dispatched=False,
                phase="pre_dispatch",
                timeout_seconds=self.startup_timeout,
                method="tools/list",
            )
            if cancelled is not None and cancelled():
                raise McpTransportError(
                    "agent_cancelled",
                    dispatched=False,
                    phase="pre_dispatch",
                    transport="stdio",
                    server_id=self.server_id,
                )
            with self._cancel_scope(cancelled):
                tools: list[dict[str, Any]] = []
                cursor: str | None = None
                seen_cursors: set[str] = set()
                total_budget = 10.0 * self.startup_timeout
                total_deadline = time.monotonic() + total_budget
                for _ in range(MAX_TOOL_PAGES):
                    remaining_total = total_deadline - time.monotonic()
                    if remaining_total <= 0:
                        raise McpTransportError(
                            "agent_mcp_timeout",
                            dispatched=False,
                            phase="await_response",
                            details={
                                "phase": "await_response",
                                "timeout_seconds": round(total_budget, 3),
                                "method": "tools/list",
                            },
                        )
                    page_timeout = min(self.startup_timeout, remaining_total)
                    params = {"cursor": cursor} if cursor else {}
                    res = self._exchange("tools/list", params, page_timeout)
                    page_tools = res.get("tools")
                    if not isinstance(page_tools, list):
                        raise McpTransportError("agent_mcp_protocol_invalid", dispatched=False)
                    for item in page_tools:
                        if isinstance(item, dict) and isinstance(item.get("name"), str) and item["name"].strip():
                            tools.append(item)
                            if len(tools) > MAX_DISCOVERED_TOOLS:
                                raise McpTransportError("agent_mcp_tools_limit", dispatched=False)
                    next_cursor = res.get("nextCursor")
                    if not next_cursor or not isinstance(next_cursor, str):
                        break
                    if next_cursor in seen_cursors:
                        break
                    seen_cursors.add(next_cursor)
                    cursor = next_cursor
                return tools

    def call_tool(
        self,
        name: str,
        arguments: dict[str, Any],
        *,
        timeout: float | None = None,
        before_dispatch: Callable[[], None] | None = None,
        cancelled: Callable[[], bool] | None = None,
    ) -> dict[str, Any]:
        if not self.initialized:
            try:
                self.initialize(cancelled=cancelled)
            except BaseException as exc:
                exc.dispatched = False  # type: ignore[attr-defined]
                raise
        eff_timeout = (
            min(self.tool_timeout, max(0.05, float(timeout)))
            if timeout is not None
            else self.tool_timeout
        )
        with self.lock:
            self._check(
                self.deadline,
                dispatched=False,
                phase="pre_dispatch",
                timeout_seconds=eff_timeout,
                method="tools/call",
            )
            if cancelled is not None and cancelled():
                raise McpTransportError(
                    "agent_cancelled",
                    dispatched=False,
                    phase="pre_dispatch",
                    transport="stdio",
                    server_id=self.server_id,
                )
            with self._cancel_scope(cancelled):
                return self._exchange(
                    "tools/call",
                    {"name": name, "arguments": arguments},
                    eff_timeout,
                    before_dispatch=before_dispatch,
                )

    def close(self) -> None:
        if self.closed.is_set():
            return
        self.closed.set()
        self._terminate()
        self.reader.join(timeout=1.0)
        for stream in (self.process.stdin, self.process.stdout):
            try:
                stream.close()
            except Exception:
                pass
        self.private_dir.cleanup()


def connector_headers(server):
    headers = {}
    env_http_headers = server.get("env_http_headers") or {}
    if isinstance(env_http_headers, dict):
        for k, env_name in env_http_headers.items():
            if isinstance(k, str) and k and isinstance(env_name, str) and env_name:
                env_val = os.environ.get(env_name)
                if env_val:
                    headers[k] = str(env_val)
    secret_headers = server.get("secret_headers") or {}
    if isinstance(secret_headers, dict):
        for k, v in secret_headers.items():
            if isinstance(k, str) and k and v is not None:
                headers[k] = str(v)
    bearer_env = server.get("bearer_token_env")
    if bearer_env and not any(key.lower() == "authorization" for key in headers):
        token = os.environ.get(str(bearer_env))
        if not token:
            raise McpTransportError("agent_mcp_auth_required", dispatched=False)
        headers["Authorization"] = f"Bearer {token}"

    return headers


class StreamableHttpMcpSession:
    """Bounded Streamable HTTP MCP client supporting JSON and incremental SSE responses."""

    def __init__(
        self,
        server: dict[str, Any],
        *,
        cancelled: Callable[[], bool] = lambda: False,
        oauth: McpOAuthManager | None = None,
    ) -> None:
        self._server = server
        self.oauth = oauth if not has_static_authorization(server) and (server.get("auth") or {}).get("mode") != "none" else None
        self.server_id = str(server["id"])
        self.instance_id = str(server.get("instance_id", ""))
        self.revision = int(server.get("revision", 1))
        self.config_generation = int(server.get("config_generation", 1))
        self.url = str(server.get("url") or "").strip()
        if not self.url.startswith(("http://", "https://")):
            raise McpTransportError("agent_mcp_url_required", dispatched=False)
        self.startup_timeout = max(
            1.0,
            min(
                MAX_STARTUP_TIMEOUT_MS / 1000.0,
                int(server.get("startup_timeout_ms", DEFAULT_STARTUP_TIMEOUT_MS)) / 1000.0,
            ),
        )
        self.tool_timeout = max(
            1.0,
            min(
                MAX_TOOL_TIMEOUT_MS / 1000.0,
                int(server.get("tool_timeout_ms", DEFAULT_TOOL_TIMEOUT_MS)) / 1000.0,
            ),
        )
        self.cancelled = cancelled
        self.sequence = 0
        self.notifications = 0
        self.initialized = False
        self.session_id: str | None = None
        self.protocol_version: str | None = None
        self.server_info: dict[str, Any] = {}
        self.closed = threading.Event()
        self.lock = threading.RLock()

        if self._is_cancelled(None):
            raise McpTransportError("agent_cancelled", dispatched=False)

        headers: dict[str, str] = {
            "Accept": "application/json, text/event-stream",
            "Content-Type": "application/json",
            "User-Agent": USER_AGENT,
        }
        headers.update(connector_headers(server))
        self._base_headers = headers
        parsed_url = urlsplit(self.url)
        host = (parsed_url.hostname or "").strip().lower()
        is_loopback = host == "localhost"
        if not is_loopback and host:
            try:
                is_loopback = ipaddress.ip_address(host).is_loopback
            except ValueError:
                pass
        trust_env = not is_loopback
        mounts = (
            {"all://127.0.0.1": None, "all://[::1]": None, "all://localhost": None}
            if trust_env
            else None
        )
        self._client = httpx.Client(
            follow_redirects=False,
            timeout=_http_timeout(self.tool_timeout),
            trust_env=trust_env,
            verify=(parsed_url.scheme == "https"),
            mounts=mounts,
        )

    def is_usable(self) -> bool:
        return not self.closed.is_set() and not self._client.is_closed

    def _is_cancelled(self, cancelled: Callable[[], bool] | None) -> bool:
        try:
            return bool(self.cancelled() or (cancelled is not None and cancelled()))
        except Exception:
            return True

    def _check_deadline(
        self,
        deadline: float,
        cancelled: Callable[[], bool] | None,
        *,
        dispatched: bool,
        phase: str | None = None,
        timeout_seconds: float | None = None,
        method: str | None = None,
    ) -> None:
        if self.closed.is_set():
            raise McpTransportError("agent_mcp_closed", dispatched=dispatched)
        if self._is_cancelled(cancelled):
            raise McpTransportError("agent_cancelled", dispatched=dispatched)
        if time.monotonic() >= deadline:
            eff_phase = phase or ("await_response" if dispatched else "pre_dispatch")
            t_details: dict[str, Any] = {"phase": eff_phase}
            if timeout_seconds is not None:
                t_details["timeout_seconds"] = round(float(timeout_seconds), 3)
            if method:
                t_details["method"] = str(method)
            raise McpTransportError(
                "agent_mcp_timeout",
                dispatched=dispatched,
                phase=eff_phase,
                details=t_details,
            )

    def _remaining_timeout(
        self,
        deadline: float,
        cancelled: Callable[[], bool] | None,
        *,
        dispatched: bool,
        phase: str | None = None,
        timeout_seconds: float | None = None,
        method: str | None = None,
    ) -> float:
        self._check_deadline(
            deadline,
            cancelled,
            dispatched=dispatched,
            phase=phase,
            timeout_seconds=timeout_seconds,
            method=method,
        )
        return max(0.05, deadline - time.monotonic())

    @contextmanager
    def _watch_response(
        self,
        response: httpx.Response,
        deadline: float,
        cancelled: Callable[[], bool] | None,
    ):
        done = threading.Event()

        def _watchdog() -> None:
            while not done.wait(0.05):
                if self.closed.is_set() or time.monotonic() >= deadline or self._is_cancelled(cancelled):
                    try:
                        response.close()
                    except Exception:
                        pass
                    return

        watcher = threading.Thread(
            target=_watchdog, daemon=True, name=f"ryn-mcp-http-watch-{self.server_id}"
        )
        watcher.start()
        try:
            yield
        finally:
            done.set()

    @contextmanager
    def _watch_request(
        self,
        deadline: float,
        cancelled: Callable[[], bool] | None,
        response_box: list[httpx.Response | None],
    ):
        done = threading.Event()

        def _watchdog() -> None:
            while not done.wait(0.05):
                if self.closed.is_set() or time.monotonic() >= deadline or self._is_cancelled(cancelled):
                    resp = response_box[0]
                    if resp is not None:
                        try:
                            resp.close()
                        except Exception:
                            pass
                    else:
                        try:
                            self._client.close()
                        except Exception:
                            pass
                    return

        watcher = threading.Thread(
            target=_watchdog, daemon=True, name=f"ryn-mcp-http-watch-{self.server_id}"
        )
        watcher.start()
        try:
            yield
        finally:
            done.set()

    def _headers(self, *, refresh_on_error=False, refresh_state=None) -> dict[str, str]:
        hdrs = dict(self._base_headers)
        if self.session_id:
            hdrs["Mcp-Session-Id"] = self.session_id
        if self.protocol_version:
            hdrs["MCP-Protocol-Version"] = self.protocol_version
        if self.oauth:
            try:
                try:
                    token = self.oauth.access_token(self._server, refresh_state=refresh_state)
                except McpOAuthError as exc:
                    if str(exc) != "agent_mcp_auth_required" or not refresh_on_error or (refresh_state and refresh_state[0]):
                        raise
                    token = self.oauth.access_token(self._server, force_refresh=True, refresh_state=refresh_state)
            except McpOAuthError as exc:
                raise McpTransportError(str(exc), dispatched=False, phase="pre_dispatch") from None
            if token:
                hdrs["Authorization"] = "Bearer " + token
        return hdrs

    def _auth_error(self, response, *, dispatched, request_id=None):
        challenge = parse_auth_challenge(response.headers.get("WWW-Authenticate", ""))
        if self.oauth:
            try:
                self.oauth.record_challenge(self._server, challenge)
            except McpOAuthError:
                # Invalid metadata hints never initiate network discovery and
                # never replace the bounded local authentication error.
                challenge = {}
        error = McpTransportError("agent_mcp_auth_required", dispatched=dispatched,
            phase="http_status", request_id=request_id, http_status=int(response.status_code))
        error.auth_challenge = challenge
        return error

    @staticmethod
    def _check_content_length(response: httpx.Response, *, dispatched: bool) -> None:
        cl_header = response.headers.get("Content-Length") or response.headers.get("content-length")
        if cl_header:
            try:
                if int(cl_header) > MAX_OUTPUT_BYTES:
                    raise McpTransportError("agent_mcp_output_limit", dispatched=dispatched)
            except ValueError:
                pass

    def _extract_sse_message(
        self,
        sse_buffer: bytearray,
        expected_id: int,
        *,
        flush_final: bool = False,
    ) -> dict[str, Any] | None:
        while True:
            match = _SSE_EVENT_SEP_RE.search(sse_buffer)
            if match is None:
                if not flush_final or not sse_buffer.strip():
                    return None
                raw_event = bytes(sse_buffer)
                sse_buffer.clear()
            else:
                raw_event = bytes(sse_buffer[: match.start()])
                del sse_buffer[: match.end()]

            event_text = raw_event.decode("utf-8", errors="replace")
            data_lines: list[str] = []
            for line in event_text.splitlines():
                if line.startswith(":"):
                    continue
                if line.startswith("data:"):
                    data_lines.append(line[5:].lstrip())
            if not data_lines:
                continue
            payload_str = "\n".join(data_lines).strip()
            if not payload_str:
                continue
            try:
                parsed = json.loads(payload_str)
            except ValueError:
                raise McpTransportError("agent_mcp_protocol_invalid", dispatched=True) from None
            if not isinstance(parsed, dict) or parsed.get("jsonrpc") != "2.0":
                raise McpTransportError("agent_mcp_protocol_invalid", dispatched=True)
            if "id" not in parsed and str(parsed.get("method", "")).startswith("notifications/"):
                if parsed.get("method") == TOOLS_LIST_CHANGED:
                    _announce_tools_changed(self)
                    continue
                self.notifications += 1
                if self.notifications > 64:
                    raise McpTransportError("agent_mcp_notification_limit", dispatched=True)
                continue
            if parsed.get("id") != expected_id:
                raise McpTransportError("agent_mcp_protocol_invalid", dispatched=True)
            return parsed

    def _exchange(
        self,
        method: str,
        params: dict[str, Any],
        timeout: float,
        *,
        before_dispatch: Callable[[], None] | None = None,
        cancelled: Callable[[], bool] | None = None,
        _deadline: float | None = None,
        _auth_retry: bool = False,
    ) -> dict[str, Any]:
        deadline = _deadline if _deadline is not None else (time.monotonic() + timeout)
        self._check_deadline(
            deadline,
            cancelled,
            dispatched=False,
            phase="pre_dispatch",
            timeout_seconds=timeout,
            method=method,
        )

        self.sequence += 1
        ident = self.sequence
        body = json.dumps(
            {"jsonrpc": "2.0", "id": ident, "method": method, "params": params},
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
        if len(body) > MAX_REQUEST_BYTES:
            raise McpTransportError(
                "agent_mcp_request_too_large", dispatched=False, phase="pre_dispatch"
            )

        refresh_state = [_auth_retry]
        headers = self._headers(refresh_on_error=method in {"initialize", "tools/list"}, refresh_state=refresh_state)
        # Refresh can block. Authenticate before marking a tool dispatched,
        # then recheck cancellation/configuration immediately before sending.
        self._check_deadline(
            deadline,
            cancelled,
            dispatched=False,
            phase="pre_dispatch",
            timeout_seconds=timeout,
            method=method,
        )
        if before_dispatch is not None:
            try:
                before_dispatch()
            except BaseException as exc:
                if not hasattr(exc, "dispatched"):
                    exc.dispatched = False  # type: ignore[attr-defined]
                if getattr(exc, "phase", None) is None:
                    exc.phase = "pre_dispatch"  # type: ignore[attr-defined]
                self.close()
                raise
        req_timeout = self._remaining_timeout(
            deadline,
            cancelled,
            dispatched=False,
            phase="pre_dispatch",
            timeout_seconds=timeout,
            method=method,
        )
        redact_vals = _extract_request_redact_values(
            headers,
            [
                v
                for v in (self._server.get("secret_headers") or {}).values()
                if isinstance(v, str)
            ],
        )
        dispatched = False
        message: dict[str, Any] | None = None
        resp_box: list[httpx.Response | None] = [None]
        try:
            dispatched = True
            with self._watch_request(deadline, cancelled, resp_box):
                with self._client.stream(
                    "POST",
                    self.url,
                    content=body,
                    headers=headers,
                    timeout=_http_timeout(req_timeout),
                ) as response:
                    resp_box[0] = response
                    self._check_deadline(
                        deadline,
                        cancelled,
                        dispatched=True,
                        phase="await_response",
                        timeout_seconds=timeout,
                        method=method,
                    )
                    if response.status_code in (401, 403):
                        raise self._auth_error(response, dispatched=True, request_id=ident)
                    if response.status_code >= 300:
                        err_buf = bytearray()
                        try:
                            for err_chunk in response.iter_bytes():
                                if self._is_cancelled(cancelled):
                                    raise McpTransportError("agent_cancelled", dispatched=True)
                                err_buf.extend(err_chunk[: max(0, 2048 - len(err_buf))])
                                if len(err_buf) >= 2048 or time.monotonic() >= deadline:
                                    break
                        except McpTransportError:
                            raise
                        except (httpx.HTTPError, OSError):
                            pass
                        http_body = _sanitize_http_error_body(
                            bytes(err_buf),
                            redact_values=redact_vals,
                            limit=1000,
                        )
                        http_details: dict[str, Any] = {"http_status": int(response.status_code)}
                        if http_body:
                            http_details["http_body"] = http_body
                        raise McpTransportError(
                            "agent_mcp_http_error",
                            dispatched=True,
                            phase="http_status",
                            request_id=ident,
                            http_status=int(response.status_code),
                            details=http_details,
                        )
                    self._check_content_length(response, dispatched=True)
                    sess_hdr = response.headers.get("Mcp-Session-Id") or response.headers.get("mcp-session-id")
                    if sess_hdr:
                        self.session_id = sess_hdr.strip()

                    ctype = (response.headers.get("Content-Type") or "").lower()
                    is_sse = "text/event-stream" in ctype
                    sse_buffer = bytearray()
                    chunks: list[bytes] = []
                    total = 0

                    for chunk in response.iter_bytes():
                        self._check_deadline(
                            deadline,
                            cancelled,
                            dispatched=True,
                            phase="await_response",
                            timeout_seconds=timeout,
                            method=method,
                        )
                        total += len(chunk)
                        if total > MAX_OUTPUT_BYTES:
                            raise McpTransportError(
                                "agent_mcp_output_limit",
                                dispatched=True,
                                phase="await_response",
                                request_id=ident,
                            )
                        if is_sse:
                            sse_buffer.extend(chunk)
                            message = self._extract_sse_message(sse_buffer, ident, flush_final=False)
                            if message is not None:
                                break
                        else:
                            chunks.append(chunk)

                    self._check_deadline(
                        deadline,
                        cancelled,
                        dispatched=True,
                        phase="await_response",
                        timeout_seconds=timeout,
                        method=method,
                    )
                    if is_sse:
                        if message is None:
                            message = self._extract_sse_message(sse_buffer, ident, flush_final=True)
                        if message is None:
                            raise McpTransportError(
                                "agent_mcp_protocol_invalid",
                                dispatched=True,
                                phase="response_parse",
                                request_id=ident,
                            )
                    else:
                        raw = b"".join(chunks)
                        try:
                            message = json.loads(raw.decode("utf-8"))
                        except Exception as exc:
                            raise McpTransportError(
                                "agent_mcp_protocol_invalid",
                                dispatched=True,
                                phase="response_parse",
                                request_id=ident,
                            ) from exc
        except McpTransportError as exc:
            if (exc.http_status == 401 and self.oauth and headers.get("Authorization")
                    and method in {"initialize", "tools/list"} and not refresh_state[0]):
                try:
                    self.oauth.access_token(self._server, force_refresh=True, refresh_state=refresh_state)
                except McpOAuthError as auth_exc:
                    raise McpTransportError(str(auth_exc), dispatched=True, phase="http_status",
                        request_id=ident, http_status=401, transport="streamable_http", server_id=self.server_id) from None
                return self._exchange(method, params, timeout, before_dispatch=before_dispatch,
                    cancelled=cancelled, _deadline=deadline, _auth_retry=True)
            if exc.request_id is None:
                exc.request_id = ident
            if exc.phase is None:
                exc.phase = "await_response" if exc.dispatched else "pre_dispatch"
            if exc.transport is None:
                exc.transport = "streamable_http"
            if exc.server_id is None:
                exc.server_id = self.server_id
            if str(exc) == "agent_mcp_timeout":
                d = dict(exc.details or {})
                d.setdefault("phase", exc.phase)
                d.setdefault("timeout_seconds", round(float(timeout), 3))
                d.setdefault("method", method)
                exc.details = d
            raise
        except httpx.TimeoutException as exc:
            eff_phase = "await_response" if dispatched else "pre_dispatch"
            raise McpTransportError(
                "agent_mcp_timeout",
                dispatched=dispatched,
                phase=eff_phase,
                request_id=ident,
                transport="streamable_http",
                server_id=self.server_id,
                details={
                    "phase": eff_phase,
                    "timeout_seconds": round(float(timeout), 3),
                    "method": method,
                },
            ) from exc
        except (httpx.HTTPError, OSError) as exc:
            self._check_deadline(
                deadline,
                cancelled,
                dispatched=dispatched,
                phase="await_response" if dispatched else "pre_dispatch",
                timeout_seconds=timeout,
                method=method,
            )
            raise McpTransportError(
                "agent_mcp_process_disconnected",
                dispatched=dispatched,
                phase="await_response" if dispatched else "pre_dispatch",
                request_id=ident,
                transport="streamable_http",
                server_id=self.server_id,
            ) from exc

        if not isinstance(message, dict) or message.get("jsonrpc") != "2.0" or message.get("id") != ident:
            raise McpTransportError(
                "agent_mcp_protocol_invalid",
                dispatched=True,
                phase="response_parse",
                request_id=ident,
                transport="streamable_http",
                server_id=self.server_id,
            )
        if "error" in message:
            err_obj = message.get("error")
            code = err_obj.get("code") if isinstance(err_obj, dict) else None
            rpc_code = code if type(code) is int else None
            raw_msg = err_obj.get("message") if isinstance(err_obj, dict) else None
            cleaned_msg = _clean_mcp_error_message(
                raw_msg,
                redact_values=redact_vals,
                limit=500,
            )
            rpc_details: dict[str, Any] = {}
            if cleaned_msg:
                rpc_details["message"] = cleaned_msg
            if rpc_code is not None:
                rpc_details["rpc_code"] = rpc_code
            if code in (-32602, -32601):
                err_code = (
                    raw_msg
                    if isinstance(raw_msg, str) and raw_msg in ALLOWLISTED_MCP_RPC_ERROR_CODES
                    else "agent_mcp_rpc_rejected"
                )
                raise McpTransportError(
                    err_code,
                    dispatched=True,
                    phase="rpc_error",
                    request_id=ident,
                    rpc_code=rpc_code,
                    transport="streamable_http",
                    server_id=self.server_id,
                    details=rpc_details or None,
                )
            raise McpTransportError(
                "agent_mcp_rpc_error",
                dispatched=True,
                phase="rpc_error",
                request_id=ident,
                rpc_code=rpc_code,
                transport="streamable_http",
                server_id=self.server_id,
                details=rpc_details or None,
            )
        result = message.get("result")
        if not isinstance(result, dict):
            raise McpTransportError(
                "agent_mcp_protocol_invalid",
                dispatched=True,
                phase="response_parse",
                request_id=ident,
                transport="streamable_http",
                server_id=self.server_id,
            )
        return result

    def initialize(self, *, cancelled: Callable[[], bool] | None = None) -> dict[str, Any]:
        with self.lock:
            deadline = time.monotonic() + self.startup_timeout
            if self.initialized:
                self._check_deadline(
                    deadline,
                    cancelled,
                    dispatched=False,
                    phase="initialize",
                    timeout_seconds=self.startup_timeout,
                    method="initialize",
                )
                return self.server_info
            result = self._exchange(
                "initialize",
                {
                    "protocolVersion": STREAMABLE_HTTP_PROTOCOL_VERSION,
                    "capabilities": {},
                    "clientInfo": {"name": "Ryn", "version": "1.0"},
                },
                self.startup_timeout,
                cancelled=cancelled,
                _deadline=deadline,
            )
            if (
                result.get("protocolVersion") not in SUPPORTED_PROTOCOL_VERSIONS
                or not isinstance(result.get("capabilities"), dict)
            ):
                self.close()
                raise McpTransportError("agent_mcp_protocol_unsupported", dispatched=False)
            self.protocol_version = result["protocolVersion"]
            notif = json.dumps(
                {"jsonrpc": "2.0", "method": "notifications/initialized"},
                separators=(",", ":"),
            ).encode("utf-8")
            try:
                refresh_state = [False]
                for attempt in range(2):
                    headers = self._headers(refresh_on_error=True, refresh_state=refresh_state)
                    req_timeout = self._remaining_timeout(
                        deadline,
                        cancelled,
                        dispatched=False,
                        phase="initialize",
                        timeout_seconds=self.startup_timeout,
                        method="notifications/initialized",
                    )
                    retry = False
                    notif_box: list[httpx.Response | None] = [None]
                    with self._watch_request(deadline, cancelled, notif_box):
                        with self._client.stream(
                            "POST",
                            self.url,
                            content=notif,
                            headers=headers,
                            timeout=_http_timeout(req_timeout),
                        ) as resp:
                            notif_box[0] = resp
                            self._check_deadline(
                                deadline,
                                cancelled,
                                dispatched=False,
                                phase="initialize",
                                timeout_seconds=self.startup_timeout,
                                method="notifications/initialized",
                            )
                            if resp.status_code in (401, 403):
                                auth_error = self._auth_error(resp, dispatched=True)
                                retry = bool(resp.status_code == 401 and self.oauth and headers.get("Authorization") and attempt == 0 and not refresh_state[0])
                                if not retry:
                                    raise auth_error
                            elif resp.status_code >= 300:
                                raise McpTransportError("agent_mcp_http_error", dispatched=False)
                            if not retry:
                                self._check_content_length(resp, dispatched=False)
                                notif_total = 0
                                for chunk in resp.iter_bytes():
                                    self._check_deadline(
                                        deadline,
                                        cancelled,
                                        dispatched=False,
                                        phase="initialize",
                                        timeout_seconds=self.startup_timeout,
                                        method="notifications/initialized",
                                    )
                                    notif_total += len(chunk)
                                    if notif_total > MAX_OUTPUT_BYTES:
                                        raise McpTransportError("agent_mcp_output_limit", dispatched=False)
                    if not retry:
                        break
                    try:
                        self.oauth.access_token(self._server, force_refresh=True, refresh_state=refresh_state)
                    except McpOAuthError as exc:
                        raise McpTransportError(str(exc), dispatched=True, phase="http_status", http_status=401) from None
            except McpTransportError:
                self.close()
                raise
            except httpx.TimeoutException as exc:
                self.close()
                raise McpTransportError(
                    "agent_mcp_timeout",
                    dispatched=False,
                    phase="initialize",
                    details={
                        "phase": "initialize",
                        "timeout_seconds": round(float(self.startup_timeout), 3),
                        "method": "notifications/initialized",
                    },
                ) from exc
            except (httpx.HTTPError, OSError) as exc:
                self.close()
                self._check_deadline(
                    deadline,
                    cancelled,
                    dispatched=False,
                    phase="initialize",
                    timeout_seconds=self.startup_timeout,
                    method="notifications/initialized",
                )
                raise McpTransportError("agent_mcp_process_disconnected", dispatched=False) from exc
            self.initialized = True
            self.server_info = result
            return result

    def list_all_tools(self, *, cancelled: Callable[[], bool] | None = None) -> list[dict[str, Any]]:
        if not self.initialized:
            self.initialize(cancelled=cancelled)
        with self.lock:
            tools: list[dict[str, Any]] = []
            cursor: str | None = None
            seen_cursors: set[str] = set()
            total_budget = 10.0 * self.startup_timeout
            total_deadline = time.monotonic() + total_budget
            for _ in range(MAX_TOOL_PAGES):
                remaining_total = total_deadline - time.monotonic()
                if remaining_total <= 0:
                    raise McpTransportError(
                        "agent_mcp_timeout",
                        dispatched=False,
                        phase="await_response",
                        details={
                            "phase": "await_response",
                            "timeout_seconds": round(total_budget, 3),
                            "method": "tools/list",
                        },
                    )
                page_timeout = min(self.startup_timeout, remaining_total)
                params = {"cursor": cursor} if cursor else {}
                res = self._exchange(
                    "tools/list", params, page_timeout, cancelled=cancelled
                )
                page_tools = res.get("tools")
                if not isinstance(page_tools, list):
                    raise McpTransportError("agent_mcp_protocol_invalid", dispatched=False)
                for item in page_tools:
                    if isinstance(item, dict) and isinstance(item.get("name"), str) and item["name"].strip():
                        tools.append(item)
                        if len(tools) > MAX_DISCOVERED_TOOLS:
                            raise McpTransportError("agent_mcp_tools_limit", dispatched=False)
                next_cursor = res.get("nextCursor")
                if not next_cursor or not isinstance(next_cursor, str):
                    break
                if next_cursor in seen_cursors:
                    break
                seen_cursors.add(next_cursor)
                cursor = next_cursor
            return tools

    def call_tool(
        self,
        name: str,
        arguments: dict[str, Any],
        *,
        timeout: float | None = None,
        before_dispatch: Callable[[], None] | None = None,
        cancelled: Callable[[], bool] | None = None,
        trusted_operation: dict[str, Any] | None = None,
        prepare: bool = False,
    ) -> dict[str, Any]:
        if not self.initialized:
            try:
                self.initialize(cancelled=cancelled)
            except BaseException as exc:
                exc.dispatched = False  # type: ignore[attr-defined]
                raise
        eff_timeout = (
            min(self.tool_timeout, max(0.05, float(timeout)))
            if timeout is not None
            else self.tool_timeout
        )
        with self.lock:
            return self._exchange(
                "tools/prepare" if prepare else "tools/call",
                {"name": name, "arguments": arguments,
                 **({"_meta": {"ryn_operation": trusted_operation}} if trusted_operation is not None else {})},
                eff_timeout,
                before_dispatch=before_dispatch,
                cancelled=cancelled,
            )

    def close(self) -> None:
        if self.closed.is_set():
            return
        self.closed.set()
        try:
            if self.session_id:
                close_deadline = time.monotonic() + 2.0
                with self._client.stream(
                    "DELETE",
                    self.url,
                    headers=self._headers(),
                    timeout=2.0,
                ) as resp, self._watch_response(resp, close_deadline, None):
                    del_total = 0
                    for chunk in resp.iter_bytes():
                        del_total += len(chunk)
                        if del_total > 65536 or time.monotonic() >= close_deadline:
                            break
        except Exception:
            pass
        finally:
            try:
                self._client.close()
            except Exception:
                pass


class McpSessionManager:
    """Manages active MCP client sessions, discovery probes, restarts, and cleanup."""

    def __init__(self, store: ExtensionStore) -> None:
        self.store = store
        self.oauth = McpOAuthManager(store, on_connected=self._oauth_connected, trusted_origin=self._managed_oauth_origin)
        self._lock = threading.RLock()
        self._sessions: dict[tuple[str, str | None], StdioMcpSession | StreamableHttpMcpSession] = {}
        self._pending: dict[
            tuple[str, str | None], list[StdioMcpSession | StreamableHttpMcpSession]
        ] = {}
        self._manager_generation = 0
        self._server_generations: dict[str, int] = {}
        self._scope_generations: dict[str, int] = {}
        self._fallback_cwd = self.store.root / "mcp_runtime"
        self._fallback_cwd.mkdir(parents=True, exist_ok=True)
        self._catalog_checked: dict[str, float] = {}
        self._catalog_refreshing: set[str] = set()

    def refresh_tool_catalogs(self, *, force: bool = False) -> list[str]:
        """Start background tools/list refreshes for ready remote plugin servers.

        Never blocks the caller. A failed refresh keeps the current list and
        status; only an actual change is written (which bumps the revision).
        """
        started: list[str] = []
        now = time.monotonic()
        try:
            servers = self.store.list_enabled_mcp_servers_internal()
        except Exception:
            return started
        for server in servers:
            sid = str(server.get("id") or "")
            if (not sid or server.get("transport") != "streamable_http"
                    or server.get("status") not in {"ready", "Ready", "connected"}):
                continue
            try:
                if self.oauth.public_status(str(server.get("instance_id") or "")).get("state") in {"logged_out", "pending", "error"}:
                    continue
            except Exception:
                continue
            with self._lock:
                if sid in self._catalog_refreshing:
                    continue
                # First sight in this process: a list read minutes ago (sign-in, test) is still fresh.
                if not force and now - self._catalog_checked.get(sid, now - _seconds_since_listed(server)) < CATALOG_REFRESH_SECONDS:
                    continue
                self._catalog_refreshing.add(sid)
                self._catalog_checked[sid] = now
            threading.Thread(target=self._refresh_catalog, args=(sid,), daemon=True,
                             name=f"ryn-mcp-catalog-{sid[:24]}").start()
            started.append(sid)
        return started

    def _refresh_catalog(self, server_id: str) -> bool:
        session = None
        try:
            current = self.store.get_mcp_server_internal(server_id, require_enabled=True)
            session = self._create_session(current)
            tools = session.list_all_tools()
            if (_catalog_signature(tools, stored=False)
                    == _catalog_signature(current.get("discovered_tools") or [], stored=True)):
                return False
            self.store.record_mcp_discovery(
                server_id,
                expected_revision=int(current["revision"]),
                expected_config_generation=current.get("config_generation"),
                expected_instance_id=current.get("instance_id"),
                expected_enabled=True,
                discovered_tools=tools,
                status="ready",
                status_message="",
            )
            return True
        except Exception:
            return False  # Keep the last good catalog; a transient error is not "no tools".
        finally:
            if session is not None:
                try:
                    session.close()
                except Exception:
                    pass
            with self._lock:
                self._catalog_refreshing.discard(server_id)

    def _managed_oauth_origin(self, resource: str, target: str) -> bool:
        from urllib.parse import urlsplit
        local, remote = urlsplit(resource), urlsplit(target)
        if local.scheme != "http" or local.hostname != "127.0.0.1" or remote.scheme != "https":
            return False
        for server in self.store.list_enabled_mcp_servers_internal():
            if server.get("url") != resource or not (server.get("secret_headers") or {}).get("X-Ryn-Service-Token"):
                continue
            issuer = urlsplit(server.get("identity_group_origin") or "")
            if (issuer.scheme == "https" and not issuer.username and not issuer.password
                    and issuer.path in {"", "/"} and not issuer.query and not issuer.fragment
                    and (remote.scheme, remote.netloc) == (issuer.scheme, issuer.netloc)):
                return True
        return False

    def rediscover_authenticated(self, server_id: str) -> None:
        """Restore a cleared tool catalog when an authenticated plugin is enabled."""
        current = self.store.get_mcp_server_internal(server_id, require_enabled=False)
        if (current.get("effective_enabled") is True
                and current.get("transport") == "streamable_http"
                and current.get("status") == "NeedsTest"
                and not current.get("discovered_tools")):
            self._oauth_connected(server_id)

    def _oauth_connected(self, server_id: str) -> None:
        from ..diagnostic_log import oauth_event
        try:
            current = self.store.get_mcp_server_internal(server_id, require_enabled=False)
            state = self.oauth.public_status(current["instance_id"])["state"]
            if state != "logged_in":
                oauth_event("discovery_skipped", server=server_id, reason=state if isinstance(state, str) else "unknown")
                return
            self.test_server(server_id, expected_revision=current["revision"], restart=True)
            after = self.store.get_mcp_server_internal(server_id, require_enabled=False)
            oauth_event("discovery_finished", server=server_id, ok=True, tools=len(after.get("discovered_tools") or []))
        except Exception as exc:
            code = str(exc) if isinstance(exc, AgentError) else ""
            oauth_event("discovery_finished", server=server_id, ok=False, code=code, reason=type(exc).__name__.lower()[:40])

    @staticmethod
    def _session_key(server_id: str, session_scope: str | None = None) -> tuple[str, str | None]:
        scope = str(session_scope).strip() if session_scope else None
        return (str(server_id), scope or None)

    def _is_generation_stale(
        self,
        server_id: str,
        scope: str | None,
        mgr_gen: int,
        srv_gen: int,
        scope_gen: int,
    ) -> bool:
        with self._lock:
            if self._manager_generation != mgr_gen:
                return True
            if self._server_generations.get(server_id, 0) != srv_gen:
                return True
            if scope is not None and self._scope_generations.get(scope, 0) != scope_gen:
                return True
            return False

    def _remove_pending(
        self,
        key: tuple[str, str | None],
        session: StdioMcpSession | StreamableHttpMcpSession,
    ) -> bool:
        with self._lock:
            bucket = self._pending.get(key)
            if not bucket or session not in bucket:
                return False
            bucket.remove(session)
            if not bucket:
                self._pending.pop(key, None)
            return True

    def _create_session(
        self,
        server: dict[str, Any],
        *,
        cancelled: Callable[[], bool] = lambda: False,
    ) -> StdioMcpSession | StreamableHttpMcpSession:
        transport = str(server.get("transport", "stdio")).lower()
        session: StdioMcpSession | StreamableHttpMcpSession
        if transport == "streamable_http":
            session = StreamableHttpMcpSession(server, cancelled=cancelled, oauth=self.oauth)
        else:
            session = StdioMcpSession(
                server,
                fallback_cwd=self._fallback_cwd,
                store_root=self.store.root,
                cancelled=cancelled,
            )
        session.on_tools_changed = lambda sid=str(server["id"]): self.catalog_changed(sid)  # type: ignore[attr-defined]
        return session

    def invalidate_server(self, server_id: str) -> None:
        sid = str(server_id)
        with self._lock:
            self._server_generations[sid] = self._server_generations.get(sid, 0) + 1
            keys = [k for k in self._sessions if k[0] == sid]
            sessions = [self._sessions.pop(k) for k in keys]
            pending_keys = [k for k in self._pending if k[0] == sid]
            for k in pending_keys:
                sessions.extend(self._pending.pop(k, ()))
        for session in sessions:
            session.close()

    def forget_task(self, task_id: str) -> None:
        scope = str(task_id or "").strip()
        if not scope:
            return
        with self._lock:
            self._scope_generations[scope] = self._scope_generations.get(scope, 0) + 1
            keys = [k for k in self._sessions if k[1] == scope]
            sessions = [self._sessions.pop(k) for k in keys]
            pending_keys = [k for k in self._pending if k[1] == scope]
            for k in pending_keys:
                sessions.extend(self._pending.pop(k, ()))
        for session in sessions:
            session.close()

    def catalog_changed(self, server_id: str) -> None:
        """The server announced tools/list_changed: re-read its list now, once."""
        sid = str(server_id)
        with self._lock:
            if sid in self._catalog_refreshing:
                return
            self._catalog_refreshing.add(sid)
            self._catalog_checked[sid] = time.monotonic()
        threading.Thread(target=self._refresh_catalog, args=(sid,), daemon=True,
                         name=f"ryn-mcp-catalog-{sid[:24]}").start()

    def wait_for_catalogs(self, server_ids: Any, timeout: float) -> bool:
        """Wait up to timeout for these tool-list refreshes (None: all now running); True when all finished."""
        with self._lock:
            wanted = set(self._catalog_refreshing) if server_ids is None else {str(sid) for sid in server_ids}
        deadline = time.monotonic() + max(0.0, float(timeout))
        while True:
            with self._lock:
                if not wanted & self._catalog_refreshing:
                    return True
            if time.monotonic() >= deadline:
                return False
            time.sleep(0.05)

    def close(self) -> None:
        self.oauth.close()
        with self._lock:
            self._manager_generation += 1
            sessions = list(self._sessions.values())
            self._sessions.clear()
            for bucket in self._pending.values():
                sessions.extend(bucket)
            self._pending.clear()
        for session in sessions:
            session.close()

    def restart_all(self, *, desktop_access: bool = False) -> dict[str, Any]:
        """Close all active MCP client sessions so servers reconnect with fresh state."""
        self.close()
        return {
            "restarted": True,
            "overview": self.store.overview(desktop_access=desktop_access),
        }

    def clear_app_data(self, *, desktop_access: bool = False) -> dict[str, Any]:
        """Close active MCP sessions and clear cached MCP app data."""
        self.close()
        return self.store.clear_mcp_app_data(desktop_access=desktop_access)

    def test_server(
        self,
        server_id: str,
        *,
        expected_revision: int,
        restart: bool = False,
        desktop_access: bool = False,
    ) -> dict[str, Any]:
        """Probe an MCP server (connect, initialize, paginated tools/list) and persist result.

        Explicit testing while disabled is allowed without enabling the server or exposing
        its tools to the Agent. Binds the probe to initial identity, config_generation,
        revision, and effective_enabled state so concurrent disable/delete/recreate invalidates it.
        """
        if type(expected_revision) is not int or expected_revision < 1:
            raise AgentError("agent_invalid_revision")

        initial = self.store.get_mcp_server_internal(
            server_id,
            require_enabled=False,
            expected_revision=expected_revision,
        )
        if restart:
            self.invalidate_server(server_id)

        had_tools = bool(initial.get("discovered_tools"))
        session: StdioMcpSession | StreamableHttpMcpSession | None = None
        try:
            session = self._create_session(initial)
            tools = session.list_all_tools()
        except AgentError as exc:
            if session is not None:
                session.close()
            err_code = str(exc)
            if err_code == "agent_mcp_auth_required":
                status = "NeedsAuth"
            elif had_tools:
                status = "Degraded"
            else:
                status = "Error"
            updated_view = self.store.record_mcp_discovery(
                server_id,
                expected_revision=initial["revision"],
                expected_config_generation=initial["config_generation"],
                expected_instance_id=initial["instance_id"],
                expected_enabled=initial["effective_enabled"],
                allow_disabled=True,
                discovered_tools=None,
                status=status,
                status_message=err_code,
                desktop_access=desktop_access,
            )
            return {
                "server": updated_view,
                "test_result": {
                    "status": "error",
                    "tools_count": updated_view["tools_count"],
                    "error": err_code,
                },
            }
        except Exception:
            if session is not None:
                session.close()
            updated_view = self.store.record_mcp_discovery(
                server_id,
                expected_revision=initial["revision"],
                expected_config_generation=initial["config_generation"],
                expected_instance_id=initial["instance_id"],
                expected_enabled=initial["effective_enabled"],
                allow_disabled=True,
                discovered_tools=None,
                status="Degraded" if had_tools else "Error",
                status_message="agent_mcp_transport_error",
                desktop_access=desktop_access,
            )
            return {
                "server": updated_view,
                "test_result": {
                    "status": "error",
                    "tools_count": updated_view["tools_count"],
                    "error": "agent_mcp_transport_error",
                },
            }

        try:
            updated_view = self.store.record_mcp_discovery(
                server_id,
                expected_revision=initial["revision"],
                expected_config_generation=initial["config_generation"],
                expected_instance_id=initial["instance_id"],
                expected_enabled=initial["effective_enabled"],
                allow_disabled=True,
                discovered_tools=tools,
                status="ready",
                status_message="",
                desktop_access=desktop_access,
            )
        except BaseException:
            session.close()
            raise

        if initial["effective_enabled"] and server_id != COMPUTER_USE_SERVER_ID:
            session.revision = int(updated_view["revision"])
            session.cancelled = self._make_session_lifetime_cancelled(
                server_id,
                expected_revision=session.revision,
                expected_config_generation=session.config_generation,
                expected_instance_id=session.instance_id,
                session_ref=[session],
            )
            key = self._session_key(server_id, None)
            with self._lock:
                old = self._sessions.pop(key, None)
                self._sessions[key] = session
            if old is not None:
                old.close()
        else:
            session.close()

        return {
            "server": updated_view,
            "test_result": {
                "status": "connected",
                "tools_count": len(tools),
            },
        }

    def _make_session_lifetime_cancelled(
        self,
        server_id: str,
        *,
        expected_revision: int,
        expected_config_generation: int,
        expected_instance_id: str,
        session_ref: list[StdioMcpSession | StreamableHttpMcpSession] | None = None,
    ) -> Callable[[], bool]:
        def _is_session_revoked() -> bool:
            target = session_ref[0] if session_ref else None
            if target is not None and target.closed.is_set():
                return True
            rev = target.revision if target is not None else expected_revision
            gen = target.config_generation if target is not None else expected_config_generation
            inst = target.instance_id if target is not None else expected_instance_id
            try:
                self.store.get_mcp_server_internal(
                    server_id,
                    require_enabled=True,
                    expected_revision=rev,
                    expected_config_generation=gen,
                    expected_instance_id=inst,
                )
                return False
            except BaseException:
                return True

        return _is_session_revoked

    def _revalidate_tool_call(
        self,
        server_id: str,
        tool_name: str,
        *,
        expected_revision: int,
        expected_config_generation: int,
        expected_instance_id: str,
        expected_schema_hash: str | None = None,
        session: StdioMcpSession | StreamableHttpMcpSession | None = None,
    ) -> dict[str, Any]:
        server = self.store.get_mcp_server_internal(
            server_id,
            require_enabled=True,
            expected_revision=expected_revision,
            expected_config_generation=expected_config_generation,
            expected_instance_id=expected_instance_id,
        )
        if session is not None and (
            not session.is_usable()
            or session.instance_id != server["instance_id"]
            or session.config_generation != int(server["config_generation"])
            or session.revision != int(server["revision"])
        ):
            raise AgentError("agent_stale_revision")

        disabled_tools = server.get("disabled_tools") or []
        if tool_name in disabled_tools:
            raise AgentError("agent_mcp_tool_disabled")
        enabled_tools = server.get("enabled_tools")
        if enabled_tools is not None and tool_name not in enabled_tools:
            raise AgentError("agent_mcp_tool_disabled")

        discovered = server.get("discovered_tools") or []
        matched_tool = next(
            (t for t in discovered if isinstance(t, dict) and t.get("name") == tool_name),
            None,
        )
        if expected_schema_hash is not None:
            actual_hash = (
                matched_tool.get("schema_sha256") or matched_tool.get("schema_hash")
                if matched_tool is not None
                else None
            )
            if matched_tool is None or actual_hash != expected_schema_hash:
                raise AgentError("agent_stale_revision")
            if not matched_tool.get("enabled", True):
                raise AgentError("agent_mcp_tool_disabled")
        elif matched_tool is not None and not matched_tool.get("enabled", True):
            raise AgentError("agent_mcp_tool_disabled")
        return server

    def _get_or_connect_enabled_session(
        self,
        server: dict[str, Any],
        *,
        session_scope: str | None = None,
        cancelled: Callable[[], bool] = lambda: False,
    ) -> StdioMcpSession | StreamableHttpMcpSession:
        try:
            if cancelled():
                raise McpTransportError("agent_cancelled", dispatched=False)
        except McpTransportError:
            raise
        except BaseException as exc:
            raise McpTransportError("agent_cancelled", dispatched=False) from exc

        sid = str(server["id"])
        key = self._session_key(sid, session_scope)
        scope = key[1]
        expected_rev = int(server.get("revision", 1))
        expected_gen = int(server.get("config_generation", 1))
        expected_inst = str(server.get("instance_id", ""))
        stale_existing: StdioMcpSession | StreamableHttpMcpSession | None = None
        with self._lock:
            start_mgr_gen = self._manager_generation
            start_srv_gen = self._server_generations.get(sid, 0)
            start_scope_gen = self._scope_generations.get(scope, 0) if scope is not None else 0
            existing = self._sessions.get(key)
            if (
                existing is not None
                and existing.is_usable()
                and existing.instance_id == expected_inst
                and existing.config_generation == expected_gen
                and existing.revision == expected_rev
            ):
                return existing
            if existing is not None:
                self._sessions.pop(key, None)
                stale_existing = existing
        if stale_existing is not None:
            stale_existing.close()

        session_ref: list[StdioMcpSession | StreamableHttpMcpSession] = []
        lifetime_cancelled = self._make_session_lifetime_cancelled(
            sid,
            expected_revision=expected_rev,
            expected_config_generation=expected_gen,
            expected_instance_id=expected_inst,
            session_ref=session_ref,
        )

        def _is_revoked_or_cancelled() -> bool:
            return bool(
                cancelled()
                or lifetime_cancelled()
                or self._is_generation_stale(
                    sid, scope, start_mgr_gen, start_srv_gen, start_scope_gen
                )
            )

        try:
            session = self._create_session(server, cancelled=lifetime_cancelled)
            session_ref.append(session)
        except BaseException as exc:
            exc.dispatched = False  # type: ignore[attr-defined]
            raise

        with self._lock:
            if self._is_generation_stale(
                sid, scope, start_mgr_gen, start_srv_gen, start_scope_gen
            ):
                stale_on_create = True
            else:
                stale_on_create = False
                self._pending.setdefault(key, []).append(session)
        if stale_on_create:
            session.close()
            raise McpTransportError("agent_cancelled", dispatched=False)

        try:
            session.initialize(cancelled=_is_revoked_or_cancelled)
        except BaseException as exc:
            self._remove_pending(key, session)
            session.close()
            if not cancelled() and not self._is_generation_stale(
                sid, scope, start_mgr_gen, start_srv_gen, start_scope_gen
            ):
                try:
                    self.store.get_mcp_server_internal(
                        sid,
                        require_enabled=True,
                        expected_revision=expected_rev,
                        expected_config_generation=expected_gen,
                        expected_instance_id=expected_inst,
                    )
                except BaseException as store_exc:
                    store_exc.dispatched = False  # type: ignore[attr-defined]
                    raise store_exc from exc
            exc.dispatched = False  # type: ignore[attr-defined]
            raise

        winner: StdioMcpSession | StreamableHttpMcpSession | None = None
        discard: StdioMcpSession | StreamableHttpMcpSession | None = None
        stale_current: StdioMcpSession | StreamableHttpMcpSession | None = None
        with self._lock:
            was_pending = self._remove_pending(key, session)
            gen_stale = self._is_generation_stale(
                sid, scope, start_mgr_gen, start_srv_gen, start_scope_gen
            )
            current = self._sessions.get(key)
            if gen_stale or not was_pending:
                winner = None
                discard = session
            elif (
                current is not None
                and current.is_usable()
                and current.instance_id == expected_inst
                and current.config_generation == expected_gen
                and current.revision == expected_rev
            ):
                winner = current
                discard = session
            elif not session.is_usable() or _is_revoked_or_cancelled():
                winner = None
                discard = session
                if current is not None and not current.is_usable():
                    self._sessions.pop(key, None)
                    stale_current = current
            else:
                self._sessions[key] = session
                winner = session
                discard = current

        if discard is not None and discard is not winner:
            discard.close()
        if stale_current is not None and stale_current is not discard and stale_current is not winner:
            stale_current.close()

        if winner is None:
            if not cancelled() and not self._is_generation_stale(
                sid, scope, start_mgr_gen, start_srv_gen, start_scope_gen
            ):
                try:
                    self.store.get_mcp_server_internal(
                        sid,
                        require_enabled=True,
                        expected_revision=expected_rev,
                        expected_config_generation=expected_gen,
                        expected_instance_id=expected_inst,
                    )
                except BaseException as store_exc:
                    store_exc.dispatched = False  # type: ignore[attr-defined]
                    raise store_exc from None
            raise McpTransportError("agent_cancelled", dispatched=False)

        if cancelled():
            raise McpTransportError("agent_cancelled", dispatched=False)
        return winner

    def call_tool(
        self,
        server_id: str,
        tool_name: str,
        arguments: dict[str, Any],
        *,
        expected_revision: int,
        expected_config_generation: int,
        expected_instance_id: str,
        expected_schema_hash: str | None = None,
        session_scope: str | None = None,
        timeout_ms: int | None = None,
        before_dispatch: Callable[[], None] | None = None,
        cancelled: Callable[[], bool] = lambda: False,
        trusted_operation: dict[str, Any] | None = None,
        prepare: bool = False,
    ) -> dict[str, Any]:
        """Execute a tool call against an enabled MCP server, enforcing identity & generation."""
        key = self._session_key(server_id, session_scope)
        try:
            server = self._revalidate_tool_call(
                server_id,
                tool_name,
                expected_revision=expected_revision,
                expected_config_generation=expected_config_generation,
                expected_instance_id=expected_instance_id,
                expected_schema_hash=expected_schema_hash,
            )
        except BaseException as exc:
            if not hasattr(exc, "dispatched"):
                exc.dispatched = False  # type: ignore[attr-defined]
            self.invalidate_server(server_id)
            raise

        session = self._get_or_connect_enabled_session(
            server, session_scope=session_scope, cancelled=cancelled
        )

        scope = key[1]
        with self._lock:
            call_mgr_gen = self._manager_generation
            call_srv_gen = self._server_generations.get(server_id, 0)
            call_scope_gen = self._scope_generations.get(scope, 0) if scope is not None else 0

        def _locked_pre_dispatch() -> None:
            self._revalidate_tool_call(
                server_id,
                tool_name,
                expected_revision=expected_revision,
                expected_config_generation=expected_config_generation,
                expected_instance_id=expected_instance_id,
                expected_schema_hash=expected_schema_hash,
                session=session,
            )
            if before_dispatch is not None:
                before_dispatch()

        def _call_cancelled() -> bool:
            if cancelled():
                return True
            if self._is_generation_stale(
                server_id, scope, call_mgr_gen, call_srv_gen, call_scope_gen
            ):
                return True
            try:
                srv = self._revalidate_tool_call(
                    server_id,
                    tool_name,
                    expected_revision=expected_revision,
                    expected_config_generation=expected_config_generation,
                    expected_instance_id=expected_instance_id,
                    expected_schema_hash=expected_schema_hash,
                    session=None,
                )
                return bool(
                    session.instance_id != srv["instance_id"]
                    or session.config_generation != int(srv["config_generation"])
                    or session.revision != int(srv["revision"])
                )
            except BaseException:
                return True

        try:
            extra: dict[str, Any] = {}
            if timeout_ms is not None:
                extra["timeout"] = min(
                    session.tool_timeout,
                    max(0.05, float(timeout_ms) / 1000.0),
                )
            if trusted_operation is not None or prepare:
                from urllib.parse import urlsplit
                url = urlsplit(server.get("url") or "")
                local = url.scheme == "http" and url.hostname == "127.0.0.1" and (server.get("secret_headers") or {}).get("X-Ryn-Service-Token")
                remote = (url.scheme == "https" and (server.get("auth") or {}).get("mode") == "oauth"
                          and any(tool.get("name") == tool_name and (tool.get("input_schema") or {}).get("x-ryn-operation") == "1"
                                  for tool in server.get("discovered_tools") or []))
                if not isinstance(session, StreamableHttpMcpSession) or not (local or remote):
                    raise AgentError("agent_local_service_binding_invalid")
                extra["trusted_operation"] = trusted_operation
                extra["prepare"] = prepare
            return session.call_tool(
                tool_name,
                arguments,
                before_dispatch=_locked_pre_dispatch,
                cancelled=_call_cancelled,
                **extra,
            )
        except BaseException as exc:
            if getattr(exc, "server_id", None) is None:
                exc.server_id = server_id  # type: ignore[attr-defined]
            if getattr(exc, "session_scope", None) is None and session_scope:
                exc.session_scope = session_scope  # type: ignore[attr-defined]
            if getattr(exc, "transport", None) is None:
                exc.transport = (  # type: ignore[attr-defined]
                    "streamable_http"
                    if isinstance(session, StreamableHttpMcpSession)
                    else "stdio"
                )
            proc = getattr(session, "process", None)
            pid = getattr(proc, "pid", None)
            if getattr(exc, "session_pid", None) is None and type(pid) is int:
                exc.session_pid = pid  # type: ignore[attr-defined]
            if (
                session.closed.is_set()
                or not session.is_usable()
                or getattr(exc, "dispatched", False)
            ):
                with self._lock:
                    if self._sessions.get(key) is session:
                        self._sessions.pop(key, None)
                session.close()
            raise
