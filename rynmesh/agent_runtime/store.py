"""Encrypted transactional Agent state using the node's existing messaging key."""
from __future__ import annotations

import copy
import hashlib
import json
import os
import pickle
import re
import shutil
import sys
import time
from pathlib import Path
from typing import Any, Callable

from cryptography.exceptions import InvalidTag

from ..atomic_io import AtomicIOError, atomic_write_bytes, atomic_write_json, fs_path, read_json
from ..file_transactions import file_transaction
from ..services import peer_box
from .diagnostic_log import performance
from .state import TaskMigrationError, migrate, migrate_task

VERSION = "ryn.agent.v1"
CHANNEL = b"rynmesh-agent-state-v1"
# Layout v2: sessions.json is a small encrypted root that commits, by sha256, one
# encrypted file per task. A transaction rewrites only the tasks it changed, then
# the root. Earlier builds refuse the v2 root instead of reading it as empty.
ROOT_VERSION = "ryn.agent.v2"
TASK_VERSION = "ryn.agent.task.v1"
TASK_CHANNEL = b"rynmesh-agent-task-v1"
# Short names: archive paths on Windows must stay well under MAX_PATH.
TASKS_DIR = "tasks"
QUARANTINE_DIR = "quarantine"
PREV_ROOT_NAME = "sessions.json.prev"
_BLOB_NAME = re.compile(r"[a-f0-9]{24}\.json")
# Local history has no product quota. Atomic I/O still takes an integer guard;
# the actual admission limit is free space on the store's volume.
MAX_BYTES = sys.maxsize
DEFAULT_SETTINGS = {
    "enabled": False, "model_id": "", "permission_mode": "workspace", "allowed_write_tools": [], "denied_tools": [],
    "workspace_root": "", "desktop_access": False, "verify_text_responses": False,
    "web_search_mode": "", "web_allowed_domains": [],
    "interrupt_message_enabled": True,
    "owner_instructions": "",
    "default_subagent_model_id": "", "default_subagent_reasoning_effort": None,
    "project_doc_fallback_filenames": ["CLAUDE.md"],
    # None follows the selected model's declared token window, reserving output
    # and framing. An integer remains an owner-selected UTF-8 request-byte cap.
    # The legacy field name is retained for saved sessions and API compatibility.
    "max_steps": 64, "max_seconds": 900, "max_context_chars": None,
    "max_output_chars": 20000,
    # Exact catalog model id -> model_profiles profile id (explicit override).
    "model_profile_overrides": {},
}
PERMISSION_MODES = frozenset({"workspace", "read_only", "confirm_changes", "automatic"})
# The Ryn app ships with full access ("自动处理"): every change runs without a
# dialog except the hard gates in execution_policy.decide.
SHIPPED_PERMISSION_MODE = "automatic"
# Root key recording where settings.permission_mode came from: "shipped_default"
# (this build chose it), "legacy" (an older build saved a non-default value) or
# "owner" (set through update_settings). Only an unrecorded source is migrated.
PERMISSION_MODE_SOURCE = "settings_permission_mode_source"
# An additional conservative byte ceiling for an undeclared model window.
UNKNOWN_WINDOW_CONTEXT_CHARS = 120000


class AgentError(ValueError):
    """Stable public error code, never an external exception's raw message."""

    def __init__(self, code: str = "", *, correction: str | None = None, details: dict | None = None):
        super().__init__(code)
        self.correction = correction if isinstance(correction, str) and correction else REJECTION_CORRECTIONS.get(code)
        self.details = dict(details) if isinstance(details, dict) else None


def cancellation_reason(cancelled) -> str:
    """Distinguish budget exhaustion from cancellation; never export callback text."""
    reason = getattr(cancelled, "reason", None)
    if isinstance(reason, str) and reason in {
        "agent_time_limit", "agent_time_budget_exhausted",
    }:
        return reason
    return "agent_cancelled"


class AgentModelResponseError(AgentError):
    """Rejected response with safe accounting metadata, never provider text."""

    def __init__(self, usage, finish_reason, code="agent_invalid_model_response"):
        super().__init__(code)
        usage = usage if isinstance(usage, dict) else {}
        self.usage = {name: usage.get(name) if type(usage.get(name)) is int and usage[name] >= 0 else None
                      for name in ("input_tokens", "output_tokens")}
        self.finish_reason = finish_reason if isinstance(finish_reason, str) and finish_reason in {"stop", "length", "tool_calls", "content_filter", "cancelled"} else "unknown"
        # Server-authored description of what was structurally wrong (never provider text). It is
        # shown to the model on the retry so the same invalid call is not simply regenerated.
        self.detail = ""


# Next step for host refusals that otherwise reach the model as a bare code.
# Without one, the second identical refusal gets the generic repeated-error
# hint, which suggests another tool: wrong when a setting is what blocks the
# call. The text rides on the refused result only, so tasks that are never
# refused pay nothing. Same idea as the owner/permission corrections in
# execution_policy.py; raise sites may still pass their own text.
TASK_NOT_RUNNING_CORRECTION = (
    "This task is not in a running state, so desktop control was refused and nothing ran. This is not the "
    "desktop access setting. Do not control the desktop another way; the action can be tried again once the "
    "task is running.")
# Go on the second and third failed result of one read. The limit text below is only
# seen if the model sends the read a fourth time; a model told to keep trying
# may switch to a shell loop instead and never meet it.
READ_FAILED_TWICE_CORRECTION = (
    "This same read has now failed twice; after one more failure the host stops sending it until the owner "
    "sends a new message. Do not move the retry into a shell command or a wait-and-retry loop; a request to "
    "retry until it works does not change this. Find out why it fails or use a different source that really "
    "covers the need. Otherwise say in your reply what could not be read, so the owner can decide whether to "
    "try again.")
READ_FAILED_THREE_TIMES_CORRECTION = (
    "This read has now failed three times, and the host will not send it again until the owner sends a new "
    "message. Do not keep retrying it another way (a different tool, a shell command or a wait-and-retry "
    "loop); a request to retry until it works does not change this. Use a different source or a different "
    "query if one really covers the need. Otherwise go on with the rest and say in your reply what could not "
    "be read, so the owner can decide whether to try again.")
REJECTION_CORRECTIONS = {
    "agent_ripgrep_unavailable_use_file_search": (
        "ripgrep (rg) is not installed in this node's command environment. Nothing was executed. "
        "Use the built-in ryn_file_search with a path and literal query, or ryn_file_list to find files. "
        "Search alternative terms in separate calls; do not repeat the unavailable rg command."),
    "agent_shell_timeout": (
        "The command was stopped at its timeout. Narrow it, give a larger timeout_seconds, "
        "or run it with background=true and read its output."),
    "agent_desktop_access_required": (
        "Desktop access is turned off for this task and nothing ran. Do not control the screen, the browser or "
        "other apps another way (a shell command, a script or a different tool). Continue with what needs no "
        "desktop control and say in your reply that the rest needs desktop access turned on."),
    "agent_operation_already_confirmed": (
        "This exact operation was already carried out earlier in this task and was not run again. Do not repeat "
        "it or redo it in another form. Its earlier result may have reported an error, so read the current "
        "state to see what took effect, then go on from what you find."),
    "agent_read_retry_limit": (
        "This read has failed three times and was not sent again. Do not keep retrying it another way (a "
        "different tool, a shell command or a wait-and-retry loop); a request to retry until it works does not "
        "change this. Use a different source or a different query if one really covers the need. Otherwise go "
        "on with the rest and say in your reply what could not be read."),
    "agent_local_path_outside_workspace": (
        "文件工具只能访问 workspace_root；需要时请主人切到自动处理或改工作区，不要用 shell 绕过"
    ),
    "agent_review_path_forbidden": (
        "目标路径包含受保护或非法的路径段（如 .git、备用数据流或尾随点/空格），文件工具拒绝访问；请改用合法的普通文件路径，不要用 shell 绕过。"
    ),
    "nas_auth_failed": (
        "NAS 认证失败（账号或密码错误，或权限被拒绝）。请提示主人在存储连接设置中检查密码与用户名，不要循环重试。"
    ),
    "nas_timeout": (
        "NAS 连接或读取超时。请检查网络连接与 NAS 运行负载，不要循环重试。"
    ),
    "nas_dns_failed": (
        "NAS 地址解析失败（DNS 无法解析该主机名）。请提示主人在存储连接设置中检查主机地址与网络 DNS，不要循环重试。"
    ),
    "nas_unreachable": (
        "NAS 不可达（连接被拒绝或目标地址端口未开放）。请提示主人检查网络、NAS 是否开机及地址端口，不要循环重试。"
    ),
    "nas_connection_failed": (
        "NAS 连接异常失败。请提示主人检查网络与连接地址配置，不要循环重试。"
    ),
}


class AgentToolRejected(AgentError):
    """Business layer guarantees this operation caused no side effects.

    Raise only before the mutation boundary; never wrap an uncertain write or
    a post-write publication/verification failure in this exception.
    """

    def __init__(self, code: str, *, correction: str | None = None, details: dict | None = None):
        super().__init__(code)
        self.correction = correction if isinstance(correction, str) and correction else REJECTION_CORRECTIONS.get(code)
        self.details = copy.deepcopy(details) if isinstance(details, dict) and details else None


class AgentReadFailure(AgentError):
    """A dispatched read failed; only bounded connection labels may accompany it."""

    def __init__(self, code, *, connection_id, connection_name):
        super().__init__(code)
        self.connection = {"connection_id": connection_id, "connection_name": connection_name}


def encoded(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")


def _copy(value: Any) -> Any:
    """Detached copy of JSON-shaped state; about 3x faster than copy.deepcopy (measured)."""
    return pickle.loads(pickle.dumps(value, protocol=pickle.HIGHEST_PROTOCOL))


def _blob_name(task_id: str, digest: str) -> str:
    # Task and content prefixes; the root still verifies the full sha256.
    return hashlib.sha256(task_id.encode()).hexdigest()[:8] + digest[:16] + ".json"


class _CorruptRecordError(Exception):
    """Internal marker for deterministic record/blob corruption (never transient I/O)."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


def _stat_or_none(path: Path) -> tuple[int, int, int, int] | None:
    try:
        info = fs_path(path).stat()
    except FileNotFoundError:
        return None
    except OSError:
        raise AgentError("agent_state_unavailable") from None
    return (info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns)


def _read_json_classified(path: Path, *, missing_is_corrupt: bool = False) -> Any:
    try:
        return read_json(path, max_bytes=MAX_BYTES)
    except AtomicIOError as exc:
        cause = exc.__cause__
        if isinstance(cause, (UnicodeDecodeError, ValueError)) or "exceeds max_bytes" in str(exc):
            raise _CorruptRecordError("invalid_json") from None
        if isinstance(cause, FileNotFoundError) and missing_is_corrupt:
            raise _CorruptRecordError("missing_blob") from None
        raise AgentError("agent_state_unavailable") from None
    except FileNotFoundError:
        if missing_is_corrupt:
            raise _CorruptRecordError("missing_blob") from None
        raise AgentError("agent_state_unavailable") from None
    except OSError:
        raise AgentError("agent_state_unavailable") from None
    except (UnicodeDecodeError, ValueError, TypeError):
        raise _CorruptRecordError("invalid_json") from None


class AgentStore:
    def __init__(self, root: str | Path, messaging_key: Any, *, permission_mode: str | None = None):
        # A bare store starts in DEFAULT_SETTINGS' conservative mode, so an
        # embedded runtime never runs changes unasked. The Ryn app passes its
        # shipped mode (SHIPPED_PERMISSION_MODE) for a first start.
        if permission_mode is not None and permission_mode not in PERMISSION_MODES:
            raise ValueError("unsupported Agent permission mode")
        self._permission_mode = permission_mode
        self.root = Path(root)
        self.path = self.root / "sessions.json"
        self.prev_path = self.root / PREV_ROOT_NAME
        self.lock = self.root / ".sessions.lock"
        self.key = messaging_key
        self.pub = peer_box.public_key_b64(messaging_key)
        self._published = None  # Atomic (file stamp, immutable committed state) pair.
        self._cached_stamp = None
        self._cached_data = None
        self._cached_layout = None
        self._migration_pending = False
        self._root_corrupt = False
        self._quarantine_seq = 0
        self._prev_files: set[str] | None = None
        self._index = {}  # task_id -> committed blob entry of the cached data
        self._total = 0  # committed plaintext bytes (root plus every task)

    def _stamp(self):
        info = fs_path(self.path).stat()
        return (info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns)

    def default_workspace(self) -> str:
        """User outputs live apart from the Agent's private state and credentials."""
        expected = self.root.resolve() / "files"
        try:
            expected.mkdir(parents=True, exist_ok=True)
            if expected.resolve(strict=True) != expected:
                raise AgentError("agent_invalid_workspace")
        except OSError:
            raise AgentError("agent_workspace_not_directory") from None
        return str(expected)

    def _read(self, *, copy_result=True) -> dict:
        root_stamp = _stat_or_none(self.path)
        if root_stamp is None:
            self._published = None
            self._cached_stamp = self._cached_data = None
            self._cached_layout = None
            self._migration_pending = False
            self._root_corrupt = False
            self._prev_files = set()
            self._index, self._total = {}, 0
            fresh = {"version": VERSION, "schema_version": 2, "settings": dict(DEFAULT_SETTINGS), "tasks": {},
                     "context_budget_version": 2, "settings_context_budget_provenance": "default_auto"}
            if self._permission_mode is not None:
                fresh["settings"]["permission_mode"] = self._permission_mode
                fresh[PERMISSION_MODE_SOURCE] = "shipped_default"
            return fresh

        prev_stamp = _stat_or_none(self.prev_path) if self._root_corrupt else None
        stamp = (root_stamp, prev_stamp) if self._root_corrupt else root_stamp
        if stamp == self._cached_stamp and self._cached_data is not None:
            return _copy(self._cached_data) if copy_result else self._cached_data

        prior_memory_index = dict(self._index)
        self._prev_files = None
        try:
            data, index, total, layout_ver, mig_pending = self._read_root_candidate(self.path)
            self._root_corrupt = False
            stamp = root_stamp
        except _CorruptRecordError:
            prev_stamp = _stat_or_none(self.prev_path)
            if prev_stamp is None:
                raise AgentError("agent_state_corrupt") from None
            try:
                data, index, total, layout_ver, _ = self._read_root_candidate(self.prev_path)
            except _CorruptRecordError:
                raise AgentError("agent_state_corrupt") from None
            self._root_corrupt = True
            mig_pending = True
            data["recovered_from_prev"] = True
            self._record_post_prev_lost_tasks(data, index, prior_memory_index)
            stamp = (root_stamp, prev_stamp)

        self._cached_stamp, self._cached_data, self._index, self._total = stamp, data, index, total
        self._published = (stamp, data)
        self._cached_layout = layout_ver
        self._migration_pending = mig_pending
        return _copy(data) if copy_result else data

    def _read_root_candidate(self, root_path: Path) -> tuple[dict, dict, int, str, bool]:
        envelope = _read_json_classified(root_path, missing_is_corrupt=False)
        if (
            not isinstance(envelope, dict)
            or envelope.get("version") not in {VERSION, ROOT_VERSION}
            or not isinstance(envelope.get("nonce"), str)
            or not isinstance(envelope.get("ciphertext"), str)
        ):
            raise _CorruptRecordError("version_mismatch")
        try:
            plaintext = peer_box.open_sealed(self.key, self.pub, envelope["nonce"], envelope["ciphertext"], info=CHANNEL)
        except (InvalidTag, ValueError, TypeError, KeyError):
            raise _CorruptRecordError("decrypt_failed") from None
        try:
            data = json.loads(plaintext)
        except (UnicodeDecodeError, ValueError):
            raise _CorruptRecordError("invalid_json") from None
        if not isinstance(data, dict):
            raise _CorruptRecordError("invalid_root")
        if "damaged_tasks" in data and not isinstance(data["damaged_tasks"], dict):
            raise _CorruptRecordError("invalid_root")

        index: dict[str, dict[str, Any]] = {}
        total = len(plaintext)
        newly_damaged = False
        now_ts = int(time.time())

        if envelope["version"] == ROOT_VERSION:
            raw_index = data.pop("task_blobs", None)
            if not isinstance(raw_index, dict) or "tasks" in data:
                raise _CorruptRecordError("invalid_root")
            previous = (self._cached_data or {}).get("tasks", {})
            tasks: dict[str, dict[str, Any]] = {}
            for task_id, entry in raw_index.items():
                cached_entry = self._index.get(task_id)
                try:
                    if (
                        isinstance(entry, dict)
                        and cached_entry == entry
                        and not cached_entry.get("damaged")
                        and task_id in previous
                    ):
                        row = _copy(previous[task_id])
                        migrate_task(row)
                    else:
                        row = self._load_task(task_id, entry)
                except TaskMigrationError as exc:
                    self._record_damaged_task(data, index, task_id, entry, exc.reason, now_ts)
                    newly_damaged = True
                    continue
                except _CorruptRecordError as exc:
                    changed_dmg = self._record_damaged_task(data, index, task_id, entry, exc.reason, now_ts)
                    if changed_dmg or exc.reason != "schema_newer":
                        newly_damaged = True
                    continue
                tasks[task_id] = row
                index[task_id] = {"file": entry["file"], "sha256": entry["sha256"], "bytes": entry["bytes"]}
                total += entry["bytes"]
                if (
                    isinstance(data.get("damaged_tasks"), dict)
                    and isinstance(data["damaged_tasks"].get(task_id), dict)
                    and data["damaged_tasks"][task_id].get("reason") == "schema_newer"
                ):
                    data["damaged_tasks"].pop(task_id, None)
                    newly_damaged = True
            data["tasks"] = tasks
        else:
            if not isinstance(data.get("tasks"), dict):
                raise _CorruptRecordError("invalid_root")
            for task_id in list(data["tasks"]):
                try:
                    migrate_task(data["tasks"][task_id])
                except TaskMigrationError as exc:
                    data["tasks"].pop(task_id, None)
                    data.setdefault("damaged_tasks", {})[task_id] = {
                        "reason": exc.reason,
                        "file": None,
                        "at": now_ts,
                    }
                    newly_damaged = True

        if data.get("version") != VERSION or not isinstance(data.get("tasks"), dict) or not isinstance(data.get("settings"), dict):
            raise _CorruptRecordError("invalid_root")
        try:
            before_migration = encoded(data)
            data = migrate(data)
            mig_changed = (before_migration != encoded(data)) or newly_damaged
        except (ValueError, TypeError, KeyError):
            raise _CorruptRecordError("invalid_root") from None
        return data, index, total, envelope["version"], mig_changed

    @staticmethod
    def _record_damaged_task(
        data: dict,
        index: dict,
        task_id: str,
        entry: Any,
        reason: str,
        now_ts: int,
    ) -> bool:
        file_name = entry.get("file") if isinstance(entry, dict) and isinstance(entry.get("file"), str) else None
        existing = (data.get("damaged_tasks") or {}).get(task_id)
        if (
            isinstance(existing, dict)
            and existing.get("reason") == reason
            and existing.get("file") == file_name
            and type(existing.get("at")) is int
        ):
            at_ts = existing["at"]
            changed = False
        else:
            at_ts = now_ts
            changed = True
        data.setdefault("damaged_tasks", {})[task_id] = {
            "reason": reason,
            "file": file_name,
            "at": at_ts,
        }
        if isinstance(entry, dict):
            index[task_id] = {**entry, "damaged": True, "reason": reason}
        else:
            index[task_id] = {"file": file_name, "bytes": 0, "sha256": "", "damaged": True, "reason": reason}
        return changed

    def _record_post_prev_lost_tasks(
        self,
        data: dict,
        index: dict,
        prior_memory_index: dict,
    ) -> None:
        prev_files = {
            e.get("file")
            for e in index.values()
            if isinstance(e, dict) and isinstance(e.get("file"), str)
        }
        now_ts = int(time.time())
        directory = self.root / TASKS_DIR
        if directory.is_dir():
            try:
                candidates = sorted(directory.iterdir(), key=lambda p: p.name)
            except OSError:
                raise AgentError("agent_state_unavailable") from None
            for path in candidates:
                if not _BLOB_NAME.fullmatch(path.name) or path.name in prev_files:
                    continue
                try:
                    env = _read_json_classified(path, missing_is_corrupt=True)
                    if not isinstance(env, dict) or env.get("version") != TASK_VERSION:
                        continue
                    plain = peer_box.open_sealed(
                        self.key, self.pub, env["nonce"], env["ciphertext"], info=TASK_CHANNEL
                    )
                    row = json.loads(plain)
                except (_CorruptRecordError, InvalidTag, ValueError, TypeError, KeyError):
                    continue
                if isinstance(row, dict) and isinstance(row.get("id"), str) and row["id"]:
                    tid = row["id"]
                    if tid not in index:
                        data.setdefault("damaged_tasks", {})[tid] = {
                            "reason": "lost_after_prev_recovery",
                            "file": path.name,
                            "at": now_ts,
                        }
                        index[tid] = {
                            "file": path.name,
                            "bytes": len(plain),
                            "sha256": hashlib.sha256(plain).hexdigest(),
                            "damaged": True,
                            "reason": "lost_after_prev_recovery",
                        }
        for tid, entry in prior_memory_index.items():
            if tid not in index and tid not in (data.get("damaged_tasks") or {}):
                fname = entry.get("file") if isinstance(entry, dict) and isinstance(entry.get("file"), str) else None
                data.setdefault("damaged_tasks", {})[tid] = {
                    "reason": "lost_after_prev_recovery",
                    "file": fname,
                    "at": now_ts,
                }
                if fname and _BLOB_NAME.fullmatch(fname):
                    index[tid] = {
                        "file": fname,
                        "bytes": 0,
                        "sha256": "",
                        "damaged": True,
                        "reason": "lost_after_prev_recovery",
                    }

    def _load_task(self, task_id: str, entry: Any) -> dict:
        if (
            not isinstance(entry, dict)
            or not isinstance(entry.get("file"), str)
            or not _BLOB_NAME.fullmatch(entry["file"])
            or type(entry.get("bytes")) is not int
            or not isinstance(entry.get("sha256"), str)
        ):
            raise _CorruptRecordError("invalid_index_entry")
        envelope = _read_json_classified(self.root / TASKS_DIR / entry["file"], missing_is_corrupt=True)
        if (
            not isinstance(envelope, dict)
            or envelope.get("version") != TASK_VERSION
            or not isinstance(envelope.get("nonce"), str)
            or not isinstance(envelope.get("ciphertext"), str)
        ):
            raise _CorruptRecordError("version_mismatch")
        try:
            plaintext = peer_box.open_sealed(self.key, self.pub, envelope["nonce"], envelope["ciphertext"], info=TASK_CHANNEL)
        except (InvalidTag, ValueError, TypeError, KeyError):
            raise _CorruptRecordError("decrypt_failed") from None
        # The root commits each task's exact bytes: a swapped or stale blob is refused.
        if len(plaintext) != entry["bytes"] or hashlib.sha256(plaintext).hexdigest() != entry["sha256"]:
            raise _CorruptRecordError("hash_mismatch")
        try:
            row = json.loads(plaintext)
        except (UnicodeDecodeError, ValueError):
            raise _CorruptRecordError("invalid_json") from None
        if not isinstance(row, dict) or row.get("id", task_id) != task_id:
            raise _CorruptRecordError("id_mismatch")
        try:
            migrate_task(row)
        except TaskMigrationError as exc:
            raise _CorruptRecordError(exc.reason) from None
        except (ValueError, TypeError, KeyError):
            raise _CorruptRecordError("incomplete") from None
        return row

    def _extract_root_blob_files(self, path: Path, *, fail_on_unavailable: bool = False) -> set[str]:
        try:
            if _stat_or_none(path) is None:
                return set()
            envelope = _read_json_classified(path, missing_is_corrupt=False)
        except AgentError:
            if fail_on_unavailable:
                raise
            return set()
        except _CorruptRecordError:
            return set()
        if (
            not isinstance(envelope, dict)
            or envelope.get("version") != ROOT_VERSION
            or not isinstance(envelope.get("nonce"), str)
            or not isinstance(envelope.get("ciphertext"), str)
        ):
            return set()
        try:
            plaintext = peer_box.open_sealed(self.key, self.pub, envelope["nonce"], envelope["ciphertext"], info=CHANNEL)
            payload = json.loads(plaintext)
        except (InvalidTag, UnicodeDecodeError, ValueError, TypeError, KeyError):
            return set()
        if not isinstance(payload, dict) or not isinstance(payload.get("task_blobs"), dict):
            return set()
        return {
            entry["file"]
            for entry in payload["task_blobs"].values()
            if isinstance(entry, dict) and isinstance(entry.get("file"), str) and _BLOB_NAME.fullmatch(entry["file"])
        }

    def _next_quarantine_dir(self) -> Path:
        base = self.root / QUARANTINE_DIR
        ts = int(time.time())
        while True:
            self._quarantine_seq += 1
            candidate = base / f"{ts}-{self._quarantine_seq}"
            if not candidate.exists():
                return candidate

    def _write(self, data: dict) -> None:
        migrate(data)
        reservations = data.get("storage_reservations", {})
        for task_id in list(reservations):
            if not data["tasks"].get(task_id, {}).get("in_flight"):
                reservations.pop(task_id)
        quarantine_entries = {
            tid: entry
            for tid, entry in self._index.items()
            if isinstance(entry, dict) and entry.get("damaged") and entry.get("reason") != "schema_newer"
        }
        schema_newer_entries = {
            tid: entry
            for tid, entry in self._index.items()
            if isinstance(entry, dict)
            and entry.get("damaged")
            and entry.get("reason") == "schema_newer"
            and tid not in data["tasks"]
        }
        # A checked transaction can leave state unchanged (menu projection,
        # admission fallback, or an idempotent control request). Keep its lock
        # and fresh read, but avoid another encrypted root/fsync/replace. Legacy
        # roots must still convert even when their logical state is unchanged.
        if (
            self._cached_layout == ROOT_VERSION
            and not self._migration_pending
            and not quarantine_entries
            and not self._root_corrupt
            and data == self._cached_data
        ):
            return
        cached = {} if self._migration_pending else (self._cached_data or {}).get("tasks", {})
        committed_index: dict[str, dict[str, Any]] = {}
        mem_index: dict[str, dict[str, Any]] = {}
        changed: dict[str, bytes] = {}
        for task_id, row in data["tasks"].items():
            entry = self._index.get(task_id)
            # Deep equality is cheap here: an unchanged row shares its strings with the cache.
            if entry is not None and not entry.get("damaged") and cached.get(task_id) == row:
                clean_entry = {"file": entry["file"], "sha256": entry["sha256"], "bytes": entry["bytes"]}
                committed_index[task_id] = clean_entry
                mem_index[task_id] = clean_entry
                continue
            raw = encoded(row)
            digest = hashlib.sha256(raw).hexdigest()
            clean_entry = {"file": _blob_name(task_id, digest), "sha256": digest, "bytes": len(raw)}
            committed_index[task_id] = clean_entry
            mem_index[task_id] = clean_entry
            changed[task_id] = raw
        for task_id, entry in schema_newer_entries.items():
            if (
                isinstance(entry.get("file"), str)
                and _BLOB_NAME.fullmatch(entry["file"])
                and type(entry.get("bytes")) is int
                and isinstance(entry.get("sha256"), str)
            ):
                clean_entry = {"file": entry["file"], "sha256": entry["sha256"], "bytes": entry["bytes"]}
                committed_index[task_id] = clean_entry
                mem_index[task_id] = {**clean_entry, "damaged": True, "reason": "schema_newer"}

        directory = self.root / TASKS_DIR
        has_task_dir = directory.is_dir()
        retired_prev_files = (
            self._prev_files
            if self._prev_files is not None
            else (self._extract_root_blob_files(self.prev_path) if has_task_dir else set())
        )
        old_root_bytes = b""
        if not self._root_corrupt and _stat_or_none(self.path) is not None:
            try:
                old_root_bytes = fs_path(self.path).read_bytes()
            except OSError:
                raise AgentError("agent_state_unavailable") from None

        root = encoded({**{key: value for key, value in data.items() if key != "tasks"}, "task_blobs": committed_index})
        total = len(root) + sum(entry["bytes"] for task_id, entry in committed_index.items() if task_id in data["tasks"])
        remaining = sum(
            max(0, item["bytes"] - max(0, committed_index[task_id]["bytes"] - item["baseline"]))
            for task_id, item in reservations.items()
            if task_id in committed_index
        )
        # Existing immutable blobs need no second copy. Include encrypted/base64
        # overhead, the new root, the .prev backup, and every outstanding receipt reservation.
        self._require_disk(
            2 * (len(root) + len(old_root_bytes) + sum(map(len, changed.values()))) + 3 * remaining + 64 * 1024
        )
        if changed:
            directory.mkdir(parents=True, exist_ok=True)
            has_task_dir = True
        for task_id, raw in changed.items():
            target = directory / committed_index[task_id]["file"]
            if not target.exists():
                nonce, ciphertext = peer_box.seal(self.key, self.pub, raw, info=TASK_CHANNEL)
                atomic_write_json(target, {"version": TASK_VERSION, "nonce": nonce, "ciphertext": ciphertext}, max_bytes=MAX_BYTES)
        if old_root_bytes:
            atomic_write_bytes(self.prev_path, old_root_bytes, max_bytes=MAX_BYTES)
        nonce, ciphertext = peer_box.seal(self.key, self.pub, root, info=CHANNEL)
        atomic_write_json(self.path, {"version": ROOT_VERSION, "nonce": nonce, "ciphertext": ciphertext}, max_bytes=MAX_BYTES)
        if quarantine_entries and has_task_dir:
            q_dir: Path | None = None
            for entry in quarantine_entries.values():
                fname = entry.get("file")
                if not isinstance(fname, str) or not _BLOB_NAME.fullmatch(fname):
                    continue
                src = directory / fname
                if not src.exists():
                    continue
                if q_dir is None:
                    q_dir = self._next_quarantine_dir()
                    q_dir.mkdir(parents=True, exist_ok=True)
                os.replace(fs_path(src), fs_path(q_dir / fname))
        # Committed. Unchanged rows keep their cached objects; only changes are copied.
        previous_index = self._index
        prev_live = (
            {
                entry["file"]
                for entry in previous_index.values()
                if isinstance(entry, dict) and isinstance(entry.get("file"), str) and _BLOB_NAME.fullmatch(entry["file"])
            }
            if old_root_bytes
            else retired_prev_files
        )
        self._prev_files = prev_live
        cache = _copy({key: value for key, value in data.items() if key != "tasks"})
        cache["tasks"] = {task_id: _copy(row) if task_id in changed else cached[task_id] for task_id, row in data["tasks"].items()}
        self._root_corrupt = False
        self._cached_stamp, self._cached_data, self._index, self._total = self._stamp(), cache, mem_index, total
        self._published = (self._cached_stamp, cache)
        self._cached_layout = ROOT_VERSION
        self._migration_pending = False
        if has_task_dir:
            live = {entry["file"] for entry in committed_index.values()} | prev_live
            candidates = retired_prev_files | {
                entry["file"]
                for entry in previous_index.values()
                if isinstance(entry, dict) and not entry.get("damaged") and isinstance(entry.get("file"), str)
            }
            for fname in candidates:
                if fname not in live:
                    try:
                        (directory / fname).unlink()
                    except OSError:
                        pass  # Swept by purge_orphan_snapshots at the next start.

    def reserve(self, data: dict, task_id: str, expected_growth: int, *, extra_disk: int = 0) -> None:
        """Called in the dispatch transaction, before any external operation.

        Other task/control transactions must respect this remaining reservation.
        As this task's receipt/events grow, actual bytes replace reserved bytes.
        Clearing its in-flight marker releases the unused portion atomically.
        """
        # Reserve only this task rewrite, not a second copy of all history.
        current = len(encoded(data["tasks"][task_id]))
        others = sum(max(0, item["bytes"] - max(0, len(encoded(data["tasks"][key])) - item["baseline"]))
                     for key, item in data.get("storage_reservations", {}).items() if key != task_id)
        self._require_disk(2 * current + 3 * (expected_growth + extra_disk + others) + 64 * 1024)
        data.setdefault("storage_reservations", {})[task_id] = {"bytes": expected_growth, "baseline": current}

    def _require_disk(self, required: int) -> None:
        root = self.root if self.root.exists() else self.root.parent
        while not root.exists():
            root = root.parent
        try:
            free = shutil.disk_usage(root).free
        except OSError:
            raise AgentError("agent_storage_unavailable") from None
        if free < required:
            raise AgentError("agent_storage_limit")

    def purge_orphan_snapshots(self) -> None:
        """Caller holds this store's transaction lock; no writer owns a temp."""
        if self.root.exists():
            for path in self.root.iterdir():
                if re.fullmatch(r"\.sessions\.json(?:\.prev)?\.[a-f0-9]{12,32}\.tmp", path.name):
                    path.unlink()
        root_exists = False
        try:
            root_exists = _stat_or_none(self.path) is not None
        except AgentError:
            return
        if not root_exists:
            self.prev_path.unlink(missing_ok=True)
        directory = self.root / TASKS_DIR
        if directory.is_dir():
            try:
                self._read(copy_result=False)
                prev_live = self._extract_root_blob_files(self.prev_path, fail_on_unavailable=True) if root_exists else set()
            except AgentError:
                return  # Never delete task files the current root could not be checked against.
            live = {
                entry["file"]
                for entry in self._index.values()
                if isinstance(entry, dict) and isinstance(entry.get("file"), str)
            } | prev_live
            for path in directory.iterdir():
                if (re.fullmatch(r"\..+\.[a-f0-9]{12,32}\.tmp", path.name)
                        or (_BLOB_NAME.fullmatch(path.name) and path.name not in live)):
                    path.unlink()

    def _snapshot(self) -> dict:
        # Writers publish only after atomic root replacement. A reader may see
        # the preceding commit while a writer prepares the next one. A changed
        # root (including another process's commit) must be reloaded under lock.
        published = self._published
        if published is not None:
            try:
                expected = (self._stamp(), _stat_or_none(self.prev_path)) if self._root_corrupt else self._stamp()
                if published[0] == expected:
                    return published[1]
            except (OSError, AgentError):
                pass
        with file_transaction(self.lock):
            return self._read(copy_result=False)

    def read(self) -> dict:
        return _copy(self._snapshot())

    def usage(self) -> dict:
        """Committed decrypted bytes of the active state, for capacity decisions.

        Per-task sizes come from the split layout's root index; a legacy single
        file reports none, and callers measure those rows themselves.
        """
        data = self._snapshot()
        total = self._total if self._index or not data.get("tasks") else len(encoded(data))
        return {
            "total": total,
            "limit": None,
            "count": len(data.get("tasks", {})),
            "tasks": {
                task_id: entry["bytes"]
                for task_id, entry in self._index.items()
                if isinstance(entry, dict) and not entry.get("damaged") and type(entry.get("bytes")) is int
            },
        }

    def read_control(self, task_id: str) -> dict:
        """Fresh cancellation authority without cloning every task's transcript.

        The committed root is validated exactly as in read(). Only a detached
        projection escapes the lock; resource origins retain their exact epoch.
        """
        data = self._snapshot()
        row = data.get("tasks", {}).get(task_id)
        if row is None:
            raise AgentError("agent_task_not_found")
        control = {key: value for key, value in row.items() if key not in {
            "messages", "events", "receipts", "result_refs", "verification_evidence", "result",
            "queue", "context_checkpoint", "verification", "verification_rejected_responses", "conversation_branches"}}
        control["messages"] = [{key: message[key] for key in ("role", "context_epoch", "resource_origin") if key in message}
                               for message in row.get("messages", []) if isinstance(message.get("resource_origin"), dict)]
        return copy.deepcopy({"settings": data["settings"], "projects": data.get("projects", {}),
                              "tasks": {task_id: control}})

    def inspect(self, view: Callable[[dict], Any]) -> Any:
        """Run a read-only view over the cached state without copying every task.

        The view must not modify its argument; only its result is detached.
        """
        started = time.monotonic()
        snapshot = self._snapshot()
        acquired = time.monotonic()
        result = _copy(view(snapshot))
        performance("state_inspect", lock_wait_seconds=acquired - started,
                    read_view_seconds=time.monotonic() - acquired)
        return result

    def transaction(self, mutate: Callable[[dict], Any]) -> Any:
        started = time.monotonic()
        snapshot = self._snapshot()
        data = _copy(snapshot)
        prepared = time.monotonic()
        with file_transaction(self.lock):
            acquired = time.monotonic()
            current = self._read(copy_result=False)
            if current is not snapshot:
                # A concurrent writer won. Rebase before invoking the callback
                # so mutations with external effects are never replayed.
                data = _copy(current)
            read = time.monotonic()
            result = mutate(data)
            mutated = time.monotonic()
            self._write(data)
            written = time.monotonic()
        # _write publishes detached objects. The transaction owns its result.
        result = copy.deepcopy(result)
        operation = getattr(getattr(mutate, "__code__", None), "co_qualname", "callback")
        performance("state_transaction", operation=operation, prepare_seconds=prepared - started,
                    lock_wait_seconds=acquired - prepared,
                    read_seconds=read - acquired, mutation_seconds=mutated - read,
                    write_seconds=written - mutated, copy_seconds=time.monotonic() - written)
        return result
