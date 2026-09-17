"""Authenticated source-to-source batches and current-policy acknowledgements.

The lock order is pairing -> source -> replica. Source commits and receipts
hold policy stable, but no file lock is held while calling a peer. Each batch
contains one scope so a partial failure in another source cannot acknowledge it.
"""
from __future__ import annotations

import json

from ..crypto import canonical_json
from ..file_transactions import file_transaction
from ..services import peer_box
from . import pair_crypto as crypto
from . import records
from .conversation_bridge import ConversationBridge
from .pairing import policy
from .reading_bridge import ReadingBridge
from .records import SyncError, fingerprint
from .store import MAX_BATCH, MAX_BATCH_BYTES, PER_ROW

PATH = '/api/peer/device-sync/batch'
MAX_WIRE_BYTES = 18 * 1024 * 1024
DATA = 'ryn.device-data.v1'
ACK = 'ryn.device-data-ack.v1'
CHANNEL = b'rynmesh-device-data-v1:'
# Identifiers of refused rows kept past the per-scope id map, so a re-reported
# refusal is recognised instead of counted again. Bounded because the pairing
# file holds every pairing on the device.
OVERFLOW_LIMIT = 1000


class DeviceTransfer:
    def __init__(self, *, pairing, replica, reading, conversations, post_json):
        self.pairing, self.replica = pairing, replica
        self.reading, self.conversations, self.post_json = reading, conversations, post_json
        self.turns = {}

    def _pair(self, pair_id):
        node = self.pairing()
        return node._pair(node.store.snapshot(), pair_id)

    def _guard(self, row, scopes):
        if row['status'] != 'active':
            raise SyncError('sync_device_not_active')
        return self.pairing().authorized(row['id'], remote_actor=row['remote']['actor'], scopes=scopes,
            sender_revision=row['remote_policy']['revision'], receiver_revision=row['local_policy']['revision'])

    @staticmethod
    def _channel(row, *, reply=False):
        # Both the fresh pairing identity and its shared capability bind data.
        return CHANNEL + row['id'].encode() + crypto.decoded(row['secret'], size=32) + (b':ack' if reply else b':batch')

    @staticmethod
    def _epoch(row):
        return fingerprint([policy(row['local_policy']), policy(row['remote_policy'])])

    def _initialize(self, row, scope):
        node = self.pairing()
        epoch = self._epoch(row)
        if row.get('transfer', {}).get('epoch') != epoch:
            # Receipts use the pair ID, never merely the remote actor. Forget
            # before recording the new epoch; interrupted reset safely repeats.
            self.replica.forget_device(row['id'])
            for previous in node.store.snapshot()['pairs'].values():
                if previous['status'] == 'revoked':
                    self.replica.forget_device(previous['id'])
            def reset(data):
                current = node._pair(data, row['id'])
                current['transfer'] = {'epoch': epoch, 'confirmed': {}, 'received': {}, 'errors': {}}
            node.store.mutate(reset)
        if scope == 'conversations':
            source = self.conversations()
            source.enable_sync()
            return ConversationBridge(source, self.replica)
        source = self.reading()
        # This bridge is used only inside the current pairing-policy guard.
        # Combine approved opt-in checks with the source read for the operation.
        return ReadingBridge(source, self.replica, ensure_enabled=True)

    @staticmethod
    def _pending(bridge, pair_id, scope):
        return bridge.pending(pair_id) if scope == 'conversations' else bridge.pending(pair_id, [scope])

    @staticmethod
    def _deliver(bridge, rows, scope):
        return bridge.receive(rows) if scope == 'conversations' else bridge.receive(rows, scopes=[scope])

    @classmethod
    def _receive(cls, bridge, rows, scope):
        """One unmergeable row must not stall every other row in its scope.

        This fallback is the production mechanism for that guarantee: a wire
        batch is merged by the source stores through their bridges, and those
        stores still raise per-row (records.merge inside reading.SyncState.merge
        and ConversationState.merge), so the only way to keep the rest of the
        batch moving is to retry it a row at a time. ReplicaStore._merge_rows
        has its own row-level skip for the replica-only import path.

        The batch is delivered as a whole; only a per-row merge failure falls
        back to a commit per row, so the rows that do merge still land and the
        row that cannot is named in the signed receipt instead of retried
        forever. A batch-level error still fails the whole batch.
        """
        if not rows:
            return []  # An all-rejected batch must not write to the source.
        try:
            return cls._deliver(bridge, rows, scope)
        except SyncError as exc:
            if str(exc) not in PER_ROW:
                raise
        receipts = []
        for row in rows:
            try:
                receipts.extend(cls._deliver(bridge, [row], scope))
            except SyncError as exc:
                if str(exc) not in PER_ROW:
                    raise
                receipts.append({'scope': row['scope'], 'id': row['id'],
                                 'revision': fingerprint(row['record']), 'rejected': str(exc)})
        return receipts

    @staticmethod
    def _acknowledge(bridge, pair_id, receipts, scope):
        return bridge.acknowledge(pair_id, receipts) if scope == 'conversations' else bridge.acknowledge(pair_id, receipts, scopes=[scope])

    def _seal(self, row, payload, *, reply=False):
        node = self.pairing()
        box = crypto.seal(payload, private_key=node.identity_private, messaging_key=node.messaging_key,
                          sender=row['local'], receiver=row['remote'], channel=self._channel(row, reply=reply))
        wire = {'pair_id': row['id'], 'box': box}
        if len(canonical_json(wire)) > MAX_WIRE_BYTES:
            raise SyncError('sync_batch_limit')
        return wire

    def _open(self, wire, *, reply=False):
        if not isinstance(wire, dict) or set(wire) != {'pair_id', 'box'} or len(canonical_json(wire)) > MAX_WIRE_BYTES:
            raise SyncError('sync_batch_invalid')
        row = self._pair(wire['pair_id'])
        if row['status'] != 'active':
            raise SyncError('sync_device_not_active')
        node = self.pairing()
        proof, sender = crypto.open_wire(wire['box'], messaging_key=node.messaging_key,
            allow_loopback=node.allow_loopback, channel=self._channel(row, reply=reply), max_bytes=MAX_WIRE_BYTES)
        value = proof.payload
        if (not node._same_identity(sender, row['remote']) or value.get('receiver') != row['local']['peer_id']
                or value.get('pair_id') != row['id']):
            raise SyncError('sync_batch_invalid')
        return row, value

    @staticmethod
    def _receipts(rows, scope):
        if not isinstance(rows, list) or len(rows) > MAX_BATCH or len(canonical_json(rows)) > MAX_BATCH_BYTES:
            raise SyncError('sync_batch_limit')
        receipts, seen = [], set()
        for row in rows:
            if not isinstance(row, dict) or set(row) != {'scope', 'id', 'record'} or row['scope'] != scope:
                raise SyncError('sync_scope_denied')
            try:
                records.validate(scope, row['id'], row['record'])
                rejected = ''
            except SyncError as exc:
                # A row this device cannot accept is reported as rejected, not
                # merged; only a batch-level error still fails the batch.
                if str(exc) not in PER_ROW:
                    raise
                rejected = str(exc)
            if row['id'] in seen:
                raise SyncError('sync_batch_invalid')
            seen.add(row['id'])
            receipt = {'scope': scope, 'id': row['id'], 'revision': fingerprint(row['record'])}
            receipts.append({**receipt, 'rejected': rejected} if rejected else receipt)
        return receipts

    def prepare(self, pair_id, scope):
        records.scope_id(scope)
        row = self._pair(pair_id)
        with self._guard(row, [scope]) as current:
            bridge = self._initialize(current, scope)
            batch = self._pending(bridge, pair_id, scope)
            payload = {'kind': DATA, 'sender': current['local'], 'receiver': current['remote']['peer_id'],
                'pair_id': pair_id, 'scope': scope, 'sender_revision': current['local_policy']['revision'],
                'receiver_revision': current['remote_policy']['revision'], 'records': batch['records']}
            return self._seal(current, payload)

    def receive(self, wire):
        row, value = self._open(wire)
        if set(value) != {'kind', 'sender', 'receiver', 'pair_id', 'scope', 'sender_revision', 'receiver_revision', 'records'} or value['kind'] != DATA:
            raise SyncError('sync_batch_invalid')
        scope = records.scope_id(value['scope'])
        expected = self._receipts(value['records'], scope)
        accepted = [entry for entry, receipt in zip(value['records'], expected, strict=True) if 'rejected' not in receipt]
        node = self.pairing()
        with node.authorized(row['id'], remote_actor=row['remote']['actor'], scopes=[scope],
                             sender_revision=value['sender_revision'], receiver_revision=value['receiver_revision']) as current:
            bridge = self._initialize(current, scope)
            merged = self._receive(bridge, accepted, scope)
            # Never report a row as merged when it was not: the source must
            # answer for exactly the rows it was given, in the same order.
            if [receipt['id'] for receipt in merged] != [entry['id'] for entry in accepted]:
                raise SyncError('sync_receipt_invalid')
            delivered = iter(merged)
            receipts = [receipt if 'rejected' in receipt else next(delivered) for receipt in expected]
            self._record(current, scope, received=True)
            return self._seal(current, {'kind': ACK, 'sender': current['local'], 'receiver': current['remote']['peer_id'],
                'pair_id': row['id'], 'scope': scope, 'sender_revision': current['local_policy']['revision'],
                'receiver_revision': current['remote_policy']['revision'], 'request_hash': fingerprint(wire),
                'receipts': receipts}, reply=True)

    @staticmethod
    def _rejected(state, scope, answers):
        """Keep the rows this peer refused and drop the ones it has now merged.

        The id map is bounded to one batch per scope, so a refusal past that cap
        keeps only its identifier, in `overflow`. Identity is what makes the
        count truthful: counting a bare refusal would count the same row again
        on every cycle that re-reports it, and an acceptance could then settle a
        refusal that was never counted. `overflow` is itself capped; past that,
        rows stop being counted at all and `overflow_truncated` says so, rather
        than letting one pairing row grow without bound in the pairing file.
        """
        rejected = state.setdefault('rejected', {})
        entry = rejected.get(scope) or {}
        rows = entry.get('rows', {})
        overflow = set(entry.get('overflow', ()))
        truncated = bool(entry.get('overflow_truncated'))
        count = entry.get('count', 0)
        for identifier, code in answers.items():
            if not code:
                # An accepted row settles exactly the one refusal it names,
                # whether that refusal was named in `rows` or only counted.
                if rows.pop(identifier, None) is not None:
                    count -= 1
                elif identifier in overflow:
                    overflow.discard(identifier)
                    count -= 1
            elif identifier in rows:
                rows[identifier] = code
            elif identifier in overflow:
                pass  # Already counted; the cap left no room for its code.
            elif len(rows) < MAX_BATCH:
                rows[identifier] = code
                count += 1
            elif len(overflow) < OVERFLOW_LIMIT:
                overflow.add(identifier)
                count += 1
            else:
                truncated = True
        count = min(max(count, 0), len(rows) + len(overflow))
        if count or truncated:
            rejected[scope] = {'count': count, 'rows': rows,
                               **({'overflow': sorted(overflow)} if overflow else {}),
                               **({'overflow_truncated': True} if truncated else {})}
        else:
            rejected.pop(scope, None)
        if not rejected:
            state.pop('rejected')

    def _record(self, row, scope, *, received=False, error='', answers=None):
        node = self.pairing()
        def update(data):
            current = node._pair(data, row['id'])
            if current['status'] != 'active' or self._epoch(current) != self._epoch(row):
                return
            state = current.setdefault('transfer', {'epoch': self._epoch(row), 'confirmed': {}, 'received': {}, 'errors': {}})
            if answers is not None:
                self._rejected(state, scope, answers)
            if error:
                state.setdefault('errors', {})[scope] = error
            elif not received:
                state.setdefault('errors', {}).pop(scope, None)
            if not error:
                state['received' if received else 'confirmed'][scope] = int(node.clock())
        node.store.mutate(update)

    def acknowledge(self, wire, response):
        # Validate the exact sent records as well as the peer's signed response.
        # The outgoing box cannot be opened with _open (its sender is local).
        row, value = self._open(response, reply=True)
        if wire.get('pair_id') != row['id'] or value.get('request_hash') != fingerprint(wire):
            raise SyncError('sync_receipt_invalid')
        return self._accept_receipt(row, value, self._outgoing(row, wire))

    def _outgoing(self, row, wire):
        node = self.pairing()
        box = wire['box']
        plain = peer_box.open_sealed(node.messaging_key, row['remote']['messaging_pub'], box['nonce'], box['ciphertext'], info=self._channel(row))
        proof = crypto.verify(json.loads(plain))
        sent = proof.payload
        if (proof.public_key != row['local']['peer_id'] or sent.get('sender') != row['local']
                or sent.get('receiver') != row['remote']['peer_id'] or sent.get('kind') != DATA
                or sent.get('pair_id') != row['id']):
            raise SyncError('sync_batch_invalid')
        return sent

    @staticmethod
    def _answers(receipts, expected):
        """Check the peer's receipts and name the rows it could not merge.

        A rejected row is answered for the exact revision that was sent, so the
        only difference from an accepted receipt is that one marker: every other
        field must still equal what this device signed and sent. Removing the
        marker therefore leaves the comparison this sender has always made.
        """
        if not isinstance(receipts, list):
            raise SyncError('sync_receipt_invalid')
        codes = []
        for receipt in receipts:
            if not isinstance(receipt, dict):
                raise SyncError('sync_receipt_invalid')
            code = receipt.get('rejected', '')
            if 'rejected' in receipt and not (isinstance(code, str) and code in PER_ROW):
                raise SyncError('sync_receipt_invalid')
            codes.append(code)
        plain = [{key: item for key, item in receipt.items() if key != 'rejected'} for receipt in receipts]
        if canonical_json(plain) != canonical_json([{key: item for key, item in receipt.items() if key != 'rejected'}
                                                    for receipt in expected]):
            raise SyncError('sync_receipt_invalid')
        return {receipt['id']: code for receipt, code in zip(plain, codes, strict=True)}

    def _accept_receipt(self, row, value, sent):
        if set(value) != {'kind', 'sender', 'receiver', 'pair_id', 'scope', 'sender_revision', 'receiver_revision', 'request_hash', 'receipts'} or value['kind'] != ACK:
            raise SyncError('sync_receipt_invalid')
        scope = records.scope_id(value['scope'])
        if (scope != sent['scope'] or value['sender_revision'] != sent['receiver_revision']
                or value['receiver_revision'] != sent['sender_revision']):
            raise SyncError('sync_receipt_invalid')
        answers = self._answers(value['receipts'], self._receipts(sent['records'], scope))
        node = self.pairing()
        with node.authorized(row['id'], remote_actor=row['remote']['actor'], scopes=[scope],
                             sender_revision=value['sender_revision'], receiver_revision=value['receiver_revision']) as current:
            bridge = self._initialize(current, scope)
            result = self._acknowledge(bridge, row['id'], value['receipts'], scope)
            self._record(current, scope, answers=answers)
            return result

    def send(self, pair_id, scope):
        wire = self.prepare(pair_id, scope)
        row = self._pair(pair_id)
        try:
            # Recheck permission immediately before starting network IO. An
            # already in-flight body cannot be recalled by local cancellation.
            sent = self._outgoing(row, wire)
            with self.pairing().authorized(pair_id, remote_actor=row['remote']['actor'], scopes=[scope],
                    sender_revision=sent['receiver_revision'], receiver_revision=sent['sender_revision']):
                pass
            response = self.post_json(row['remote']['endpoint'], PATH, wire)
            return self.acknowledge(wire, response)
        except Exception:
            with file_transaction(self.pairing().store.lock):
                self._record(row, scope, error='sync_transfer_unconfirmed')
            raise

    def run_once(self, pair_id):
        row = self._pair(pair_id)
        scopes = self.pairing().public(row)['effective_scopes']
        if not scopes:
            return False
        index = self.turns.get(pair_id, 0) % len(scopes)
        self.turns[pair_id] = index + 1
        return bool(self.send(pair_id, scopes[index])['acknowledged'])

    def status(self, pair_id):
        row = self._pair(pair_id)
        public = self.pairing().public(row)
        base = {'pending': None, 'last_success_at': None, 'error_code': '', 'conflicts': 0, 'rejected_by_peer': {}, 'rejected_details_truncated': False}
        if public['status'] != 'active':
            return {**base, 'state': 'unpaired'}
        if public['paused'] or public['remote_paused']:
            return {**base, 'state': 'paused'}
        scopes = public['effective_scopes']
        if not scopes:
            return {**base, 'state': 'no_scope'}
        try:
            with self._guard(row, scopes) as current:
                pending, conflicts = 0, 0
                for scope in scopes:
                    bridge = self._initialize(current, scope)
                    state = bridge.status(pair_id) if scope == 'conversations' else bridge.status(pair_id, [scope])
                    pending += state['pending']
                    conflicts += state['conflicts']
                    current = self._pair(pair_id)  # _initialize may have reset the epoch.
                saved = current.get('transfer', {})
                stamps = saved.get('confirmed', {})
                last = min(stamps[scope] for scope in scopes) if all(scope in stamps for scope in scopes) else None
                error = next((saved.get('errors', {}).get(scope) for scope in scopes if saved.get('errors', {}).get(scope)), '')
                # A confirmed scope reports the rows the peer accepted, so this
                # count is the only signal for the rows it refused: they are
                # settled here and go again when they change on this device.
                refused = {scope: saved.get('rejected', {}).get(scope, {}).get('count', 0) for scope in scopes}
                truncated = bool(saved.get('rejected_truncated')) or any(
                    saved.get('rejected', {}).get(scope, {}).get('overflow_truncated') for scope in scopes)
                state = 'conflict' if conflicts else 'waiting' if error else 'pending' if pending or last is None else 'confirmed'
                return {'state': state, 'pending': pending, 'last_success_at': last, 'error_code': error, 'conflicts': conflicts,
                        'rejected_by_peer': {scope: count for scope, count in refused.items() if count},
                        'rejected_details_truncated': truncated}
        except Exception:
            return {**base, 'state': 'failed', 'error_code': 'sync_storage_unavailable'}
