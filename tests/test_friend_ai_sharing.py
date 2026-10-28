"""Real signatures/stores with explicit simulated transport and storage faults."""
from copy import deepcopy
from datetime import timedelta

import pytest
from test_friends import Mesh, _node

from rynmesh.ai_access.catalog import PATH, FriendAICatalog
from rynmesh.ai_access.store import AIAccessStore
from rynmesh.crypto import canonical_json
from rynmesh.friends.ai_sharing import FriendAISharing
from rynmesh.friends.service import FriendError


class AIMesh(Mesh):
    def post(self, endpoint, path, payload, headers, **kwargs):
        node = self.nodes[endpoint]
        if endpoint not in self.online:
            raise OSError('offline')
        if path == '/api/peer/friends/ai-review':
            return node.ai_sharing.peer_review(payload)
        if path == PATH:
            relation = node.verify_request(path=path, body=canonical_json(payload), headers=headers)
            return node.ai_sharing.catalog().respond(payload, relation)
        return super().post(endpoint, path, payload, headers, **kwargs)


def setup(tmp_path):
    mesh = AIMesh()
    nodes = [_node(tmp_path, name, 19100 + i, mesh) for i, name in enumerate(('A', 'B', 'C'))]
    status = {'configured': True, 'ready': True, 'online': True, 'publication_enabled': True,
              'capacity': {'available': 1}, 'network_id': 'rynmesh-main',
              'service': {'package_id': 'model-x', 'model_alias': 'Qwen', 'model_fingerprint': 'sha256:123',
                          'pricing': {'currency': 'DEV_TASK_BALANCE', 'input_per_1k': 0,
                                      'output_per_1k': 0, 'minimum': 0, 'maximum_per_task': 1}}}
    for node in nodes:
        grants = AIAccessStore(node.home, relationship=node.store.relationship)
        def provider(n=node):
            return deepcopy(status) if n is nodes[0] else None
        catalog = FriendAICatalog(friends=lambda n=node: n, grants=lambda g=grants: g, provider=provider)
        node.ai_sharing = FriendAISharing(node, provider=provider, grants=lambda g=grants: g, catalog=lambda c=catalog: c)
    return *nodes, mesh, status


def invite(a, b):
    invitation = a.ai_sharing.create('model-x')
    uri = invitation['invite_uri']
    preview = b.inspect_invite(uri)
    return uri, preview['review_token'], preview['invite_id']


def test_ordinary_invite_never_grants_ai_and_ai_requires_explicit_review(tmp_path):
    a, b, _, _, _ = setup(tmp_path)
    b.join(a.create_invite()['invite_uri'])
    assert a.ai_sharing.grants().list() == []
    uri, _, _ = invite(a, b)
    with pytest.raises(FriendError, match='ai_review_required'):
        b.join(uri)
    assert a.ai_sharing.grants().list() == []


@pytest.mark.parametrize('revoked', [False, True])
def test_reviewed_address_recovery_keeps_relationship_and_permission_revision(tmp_path, revoked):
    a, b, _, mesh, _ = setup(tmp_path)
    uri, token, op = invite(a, b)
    b.ai_sharing.begin(uri, token)
    rid = b.list_friends()[0]['relationship_id']
    if revoked:
        a.ai_sharing.grants().set('model-x', rid, allowed=False, expected_revision=1)
    grants_before = a.ai_sharing.grants().list()
    relation_before = b.store.relationship(rid)
    old_address = a.endpoint
    a.endpoint = 'http://127.0.0.1:19222'
    mesh.nodes[a.endpoint] = a
    mesh.online.add(a.endpoint)
    mesh.online.remove(old_address)
    address_invite = a.create_invite()
    restored = b.ai_sharing.reconnect(address_invite['invite_uri'])
    assert restored['relationship_id'] == rid and restored['endpoint'] == a.endpoint
    assert b.store.relationship(rid) == {**relation_before, 'endpoint': a.endpoint}
    assert a.ai_sharing.grants().list() == grants_before
    assert len(a.list_friends()) == len(b.list_friends()) == 1
    assert a.store.state()['invites'][address_invite['invite']['invite_id']]['status'] == 'active'
    result = b.ai_sharing.resume(op)
    assert result['error'] == 'ai_invite_revoked' if revoked else result['stage'] == 'complete'


def test_address_recovery_rejects_unreachable_or_removed_peer_without_changing_address(tmp_path, monkeypatch):
    a, b, _, mesh, _ = setup(tmp_path)
    uri, token, _ = invite(a, b)
    b.ai_sharing.begin(uri, token)
    original = b.list_friends()[0]
    a.endpoint = 'http://127.0.0.1:19222'
    invitation = a.create_invite()['invite_uri']
    with pytest.raises(FriendError, match='could_not_join_friend'):
        b.ai_sharing.reconnect(invitation)
    assert b.list_friends()[0] == original
    mesh.nodes[a.endpoint] = a
    mesh.online.add(a.endpoint)
    post = b.post_json
    def revoke_during_response(*args, **kwargs):
        response = post(*args, **kwargs)
        b.store.revoke(original['relationship_id'], b.clock().isoformat())
        return response
    monkeypatch.setattr(b, 'post_json', revoke_during_response)
    with pytest.raises(FriendError, match='could_not_join_friend'):
        b.ai_sharing.reconnect(invitation)
    saved = b.store.relationship(original['relationship_id'], active_only=False)
    assert saved['status'] == 'revoked' and saved['endpoint'] == original['endpoint']


def test_signed_single_use_operation_survives_response_loss_and_restart(tmp_path, monkeypatch):
    a, b, c, mesh, _ = setup(tmp_path)
    uri, token, op = invite(a, b)
    post = b.post_json
    def lose_response(endpoint, path, payload, headers, **kwargs):
        result = post(endpoint, path, payload, headers, **kwargs)
        if path.endswith('/accept'):
            raise OSError('simulated response loss after both provider writes')
        return result
    monkeypatch.setattr(b, 'post_json', lose_response)
    assert b.ai_sharing.begin(uri, token)['stage'] == 'joining'
    assert a.ai_sharing.grants().list()[0]['effective']
    assert b.list_friends() == []
    assert b.ai_sharing.read(op)['uri'] == uri
    monkeypatch.setattr(b, 'post_json', post)
    b.ai_sharing = FriendAISharing(b, provider=lambda: None, grants=b.ai_sharing.grants, catalog=b.ai_sharing.catalog)
    result = b.ai_sharing.resume(op)
    assert result['stage'] == 'complete' and result['service_id'] == 'model-x'
    assert len(a.list_friends()) == len(b.list_friends()) == 1
    assert 'uri' not in b.ai_sharing.read(op)
    assert 'review_token' not in b.ai_sharing.read(op)
    assert b.ai_sharing.resume(op)['stage'] == 'complete'
    assert c.ai_sharing.begin(uri, token)['error'] == 'invite_used'
    assert len(a.list_friends()) == 1


@pytest.mark.parametrize('barrier', ['cancel', 'revoke', 'remove'])
def test_partial_grant_write_never_revives_after_owner_barrier(tmp_path, monkeypatch, barrier):
    a, b, _, _, _ = setup(tmp_path)
    uri, token, op = invite(a, b)
    grants = a.ai_sharing.grants()
    original = grants.set
    monkeypatch.setattr(grants, 'set', lambda *args, **kwargs: (_ for _ in ()).throw(OSError('disk full')))
    row = b.ai_sharing.begin(uri, token)
    assert row['stage'] == 'joining' and row['error'] == 'ai_grant_pending'
    assert len(a.list_friends()) == 1 and not b.list_friends()
    assert a.list_invites()[0]['ai_stage'] == 'grant_pending'
    monkeypatch.setattr(grants, 'set', original)
    rid = a.list_friends()[0]['relationship_id']
    if barrier == 'cancel':
        a.cancel_invite(op)
    elif barrier == 'revoke':
        grants.set('model-x', rid, allowed=False, expected_revision=0)
    else:
        a.revoke(rid, notify=False)
    retry = b.ai_sharing.resume(op)
    assert retry['stage'] != 'complete'
    assert not any(g['effective'] for g in grants.list())
    assert len(a.list_friends()) == 1


def test_grant_progress_write_failure_recovers_exact_grant(tmp_path, monkeypatch):
    a, b, _, _, _ = setup(tmp_path)
    uri, token, op = invite(a, b)
    patch = a.store.patch_invite
    monkeypatch.setattr(a.store, 'patch_invite', lambda *a, **k: (_ for _ in ()).throw(OSError('crash before progress')))
    assert b.ai_sharing.begin(uri, token)['stage'] == 'joining'
    assert a.ai_sharing.grants().list()[0]['revision'] == 1
    monkeypatch.setattr(a.store, 'patch_invite', patch)
    assert b.ai_sharing.resume(op)['stage'] == 'complete'
    assert a.ai_sharing.grants().list()[0]['revision'] == 1


def test_lost_cancel_write_is_already_fenced_and_no_regrant(tmp_path, monkeypatch):
    a, b, _, _, _ = setup(tmp_path)
    uri, token, op = invite(a, b)
    original = a.store.patch_invite
    monkeypatch.setattr(a.store, 'patch_invite', lambda *a, **k: (_ for _ in ()).throw(OSError('progress lost')))
    b.ai_sharing.begin(uri, token)
    monkeypatch.setattr(a.store, 'patch_invite', original)
    monkeypatch.setattr(a.store, 'cancel_invite', lambda *a, **k: (_ for _ in ()).throw(OSError('cancel write lost')))
    with pytest.raises(OSError):
        a.cancel_invite(op)
    assert b.ai_sharing.resume(op)['error'] == 'ai_invite_revoked'
    assert a.ai_sharing.grants().list()[0]['allowed'] is False


def test_price_or_model_changes_require_new_review_and_service_stays_bound(tmp_path):
    a, b, _, _, status = setup(tmp_path)
    uri, token, op = invite(a, b)
    status['service']['pricing']['minimum'] = 0.2
    assert b.ai_sharing.begin(uri, token)['error'] == 'ai_review_changed'
    assert not a.list_friends()
    review = b.ai_sharing.rereview(op)
    assert review['changed'] and review['ai']['service']['pricing']['minimum'] == 0.2
    status['service']['model_fingerprint'] = 'sha256:456'
    assert b.ai_sharing.resume(op, review['review_token'])['error'] == 'ai_review_changed'
    review = b.ai_sharing.rereview(op)
    assert b.ai_sharing.resume(op, review['review_token'])['stage'] == 'complete'
    status['service']['package_id'] = 'replacement'
    assert b.ai_sharing.resume(op)['stage'] != 'complete'
    assert len(a.ai_sharing.grants().list()) == 1
    assert a.ai_sharing.grants().list()[0]['service_id'] == 'model-x'


def test_expired_cancelled_and_unready_invites_have_no_new_grants(tmp_path):
    a, b, _, _, status = setup(tmp_path)
    for condition in ('expired', 'cancelled', 'unready', 'busy', 'paused'):
        uri, token, op = invite(a, b)
        if condition == 'expired':
            now = a.clock()
            a.clock = lambda stamp=now: stamp + timedelta(minutes=16)
        elif condition == 'cancelled':
            a.cancel_invite(op)
        elif condition == 'unready':
            status['ready'] = False
        elif condition == 'busy':
            status['capacity']['available'] = 0
        else:
            status['publication_enabled'] = False
        assert b.ai_sharing.begin(uri, token)['stage'] != 'complete'
        assert a.ai_sharing.grants().list() == []
        status.update(ready=True, publication_enabled=True, capacity={'available': 1})
    assert not a.list_friends()


def test_discovery_failure_preserves_operation_then_revocation_blocks_resume(tmp_path, monkeypatch):
    a, b, _, _, _ = setup(tmp_path)
    uri, token, op = invite(a, b)
    catalog = b.ai_sharing.catalog()
    refresh = catalog.refresh
    monkeypatch.setattr(catalog, 'refresh', lambda _: (_ for _ in ()).throw(OSError('network lost')))
    assert b.ai_sharing.begin(uri, token)['stage'] == 'discovering'
    assert len(b.list_friends()) == 1
    grant = a.ai_sharing.grants().list()[0]
    a.ai_sharing.grants().set('model-x', grant['relationship_id'], allowed=False, expected_revision=1)
    monkeypatch.setattr(catalog, 'refresh', refresh)
    assert b.ai_sharing.resume(op)['error'] == 'ai_invite_revoked'
    assert not a.ai_sharing.grants().list()[0]['effective']


def test_abandon_clears_capability_and_does_not_restart(tmp_path):
    a, b, _, mesh, _ = setup(tmp_path)
    uri, token, op = invite(a, b)
    mesh.online.remove(a.endpoint)
    b.ai_sharing.begin(uri, token)
    b.ai_sharing.abandon(op)
    assert 'uri' not in b.ai_sharing.read(op)
    mesh.online.add(a.endpoint)
    with pytest.raises(FriendError, match='cancelled'):
        b.ai_sharing.begin(uri, token)
    assert not a.list_friends()


def test_completed_invitation_reopen_requires_review_when_price_changes(tmp_path):
    a, b, _, _, status = setup(tmp_path)
    uri, token, op = invite(a, b)
    assert b.ai_sharing.begin(uri, token)['stage'] == 'complete'
    status['service']['pricing']['minimum'] = 0.3
    assert b.ai_sharing.resume(op)['error'] == 'ai_review_changed'
    review = b.ai_sharing.rereview(op)
    assert review['ai']['service']['pricing']['minimum'] == 0.3
    assert b.ai_sharing.resume(op, review['review_token'])['stage'] == 'complete'
    assert a.ai_sharing.grants().list()[0]['revision'] == 1


def test_owner_cancel_serializes_with_acceptance_and_fences_final_result(tmp_path, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event

    a, b, _, _, _ = setup(tmp_path)
    uri, token, op = invite(a, b)
    entered, release = Event(), Event()
    original = a.ai_sharing.finish_accept
    def pause(payload, relation):
        entered.set()
        assert release.wait(5)
        return original(payload, relation)
    monkeypatch.setattr(a.ai_sharing, 'finish_accept', pause)
    with ThreadPoolExecutor(2) as pool:
        accept = pool.submit(b.ai_sharing.begin, uri, token)
        assert entered.wait(5)
        cancel = pool.submit(a.cancel_invite, op)
        release.set()
        accept.result(timeout=5)
        cancel.result(timeout=5)
    assert not a.ai_sharing.grants().list()[0]['effective']
    assert b.ai_sharing.resume(op)['stage'] != 'complete'
