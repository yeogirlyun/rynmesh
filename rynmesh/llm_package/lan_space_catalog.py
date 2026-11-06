"""Discover permitted space models directly when the registry is unavailable."""
from __future__ import annotations

import json
import time
from urllib.request import Request, urlopen

from ..transport import network_key_header
from .task_protocol import TaskProtocolError


def read_lan_key(endpoint, peer):
    request = Request(endpoint.rstrip('/') + '/api/peer/pubkey', headers=network_key_header())
    with urlopen(request, timeout=2) as response:
        raw = response.read(4097)
    if len(raw) > 4096:
        raise TaskProtocolError('space_catalog_key_response_limit')
    value = json.loads(raw)
    if value.get('peer_id') != peer:
        raise TaskProtocolError('space_catalog_peer_mismatch')
    from .routes import _validate_messaging_pub
    _validate_messaging_pub(value.get('x25519_pub'))
    return value['x25519_pub']


class LanSpaceCatalogRefresh:
    def __init__(self, store, catalog, *, read_key=read_lan_key, clock=time.monotonic):
        self.store, self.catalog, self.read_key, self.clock = store, catalog, read_key, clock
        self.attempts = {}

    def refresh_once(self, network):
        space = self.store.personal_space
        status = space.status()
        if status.get('membership') != 'active':
            return False
        candidates = []
        for member in status.get('members', []):
            peer = member.get('peer_id')
            if not peer or peer == self.store.peer_id or member.get('removed') or not space.allows_ai(peer):
                continue
            endpoints = self.store.lan_peer_endpoints(peer, network_id=network)
            if endpoints:
                candidates.append((peer, endpoints))
        allowed = {peer for peer, _ in candidates}
        self.attempts = {peer: value for peer, value in self.attempts.items() if peer in allowed}
        if not candidates:
            return False
        peer, endpoints = min(candidates, key=lambda item: self.attempts.get(item[0], float('-inf')))
        now = self.clock()
        if now - self.attempts.get(peer, float('-inf')) < 30:
            return False
        self.attempts[peer] = now
        for endpoint in endpoints[:2]:
            try:
                key = self.read_key(endpoint, peer)
                # The public key is only a locator. refresh verifies the sealed,
                # signed response and both devices' current space permissions.
                self.catalog.refresh({'peer_id': peer, 'node_messaging_pub': key})
                return True
            except Exception:
                continue
        return False
