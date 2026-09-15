"""Real-key, durable two-owner protocol tests; transport is an in-memory bus."""
import uuid
from copy import deepcopy

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey

from rynmesh.device_sync import pair_crypto as crypto
from rynmesh.device_sync.pairing import POLICY, POLICY_CHANNEL, PairingService
from rynmesh.device_sync.records import SyncError, fingerprint


class Devices:
    def __init__(self, home):
        self.home, self.now, self.nodes, self.keys = home, 1000, {}, {}
        self.wires, self.drop = [], None

    def node(self, name, *, label=None):
        self.keys.setdefault(name, (Ed25519PrivateKey.generate().private_bytes_raw(), X25519PrivateKey.generate()))
        private, messaging = self.keys[name]
        endpoint = f'http://127.0.0.1:{18000 + ord(name)}'
        node = PairingService(self.home / name, identity_private=private, messaging_key=messaging,
                              name=label or name, endpoint=endpoint, clock=lambda: self.now,
                              post_json=self.post, allow_loopback=True)
        self.nodes[endpoint] = node
        return node

    def post(self, endpoint, path, wire):
        self.wires.append((endpoint, path, deepcopy(wire)))
        action = path.rsplit('/', 1)[-1]
        response = getattr(self.nodes[endpoint], 'receive_' + action)(wire)
        if self.drop == action:
            self.drop = None
            raise TimeoutError('response lost after commit')
        return response

    def pair(self, scopes=None):
        scopes = ['bookmarks', 'conversations', 'reading'] if scopes is None else scopes
        a, b = self.node('A'), self.node('B')
        invite = a.create_invite(scopes)
        pair = b.join(invite['uri'], scopes)
        a.approve(pair['id'], review_token=pair['review_token'], scopes=scopes)
        b.retry(pair['id'])
        return a, b, pair['id']


@pytest.fixture
def devices(tmp_path):
    return Devices(tmp_path)


def authorized(node, remote, pair_id, *, scopes=None, sender_revision=1, receiver_revision=1):
    return node.authorize(pair_id, remote_actor=remote.identity['actor'], scopes=scopes or ['bookmarks'],
                          sender_revision=sender_revision, receiver_revision=receiver_revision)


def test_both_owners_must_confirm_and_only_agreed_scopes_activate(devices):
    a, b = devices.node('A'), devices.node('B')
    invite = a.create_invite(['reading', 'bookmarks', 'conversations'])
    pair = b.join(invite['uri'], ['bookmarks', 'reading'])
    pair_id = pair['id']
    assert pair['status'] == 'awaiting_inviter'
    assert a.get(pair_id)['status'] == 'awaiting_owner'
    assert a.get(pair_id)['verification_code'] == pair['verification_code']
    for node, remote in ((a, b), (b, a)):
        with pytest.raises(SyncError, match='sync_device_not_active'):
            authorized(node, remote, pair_id)
    with pytest.raises(SyncError, match='sync_pairing_review_changed'):
        a.approve(pair_id, review_token='stale-screen', scopes=['bookmarks'])
    a.approve(pair_id, review_token=pair_id, scopes=['bookmarks'])
    assert a.get(pair_id)['effective_scopes'] == []
    assert b.retry(pair_id)['status'] == 'active'
    assert a.get(pair_id)['effective_scopes'] == ['bookmarks']
    authorized(a, b, pair_id)
    with pytest.raises(SyncError, match='sync_scope_denied'):
        authorized(a, b, pair_id, scopes=['reading'])
    assert {path.rsplit('/', 1)[-1] for _, path, _ in devices.wires} == {'join', 'confirm'}
    # Pairing cannot create or read either personal source store.
    assert not (devices.home / 'A' / 'ask-ryn').exists()
    assert not (devices.home / 'B' / 'content').exists()


def test_empty_scope_choice_is_explicit_and_valid(devices):
    a, b, pair_id = devices.pair([])
    assert b.get(pair_id)['effective_scopes'] == []
    with pytest.raises(SyncError, match='sync_scope_denied'):
        authorized(a, b, pair_id)
    with pytest.raises(SyncError):
        a.create_invite(None)


@pytest.mark.parametrize('action', ['join', 'confirm'])
def test_response_loss_and_restart_reuses_committed_intent(devices, action):
    a, b = devices.node('A'), devices.node('B')
    invite = a.create_invite(['bookmarks'])
    pair_id = b.start_join(invite['uri'], ['bookmarks'])['id']
    if action == 'confirm':
        b.retry(pair_id)
        a.approve(pair_id, review_token=pair_id, scopes=['bookmarks'])
    devices.drop = action
    with pytest.raises(TimeoutError):
        b.retry(pair_id)
    a, b = devices.node('A', label='Renamed A'), devices.node('B', label='Renamed B')
    if action == 'join':
        a.approve(pair_id, review_token=pair_id, scopes=['bookmarks'])
    else:
        assert a.get(pair_id)['status'] == 'active'
        assert b.get(pair_id)['status'] == 'awaiting_ack'
        devices.now += 4000  # A committed before expiry; a lost ack must remain recoverable.
    assert b.retry(pair_id)['status'] == 'active'
    assert len(a.list()) == len(b.list()) == 1
    repeated = [wire for _, path, wire in devices.wires if path.endswith('/' + action)]
    assert repeated[0] == repeated[-1]


@pytest.mark.parametrize('cancel', [False, True])
def test_expired_or_cancelled_pending_invite_never_activates(devices, cancel):
    a, b = devices.node('A'), devices.node('B')
    invite = a.create_invite(['bookmarks'], ttl_seconds=60)
    pair_id = b.join(invite['uri'], ['bookmarks'])['id']
    a.approve(pair_id, review_token=pair_id, scopes=['bookmarks'])
    if cancel:
        a.cancel_invite(invite['invite']['id'])
    else:
        devices.now += 61
    assert b.retry(pair_id)['status'] == 'rejected'
    # A cancelled pair is a finished row the store compacts away on its next mutate, so a
    # later lookup reports "not found" rather than "not active"; an expired-but-never-approved
    # pair stays awaiting (never dropped) and is still found, just not active.
    expected = 'sync_pairing_not_found' if cancel else 'sync_device_not_active'
    with pytest.raises(SyncError, match=expected):
        authorized(a, b, pair_id)


def test_expired_and_cancelled_invites_free_capacity(devices):
    a = devices.node('A')
    scopes = ['bookmarks']
    for _ in range(256):
        a.create_invite(scopes, ttl_seconds=60)
    assert len(a.list_invites()) == 256

    devices.now += 61  # past every invite's TTL
    for _ in range(44):
        a.create_invite(scopes, ttl_seconds=60)  # the expired batch must be compacted away, not refused
    assert len(a.list_invites()) == 44

    for _ in range(212):
        a.create_invite(scopes, ttl_seconds=3600)
    assert len(a.list_invites()) == 256

    cancel_id = a.list_invites()[0]['id']
    a.cancel_invite(cancel_id)
    a.create_invite(scopes, ttl_seconds=3600)  # cancelling one must free a slot at the cap
    assert len(a.list_invites()) == 256


def test_receive_join_for_a_compacted_expired_invite_reports_not_found(devices):
    a, b = devices.node('A'), devices.node('B')
    invite = a.create_invite(['bookmarks'], ttl_seconds=60)
    pair_id = b.start_join(invite['uri'], ['bookmarks'])['id']
    wire = b.store.snapshot()['pairs'][pair_id]['join_wire']

    devices.now += 61  # past the invite's TTL
    a.create_invite(['bookmarks'], ttl_seconds=60)  # forces a mutate that compacts the expired invite away
    assert invite['invite']['id'] not in a.store.snapshot()['invites']

    with pytest.raises(SyncError, match='sync_invite_invalid'):
        a.receive_join(wire)


def test_trimming_the_oldest_revoked_pair_clears_its_linked_invite(devices):
    a, b, pair_id = devices.pair()
    a.revoke(pair_id, expected_revision=1)
    # revoke() never touches the invite: A's claimed invite is still linked to the pair it
    # just revoked, and is now the oldest revoked row in the store.
    invites = a.store.snapshot()['invites']
    invite_id = next(iter(invites))
    assert invites[invite_id]['pair_id'] == pair_id

    revoked = a.store.snapshot()['pairs'][pair_id]
    local, remote = revoked['local'], revoked['remote']

    def seed(data):
        # Bulk-insert 256 more schema-valid revoked pairs (reusing A's real identity blocks)
        # so the oldest one -- the real, signed pair created above -- is the one that gets
        # trimmed once the revoked count crosses MAX_RECORDS.
        for index in range(256):
            synthetic_id = fingerprint(f'synthetic-revoked-{index}')
            data['pairs'][synthetic_id] = {'id': synthetic_id, 'role': 'inviter', 'local': local, 'remote': remote,
                'invite_id': uuid.uuid4().hex, 'requested_scopes': [], 'expires': 2000, 'status': 'revoked'}
        return None
    a.store.mutate(seed)  # must not raise sync_pairing_store_unavailable on the dangling pair_id

    snapshot = a.store.snapshot()
    assert pair_id not in snapshot['pairs']  # the oldest revoked pair was trimmed
    assert sum(1 for row in snapshot['pairs'].values() if row['status'] == 'revoked') == 256
    if invite_id in snapshot['invites']:
        assert snapshot['invites'][invite_id]['pair_id'] is None  # cleared, not dangling
    # else: the invite itself was also dropped -- an equally acceptable outcome.

    # The store must still be writable, not permanently locked by a dangling reference.
    a.create_invite(['bookmarks'], ttl_seconds=60)


def test_receive_revoke_for_an_already_compacted_pair_is_idempotent(devices):
    a, b, pair_id = devices.pair()
    assert a.revoke(pair_id, expected_revision=1)['status'] == 'revoked'
    wire = a.store.snapshot()['pairs'][pair_id]['revoke_wire']

    def drop(data):
        data['pairs'].pop(pair_id)
        return None
    b.store.mutate(drop)  # simulate the pair already having been compacted away on B's side

    result = a._response(wire, b.receive_revoke(wire), b.identity)
    assert result['state'] == 'revoked' and result['pair_id'] == pair_id


def test_invite_cannot_admit_second_device_or_changed_intent(devices):
    a, b, c = devices.node('A'), devices.node('B'), devices.node('C')
    invite = a.create_invite(['bookmarks', 'reading'])
    pair_id = b.join(invite['uri'], ['bookmarks'])['id']
    assert c.join(invite['uri'], ['reading'])['status'] == 'rejected'
    with pytest.raises(SyncError, match='sync_pairing_intent_conflict'):
        b.start_join(invite['uri'], ['reading'])
    a.approve(pair_id, review_token=pair_id, scopes=['bookmarks'])
    assert b.retry(pair_id)['status'] == 'active'
    assert len(a.list()) == 1


def test_invitation_signature_secret_domain_and_self_pairing(devices):
    a, b = devices.node('A'), devices.node('B')
    uri = a.create_invite(['bookmarks'])['uri']
    with pytest.raises(SyncError, match='sync_cannot_pair_self'):
        a.start_join(uri, ['bookmarks'])
    with pytest.raises(SyncError, match='sync_invite_invalid'):
        b.start_join(uri.replace('device-pair', 'friend'), ['bookmarks'])
    import json
    envelope = json.loads(crypto.decoded(uri.rsplit('/', 1)[-1]))
    for field in ('secret', 'signature', 'scopes'):
        forged = deepcopy(envelope)
        if field == 'secret':
            forged['secret'] = crypto.encoded(b'x' * 32)
        elif field == 'signature':
            forged['proof']['signature'] = 'x' * 88
        else:
            forged['proof']['payload']['scopes'] = []
        modified = 'rynmesh://device-pair/' + crypto.encoded(crypto.canonical_json(forged))
        with pytest.raises(SyncError, match='sync_invite_invalid'):
            b.start_join(modified, [])
    assert b.list() == []


def test_response_is_bound_to_peer_and_exact_request(devices):
    a, b, c = devices.node('A'), devices.node('B'), devices.node('C')
    pair_id = b.start_join(a.create_invite(['bookmarks'])['uri'], ['bookmarks'])['id']
    wire = b.store.snapshot()['pairs'][pair_id]['join_wire']
    wrong_peer = c._reply(wire, b.identity, pair_id=pair_id, state='pending')
    with pytest.raises(SyncError, match='sync_pairing_response_invalid'):
        b._response(wire, wrong_peer, a.identity)
    wrong_request = a._reply({'unrelated': True}, b.identity, pair_id=pair_id, state='pending')
    with pytest.raises(SyncError, match='sync_pairing_response_invalid'):
        b._response(wire, wrong_request, a.identity)
    tampered = deepcopy(wire)
    tampered['sender'] = c.identity
    with pytest.raises(SyncError, match='sync_pairing_invalid'):
        a.receive_join(tampered)
    assert a.list() == []


def test_policy_intersection_pause_resume_and_old_packet_rejection(devices):
    a, b, pair_id = devices.pair(['bookmarks'])
    a.configure(pair_id, expected_revision=1, scopes=['bookmarks', 'reading'], paused=False)
    assert a.exchange_policy(pair_id)['effective_scopes'] == ['bookmarks']
    b.configure(pair_id, expected_revision=1, scopes=['bookmarks', 'reading'], paused=False)
    b.exchange_policy(pair_id)
    authorized(a, b, pair_id, scopes=['reading'], sender_revision=2, receiver_revision=2)
    a.configure(pair_id, expected_revision=2, scopes=['bookmarks', 'reading'], paused=True)
    with pytest.raises(SyncError, match='sync_paused'):
        authorized(a, b, pair_id, sender_revision=2, receiver_revision=2)
    a.exchange_policy(pair_id)
    assert b.get(pair_id)['remote_paused']
    a.configure(pair_id, expected_revision=3, scopes=['bookmarks'], paused=False)
    a.exchange_policy(pair_id)
    with pytest.raises(SyncError, match='sync_policy_changed'):
        authorized(a, b, pair_id, sender_revision=2, receiver_revision=2)
    authorized(a, b, pair_id, sender_revision=2, receiver_revision=4)
    with pytest.raises(SyncError, match='sync_scope_denied'):
        authorized(a, b, pair_id, scopes=['reading'], sender_revision=2, receiver_revision=4)
    devices.now += 86400
    authorized(a, b, pair_id, sender_revision=2, receiver_revision=4)


def test_policy_replay_cannot_undo_newer_choice_or_cross_channels(devices):
    a, b, pair_id = devices.pair()
    a.exchange_policy(pair_id)
    old = devices.wires[-1][2]
    a.configure(pair_id, expected_revision=1, scopes=[], paused=True)
    a.exchange_policy(pair_id)
    b.receive_policy(old)
    assert b.get(pair_id)['remote_paused']
    with pytest.raises(SyncError, match='sync_pairing_invalid'):
        b.receive_join(old)
    conflicting = a._seal({'kind': POLICY, 'sender': a.identity, 'receiver': b.identity['peer_id'],
        'pair_id': pair_id, 'policy': {'revision': 2, 'scopes': ['bookmarks'], 'paused': False}}, b.identity, channel=POLICY_CHANNEL)
    with pytest.raises(SyncError, match='sync_policy_revision_conflict'):
        b.receive_policy(conflicting)


def test_revocation_stops_local_access_and_retries_notice_after_restart(devices):
    a, b, pair_id = devices.pair(['bookmarks'])
    old_confirm = b.store.snapshot()['pairs'][pair_id]['confirm_wire']
    assert a.revoke(pair_id, expected_revision=1)['removal_pending']
    private = a.store.snapshot()['pairs'][pair_id]
    assert not {'secret', 'offer', 'join_wire', 'confirm_wire'} & set(private)
    with pytest.raises(SyncError, match='sync_device_not_active'):
        authorized(a, b, pair_id)
    devices.drop = 'revoke'
    with pytest.raises(TimeoutError):
        a.retry_removal(pair_id)
    assert b.get(pair_id)['status'] == 'revoked'
    a, b = devices.node('A'), devices.node('B')
    assert not a.retry_removal(pair_id)['removal_pending']
    assert b._response(old_confirm, a.receive_confirm(old_confirm), a.identity)['state'] == 'rejected'
    new_pair = b.join(a.create_invite(['bookmarks'])['uri'], ['bookmarks'])
    assert new_pair['id'] != pair_id
    a.approve(new_pair['id'], review_token=new_pair['id'], scopes=['bookmarks'])
    b.retry(new_pair['id'])
    old_notice = [wire for _, path, wire in devices.wires if path.endswith('/revoke')][0]
    b.receive_revoke(old_notice)
    assert b.get(new_pair['id'])['status'] == 'active'
    with pytest.raises(SyncError, match='sync_device_not_active'):
        authorized(a, b, pair_id)


def test_failed_source_commit_cannot_emit_activation_ack(devices, monkeypatch):
    a, b = devices.node('A'), devices.node('B')
    pair_id = b.join(a.create_invite(['bookmarks'])['uri'], ['bookmarks'])['id']
    a.approve(pair_id, review_token=pair_id, scopes=['bookmarks'])
    import rynmesh.device_sync.pair_store as persistence
    original = persistence.atomic_write_json

    def fail_a(path, *args, **kwargs):
        if path == a.store.path:
            raise OSError('disk full')
        return original(path, *args, **kwargs)

    monkeypatch.setattr(persistence, 'atomic_write_json', fail_a)
    with pytest.raises(OSError):
        b.retry(pair_id)
    assert a.get(pair_id)['status'] == 'awaiting_peer'
    assert b.get(pair_id)['status'] == 'awaiting_ack'
    monkeypatch.setattr(persistence, 'atomic_write_json', original)
    assert b.retry(pair_id)['status'] == 'active'


def test_encrypted_storage_private_projection_and_extension_preservation(devices):
    a, b, pair_id = devices.pair()
    a.store.mutate(lambda data: data.update(local_extension={'preserve': True}))
    a.configure(pair_id, expected_revision=1, scopes=[], paused=True)
    assert devices.node('A').store.snapshot()['local_extension'] == {'preserve': True}
    raw = a.store.path.read_text()
    assert 'local_extension' not in raw and 'requested_scopes' not in raw
    public = a.get(pair_id)
    assert not {'secret', 'offer', 'join_wire', 'confirm_wire', 'revoke_wire'} & set(public)
    assert b.identity['messaging_pub'] not in str(public)


@pytest.mark.parametrize('scopes', [None, 'bookmarks', ['bookmarks', 'bookmarks'], ['api_keys'], [True], [{}]])
def test_invalid_scope_types_fail_before_persistence(devices, scopes):
    a = devices.node('A')
    with pytest.raises(SyncError):
        a.create_invite(scopes)
    assert not a.store.path.exists()


def test_boolean_revisions_do_not_authorize_or_revoke(devices):
    a, b, pair_id = devices.pair()
    with pytest.raises(SyncError, match='sync_policy_changed'):
        authorized(a, b, pair_id, sender_revision=True)
    with pytest.raises(SyncError, match='sync_revision_conflict'):
        a.revoke(pair_id, expected_revision=True)


def test_missing_advertised_endpoint_preserves_local_device_controls(devices):
    a, b, pair_id = devices.pair()
    a.identity['endpoint'] = ''
    assert not a.readiness()['pairing_available']
    assert a.get(pair_id)['status'] == 'active'
    assert a.revoke(pair_id, expected_revision=1)['status'] == 'revoked'
    assert not a.retry_removal(pair_id)['removal_pending']
    assert b.get(pair_id)['status'] == 'revoked'


def test_policy_guard_serializes_removal_with_local_source_commit(devices):
    import threading
    a, b, pair_id = devices.pair()
    entered, attempting, release, revoked = (threading.Event() for _ in range(4))
    errors = []

    def commit():
        try:
            with a.authorized(pair_id, remote_actor=b.identity['actor'], scopes=['bookmarks'], sender_revision=1, receiver_revision=1):
                entered.set()
                assert release.wait(3)
        except Exception as exc:
            errors.append(exc)

    def remove():
        try:
            attempting.set()
            a.revoke(pair_id, expected_revision=1)
            revoked.set()
        except Exception as exc:
            errors.append(exc)

    commit_thread = threading.Thread(target=commit)
    remove_thread = threading.Thread(target=remove)
    commit_thread.start()
    assert entered.wait(3)
    remove_thread.start()
    try:
        assert attempting.wait(3)
        assert not revoked.wait(0.05)
    finally:
        release.set()
        commit_thread.join(3)
        remove_thread.join(3)
    assert not errors and revoked.is_set()
    with pytest.raises(SyncError, match='sync_device_not_active'):
        authorized(a, b, pair_id)


@pytest.mark.parametrize('damage', ['future', 'ciphertext', 'policy'])
def test_unreadable_pairing_store_is_preserved_and_never_authorizes(devices, damage):
    from rynmesh.atomic_io import atomic_write_json, read_json
    from rynmesh.device_sync.pair_store import CHANNEL
    from rynmesh.services import peer_box
    a, b, pair_id = devices.pair()
    envelope = read_json(a.store.path)
    if damage == 'future':
        envelope['version'] = 'ryn.device-pairings.v999'
    elif damage == 'ciphertext':
        envelope['ciphertext'] = 'invalid'
    else:
        private = a.store.snapshot()
        private['pairs'][pair_id]['local_policy']['revision'] = True
        envelope['nonce'], envelope['ciphertext'] = peer_box.seal(a.messaging_key, a.store.pub, crypto.canonical_json(private), info=CHANNEL)
    atomic_write_json(a.store.path, envelope)
    before = a.store.path.read_bytes()
    with pytest.raises(SyncError):
        authorized(a, b, pair_id)
    with pytest.raises(SyncError):
        a.configure(pair_id, expected_revision=1, scopes=[], paused=True)
    assert a.store.path.read_bytes() == before
