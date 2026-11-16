"""Bounded signed challenge catalog for personal-space model destinations."""
from __future__ import annotations

import copy
import hashlib
import json
import re
import threading
import time
import uuid
from concurrent.futures import Future, TimeoutError
from datetime import datetime, timezone

from ..services import peer_box
from .peer_binding import valid
from .adapters import AdapterError
from .task_protocol import TaskProtocolError, open_task, seal_task

PATH = "/api/peer/llm/space-catalog"
MAX_BYTES = 64 * 1024
FRESH_SECONDS = 120
ENVELOPE_TTL_SECONDS = 300
_CLOSED_CODE_RE = re.compile(r"space_catalog_[a-z0-9_]{1,80}")


def _service_sort_key(row: dict) -> tuple[str, str]:
    service = row.get("service") if isinstance(row, dict) else None
    if not isinstance(service, dict):
        return ("", "")
    name = str(service.get("model_alias") or service.get("name") or service.get("package_id") or "")
    pkg = str(service.get("package_id") or "")
    return (name, pkg)


def _classify_refresh_error(exc: BaseException, *, transport_failed: bool) -> str:
    if transport_failed:
        detail_code = getattr(exc, "detail_code", None)
        if (
            isinstance(detail_code, str)
            and _CLOSED_CODE_RE.fullmatch(detail_code)
            and detail_code != "space_catalog_transport_unavailable"
        ):
            return detail_code
        text = str(exc)
        if isinstance(exc, TaskProtocolError) and _CLOSED_CODE_RE.fullmatch(text):
            if text != "space_catalog_transport_unavailable":
                return text
        if text.startswith("peer_http_error:"):
            tail = text.rsplit(":", 1)[-1]
            if _CLOSED_CODE_RE.fullmatch(tail) and tail != "space_catalog_transport_unavailable":
                return tail
        return "space_catalog_unreachable"
    if isinstance(exc, TaskProtocolError):
        text = str(exc)
        if _CLOSED_CODE_RE.fullmatch(text):
            return text
        if text.startswith("task envelope expired"):
            return "space_catalog_clock_skew"
    return "space_catalog_response_invalid"


class SpaceModelCatalog:
    def __init__(self, *, store, messaging_key, providers, exchange, clock=time.time, network_id=lambda: "rynmesh-main"):
        self.store, self.key, self.providers, self.exchange, self.clock = store, messaging_key, providers, exchange, clock
        self.network_id = network_id
        self.lock = threading.RLock()
        self.slots = threading.BoundedSemaphore(2)
        self.outgoing_slots = threading.BoundedSemaphore(2)
        self.last_requests, self.cache, self.tickets = {}, {}, {}
        self.refreshes = {}
        self.last_errors = {}
        self.skipped_rows = 0

    def _local_generation(self, peer):
        permission = self.store.personal_space.model_permission(peer)
        return hashlib.sha256(json.dumps(permission, sort_keys=True).encode()).hexdigest()

    def _seal(self, body, task_id, kind, recipient, recipient_key):
        expiry = datetime.fromtimestamp(self.clock() + ENVELOPE_TTL_SECONDS, timezone.utc).isoformat()
        return seal_task(body=body, task_id=task_id, kind=kind, sender_peer_id=self.store.peer_id,
            recipient_peer_id=recipient, sender_signing_key=self.store.private_key_bytes,
            recipient_messaging_pub=recipient_key, expires_at=expiry).to_dict()

    def respond(self, wire):
        if len(json.dumps(wire).encode()) > 8192 or not self.slots.acquire(blocking=False):
            raise TaskProtocolError("space_catalog_request_rejected")
        try:
            outer, request = open_task(wire, recipient_peer_id=self.store.peer_id,
                recipient_messaging_key=self.key, expected_kind="space_model_catalog_request")
            if (set(request) != {"task_id", "reply_messaging_pub"} or not isinstance(request["task_id"], str) or request["task_id"] != outer["task_id"]
                    or len(request["task_id"]) > 80 or not isinstance(request["reply_messaging_pub"], str)):
                raise TaskProtocolError("space_catalog_request_invalid")
            sender = outer["from_peer_id"]
            permission = self.store.personal_space.model_permission(sender)
            with self.lock:
                now = self.clock()
                self.last_requests = {k: v for k, v in self.last_requests.items() if now - v < 60}
                if sender in self.last_requests and now - self.last_requests[sender] < 1 or len(self.last_requests) >= 64 and sender not in self.last_requests:
                    raise TaskProtocolError("space_catalog_rate_limited")
                self.last_requests[sender] = now
            rows = []
            for provider in self.providers():
                if not provider.accepting_orders:
                    continue
                # This is a short-lived identity challenge, not CLI discovery.
                # Publication refreshes the CLI inventory independently; spawning
                # a CLI here can exceed the peer HTTP timeout for every model.
                try:
                    status = provider.public_status(cached_cli_models=True)
                except (AdapterError, OSError):
                    # One unavailable executable must not hide healthy sources.
                    continue
                # CLI services follow the node's current sharing policy. Older
                # manifests do not carry the API-source personal-space-only flag.
                if status.get("access_policy") != "personal_space":
                    continue
                if valid(status.get("model_binding"), provider.manifest.package_id):
                    rows.append({**status, "network_id": self.network_id()})
            if len(rows) > 100:
                rows = sorted(rows, key=_service_sort_key)[:100]
            body = {"task_id": outer["task_id"], "services": rows, "space_permission": permission,
                    "provider_messaging_pub": peer_box.public_key_b64(self.key)}
            if len(json.dumps(body).encode()) > MAX_BYTES:
                rows = sorted(rows, key=_service_sort_key)
                while rows:
                    body["services"] = rows
                    if len(json.dumps(body).encode()) <= MAX_BYTES:
                        break
                    rows.pop()
                else:
                    body["services"] = []
                    if len(json.dumps(body).encode()) > MAX_BYTES:
                        raise TaskProtocolError("space_catalog_capacity_exhausted")
            return self._seal(body, outer["task_id"], "space_model_catalog_response", sender, request["reply_messaging_pub"])
        finally:
            self.slots.release()

    def refresh(self, advertised):
        # UI, background discovery and an Agent can need the same signed
        # challenge at once. Share its result instead of superseding a valid
        # response with a second request that hits the provider rate limit.
        peer = advertised["peer_id"]
        generation = self._local_generation(peer)
        with self.lock:
            existing = self.refreshes.get(peer)
            if existing is None:
                future = Future()
                self.refreshes[peer] = (generation, future)
            else:
                prior_generation, future = existing
                if prior_generation != generation:
                    self.last_errors[peer] = ("space_catalog_peer_not_allowed", self.clock())
                    raise TaskProtocolError("space_catalog_peer_not_allowed")
        if existing is not None:
            try:
                rows = future.result(timeout=30)
            except TimeoutError:
                with self.lock:
                    self.last_errors[peer] = ("space_catalog_timeout", self.clock())
                raise TaskProtocolError("space_catalog_timeout") from None
            if self._local_generation(peer) != generation:
                with self.lock:
                    self.last_errors[peer] = ("space_catalog_peer_not_allowed", self.clock())
                raise TaskProtocolError("space_catalog_peer_not_allowed")
            return copy.deepcopy(rows)
        try:
            rows = self._refresh(advertised)
            future.set_result(copy.deepcopy(rows))
            return rows
        except Exception as exc:
            future.set_exception(exc)
            raise
        finally:
            with self.lock:
                self.refreshes.pop(peer, None)

    def _refresh(self, advertised):
        peer = advertised["peer_id"]
        local_generation = self._local_generation(peer)
        if not self.outgoing_slots.acquire(timeout=10):
            with self.lock:
                self.last_errors[peer] = ("space_catalog_capacity_exhausted", self.clock())
            raise TaskProtocolError("space_catalog_capacity_exhausted")
        task_id = "space-catalog-" + uuid.uuid4().hex
        with self.lock:
            self.tickets[peer] = task_id
        transport_failed = False
        try:
            request = self._seal({"task_id": task_id, "reply_messaging_pub": peer_box.public_key_b64(self.key)},
                task_id, "space_model_catalog_request", peer, advertised["node_messaging_pub"])
            try:
                wire = self.exchange(peer, request)
            except Exception:
                transport_failed = True
                raise
            if len(json.dumps(wire).encode()) > 2 * MAX_BYTES:
                raise TaskProtocolError("space_catalog_response_limit")
            outer, body = open_task(wire, recipient_peer_id=self.store.peer_id,
                recipient_messaging_key=self.key, expected_kind="space_model_catalog_response")
            if (outer["from_peer_id"] != peer or outer["task_id"] != task_id or body.get("task_id") != task_id
                    or set(body) != {"task_id", "services", "space_permission", "provider_messaging_pub"}
                    or not isinstance(body["services"], list) or len(body["services"]) > 100):
                raise TaskProtocolError("space_catalog_response_invalid")
            from .routes import _validate_messaging_pub
            _validate_messaging_pub(body["provider_messaging_pub"])
            from .peer_binding import expected
            rows, identifiers = [], set()
            skipped = 0
            for status in body["services"]:
                if not isinstance(status, dict):
                    skipped += 1
                    continue
                service = status.get("service")
                service_id = service.get("package_id") if isinstance(service, dict) else None
                if not isinstance(service_id, str) or not service_id or service_id in identifiers:
                    skipped += 1
                    continue
                record = {**status, "peer_id": peer, "consumer_peer_id": self.store.peer_id, "binding_verified": True,
                    "consumer_access_generation": local_generation,
                    "space_permission": body["space_permission"], "node_messaging_pub": body["provider_messaging_pub"],
                    "access": "personal_space", "updated_at": datetime.fromtimestamp(self.clock(), timezone.utc).isoformat()}
                if expected(record, self.store.peer_id) is None:
                    skipped += 1
                    continue
                identifiers.add(service_id)
                rows.append(record)
            if self._local_generation(peer) != local_generation:
                raise TaskProtocolError("space_catalog_peer_not_allowed")
            with self.lock:
                if self.tickets.get(peer) != task_id:
                    raise TaskProtocolError("space_catalog_refresh_superseded")
                self.skipped_rows += skipped
                self.last_errors.pop(peer, None)
                if peer not in self.cache and len(self.cache) >= 64:
                    self.cache.pop(min(self.cache, key=lambda k: self.cache[k]["checked_at"]))
                self.cache[peer] = {
                    "checked_at": self.clock(),
                    "services": rows,
                    "local_generation": local_generation,
                    "skipped_rows": skipped,
                }
            return copy.deepcopy(rows)
        except Exception as exc:
            code = _classify_refresh_error(exc, transport_failed=transport_failed)
            with self.lock:
                self.last_errors[peer] = (code, self.clock())
                # A failed connection is not a new catalog. Keep the previous
                # signed snapshot only until its original expiry; records()
                # still checks the current membership generation on every read.
                if self.tickets.get(peer) == task_id and not transport_failed:
                    self.cache.pop(peer, None)
            raise TaskProtocolError(code) from None
        finally:
            self.outgoing_slots.release()
            with self.lock:
                if self.tickets.get(peer) == task_id:
                    self.tickets.pop(peer, None)

    def records(self):
        with self.lock:
            values = copy.deepcopy(self.cache)
        rows = []
        for peer, cached in values.items():
            try:
                current = self._local_generation(peer)
            except (ValueError, RuntimeError, OSError):
                continue
            if current != cached["local_generation"]:
                continue
            age = self.clock() - cached["checked_at"]
            if 0 <= age <= FRESH_SECONDS:
                rows.extend(cached["services"])
            elif age > FRESH_SECONDS:
                rows.extend(
                    {
                        **service,
                        "catalog_state": "stale",
                        "available": False,
                        "binding_verified": False,
                    }
                    for service in cached["services"]
                )
        return rows
