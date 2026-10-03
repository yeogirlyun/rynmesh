"""Explicit, bounded direct-link checks without sending user content."""
from __future__ import annotations

import hashlib
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor, wait
from urllib.error import HTTPError, URLError

from ..crypto import SignedPayload, canonical_json, sign_payload, verify_signed_payload
from .crypto import FriendCryptoError, validate_endpoint
from .service import FriendError

PATH = '/api/peer/friends/diagnostics'
KIND = 'ryn.friend-probe.v1'
MAX_LINKS = 5


def respond(service, body, headers):
    if (not isinstance(body, dict) or set(body) != {'kind', 'from', 'to', 'relationship_id', 'challenge'}
            or body.get('kind') != KIND or not isinstance(body.get('challenge'), str)
            or len(body['challenge']) != 32):
        raise FriendError('friend_probe_invalid')
    relationship = service.verify_request(path=PATH, body=canonical_json(body), headers=headers)
    if (body['from'] != relationship['peer_id'] or body['to'] != service.peer_id
            or body['relationship_id'] != relationship['relationship_id']):
        raise FriendError('friend_probe_invalid')
    payload = {**body, 'from': service.peer_id, 'to': relationship['peer_id']}
    return {'proof': sign_payload(payload, private_key_bytes=service.identity_private).to_dict()}


def failure_code(exc):
    """Inspect typed causes only; never return transport exception strings."""
    seen = set()
    while exc is not None and id(exc) not in seen:
        seen.add(id(exc))
        if isinstance(exc, HTTPError):
            if exc.code in {401, 403}:
                return 'authentication_failed'
            if exc.code in {404, 405}:
                return 'unsupported'
            if exc.code == 429:
                return 'rate_limited'
        if isinstance(exc, TimeoutError):
            return 'timed_out'
        if isinstance(exc, FriendCryptoError):
            return 'endpoint_rejected'
        if getattr(exc, 'reason', None) == 'too_large':
            return 'invalid_response'
        if type(exc).__name__ == 'PeerTransportError' and str(exc) == 'peer_transport_post_unsupported':
            return 'transport_unsupported'
        if isinstance(exc, URLError) and isinstance(exc.reason, BaseException):
            exc = exc.reason
        else:
            exc = exc.__cause__
    return 'unreachable'


class FriendDiagnostics:
    def __init__(self, friends, *, deadline=7.0):
        self.friends = friends
        self.deadline = deadline
        self._executor = ThreadPoolExecutor(max_workers=MAX_LINKS, thread_name_prefix='friend-probe')
        self._lock = threading.RLock()
        self._running = False
        self._results = {}

    @staticmethod
    def _binding(row):
        return hashlib.sha256(canonical_json([row['peer_id'], row.get('endpoint', '')])).hexdigest()

    def snapshot(self):
        now = time.time()
        friends = [row for row in self.friends().list_friends() if row['status'] == 'active']
        with self._lock:
            current = {row['relationship_id'] for row in friends}
            self._results = {rid: result for rid, result in self._results.items()
                if rid in current and now - result['checked_at'] < 300}
            links = []
            for row in friends:
                previous = self._results.get(row['relationship_id'], {})
                if previous.get('binding') != self._binding(row):
                    previous = {}
                links.append({'relationship_id': row['relationship_id'], 'peer_id': row['peer_id'],
                    'node_name': row['node_name'], 'state': previous.get('state', 'not_checked'),
                    'checked_at': previous.get('checked_at'), 'latency_ms': previous.get('latency_ms'),
                    'stale': bool(previous and now - previous['checked_at'] > 60)})
            return {'from_peer_id': self.friends().peer_id, 'running': self._running,
                    'scope': 'outbound_direct', 'links': links}

    def probe(self, row):
        service = self.friends()
        started = time.monotonic()
        result = {'binding': self._binding(row), 'checked_at': time.time(), 'state': 'unreachable', 'latency_ms': None}
        if not row.get('endpoint'):
            return {**result, 'state': 'endpoint_missing'}
        try:
            validate_endpoint(row['endpoint'], allow_loopback=service.allow_loopback)
            record, secret = service._relationship(row['peer_id'])
            if record['relationship_id'] != row['relationship_id']:
                return {**result, 'state': 'friend_inactive'}
            wire = {'kind': KIND, 'from': service.peer_id, 'to': row['peer_id'],
                    'relationship_id': row['relationship_id'], 'challenge': uuid.uuid4().hex}
            response = service._request_wire(record, secret, PATH, wire, max_response_bytes=4096)
        except Exception as exc:
            return {**result, 'state': failure_code(exc)}
        try:
            proof = SignedPayload.from_dict(response['proof'])
            verify_signed_payload(proof)
            if proof.public_key != row['peer_id']:
                return {**result, 'state': 'identity_mismatch'}
            if proof.payload != {**wire, 'from': row['peer_id'], 'to': service.peer_id}:
                return {**result, 'state': 'invalid_response'}
            current, _ = service._relationship(row['peer_id'])
            if current['relationship_id'] != row['relationship_id'] or self._binding(current) != result['binding']:
                return {**result, 'state': 'friend_inactive'}
        except (KeyError, TypeError, ValueError):
            return {**result, 'state': 'invalid_response'}
        return {**result, 'checked_at': time.time(), 'state': 'reachable',
                'latency_ms': round((time.monotonic() - started) * 1000, 1)}

    def run(self, relationship_ids):
        if (not isinstance(relationship_ids, list) or not 1 <= len(relationship_ids) <= MAX_LINKS
                or any(not isinstance(rid, str) for rid in relationship_ids)
                or len(set(relationship_ids)) != len(relationship_ids)):
            raise FriendError('friend_probe_selection_invalid')
        friends = {row['relationship_id']: row for row in self.friends().list_friends() if row['status'] == 'active'}
        if any(rid not in friends for rid in relationship_ids):
            raise FriendError('active_friend_required')
        with self._lock:
            if self._running:
                raise FriendError('friend_probe_busy')
            self._running = True
        futures = {self._executor.submit(self.probe, friends[rid]): rid for rid in relationship_ids}
        done, pending = wait(futures, timeout=self.deadline)
        with self._lock:
            for future, rid in futures.items():
                fallback = {
                    'binding': self._binding(friends[rid]), 'checked_at': time.time(),
                    'state': 'timed_out', 'latency_ms': None}
                if future in done:
                    try:
                        self._results[rid] = future.result()
                    except Exception:
                        self._results[rid] = {**fallback, 'state': 'unreachable'}
                else:
                    self._results[rid] = fallback
            # A misbehaving transport may outlive its timeout. Keep the batch
            # reserved until its threads exit, preventing unbounded retries.
            remaining = set(pending)

            def finished(future):
                with self._lock:
                    remaining.discard(future)
                    if not remaining:
                        self._running = False

            for future in pending:
                future.add_done_callback(finished)
            if not remaining:
                self._running = False
        return self.snapshot()

    def close(self):
        self._executor.shutdown(wait=False, cancel_futures=True)
