"""Local state: the sites this node hosts and the remote sites it has opened."""

from __future__ import annotations

import secrets
import threading
import time
from pathlib import Path
from typing import Any

from ..atomic_io import atomic_write_json, read_json
from .protocol import MAX_SITES, SITE_KEY, SiteError, site_id

AUDIENCE_MODES = ("all", "members")
MAX_TITLE = 80
MAX_MEMBERS = 500
MAX_OPENED = 512


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _title(value: Any) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > MAX_TITLE \
            or any(ord(c) < 32 or ord(c) == 127 for c in value):
        raise SiteError("site_bad_title")
    return value.strip()


def _port(value: Any) -> int:
    if type(value) is not int or not 1024 <= value <= 65535:
        raise SiteError("site_bad_port")
    return value


def _entry(value: Any) -> str:
    if not isinstance(value, str) or not value.startswith("/") or value.startswith("//") or len(value) > 512 \
            or any(ord(c) < 33 or ord(c) == 127 or c in "\\#" for c in value):
        raise SiteError("site_bad_entry")
    return value


def _audience(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict) or value.get("mode") not in AUDIENCE_MODES or set(value) - {"mode", "members"}:
        raise SiteError("site_bad_audience")
    members = value.get("members", [])
    if (not isinstance(members, list) or len(members) > MAX_MEMBERS
            or any(not isinstance(m, str) or not m or len(m) > 128 for m in members)):
        raise SiteError("site_bad_audience")
    return {"mode": value["mode"], "members": sorted(set(members))}


class SiteStore:
    """``<home>/node-sites/{sites,opened}.json``, written atomically under one lock."""

    def __init__(self, home: Path) -> None:
        self.root = Path(home) / "node-sites"
        self.root.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()

    # ---- hosted sites -------------------------------------------------------
    def _sites(self) -> dict[str, Any]:
        data = read_json(self.root / "sites.json", default={"version": 1, "sites": {}})
        return data if isinstance(data, dict) and isinstance(data.get("sites"), dict) else {"version": 1, "sites": {}}

    def sites(self) -> dict[str, dict[str, Any]]:
        with self._lock:
            return dict(self._sites()["sites"])

    def site(self, value: str) -> dict[str, Any] | None:
        return self.sites().get(value)

    @staticmethod
    def public(sid: str, row: dict[str, Any]) -> dict[str, Any]:
        """Owner view; never includes the backend token."""
        return {"site_id": sid, "title": row["title"], "port": row["port"], "entry": row["entry"],
                "enabled": bool(row["enabled"]), "audience": row["audience"], "updated_at": row["updated_at"]}

    def register(self, *, sid: Any, title: Any, port: Any, entry: Any = "/") -> tuple[dict[str, Any], str]:
        """Add or replace a loopback site. New registrations start closed; a replace keeps exposure."""
        sid = site_id(sid)
        row = {"title": _title(title), "port": _port(port), "entry": _entry(entry)}
        with self._lock:
            data = self._sites()
            current = data["sites"].get(sid)
            if current is None and len(data["sites"]) >= MAX_SITES:
                raise SiteError("site_limit", 409)
            token = secrets.token_urlsafe(32)
            data["sites"][sid] = {**row, "token": token,
                                  "enabled": bool(current and current.get("enabled")),
                                  "audience": (current or {}).get("audience") or {"mode": "all", "members": []},
                                  "created_at": (current or {}).get("created_at") or _now(), "updated_at": _now()}
            atomic_write_json(self.root / "sites.json", data)
            return self.public(sid, data["sites"][sid]), token

    def update(self, sid: Any, changes: Any) -> dict[str, Any]:
        sid = site_id(sid)
        if not isinstance(changes, dict) or not changes or set(changes) - {"enabled", "audience", "title"}:
            raise SiteError("site_bad_request")
        with self._lock:
            data = self._sites()
            row = data["sites"].get(sid)
            if row is None:
                raise SiteError("site_not_found", 404)
            if "enabled" in changes:
                if type(changes["enabled"]) is not bool:
                    raise SiteError("site_bad_request")
                row["enabled"] = changes["enabled"]
            if "audience" in changes:
                row["audience"] = _audience(changes["audience"])
            if "title" in changes:
                row["title"] = _title(changes["title"])
            row["updated_at"] = _now()
            atomic_write_json(self.root / "sites.json", data)
            return self.public(sid, row)

    def remove(self, sid: Any) -> bool:
        sid = site_id(sid)
        with self._lock:
            data = self._sites()
            removed = data["sites"].pop(sid, None) is not None
            if removed:
                atomic_write_json(self.root / "sites.json", data)
            return removed

    # ---- remote sites opened here ---------------------------------------------
    def _opened(self) -> dict[str, Any]:
        data = read_json(self.root / "opened.json", default={"version": 1, "keys": {}})
        return data if isinstance(data, dict) and isinstance(data.get("keys"), dict) else {"version": 1, "keys": {}}

    def key_for(self, resource: str, peer_id: str, sid: str) -> str:
        """A stable, unguessable origin key per (provider, device, site); it is the capability."""
        with self._lock:
            data = self._opened()
            for key, row in data["keys"].items():
                if (row.get("resource"), row.get("peer_id"), row.get("site_id")) == (resource, peer_id, sid):
                    return key
            if len(data["keys"]) >= MAX_OPENED:
                oldest = min(data["keys"], key=lambda k: data["keys"][k].get("created_at", ""))
                data["keys"].pop(oldest)
            key = secrets.token_hex(16)
            data["keys"][key] = {"resource": resource, "peer_id": peer_id, "site_id": sid, "created_at": _now()}
            atomic_write_json(self.root / "opened.json", data)
            return key

    def opened(self, key: str) -> dict[str, Any] | None:
        if not isinstance(key, str) or not SITE_KEY.fullmatch(key):
            return None
        with self._lock:
            row = self._opened()["keys"].get(key)
            return dict(row) if isinstance(row, dict) else None

    def gateway_port(self) -> int | None:
        with self._lock:
            value = self._opened().get("gateway_port")
            return value if type(value) is int and 1024 <= value <= 65535 else None

    def set_gateway_port(self, port: int) -> None:
        with self._lock:
            data = self._opened()
            if data.get("gateway_port") != port:
                data["gateway_port"] = port
                atomic_write_json(self.root / "opened.json", data)
