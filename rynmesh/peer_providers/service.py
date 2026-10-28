"""Plugin-sourced peer membership, with online authorization on every operation."""
from __future__ import annotations

import asyncio
import base64
import copy
import inspect
import re
import threading
import time
import uuid
from datetime import datetime

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from ..services import peer_box
from . import validation as v
from .oauth import OAuthClient, ProviderHttpError, request_json
from .storage import SealedStore
from .validation import PeerProviderError


class PeerProviderService:
    def __init__(self, home, store, messaging_key, *, transport=None, clock=time.time,
                 callback_factory=None, model_status=None, endpoints=None, extension_source=None,
                 browser_open=None, mcp_token_source=None, mcp_workspace_source=None):
        self.store, self.messaging_key, self.clock = store, messaging_key, clock
        self.storage = SealedStore(home, messaging_key)
        self.oauth = OAuthClient(transport or request_json, clock=clock, callback_factory=callback_factory)
        self.model_status = model_status or (lambda: [])
        self.endpoints = endpoints or (lambda: [])
        self.extension_source, self.browser_open = extension_source, browser_open
        self.mcp_token_source = mcp_token_source
        self.mcp_workspace_source = mcp_workspace_source
        self._lock = threading.RLock()
        self._rows = self.storage.read()
        self._cache, self._pending, self._requests = {}, {}, {}
        self._companion_failed: set[tuple[str, str]] = set()

    def _save(self):
        self.storage.write(self._rows)

    def _clear(self, plugin_id):
        self._cache.pop(plugin_id, None)
        pending = self._pending.pop(plugin_id, None)
        if pending:
            pending["close"]()

    def configure(self, plugin_id, declaration, *, instance_id=None, enabled=True, revision=None):
        if not isinstance(plugin_id, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}", plugin_id) or type(enabled) is not bool:
            v.fail("peer_provider_invalid_configuration")
        if revision is not None and (type(revision) is not int or revision < 1):
            v.fail("peer_provider_invalid_configuration")
        config = v.manifest(declaration)
        with self._lock:
            old = self._rows.get(plugin_id)
            instance = str(instance_id or (old or {}).get("instance_id") or uuid.uuid4().hex)
            if old and old["config"] == config and old["instance_id"] == instance and old["enabled"] == enabled and old.get("revision") == revision:
                return self._public(plugin_id, old)
            if any(
                k != plugin_id
                and r["config"]["url"] == config["url"]
                and r["enabled"]
                for k, r in self._rows.items()
            ) and enabled:
                v.fail("peer_provider_duplicate_resource")
            self._clear(plugin_id)
            new_row = {"config": config, "instance_id": instance, "enabled": enabled, "revision": revision, "epoch": uuid.uuid4().hex, "published": {}}
            self._rows[plugin_id] = new_row
            self._save()
            return self._public(plugin_id, self._rows[plugin_id])

    def sync_from_extensions(self):
        if self.extension_source is None:
            return
        try:
            records = self.extension_source()
            if not isinstance(records, list):
                raise ValueError
            seen = set()
            for row in records:
                plugin_id = row["plugin_id"]
                self.configure(plugin_id, row["peer_provider"], instance_id=row["instance_id"], enabled=row["enabled"] is True, revision=row.get("revision"))
                seen.add(plugin_id)
            with self._lock:
                removed = set(self._rows) - seen
                for plugin_id in removed:
                    self._clear(plugin_id)
                    self._rows.pop(plugin_id, None)
                if removed:
                    self._save()
        except Exception:
            # Unreadable ownership/configuration cannot preserve usable grants.
            with self._lock:
                for plugin_id, row in self._rows.items():
                    self._clear(plugin_id)
                    row.pop("session", None)
                    row.pop("binding", None)
                    row["published"] = {}
                    row["epoch"] = uuid.uuid4().hex
                self._save()
            raise PeerProviderError("peer_provider_configuration_unavailable") from None

    def _resolve_plugin_id(self, plugin_id):
        if plugin_id in self._rows:
            return plugin_id
        return plugin_id

    def _get(self, plugin_id, *, session=False):
        self.sync_from_extensions()
        with self._lock:
            plugin_id = self._resolve_plugin_id(plugin_id)
            row = self._rows.get(plugin_id)
            if not row or not row["enabled"]:
                v.fail("peer_provider_unavailable")
            if session and not row.get("session"):
                v.fail("peer_provider_login_required")
            return copy.deepcopy(row)

    def _resource(self, resource):
        # Never normalize an incoming hint into a new URL or discover it.
        self.sync_from_extensions()
        with self._lock:
            matches = [p for p, r in self._rows.items() if r["enabled"] and r["config"]["url"] == resource and r.get("session")]
        if not matches or len(matches) > 1:
            v.fail("peer_provider_access_denied")
        return matches[0]

    def _current(self, plugin_id, epoch):
        self.sync_from_extensions()
        plugin_id = self._resolve_plugin_id(plugin_id)
        row = self._rows.get(plugin_id)
        if not row or not row["enabled"] or row["epoch"] != epoch:
            v.fail("peer_provider_access_denied")
        return row

    def _public(self, plugin_id, row):
        pending = self._pending.get(plugin_id)
        if pending and pending["flow"]["expires_at"] <= self.clock():
            self._clear(plugin_id)
            pending = None
        return {"plugin_id": plugin_id, "resource": row["config"]["url"], "enabled": row["enabled"],
                "status": "pending" if pending else "connected" if row.get("session") and row["enabled"] else "signed_out",
                "bound": bool(row.get("binding") and row.get("session") and row["enabled"]),
                "selected_service_ids": sorted(row.get("published", {}))}

    def status(self, plugin_id=None):
        self.sync_from_extensions()
        with self._lock:
            if plugin_id is not None:
                resolved = self._resolve_plugin_id(plugin_id)
                if resolved not in self._rows:
                    v.fail("peer_provider_unavailable")
                return self._public(plugin_id, self._rows[resolved])
            return [self._public(p, r) for p, r in self._rows.items()]

    async def start_login(self, plugin_id):
        row = self._get(plugin_id)
        with self._lock:
            if plugin_id in self._pending:
                v.fail("peer_provider_login_pending")
        if row.get("session"):
            result = await self.logout(plugin_id)
            if not result["revocation_confirmed"]:
                v.fail("peer_provider_revocation_unconfirmed")
            row = self._get(plugin_id)
        async def completed(params):
            await self.finish_login(plugin_id, params)
        flow, browser_url, close = await self.oauth.start(row["config"], completed)
        with self._lock:
            try:
                current = self._current(plugin_id, row["epoch"])
                if plugin_id in self._pending:
                    v.fail("peer_provider_login_pending")
                # Relogin starts a fresh grant: no prior explicit publication is
                # silently inherited by another authenticated subject.
                current.pop("session", None)
                current.pop("binding", None)
                current["published"] = {}
                current["epoch"] = uuid.uuid4().hex
                self._cache.pop(plugin_id, None)
                self._pending[plugin_id] = {"flow": flow, "epoch": current["epoch"], "close": close}
                self._save()
            except Exception:
                close()
                raise
        if self.browser_open:
            try:
                result = self.browser_open(browser_url)
                if inspect.isawaitable(result):
                    await result
            except Exception:
                pass  # The owner UI still receives the public authorization URL.
        return {"status": "pending", "browser_url": browser_url}

    async def finish_login(self, plugin_id, params):
        self._get(plugin_id)
        with self._lock:
            pending = self._pending.get(plugin_id)
            if not pending:
                v.fail("peer_provider_callback_invalid")
            try:
                self.oauth.validate_callback(pending["flow"], params)
            except PeerProviderError as exc:
                if str(exc) in {"peer_provider_login_denied", "peer_provider_login_expired"}:
                    self._clear(plugin_id)
                raise
            self._pending.pop(plugin_id)
            pending["close"]()
        session = await self.oauth.exchange(pending["flow"], params)
        with self._lock:
            row = self._current(plugin_id, pending["epoch"])
            row["session"] = session
            row["epoch"] = uuid.uuid4().hex
            self._save()
        await self.bind(plugin_id)
        return self.status(plugin_id)

    async def logout(self, plugin_id):
        self.sync_from_extensions()
        with self._lock:
            row = self._rows.get(plugin_id)
            if not row:
                v.fail("peer_provider_unavailable")
            session = row.pop("session", None)
            row.pop("binding", None)
            row["published"] = {}
            row["epoch"] = uuid.uuid4().hex
            self._clear(plugin_id)
            self._save()
        confirmed = await self.oauth.revoke(session) if session else True
        return {**self.status(plugin_id), "revocation_confirmed": confirmed}

    async def remove(self, plugin_id):
        result = await self.logout(plugin_id)
        with self._lock:
            self._rows.pop(plugin_id, None)
            self._save()
        return result

    def _invalidate(self, plugin_id, epoch):
        with self._lock:
            row = self._rows.get(plugin_id)
            self._cache.pop(plugin_id, None)
            if row and row["epoch"] == epoch:
                row.pop("session", None)
                row.pop("binding", None)
                row["published"] = {}
                row["epoch"] = uuid.uuid4().hex
                self._save()

    async def _request(self, plugin_id, method, suffix, body=None):
        # Calls can originate in independent worker event loops. A process lock
        # serializes refresh rotation, and epoch checks reject late old results.
        with self._lock:
            lock = self._requests.setdefault(plugin_id, threading.Lock())
        while not lock.acquire(blocking=False):
            await asyncio.sleep(0.01)
        try:
            row = self._get(plugin_id, session=True)
            session, refreshed = row["session"], False
            async def refresh():
                try:
                    new = await self.oauth.refresh(session)
                    with self._lock:
                        self._current(plugin_id, row["epoch"])["session"] = new
                        self._save()
                    return new
                except PeerProviderError:
                    self._invalidate(plugin_id, row["epoch"])
                    raise PeerProviderError("peer_provider_login_required") from None
            if session["expires_at"] <= self.clock() + 30:
                session, refreshed = await refresh(), True
            for attempt in range(2):
                try:
                    result = await self.oauth.call(method, row["config"]["url"] + suffix,
                        headers={"Authorization": "Bearer " + session["access_token"]}, **({"json": body} if body is not None else {}))
                    with self._lock:
                        self._current(plugin_id, row["epoch"])
                    return result, row
                except ProviderHttpError as exc:
                    self._cache.pop(plugin_id, None)
                    if exc.status == 401 and not refreshed and attempt == 0:
                        session, refreshed = await refresh(), True
                        continue
                    if exc.status == 401:
                        self._invalidate(plugin_id, row["epoch"])
                    raise PeerProviderError("peer_provider_access_denied") from None
                except PeerProviderError:
                    self._cache.pop(plugin_id, None)
                    raise
            v.fail("peer_provider_access_denied")
        finally:
            lock.release()

    async def bind(self, plugin_id, name=None, endpoints=None):
        row = self._get(plugin_id, session=True)
        fields = {"peer_id": v.key(self.store.peer_id), "messaging_pub": peer_box.public_key_b64(self.messaging_key),
                  "name": v.text(name if name is not None else getattr(self.store, "node_name", "This device"), 80),
                  "endpoints": v.endpoints(endpoints if endpoints is not None else self.endpoints())}
        challenge, _ = await self._request(plugin_id, "POST", "/challenge", fields)
        challenge_id = v.text(challenge.get("challenge_id"))
        message = v.text(challenge.get("message"), 16384)
        try:
            expiry = challenge["expires_at"]
            expires_at = float(expiry) if type(expiry) in (int, float) else datetime.fromisoformat(expiry.replace("Z", "+00:00")).timestamp()
            if not self.clock() < expires_at <= self.clock() + 600:
                v.fail()
            statement = __import__("json").loads(message)
            if not isinstance(statement, dict) or statement.get("version") != "ryn.peer-bind.v1" or statement.get("issuer") != row["session"]["issuer"] or statement.get("resource") != row["config"]["url"]:
                v.fail()
            if statement.get("challenge_id") != challenge_id or statement.get("expires_at") != expiry:
                v.fail()
            for name in ("grant_id", "member_id", "nonce"):
                v.text(statement.get(name))
            if any(statement.get(k) != value for k, value in fields.items()):
                v.fail()
        except PeerProviderError:
            raise
        except Exception:
            v.fail()
        signature = Ed25519PrivateKey.from_private_bytes(self.store.private_key_bytes).sign(message.encode("utf-8"))
        result, current = await self._request(plugin_id, "POST", "/bind", {"challenge_id": challenge_id, "signature": base64.b64encode(signature).decode()})
        safe = {"peer_id": v.key(result.get("peer_id")), "member_id": v.text(result.get("member_id")), "revision": v.revision(result.get("revision"))}
        if safe["peer_id"] != fields["peer_id"] or current["epoch"] != row["epoch"]:
            v.fail()
        with self._lock:
            self._current(plugin_id, row["epoch"])["binding"] = safe
            self._save()
        return {"bound": True}

    async def directory(self, plugin_id):
        value, row = await self._request(plugin_id, "GET", "/directory")
        try:
            safe = v.directory(value, issuer=row["session"]["issuer"], resource=row["config"]["url"])
        except PeerProviderError:
            with self._lock:
                self._cache.pop(plugin_id, None)
            raise
        for model in safe["models"]:
            model.update(provider_resource=safe["resource"], plugin_id=plugin_id, binding_verified=True,
                         consumer_peer_id=self.store.peer_id, external_permission={"resource": safe["resource"]})
        with self._lock:
            self._current(plugin_id, row["epoch"])
            self._cache[plugin_id] = {"expires_at": self.clock() + 60, "epoch": row["epoch"], "directory": safe}
        return copy.deepcopy(safe)

    def _cached(self, plugin_id=None):
        self.sync_from_extensions()
        with self._lock:
            return [(p, copy.deepcopy(c["directory"])) for p, c in self._cache.items()
                    if (plugin_id is None or plugin_id == p) and c["expires_at"] > self.clock()
                    and p in self._rows and self._rows[p]["enabled"] and self._rows[p].get("session")
                    and self._rows[p]["epoch"] == c["epoch"]]

    def cached_models(self, plugin_id=None):
        return [m for _, d in self._cached(plugin_id) for m in d["models"]]

    def cached_peers(self, plugin_id=None):
        return [{**device, "member_id": member["id"], "subject_id": member["id"], "member_name": member["name"],
                 "display_name": device["name"], "endpoint": next(iter(device["endpoints"]), ""), "plugin_id": p, "provider_resource": d["resource"]}
                for p, d in self._cached(plugin_id) for member in d["members"] for device in member["devices"]]

    async def list_models(self, plugin_id=None):
        await self.refresh_directories(plugin_id)
        return self.cached_models(plugin_id)

    async def list_peers(self, plugin_id=None):
        await self.refresh_directories(plugin_id)
        return self.cached_peers(plugin_id)

    async def refresh_directories(self, plugin_id=None):
        for status in self.status():
            if plugin_id is None or status["plugin_id"] == plugin_id:
                if not status["bound"] and self.mcp_token_source is not None:
                    try:
                        await self.auto_sync(status["plugin_id"])
                        status = self.status(status["plugin_id"])
                    except Exception:
                        pass
                if status["bound"]:
                    await self.directory(status["plugin_id"])

    async def auto_sync(self, plugin_id):
        row = self._get(plugin_id)
        if not row.get("session") and self.mcp_token_source is not None:
            mcp_token = self.mcp_token_source(plugin_id)
            if mcp_token and (plugin_id, mcp_token) not in self._companion_failed:
                try:
                    session = await self.oauth.exchange_companion(row["config"], mcp_token)
                except Exception:
                    self._companion_failed.add((plugin_id, mcp_token))
                    raise
                with self._lock:
                    current = self._current(plugin_id, row["epoch"])
                    current["session"] = session
                    current["epoch"] = uuid.uuid4().hex
                    self._save()
                row = self._get(plugin_id)
        if row.get("session") and not row.get("binding"):
            await self.bind(plugin_id)
            row = self._get(plugin_id)
        if row.get("binding"):
            try:
                await self.directory(plugin_id)
            except Exception:
                pass
        return self.status(plugin_id)

    async def workspace(self, plugin_id):
        status = self.status(plugin_id)
        dir_data = None
        ws_data = None
        if not status.get("bound") and self.mcp_workspace_source is not None:
            try:
                fallback_ws = await asyncio.to_thread(self.mcp_workspace_source, plugin_id)
                if isinstance(fallback_ws, dict) and isinstance(fallback_ws.get("colleagues"), list):
                    ws_data = fallback_ws
                    status = {**status, "status": "connected", "bound": True}
            except Exception:
                pass
        if ws_data is None and not status.get("bound"):
            try:
                await self.auto_sync(plugin_id)
                status = self.status(plugin_id)
            except Exception:
                pass
        if status.get("bound") and ws_data is None:
            try:
                dir_data = await self.directory(plugin_id)
            except Exception:
                pass
            try:
                ws_raw, _ = await self._request(plugin_id, "GET", "/workspace")
                if isinstance(ws_raw, dict) and ws_raw.get("version") == "ryn.workspace.v1":
                    ws_data = ws_raw
            except Exception:
                ws_data = None
        if ws_data is None and self.mcp_workspace_source is not None:
            try:
                fallback_ws = await asyncio.to_thread(self.mcp_workspace_source, plugin_id)
                if isinstance(fallback_ws, dict) and isinstance(fallback_ws.get("colleagues"), list):
                    ws_data = fallback_ws
                    status = {**status, "status": "connected", "bound": True}
            except Exception:
                pass
        if not status.get("bound") and ws_data is None:
            return {
                **status,
                "current_member": None,
                "colleagues": [],
                "departments": [],
                "models": [],
                "directory": None,
            }
        local_peer_id = str(getattr(self.store, "peer_id", "") or "")
        current_member = (ws_data or {}).get("current_member")
        dir_devs_by_uid: dict[str, list[dict]] = {}
        if isinstance(dir_data, dict) and isinstance(dir_data.get("members"), list):
            for member in dir_data["members"]:
                if not isinstance(member, dict):
                    continue
                m_uid = str(member.get("id") or "").strip().lower()
                if not m_uid:
                    continue
                m_devs = [
                    d
                    for d in (member.get("devices") or [])
                    if isinstance(d, dict)
                    and d.get("peer_id")
                    and d.get("peer_id") != local_peer_id
                ]
                if m_devs:
                    dir_devs_by_uid[m_uid] = m_devs
        colleagues = []
        if ws_data and isinstance(ws_data.get("colleagues"), list):
            for item in ws_data["colleagues"]:
                if not isinstance(item, dict):
                    continue
                item_uid = str(item.get("id") or "").strip()
                raw_devs = [
                    d
                    for d in (item.get("devices") or [])
                    if isinstance(d, dict)
                    and d.get("peer_id")
                    and d.get("peer_id") != local_peer_id
                ]
                merged_devs = list(raw_devs)
                seen_pids = {str(d.get("peer_id")) for d in merged_devs}
                for d in dir_devs_by_uid.get(item_uid.lower(), []):
                    pid = str(d.get("peer_id") or "")
                    if pid and pid not in seen_pids:
                        merged_devs.append(d)
                        seen_pids.add(pid)
                col_entry = {
                    "id": item_uid,
                    "name": str(item.get("name") or "企业微信同事"),
                    "alias": str(item.get("alias") or ""),
                    "department": str(item.get("department") or "业务协同部门"),
                    "position": str(item.get("position") or "认证员工"),
                    "role": str(item.get("role") or "member"),
                    "online": bool(merged_devs) or bool(item.get("online")),
                    "devices": merged_devs,
                }
                if isinstance(item.get("department_ids"), list):
                    col_entry["department_ids"] = [str(x) for x in item["department_ids"] if x]
                if isinstance(item.get("department_names"), list):
                    col_entry["department_names"] = [str(x) for x in item["department_names"] if x]
                if "is_leader" in item:
                    col_entry["is_leader"] = bool(item.get("is_leader"))
                if item.get("location"):
                    col_entry["location"] = str(item.get("location"))
                if item.get("badge"):
                    col_entry["badge"] = str(item.get("badge"))
                if item.get("email"):
                    col_entry["email"] = str(item.get("email"))
                if item.get("emp_status"):
                    col_entry["emp_status"] = str(item.get("emp_status"))
                colleagues.append(col_entry)
        elif dir_data and isinstance(dir_data.get("members"), list):
            for member in dir_data["members"]:
                devs = [d for d in (member.get("devices") or []) if isinstance(d, dict) and d.get("peer_id") != local_peer_id]
                if any(d.get("peer_id") == local_peer_id for d in (member.get("devices") or [])):
                    if not current_member:
                        current_member = {"id": member["id"], "name": member["name"], "alias": "", "department": "财务部", "position": "认证员工", "role": "finance_admin", "can_publish_models": True}
                    if not devs:
                        continue
                colleagues.append({
                    "id": member["id"],
                    "name": member["name"] or "企业微信同事",
                    "alias": "",
                    "department": "企业微信实名认证",
                    "position": "组织成员",
                    "role": "member",
                    "online": len(devs) > 0,
                    "devices": devs,
                })
        return {
            **status,
            "current_member": current_member,
            "colleagues": colleagues,
            "departments": (ws_data or {}).get("departments") or [],
            "models": (dir_data or {}).get("models", []),
            "directory": dir_data,
        }

    def available_models(self):
        try:
            records = self.model_status()
            if not isinstance(records, list) or len(records) > 500:
                v.fail()
            return [v.model_record(record) for record in records]
        except Exception:
            raise PeerProviderError("peer_provider_models_unavailable") from None

    async def publish(self, plugin_id, service_ids):
        if not isinstance(service_ids, list) or len(service_ids) > 32 or any(not isinstance(s, str) for s in service_ids) or len(set(service_ids)) != len(service_ids):
            v.fail("peer_provider_invalid_selection")
        row = self._get(plugin_id, session=True)
        if not row.get("binding"):
            v.fail("peer_provider_binding_required")
        candidates = {m["service"]["package_id"]: m for m in self.available_models()}
        if any(s not in candidates for s in service_ids):
            v.fail("peer_provider_invalid_selection")
        models = [candidates[s] for s in service_ids]
        if len(__import__("json").dumps({"models": models}).encode()) > 65536:
            v.fail("peer_provider_invalid_selection")
        with self._lock:
            # Owner deselection takes effect locally even if the remote update
            # subsequently fails. A late earlier response cannot restore it.
            current = self._current(plugin_id, row["epoch"])
            current["published"] = {}
            publication_revision = uuid.uuid4().hex
            current["publication_revision"] = publication_revision
            self._save()
        await self._request(plugin_id, "PUT", "/models", {"models": models})
        # Re-read generations after the request; a concurrent source change
        # cannot turn an old directory publication into a local permission.
        current = {m["service"]["package_id"]: m for m in self.available_models()}
        if any(current.get(s, {}).get("model_binding") != candidates[s]["model_binding"] for s in service_ids):
            v.fail("peer_provider_model_changed")
        with self._lock:
            current = self._current(plugin_id, row["epoch"])
            if current.get("publication_revision") != publication_revision:
                return self._public(plugin_id, current)
            current["published"] = {s: candidates[s]["model_binding"] for s in service_ids}
            self._cache.pop(plugin_id, None)
            self._save()
        return self.status(plugin_id)

    def locally_published(self, resource, service_id, current_binding):
        try:
            plugin_id = self._resource(resource)
            row = self._get(plugin_id, session=True)
            return bool(row.get("binding") and current_binding and row.get("published", {}).get(service_id) == current_binding)
        except PeerProviderError:
            return False

    async def authorize(self, resource, from_peer_id, to_peer_id, capability, service_id=None):
        plugin_id = self._resource(resource)
        if capability not in {"model.invoke", "message.text"} or self.store.peer_id not in (from_peer_id, to_peer_id):
            v.fail("peer_provider_access_denied")
        request = {"from_peer_id": v.key(from_peer_id), "to_peer_id": v.key(to_peer_id), "capability": capability}
        if capability == "model.invoke":
            request["service_id"] = v.text(service_id)
        elif service_id is not None:
            v.fail("peer_provider_access_denied")
        row = self._get(plugin_id, session=True)
        if not row.get("binding"):
            v.fail("peer_provider_access_denied")
        value, _ = await self._request(plugin_id, "POST", "/authorize", request)
        if value.get("allowed") is not True or value.get("resource") != resource or value.get("issuer") != row["session"]["issuer"] or any(value.get(k) != val for k, val in request.items()):
            with self._lock:
                self._cache.pop(plugin_id, None)
            v.fail("peer_provider_access_denied")
        if capability == "message.text" and "service_id" in value:
            v.fail("peer_provider_access_denied")
        return {"allowed": True, "issuer": row["session"]["issuer"], "resource": resource, **request, "revision": v.revision(value.get("revision"))}

    def sync_authorize(self, resource, from_peer_id, to_peer_id, capability, service_id=None):
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return asyncio.run(self.authorize(resource, from_peer_id, to_peer_id, capability, service_id))
        v.fail("peer_provider_worker_required")

    def close(self):
        with self._lock:
            for plugin_id in list(self._pending):
                self._clear(plugin_id)
