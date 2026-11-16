"""Persistent local node settings (auto_update, ...) at $RYNMESH_HOME/settings.json."""
from __future__ import annotations

import base64
import json
from pathlib import Path
from typing import Any

from . import os_secret
from .atomic_io import atomic_write_json

_DEFAULTS: dict[str, Any] = {
    # Daily recap email config (nested dict; see services/recap.py).
    "recap": {},
    "auto_update": True,
    "ai_model": "",
    "node_name": "",
    "safety_policy": "standard",
    "ai_provider": "local",
    "cloud_access": False,
    "rank_default": "weight",
    "publish_visibility": "network",
    "fetch_budget_mb": 8192,
    "fetch_timeout_s": 20,
    "onboarding_version": 0,
    "notifications_enabled": True,
    "notification_frequency": "immediate",
    "notification_quiet_start": 22,
    "notification_quiet_end": 8,
}
_WHITELIST = set(_DEFAULTS)

_CHOICES = {
    "safety_policy": {"permissive", "standard", "strict"},
    "ai_provider": {"local", "cloud"},
    "rank_default": {"weight", "newest", "trusted", "ai", "novelty"},
    "publish_visibility": {"network", "trusted", "local"},
    "notification_frequency": {"immediate", "hourly", "daily"},
}


class SettingsStore:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)

    def get(self) -> dict[str, Any]:
        data = dict(_DEFAULTS)
        loaded_dict: dict[str, Any] | None = None
        try:
            loaded = json.loads(self.path.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                loaded_dict = loaded
                for k in _WHITELIST:
                    if k in loaded:
                        data[k] = loaded[k]
        except (OSError, ValueError):
            pass
        recap = data.get("recap")
        if isinstance(recap, dict) and "smtp_password" in recap:
            migrated = dict(recap)
            raw_pw = str(migrated.pop("smtp_password", "") or "").strip()[:512]
            if raw_pw:
                migrated["smtp_password_protected"] = base64.b64encode(
                    os_secret.protect(raw_pw.encode("utf-8"))
                ).decode("ascii")
            data["recap"] = migrated
            if loaded_dict is not None:
                try:
                    to_write = dict(loaded_dict)
                    to_write["recap"] = migrated
                    atomic_write_json(self.path, to_write, indent=2, sort_keys=False)
                except OSError:
                    pass
        return data

    def patch(self, patch: dict[str, Any]) -> dict[str, Any]:
        data = self.get()
        for k, v in patch.items():
            if k not in _WHITELIST:
                continue
            if k in _CHOICES:
                value = str(v or "").strip().lower()
                if value in _CHOICES[k]:
                    data[k] = value
            elif k in {"auto_update", "cloud_access", "notifications_enabled"}:
                data[k] = bool(v)
            elif k in {
                "fetch_budget_mb",
                "fetch_timeout_s",
                "onboarding_version",
                "notification_quiet_start",
                "notification_quiet_end",
            }:
                try:
                    number = int(v)
                except (TypeError, ValueError):
                    continue
                minimum = 0 if k in {
                    "onboarding_version",
                    "notification_quiet_start",
                    "notification_quiet_end",
                } else 1
                maximum = 23 if k in {
                    "notification_quiet_start",
                    "notification_quiet_end",
                } else None
                if number >= minimum and (maximum is None or number <= maximum):
                    data[k] = number
            elif k in {"ai_model", "node_name"}:
                data[k] = str(v or "").strip()[:256]
            elif k == "recap":
                # Nested config block. Merged, not replaced, so a partial patch
                # can't silently wipe SMTP credentials the caller didn't send.
                if not isinstance(v, dict):
                    continue
                merged = dict(data.get(k) or {})
                for field, value in v.items():
                    if field == "smtp_password":
                        merged.pop("smtp_password", None)
                        if value is not None:
                            raw_pw = str(value).strip()[:512]
                            merged["smtp_password_protected"] = (
                                base64.b64encode(os_secret.protect(raw_pw.encode("utf-8"))).decode("ascii")
                                if raw_pw
                                else ""
                            )
                    elif field == "smtp_password_protected":
                        merged.pop("smtp_password", None)
                        merged["smtp_password_protected"] = str(value or "").strip()[:8192]
                    elif field in {"to_address", "from_address", "smtp_host",
                                   "smtp_user", "base_url"}:
                        merged[field] = str(value or "").strip()[:512]
                    elif field in {"smtp_port", "per_source", "send_hour_utc"}:
                        try:
                            merged[field] = int(value)
                        except (TypeError, ValueError):
                            continue
                    elif field in {"use_tls", "enabled"}:
                        merged[field] = bool(value)
                    elif field == "last_sent_unix":
                        try:
                            merged[field] = float(value)
                        except (TypeError, ValueError):
                            continue
                data[k] = merged
        atomic_write_json(self.path, data, indent=2, sort_keys=False)
        return data
