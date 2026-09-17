import math
import re
import threading
import time
import uuid
from copy import deepcopy

from ..crypto import canonical_json
from ..device_sync import pair_crypto as crypto
from ..device_sync.records import fingerprint
from .adapters import PHASES
from .store import ErasureError

PATH = '/api/peer/device-erasure'
KIND = 'ryn.device-erasure.v1'
CHANNEL = b'rynmesh-device-erasure-v1:'
LIMIT = 64 * 1024
STATES = {'awaiting_owner', 'approved', 'partial', 'confirmed', 'rejected'}


def token(value, size=64):
    if not isinstance(value, str) or not re.fullmatch('[a-f0-9]{' + str(size) + '}', value):
        raise ErasureError('erasure_request_invalid')
    return value


class DeviceErasure:
    def __init__(self, store, pairing, adapters, *, clock=time.time):
        self.store, self.pairing, self.adapters, self.clock = store, pairing, adapters, clock
        self.running = threading.Lock()
        self.cursor = 0

    def pair(self, identifier, epoch=None):
        node = self.pairing()
        row = node._pair(node.store.snapshot(), token(identifier))
        if row['status'] != 'active':
            raise ErasureError('erasure_device_inactive')
        if epoch is not None and self.epoch(row) != epoch:
            raise ErasureError('erasure_policy_changed')
        return row

    @staticmethod
    def epoch(row):
        return fingerprint({row['local']['actor']: row['local_policy'], row['remote']['actor']: row['remote_policy']})

    @staticmethod
    def channel(row, reply):
        return CHANNEL + row['id'].encode() + crypto.decoded(row['secret'], size=32) + (b':reply' if reply else b':request')

    def seal(self, row, value, *, reply=False):
        node = self.pairing()
        value = {**value, 'kind': KIND, 'sender': row['local'], 'receiver': row['remote']['peer_id'], 'pair_id': row['id']}
        return {'pair_id': row['id'], 'box': crypto.seal(value, private_key=node.identity_private,
            messaging_key=node.messaging_key, sender=row['local'], receiver=row['remote'], channel=self.channel(row, reply))}

    def open(self, wire, *, reply=False):
        if not isinstance(wire, dict) or set(wire) != {'pair_id', 'box'} or len(canonical_json(wire)) > LIMIT:
            raise ErasureError('erasure_request_invalid')
        row = self.pair(wire['pair_id'])
        node = self.pairing()
        proof, sender = crypto.open_wire(wire['box'], messaging_key=node.messaging_key, allow_loopback=node.allow_loopback,
                                        channel=self.channel(row, reply), max_bytes=LIMIT)
        value = proof.payload
        if not node._same_identity(sender, row['remote']) or value.get('receiver') != row['local']['peer_id'] or value.get('pair_id') != row['id'] or value.get('kind') != KIND:
            raise ErasureError('erasure_request_invalid')
        return row, value

    def begin(self, identifier, category, pair_ids):
        token(identifier, 32)
        if category not in PHASES or not isinstance(pair_ids, list) or not 1 <= len(pair_ids) <= 16 or len(set(pair_ids)) != len(pair_ids):
            raise ErasureError('erasure_request_invalid')
        prior = self.store.read()['outgoing'].get(identifier)
        if prior:
            if prior['category'] != category or sorted(prior['targets']) != sorted(pair_ids):
                raise ErasureError('erasure_request_changed')
            return self.public_job(prior)
        targets = {}
        for pair_id in pair_ids:
            row = self.pair(pair_id)
            targets[pair_id] = {'pair_id': pair_id, 'peer_id': row['remote']['peer_id'], 'name': row['remote']['name'],
                                'epoch': self.epoch(row), 'state': 'queued'}
        manifest = fingerprint({'id': identifier, 'category': category,
                                'targets': [{key: target[key] for key in ('pair_id', 'peer_id', 'epoch')} for _, target in sorted(targets.items())]})
        job = {'id': identifier, 'category': category, 'manifest': manifest, 'targets': targets}
        def save(data):
            current = data['outgoing'].get(identifier)
            if current and current['manifest'] != manifest:
                raise ErasureError('erasure_request_changed')
            data['outgoing'].setdefault(identifier, job)
            return self.public_job(data['outgoing'][identifier])
        return self.store.mutate(save)

    @staticmethod
    def public_job(job):
        targets = [{key: value for key, value in row.items() if key in {'pair_id', 'name', 'state', 'completed_at'}} for row in job['targets'].values()]
        return {'id': job['id'], 'category': job['category'], 'targets': targets,
                'remote_confirmed': bool(targets) and all(row['state'] == 'confirmed' for row in targets)}

    def status(self):
        data = self.store.read()
        incoming = []
        for identifier, row in data['incoming'].items():
            value = {key: deepcopy(row[key]) for key in ('category', 'state', 'name')}
            try:
                self.pair(row['pair_id'], row['epoch'])
                value['available'] = True
            except Exception:
                value['available'] = False
            incoming.append({'id': identifier, **value})
        return {'outgoing': [self.public_job(job) for job in data['outgoing'].values()], 'incoming': incoming}

    def receive(self, wire):
        pair, value = self.open(wire)
        if set(value) != {'kind', 'sender', 'receiver', 'pair_id', 'id', 'category', 'manifest', 'epoch', 'nonce'} or value['category'] not in PHASES:
            raise ErasureError('erasure_request_invalid')
        token(value['id'], 32)
        token(value['manifest'])
        token(value['epoch'])
        token(value['nonce'], 32)
        self.pair(pair['id'], value['epoch'])
        identifier = fingerprint([pair['id'], value['id']])
        binding = {key: value[key] for key in ('id', 'category', 'manifest', 'epoch', 'pair_id')}
        def save(data):
            row = data['incoming'].get(identifier)
            if row and any(row[key] != item for key, item in binding.items()):
                raise ErasureError('erasure_request_changed')
            if not row:
                row = {**binding, 'name': pair['remote']['name'], 'state': 'awaiting_owner'}
                data['incoming'][identifier] = row
            # Never run cleanup in a peer handler. Only explicit owner approval
            # calls an adapter; polling returns the durable receipt alone.
            return {key: deepcopy(row[key]) for key in ('state', 'approved_token', 'phases', 'completed_at') if key in row}
        result = self.store.mutate(save)
        pair = self.pair(pair['id'], value['epoch'])
        return self.seal(pair, {'request_hash': fingerprint(wire), **binding, 'nonce': value['nonce'], 'result': result}, reply=True)

    def exchange(self, identifier, pair_id):
        job = self.store.read()['outgoing'].get(token(identifier, 32))
        if not job or pair_id not in job['targets']:
            raise ErasureError('erasure_request_invalid')
        target = job['targets'][pair_id]
        if target['state'] in {'confirmed', 'rejected'}:
            return self.public_job(job)
        try:
            row = self.pair(pair_id, target['epoch'])
            request = {'id': identifier, 'category': job['category'], 'manifest': job['manifest'], 'epoch': target['epoch'], 'nonce': uuid.uuid4().hex}
            wire = self.seal(row, request)
            response = self.pairing().post_json(row['remote']['endpoint'], PATH, wire)
            _, value = self.open(response, reply=True)
            if value.get('request_hash') != fingerprint(wire) or any(value.get(key) != item for key, item in request.items()) or value.get('pair_id') != pair_id:
                raise ErasureError('erasure_receipt_invalid')
            self.pair(pair_id, target['epoch'])
            result = value.get('result')
            if not isinstance(result, dict) or result.get('state') not in STATES:
                raise ErasureError('erasure_receipt_invalid')
            if result['state'] == 'confirmed':
                token(result.get('approved_token'))
                if result.get('phases') != PHASES[job['category']] or type(result.get('completed_at')) not in {int, float} or not math.isfinite(result['completed_at']) or result['completed_at'] < 0:
                    raise ErasureError('erasure_receipt_invalid')
            update = {key: result[key] for key in ('state', 'completed_at') if key in result}
            if result['state'] == 'confirmed':
                update['receipt'] = response
        except Exception as exc:
            update = {'state': 'policy_changed' if isinstance(exc, ErasureError) and str(exc) in {'erasure_policy_changed', 'erasure_device_inactive'} else 'unconfirmed'}
        def save(data):
            current = data['outgoing'][identifier]['targets'][pair_id]
            if current['state'] not in {'confirmed', 'rejected'}:
                current.update(update)
            return self.public_job(data['outgoing'][identifier])
        return self.store.mutate(save)

    def incoming(self, identifier):
        row = self.store.read()['incoming'].get(token(identifier))
        if not row:
            raise ErasureError('erasure_request_invalid')
        self.pair(row['pair_id'], row['epoch'])
        return row

    def preview(self, identifier):
        row = self.incoming(identifier)
        if row['state'] != 'awaiting_owner':
            raise ErasureError('erasure_already_reviewed')
        review = self.adapters.preview(row['category'])
        token(review['review_token'])
        def save(data):
            current = data['incoming'][identifier]
            if current['state'] != 'awaiting_owner':
                raise ErasureError('erasure_already_reviewed')
            current['review'] = review
        self.store.mutate(save)
        return {**review, 'category': row['category'], 'id': identifier}

    def approve(self, identifier, review_token):
        token(review_token)
        row = self.incoming(identifier)
        def decide(data):
            current = data['incoming'][identifier]
            if current['state'] == 'rejected' or (current.get('approved_token') or current.get('review', {}).get('review_token')) != review_token:
                raise ErasureError('erasure_review_changed')
            if current['state'] == 'confirmed':
                return False
            current.update(state='approved', approved_token=review_token)
            return True
        if not self.store.mutate(decide):
            return self.status()
        try:
            self.pair(row['pair_id'], row['epoch'])
            # This local owner-approved plan remains its own cleanup operation.
            # Pair revocation blocks dispatch/receipts, not an atomic local phase
            # already authorized by that owner. No network call runs under locks.
            phases = self.adapters.execute(row['category'], review_token)
            if phases != PHASES[row['category']]:
                raise ErasureError('erasure_local_incomplete')
            self.pair(row['pair_id'], row['epoch'])
            def complete(data):
                current = data['incoming'][identifier]
                if current['state'] != 'rejected':
                    current.update(state='confirmed', phases=phases, completed_at=self.clock())
            self.store.mutate(complete)
        except Exception:
            def partial(data):
                current = data['incoming'][identifier]
                if current['state'] not in {'rejected', 'confirmed'}:
                    current['state'] = 'partial'
            self.store.mutate(partial)
            raise ErasureError('erasure_local_incomplete') from None
        return self.status()

    def reject(self, identifier):
        self.incoming(identifier)
        def change(data):
            row = data['incoming'][identifier]
            if row['state'] == 'confirmed':
                raise ErasureError('erasure_already_confirmed')
            row['state'] = 'rejected'
        self.store.mutate(change)
        return self.status()

    def resume(self, identifier):
        row = self.incoming(identifier)
        return self.approve(identifier, row.get('approved_token'))

    def run_once(self):
        if not self.running.acquire(blocking=False):
            return False
        try:
            rows = [(job['id'], pair_id) for job in self.store.read()['outgoing'].values()
                    for pair_id, target in job['targets'].items() if target['state'] not in {'confirmed', 'rejected', 'policy_changed'}]
            if not rows:
                return False
            row = rows[self.cursor % len(rows)]
            self.cursor += 1
            self.exchange(*row)
            return True
        finally:
            self.running.release()
