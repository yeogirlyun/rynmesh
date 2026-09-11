"""Durable encrypted replica state, scoped batches, and explicit acknowledgements.

This is the local persistence layer. It performs no discovery or network calls;
the device-pairing layer must authenticate and authorize each batch/receipt.
An import returns receipts only after the complete batch is atomically stored.
"""
from __future__ import annotations

import json
from copy import deepcopy
from pathlib import Path

from cryptography.exceptions import InvalidTag

from ..atomic_io import atomic_write_json, read_json
from ..crypto import canonical_json
from ..file_transactions import file_transaction
from ..services import peer_box
from . import records
from .records import SyncError

VERSION = 'ryn.device-replica.v1'
CHANNEL = b'rynmesh-device-replica-storage-v1'
MAX_PLAINTEXT = 128 * 1024 * 1024
MAX_FILE = 176 * 1024 * 1024
MAX_ENTITIES = 30000
MAX_BATCH = 100
MAX_BATCH_BYTES = 12 * 1024 * 1024


class ReplicaStore:
    def __init__(self, home, *, messaging_key):
        self.path = Path(home) / 'device-sync' / 'replica.json'
        self.lock = self.path.parent / '.replica.lock'
        self.key = messaging_key
        self.pub = peer_box.public_key_b64(messaging_key)
        self.actor = records.fingerprint(self.pub)
        self._validation_cache = set()

    def _read(self):
        if not self.path.exists():
            return {}, {'version': VERSION, 'actor': self.actor, 'records': {}, 'receipts': {}}
        try:
            envelope = read_json(self.path, max_bytes=MAX_FILE)
            if not isinstance(envelope, dict) or envelope.get('version') != VERSION:
                raise SyncError('sync_version_unsupported')
            plain = peer_box.open_sealed(self.key, self.pub, envelope['nonce'], envelope['ciphertext'], info=CHANNEL)
            if len(plain) > MAX_PLAINTEXT:
                raise SyncError('sync_capacity_exhausted')
            data = json.loads(plain)
            if not isinstance(data, dict) or data.get('version') != VERSION:
                raise SyncError('sync_version_unsupported')
            if data.get('actor') != self.actor:
                raise SyncError('sync_device_identity_changed')
            if not isinstance(data.get('records'), dict) or not isinstance(data.get('receipts'), dict):
                raise ValueError
            if len(data['records']) > MAX_ENTITIES:
                raise ValueError
            if len(data['receipts']) > records.MAX_ACTORS:
                raise ValueError
            for device, receipts in data['receipts'].items():
                records.actor_id(device)
                if not isinstance(receipts, dict) or len(receipts) > MAX_ENTITIES:
                    raise ValueError
                for identifier, revision in receipts.items():
                    records.actor_id(identifier)
                    records.actor_id(revision)
            for key, row in data['records'].items():
                if key != self._key(row['scope'], row['id']):
                    raise ValueError
                records.validate_cached(row['scope'], row['id'], row['record'], self._validation_cache, max_entries=MAX_ENTITIES)
            return envelope, data
        except SyncError:
            raise
        except (OSError, ValueError, KeyError, TypeError, InvalidTag):
            raise SyncError('sync_store_unavailable') from None

    @staticmethod
    def _key(scope, identifier):
        return records.fingerprint([records.scope_id(scope), records.entity_id(identifier)])

    def _mutate(self, operation):
        with file_transaction(self.lock):
            envelope, data = self._read()
            before = canonical_json(data)
            result = operation(data)
            if len(data['records']) > MAX_ENTITIES:
                raise SyncError('sync_capacity_exhausted')
            plain = canonical_json(data)
            if len(plain) > MAX_PLAINTEXT:
                raise SyncError('sync_capacity_exhausted')
            if before != plain:
                nonce, ciphertext = peer_box.seal(self.key, self.pub, plain, info=CHANNEL)
                atomic_write_json(self.path, {**envelope, 'version': VERSION, 'nonce': nonce, 'ciphertext': ciphertext}, max_bytes=MAX_FILE)
            return deepcopy(result)

    def read(self, scope, identifier):
        key = self._key(scope, identifier)
        with file_transaction(self.lock):
            _, data = self._read()
            record = data['records'].get(key, {}).get('record', records.empty())
            return records.view(scope, identifier, record)

    def write(self, scope, identifier, value, *, expected_revision, erase=False):
        return self.write_many([{'scope': scope, 'id': identifier, 'value': value, 'expected_revision': expected_revision, 'erase': erase}])[0]

    def write_many(self, changes):
        if not isinstance(changes, list) or not 0 < len(changes) <= MAX_BATCH:
            raise SyncError('sync_batch_limit')

        def change(data):
            result, seen = [], set()
            for entry in changes:
                scope, identifier = entry['scope'], entry['id']
                key = self._key(scope, identifier)
                if key in seen:
                    raise SyncError('sync_batch_invalid')
                seen.add(key)
                previous = data['records'].get(key, {}).get('record', records.empty())
                if entry['expected_revision'] != records.fingerprint(previous):
                    raise SyncError('sync_revision_conflict')
                record = records.write(scope, identifier, previous, self.actor, entry['value'], erase=entry.get('erase', False))
                data['records'][key] = {**data['records'].get(key, {}), 'scope': scope, 'id': identifier, 'record': record}
                result.append(records.view(scope, identifier, record))
            return result
        return self._mutate(change)

    @staticmethod
    def scopes(scopes):
        if not isinstance(scopes, (list, tuple, set, frozenset)):
            raise SyncError('sync_scope_invalid')
        selected = {records.scope_id(scope) for scope in scopes}
        if len(selected) != len(scopes):
            raise SyncError('sync_scope_invalid')
        return selected

    def pending(self, device, scopes):
        """Return bounded unacknowledged snapshots for selected scopes only."""
        records.actor_id(device)
        selected = self.scopes(scopes)
        with file_transaction(self.lock):
            _, data = self._read()
            receipts = data['receipts'].get(device, {})
            rows = [row for key, row in sorted(data['records'].items())
                    if row['scope'] in selected and receipts.get(key) != records.fingerprint(row['record'])]
            batch, size = [], 2
            for row in rows[:MAX_BATCH]:
                wire = {key: row[key] for key in ('scope', 'id', 'record')}
                row_size = len(canonical_json(wire)) + bool(batch)
                if size + row_size > MAX_BATCH_BYTES:
                    break
                batch.append(deepcopy(wire))
                size += row_size
            return {'records': batch, 'pending': len(rows)}

    def receive(self, rows, *, scopes):
        selected = self.scopes(scopes)
        if not isinstance(rows, list) or len(rows) > MAX_BATCH or len(canonical_json(rows)) > MAX_BATCH_BYTES:
            raise SyncError('sync_batch_limit')
        return self._receive(rows, selected)

    def reconcile_source(self, rows, *, scopes):
        """Internal source snapshot import, not an unbounded network endpoint.

        The source outbox is already durable. Reconcile it in one transaction so
        restart recovery does not repeatedly rewrite the whole replica per row.
        Only the source adapters may call this; network input uses receive().
        """
        selected = self.scopes(scopes)
        if not isinstance(rows, list) or len(rows) > MAX_ENTITIES or len(canonical_json(rows)) > MAX_PLAINTEXT:
            raise SyncError('sync_capacity_exhausted')
        return self._receive(rows, selected)

    def _receive(self, rows, selected):
        def change(data):
            receipts, seen = [], set()
            for row in rows:
                if not isinstance(row, dict) or set(row) != {'scope', 'id', 'record'} or records.scope_id(row['scope']) not in selected:
                    raise SyncError('sync_scope_denied')
                key = self._key(row['scope'], row['id'])
                if key in seen:
                    raise SyncError('sync_batch_invalid')
                seen.add(key)
                previous = data['records'].get(key, {}).get('record', records.empty())
                # _read validated the previous record. Identical canonical
                # bytes need no causal merge; changed input still takes the
                # complete validation/merge path, including operation clashes.
                merged = previous if canonical_json(previous) == canonical_json(row['record']) else records.merge(row['scope'], row['id'], previous, row['record'])
                data['records'][key] = {**data['records'].get(key, {}), **row, 'record': merged}
                receipts.append({'scope': row['scope'], 'id': row['id'], 'revision': records.fingerprint(row['record'])})
            return receipts
        return self._mutate(change)

    def acknowledge(self, device, receipts, *, scopes):
        records.actor_id(device)
        selected = self.scopes(scopes)
        if not isinstance(receipts, list) or len(receipts) > MAX_BATCH:
            raise SyncError('sync_batch_limit')

        def change(data):
            if device not in data['receipts'] and len(data['receipts']) >= records.MAX_ACTORS:
                raise SyncError('sync_device_limit')
            saved = data['receipts'].setdefault(device, {})
            confirmed, seen = 0, set()
            for receipt in receipts:
                if not isinstance(receipt, dict) or set(receipt) != {'scope', 'id', 'revision'} or records.scope_id(receipt['scope']) not in selected:
                    raise SyncError('sync_scope_denied')
                key = self._key(receipt['scope'], receipt['id'])
                if key in seen:
                    raise SyncError('sync_batch_invalid')
                seen.add(key)
                records.actor_id(receipt['revision'])  # SHA256, the same bounded hexadecimal shape.
                row = data['records'].get(key)
                if not row:
                    raise SyncError('sync_receipt_invalid')
                # A late acknowledgement cannot mark a newer local value synced.
                if records.fingerprint(row['record']) == receipt['revision']:
                    saved[key] = receipt['revision']
                    confirmed += 1
            return {'acknowledged': confirmed, 'outdated': len(receipts) - confirmed}
        return self._mutate(change)

    def forget_device(self, device):
        """Pairing removal/reset must discard old acknowledgements before reuse."""
        records.actor_id(device)
        return self._mutate(lambda data: {'removed': data['receipts'].pop(device, None) is not None})
