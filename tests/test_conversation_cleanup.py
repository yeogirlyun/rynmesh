"""Actual encrypted stores and durable retries for local conversation copies."""
from pathlib import Path

import pytest
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey
from test_ask_history import sample
from test_local_search import document

from rynmesh.ask_ryn.cleanup import STEPS, ConversationCleanup
from rynmesh.ask_ryn.privacy import ConversationPrivacy
from rynmesh.ask_ryn.store import ConversationError, ConversationStore
from rynmesh.device_sync import records
from rynmesh.device_sync.store import ReplicaStore
from rynmesh.local_search.index import LocalSearchIndex, SearchError


def fixture(tmp_path, *, enabled=True, source=None, replica=None, orders=None):
    source = source or ConversationStore(tmp_path / 'ask-ryn', X25519PrivateKey.generate())
    if not source.path.exists():
        source.save(sample(), expected_revision=0)
    replica = replica or ReplicaStore(tmp_path, messaging_key=source.key)
    if enabled:
        source.enable_sync()
        replica.reconcile_source(source.sync_export(), scopes=['conversations'])
    def rows():
        return [document(row['id'], text=' '.join(message['content'] for message in row['messages']), kinds=['chat'])
                for row in source.list()]
    search = LocalSearchIndex(tmp_path / 'local-search', messaging_key=source.key, source=rows)
    search.rebuild(force=True)
    order_calls = [] if orders is None else orders
    job = ConversationCleanup(tmp_path, source=source, replica=replica,
        pairing_lock=tmp_path / 'device-sync' / '.pairings.lock', search=search,
        erase_order_results=lambda ids: order_calls.append(ids))
    return job, order_calls


def test_reviewed_copies_clear_and_restart_retry_keeps_new_work(tmp_path):
    job, calls = fixture(tmp_path)
    backup = job.source.path.with_name('history.json.migrated')
    assert backup.exists()
    orphan = job.source.path.with_name('.history.json.' + 'a' * 32 + '.tmp')
    orphan.write_bytes(job.source.path.read_bytes())
    unrelated = job.source.path.with_name('user-backup.json')
    unrelated.write_bytes(b'leave this file')
    preview = job.preview()
    assert preview['backup_files'] == 2 and preview['identities'] == 1
    result = job.begin(review_token=preview['review_token'])
    assert result['done'] == list(STEPS) and result['local_copies_complete']
    assert result['browser_cleanup_required'] and not result['remote_confirmed']
    assert not backup.exists() and not orphan.exists() and unrelated.read_bytes() == b'leave this file'
    assert job.source.list() == []
    assert job.replica.read('conversations', sample()['id'])['erased']
    assert job.search._read()[1]['documents'] == []
    assert sample()['messages'][0]['content'] not in job.path.read_text()
    assert calls == [[]]
    new = {**sample(), 'id': 'new-chat'}
    job.source.save(new, expected_revision=0)
    restarted, _ = fixture(tmp_path, source=job.source, replica=job.replica, orders=calls)
    assert restarted.begin(review_token=preview['review_token']) == result
    assert restarted.source.get('new-chat')['messages'] == new['messages']
    assert calls == [[]]


def test_replica_only_identity_is_erased_without_opt_in_and_later_opt_in_keeps_barrier(tmp_path):
    job, _ = fixture(tmp_path, enabled=False)
    other = {**sample(), 'id': 'replica-only'}
    record = records.write('conversations', other['id'], records.empty(), 'b' * 64, other)
    stale = [{'scope': 'conversations', 'id': other['id'], 'record': record}]
    job.replica.receive(stale, scopes=['conversations'])
    preview = job.preview()
    assert preview['identities'] == 2
    job.begin(review_token=preview['review_token'])
    assert job.source._read()[1]['version'] == 'ryn.ask-history.v1'
    assert job.replica.read('conversations', other['id'])['erased']
    job.source.enable_sync()
    job.source.sync_receive(stale)
    assert job.source.list() == job.source.sync_conflicts() == []
    assert all(row['record']['erased'] for row in job.source.sync_export())


@pytest.mark.parametrize('change', ['source', 'backup', 'replica'])
def test_stale_review_mutates_no_source_or_journal(tmp_path, change):
    job, _ = fixture(tmp_path)
    preview = job.preview()
    if change == 'source':
        job.source.save_draft('new draft', expected_revision=0)
    elif change == 'backup':
        job.source.path.with_name('history.json.migrated').write_bytes(b'changed backup')
    else:
        row = {**sample(), 'id': 'new-replica'}
        job.replica.receive([{'scope': 'conversations', 'id': row['id'],
            'record': records.write('conversations', row['id'], records.empty(), 'b' * 64, row)}], scopes=['conversations'])
    source_bytes = job.source.path.read_bytes()
    replica_bytes = job.replica.path.read_bytes()
    with pytest.raises(ConversationError, match='ask_privacy_review_changed'):
        job.begin(review_token=preview['review_token'])
    assert job.source.path.read_bytes() == source_bytes and job.replica.path.read_bytes() == replica_bytes
    assert not job.path.exists()


def test_source_commit_then_lost_journal_write_resumes_without_erasing_new_work(tmp_path, monkeypatch):
    job, _ = fixture(tmp_path)
    preview = job.preview()
    save, writes = job._save, []
    def fail_second(envelope, data):
        writes.append(1)
        if len(writes) == 2:
            raise OSError('disk unavailable')
        save(envelope, data)
    monkeypatch.setattr(job, '_save', fail_second)
    with pytest.raises(OSError):
        job.begin(review_token=preview['review_token'])
    assert job.status(preview['review_token'])['done'] == []
    job.source.save({**sample(), 'id': 'created-after-failure'}, expected_revision=0)
    restarted, _ = fixture(tmp_path, source=job.source, replica=job.replica)
    assert restarted.resume(preview['review_token'])['local_copies_complete']
    assert restarted.source.get('created-after-failure')
    assert [row['id'] for row in restarted.search._read()[1]['documents']] == ['created-after-failure']


def test_partial_backup_failure_retries_only_reviewed_files(tmp_path, monkeypatch):
    job, _ = fixture(tmp_path)
    orphan = job.source.path.with_name('.history.json.' + 'c' * 32 + '.tmp')
    orphan.write_bytes(job.source.path.read_bytes())
    preview = job.preview()
    unlink = Path.unlink
    def fail_backup(path, *args, **kwargs):
        if path.name == 'history.json.migrated':
            raise OSError('backup locked')
        return unlink(path, *args, **kwargs)
    monkeypatch.setattr(Path, 'unlink', fail_backup)
    with pytest.raises(OSError):
        job.begin(review_token=preview['review_token'])
    assert job.status(preview['review_token'])['done'] == ['source', 'replica']
    assert not orphan.exists()
    # New orphan copies were not reviewed and must not be swept up on retry.
    later = job.source.path.with_name('.history.json.' + 'd' * 32 + '.tmp')
    later.write_bytes(b'new content')
    monkeypatch.setattr(Path, 'unlink', unlink)
    assert job.resume(preview['review_token'])['local_copies_complete']
    assert later.read_bytes() == b'new content'


def test_busy_index_leaves_cleanup_pending_and_retries_after_writer_releases(tmp_path, monkeypatch):
    job, calls = fixture(tmp_path)
    preview = job.preview()
    save = job._save
    def busy_after_backup(envelope, data):
        save(envelope, data)
        if data['jobs'][preview['review_token']]['done'] == ['source', 'replica', 'backups']:
            job.search.writer.acquire()
    monkeypatch.setattr(job, '_save', busy_after_backup)
    try:
        with pytest.raises(SearchError, match='search_index_busy'):
            job.begin(review_token=preview['review_token'])
    finally:
        job.search.writer.release()
    monkeypatch.setattr(job, '_save', save)
    assert job.status(preview['review_token'])['pending'] == ['search', 'orders']
    assert calls == []
    assert job.resume(preview['review_token'])['local_copies_complete']


def test_changed_remaining_backup_is_not_deleted_and_not_reported_complete(tmp_path, monkeypatch):
    job, _ = fixture(tmp_path)
    preview = job.preview()
    def fail(_):
        raise OSError('interrupted before backup cleanup')
    original = job._step_backups
    monkeypatch.setattr(job, '_step_backups', fail)
    with pytest.raises(OSError):
        job.begin(review_token=preview['review_token'])
    backup = job.source.path.with_name('history.json.migrated')
    backup.write_bytes(b'unreviewed backup')
    monkeypatch.setattr(job, '_step_backups', original)
    with pytest.raises(ConversationError, match='ask_cleanup_backup_changed'):
        job.resume(preview['review_token'])
    assert backup.read_bytes() == b'unreviewed backup'
    assert not job.status(preview['review_token'])['local_copies_complete']


def test_source_review_binds_additional_identities(tmp_path):
    job, _ = fixture(tmp_path, enabled=False)
    privacy = ConversationPrivacy(job.source)
    preview = privacy.preview(additional_identifiers=['old-replica'])
    before = job.source.path.read_bytes()
    with pytest.raises(ConversationError, match='ask_privacy_review_changed'):
        privacy.erase_source(review_token=preview['review_token'], additional_identifiers=['different'])
    assert job.source.path.read_bytes() == before


def test_changed_backup_needs_new_review_and_keeps_other_new_work(tmp_path, monkeypatch):
    job, _ = fixture(tmp_path)
    token = job.preview()['review_token']
    original = job._step_backups
    def fail(_):
        raise OSError('locked')
    monkeypatch.setattr(job, '_step_backups', fail)
    with pytest.raises(OSError):
        job.begin(review_token=token)
    monkeypatch.setattr(job, '_step_backups', original)
    backup = job.source.path.with_name('history.json.migrated')
    backup.write_bytes(b'replacement version')
    reviewed = job.review_backups(token)
    backup.write_bytes(b'another replacement')
    with pytest.raises(ConversationError, match='ask_cleanup_backup_changed'):
        job.approve_backups(token, review_token=reviewed['review_token'])
    reviewed = job.review_backups(token)
    job.source.save({**sample(), 'id': 'new-work'}, expected_revision=0)
    assert job.approve_backups(token, review_token=reviewed['review_token'])['local_copies_complete']
    assert not backup.exists() and job.source.get('new-work')


def test_abandon_uncommitted_failure_allows_fresh_review_but_old_request_stays_cancelled(tmp_path, monkeypatch):
    job, _ = fixture(tmp_path)
    token = job.preview()['review_token']
    original = job._step_source
    def fail(_):
        raise OSError('source unavailable')
    monkeypatch.setattr(job, '_step_source', fail)
    with pytest.raises(OSError):
        job.begin(review_token=token)
    assert job.cancel_uncommitted(token)['cancelled']
    assert job.begin(review_token=token)['cancelled']
    assert job.source.get(sample()['id'])
    fresh = job.preview()['review_token']
    assert fresh != token
    monkeypatch.setattr(job, '_step_source', original)
    assert job.begin(review_token=fresh)['local_copies_complete']
    assert job.cancel_uncommitted(token)['cancelled']
    with pytest.raises(ConversationError, match='ask_cleanup_already_started'):
        job.cancel_uncommitted(fresh)


def test_reviewed_search_orphan_is_removed_but_canonical_index_is_rebuilt(tmp_path):
    job, _ = fixture(tmp_path)
    old = job.search.path.read_bytes()
    orphan = job.search.path.with_name('.index.json.' + 'f' * 32 + '.tmp')
    orphan.write_bytes(old)
    token = job.preview()['review_token']
    assert job.begin(review_token=token)['local_copies_complete']
    assert not orphan.exists()
    assert job.search.path.exists() and job.search.path.read_bytes() != old
    assert job.search._read()[1]['documents'] == []
