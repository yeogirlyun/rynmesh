import json
import uuid

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
from test_friends import _pair

from rynmesh.background_workers import BackgroundWorkerRegistry
from rynmesh.crypto import canonical_json
from rynmesh.shared_reading.routes import install_shared_reading
from rynmesh.shared_reading.service import PATH, SharedReading
from rynmesh.shared_reading.store import ListError, ListStore


def identifier():
    return uuid.uuid4().hex


def setup(tmp_path):
    mesh, alice, bob = _pair(tmp_path)
    a = SharedReading(ListStore(alice.home, alice.messaging_private), lambda: alice)
    b = SharedReading(ListStore(bob.home, bob.messaging_private), lambda: bob)
    services = {alice.endpoint: a, bob.endpoint: b}
    original = mesh.post
    network = {'drop_response': False, 'wires': []}
    def post(endpoint, path, wire, headers=None, **kwargs):
        if path != PATH:
            return original(endpoint, path, wire, headers, **kwargs)
        if endpoint not in mesh.online:
            raise OSError('offline')
        receiver = services[endpoint]
        relation = receiver.friends().verify_request(path=path, body=canonical_json(wire), headers=headers)
        result = receiver.respond(wire, relation)
        network['wires'].append((wire, result))
        if network['drop_response']:
            network['drop_response'] = False
            raise OSError('lost reply after commit')
        return result
    alice.post_json = bob.post_json = post
    rid = alice.store.relationship_for_peer(bob.peer_id)['relationship_id']
    return mesh, a, b, rid, network, services


def joined(tmp_path):
    mesh, a, b, rid, network, services = setup(tmp_path)
    row = a.create(rid, 'Private shared reading', identifier())
    b.accept(rid, row['id'])
    return mesh, a, b, row['id'], rid, network, services


def add(service, list_id, title='A private article'):
    op = identifier()
    service.edit(list_id, op, 'add', {'id': op, 'title': title, 'url': 'https://example.test/article'})
    return op


def test_invitation_requires_explicit_accept_and_shares_no_personal_history(tmp_path):
    _, a, b, rid, network, _ = setup(tmp_path)
    row = a.create(rid, 'Private shared reading', identifier())
    assert b.listing() == []
    assert b.discover(rid) == [{'id': row['id'], 'title': row['title']}]
    assert b.listing() == [] and a.listing()[0]['status'] == 'invited'
    b.accept(rid, row['id'])
    assert a.listing()[0]['status'] == b.listing()[0]['status'] == 'active'
    assert a.listing()[0]['items'] == {}
    assert 'Private shared reading' not in json.dumps(network['wires'])
    assert b'Private shared reading' not in a.store.path.read_bytes()


def test_offline_add_resume_after_restart_and_lost_reply_is_exactly_once(tmp_path):
    mesh, a, b, list_id, rid, network, services = joined(tmp_path)
    mesh.online.remove(a.friends().endpoint)
    item_id = add(b, list_id)
    with pytest.raises(ListError, match='unconfirmed'):
        b.sync(list_id)
    assert b.listing()[0]['pending_count'] == 1
    assert a.listing()[0]['items'] == {}
    restored = SharedReading(ListStore(b.friends().home, b.friends().messaging_private), b.friends)
    services[b.friends().endpoint] = restored
    mesh.online.add(a.friends().endpoint)
    network['drop_response'] = True
    with pytest.raises(ListError, match='unconfirmed'):
        restored.sync(list_id)
    revision = a.listing()[0]['revision']
    assert len(a.listing()[0]['items']) == 1
    restored.sync(list_id)
    assert restored.listing()[0]['pending_count'] == 0
    assert a.listing()[0]['revision'] == revision
    # A lost owner API response retried after transport acknowledgement is also a no-op.
    restored.edit(list_id, item_id, 'add', {'id': item_id, 'title': 'A private article', 'url': 'https://example.test/article'})
    assert restored.listing()[0]['pending_count'] == 0


def test_both_members_add_and_mark_only_their_own_status_with_delete_winning(tmp_path):
    _, a, b, list_id, _, _, _ = joined(tmp_path)
    one = add(a, list_id, 'From Alice')
    two = add(b, list_id, 'From Bob')
    b.sync(list_id)
    a.edit(list_id, identifier(), 'read', {'id': one, 'read': True})
    b.edit(list_id, identifier(), 'read', {'id': one, 'read': True})
    b.sync(list_id)
    assert a.listing()[0]['items'][one]['read_by'] == {a.peer: True, b.peer: True}
    b.edit(list_id, identifier(), 'read', {'id': two, 'read': True})
    a.edit(list_id, identifier(), 'remove', {'id': two})
    b.sync(list_id)
    assert b.listing()[0]['items'] == a.listing()[0]['items']
    assert b.listing()[0]['items'][two]['removed']
    assert b.listing()[0]['items'][two]['read_by'] == {}


def test_closed_list_lost_receipt_retries_without_reopening_and_revocation_blocks_new_edits(tmp_path):
    _, a, b, list_id, rid, network, _ = joined(tmp_path)
    b.edit(list_id, identifier(), 'close', {})
    network['drop_response'] = True
    with pytest.raises(ListError):
        b.sync(list_id)
    b.sync(list_id)
    assert a.listing()[0]['status'] == b.listing()[0]['status'] == 'closed'
    with pytest.raises(ListError, match='inactive'):
        add(a, list_id)
    a.friends().revoke(rid, notify=False)
    assert a.listing()[0]['blocked']
    with pytest.raises(ListError):
        b.accept(rid, list_id)


def test_invalid_link_status_spoof_and_changed_operation_do_not_queue(tmp_path):
    _, a, b, list_id, _, _, _ = joined(tmp_path)
    with pytest.raises(ListError, match='link_invalid'):
        b.edit(list_id, identifier(), 'add', {'id': identifier(), 'title': 'bad', 'url': 'javascript:alert(1)'})
    with pytest.raises(ListError, match='invalid'):
        b.edit(list_id, identifier(), 'read', {'id': identifier(), 'read': True, 'peer': a.peer})
    op = add(b, list_id)
    with pytest.raises(ListError, match='changed'):
        b.edit(list_id, op, 'add', {'id': op, 'title': 'Different', 'url': 'https://example.test/article'})
    assert b.listing()[0]['pending_count'] == 1


def test_owner_closure_cancels_offline_edits_after_restart_without_losing_them(tmp_path):
    mesh, a, b, list_id, _, network, _ = joined(tmp_path)
    item = add(a, list_id)
    b.sync(list_id)
    mesh.online.remove(a.friends().endpoint)
    b.edit(list_id, identifier(), 'read', {'id': item, 'read': True})
    add(b, list_id, 'Queued offline')
    b.edit(list_id, identifier(), 'close', {})
    pending = b.store.read()['lists'][list_id]['pending']
    a.edit(list_id, identifier(), 'close', {})
    mesh.online.add(a.friends().endpoint)
    restored = SharedReading(ListStore(b.friends().home, b.friends().messaging_private), b.friends)
    network['drop_response'] = True
    with pytest.raises(ListError, match='unconfirmed'):
        restored.sync(list_id)
    assert restored.store.read()['lists'][list_id]['pending'] == pending
    restored.sync(list_id)
    for _ in range(2):
        row = restored.listing()[0]
        assert row['status'] == 'closed'
        assert row['pending_count'] == 0
        assert row['cancelled_count'] == 3
        assert row['items'] == a.listing()[0]['items']
        assert restored.store.read()['lists'][list_id]['cancelled'] == pending
        restored.sync(list_id)
    with pytest.raises(ListError, match='inactive'):
        add(restored, list_id)


@pytest.mark.parametrize('refresh', ['accept', 'snapshot'])
def test_new_receipt_reconciles_pending_edits_before_assigning_sequences(tmp_path, refresh):
    _, a, b, list_id, rid, network, _ = joined(tmp_path)
    first = add(b, list_id, 'First')
    network['drop_response'] = True
    with pytest.raises(ListError, match='unconfirmed'):
        b.sync(list_id)
    second = add(b, list_id, 'Second')
    if refresh == 'accept':
        b.accept(rid, list_id)
    else:
        b.receive(list_id, rid, a.peer, b.request(a.peer, 'snapshot', id=list_id))
    assert b.listing()[0]['pending_count'] == 1
    third = add(b, list_id, 'Third')
    assert [op['sequence'] for op in b.store.read()['lists'][list_id]['pending']] == [2, 3]
    b.sync(list_id)
    b.sync(list_id)
    assert b.listing()[0]['pending_count'] == 0
    assert set(a.listing()[0]['items']) == {first, second, third}
    assert a.listing()[0]['items'] == b.listing()[0]['items']
    b.edit(list_id, first, 'add', {'id': first, 'title': 'First', 'url': 'https://example.test/article'})
    assert b.listing()[0]['pending_count'] == 0


def test_snapshot_receipt_must_match_pending_operation_before_reconciliation(tmp_path):
    _, a, b, list_id, rid, network, _ = joined(tmp_path)
    add(b, list_id)
    network['drop_response'] = True
    with pytest.raises(ListError):
        b.sync(list_id)
    before = b.store.read()
    result = a.snapshot(a.store.read()['lists'][list_id])
    result['receipts'][b.peer]['digest'] = '0' * 64
    with pytest.raises(ListError, match='response_invalid'):
        b.receive(list_id, rid, a.peer, result)
    assert b.store.read() == before


def test_closed_snapshot_distinguishes_committed_edits_from_cancelled_edits(tmp_path):
    _, a, b, list_id, rid, network, _ = joined(tmp_path)
    stale = a.snapshot(a.store.read()['lists'][list_id])
    committed = add(b, list_id, 'Committed before closure')
    network['drop_response'] = True
    with pytest.raises(ListError):
        b.sync(list_id)
    cancelled = add(b, list_id, 'Not committed')
    a.edit(list_id, identifier(), 'close', {})
    b.sync(list_id)
    b.receive(list_id, rid, a.peer, stale)
    row = b.listing()[0]
    assert row['status'] == 'closed'
    assert row['pending_count'] == 0 and row['cancelled_count'] == 1
    assert set(row['items']) == {committed}
    assert [op['id'] for op in b.store.read()['lists'][list_id]['cancelled']] == [cancelled]


def test_failed_closure_snapshot_preserves_pending_edits_until_retry(tmp_path, monkeypatch):
    _, a, b, list_id, _, _, _ = joined(tmp_path)
    add(b, list_id)
    pending = b.store.read()['lists'][list_id]['pending']
    a.edit(list_id, identifier(), 'close', {})
    original = b.request
    def request(peer, action, **args):
        if action == 'snapshot':
            raise ListError('shared_sync_unconfirmed')
        return original(peer, action, **args)
    with monkeypatch.context() as patch:
        patch.setattr(b, 'request', request)
        with pytest.raises(ListError, match='unconfirmed'):
            b.sync(list_id)
    assert b.listing()[0]['status'] == 'active'
    assert b.store.read()['lists'][list_id]['pending'] == pending
    b.sync(list_id)
    assert b.listing()[0]['status'] == 'closed'
    assert b.listing()[0]['pending_count'] == 0


def test_snapshot_cannot_replace_list_identity_or_introduce_executable_links(tmp_path):
    _, a, b, list_id, rid, _, _ = joined(tmp_path)
    add(a, list_id)
    result = a.snapshot(a.store.read()['lists'][list_id])
    result['items'][next(iter(result['items']))]['url'] = 'javascript:alert(1)'
    with pytest.raises(ListError, match='response_invalid'):
        b.receive(list_id, rid, a.peer, result)
    assert b.listing()[0]['items'] == {}
    result['id'] = identifier()
    with pytest.raises(ListError, match='response_invalid'):
        b.receive(list_id, rid, a.peer, result)


def test_owner_and_peer_http_boundaries_reject_untrusted_requests(tmp_path):
    _, a, _, rid, _, _ = setup(tmp_path)
    app, workers = FastAPI(), BackgroundWorkerRegistry()
    def guard(request):
        if request.headers.get('x-owner') != 'yes':
            raise HTTPException(403)
    service = install_shared_reading(app, home=a.friends().home, messaging_key=a.friends().messaging_private,
        friends=a.friends, local_control=guard, workers=workers)
    with TestClient(app) as client:
        assert client.get('/api/local/shared-reading').status_code == 403
        assert client.post('/api/local/shared-reading/action', json={'action': 'create'}).status_code == 403
        assert client.post(PATH, json={}).status_code == 403
        headers = {'x-owner': 'yes'}
        value = {'action': 'create', 'relationship_id': rid, 'title': 'List', 'id': identifier()}
        assert client.post('/api/local/shared-reading/action', headers=headers, json=value).status_code == 200
        assert client.post('/api/local/shared-reading/action', headers=headers, json=value).status_code == 200
        assert len(service.listing()) == 1
        assert client.post('/api/local/shared-reading/action', headers=headers, content=b'x' * 8193).status_code == 413
