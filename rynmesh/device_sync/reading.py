"""Causal reading state embedded in the source store's atomic document.

The source operation and its causal identity commit together. This state is an
outbox as well as a conflict record; eviction from the recent-history list must
not remove it. Network authorization belongs to the device-pairing layer.
"""
from __future__ import annotations

from copy import deepcopy
from urllib.parse import urlsplit, urlunsplit

from . import records
from .records import SyncError

VERSION = 'ryn.reading-sync.v1'
SCOPES = frozenset({'bookmarks', 'reading'})
MAX_ENTITIES = 20000


def scopes(value):
    if not isinstance(value, (list, tuple, set, frozenset)):
        raise SyncError('sync_scope_invalid')
    result = {records.scope_id(item) for item in value}
    if len(result) != len(value) or not result <= SCOPES:
        raise SyncError('sync_scope_invalid')
    return result


def metadata(item):
    result = {'item_id': item['item_id']}
    for key, limit in (('title', 4000), ('source_title', 4000), ('source_id', 4000), ('content_kind', 32), ('link', 4096)):
        result[key] = ''.join(c if ord(c) >= 32 else ' ' for c in str(item.get(key, '')))[:limit]
    parsed = urlsplit(result['link'])
    if parsed.username is not None or parsed.password is not None:
        result['link'] = urlunsplit((parsed.scheme, parsed.netloc.rsplit('@', 1)[-1], parsed.path, parsed.query, parsed.fragment))
    return result


class ReadingState:
    def __init__(self, value):
        if not isinstance(value, dict) or value.get('version') != VERSION:
            raise SyncError('sync_version_unsupported')
        records.actor_id(value.get('actor'))
        scopes(value.get('scopes'))
        if not isinstance(value.get('entities'), dict) or len(value['entities']) > MAX_ENTITIES:
            raise SyncError('sync_capacity_exhausted')
        self.value = value
        for key, entity in value['entities'].items():
            if not isinstance(entity, dict) or not isinstance(entity.get('scope'), str) or entity['scope'] not in SCOPES or not {'id', 'record', 'projected'} <= entity.keys() or key != self.key(entity['scope'], entity['id']):
                raise SyncError('sync_record_invalid')
            if entity['scope'] not in value['scopes']:
                raise SyncError('sync_scope_denied')
            records.validate(entity['scope'], entity['id'], entity['record'])
            records.validate(entity['scope'], entity['id'], entity['projected'])

    @classmethod
    def create(cls, actor):
        records.actor_id(actor)
        return cls({'version': VERSION, 'actor': actor, 'scopes': [], 'entities': {}})

    @staticmethod
    def key(scope, identifier):
        return records.fingerprint([records.scope_id(scope), records.entity_id(identifier)])

    def enable(self, actor, selected, rows):
        if self.value['actor'] != actor:
            raise SyncError('sync_device_identity_changed')
        selected = scopes(selected)
        added = selected - set(self.value['scopes'])
        self.value['scopes'] = sorted(set(self.value['scopes']) | selected)
        for row in rows.values():
            if 'bookmarks' in added and row.get('bookmarked'):
                self.capture(row, 'bookmarks')
            if 'reading' in added and (row.get('open_count') or row.get('progress') or row.get('completed')):
                self.capture(row, 'reading')

    def capture(self, row, scope, *, expected_revision=None):
        if scope not in self.value['scopes']:
            if expected_revision is not None:
                raise SyncError('sync_scope_denied')
            return
        identifier = row['item_id']
        value = {'item': metadata(row['item'])}
        if scope == 'bookmarks':
            value['bookmarked'] = bool(row['bookmarked'])
        else:
            value.update(progress=row['progress'], completed=bool(row['completed']), content_version=row.get('content_version', ''))
        self._write(scope, identifier, value, expected_revision=expected_revision)

    def _write(self, scope, identifier, value, *, expected_revision=None):
        key = self.key(scope, identifier)
        entity = self.value['entities'].get(key, {'scope': scope, 'id': identifier, 'record': records.empty(), 'projected': records.empty()})
        if key not in self.value['entities'] and len(self.value['entities']) >= MAX_ENTITIES:
            raise SyncError('sync_capacity_exhausted')
        current = entity['record']
        if expected_revision is not None:
            if expected_revision != records.fingerprint(current):
                raise SyncError('sync_revision_conflict')
            base = current  # An explicit review can resolve all observed candidates.
        else:
            base = deepcopy(entity['projected'])
            # Retain unseen concurrent candidates during ordinary local reading.
            actor = self.value['actor']
            if current['clock'].get(actor, 0) > base['clock'].get(actor, 0):
                base['clock'][actor] = current['clock'][actor]
                base['heads'] = [head for head in base['heads'] if head['actor'] != actor]
        updated = records.write(scope, identifier, base, self.value['actor'], value)
        merged = records.merge(scope, identifier, current, updated)
        entity = {**entity, 'record': merged, 'projected': updated if records.view(scope, identifier, merged)['conflict'] else merged}
        self.value['entities'][key] = entity

    def clear(self):
        for entity in list(self.value['entities'].values()):
            self._write(entity['scope'], entity['id'], None, expected_revision=records.fingerprint(entity['record']))

    def export(self, selected):
        selected = scopes(selected)
        if not selected <= set(self.value['scopes']):
            raise SyncError('sync_scope_denied')
        return [deepcopy({key: entity[key] for key in ('scope', 'id', 'record')}) for entity in self.value['entities'].values()
                if entity['scope'] in selected]

    def merge(self, rows, selected):
        selected = scopes(selected)
        if not selected <= set(self.value['scopes']):
            raise SyncError('sync_scope_denied')
        receipts, seen = [], set()
        for row in rows:
            if not isinstance(row, dict) or set(row) != {'scope', 'id', 'record'} or records.scope_id(row['scope']) not in selected:
                raise SyncError('sync_scope_denied')
            key = self.key(row['scope'], row['id'])
            if key in seen:
                raise SyncError('sync_batch_invalid')
            seen.add(key)
            prior = self.value['entities'].get(key)
            merged = records.merge(row['scope'], row['id'], (prior or {}).get('record', records.empty()), row['record'])
            projected = (prior or {}).get('projected', records.empty()) if records.view(row['scope'], row['id'], merged)['conflict'] else merged
            self.value['entities'][key] = {**(prior or {}), **row, 'record': merged, 'projected': projected}
            receipts.append({'scope': row['scope'], 'id': row['id'], 'revision': records.fingerprint(row['record'])})
        if len(self.value['entities']) > MAX_ENTITIES:
            raise SyncError('sync_capacity_exhausted')
        return receipts

    def project(self, original):
        """Overlay selected fields only; never overwrite unrelated local history."""
        result = deepcopy(original)
        for entity in self.value['entities'].values():
            scope, identifier = entity['scope'], entity['id']
            current = records.view(scope, identifier, entity['record'])
            projected = records.view(scope, identifier, entity['projected'])
            candidate = next((row['value'] for row in projected['candidates'] if row['value'] is not None), None)
            # Metadata may label a conflict, but must not silently choose its position.
            description = candidate or next((row['value'] for row in current['candidates'] if row['value'] is not None), None)
            active = current['bookmarked'] if scope == 'bookmarks' else candidate is not None and not any(row['value'] is None for row in current['candidates'])
            if identifier not in result and not active and not (current['conflict'] and description):
                continue
            if identifier not in result:
                result[identifier] = {'item_id': identifier, 'item': deepcopy(description['item']), 'bookmarked': False,
                    'progress': 0.0, 'completed': False, 'open_count': 0, 'first_opened_unix': 0.0,
                    'last_opened_unix': 0.0, 'last_activity_unix': 0.0}
            row = result[identifier]
            if scope == 'bookmarks':
                row['bookmarked'] = current['bookmarked']
            elif active:
                row.update(progress=candidate['progress'], completed=candidate['completed'], content_version=candidate['content_version'])
            else:
                row.update(progress=0.0, completed=False)
            row.setdefault('sync_revisions', {})[scope] = current['revision']
            row.setdefault('sync_conflicts', {})[scope] = current['conflict']
            if scope == 'reading':
                # A remote position is readable history without inventing a
                # local open event or a wall-clock timestamp for that event.
                row['sync_reading_available'] = bool(active or current['conflict'] and description)
        return result
