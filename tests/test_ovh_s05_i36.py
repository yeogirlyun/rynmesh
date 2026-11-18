"""S05-I36: real snapshot accounting, bounded scans and shared-process writes."""
import json
import os
from pathlib import Path
import subprocess
import sys
import time
from types import SimpleNamespace

import pytest

from test_file_review import _make_review_env
from rynmesh.agent_runtime import file_review
from rynmesh.agent_runtime.local_tools import LocalTools
from rynmesh.agent_runtime.operation_ledger import OperationLedger
from rynmesh.agent_runtime.store import AgentToolRejected
from rynmesh.file_transactions import file_transaction


@pytest.fixture
def env(tmp_path):
    value = _make_review_env(tmp_path)
    yield value
    value.local.processes.close()
    value.client.close()


def actual_usage(parent):
    return {task.name: sum(p.stat().st_size for p in task.iterdir() if p.is_file())
            for task in parent.iterdir() if task.is_dir()}


def cache(review, *, verified_at=None, total=None):
    parent = review.directory.parent
    parent.mkdir(parents=True, exist_ok=True)
    tasks = actual_usage(parent)
    data = {'total_bytes': sum(tasks.values()) if total is None else total, 'tasks': tasks,
            'verified_at': time.time() if verified_at is None else verified_at}
    (parent / 'usage-v1.json').write_text(json.dumps(data), encoding='utf-8')
    return data


def assert_exact(review):
    actual = actual_usage(review.directory.parent)
    recorded = json.loads((review.directory.parent / 'usage-v1.json').read_text(encoding='utf-8'))
    assert recorded['total_bytes'] == sum(actual.values())
    assert {k: v for k, v in recorded['tasks'].items() if v} == {k: v for k, v in actual.items() if v}
    return recorded


def scan_spy(monkeypatch, parent):
    calls = []
    original = Path.glob
    def glob(path, pattern, *args, **kwargs):
        if path == parent and pattern == '*/*':
            calls.append(1)
        return original(path, pattern, *args, **kwargs)
    monkeypatch.setattr(Path, 'glob', glob)
    return calls


def test_t1_fifty_tasks_twenty_snapshots_apply_uses_warm_count(env, monkeypatch):
    parent = env.review.directory.parent
    for task in range(50):
        folder = parent / f'other_{task}'
        folder.mkdir(parents=True)
        for snapshot in range(20):
            (folder / f'{snapshot}.blob').write_bytes(b'old snapshot')
    cache(env.review)
    calls = scan_spy(monkeypatch, parent)
    env.review.apply([(env.workspace / 'new.txt', None, b'new data', None)])
    assert calls == []
    assert_exact(env.review)


def test_t1_warm_apply_flushes_usage_once_before_releasing_lock(env, monkeypatch):
    cache(env.review)
    writes = []
    original = file_review.atomic_write_json
    def write(path, data):
        if Path(path).name == 'usage-v1.json':
            writes.append(1)
        return original(path, data)
    monkeypatch.setattr(file_review, 'atomic_write_json', write)
    env.review.apply([(env.workspace / 'once.txt', None, b'only one usage flush', None)])
    assert len(writes) == 1
    assert_exact(env.review)


def test_t2_real_file_tools_manifest_growth_dedup_and_undo_match_disk(env):
    env.write('one.txt', 'before\n')
    first = assert_exact(env.review)
    env.write('one.txt', 'after\n')
    second = assert_exact(env.review)
    assert second['total_bytes'] > first['total_bytes']
    # Reusing an existing immutable blob does not add its bytes again.
    env.review.blob(b'after\n')
    assert_exact(env.review)
    preview = env.review.preview(None)
    env.review.undo(None, preview['token'], 'i36-undo')
    assert not (env.workspace / 'one.txt').exists()
    assert_exact(env.review)


@pytest.mark.parametrize('damage', ['missing', 'invalid_json', 'invalid_numbers'])
def test_t3_missing_or_corrupt_cache_rebuilds_once(env, monkeypatch, damage):
    env.write('first.txt', 'first')
    path = env.review.directory.parent / 'usage-v1.json'
    if damage == 'missing':
        path.unlink(missing_ok=True)
    else:
        path.write_text('broken' if damage == 'invalid_json' else '{"total_bytes":-1,"tasks":{},"verified_at":0}', encoding='utf-8')
    calls = scan_spy(monkeypatch, env.review.directory.parent)
    env.write('second.txt', 'second')
    assert len(calls) == 1
    assert_exact(env.review)


def test_t4_forget_removes_only_task_usage_under_shared_lock(env):
    env.write('first.txt', 'one')
    other = env.local.file_review('other_task', str(env.workspace))
    other.apply([(env.workspace / 'other.txt', None, b'other', None)])
    before = assert_exact(env.review)
    env.local.forget_baselines(env.task_id)
    after = assert_exact(env.review)
    assert after['total_bytes'] < before['total_bytes']
    assert env.review.directory.name not in after['tasks']
    assert other.directory.exists() and after['tasks']['other_task'] == before['tasks']['other_task']


def test_t5_near_limit_recomputes_only_once_for_entire_apply(env, monkeypatch):
    parent = env.review.directory.parent
    folder = parent / 'other'
    folder.mkdir(parents=True)
    (folder / 'orphan.blob').write_bytes(b'x' * 900100)
    monkeypatch.setattr(file_review, 'MAX_HOME_BYTES', 1000000)
    cache(env.review)
    calls = scan_spy(monkeypatch, parent)
    env.review.apply([(env.workspace / 'one.txt', None, b'small', None)])
    assert len(calls) == 1
    assert_exact(env.review)


def test_t5_near_limit_reservation_still_rejects_after_recount(env, monkeypatch):
    parent = env.review.directory.parent
    folder = parent / 'other'
    folder.mkdir(parents=True)
    (folder / 'orphan.blob').write_bytes(b'x' * 95000)
    monkeypatch.setattr(file_review, 'MAX_HOME_BYTES', 100000)
    data = cache(env.review, total=91000)
    data['tasks']['other'] = 91000
    (parent / 'usage-v1.json').write_text(json.dumps(data), encoding='utf-8')
    calls = scan_spy(monkeypatch, parent)
    with file_transaction(env.review.lock), pytest.raises(AgentToolRejected, match='agent_review_storage_limit'):
        env.review.reserve_progress(env.review.load())
    assert len(calls) == 1
    assert_exact(env.review)
    assert not (env.workspace / 'unwritten.txt').exists()


def test_t2_t3_expired_cache_counts_orphans_without_collecting_them(env, monkeypatch):
    cache(env.review, verified_at=time.time() - 601)
    orphan = env.review.directory.parent / 'old_task' / 'unreferenced.blob'
    orphan.parent.mkdir()
    orphan.write_bytes(b'orphan remains')
    calls = scan_spy(monkeypatch, env.review.directory.parent)
    env.write('fresh.txt', 'fresh')
    assert len(calls) == 1 and orphan.read_bytes() == b'orphan remains'
    assert_exact(env.review)


def test_t2_two_processes_share_lock_and_accounting(tmp_path):
    home, work = tmp_path / 'home', tmp_path / 'work'
    home.mkdir(); work.mkdir()
    script = Path(__file__).resolve()
    children = [subprocess.Popen([sys.executable, str(script), str(home), str(work), name],
                env={**os.environ, 'PYTHONPATH': str(script.parent.parent)}, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
                for name in ('node_a', 'node_b')]
    try:
        for child in children:
            _, err = child.communicate(timeout=45)
            assert child.returncode == 0, err.decode(errors='replace')
    finally:
        for child in children:
            if child.poll() is None:
                child.kill()
                child.communicate(timeout=5)
    local = LocalTools(SimpleNamespace(home=home), lambda: OperationLedger(home))
    try:
        review = local.file_review('node_a', str(work))
        recorded = assert_exact(review)
        assert set(recorded['tasks']) == {'node_a', 'node_b'}
        assert (work / 'node_a.txt').read_text() == 'node_a'
        assert (work / 'node_b.txt').read_text() == 'node_b'
    finally:
        local.processes.close()


if __name__ == '__main__':
    home, work, task = Path(sys.argv[1]), Path(sys.argv[2]), sys.argv[3]
    local = LocalTools(SimpleNamespace(home=home), lambda: OperationLedger(home))
    try:
        local.file_review(task, str(work)).apply([(work / (task + '.txt'), None, task.encode(), None)])
    finally:
        local.processes.close()
