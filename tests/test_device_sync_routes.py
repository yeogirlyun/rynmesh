"""Pairing through installed owner/peer HTTP handlers and the shared worker."""
import asyncio
import threading
import time
from types import SimpleNamespace

import httpx
import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
from test_device_sync_pairing import code_for

from rynmesh.background_workers import BackgroundWorkerRegistry
from rynmesh.device_sync.routes import install_device_sync

PREFIX = '/api/local/device-sync'
OWNER = {'x-test-owner': 'yes'}


class Node:
    def __init__(self, home, endpoint='', post=None, *, transfer=False):
        self.app, self.workers = FastAPI(), BackgroundWorkerRegistry()
        self.store = SimpleNamespace(home=home, private_key_bytes=Ed25519PrivateKey.generate().private_bytes_raw(), node_name=home.name)
        self.key, self.endpoint, self.post = X25519PrivateKey.generate(), endpoint, post
        self.transfer_enabled = transfer
        if transfer:
            from rynmesh.ask_ryn.store import ConversationStore
            from rynmesh.services.consumption import ConsumptionStore
            self.reader = ConsumptionStore(home / 'consumption.json')
            self.history = ConversationStore(home / 'ask', self.key)
        self.install()
        self.client = TestClient(self.app)

    @staticmethod
    def owner(request):
        if request.headers.get('x-test-owner') != 'yes':
            raise HTTPException(403, detail='owner_required')

    def install(self):
        return install_device_sync(self.app, store=self.store, home=self.store.home / 'wrong-home',
            workers=self.workers, messaging_key=self.key, endpoint=self.endpoint, allow_loopback=True,
            local_control=self.owner, post_json=self.post,
            reading=(lambda: self.reader) if self.transfer_enabled else None,
            conversations=(lambda: self.history) if self.transfer_enabled else None)

    @property
    def service(self):
        return self.app.state.device_sync.service

    def tick(self):
        return self.workers.specs()[0].run_once()

    def request(self, method, path='', **kwargs):
        result = self.client.request(method, PREFIX + path, headers=OWNER, **kwargs)
        assert result.status_code == 200, result.text
        return result.json()


def test_owner_handlers_and_worker_complete_pair_pause_remove(tmp_path):
    nodes = {}

    def post(endpoint, path, wire):
        response = nodes[endpoint].client.post(path, json=wire)
        assert response.status_code == 200, response.text
        return response.json()

    a = Node(tmp_path / 'A', 'http://127.0.0.1:18901', post)
    b = Node(tmp_path / 'B', 'http://127.0.0.1:18902', post)
    nodes.update({a.endpoint: a, b.endpoint: b})
    uri = a.request('POST', '/invites', json={'scopes': ['bookmarks']})['uri']
    preview = b.request('POST', '/invites/inspect', json={'uri': uri})
    assert preview['device']['name'] == 'A'
    joining = b.request('POST', '/join', json={'uri': uri, 'scopes': ['bookmarks']})
    pair_id = joining['id']
    assert a.request('GET')['devices'] == []  # Local consent committed; IO is deferred.
    b.tick()
    pending = a.request('GET')['devices'][0]
    assert pending['status'] == 'awaiting_owner'
    a.request('POST', f'/devices/{pair_id}/approve', json={'review_token': pair_id, 'scopes': ['bookmarks'], 'verification_code': code_for(pair_id)})
    b.tick()
    assert a.request('GET')['devices'][0]['status'] == b.request('GET')['devices'][0]['status'] == 'active'
    assert a.request('GET')['data_transfer_available'] is False  # Pairing never implies data convergence.
    a.request('PUT', f'/devices/{pair_id}/policy', json={'expected_revision': 1, 'scopes': ['bookmarks'], 'paused': True})
    a.tick()
    assert b.request('GET')['devices'][0]['remote_paused']
    removed = a.request('POST', f'/devices/{pair_id}/remove', json={'expected_revision': 2})
    assert removed['removal_pending']
    a.tick()
    assert b.request('GET')['devices'][0]['status'] == 'revoked'
    assert not a.request('GET')['devices'][0]['removal_pending']
    assert not (a.store.home / 'wrong-home').exists()


def test_approve_route_rejects_a_mismatched_or_missing_verification_code(tmp_path):
    nodes = {}

    def post(endpoint, path, wire):
        response = nodes[endpoint].client.post(path, json=wire)
        assert response.status_code == 200, response.text
        return response.json()

    a = Node(tmp_path / 'A', 'http://127.0.0.1:18905', post)
    b = Node(tmp_path / 'B', 'http://127.0.0.1:18906', post)
    nodes.update({a.endpoint: a, b.endpoint: b})
    uri = a.request('POST', '/invites', json={'scopes': ['bookmarks']})['uri']
    pair_id = b.request('POST', '/join', json={'uri': uri, 'scopes': ['bookmarks']})['id']
    b.tick()
    mismatched = a.client.post(PREFIX + f'/devices/{pair_id}/approve', headers=OWNER,
                               json={'review_token': pair_id, 'scopes': ['bookmarks'], 'verification_code': 'wrong-code'})
    assert mismatched.status_code == 409
    assert mismatched.json()['detail'] == 'sync_verification_code_mismatch'
    missing = a.client.post(PREFIX + f'/devices/{pair_id}/approve', headers=OWNER,
                            json={'review_token': pair_id, 'scopes': ['bookmarks']})
    assert missing.status_code == 409
    assert missing.json()['detail'] == 'sync_verification_code_mismatch'
    assert a.request('GET')['devices'][0]['status'] == 'awaiting_owner'
    a.request('POST', f'/devices/{pair_id}/approve', json={'review_token': pair_id, 'scopes': ['bookmarks'], 'verification_code': code_for(pair_id)})
    assert a.request('GET')['devices'][0]['status'] == 'awaiting_peer'


def test_reinstallation_replaces_guard_service_and_worker_without_duplicate_routes(tmp_path):
    node = Node(tmp_path / 'A', 'http://127.0.0.1:18901')
    first, count = node.service, len(node.app.routes)
    second = node.install()
    assert first is not second
    assert len(node.app.routes) == count
    assert [spec.name for spec in node.workers.specs()] == ['device-sync.pairing']
    second.readiness = lambda: {'pairing_available': False, 'reason': 'new-state'}
    assert node.request('GET')['reason'] == 'new-state'
    node.app.state.device_sync.local_control = lambda _: (_ for _ in ()).throw(HTTPException(403, 'replaced-guard'))
    assert node.client.get(PREFIX, headers=OWNER).json()['detail'] == 'replaced-guard'


def test_missing_endpoint_does_not_block_node_or_local_revocation(tmp_path):
    node = Node(tmp_path / 'A')
    assert node.request('GET')['reason'] == 'sync_endpoint_unavailable'
    assert node.client.post(PREFIX + '/invites', headers=OWNER, json={'scopes': []}).status_code == 503
    assert not node.service.store.path.exists()
    node.tick()


@pytest.mark.parametrize('method,path', [
    ('GET', ''), ('POST', '/invites'), ('POST', '/invites/inspect'), ('DELETE', '/invites/' + 'a' * 32),
    ('POST', '/join'), ('POST', '/devices/' + 'a' * 64 + '/approve'),
    ('PUT', '/devices/' + 'a' * 64 + '/policy'), ('POST', '/devices/' + 'a' * 64 + '/remove'),
    ('POST', '/devices/' + 'a' * 64 + '/retry'),
    ('GET', '/reading/conflicts'), ('POST', '/reading/resolve'),
])
def test_every_owner_route_checks_guard_before_request_body(tmp_path, method, path):
    node = Node(tmp_path / 'A')
    result = node.client.request(method, PREFIX + path, content=b'x' * 65537)
    assert result.status_code == 403
    assert not node.service.store.path.exists()


def test_peer_routes_bound_input_rate_and_error_details(tmp_path):
    node = Node(tmp_path / 'A')
    path = '/api/peer/device-sync/join'
    assert node.client.post(path, content=b'x' * 65537).status_code == 413
    assert node.client.post(path, json=[]).status_code == 400
    assert node.client.post(path, json={'secret': 'must-not-leak'}).json() == {'detail': 'sync_request_rejected'}
    assert node.client.post('/api/peer/device-sync/unexpected', json={}).status_code == 404
    for _ in range(57):
        assert node.client.post(path, json={}).status_code == 403
    result = node.client.post(path, json={})
    assert result.status_code == 429 and result.headers['retry-after'] == '60'
    assert len(node.app.state.device_sync.attempts) == 1


def test_new_peer_host_is_admitted_when_table_is_full(tmp_path):
    node = Node(tmp_path / 'A')
    path = '/api/peer/device-sync/join'
    attempts = node.app.state.device_sync.attempts
    now = time.monotonic()
    for index in range(256):
        attempts[f'host-{index}'] = [now - 1 + index * 0.001]  # host-0's newest stamp is the oldest
    assert len(attempts) == 256

    result = node.client.post(path, json={})
    assert result.status_code == 403  # admitted into handling, not rate limited
    assert len(attempts) == 256  # table stays bounded
    assert 'host-0' not in attempts  # the stalest host was evicted to make room
    assert 'testclient' in attempts  # the new host was admitted

    # A host already at its own 60/min ceiling must still be refused, even though the
    # table itself is not full.
    attempts.clear()
    attempts['testclient'] = [now] * 60
    result = node.client.post(path, json={})
    assert result.status_code == 429 and result.headers['retry-after'] == '60'


def test_slow_pairing_io_does_not_block_http_event_loop(tmp_path):
    node = Node(tmp_path / 'A')
    entered, release = threading.Event(), threading.Event()

    def slow(_wire):
        entered.set()
        assert release.wait(5)
        return {'done': True}

    node.service.receive_join = slow

    @node.app.get('/health-test')
    async def health():
        return {'ok': True}

    async def scenario():
        transport = httpx.ASGITransport(app=node.app)
        async with httpx.AsyncClient(transport=transport, base_url='http://node') as client:
            pending = asyncio.create_task(client.post('/api/peer/device-sync/join', json={}))
            try:
                assert await asyncio.to_thread(entered.wait, 3)
                response = await asyncio.wait_for(client.get('/health-test'), timeout=1)
                assert response.json() == {'ok': True}
            finally:
                release.set()
            assert (await pending).status_code == 200

    asyncio.run(scenario())


def test_worker_sanitizes_network_error(tmp_path):
    def offline(*_):
        raise OSError('private invite and address')
    a = Node(tmp_path / 'A', 'http://127.0.0.1:18901')
    b = Node(tmp_path / 'B', 'http://127.0.0.1:18902', offline)
    uri = a.request('POST', '/invites', json={'scopes': []})['uri']
    b.request('POST', '/join', json={'uri': uri, 'scopes': []})
    with pytest.raises(RuntimeError, match='^sync_pairing_retry_unavailable$'):
        b.tick()
    assert b.request('GET')['devices'][0]['status'] == 'awaiting_inviter'


def test_installed_worker_moves_real_sources_through_encrypted_http_batches(tmp_path):
    from test_ask_history import sample
    from test_device_sync_reading import ITEM
    nodes, paths = {}, []

    def post(endpoint, path, wire):
        paths.append(path)
        result = nodes[endpoint].client.post(path, json=wire)
        assert result.status_code == 200, result.text
        return result.json()

    a = Node(tmp_path / 'A', 'http://127.0.0.1:18901', post, transfer=True)
    b = Node(tmp_path / 'B', 'http://127.0.0.1:18902', post, transfer=True)
    nodes.update({a.endpoint: a, b.endpoint: b})
    scopes = ['bookmarks', 'reading', 'conversations']
    a.reader.record(ITEM, 'bookmark')
    a.reader.record(ITEM, 'progress', progress=.7)
    conversation = sample()
    conversation['messages'][0]['content'] = 'Large history fragment. ' * 6000
    a.history.save(conversation, expected_revision=0)
    uri = a.request('POST', '/invites', json={'scopes': scopes})['uri']
    pair_id = b.request('POST', '/join', json={'uri': uri, 'scopes': scopes})['id']
    b.tick()
    assert not b.reader.path.exists() and not b.history.path.exists()
    a.request('POST', f'/devices/{pair_id}/approve', json={'review_token': pair_id, 'scopes': scopes, 'verification_code': code_for(pair_id)})
    b.tick()
    for _ in range(3):
        a.tick()
    status = a.request('GET')
    assert status['data_transfer_available']
    assert status['devices'][0]['sync']['state'] == 'confirmed'
    assert b.reader.sync_read('reading', ITEM['item_id'])['value']['progress'] == .7
    assert b.history.get(conversation['id'])['messages'][0]['content'] == conversation['messages'][0]['content']
    assert paths.count('/api/peer/device-sync/batch') == 3
    a.reader.record(ITEM, 'unbookmark')
    assert a.request('GET')['devices'][0]['sync']['pending'] == 1
    assert b.client.post('/api/peer/device-sync/batch', json={}).status_code == 403


def paired(tmp_path, scopes):
    """Two installed nodes with an approved pair, ready to exchange data batches."""
    nodes = {}

    def post(endpoint, path, wire):
        result = nodes[endpoint].client.post(path, json=wire)
        assert result.status_code == 200, result.text
        return result.json()

    a = Node(tmp_path / 'A', 'http://127.0.0.1:18901', post, transfer=True)
    b = Node(tmp_path / 'B', 'http://127.0.0.1:18902', post, transfer=True)
    nodes.update({a.endpoint: a, b.endpoint: b})
    uri = a.request('POST', '/invites', json={'scopes': scopes})['uri']
    pair_id = b.request('POST', '/join', json={'uri': uri, 'scopes': scopes})['id']
    b.tick()
    a.request('POST', f'/devices/{pair_id}/approve', json={'review_token': pair_id, 'scopes': scopes, 'verification_code': code_for(pair_id)})
    b.tick()
    return a, b, pair_id


def test_transfer_receive_returns_signed_receipts_with_rejections(tmp_path):
    from test_device_sync_reading import ITEM

    from rynmesh.device_sync import records
    scopes = ['reading']
    a, b, pair_id = paired(tmp_path, scopes)

    def position(progress):
        return {'item': ITEM, 'progress': progress, 'completed': False, 'content_version': ''}

    # A restored backup reissues one actor counter with a different value. That
    # row cannot merge on B; every other row in the scope must still arrive.
    for node, progress in ((a, .9), (b, .2)):
        actor = node.app.state.device_sync.transfer.replica.actor
        node.reader.enable_sync(actor, scopes)
        record = records.write('reading', ITEM['item_id'], records.empty(), 'c' * 64, position(progress))
        node.reader.sync_receive([{'scope': 'reading', 'id': ITEM['item_id'], 'record': record}], scopes=scopes)
    a.reader.record({**ITEM, 'item_id': 'other'}, 'progress', progress=.5)

    sender, receiver = a.app.state.device_sync.transfer, b.app.state.device_sync.transfer
    wire = sender.prepare(pair_id, 'reading')
    sent = sender._outgoing(sender._pair(pair_id), wire)
    assert {row['id'] for row in sent['records']} == {ITEM['item_id'], 'other'}
    receipts = sender._open(receiver.receive(wire), reply=True)[1]['receipts']
    marked = {receipt['id']: receipt for receipt in receipts}
    assert marked[ITEM['item_id']]['rejected'] == 'sync_dot_conflict'
    assert 'rejected' not in marked['other']
    # Removing the marker leaves exactly the receipts the sender already checks.
    assert [{key: value for key, value in receipt.items() if key != 'rejected'} for receipt in receipts] \
        == sender._receipts(sent['records'], 'reading')
    assert b.reader.sync_read('reading', 'other')['value']['progress'] == .5
    assert b.reader.sync_read('reading', ITEM['item_id'])['value']['progress'] == .2


def test_conversation_batch_merges_the_good_row_and_rejects_the_unmergeable_one(tmp_path):
    from test_ask_history import sample

    from rynmesh.device_sync import records
    a, b, pair_id = paired(tmp_path, ['conversations'])

    def reissued(text):
        conversation = sample() | {'title': text}
        conversation['messages'] = [{**conversation['messages'][0], 'content': text}]
        return records.write('conversations', conversation['id'], records.empty(), 'c' * 64, conversation)

    # The same reissued counter carries a different conversation on each device,
    # so the ask-history store cannot merge that row.
    for node, text in ((a, 'From the restored backup'), (b, 'Written on this device')):
        node.history.enable_sync()
        node.history.sync_receive([{'scope': 'conversations', 'id': sample()['id'], 'record': reissued(text)}])
    a.history.save(sample() | {'id': 'conversation-2'}, expected_revision=0)

    sender, receiver = a.app.state.device_sync.transfer, b.app.state.device_sync.transfer
    wire = sender.prepare(pair_id, 'conversations')
    receipts = sender._open(receiver.receive(wire), reply=True)[1]['receipts']
    marked = {receipt['id']: receipt for receipt in receipts}
    assert marked[sample()['id']]['rejected'] == 'sync_dot_conflict'
    assert 'rejected' not in marked['conversation-2']
    assert [{key: value for key, value in receipt.items() if key != 'rejected'} for receipt in receipts] \
        == sender._receipts(sender._outgoing(sender._pair(pair_id), wire)['records'], 'conversations')
    assert b.history.get('conversation-2')['messages'][0]['content'] == sample()['messages'][0]['content']
    assert b.history.get(sample()['id'])['messages'][0]['content'] == 'Written on this device'


def test_receiver_refuses_a_source_answer_that_does_not_match_the_rows_it_was_given(tmp_path):
    from test_device_sync_reading import ITEM

    from rynmesh.device_sync.records import SyncError
    a, b, pair_id = paired(tmp_path, ['reading'])
    a.reader.enable_sync(a.app.state.device_sync.transfer.replica.actor, ['reading'])
    for identifier in (ITEM['item_id'], 'other'):
        a.reader.record({**ITEM, 'item_id': identifier}, 'progress', progress=.5)
    sender, receiver = a.app.state.device_sync.transfer, b.app.state.device_sync.transfer
    wire = sender.prepare(pair_id, 'reading')
    honest = sender._receipts(sender._outgoing(sender._pair(pair_id), wire)['records'], 'reading')
    assert len(honest) == 2

    class Bridge:
        def __init__(self, receipts):
            self.receipts = receipts

        def receive(self, rows, *, scopes):
            return self.receipts

    # A source that answers for other rows, in another order, or for fewer rows
    # than it was given can never have its answer signed as a receipt.
    for receipts in ([honest[1], honest[0]], [honest[0] | {'id': 'ghost'}, honest[1]], honest[:1]):
        receiver._initialize = lambda current, scope, answer=receipts: Bridge(answer)
        with pytest.raises(SyncError, match='sync_receipt_invalid'):
            receiver.receive(wire)
    assert not b.reader.path.exists()


def test_owner_reading_choice_uses_stored_candidate_and_rejects_stale_revision(tmp_path):
    from test_device_sync_reading import ITEM

    from rynmesh.device_sync import records
    node = Node(tmp_path / 'A', transfer=True)
    node.reader.enable_sync(node.service.store.actor, ['reading'])
    for actor, position in (('b' * 64, .2), ('c' * 64, .8)):
        record = records.write('reading', ITEM['item_id'], records.empty(), actor,
            {'item': ITEM, 'progress': position, 'completed': False, 'content_version': ''})
        node.reader.sync_receive([{'scope': 'reading', 'id': ITEM['item_id'], 'record': record}], scopes=['reading'])
    result = node.request('GET', '/reading/conflicts')
    assert result['local_actor'] == node.service.store.actor
    issue = result['conflicts'][0]
    choice = next(row for row in issue['candidates'] if row['value']['progress'] == .2)
    payload = {'id': issue['id'], 'scope': 'reading', 'expected_revision': issue['revision'],
               'choice_id': choice['choice_id'], 'progress': .99}
    assert node.request('POST', '/reading/resolve', json=payload)['value']['progress'] == .2
    assert node.request('POST', '/reading/resolve', json=payload)['value']['progress'] == .2
    node.reader.record(ITEM, 'progress', progress=.4)
    response = node.client.post(PREFIX + '/reading/resolve', headers=OWNER, json=payload)
    assert response.status_code == 409 and response.json()['detail'] == 'sync_revision_conflict'
    assert node.request('GET', '/reading/conflicts')['conflicts'] == []


def test_capture_failures_appear_in_status_while_local_write_still_succeeds(tmp_path):
    from test_device_sync_reading import ITEM

    node = Node(tmp_path / 'A', transfer=True)
    node.reader.enable_sync(node.service.store.actor, ['reading'])
    assert node.request('GET')['capture_failures'] == {'count': 0, 'codes': {}}
    item = {**ITEM, 'item_id': 'no-host', 'link': 'https:///no-host'}
    node.reader.record(item, 'progress', progress=.5)
    assert next(row for row in node.reader.list() if row['item_id'] == 'no-host')['progress'] == .5
    assert node.request('GET')['capture_failures'] == {'count': 1, 'codes': {'sync_item_link_invalid': 1}}


def test_quarantined_replica_rows_appear_in_status_with_their_reason(tmp_path):
    """A local row the replica could not merge is reported, not silently dropped.

    `reconcile_source` quarantines such a row instead of raising, so the owner
    only learns about it through the status projection.
    """
    from copy import deepcopy

    from test_device_sync_records import reading

    node = Node(tmp_path / 'A', transfer=True)
    replica = node.app.state.device_sync.transfer.replica
    status = node.request('GET')
    assert status['quarantined'] == [] and status['quarantined_count'] == 0

    replica.write('reading', 'article', reading(0), expected_revision=replica.read('reading', 'article')['revision'])
    batch = replica.pending('b' * 64, ['reading'])['records']
    # The same operation dot cannot acquire a differently encoded value, so the
    # re-import of this row is refused by the merge and quarantined.
    changed = deepcopy(batch)
    old = changed[0]['record']['heads'][0]['value']['progress']
    changed[0]['record']['heads'][0]['value']['progress'] = float(old) if type(old) is int else int(old)
    replica.reconcile_source(changed, scopes=['reading'])

    status = node.request('GET')
    assert status['quarantined'] == [{'scope': 'reading', 'id': 'article', 'code': 'sync_dot_conflict'}]
    assert status['quarantined_count'] == 1


def test_rejected_row_is_acknowledged_and_other_rows_keep_flowing(tmp_path):
    from test_device_sync_reading import ITEM

    from rynmesh.device_sync import records
    scopes = ['reading']
    a, b, pair_id = paired(tmp_path, scopes)

    def position(progress):
        return {'item': ITEM, 'progress': progress, 'completed': False, 'content_version': ''}

    # A restored backup reissues one actor counter with a different value. That
    # row can never merge on the peer; every other row must still keep flowing.
    for node, progress in ((a, .9), (b, .2)):
        actor = node.app.state.device_sync.transfer.replica.actor
        node.reader.enable_sync(actor, scopes)
        record = records.write('reading', ITEM['item_id'], records.empty(), 'c' * 64, position(progress))
        node.reader.sync_receive([{'scope': 'reading', 'id': ITEM['item_id'], 'record': record}], scopes=scopes)
    a.reader.record({**ITEM, 'item_id': 'other'}, 'progress', progress=.5)

    sender = a.app.state.device_sync.transfer

    def batch():
        wire = sender.prepare(pair_id, 'reading')
        return [row['id'] for row in sender._outgoing(sender._pair(pair_id), wire)['records']]

    a.tick()
    sync = a.request('GET')['devices'][0]['sync']
    assert b.reader.sync_read('reading', 'other')['value']['progress'] == .5
    assert b.reader.sync_read('reading', ITEM['item_id'])['value']['progress'] == .2
    assert sync['rejected_by_peer'] == {'reading': 1}
    assert sync['pending'] == 0 and sync['error_code'] == '' and sync['state'] == 'confirmed'
    # The peer answered for the exact revision it refused, so the identical
    # batch is not rebuilt tick after tick.
    assert batch() == []
    a.tick()
    assert a.request('GET')['devices'][0]['sync']['rejected_by_peer'] == {'reading': 1}
    # The same row is unmergeable in the other direction, so both owners see it.
    b.tick()
    assert b.request('GET')['devices'][0]['sync']['rejected_by_peer'] == {'reading': 1}
    # Changing the record locally sends it again; nothing else is resent.
    a.reader.record(ITEM, 'progress', progress=.95)
    assert a.request('GET')['devices'][0]['sync']['pending'] == 1
    assert batch() == [ITEM['item_id']]

    class Merging:
        """A peer whose source merges every row it is given this time."""

        def receive(self, rows, *, scopes):
            return [{'scope': row['scope'], 'id': row['id'], 'revision': records.fingerprint(row['record'])} for row in rows]

    b.app.state.device_sync.transfer._initialize = lambda current, scope: Merging()
    a.tick()
    sync = a.request('GET')['devices'][0]['sync']
    assert sync['rejected_by_peer'] == {} and sync['state'] == 'confirmed' and sync['pending'] == 0


def test_rejected_ids_are_capped_while_the_count_stays_whole():
    from rynmesh.device_sync.store import MAX_BATCH
    from rynmesh.device_sync.transfer import DeviceTransfer
    state, refused = {}, {f'row-{index}': 'sync_dot_conflict' for index in range(MAX_BATCH + 20)}
    DeviceTransfer._rejected(state, 'reading', refused)
    # The pairing file keeps one batch of ids; the owner is still told them all.
    assert state['rejected']['reading']['count'] == MAX_BATCH + 20
    assert len(state['rejected']['reading']['rows']) == MAX_BATCH
    DeviceTransfer._rejected(state, 'reading', dict.fromkeys(refused, ''))
    assert 'rejected' not in state
    # A row refused twice is one row, and another row's acceptance is not its own.
    DeviceTransfer._rejected(state, 'reading', {'row-1': 'sync_dot_conflict'})
    DeviceTransfer._rejected(state, 'reading', {'row-1': 'sync_value_invalid', 'row-2': ''})
    assert state['rejected'] == {'reading': {'count': 1, 'rows': {'row-1': 'sync_value_invalid'}}}
    DeviceTransfer._rejected(state, 'reading', {'row-1': ''})
    assert 'rejected' not in state


def test_sender_refuses_an_acknowledgement_that_is_not_the_batch_it_signed():
    from rynmesh.device_sync.records import SyncError
    from rynmesh.device_sync.transfer import DeviceTransfer
    honest = [{'scope': 'reading', 'id': 'first', 'revision': 'a' * 64},
              {'scope': 'reading', 'id': 'second', 'revision': 'b' * 64}]
    # _accept_receipt delegates its whole receipt comparison to _answers.
    assert DeviceTransfer._answers([honest[0], honest[1] | {'rejected': 'sync_dot_conflict'}], honest) \
        == {'first': '', 'second': 'sync_dot_conflict'}
    refused = (
        [honest[0], honest[1] | {'rejected': 'not_a_code'}],
        [honest[0], honest[1] | {'rejected': {}}],
        [honest[0], honest[1] | {'rejected': ''}],
        [honest[0], honest[1] | {'rejected': 'sync_scope_denied'}],  # A batch error is not a row note.
        [honest[1], honest[0]],
        [honest[0], honest[1] | {'id': 'ghost'}],
        [honest[0], honest[1], honest[0]],
        honest[:1],
        [honest[0], 'second'],
        {'receipts': honest},
        None,
    )
    for receipts in refused:
        with pytest.raises(SyncError, match='sync_receipt_invalid'):
            DeviceTransfer._answers(receipts, honest)
