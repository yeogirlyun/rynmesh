"""Signed single-service invitations and recoverable relationship/grant joins.

The invite ID is the provider's operation ID. The relationship is committed
before its grant; retries may only write revision 1, never regrant a revocation.
The shared lock orders acceptance, owner permission writes and cancellation.
Consumer progress is durable before network I/O and clears bearer data on success.
"""
from __future__ import annotations

import hashlib
import re
import uuid
from pathlib import Path
from typing import Callable

from ..ai_access.store import AIAccessError
from ..atomic_io import atomic_write_json, read_json
from ..crypto import SignedPayload, canonical_json, sign_payload, verify_signed_payload
from ..file_transactions import file_transaction
from .crypto import parse_invite
from .service import FriendError

ERRORS = {
    'ai_review_required', 'ai_review_changed', 'ai_service_changed', 'ai_not_ready',
    'ai_busy', 'ai_sharing_paused', 'ai_invite_revoked', 'ai_grant_pending',
    'ai_discovery_pending', 'ai_operation_cancelled', 'ai_operation_not_found',
}


def sharing_lock(home: Path) -> Path:
    return Path(home) / 'ai-access' / '.sharing.lock'


def review_token(snapshot: dict) -> str:
    return hashlib.sha256(canonical_json({k: snapshot[k] for k in ('service', 'network_id')})).hexdigest()


class FriendAISharing:
    def __init__(self, friends, *, provider: Callable, grants: Callable, catalog: Callable):
        self.friends = friends
        self.provider = provider
        self.grants = grants
        self.catalog = catalog
        self.root = Path(friends.home) / 'friends' / 'ai-joins'

    def snapshot(self, service_id: str | None = None) -> dict:
        status = self.provider() or {}
        if isinstance(status, list):
            matches = [row for row in status if row.get('service') and
                       (not service_id or row['service'].get('package_id') == service_id)]
            if len(matches) != 1:
                raise FriendError('ai_service_changed')
            status = matches[0]
        service = status.get('service')
        if not service or (service_id and service['package_id'] != service_id):
            raise FriendError('ai_service_changed')
        if not isinstance(service.get('pricing'), dict):
            raise FriendError('ai_review_required')
        return {'service': service, 'network_id': status.get('network_id', 'rynmesh-main'),
                'ready': status.get('ready') is True,
                'sharing': status.get('publication_enabled') is True,
                'busy': status.get('capacity', {}).get('available') == 0}

    @staticmethod
    def require_ready(snapshot: dict):
        if not snapshot['sharing']:
            raise FriendError('ai_sharing_paused')
        if not snapshot['ready']:
            raise FriendError('ai_not_ready')
        if snapshot['busy']:
            raise FriendError('ai_busy')

    def create(self, service_id: str) -> dict:
        current = self.snapshot(service_id)
        self.require_ready(current)
        return self.friends.create_invite(ai=current)

    def peer_review(self, body: dict) -> dict:
        signed, _ = parse_invite(str(body.get('invite_uri', '')), now=self.friends.clock(),
                                allow_loopback=self.friends.allow_loopback, allow_expired=True)
        payload = signed.payload
        if payload['peer_id'] != self.friends.peer_id or payload.get('purpose') != 'ai':
            raise FriendError('invalid_invite')
        stored = self.friends.store.state()['invites'].get(payload['invite_id'])
        if not stored or stored.get('secret_hash') != payload['secret_hash']:
            raise FriendError('invite_not_found')
        if stored['status'] == 'cancelled':
            raise FriendError('invite_cancelled')
        if stored['status'] == 'active' and stored['expires_at'] <= self.friends.clock().isoformat():
            raise FriendError('invite_expired')
        current = self.snapshot(payload['ai']['service']['package_id'])
        reply = {'request_digest': hashlib.sha256(canonical_json(body)).hexdigest(),
                 'ai': current, 'review_token': review_token(current)}
        return sign_payload(reply, private_key_bytes=self.friends.identity_private).to_dict()

    def inspect(self, uri: str) -> dict:
        signed, _ = parse_invite(uri, now=self.friends.clock(), allow_loopback=self.friends.allow_loopback,
                                 allow_expired=True)
        if signed.payload.get('purpose') != 'ai':
            return self.friends._public_invite(signed.payload)
        request = {'invite_uri': uri, 'nonce': uuid.uuid4().hex}
        try:
            wire = self.friends.post_json(signed.payload['endpoint'], '/api/peer/friends/ai-review', request, None)
            response = SignedPayload.from_dict(wire)
            verify_signed_payload(response)
            if (response.public_key != signed.payload['peer_id'] or
                    response.payload['request_digest'] != hashlib.sha256(canonical_json(request)).hexdigest()):
                raise ValueError('invalid signature binding')
            if response.payload.get('error'):
                raise FriendError(response.payload['error'])
            snapshot = response.payload['ai']
            if snapshot['service']['package_id'] != signed.payload['ai']['service']['package_id']:
                raise FriendError('ai_service_changed')
            return {**self.friends._public_invite(signed.payload), 'ai': snapshot,
                    'review_token': response.payload['review_token'],
                    'changed': review_token(signed.payload['ai']) != review_token(snapshot)}
        except FriendError:
            raise
        except Exception as exc:
            raise FriendError('could_not_join_friend') from exc

    def reconnect(self, uri: str) -> dict:
        signed, _ = parse_invite(uri, now=self.friends.clock(), allow_loopback=self.friends.allow_loopback)
        relation, _ = self.friends._relationship(signed.payload['peer_id'])
        if relation['messaging_pub'] != signed.payload['messaging_pub']:
            raise FriendError('friend_acceptance_invalid')
        try:
            self.catalog().refresh(relation['peer_id'], verified_endpoint=signed.payload['endpoint'])
        except Exception:
            raise FriendError('could_not_join_friend') from None
        current = self.friends.store.relationship(relation['relationship_id'])
        if not current:
            raise FriendError('friend_revoked')
        return self.friends.public_relationship(current)

    def before_accept(self, payload: dict, body: dict):
        if payload.get('purpose') != 'ai':
            return
        stored = self.friends.store.state()['invites'].get(payload['invite_id'])
        if not stored or stored.get('ai') != payload.get('ai') or stored.get('purpose') != 'ai':
            raise FriendError('invalid_invite')
        if stored['status'] == 'cancelled':
            raise FriendError('invite_cancelled')
        current = self.snapshot(payload['ai']['service']['package_id'])
        if body.get('ai_review') != review_token(current):
            raise FriendError('ai_review_changed')
        self.require_ready(current)

    def finish_accept(self, payload: dict, relationship: dict) -> dict:
        if payload.get('purpose') != 'ai':
            return {}
        rid = relationship['relationship_id']
        service_id = payload['ai']['service']['package_id']
        try:
            # Zero is fixed forever for this operation. Even a lost progress
            # write cannot cause a revoked grant to be reissued on a retry.
            grant = self.grants().set(service_id, rid, allowed=True, expected_revision=0)
        except AIAccessError as exc:
            raise FriendError('ai_invite_revoked') from exc
        except OSError as exc:
            raise FriendError('ai_grant_pending') from exc
        if not grant['effective'] or grant['revision'] != 1:
            raise FriendError('ai_invite_revoked')
        self.friends.store.patch_invite(payload['invite_id'], {'ai_stage': 'authorized'})
        return {'ai_operation': {'operation_id': payload['invite_id'], 'service_id': service_id,
                                 'relationship_id': rid, 'revision': 1, 'stage': 'authorized'}}

    def path(self, operation_id: str) -> Path:
        if not re.fullmatch('[a-f0-9]{32}', operation_id):
            raise FriendError('ai_operation_not_found')
        return self.root / f'{operation_id}.json'

    def read(self, operation_id: str) -> dict:
        path = self.path(operation_id)
        if not path.exists():
            raise FriendError('ai_operation_not_found')
        row = read_json(path, max_bytes=64 * 1024)
        if row.get('version') != 'ryn.ai-join.v1' or row.get('operation_id') != operation_id:
            raise FriendError('ai_operation_not_found')
        return row

    def write(self, row: dict):
        atomic_write_json(self.path(row['operation_id']), row, max_bytes=64 * 1024)

    @staticmethod
    def public(row: dict) -> dict:
        return {k: v for k, v in row.items() if k not in {'uri', 'review_token'}}

    def operations(self) -> list[dict]:
        return [self.public(self.read(path.stem)) for path in sorted(self.root.glob('*.json'))]

    def begin(self, uri: str, token: str) -> dict:
        signed, _ = parse_invite(uri, now=self.friends.clock(), allow_loopback=self.friends.allow_loopback,
                                 allow_expired=True)
        payload = signed.payload
        if payload.get('purpose') != 'ai' or not re.fullmatch('[a-f0-9]{64}', token):
            raise FriendError('ai_review_required')
        operation_id = payload['invite_id']
        with file_transaction(self.path(operation_id).with_suffix('.lock')):
            try:
                row = self.read(operation_id)
            except FriendError as exc:
                if str(exc) != 'ai_operation_not_found':
                    raise
                if len(list(self.root.glob('*.json'))) >= 500:
                    raise FriendError('friend_capacity_exhausted') from None
                row = {'version': 'ryn.ai-join.v1', 'operation_id': operation_id, 'uri': uri,
                       'review_token': token, 'stage': 'joining', 'peer_id': payload['peer_id'],
                       'accepted_digest': token, 'preview': self.friends._public_invite(payload),
                       'node_name': payload['node_name'], 'service_id': payload['ai']['service']['package_id'],
                       'network_id': payload['ai']['network_id']}
            if row['stage'] == 'cancelled':
                raise FriendError('ai_operation_cancelled')
            if row.get('uri'):
                row['review_token'] = token
                row['accepted_digest'] = token
            self.write(row)  # Durable intent before the first request.
        return self.resume(operation_id)

    def rereview(self, operation_id: str) -> dict:
        row = self.read(operation_id)
        if row.get('uri'):
            return self.inspect(row['uri'])
        snapshot = self.catalog().refresh(row['peer_id'])
        record = next((item for item in snapshot['services'] if item['service']['package_id'] == row['service_id']), None)
        if not record:
            raise FriendError('ai_invite_revoked')
        ai = {'service': record['service'], 'network_id': record.get('network_id', row['network_id']),
              'ready': record.get('ready'), 'sharing': record.get('online'), 'busy': record.get('capacity', {}).get('available') == 0}
        return {**row['preview'], 'ai': ai, 'review_token': review_token(ai), 'changed': True}

    def resume(self, operation_id: str, token: str = '') -> dict:
        with file_transaction(self.path(operation_id).with_suffix('.lock')):
            row = self.read(operation_id)
            if row['stage'] == 'cancelled':
                raise FriendError('ai_operation_cancelled')
            try:
                if row.get('uri'):
                    if token:
                        row['review_token'] = token
                        row['accepted_digest'] = token
                        self.write(row)
                    friend = self.friends.join(row['uri'], ai_review=row['review_token'])
                    operation = friend.get('ai_operation') or {}
                    if operation.get('operation_id') != operation_id or operation.get('stage') != 'authorized':
                        raise FriendError('ai_grant_pending')
                    row.update(stage='discovering', relationship_id=friend['relationship_id'])
                    self.write(row)
                snapshot = self.catalog().refresh(row['peer_id'])
                service = next((item for item in snapshot['services'] if item['service']['package_id'] == row['service_id']), None)
                if not service:
                    raise FriendError('ai_invite_revoked' if snapshot['status'] == 'revoked' else 'ai_discovery_pending')
                current_digest = review_token({'service': service['service'], 'network_id': service.get('network_id', row['network_id'])})
                if current_digest != (token or row['accepted_digest']):
                    raise FriendError('ai_review_changed')
                row['accepted_digest'] = current_digest
                if not service.get('ready'):
                    raise FriendError('ai_not_ready')
                if not service.get('online'):
                    raise FriendError('ai_sharing_paused')
                if service.get('capacity', {}).get('available') == 0:
                    raise FriendError('ai_busy')
                row.update(stage='complete', error='', network_id=service.get('network_id', row['network_id']))
                row.pop('uri', None)
                row.pop('review_token', None)
                self.write(row)
            except Exception as exc:
                code = str(exc) if str(exc) in ERRORS | {'invite_expired', 'invite_cancelled', 'invite_used', 'friend_revoked'} else 'could_not_join_friend'
                row.update(error=code)
                if row['stage'] == 'complete':
                    row['stage'] = 'discovering'
                self.write(row)
            return self.public(row)

    def abandon(self, operation_id: str) -> dict:
        with file_transaction(self.path(operation_id).with_suffix('.lock')):
            row = self.read(operation_id)
            row.update(stage='cancelled', error='')
            row.pop('uri', None)
            row.pop('review_token', None)
            self.write(row)
            return self.public(row)
