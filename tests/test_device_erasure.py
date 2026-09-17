import uuid
from copy import deepcopy

import pytest
from test_device_sync_pairing import Devices, code_for
from test_reading_cleanup import ITEM, fixture

from rynmesh.device_erasure.adapters import PHASES, CleanupAdapters
from rynmesh.device_erasure.service import PATH, DeviceErasure
from rynmesh.device_erasure.store import ErasureError, ErasureStore


def setup(tmp_path):
    devices = Devices(tmp_path)
    a, b, pair_id = devices.pair()
    c = devices.node('C')
    invitation = a.create_invite(['reading'])
    pair = c.join(invitation['uri'], ['reading'])
    a.approve(pair['id'], review_token=pair['review_token'], scopes=['reading'], verification_code=code_for(pair['id']))
    c.retry(pair['id'])
    services, cleanups = {}, {}
    network = {'offline': set(), 'drop': False, 'wire': None}
    original = devices.post
    def post(endpoint, path, wire):
        if path != PATH:
            return original(endpoint, path, wire)
        if endpoint in network['offline']:
            raise OSError('offline')
        network['wire'] = deepcopy(wire)
        result = services[endpoint].receive(wire)
        if network['drop']:
            network['drop'] = False
            raise OSError('response lost')
        return result
    for name, node in [('A', a), ('B', b), ('C', c)]:
        cleanup = fixture(tmp_path / name)
        adapters = CleanupAdapters(None)
        adapters.target = lambda category, cleanup=cleanup: cleanup
        service = DeviceErasure(ErasureStore(tmp_path / name, node.messaging_key), lambda node=node: node, adapters)
        services[node.identity['endpoint']] = service
        cleanups[name] = cleanup
        node.post_json = post
    return devices, [services[node.identity['endpoint']] for node in (a, b, c)], [pair_id, pair['id']], cleanups, network


def job_id():
    return uuid.uuid4().hex


def test_three_devices_owner_review_offline_and_lost_response_never_false_confirm(tmp_path):
    _, (a, b, c), pairs, cleanups, network = setup(tmp_path)
    job = job_id()
    network['offline'].add(c.pairing().identity['endpoint'])
    a.begin(job, 'reading', pairs)
    a.exchange(job, pairs[0])
    a.exchange(job, pairs[1])
    assert cleanups['B'].source.list() and cleanups['C'].source.list()
    incoming = b.status()['incoming'][0]['id']
    preview = b.preview(incoming)
    assert preview['counts']['local_items'] == 1
    assert cleanups['B'].source.list()  # Preview and network dispatch are not deletion authority.
    b.approve(incoming, preview['review_token'])
    assert cleanups['B'].source.list() == []
    network['drop'] = True
    assert not a.exchange(job, pairs[0])['remote_confirmed']
    result = a.exchange(job, pairs[0])
    assert not result['remote_confirmed'] and result['targets'][0]['state'] == 'confirmed'
    network['offline'].clear()
    a.exchange(job, pairs[1])
    incoming_c = c.status()['incoming'][0]['id']
    c.approve(incoming_c, c.preview(incoming_c)['review_token'])
    assert a.exchange(job, pairs[1])['remote_confirmed']
    assert cleanups['A'].source.list()  # The initiating node is not implicitly a target.
    assert b'SYNTHETIC-PRIVATE-READING-MARKER' not in b.store.path.read_bytes()
    # A later recreated record survives replay of the same approved cleanup.
    cleanups['B'].source.record(ITEM, 'bookmark')
    b.approve(incoming, preview['review_token'])
    assert cleanups['B'].source.list()


def test_partial_cleanup_restart_resumes_exact_plan_and_keeps_later_records(tmp_path, monkeypatch):
    _, (a, b, _), pairs, cleanups, _ = setup(tmp_path)
    job = job_id()
    a.begin(job, 'reading', [pairs[0]])
    a.exchange(job, pairs[0])
    incoming = b.status()['incoming'][0]['id']
    preview = b.preview(incoming)
    cleanup = cleanups['B']
    original = cleanup._step_backups
    monkeypatch.setattr(cleanup, '_step_backups', lambda job: (_ for _ in ()).throw(OSError('disk locked')))
    with pytest.raises(ErasureError, match='incomplete'):
        b.approve(incoming, preview['review_token'])
    assert b.status()['incoming'][0]['state'] == 'partial'
    assert not a.exchange(job, pairs[0])['remote_confirmed']
    cleanup.source.record({**ITEM, 'item_id': 'new-reading'}, 'bookmark')
    monkeypatch.setattr(cleanup, '_step_backups', original)
    restarted = DeviceErasure(ErasureStore(tmp_path / 'B', b.pairing().messaging_key), b.pairing, b.adapters)
    restarted.resume(incoming)
    assert restarted.status()['incoming'][0]['state'] == 'confirmed'
    assert [row['item_id'] for row in cleanup.source.list()] == ['new-reading']


def test_changed_local_plan_rejected_instead_of_erasing_unreviewed_work(tmp_path):
    _, (a, b, _), pairs, cleanups, _ = setup(tmp_path)
    job = job_id()
    a.begin(job, 'reading', [pairs[0]])
    a.exchange(job, pairs[0])
    incoming = b.status()['incoming'][0]['id']
    preview = b.preview(incoming)
    cleanups['B'].source.record({**ITEM, 'item_id': 'new-reading'}, 'bookmark')
    with pytest.raises(ErasureError, match='incomplete'):
        b.approve(incoming, preview['review_token'])
    assert len(cleanups['B'].source.list()) == 2
    assert not a.exchange(job, pairs[0])['remote_confirmed']
    b.reject(incoming)
    assert a.exchange(job, pairs[0])['targets'][0]['state'] == 'rejected'


def test_empty_targets_reused_identity_wrong_manifest_and_policy_changes_fail_closed(tmp_path):
    _, (a, b, _), pairs, cleanups, network = setup(tmp_path)
    with pytest.raises(ErasureError):
        a.begin(job_id(), 'reading', [])
    job = job_id()
    a.begin(job, 'reading', [pairs[0]])
    with pytest.raises(ErasureError, match='changed'):
        a.begin(job, 'documents', [pairs[0]])
    a.exchange(job, pairs[0])
    wire = network['wire']
    pair, value = b.open(wire)
    value['manifest'] = '0' * 64
    forged = a.seal(a.pair(pairs[0]), {key: value[key] for key in ('id', 'category', 'manifest', 'epoch', 'nonce')})
    with pytest.raises(ErasureError, match='changed'):
        b.receive(forged)
    incoming = b.status()['incoming'][0]['id']
    preview = b.preview(incoming)
    b.pairing().configure(pairs[0], scopes=['reading'], paused=True, expected_revision=b.pairing().get(pairs[0])['revision'])
    with pytest.raises(ErasureError, match='policy_changed'):
        b.approve(incoming, preview['review_token'])
    assert cleanups['B'].source.list()
    assert not a.exchange(job, pairs[0])['remote_confirmed']


def test_incomplete_phase_receipt_cannot_confirm(tmp_path):
    _, (a, b, _), pairs, _, _ = setup(tmp_path)
    job = job_id()
    a.begin(job, 'reading', [pairs[0]])
    a.exchange(job, pairs[0])
    incoming = b.status()['incoming'][0]['id']
    preview = b.preview(incoming)
    b.adapters.execute = lambda category, token: PHASES[category][:-1]
    with pytest.raises(ErasureError, match='incomplete'):
        b.approve(incoming, preview['review_token'])
    assert not a.exchange(job, pairs[0])['remote_confirmed']


def test_replayed_signed_reply_cannot_complete_a_new_nonce_or_request(tmp_path):
    _, (a, b, _), pairs, _, _ = setup(tmp_path)
    original = a.pairing().post_json
    captured = []
    def capture(endpoint, path, wire):
        response = original(endpoint, path, wire)
        captured.append(response)
        return response
    a.pairing().post_json = capture
    first = job_id()
    a.begin(first, 'reading', [pairs[0]])
    a.exchange(first, pairs[0])
    incoming = b.status()['incoming'][0]['id']
    b.approve(incoming, b.preview(incoming)['review_token'])
    assert a.exchange(first, pairs[0])['remote_confirmed']
    old_receipt = captured[-1]
    second = job_id()
    a.begin(second, 'reading', [pairs[0]])
    a.pairing().post_json = lambda *args: old_receipt
    result = a.exchange(second, pairs[0])
    assert not result['remote_confirmed'] and result['targets'][0]['state'] == 'unconfirmed'
