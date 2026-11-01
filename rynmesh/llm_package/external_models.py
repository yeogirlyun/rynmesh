"""Narrow model access through a locally installed peer provider."""
from __future__ import annotations

import asyncio
import threading
import time
import uuid
from datetime import datetime, timezone

from ..drop.peers import http_json, verify_hello
from .peer_binding import valid


def resource_of(binding):
    permission = binding.get("external_permission") if isinstance(binding, dict) else None
    return permission.get("resource", "") if isinstance(permission, dict) else ""


def authorize_model(store, sender, service_id, permission, binding):
    service = getattr(store, "peer_providers", None)
    resource = permission.get("resource") if isinstance(permission, dict) and set(permission) == {"resource"} else None
    if (service is None or not resource or not valid(binding, service_id)
            or not service.locally_published(resource, service_id, binding)):
        raise ValueError("peer_provider_access_denied")
    # ProviderService runs in a worker, including its stream worker. No token is
    # ever placed in the encrypted peer request or returned to the consumer.
    try:
        asyncio.run(service.authorize(resource, sender, store.peer_id, "model.invoke", service_id))
    except Exception:
        raise ValueError("peer_provider_access_denied") from None


class ExternalModelCatalog:
    """Directory metadata plus a short-lived signed device reachability probe."""

    def __init__(self, providers, store, *, clock=time.monotonic, request=None, probe=None):
        self.providers, self.store, self.clock, self.request = providers, store, clock, request or http_json
        self.probe = probe
        self._lock = threading.Lock()
        self._observations = {}
        self._index = 0

    @staticmethod
    def _key(row):
        return (row.get("provider_resource"), row.get("peer_id"), row.get("messaging_pub"),
                tuple(row.get("endpoints", [])))

    def refresh(self, row):
        key = self._key(row)
        with self._lock:
            cached = self._observations.get(key)
        if cached and self.clock() - cached[0] < 8:
            return cached[1]
        online = False
        if self.probe:
            try:
                observed = self.probe(row["provider_resource"], row["peer_id"])
                online = observed.online and observed.messaging_pub == row.get("messaging_pub")
            except Exception:
                online = False
        for endpoint in ([] if self.probe else row.get("endpoints", [])[:4]):
            nonce = uuid.uuid4().hex
            try:
                status, body = self.request(endpoint, "GET", "/api/peer/drop/hello?nonce=" + nonce, timeout=2.5, network_auth=False)
                hello = verify_hello(body, peer_id=row["peer_id"], nonce=nonce) if status == 200 else {}
                if hello.get("messaging_pub") == row.get("messaging_pub"):
                    online = True
                    break
            except Exception:
                continue
        with self._lock:
            self._observations[key] = (self.clock(), online)
            if len(self._observations) > 512:
                oldest = min(self._observations, key=lambda k: self._observations[k][0])
                self._observations.pop(oldest, None)
        return online

    def run_once(self):
        rows = self.providers.cached_models()
        candidates = {self._key(row): row for row in rows if row.get("peer_id") != self.store.peer_id}
        if not candidates:
            return False
        values = list(candidates.values())
        row = values[self._index % len(values)]
        self._index += 1
        self.refresh(row)
        return True

    def records(self):
        rows = self.providers.cached_models()
        result = []
        for row in rows:
            if row.get("peer_id") == self.store.peer_id:
                continue
            with self._lock:
                observed = self._observations.get(self._key(row))
            online = bool(observed and self.clock() - observed[0] <= 15 and observed[1])
            result.append({**row, "online": online, "ready": True, "access": "provider",
                "node_messaging_pub": row.get("messaging_pub", ""),
                "updated_at": datetime.now(timezone.utc).isoformat(),
                "presence": {"state": "online" if online else "unconfirmed", "basis": "signed_device_probe"}})
        return result
