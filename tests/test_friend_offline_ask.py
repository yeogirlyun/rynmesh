import pytest
from test_friend_feed import publish, selected, setup

from rynmesh.ask_ryn.context import AskContextService
from rynmesh.friend_feed.store import FeedError
from rynmesh.friends.service import FriendError
from rynmesh.offline_reading.service import OfflineReading, OfflineSources
from rynmesh.offline_reading.store import OfflineStore


def test_verified_friend_copy_survives_disconnect_revocation_and_restart_with_ask_provenance(tmp_path):
    mesh, nodes, feeds, contents, reference, _ = setup(tmp_path)
    author, bob = nodes[:2]
    a, b = feeds[:2]
    content = contents[1]
    publication = publish(a, reference, selected(author, bob))
    rid = bob.store.relationship_for_peer(author.peer_id)['relationship_id']
    copy = b.fetch(rid, publication['id'], expected_revision=publication['published']['revision'])
    origin = content.imports.get(copy['import_id'])['source']
    assert origin['peer_id'] == author.peer_id
    sources = OfflineSources(consumption=content.consumption, imports=lambda: content.imports,
                             native=lambda: None, fetch=lambda *args, **kwargs: pytest.fail('No source network request'))
    def restart():
        return OfflineReading(store=OfflineStore(bob.home, messaging_key=bob.messaging_private), sources=sources)
    offline = restart()
    content.offline = lambda: offline
    assert offline.request(copy['library_id'])['state'] == 'queued'
    mesh.online.clear()
    assert offline.run_once()
    offline = restart()
    saved = offline.read(copy['library_id'])
    assert saved['text'] == 'Private article body 中文'
    assert saved['shared_by_peer_id'] == author.peer_id
    assert saved['publisher_peer_id'] == origin['publisher_peer_id']
    author.revoke(author.store.relationship_for_peer(bob.peer_id)['relationship_id'], notify=False)
    assert offline.read(copy['library_id']) == saved
    ask = AskContextService(lambda: content, lambda network: pytest.fail('No model discovery or inference'))
    direct = ask.prepare(copy['library_id'], prefer_source=True)
    prepared = ask.prepare(copy['library_id'], offline_job_id=saved['job_id'])
    assert direct['shared_by_peer_id'] == prepared['shared_by_peer_id'] == author.peer_id
    assert direct['publisher_peer_id'] == prepared['publisher_peer_id'] == origin['publisher_peer_id']
    assert ask.describe(prepared['library_id'], include_text=True)['text'] == saved['text']
    offline.clear(review_token=offline.clear_preview()['review_token'])
    assert ask.describe(prepared['library_id'], include_text=True)['text'] == saved['text']


def test_revoked_publication_never_creates_a_local_copy(tmp_path):
    _, nodes, feeds, contents, reference, _ = setup(tmp_path)
    author, bob = nodes[:2]
    a, b = feeds[:2]
    publication = publish(a, reference, selected(author, bob))
    rid = bob.store.relationship_for_peer(author.peer_id)['relationship_id']
    author.revoke(author.store.relationship_for_peer(bob.peer_id)['relationship_id'], notify=False)
    with pytest.raises((FeedError, FriendError)):
        b.fetch(rid, publication['id'], expected_revision=publication['published']['revision'])
    assert contents[1].consumption().list() == []
