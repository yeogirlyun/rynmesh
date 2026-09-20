"""The creating node serializes edits; other members queue durable operations.

No last-writer wall-clock merge. Per-member sequence receipts make lost-response
retries idempotent. Reading status belongs to the authenticated editing member.
"""
import hashlib
import json
import re
import threading
import uuid
from copy import deepcopy
from urllib.parse import urlsplit

from ..crypto import canonical_json
from ..services import peer_box
from .store import ListError

PATH = '/api/peer/shared-reading'
WIRE = 'ryn.shared-reading-wire.v1'
CHANNEL = b'rynmesh-shared-reading-wire-v1'
MAX_WIRE = 1024 * 1024


def identity(value):
    if not isinstance(value, str) or not re.fullmatch('[a-f0-9]{32}', value):
        raise ListError('shared_request_invalid')
    return value


def text(value, limit):
    if not isinstance(value, str) or not value.strip() or len(value.encode()) > limit:
        raise ListError('shared_request_invalid')
    return value.strip()


def digest(value):
    return hashlib.sha256(canonical_json(value)).hexdigest()


def validate_edit(action, value):
    keys = {'add': {'id', 'title', 'url'}, 'read': {'id', 'read'}, 'remove': {'id'}, 'close': set()}
    if action not in keys or not isinstance(value, dict) or set(value) != keys[action]:
        raise ListError('shared_request_invalid')
    if action != 'close':
        identity(value['id'])
    if action == 'read' and type(value['read']) is not bool:
        raise ListError('shared_request_invalid')
    if action == 'add':
        text(value['title'], 500)
        parsed = urlsplit(text(value['url'], 2048))
        if parsed.scheme not in {'https', 'http'} or not parsed.hostname or parsed.username or parsed.password:
            raise ListError('shared_link_invalid')


class SharedReading:
    def __init__(self, store, friends):
        self.store, self.friends = store, friends
        self.running = threading.Lock()
        self.cursor = 0

    @property
    def peer(self):
        return self.friends().peer_id

    def active(self, rid, peer=None):
        row = self.friends().store.relationship(identity(rid))
        if not row or row['status'] != 'active' or peer is not None and row['peer_id'] != peer:
            raise ListError('shared_friend_inactive')
        return row

    def create(self, rid, title, identifier):
        friend = self.active(rid)
        identity(identifier)
        title = text(title, 300)
        def change(data):
            prior = data['lists'].get(identifier)
            if prior:
                if prior['owner'] != self.peer or prior['friend'] != friend['peer_id'] or prior['title'] != title:
                    raise ListError('shared_operation_changed')
                return prior
            row = {'id': identifier, 'owner': self.peer, 'friend': friend['peer_id'], 'relationship_id': rid,
                   'title': title, 'status': 'invited', 'revision': 1, 'items': {}, 'receipts': {}, 'pending': []}
            data['lists'][identifier] = row
            return row
        return self.store.mutate(change)

    def snapshot(self, row):
        return {key: deepcopy(row[key]) for key in ('id', 'owner', 'friend', 'relationship_id', 'title', 'status', 'revision', 'items', 'receipts')}

    def listing(self):
        result = []
        for row in self.store.read()['lists'].values():
            value = deepcopy(row)
            value['pending_count'] = len(value.pop('pending', []))
            value['cancelled_count'] = len(value.pop('cancelled', []))
            value['local_peer'] = self.peer
            try:
                friend = self.active(row['relationship_id'], row['friend'] if row['owner'] == self.peer else row['owner'])
                value['friend_name'] = friend['node_name']
            except ListError:
                value['blocked'] = True
                value['friend_name'] = 'Inactive friend'
            result.append(value)
        return result

    def owner_row(self, data, identifier, peer, *, active=True):
        row = data['lists'].get(identity(identifier))
        if not row or row['owner'] != self.peer or peer not in (row['owner'], row['friend']):
            raise ListError('shared_list_unavailable')
        self.active(row['relationship_id'], row['friend'])
        if active and row['status'] != 'active':
            raise ListError('shared_list_inactive')
        return row

    def apply(self, data, identifier, peer, operation):
        row = self.owner_row(data, identifier, peer, active=False)
        if not isinstance(operation, dict) or set(operation) != {'id', 'sequence', 'action', 'value'}:
            raise ListError('shared_request_invalid')
        identity(operation['id'])
        seq = operation['sequence']
        prior = row['receipts'].get(peer, {'sequence': 0})
        if type(seq) is not int or not 1 <= seq <= 10**9:
            raise ListError('shared_request_invalid')
        fingerprint = digest(operation)
        if seq == prior['sequence']:
            if prior.get('digest') != fingerprint:
                raise ListError('shared_operation_changed')
            return self.snapshot(row)
        if seq != prior['sequence'] + 1:
            raise ListError('shared_sequence_changed')
        if row['status'] != 'active':
            raise ListError('shared_list_inactive')
        action, value = operation['action'], operation['value']
        if not isinstance(value, dict):
            raise ListError('shared_request_invalid')
        if action == 'add':
            if set(value) != {'id', 'title', 'url'}:
                raise ListError('shared_request_invalid')
            item_id = identity(value['id'])
            title, url = text(value['title'], 500), text(value['url'], 2048)
            parsed = urlsplit(url)
            if parsed.scheme not in {'https', 'http'} or not parsed.hostname or parsed.username or parsed.password:
                raise ListError('shared_link_invalid')
            if item_id in row['items'] or len(row['items']) >= 256:
                raise ListError('shared_capacity')
            row['items'][item_id] = {'id': item_id, 'title': title, 'url': url, 'added_by': peer,
                                     'read_by': {}, 'removed': False}
        elif action in {'read', 'remove'}:
            if set(value) != ({'id', 'read'} if action == 'read' else {'id'}):
                raise ListError('shared_request_invalid')
            item = row['items'].get(identity(value['id']))
            if not item:
                raise ListError('shared_item_unavailable')
            if action == 'read':
                if type(value['read']) is not bool:
                    raise ListError('shared_request_invalid')
                # A deletion wins over later/queued reading changes.
                if not item['removed']:
                    item['read_by'][peer] = value['read']
            else:
                item.update(removed=True, title='', url='', read_by={})
        elif action == 'close':
            if value:
                raise ListError('shared_request_invalid')
            row['status'] = 'closed'
        else:
            raise ListError('shared_request_invalid')
        row['revision'] += 1
        row['receipts'][peer] = {'sequence': seq, 'digest': fingerprint}
        return self.snapshot(row)

    def edit(self, identifier, operation_id, action, value):
        identity(identifier)
        identity(operation_id)
        validate_edit(action, value)
        def change(data):
            row = data['lists'].get(identifier)
            if not row:
                raise ListError('shared_list_inactive')
            self.active(row['relationship_id'], row['friend'] if row['owner'] == self.peer else row['owner'])
            fingerprint = digest({'action': action, 'value': value})
            seen = row.setdefault('local_operations', {})
            if operation_id in seen:
                if seen[operation_id] != fingerprint:
                    raise ListError('shared_operation_changed')
                return row
            if len(seen) >= 4096:
                raise ListError('shared_capacity')
            if row['status'] != 'active' or any(op['action'] == 'close' for op in row['pending']):
                raise ListError('shared_list_inactive')
            pending = row['pending']
            for prior in pending:
                if prior['id'] == operation_id:
                    if prior['action'] != action or prior['value'] != value:
                        raise ListError('shared_operation_changed')
                    return row
            last = row['receipts'].get(self.peer, {'sequence': 0})['sequence']
            operation = {'id': operation_id, 'sequence': last + len(pending) + 1, 'action': action, 'value': value}
            if row['owner'] == self.peer:
                receipt = row['receipts'].get(self.peer)
                if row.get('last_local_id') == operation_id:
                    prior = {**operation, 'sequence': receipt['sequence']}
                    if digest(prior) != receipt['digest']:
                        raise ListError('shared_operation_changed')
                    return row
                self.apply(data, identifier, self.peer, operation)
                row['last_local_id'] = operation_id
            else:
                if len(pending) >= 128 or len(canonical_json(operation)) > 4096:
                    raise ListError('shared_capacity')
                if action not in {'add', 'read', 'remove', 'close'} or not isinstance(value, dict):
                    raise ListError('shared_request_invalid')
                pending.append(operation)
            seen[operation_id] = fingerprint
            return row
        return self.store.mutate(change)

    def request(self, peer, action, **args):
        friends = self.friends()
        relation, secret = friends._relationship(peer)
        self.active(relation['relationship_id'], peer)
        request_id = uuid.uuid4().hex
        payload = {'version': WIRE, 'request_id': request_id, 'relationship_id': relation['relationship_id'],
                   'from': self.peer, 'to': peer, 'action': action, **args}
        nonce, ciphertext = peer_box.seal(friends.messaging_private, relation['messaging_pub'], canonical_json(payload), info=CHANNEL)
        try:
            wire = friends._request_wire(relation, secret, PATH, {'nonce': nonce, 'ciphertext': ciphertext}, max_response_bytes=MAX_WIRE)
        except Exception:
            raise ListError('shared_sync_unconfirmed') from None
        try:
            raw = peer_box.open_sealed(friends.messaging_private, relation['messaging_pub'], wire['nonce'], wire['ciphertext'], info=CHANNEL)
            result = json.loads(raw)
            if len(raw) > MAX_WIRE or result['version'] != WIRE or result['request_id'] != request_id or result['relationship_id'] != relation['relationship_id']:
                raise ValueError
        except Exception:
            raise ListError('shared_response_invalid') from None
        self.active(relation['relationship_id'], peer)
        if 'error' in result:
            raise ListError(result['error'])
        return result['result']

    def respond(self, wire, relation):
        friends = self.friends()
        relation = self.active(relation['relationship_id'], relation['peer_id'])
        try:
            raw = peer_box.open_sealed(friends.messaging_private, relation['messaging_pub'], wire['nonce'], wire['ciphertext'], info=CHANNEL)
            request = json.loads(raw)
            identity(request['request_id'])
            if len(raw) > 8192 or request['version'] != WIRE or request['relationship_id'] != relation['relationship_id'] or request['from'] != relation['peer_id'] or request['to'] != self.peer:
                raise ValueError
        except Exception:
            raise ListError('shared_request_invalid') from None
        result = {'version': WIRE, 'request_id': request['request_id'], 'relationship_id': relation['relationship_id']}
        def change(data):
            if request['action'] == 'invitations':
                return [{'id': row['id'], 'title': row['title']} for row in data['lists'].values()
                        if row['owner'] == self.peer and row['friend'] == relation['peer_id'] and row['relationship_id'] == relation['relationship_id'] and row['status'] in {'invited', 'active'}]
            row = self.owner_row(data, request.get('id'), relation['peer_id'], active=False)
            if row['relationship_id'] != relation['relationship_id']:
                raise ListError('shared_list_unavailable')
            if request['action'] == 'accept':
                if row['status'] == 'closed':
                    raise ListError('shared_list_inactive')
                if row['status'] == 'invited':
                    row['status'] = 'active'
                    row['revision'] += 1
                return self.snapshot(row)
            if request['action'] == 'snapshot':
                return self.snapshot(row)
            if request['action'] == 'edit':
                return self.apply(data, row['id'], relation['peer_id'], request.get('operation'))
            raise ListError('shared_request_invalid')
        try:
            result['result'] = self.store.mutate(change)
        except ListError as exc:
            result['error'] = str(exc)
        nonce, ciphertext = peer_box.seal(friends.messaging_private, relation['messaging_pub'], canonical_json(result), info=CHANNEL)
        return {'nonce': nonce, 'ciphertext': ciphertext}

    def discover(self, rid):
        result = self.request(self.active(rid)['peer_id'], 'invitations')
        if not isinstance(result, list) or len(result) > 32:
            raise ListError('shared_response_invalid')
        return [{'id': identity(row['id']), 'title': text(row['title'], 300)} for row in result]

    def accept(self, rid, identifier):
        friend = self.active(rid)
        identity(identifier)
        if len(self.store.read()['lists']) >= 32 and identifier not in self.store.read()['lists']:
            raise ListError('shared_capacity')
        result = self.request(friend['peer_id'], 'accept', id=identifier)
        self.receive(identifier, rid, friend['peer_id'], result)
        return self.listing()

    def receive(self, identifier, rid, owner, result, *, acknowledged=None):
        if not isinstance(result, dict) or result.get('id') != identifier or result.get('relationship_id') != rid or result.get('owner') != owner or result.get('friend') != self.peer:
            raise ListError('shared_response_invalid')
        if type(result.get('revision')) is not int or not 1 <= result['revision'] <= 10**12 or not isinstance(result.get('items'), dict) or len(result['items']) > 256 or result.get('status') not in {'active', 'closed'}:
            raise ListError('shared_response_invalid')
        text(result.get('title'), 300)
        for key, item in result['items'].items():
            identity(key)
            if not isinstance(item, dict) or item.get('id') != key or item.get('added_by') not in {owner, self.peer} or type(item.get('removed')) is not bool:
                raise ListError('shared_response_invalid')
            if not isinstance(item.get('read_by'), dict) or any(peer not in {owner, self.peer} or type(read) is not bool for peer, read in item['read_by'].items()):
                raise ListError('shared_response_invalid')
            if not item['removed']:
                text(item.get('title'), 500)
                parsed = urlsplit(text(item.get('url'), 2048))
                if parsed.scheme not in {'https', 'http'} or not parsed.hostname or parsed.username or parsed.password:
                    raise ListError('shared_response_invalid')
        if not isinstance(result.get('receipts'), dict) or any(peer not in {owner, self.peer} or not isinstance(receipt, dict) or type(receipt.get('sequence')) is not int or not 1 <= receipt['sequence'] <= 10**9 or not isinstance(receipt.get('digest'), str) or not re.fullmatch('[a-f0-9]{64}', receipt['digest']) for peer, receipt in result['receipts'].items()):
            raise ListError('shared_response_invalid')
        def change(data):
            prior = data['lists'].get(identifier)
            if prior and (prior['owner'] != owner or prior['relationship_id'] != rid):
                raise ListError('shared_response_invalid')
            if prior and prior['revision'] > result['revision']:
                return
            pending = deepcopy(prior['pending']) if prior else []
            receipt = result['receipts'].get(self.peer, {})
            if acknowledged:
                if receipt.get('sequence') != acknowledged['sequence'] or receipt.get('digest') != digest(acknowledged):
                    raise ListError('shared_response_invalid')
            # Accept/snapshot responses can confirm a write whose reply was lost.
            # Match the receipt before advancing past any queued operations.
            confirmed = [op for op in pending if op['sequence'] <= receipt.get('sequence', 0)]
            if confirmed:
                latest = confirmed[-1]
                if latest['sequence'] != receipt['sequence'] or digest(latest) != receipt['digest']:
                    raise ListError('shared_response_invalid')
                pending = [op for op in pending if op['sequence'] > receipt['sequence']]
            cancelled = deepcopy(prior.get('cancelled', [])) if prior else []
            if result['status'] == 'closed':
                # Preserve unaccepted edits locally, but never retry a closed list.
                cancelled.extend(pending)
                pending = []
            data['lists'][identifier] = {**self.snapshot(result), 'pending': pending, 'cancelled': cancelled, 'error': '',
                                         'local_operations': deepcopy(prior.get('local_operations', {})) if prior else {}}
        self.active(rid, owner)
        self.store.mutate(change)

    def sync(self, identifier):
        row = self.store.read()['lists'].get(identity(identifier))
        if not row:
            raise ListError('shared_list_unavailable')
        if row['owner'] == self.peer:
            self.active(row['relationship_id'], row['friend'])
            return
        operation = next(iter(row['pending']), None)
        try:
            try:
                result = self.request(row['owner'], 'edit' if operation else 'snapshot', id=identifier,
                                      **({'operation': operation} if operation else {}))
            except ListError as exc:
                if not operation or str(exc) != 'shared_list_inactive':
                    raise
                result = self.request(row['owner'], 'snapshot', id=identifier)
                if not isinstance(result, dict) or result.get('status') != 'closed':
                    raise ListError('shared_response_invalid') from None
                operation = None
            self.receive(identifier, row['relationship_id'], row['owner'], result, acknowledged=operation)
        except Exception:
            def failed(data):
                if identifier in data['lists']:
                    data['lists'][identifier]['error'] = 'Sync unconfirmed. Reconnect or review this friendship.'
            self.store.mutate(failed)
            raise ListError('shared_sync_unconfirmed') from None

    def run_once(self):
        if not self.running.acquire(blocking=False):
            return False
        try:
            rows = [row for row in self.store.read()['lists'].values() if row['owner'] != self.peer and row['status'] == 'active']
            if not rows:
                return False
            row = rows[self.cursor % len(rows)]
            self.cursor += 1
            try:
                self.sync(row['id'])
            except ListError:
                return False
            return True
        finally:
            self.running.release()
