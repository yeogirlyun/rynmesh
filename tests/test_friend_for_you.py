import json
import time

import pytest
from test_digest import RSS, make_fetcher
from test_friend_feed import publish, selected, setup

from rynmesh.friend_feed.service import FriendFeed
from rynmesh.friend_feed.store import FeedError, FeedStore
from rynmesh.recommendation_profile import RecommendationProfileStore
from rynmesh.services.digest import DigestError, DigestService


def slate(tmp_path):
    mesh, nodes, feeds, contents, reference, wires = setup(tmp_path)
    author, bob, *_ = nodes
    publication = publish(feeds[0], reference, selected(author, bob))
    rid = bob.store.relationship_for_peer(author.peer_id)['relationship_id']
    feeds[1].subscribe(rid, enabled=True, expected_revision=0)
    feeds[1].refresh(rid)
    profile = RecommendationProfileStore(bob.home / 'profile.json')
    service = DigestService(bob.home, profile_store=profile, friend_items=feeds[1].for_you_items,
                            fetcher=make_fetcher({'https://public.example/feed': RSS}))
    return mesh, nodes, feeds, contents, publication, rid, service, profile, wires


def test_cold_start_mixed_ranking_feedback_undo_and_restart_keep_metadata_private(tmp_path, caplog):
    _, nodes, feeds, contents, _, _, service, profile, wires = slate(tmp_path)
    # No public digest is required to see an explicitly followed friend.
    first = service.for_you_digest()['items'][0]
    assert first['title'] == 'Private feed marker'
    proof = first['friend_provenance']
    assert proof['publisher_peer_id'] == proof['serving_peer_id'] == nodes[0].peer_id
    assert proof['node_name'] == 'Author' and proof['revision'] == 2
    assert first['link'] == '' and first['thumbnail'] == ''
    service.add_source('https://public.example/feed')
    service.build(now_unix=time.time())
    mixed = service.for_you_digest()['items']
    assert any(not item.get('friend_provenance') for item in mixed)
    assert any(item.get('friend_provenance') for item in mixed)
    assert mixed == sorted(mixed, key=lambda item: -item['score'])
    # A source preference participates in the same ranker, not a pinned section.
    before = next(item['score'] for item in mixed if item['item_id'] == first['item_id'])
    for _ in range(4):
        service.feedback(first['item_id'], 'down')
    after = next(item['score'] for item in service.for_you_digest()['items'] if item['item_id'] == first['item_id'])
    assert after <= before
    service.feedback(first['item_id'], 'hide')
    assert all(item['item_id'] != first['item_id'] for item in service.for_you_digest()['items'])
    event = profile.history()['items'][0]
    profile.undo(event['event_id'])
    assert any(item['item_id'] == first['item_id'] for item in service.for_you_digest()['items'])
    restarted_feed = FriendFeed(store=FeedStore(nodes[1].home, messaging_key=nodes[1].messaging_private),
        friends=lambda: nodes[1], content=lambda: contents[1])
    restarted = DigestService(nodes[1].home, profile_store=profile, friend_items=restarted_feed.for_you_items)
    assert any(item['item_id'] == first['item_id'] for item in restarted.for_you_digest()['items'])
    assert all(not item.get('friend_provenance') for item in service.last_digest()['items'])
    assert 'Private feed marker' not in json.dumps(service.recommendation_items())
    assert 'Private feed marker' not in json.dumps(wires) + caplog.text
    for path in nodes[1].home.rglob('*.json'):
        assert 'Private feed marker' not in path.read_text(encoding='utf-8'), path.name


@pytest.mark.parametrize('change', ['unfollow', 'revoke', 'withdraw'])
def test_current_access_removes_cached_candidates_and_feedback_cannot_resurrect_them(tmp_path, change):
    _, nodes, feeds, _, publication, rid, service, _, _ = slate(tmp_path)
    first = service.for_you_digest()['items'][0]
    if change == 'unfollow':
        feeds[1].subscribe(rid, enabled=False, expected_revision=1)
    elif change == 'revoke':
        nodes[1].revoke(rid)
    else:
        feeds[0].stop(publication['id'], expected_revision=publication['revision'], operation_id='f' * 32)
        feeds[1].refresh(rid)
        with pytest.raises(FeedError):
            feeds[1].fetch(rid, publication['id'], expected_revision=2)
    assert service.for_you_digest()['items'] == []
    with pytest.raises(DigestError, match='feedback_item_unknown'):
        service.feedback(first['item_id'], 'up')


def test_offline_provenance_and_corrupt_private_cache_do_not_break_public_feed(tmp_path):
    mesh, nodes, feeds, _, _, rid, service, _, _ = slate(tmp_path)
    service.add_source('https://public.example/feed')
    service.build(now_unix=time.time())
    mesh.online.remove(nodes[0].endpoint)
    with pytest.raises(FeedError):
        feeds[1].refresh(rid)
    friend = next(item for item in service.for_you_digest()['items'] if item.get('friend_provenance'))
    assert friend['friend_provenance']['unreachable'] is True
    assert friend['friend_provenance']['checked_at']
    feeds[1].store.path.write_text('{}', encoding='utf-8')
    degraded = service.for_you_digest()
    assert degraded['friend_feed_unavailable'] is True
    assert degraded['items'] and all(not item.get('friend_provenance') for item in degraded['items'])


def test_automatic_enrichment_and_public_consumers_never_receive_private_metadata(tmp_path):
    _, _, _, _, _, _, service, _, _ = slate(tmp_path)
    service.add_source('https://public.example/feed')

    class Provider:
        id, model = 'test', 'test'
        prompts = []

        def generate(self, prompt, **kwargs):
            self.prompts.append(prompt)
            return 'Public metadata summary'

    provider = Provider()
    service.enrich_latest(provider)
    assert provider.prompts
    assert 'Private feed marker' not in json.dumps(provider.prompts)
    assert any(item.get('friend_provenance') for item in service.for_you_digest()['items'])
    assert 'Private feed marker' not in json.dumps(service.last_digest())


def test_local_digest_route_projects_current_friend_state_only_for_owner(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    from rynmesh.peer_http import create_app
    from rynmesh.store import RynmeshStore

    _, nodes, feeds, _, _, rid, _, _, _ = slate(tmp_path)
    monkeypatch.setenv('RYNMESH_LOCAL_TOKEN', 'test-owner-only')
    monkeypatch.setenv('RYNMESH_AUTO_REGISTER', '0')
    monkeypatch.setenv('RYNMESH_DISABLE_DISCOVERY', '1')
    monkeypatch.setenv('RYNMESH_MODEL_PROVIDER', 'none')
    store = RynmeshStore(home=tmp_path / 'http-consumer', network_dir=tmp_path / 'http-network')
    app = create_app(store)
    app.state.friend_feed.service = feeds[1]
    client = TestClient(app)
    assert client.get('/api/local/digest').status_code == 403
    headers = {'x-ryn-local-token': 'test-owner-only'}
    response = client.get('/api/local/digest', headers=headers)
    assert response.status_code == 200
    assert response.json()['items'][0]['friend_provenance']['publisher_peer_id'] == nodes[0].peer_id
    feeds[1].subscribe(rid, enabled=False, expected_revision=1)
    assert client.get('/api/local/digest', headers=headers).json()['items'] == []
