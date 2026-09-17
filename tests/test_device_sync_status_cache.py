import os

import pytest
from test_device_sync_records import bookmark
from test_device_sync_store import put, replica

from rynmesh.device_sync.records import SyncError


def test_status_reuses_only_bounded_projection_and_invalidates_on_local_or_external_changes(tmp_path, monkeypatch):
    store = replica(tmp_path)
    put(store, 'bookmarks', bookmark())
    read = store._read
    calls = []

    def counted():
        calls.append(True)
        return read()

    monkeypatch.setattr(store, '_read', counted)
    first = store.status()
    first['quarantined'].append({'scope': 'fake'})
    for _ in range(10):
        assert store.status() == {'quarantined': [], 'quarantined_count': 0}
    assert len(calls) == 1
    put(store, 'bookmarks', bookmark(False))
    before = len(calls)
    store.status()
    assert len(calls) == before + 1
    external = replica(tmp_path)
    put(external, 'bookmarks', bookmark())
    before = len(calls)
    store.status()
    assert len(calls) == before + 1


def test_same_size_atomic_replacement_restored_timestamp_deletion_and_corruption_are_not_cached(tmp_path):
    store = replica(tmp_path)
    put(store, 'bookmarks', bookmark())
    store.status()
    stat = store.path.stat()
    replacement = store.path.with_suffix('.replacement')
    replacement.write_bytes(b'x' * stat.st_size)
    os.utime(replacement, ns=(stat.st_atime_ns, stat.st_mtime_ns))
    os.replace(replacement, store.path)
    with pytest.raises(SyncError, match='sync_store_unavailable'):
        store.status()
    store.path.unlink()
    assert store.status() == {'quarantined': [], 'quarantined_count': 0}
    store.path.write_text('invalid', encoding='utf-8')
    with pytest.raises(SyncError, match='sync_store_unavailable'):
        store.status()
