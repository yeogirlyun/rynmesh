"""The body the reader displayed stays the body prepared for Ask and sharing."""
import time
from types import SimpleNamespace

import pytest
from test_friends import _pair
from test_offline_reading import BODY, download, fixture

from rynmesh.ask_ryn.context import AskContextService
from rynmesh.ask_ryn.store import ConversationError
from rynmesh.friends.content import FriendContent
from rynmesh.friends.service import FriendError
from rynmesh.services.consumption import ConsumptionStore
from rynmesh.services.library_imports import LibraryImportStore
from rynmesh.services.reader import MAX_BLOCKS, ReaderCache


def content_for(f):
    return FriendContent(store=None, imports=f.imports, cache=lambda: ReaderCache(f.home / 'reader-cache'),
                         consumption=lambda: f.consumption, offline=lambda: f.service)


def test_offline_ask_material_is_local_version_bound_and_survives_download_cleanup(tmp_path):
    f = fixture(tmp_path, images=False)
    original = download(f)
    content = content_for(f)
    ask = AskContextService(lambda: content, lambda network: [])
    f.network['fail'].add(f.item['link'])
    calls = list(f.network['calls'])
    prepared = ask.prepare(f.item['item_id'], offline_job_id=original['job_id'])
    assert prepared['source_url'] == original['url']
    assert ask.describe(prepared['library_id'], include_text=True)['text'] == BODY
    assert f.network['calls'] == calls
    assert ask.prepare(f.item['item_id'], offline_job_id=original['job_id']) == prepared
    f.network['fail'].clear()
    f.network['body'] = BODY + ' Explicitly updated article.'
    f.service.request(f.item['item_id'], update=True)
    assert f.service.run_once()
    with pytest.raises(ConversationError, match='ask_context_changed'):
        ask.prepare(f.item['item_id'], offline_job_id=original['job_id'])
    current = ask.prepare(f.item['item_id'], offline_job_id=f.service.read(f.item['item_id'])['job_id'])
    assert current['library_id'] != prepared['library_id']
    f.service.clear(review_token=f.service.clear_preview()['review_token'])
    assert ask.describe(prepared['library_id'], include_text=True)['text'] == BODY
    assert ask.describe(current['library_id'], include_text=True)['text'] == f.network['body']
    with pytest.raises(ConversationError, match='ask_context_changed'):
        ask.prepare(f.item['item_id'], offline_job_id=original['job_id'])


def test_truncated_html_remains_shortened_in_ask_and_at_the_receiving_friend(tmp_path):
    _, alice, bob = _pair(tmp_path)
    f = fixture(alice.home, images=False)
    html = ('<html><article>' + ''.join(f'<p>Paragraph {index}: this source has more prose than the extraction limit.</p>'
            for index in range(MAX_BLOCKS + 5)) + '</article></html>').encode()
    f.sources.fetch = lambda url, **options: {'data': html, 'url': url, 'mime': 'text/html'}
    body = download(f)
    assert body['truncated'] is True and body['partial'] is True
    assert f'Paragraph {MAX_BLOCKS}:' not in body['text']
    content = content_for(f)
    bob_imports = LibraryImportStore(bob.home / 'library-imports')
    receiver = FriendContent(store=None, imports=bob_imports, cache=lambda: None,
                             consumption=lambda: ConsumptionStore(bob.home / 'consumption.json'))
    alice.resolve_content = content.resolve
    bob.import_content = receiver.import_card
    bob.import_generation = bob_imports.generation
    bob.verify_import = lambda identifier: bob_imports.body(identifier.removeprefix('import:'))
    card = content.prepare({'item_id': f.item['item_id'], 'offline_job_id': body['job_id']})
    card['source_item_id'] = f.item['item_id']
    alice.send_content_card(bob.peer_id, card, card_id='b' * 32)
    assert alice.store.card('b' * 32)['source_offline_job_id'] == body['job_id']
    assert bob.content_cards()[0]['card']['content_truncated'] is True
    fetched = bob.fetch_content_card('b' * 32)
    received = bob_imports.body(fetched['library_id'].removeprefix('import:'))
    assert received['text'] == body['text'] and received['truncated'] is True
    ask = AskContextService(lambda: content, lambda network: [])
    assert ask.describe(card['library_id'])['extraction_truncated'] is True
    assert alice.send_content_card(bob.peer_id, card, card_id='b' * 32)['card_id'] == 'b' * 32
    with pytest.raises(FriendError, match='friend_card_id_conflict'):
        alice.send_content_card(bob.peer_id, {**card, 'source_offline_job_id': 'c' * 32}, card_id='b' * 32)
    alice.revoke(alice.store.relationship_for_peer(bob.peer_id)['relationship_id'], notify=False)
    assert bob_imports.body(fetched['library_id'].removeprefix('import:')) == received


def test_explicit_source_material_does_not_silently_use_a_new_offline_version(tmp_path):
    f = fixture(tmp_path, images=False)
    download(f)
    content = content_for(f)
    content.cache().put(f.item['link'], {'title': 'Source view', 'blocks': [{'text': 'The source version currently being read.'}]}, now=time.time())
    from_source = content.prepare({'item_id': f.item['item_id'], 'prefer_source': True})
    assert f.imports.body(from_source['library_id'].removeprefix('import:'))['text'] == 'The source version currently being read.'
    assert not from_source.get('source_offline_job_id')
    with pytest.raises(FriendError, match='friend_card_content_changed'):
        content.prepare({'item_id': f.item['item_id'], 'offline_job_id': 'a' * 32, 'prefer_source': True})


def test_explicit_source_can_use_independent_import_when_offline_storage_is_unavailable(tmp_path):
    f = fixture(tmp_path, images=False)
    saved = f.imports.save(BODY.encode(), filename='saved.txt', mime='text/plain')
    content = content_for(f)
    content.offline = lambda: SimpleNamespace(resolve=lambda _: (_ for _ in ()).throw(AssertionError('Must use reviewed source')))
    prepared = content.prepare({'item_id': 'import:' + saved['import_id'], 'prefer_source': True})
    assert prepared['library_id'] == 'import:' + saved['import_id']


def test_owner_routes_reject_stale_offline_material_and_retry_the_original_share(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    from rynmesh.peer_http import create_app
    from rynmesh.store import RynmeshStore

    _, alice, bob = _pair(tmp_path / 'mesh')
    f = fixture(alice.home, images=False)
    original = download(f)
    for name, value in {'RYNMESH_HOME': str(alice.home), 'RYNMESH_AUTO_REGISTER': '0',
                        'RYNMESH_DISABLE_DISCOVERY': '1', 'RYNMESH_MODEL_PROVIDER': 'none',
                        'RYNMESH_LOCAL_TOKEN': 'offline-material-owner'}.items():
        monkeypatch.setenv(name, value)
    app = create_app(RynmeshStore(home=alice.home, network_dir=tmp_path / 'network', node_name='Material routes'))
    app.state.friends.service = alice
    app.state.friends.content = content_for(f)
    alice.resolve_content = app.state.friends.content.resolve
    client = TestClient(app)
    auth = {'x-ryn-local-token': 'offline-material-owner'}
    reference = {'item_id': f.item['item_id'], 'offline_job_id': original['job_id']}
    share = {**reference, 'peer_id': bob.peer_id, 'card_id': 'e' * 32}
    assert client.post('/api/local/ask/contexts', json=reference).status_code == 403
    assert client.post('/api/local/friends/share', json=share).status_code == 403
    prepared = client.post('/api/local/ask/contexts', json=reference, headers=auth)
    assert prepared.status_code == 200, prepared.text
    sent = client.post('/api/local/friends/share', json=share, headers=auth)
    assert sent.status_code == 200, sent.text
    assert sent.json()['delivery_state'] == 'delivered'
    f.network['body'] = BODY + ' A new verified version.'
    f.service.request(f.item['item_id'], update=True)
    assert f.service.run_once()
    stale_ask = client.post('/api/local/ask/contexts', json=reference, headers=auth)
    assert stale_ask.status_code == 409 and stale_ask.json()['detail'] == 'ask_context_changed'
    stale_share = client.post('/api/local/friends/share', json={**share, 'card_id': 'f' * 32}, headers=auth)
    assert stale_share.status_code == 409 and stale_share.json()['detail'] == 'friend_card_content_changed'
    # A lost reply retries the original frozen card even after download replacement.
    retried = client.post('/api/local/friends/share', json=share, headers=auth)
    assert retried.status_code == 200 and retried.json()['card_id'] == share['card_id']
    assert len(bob.content_cards()) == 1
    changed = {**share, 'offline_job_id': f.service.read(f.item['item_id'])['job_id']}
    conflict = client.post('/api/local/friends/share', json=changed, headers=auth)
    assert conflict.status_code == 409 and conflict.json()['detail'] == 'friend_card_id_conflict'
    assert len(bob.content_cards()) == 1
