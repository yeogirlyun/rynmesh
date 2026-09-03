"""Persistent milestones for the owner's first useful Ryn session.

The record intentionally contains only booleans and timestamps. Content titles,
URLs, feedback text, and other personal material belong to their existing local
stores and are never duplicated here.
"""

from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path
from typing import Any, Mapping

__all__ = ["FIRST_SUCCESS_VERSION", "FirstSuccessStore"]

FIRST_SUCCESS_VERSION = "ryn.first-success.v1"
_MILESTONES = (
    "node_ready",
    "content_ready",
    "first_item_opened",
    "first_signal_recorded",
    "completed",
)


def _empty() -> dict[str, Any]:
    return {
        "version": FIRST_SUCCESS_VERSION,
        "dismissed": False,
        "dismissed_at_unix": 0.0,
        "replay_started_at_unix": 0.0,
        "milestones": dict.fromkeys(_MILESTONES, 0.0),
    }


class FirstSuccessStore:
    """Atomic, monotonic local record of first-success milestones."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self._lock = threading.RLock()

    def get(self) -> dict[str, Any]:
        with self._lock:
            return self._load()

    def record(self, milestone: str, *, now_unix: float | None = None) -> dict[str, Any]:
        name = str(milestone or "").strip()
        if name not in _MILESTONES:
            raise ValueError("first_success_milestone_invalid")
        stamp = time.time() if now_unix is None else max(0.0, float(now_unix))
        with self._lock:
            data = self._load()
            if not float(data["milestones"].get(name, 0.0) or 0.0):
                data["milestones"][name] = stamp
                self._write(data)
            return data

    def sync(
        self,
        *,
        node_ready: bool,
        content_ready: bool,
        first_item_opened: bool,
        first_signal_recorded: bool,
        now_unix: float | None = None,
    ) -> dict[str, Any]:
        """Persist newly observed milestones without ever moving progress backwards."""
        stamp = time.time() if now_unix is None else max(0.0, float(now_unix))
        observed = {
            "node_ready": bool(node_ready),
            "content_ready": bool(content_ready),
            "first_item_opened": bool(first_item_opened),
            "first_signal_recorded": bool(first_signal_recorded),
        }
        with self._lock:
            data = self._load()
            changed = False
            for name, ready in observed.items():
                if ready and not float(data["milestones"].get(name, 0.0) or 0.0):
                    data["milestones"][name] = stamp
                    changed = True
            complete = all(
                float(data["milestones"].get(name, 0.0) or 0.0)
                for name in ("first_item_opened", "first_signal_recorded")
            )
            if complete and not float(data["milestones"].get("completed", 0.0) or 0.0):
                data["milestones"]["completed"] = stamp
                changed = True
            if changed:
                self._write(data)
            return data

    def dismiss(self, *, now_unix: float | None = None) -> dict[str, Any]:
        stamp = time.time() if now_unix is None else max(0.0, float(now_unix))
        with self._lock:
            data = self._load()
            data["dismissed"] = True
            if not float(data.get("dismissed_at_unix", 0.0) or 0.0):
                data["dismissed_at_unix"] = stamp
            self._write(data)
            return data

    def reset(self, *, now_unix: float | None = None) -> dict[str, Any]:
        stamp = time.time() if now_unix is None else max(0.0, float(now_unix))
        with self._lock:
            data = _empty()
            data["replay_started_at_unix"] = stamp
            self._write(data)
            return data

    def _load(self) -> dict[str, Any]:
        data = _empty()
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return data
        if not isinstance(raw, Mapping) or raw.get("version") != FIRST_SUCCESS_VERSION:
            return data
        data["dismissed"] = bool(raw.get("dismissed", False))
        try:
            data["dismissed_at_unix"] = max(
                0.0, float(raw.get("dismissed_at_unix", 0.0) or 0.0)
            )
        except (TypeError, ValueError):
            data["dismissed_at_unix"] = 0.0
        try:
            data["replay_started_at_unix"] = max(
                0.0, float(raw.get("replay_started_at_unix", 0.0) or 0.0)
            )
        except (TypeError, ValueError):
            data["replay_started_at_unix"] = 0.0
        raw_milestones = raw.get("milestones", {})
        if isinstance(raw_milestones, Mapping):
            for name in _MILESTONES:
                try:
                    data["milestones"][name] = max(
                        0.0, float(raw_milestones.get(name, 0.0) or 0.0)
                    )
                except (TypeError, ValueError):
                    data["milestones"][name] = 0.0
        return data

    def _write(self, data: Mapping[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(self.path.suffix + ".tmp")
        temporary.write_text(
            json.dumps(dict(data), indent=2, sort_keys=True), encoding="utf-8"
        )
        os.replace(temporary, self.path)
