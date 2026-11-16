"""Transport contracts and optional model diagnostics, separate from admission."""
from __future__ import annotations

import hashlib
import json
import threading
import time
from collections import OrderedDict
from pathlib import Path

from ..atomic_io import atomic_write_json, read_json
from ..file_transactions import file_transaction

_CLI_STAT_TTL_SECONDS = 30.0
_CLI_STAT_LOCK = threading.Lock()
_CLI_STAT_CACHE: dict[tuple[str, object], tuple[float, list | str]] = {}


def client_tools_contract(adapter, capabilities=()):
    """Declared bridge support; never run capability discovery or model inference."""
    return (adapter != "claude_cli" and "api-text-only" not in capabilities
            and (adapter in {"openai_compatible", "openai_chat", "openai_responses", "anthropic_messages",
                             "ollama", "llama_cpp", "codex_cli", "antigravity_cli"}
                 or "client-tools" in capabilities))


class ToolCapabilities:
    def __init__(self, home):
        self.path = Path(home) / "llm" / "tool-capabilities.json"
        self.lock = self.path.with_suffix(".lock")
        self._mem_lock = threading.Lock()
        self._cached_sig: tuple[int, int] | None = None
        self._cached_rows: OrderedDict[str, dict] = OrderedDict()
        self._access_seq = 0
        self._access_order: dict[str, int] = {}

    @staticmethod
    def fingerprint(manifest):
        # Persist a digest only; endpoint credentials/paths never leave this process.
        config = manifest.to_dict()
        config.pop("capabilities", None)
        if manifest.adapter == "codex_cli":
            config["client_tool_bridge_revision"] = "structured-proposals-v1"
        if manifest.adapter in {"codex_cli", "claude_cli", "antigravity_cli"}:
            from .cli_adapter import CLIAgentAdapter

            cache_key = (manifest.adapter, CLIAgentAdapter._command_path)
            now = time.monotonic()
            with _CLI_STAT_LOCK:
                cached = _CLI_STAT_CACHE.get(cache_key)
            if cached is not None and now < cached[0]:
                config["cli_revision"] = cached[1]
            else:
                try:
                    executable = Path(CLIAgentAdapter(manifest.adapter)._command_path())
                    stat = executable.stat()
                    cli_rev: list | str = [str(executable), stat.st_mtime_ns, stat.st_size]
                except (OSError, RuntimeError):
                    cli_rev = "unavailable"
                with _CLI_STAT_LOCK:
                    if len(_CLI_STAT_CACHE) >= 64:
                        _CLI_STAT_CACHE.clear()
                    _CLI_STAT_CACHE[cache_key] = (time.monotonic() + _CLI_STAT_TTL_SECONDS, cli_rev)
                config["cli_revision"] = cli_rev
        return hashlib.sha256(json.dumps(config, sort_keys=True).encode()).hexdigest()

    def _read_ordered(self) -> OrderedDict[str, dict]:
        try:
            value = read_json(self.path, max_bytes=128 * 1024)
            return OrderedDict(value) if isinstance(value, dict) else OrderedDict()
        except (OSError, ValueError):
            return OrderedDict()

    def _read(self):
        return dict(self._read_ordered())

    def _snapshot_unlocked(self) -> OrderedDict[str, dict]:
        try:
            st = self.path.stat()
            sig = (st.st_mtime_ns, st.st_size)
        except OSError:
            return OrderedDict()
        with self._mem_lock:
            if self._cached_sig == sig:
                return OrderedDict(self._cached_rows)
        with file_transaction(self.lock):
            rows = self._read_ordered()
            try:
                st2 = self.path.stat()
                sig2 = (st2.st_mtime_ns, st2.st_size)
            except OSError:
                sig2 = sig
            with self._mem_lock:
                self._cached_sig = sig2
                self._cached_rows = OrderedDict(rows)
            return rows

    def support(self, manifest, model=""):
        key = manifest.package_id + ("/model/" + model if model else "")
        rows = self._snapshot_unlocked()
        row = rows.get(key)
        if not isinstance(row, dict):
            return "unknown"
        if row.get("fingerprint") != self.fingerprint(manifest):
            return "unknown"
        with self._mem_lock:
            self._access_seq += 1
            self._access_order[key] = self._access_seq
        return row.get("tool_support", "unknown")

    def save(self, manifest, support, model=""):
        key = manifest.package_id + ("/model/" + model if model else "")
        if support not in {"supported", "unsupported", "unknown"}:
            raise ValueError("invalid_tool_support")
        fp = self.fingerprint(manifest)
        with file_transaction(self.lock):
            rows = self._read_ordered()
            with self._mem_lock:
                # Apply in-process read recency before evicting, then move the saved key to the tail.
                ordered_keys = sorted(
                    enumerate(rows.keys()),
                    key=lambda pair: (1, self._access_order[pair[1]]) if pair[1] in self._access_order else (0, pair[0]),
                )
                reordered = OrderedDict((k, rows[k]) for _idx, k in ordered_keys if k != key)
                reordered[key] = {"fingerprint": fp, "tool_support": support}
                # Local sources keep their own evidence; this file tracks only managed services.
                while len(reordered) > 128:
                    evicted_key, _ = reordered.popitem(last=False)
                    self._access_order.pop(evicted_key, None)
                self._access_seq += 1
                self._access_order[key] = self._access_seq
            atomic_write_json(self.path, reordered, sort_keys=False, max_bytes=128 * 1024)
            try:
                st = self.path.stat()
                with self._mem_lock:
                    self._cached_sig = (st.st_mtime_ns, st.st_size)
                    self._cached_rows = OrderedDict(reordered)
            except OSError:
                with self._mem_lock:
                    self._cached_sig = None
