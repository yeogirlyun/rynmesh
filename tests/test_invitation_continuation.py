import pytest
from test_friends import Mesh, _node

from rynmesh.friends.continuation import InvitationContinuation
from rynmesh.friends.crypto import FriendCryptoError


def test_continuation_validates_persists_and_clears_only_reviewed_invitation(tmp_path):
    node = _node(tmp_path, 'A', 19111, Mesh())
    continuation = InvitationContinuation(tmp_path / 'B', allow_loopback=True)
    first = continuation.save(node.create_invite()['invite_uri'])
    restarted = InvitationContinuation(tmp_path / 'B', allow_loopback=True)
    assert restarted.get() == first
    second = restarted.save(node.create_invite()['invite_uri'])
    assert restarted.clear(first['id']) is False  # A new deep link arrived during acceptance.
    assert restarted.get() == second
    assert restarted.clear(second['id']) is True
    assert restarted.get() == {}


def test_invalid_continuation_does_not_overwrite_valid_invitation(tmp_path):
    node = _node(tmp_path, 'A', 19111, Mesh())
    continuation = InvitationContinuation(tmp_path / 'B', allow_loopback=True)
    saved = continuation.save(node.create_invite()['invite_uri'])
    with pytest.raises(FriendCryptoError):
        continuation.save('rynmesh://join/not-a-signed-invite')
    assert continuation.get() == saved


def test_expired_invite_can_be_retained_for_explanation_but_cannot_be_accepted(tmp_path):
    from datetime import timedelta
    node = _node(tmp_path, 'A', 19111, Mesh())
    now = node.clock()
    node.clock = lambda: now - timedelta(hours=1)
    uri = node.create_invite()['invite_uri']
    continuation = InvitationContinuation(tmp_path / 'B', allow_loopback=True)
    assert continuation.save(uri)['invite_uri'] == uri
    node.clock = lambda: now
    with pytest.raises(FriendCryptoError, match='expired'):
        node.inspect_invite(uri)
