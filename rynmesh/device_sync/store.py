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
# Errors that describe one row rather than the batch carrying it. A row raising
# one of these is skipped and reported; every other code stays a batch error, so
# a protocol violation can never be downgraded to a per-row note.
PER_ROW = frozenset({'sync_dot_conflict', 'sync_device_limit', 'sync_value_invalid',
                     'sync_item_invalid', 'sync_item_link_invalid'})


class ReplicaStore:
    def __init__(self, home, *, messaging_key):
        self.path = Path(home) / 'device-sync' / 'replica.json'
        self.lock = self.path.parent / '.replica.lock'
        self.key = messaging_key
        self.pub = peer_box.public_key_b64(messaging_key)
        self.actor = records.fingerprint(self.pub)
        self._validation_cache = set()
        self._status_cache = None

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
            self._quarantine(data)
            return envelope, data
        except SyncError:
            raise
        except (OSError, ValueError, KeyError, TypeError, InvalidTag):
            raise SyncError('sync_store_unavailable') from None

    @staticmethod
    def _quarantine(data):
        """Validate the optional section naming rows this replica could not merge.

        Each entry carries its own scope and ID so a row rejected on its first
        import - one this replica holds no record for - stays projectable.
        """
        section = data.get('quarantine', {})
        if not isinstance(section, dict) or len(section) > MAX_ENTITIES:
            raise ValueError
        for key, entry in section.items():
            if not isinstance(entry, dict) or set(entry) != {'scope', 'id', 'code', 'revision'} or entry['code'] not in PER_ROW:
                raise ValueError
            if key != records.entity_key(entry['scope'], entry['id']):
                raise ValueError
            records.actor_id(entry['revision'])

    @staticmethod
    def _key(scope, identifier):
        return records.entity_key(scope, identifier)

    def _mutate(self, operation):
        with file_transaction(self.lock):
            envelope, data = self._read()
            before = canonical_json(data)
            result = operation(data)
            if len(data['records']) > MAX_ENTITIES or len(data.get('quarantine', {})) > MAX_ENTITIES:
                raise SyncError('sync_capacity_exhausted')
            plain = canonical_json(data)
            if len(plain) > MAX_PLAINTEXT:
                raise SyncError('sync_capacity_exhausted')
            if before != plain:
                nonce, ciphertext = peer_box.seal(self.key, self.pub, plain, info=CHANNEL)
                atomic_write_json(self.path, {**envelope, 'version': VERSION, 'nonce': nonce, 'ciphertext': ciphertext}, max_bytes=MAX_FILE)
                self._status_cache = None
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
            return self._pending(data, device, selected)

    def _pending(self, data, device, selected):
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
        selected = self._source_scope(rows, scopes)
        # A local source cannot be answered with a receipt, so a row it cannot
        # merge is quarantined instead: the import stays a no-op until the row
        # changes, and status() names it for the owner.
        return self._mutate(lambda data: self._merge_rows(data, rows, selected, collect_receipts=False))

    def _source_scope(self, rows, scopes):
        selected = self.scopes(scopes)
        if not isinstance(rows, list) or len(rows) > MAX_ENTITIES or len(canonical_json(rows)) > MAX_PLAINTEXT:
            raise SyncError('sync_capacity_exhausted')
        return selected

    def source_pending(self, device, rows, *, scopes):
        """Import a durable source and derive its pending batch in one transaction."""
        records.actor_id(device)
        selected = self._source_scope(rows, scopes)

        def change(data):
            self._merge_rows(data, rows, selected, collect_receipts=False)
            return self._pending(data, device, selected)
        return self._mutate(change)

    def source_acknowledge(self, device, rows, receipts, *, scopes):
        """Check receipts against current source values in the same replica commit."""
        records.actor_id(device)
        selected = self._source_scope(rows, scopes)
        if not isinstance(receipts, list) or len(receipts) > MAX_BATCH:
            raise SyncError('sync_batch_limit')

        def change(data):
            self._merge_rows(data, rows, selected, collect_receipts=False)
            return self._acknowledge(data, device, receipts, selected)
        return self._mutate(change)

    def _receive(self, rows, selected):
        return self._mutate(lambda data: self._merge_rows(data, rows, selected))

    def _merge_rows(self, data, rows, selected, *, collect_receipts=True):
        receipts, seen = [], set()
        for row in rows:
            if not isinstance(row, dict) or set(row) != {'scope', 'id', 'record'} or records.scope_id(row['scope']) not in selected:
                raise SyncError('sync_scope_denied')
            key = self._key(row['scope'], row['id'])
            if key in seen:
                raise SyncError('sync_batch_invalid')
            seen.add(key)
            previous = data['records'].get(key, {}).get('record', records.empty())
            # _read validated the previous record. Identical canonical bytes
            # need no merge; changed input retains full validation.
            same = canonical_json(previous) == canonical_json(row['record'])
            try:
                merged = previous if same else records.merge(row['scope'], row['id'], previous, row['record'])
            except SyncError as exc:
                # One unmergeable row is skipped and reported, never merged and
                # never counted as merged. The rest of the batch still commits,
                # so a single bad row cannot stall its scope forever.
                if str(exc) not in PER_ROW:
                    raise
                if collect_receipts:
                    # The receipt branch serves ReplicaStore.receive, the
                    # replica-only import; production wire batches answer
                    # through transfer._receive against the source bridges.
                    receipts.append({'scope': row['scope'], 'id': row['id'],
                                     'revision': records.fingerprint(row['record']), 'rejected': str(exc)})
                else:
                    data.setdefault('quarantine', {})[key] = {'scope': row['scope'], 'id': row['id'],
                                                              'code': str(exc), 'revision': records.fingerprint(row['record'])}
                continue
            data['records'][key] = {**data['records'].get(key, {}), **row, 'record': merged}
            data.get('quarantine', {}).pop(key, None)
            if collect_receipts:
                receipts.append({'scope': row['scope'], 'id': row['id'], 'revision': records.fingerprint(row['record'])})
        if not collect_receipts:
            self._evict(data, selected, seen)
        if 'quarantine' in data and not data['quarantine']:
            data.pop('quarantine')
        return receipts

    @staticmethod
    def _evict(data, selected, seen):
        """Drop quarantined rows the source no longer holds.

        A collect_receipts=False import is the whole source snapshot for the
        selected scopes, so a quarantined key missing from it was deleted
        locally and must not be reported or retained forever.
        """
        section = data.get('quarantine', {})
        for key in [key for key, entry in section.items() if entry['scope'] in selected and key not in seen]:
            del section[key]

    def status(self):
        """Bounded projection of the rows this replica could not merge locally.

        The listing is capped so one status response cannot carry the whole
        section, but the count is the true total: the owner must be told how
        many rows are held back, not only how many fit in the projection.
        """
        with file_transaction(self.lock):
            try:
                stat = self.path.stat()
                signature = (stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns)
            except FileNotFoundError:
                signature = None
            except OSError:
                raise SyncError('sync_store_unavailable') from None
            if self._status_cache is not None and self._status_cache[0] == signature:
                return deepcopy(self._status_cache[1])
            _, data = self._read()
            rows = [{'scope': entry['scope'], 'id': entry['id'], 'code': entry['code']}
                    for _, entry in sorted(data.get('quarantine', {}).items())]
            result = {'quarantined': rows[:MAX_BATCH], 'quarantined_count': len(rows)}
            self._status_cache = (signature, result)
            return deepcopy(result)

    def acknowledge(self, device, receipts, *, scopes):
        records.actor_id(device)
        selected = self.scopes(scopes)
        if not isinstance(receipts, list) or len(receipts) > MAX_BATCH:
            raise SyncError('sync_batch_limit')

        return self._mutate(lambda data: self._acknowledge(data, device, receipts, selected))

    def _acknowledge(self, data, device, receipts, selected):
        if device not in data['receipts'] and len(data['receipts']) >= records.MAX_ACTORS:
            raise SyncError('sync_device_limit')
        saved = data['receipts'].setdefault(device, {})
        confirmed, seen = 0, set()
        for receipt in receipts:
            # A row the peer answered but could not merge carries the code that
            # refused it. It is stored exactly like an accepted row, so the same
            # bytes are not sent again until the local record changes; the sender
            # keeps the code for the owner (transfer._record).
            if not isinstance(receipt, dict) or set(receipt) - {'rejected'} != {'scope', 'id', 'revision'} \
                    or records.scope_id(receipt['scope']) not in selected \
                    or 'rejected' in receipt and not (isinstance(receipt['rejected'], str) and receipt['rejected'] in PER_ROW):
                raise SyncError('sync_scope_denied')
            key = self._key(receipt['scope'], receipt['id'])
            if key in seen:
                raise SyncError('sync_batch_invalid')
            seen.add(key)
            records.actor_id(receipt['revision'])
            row = data['records'].get(key)
            if not row:
                raise SyncError('sync_receipt_invalid')
            # A late acknowledgement cannot mark a newer local value synced.
            if records.fingerprint(row['record']) == receipt['revision']:
                saved[key] = receipt['revision']
                confirmed += 1
        return {'acknowledged': confirmed, 'outdated': len(receipts) - confirmed}

    def forget_device(self, device):
        """Pairing removal/reset must discard old acknowledgements before reuse."""
        records.actor_id(device)
        return self._mutate(lambda data: {'removed': data['receipts'].pop(device, None) is not None})
