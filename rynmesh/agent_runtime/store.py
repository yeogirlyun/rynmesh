"""Encrypted transactional Agent state using the node's existing messaging key."""
from __future__ import annotations

import copy
import hashlib
import json
import pickle
import re
import shutil
import time
from pathlib import Path
from typing import Any, Callable

from cryptography.exceptions import InvalidTag

from ..atomic_io import atomic_write_json, read_json
from ..file_transactions import file_transaction
from ..services import peer_box
from .diagnostic_log import performance
from .state import migrate

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
_BLOB_NAME = re.compile(r"[a-f0-9]{24}\.json")
MAX_PLAINTEXT = 24 * 1024 * 1024
MAX_BYTES = 34 * 1024 * 1024
DEFAULT_SETTINGS = {
    "enabled": False, "model_id": "", "permission_mode": "workspace", "allowed_write_tools": [],
    "workspace_root": "", "desktop_access": False, "verify_text_responses": False,
    # 60000 left ~30 KB for conversation after the always-sent tool schemas, so a coding task
    # compacted every second step and spent half its time budget on summaries (measured).
    # Agent-wide defaults, separate from model-source settings (output cap, reasoning effort).
    # max_context_chars is only a ceiling: the selected model's declared window clamps it further.
    # Time and steps stay moderate (owners can raise them to 3600 s / 128 steps). A run that
    # needed 48+ steps (23 of them locating the code) hit the old 48-step cap mid-edit; the loop
    # now flags repeated reads, ranks search hits per file and announces budget use at 60%/85%.
    # max_context_chars is a ceiling: a model with a declared window gets most of that window
    # (120000 when no window is declared), so long work compacts far less often. 120000 as a
    # ceiling compacted a 31-step task four times (71 s of summaries, measured).
    "max_steps": 64, "max_seconds": 900, "max_context_chars": 600000,
    "max_output_chars": 20000,
}
# The previous default ceiling, still used when a model does not declare its window.
UNKNOWN_WINDOW_CONTEXT_CHARS = 120000


class AgentError(ValueError):
    """Stable public error code, never an external exception's raw message."""


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


class AgentToolRejected(AgentError):
    """Business layer guarantees this operation caused no side effects.

    Raise only before the mutation boundary; never wrap an uncertain write or
    a post-write publication/verification failure in this exception.
    """

    def __init__(self, code: str, *, correction: str | None = None, details: dict | None = None):
        super().__init__(code)
        self.correction = correction if isinstance(correction, str) and correction else None
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


class AgentStore:
    def __init__(self, root: str | Path, messaging_key: Any):
        self.root = Path(root)
        self.path = self.root / "sessions.json"
        self.lock = self.root / ".sessions.lock"
        self.key = messaging_key
        self.pub = peer_box.public_key_b64(messaging_key)
        self._cached_stamp = None
        self._cached_data = None
        self._index = {}  # task_id -> committed blob entry of the cached data
        self._total = 0  # committed plaintext bytes (root plus every task)

    def _stamp(self):
        info = self.path.stat()
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
        if not self.path.exists():
            self._cached_stamp = self._cached_data = None
            self._index, self._total = {}, 0
            return {"version": VERSION, "schema_version": 2, "settings": dict(DEFAULT_SETTINGS), "tasks": {},
                    "context_budget_version": 1}
        try:
            stamp = self._stamp()
            if stamp == self._cached_stamp and self._cached_data is not None:
                return _copy(self._cached_data) if copy_result else self._cached_data
            envelope = read_json(self.path, max_bytes=MAX_BYTES)
            if not isinstance(envelope, dict) or envelope.get("version") not in {VERSION, ROOT_VERSION}:
                raise AgentError("agent_state_version_unsupported")
            plaintext = peer_box.open_sealed(self.key, self.pub, envelope["nonce"], envelope["ciphertext"], info=CHANNEL)
            if len(plaintext) > MAX_PLAINTEXT:
                raise AgentError("agent_storage_limit")
            data = json.loads(plaintext)
            if not isinstance(data, dict):
                raise AgentError("agent_state_unreadable")
            index, total = {}, len(plaintext)
            if envelope["version"] == ROOT_VERSION:
                index = data.pop("task_blobs", None)
                if not isinstance(index, dict) or "tasks" in data:
                    raise AgentError("agent_state_unreadable")
                previous = (self._cached_data or {}).get("tasks", {})
                tasks = {}
                for task_id, entry in index.items():
                    # A content-addressed blob already parsed for the cache is reused.
                    tasks[task_id] = (previous[task_id] if self._index.get(task_id) == entry and task_id in previous
                                      else self._load_task(task_id, entry))
                    total += entry["bytes"]
                if total > MAX_PLAINTEXT:
                    raise AgentError("agent_storage_limit")
                data["tasks"] = tasks
            if data.get("version") != VERSION or not isinstance(data.get("tasks"), dict) or not isinstance(data.get("settings"), dict):
                raise AgentError("agent_state_unreadable")
            data = migrate(data)
            self._cached_stamp, self._cached_data, self._index, self._total = stamp, data, index, total
            return _copy(data) if copy_result else data
        except AgentError:
            raise
        except (OSError, ValueError, KeyError, TypeError, InvalidTag):
            raise AgentError("agent_state_unreadable") from None

    def _load_task(self, task_id: str, entry: Any) -> dict:
        if (not isinstance(entry, dict) or not isinstance(entry.get("file"), str) or not _BLOB_NAME.fullmatch(entry["file"])
                or type(entry.get("bytes")) is not int or not isinstance(entry.get("sha256"), str)):
            raise AgentError("agent_state_unreadable")
        envelope = read_json(self.root / TASKS_DIR / entry["file"], max_bytes=MAX_BYTES)
        if not isinstance(envelope, dict) or envelope.get("version") != TASK_VERSION:
            raise AgentError("agent_state_unreadable")
        plaintext = peer_box.open_sealed(self.key, self.pub, envelope["nonce"], envelope["ciphertext"], info=TASK_CHANNEL)
        # The root commits each task's exact bytes: a swapped or stale blob is refused.
        if len(plaintext) != entry["bytes"] or hashlib.sha256(plaintext).hexdigest() != entry["sha256"]:
            raise AgentError("agent_state_unreadable")
        row = json.loads(plaintext)
        if not isinstance(row, dict) or row.get("id", task_id) != task_id:
            raise AgentError("agent_state_unreadable")
        return row

    def _write(self, data: dict) -> None:
        migrate(data)
        reservations = data.get("storage_reservations", {})
        for task_id in list(reservations):
            if not data["tasks"].get(task_id, {}).get("in_flight"):
                reservations.pop(task_id)
        cached = (self._cached_data or {}).get("tasks", {})
        index, changed = {}, {}
        for task_id, row in data["tasks"].items():
            entry = self._index.get(task_id)
            # Deep equality is cheap here: an unchanged row shares its strings with the cache.
            if entry is not None and cached.get(task_id) == row:
                index[task_id] = entry
                continue
            raw = encoded(row)
            digest = hashlib.sha256(raw).hexdigest()
            index[task_id] = {"file": _blob_name(task_id, digest), "sha256": digest, "bytes": len(raw)}
            changed[task_id] = raw
        root = encoded({**{key: value for key, value in data.items() if key != "tasks"}, "task_blobs": index})
        total = len(root) + sum(entry["bytes"] for entry in index.values())
        remaining = sum(max(0, item["bytes"] - max(0, index[task_id]["bytes"] - item["baseline"]))
                        for task_id, item in reservations.items())
        if total + remaining > MAX_PLAINTEXT:
            raise AgentError("agent_storage_limit")
        directory = self.root / TASKS_DIR
        if changed:
            directory.mkdir(parents=True, exist_ok=True)
        for task_id, raw in changed.items():
            target = directory / index[task_id]["file"]
            if not target.exists():
                nonce, ciphertext = peer_box.seal(self.key, self.pub, raw, info=TASK_CHANNEL)
                atomic_write_json(target, {"version": TASK_VERSION, "nonce": nonce, "ciphertext": ciphertext}, max_bytes=MAX_BYTES)
        nonce, ciphertext = peer_box.seal(self.key, self.pub, root, info=CHANNEL)
        atomic_write_json(self.path, {"version": ROOT_VERSION, "nonce": nonce, "ciphertext": ciphertext}, max_bytes=MAX_BYTES)
        # Committed. Unchanged rows keep their cached objects; only changes are copied.
        previous_index = self._index
        cache = _copy({key: value for key, value in data.items() if key != "tasks"})
        cache["tasks"] = {task_id: _copy(row) if task_id in changed else cached[task_id] for task_id, row in data["tasks"].items()}
        self._cached_stamp, self._cached_data, self._index, self._total = self._stamp(), cache, index, total
        live = {entry["file"] for entry in index.values()}
        for entry in previous_index.values():
            if entry["file"] not in live:
                try:
                    (directory / entry["file"]).unlink()
                except OSError:
                    pass  # Swept by purge_orphan_snapshots at the next start.

    def reserve(self, data: dict, task_id: str, expected_growth: int, *, extra_disk: int = 0) -> None:
        """Called in the dispatch transaction, before any external operation.

        Other task/control transactions must respect this remaining reservation.
        As this task's receipt/events grow, actual bytes replace reserved bytes.
        Clearing its in-flight marker releases the unused portion atomically.
        """
        # The committed size is a sufficient headroom estimate; encoding the whole
        # state here cost as much as the write itself.
        current = self._total if self._cached_data is not None else len(encoded(data))
        root = self.root if self.root.exists() else self.root.parent
        try:
            free = shutil.disk_usage(root).free
        except OSError:
            raise AgentError("agent_storage_unavailable") from None
        if free < 2 * current + 3 * (expected_growth + extra_disk) + 64 * 1024:
            raise AgentError("agent_storage_limit")
        data.setdefault("storage_reservations", {})[task_id] = {"bytes": expected_growth, "baseline": len(encoded(data["tasks"][task_id]))}

    def purge_orphan_snapshots(self) -> None:
        """Caller holds this store's transaction lock; no writer owns a temp."""
        if self.root.exists():
            for path in self.root.iterdir():
                if re.fullmatch(r"\.sessions\.json\.[a-f0-9]{32}\.tmp", path.name):
                    path.unlink()
        directory = self.root / TASKS_DIR
        if directory.is_dir():
            try:
                self._read(copy_result=False)
            except AgentError:
                return  # Never delete task files the current root could not be checked against.
            live = {entry["file"] for entry in self._index.values()}
            for path in directory.iterdir():
                if (re.fullmatch(r"\..+\.[a-f0-9]{32}\.tmp", path.name)
                        or (_BLOB_NAME.fullmatch(path.name) and path.name not in live)):
                    path.unlink()

    def read(self) -> dict:
        with file_transaction(self.lock):
            return self._read()

    def read_control(self, task_id: str) -> dict:
        """Fresh cancellation authority without cloning every task's transcript.

        The cache stamp and file lock are identical to read(). Only a detached
        projection escapes the lock; resource origins retain their exact epoch.
        """
        with file_transaction(self.lock):
            data = self._read(copy_result=False)
            row = data.get("tasks", {}).get(task_id)
            if row is None:
                raise AgentError("agent_task_not_found")
            control = {key: value for key, value in row.items() if key not in {
                "messages", "events", "receipts", "result_refs", "verification_evidence", "result",
                "queue", "context_checkpoint", "verification", "verification_rejected_responses"}}
            control["messages"] = [{key: message[key] for key in ("role", "context_epoch", "resource_origin") if key in message}
                                   for message in row.get("messages", []) if isinstance(message.get("resource_origin"), dict)]
            return copy.deepcopy({"settings": data["settings"], "projects": data.get("projects", {}),
                                  "tasks": {task_id: control}})

    def inspect(self, view: Callable[[dict], Any]) -> Any:
        """Run a read-only view over the cached state without copying every task.

        The view must not modify its argument; only its result is detached.
        """
        started = time.monotonic()
        with file_transaction(self.lock):
            acquired = time.monotonic()
            result = _copy(view(self._read(copy_result=False)))
        performance("state_inspect", lock_wait_seconds=acquired - started,
                    read_view_seconds=time.monotonic() - acquired)
        return result

    def transaction(self, mutate: Callable[[dict], Any]) -> Any:
        started = time.monotonic()
        with file_transaction(self.lock):
            acquired = time.monotonic()
            data = self._read()
            read = time.monotonic()
            result = mutate(data)
            mutated = time.monotonic()
            self._write(data)
            written = time.monotonic()
            result = copy.deepcopy(result)
        performance("state_transaction", lock_wait_seconds=acquired - started,
                    read_seconds=read - acquired, mutation_seconds=mutated - read,
                    write_seconds=written - mutated, copy_seconds=time.monotonic() - written)
        return result
