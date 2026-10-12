"""Bounded signed challenge catalog for personal-space model destinations."""
from __future__ import annotations

import copy
import hashlib
import json
import threading
import time
import uuid
from datetime import datetime, timezone

from ..services import peer_box
from .peer_binding import valid
from .task_protocol import TaskProtocolError, open_task, seal_task

PATH = "/api/peer/llm/space-catalog"
MAX_BYTES = 64 * 1024
FRESH_SECONDS = 120


class SpaceModelCatalog:
    def __init__(self, *, store, messaging_key, providers, exchange, clock=time.time, network_id=lambda: "rynmesh-main"):
        self.store, self.key, self.providers, self.exchange, self.clock = store, messaging_key, providers, exchange, clock
        self.network_id = network_id
        self.lock = threading.RLock()
        self.slots = threading.BoundedSemaphore(2)
        self.outgoing_slots = threading.BoundedSemaphore(2)
        self.last_requests, self.cache, self.tickets = {}, {}, {}

    def _local_generation(self, peer):
        permission = self.store.personal_space.model_permission(peer)
        return hashlib.sha256(json.dumps(permission, sort_keys=True).encode()).hexdigest()

    def _seal(self, body, task_id, kind, recipient, recipient_key):
        expiry = datetime.fromtimestamp(self.clock() + 60, timezone.utc).isoformat()
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
                if "personal-space-only" not in provider.manifest.capabilities or not provider.accepting_orders:
                    continue
                status = provider.public_status()
                if valid(status.get("model_binding"), provider.manifest.package_id):
                    rows.append({**status, "network_id": self.network_id()})
            if len(rows) > 100:
                raise TaskProtocolError("space_catalog_capacity_exhausted")
            body = {"task_id": outer["task_id"], "services": rows, "space_permission": permission,
                    "provider_messaging_pub": peer_box.public_key_b64(self.key)}
            if len(json.dumps(body).encode()) > MAX_BYTES:
                raise TaskProtocolError("space_catalog_capacity_exhausted")
            return self._seal(body, outer["task_id"], "space_model_catalog_response", sender, request["reply_messaging_pub"])
        finally:
            self.slots.release()

    def refresh(self, advertised):
        peer = advertised["peer_id"]
        local_generation = self._local_generation(peer)
        if not self.outgoing_slots.acquire(blocking=False):
            raise TaskProtocolError("space_catalog_capacity_exhausted")
        task_id = "space-catalog-" + uuid.uuid4().hex
        with self.lock:
            self.tickets[peer] = task_id
        try:
            request = self._seal({"task_id": task_id, "reply_messaging_pub": peer_box.public_key_b64(self.key)},
                task_id, "space_model_catalog_request", peer, advertised["node_messaging_pub"])
            wire = self.exchange(peer, request)
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
            for status in body["services"]:
                service_id = status["service"]["package_id"]
                record = {**status, "peer_id": peer, "consumer_peer_id": self.store.peer_id, "binding_verified": True,
                    "consumer_access_generation": local_generation,
                    "space_permission": body["space_permission"], "node_messaging_pub": body["provider_messaging_pub"],
                    "access": "personal_space", "updated_at": datetime.fromtimestamp(self.clock(), timezone.utc).isoformat()}
                if service_id in identifiers or expected(record, self.store.peer_id) is None:
                    raise TaskProtocolError("space_catalog_response_invalid")
                identifiers.add(service_id)
                rows.append(record)
            if self._local_generation(peer) != local_generation:
                raise TaskProtocolError("space_catalog_peer_not_allowed")
            with self.lock:
                if self.tickets.get(peer) != task_id:
                    raise TaskProtocolError("space_catalog_refresh_superseded")
                if peer not in self.cache and len(self.cache) >= 64:
                    self.cache.pop(min(self.cache, key=lambda k: self.cache[k]["checked_at"]))
                self.cache[peer] = {"checked_at": self.clock(), "services": rows, "local_generation": local_generation}
            return copy.deepcopy(rows)
        except Exception:
            with self.lock:
                if self.tickets.get(peer) == task_id:
                    self.cache.pop(peer, None)
            raise TaskProtocolError("space_catalog_refresh_failed") from None
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
            if 0 <= self.clock() - cached["checked_at"] <= FRESH_SECONDS and current == cached["local_generation"]:
                rows.extend(cached["services"])
        return rows
