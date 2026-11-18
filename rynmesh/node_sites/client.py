"""Opening side: list peers' sites and carry one HTTP request to the hosting node."""

from __future__ import annotations

import json
import threading
import time
from concurrent.futures import Future, ThreadPoolExecutor, TimeoutError as FutureTimeout
from typing import Any, Callable

from ..drop import codec
from ..drop.codec import DropProtocolError
from ..drop.engine import DropError, _PeerRefused
from .protocol import (
    CAPABILITY,
    LIST_INFO,
    LIST_KIND,
    LIST_PATH,
    MAX_RESPONSE_BODY,
    REQUEST_HEADERS,
    REQUEST_INFO,
    REQUEST_KIND,
    REQUEST_PATH,
    REQUEST_TIMEOUT_S,
    RESPONSE_HEADERS,
    RESPONSE_INFO,
    SiteError,
    b64,
    clean_headers,
    request_path,
    site_id,
    unb64,
)
from .store import SiteStore

AUTH_TTL_S = 60.0
LIST_TTL_S = 60.0
STATUS_TIMEOUT_S = 4.0
MAX_STATUS_PROBES = 8


def _listed_site(value: dict[str, Any]) -> dict[str, Any]:
    site = {"site_id": site_id(value["site_id"]), "title": str(value["title"])[:80],
            "entry": request_path(value["entry"])}
    summary = value.get("summary")
    fields = ("queue_length", "eta_seconds")
    if (isinstance(summary, dict) and summary.get("state") in ("idle", "busy")
            and all(type(summary.get(k)) is int and 0 <= summary[k] <= 2**31 - 1 for k in fields)):
        site["summary"] = {key: summary[key] for key in ("state", *fields)}
    return site


class SiteClient:
    def __init__(self, engine: Any, store: SiteStore, *, clock: Callable[[], float] = time.monotonic) -> None:
        self.engine = engine
        self.store = store
        self.clock = clock
        self._lock = threading.Lock()
        self._auth: dict[tuple[str, str, str], float] = {}
        self._lists: dict[tuple[str, str], tuple[float, list[dict[str, Any]]]] = {}
        self._status_probes: dict[tuple[str, str], Future] = {}
        self.local_fetch = None

    def _status_probe(self, resource: str, peer_id: str, *, fresh: bool):
        # NAT discovery may take a minute. Keep it running for eventual recovery,
        # but never hold the trusted frame's offline indicator behind that work.
        # One worker per peer also prevents overlapping polls queuing fresh probes.
        key = (resource, peer_id)
        with self._lock:
            future = self._status_probes.get(key)
            if future is None:
                if len(self._status_probes) >= MAX_STATUS_PROBES:
                    self._status_probes = {k: f for k, f in self._status_probes.items() if not f.done()}
                if len(self._status_probes) >= MAX_STATUS_PROBES:
                    raise SiteError("site_offline", 502)
                future = Future()
                self._status_probes[key] = future

                def run():
                    try:
                        future.set_result(self._probe(resource, peer_id, fresh=fresh))
                    except Exception as exc:
                        future.set_exception(exc)

                threading.Thread(target=run, name="site-status-probe", daemon=True).start()
        try:
            return future.result(timeout=STATUS_TIMEOUT_S)
        except FutureTimeout:
            raise SiteError("site_offline", 502) from None
        finally:
            if future.done():
                with self._lock:
                    if self._status_probes.get(key) is future:
                        self._status_probes.pop(key, None)

    # ---- helpers ---------------------------------------------------------------
    def _member(self, resource: str, peer_id: str) -> dict[str, Any]:
        try:
            return self.engine.provider_member(resource, peer_id)
        except DropError:
            raise SiteError("site_unknown_peer", 404) from None

    def _probe(self, resource: str, peer_id: str, *, fresh: bool = False):
        try:
            probe = self.engine.provider_probe(resource, peer_id, fresh=fresh)
        except DropError as exc:
            raise SiteError("site_identity_changed" if exc.code == "identity_mismatch" else "site_unknown_peer",
                            403) from None
        if not probe.online:
            raise SiteError("site_offline", 502)
        return probe

    def _authorized(self, resource: str, peer_id: str, sid: str) -> bool:
        key = (resource, peer_id, sid)
        with self._lock:
            if self._auth.get(key, 0.0) > self.clock():
                return True
        allowed = self.engine.provider_allows(resource, self.engine.me, peer_id, CAPABILITY, sid)
        with self._lock:
            if allowed:
                self._auth[key] = self.clock() + AUTH_TTL_S
            else:
                self._auth.pop(key, None)
        return allowed

    def _call(self, resource: str, peer_id: str, path: str, kind: str, payload: dict[str, Any],
              timeout: float) -> dict[str, Any]:
        probe = self._probe(resource, peer_id)
        try:
            return self.engine.signed_call(probe.endpoint, path, kind, peer_id, payload, timeout=timeout,
                                           directory=self.engine.provider_directory(resource))
        except _PeerRefused as exc:
            if exc.status == 429:
                raise SiteError("site_rate_limited", 429) from None
            raise SiteError("site_denied", 403) from None
        except (ConnectionError, OSError, DropProtocolError):
            # A failed call can mean the device just went away; next probe decides.
            self.engine.provider_directory(resource).forget(peer_id)
            raise SiteError("site_offline", 502) from None

    # ---- directory ---------------------------------------------------------------
    def list_sites(self, resource: str, peer_id: str, *, refresh: bool = False) -> list[dict[str, Any]]:
        key = (resource, peer_id)
        with self._lock:
            cached = self._lists.get(key)
            if cached and not refresh and cached[0] > self.clock():
                return list(cached[1])
        member = self._member(resource, peer_id)
        response = self._call(resource, peer_id, LIST_PATH, LIST_KIND,
                              {"provider_resource": resource, "from_pub": self.engine.msg_pub}, timeout=10.0)
        try:
            value = json.loads(codec.open_from(self.engine.msg_priv, member["messaging_pub"],
                                               response["sealed"], LIST_INFO))
            sites = [_listed_site(s) for s in value["sites"][:32]]
        except (DropProtocolError, KeyError, TypeError, ValueError, SiteError):
            raise SiteError("site_bad_response", 502) from None
        with self._lock:
            self.store.cache_remote(resource, peer_id, sites)
            self._lists[key] = (self.clock() + LIST_TTL_S, sites)
        return list(sites)

    def remote(self, *, refresh: bool = False) -> list[dict[str, Any]]:
        """Every provider device with its open sites; offline devices are listed as offline."""
        rows = [row for row in self.engine.provider_members() if row.get("peer_id") != self.engine.me]

        def one(row: dict[str, Any]) -> dict[str, Any]:
            resource, peer_id = row["provider_resource"], row["peer_id"]
            out = {"provider_resource": resource, "peer_id": peer_id,
                   "member_name": str(row.get("member_name") or ""),
                   "device_name": str(row.get("name") or row.get("display_name") or ""),
                   "online": False, "sites": []}
            try:
                self._probe(resource, peer_id, fresh=refresh)
                out["online"] = True
                out["sites"] = self.list_sites(resource, peer_id, refresh=refresh)
            except SiteError as exc:
                if exc.code != "site_offline":
                    out["error"] = exc.code
            return out

        if not rows:
            return []
        with ThreadPoolExecutor(max_workers=min(8, len(rows))) as pool:
            return list(pool.map(one, rows))

    def shared_apps(self, *, refresh: bool = False, members: set[tuple[str, str]] | None = None) -> dict[str, Any]:
        rows = self.engine.provider_members()

        def member(row):
            return row["provider_resource"], row["member_id"]

        if members is None:
            members = {member(row) for row in rows}
        colleagues = [row for row in rows if member(row) in members]

        def lookup(row):
            resource, peer_id = row["provider_resource"], row["peer_id"]
            try:
                self._probe(resource, peer_id, fresh=refresh)
                sites = self.list_sites(resource, peer_id, refresh=refresh)
                online = True
            except SiteError as exc:
                if exc.code != "site_offline":
                    return row, []
                online = False
                sites = self.store.cached_remote(resource, peer_id)
            provider = {"provider_resource": resource, "peer_id": peer_id,
                        "member_name": str(row.get("member_name") or ""),
                        "device_name": str(row.get("name") or row.get("display_name") or ""),
                        "online": online, "last_seen": self.engine.provider_directory(resource).last_seen(peer_id)}
            return row, [(site, {**provider, "summary": site.get("summary")}) for site in sites]

        groups = {}
        sharing = set()
        with ThreadPoolExecutor(max_workers=8) as pool:
            for row, listings in pool.map(lookup, colleagues):
                for site, provider in listings:
                    sharing.add(member(row))
                    sid = site["site_id"]
                    group = groups.setdefault(sid, {"site_id": sid, "title": site["title"], "providers": []})
                    if provider["online"]:
                        group["title"] = site["title"]
                    group["providers"].append(provider)
        for group in groups.values():
            group["providers"].sort(key=lambda p: (not p["online"], p["member_name"], p["device_name"], p["peer_id"]))
        apps = sorted(groups.values(), key=lambda g: (not g["providers"][0]["online"], g["title"], g["site_id"]))
        return {"apps": apps, "colleagues_total": len(members),
                "sharing_total": len(sharing)}

    # ---- opening -------------------------------------------------------------------
    def open(self, resource: Any, peer_id: Any, sid: Any) -> dict[str, Any]:
        if not isinstance(resource, str) or not isinstance(peer_id, str):
            raise SiteError("site_bad_request")
        sid = site_id(sid)
        member = self._member(resource, peer_id)
        if not self._authorized(resource, peer_id, sid):
            raise SiteError("site_denied", 403)
        entry, title = "/", ""
        try:
            for site in self.list_sites(resource, peer_id):
                if site["site_id"] == sid:
                    entry, title = site["entry"], site["title"]
                    break
            else:
                raise SiteError("site_not_found", 404)
        except SiteError as exc:
            if exc.code != "site_offline":
                raise
        key = self.store.key_for(resource, peer_id, sid)
        return {"key": key, "entry": entry, "title": title, "peer_id": peer_id, "provider_resource": resource,
                "member_name": str(member.get("member_name") or ""),
                "device_name": str(member.get("name") or member.get("display_name") or "")}

    def status(self, key: str, *, fresh: bool = False) -> dict[str, Any]:
        row = self.store.opened(key)
        if row is None:
            raise SiteError("site_not_found", 404)
        member = self._member(row["resource"], row["peer_id"])
        online, route = False, None
        try:
            probe = self._status_probe(row["resource"], row["peer_id"], fresh=fresh)
            online, route = True, probe.route
        except SiteError as exc:
            if exc.code != "site_offline":
                raise
        last_seen = self.engine.provider_directory(row["resource"]).last_seen(row["peer_id"])
        return {"key": key, "site_id": row["site_id"], "peer_id": row["peer_id"], "online": online, "route": route,
                "member_name": str(member.get("member_name") or ""),
                "device_name": str(member.get("name") or member.get("display_name") or ""),
                "last_seen": last_seen}

    def fetch(self, key: str, method: str, path: str, headers: list[list[str]], body: bytes
              ) -> tuple[int, list[list[str]], bytes]:
        row = self.store.opened(key)
        if row is None:
            raise SiteError("site_not_found", 404)
        resource, peer_id, sid = row["resource"], row["peer_id"], row["site_id"]
        if resource == "local" and peer_id == self.engine.me:
            if self.local_fetch is None:
                raise SiteError("site_denied", 403)
            return self.local_fetch(sid, method, request_path(path), body)
        member = self._member(resource, peer_id)
        if not self._authorized(resource, peer_id, sid):
            raise SiteError("site_denied", 403)
        secret = json.dumps({"method": method, "path": request_path(path),
                             "headers": clean_headers(headers, REQUEST_HEADERS), "body": b64(body)}).encode("utf-8")
        payload = {"provider_resource": resource, "site_id": sid, "from_pub": self.engine.msg_pub,
                   "sealed": codec.seal_for(self.engine.msg_priv, member["messaging_pub"], secret, REQUEST_INFO)}
        response = self._call(resource, peer_id, REQUEST_PATH, REQUEST_KIND, payload,
                              timeout=REQUEST_TIMEOUT_S + 10)
        try:
            value = json.loads(codec.open_from(self.engine.msg_priv, member["messaging_pub"],
                                               response["sealed"], RESPONSE_INFO))
            status = value["status"]
            if type(status) is not int or not 100 <= status <= 599:
                raise ValueError
            return status, clean_headers(value["headers"], RESPONSE_HEADERS), unb64(value["body"], MAX_RESPONSE_BODY)
        except (DropProtocolError, KeyError, TypeError, ValueError):
            raise SiteError("site_bad_response", 502) from None
