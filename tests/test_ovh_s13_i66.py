"""S13-I66 content scan isolation, discovery identity and verified full fetch."""
import copy
import json
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import urlparse

import pytest
from fastapi.testclient import TestClient

from rynmesh import peer_http
from rynmesh.manifest import ManifestValidation
from rynmesh.peer_http import HttpPeerClient, PeerTransportError, create_app
from rynmesh.store import RynmeshStore, StoreError
from rynmesh.transport import TransportError, network_key_header


@pytest.fixture
def scan(tmp_path, monkeypatch):
    for key in ['RYNMESH_REGISTRY_URL', 'RYNMESH_NETWORK_KEY']:
        monkeypatch.setenv(key, '')
    monkeypatch.setenv('RYNMESH_AUTO_REGISTER', '0')
    monkeypatch.setenv('RYNMESH_DEFAULT_DISCOVERY', '0')
    monkeypatch.setattr('rynmesh.store._primary_lan_ip', lambda: '192.0.2.10')
    monkeypatch.setattr('rynmesh.store._local_ip_addresses', lambda: ('192.0.2.10',))
    store = RynmeshStore(home=tmp_path/'viewer', network_dir=tmp_path/'mesh')
    good = RynmeshStore(home=tmp_path/'good', network_dir=tmp_path/'mesh')
    bad = RynmeshStore(home=tmp_path/'bad', network_dir=tmp_path/'mesh')
    peers = [{'peer_id': node.peer_id, 'node_name': name, 'endpoints': ['http://'+name+'.test']}
             for node, name in [(good, 'good'), (bad, 'bad')]]
    responses = {}
    for peer in peers:
        host = urlparse(peer['endpoints'][0]).netloc
        responses[(host, '/api/v1/node')] = {'peer_id': peer['peer_id'], 'node_name': host}
        responses[(host, '/api/v1/content')] = {'content': [{'content_id': host, 'title': host}]}

    class Transport:
        def get_bytes(self, url, **kwargs):
            parsed = urlparse(url)
            value = responses[(parsed.netloc, parsed.path)]
            if isinstance(value, Exception):
                raise value
            return value if isinstance(value, bytes) else json.dumps(value).encode()

    monkeypatch.setattr(peer_http, 'get_transport', lambda: Transport())
    monkeypatch.setattr(store, 'discover_peers', lambda **kwargs: {'peers': copy.deepcopy(peers)})
    app = create_app(store)
    client = TestClient(app)
    yield SimpleNamespace(store=store, good=good, bad=bad, peers=peers, responses=responses, app=app, client=client)
    app.state.directory_refresher.close()


def test_T1_s13_i66_bad_utf8_isolated_with_real_http_client(scan):
    scan.responses[('bad.test', '/api/v1/content')] = '坏编码'.encode('gbk')
    response = scan.client.get('/api/local/content?include_scan_errors=1')
    assert response.status_code == 200
    result = response.json()
    assert [item['title'] for item in result['content']] == ['good.test']
    assert result['scan_errors'] == [{'peer_id': scan.bad.peer_id, 'code': 'peer_invalid_json'}]
    # Old array clients and both downstream consumers remain callable.
    assert isinstance(scan.client.get('/api/local/content').json(), list)
    assert scan.client.post('/api/local/recommendations', json={}).status_code == 200
    answer = scan.client.post('/api/local/search-ask', json={'text': 'status'})
    assert answer.status_code == 200 and answer.json()['scan_errors'] == result['scan_errors']


def test_T1_s13_i66_unexpected_and_malformed_listing_isolated(scan):
    scan.responses[('bad.test', '/api/v1/content')] = RuntimeError('PRIVATE_ENDPOINT_AND_CREDENTIAL')
    result = scan.client.get('/api/local/content?include_scan_errors=1').json()
    assert len(result['content']) == 1
    assert result['scan_errors'] == [{'peer_id': scan.bad.peer_id, 'code': 'peer_scan_failed'}]
    assert 'PRIVATE_ENDPOINT' not in json.dumps(result)
    scan.responses[('bad.test', '/api/v1/content')] = {'content': [{'content_id': 'broken', 'size_bytes': 'nan'}]}
    result = scan.client.get('/api/local/content?include_scan_errors=1').json()
    assert [item['content_id'] for item in result['content']] == ['good.test']
    assert result['scan_errors'][0]['peer_id'] == scan.bad.peer_id


def test_T2_s13_i66_identity_mismatch_drops_entire_listing(scan):
    scan.responses[('bad.test', '/api/v1/node')]['peer_id'] = scan.good.peer_id
    result = scan.client.get('/api/local/content?include_scan_errors=1').json()
    assert [item['content_id'] for item in result['content']] == ['good.test']
    assert result['scan_errors'] == [{'peer_id': scan.bad.peer_id, 'code': 'peer_identity_mismatch'}]


def test_T2_s13_i66_discovery_provider_and_display_names_are_separate(scan):
    scan.responses[('bad.test', '/api/v1/node')].update(node_name='Friendly name', distribution_weight=1e9)
    scan.responses[('bad.test', '/api/v1/content')]['content'][0]['provider_peer_id'] = scan.good.peer_id
    result = scan.client.get('/api/local/content?include_scan_errors=1').json()
    bad = next(item for item in result['content'] if item['content_id'] == 'bad.test')
    assert bad['provider_peer_id'] == scan.bad.peer_id and bad['source_peer_name'] == 'Friendly name'
    assert bad['distribution_weight'] != 1e9 and result['scan_errors'] == []
    listing = scan.store.list_peer_content('http://bad.test', expected_peer_id=scan.bad.peer_id)
    assert listing['content'][0]['provider_peer_id'] == scan.bad.peer_id


@pytest.mark.parametrize('method', ['node_info', 'download_content'])
@pytest.mark.parametrize('status,reason,expected', [(403, 'http_error', 'http_403'), (None, 'connection_failed', 'connection_failed'),
 (None, 'too_large', None), (None, 'unsupported_encoding', 'peer_unsupported_encoding')])
def test_T3_s13_i66_get_errors_never_export_exception_text(tmp_path, method, status, reason, expected):
    class Transport:
        def get_bytes(self, *args, **kwargs):
            raise TransportError('PRIVATE url=https://secret.test?key=secret', reason=reason, status=status)
        download = get_bytes
    client = HttpPeerClient('http://peer.test', transport=Transport())
    with pytest.raises(PeerTransportError) as error:
        client.node_info() if method == 'node_info' else client.download_content('item', tmp_path/'media')
    expected = ('peer_media_too_large' if method == 'download_content' else 'peer_response_too_large') if reason == 'too_large' else (expected if reason == 'unsupported_encoding' else 'peer_http_error:'+expected)
    assert str(error.value) == expected
    assert error.value.__cause__ is None


def test_T1_s13_i66_invalid_utf8_has_stable_transport_error():
    client = HttpPeerClient('http://peer.test', transport=SimpleNamespace(get_bytes=lambda *a, **kw: b'\xff'))
    with pytest.raises(PeerTransportError, match='^peer_invalid_json$'):
        client.list_content()


def test_T4_s13_i66_stream_stamps_network_key_on_real_client(monkeypatch):
    monkeypatch.setenv('RYNMESH_NETWORK_KEY', 'fixture-network-key')
    calls = []
    def stream(url, body, **kwargs):
        calls.append(kwargs)
        yield b'{"ok":true}\n'
    client = HttpPeerClient('http://peer.test', transport=SimpleNamespace(iter_post_bytes=stream))
    assert list(client.iter_post_ndjson('/inference', {'message': 'test'})) == [{'ok': True}]
    assert calls[0]['headers'].items() >= network_key_header().items()
    assert calls[0]['headers']['Accept'] == 'application/x-ndjson'


@pytest.fixture
def fetched(tmp_path, monkeypatch):
    monkeypatch.setenv('RYNMESH_REGISTRY_URL', '')
    monkeypatch.setenv('RYNMESH_AUTO_REGISTER', '0')
    monkeypatch.setattr('rynmesh.store._primary_lan_ip', lambda: '192.0.2.10')
    monkeypatch.setattr('rynmesh.store._local_ip_addresses', lambda: ('192.0.2.10',))
    publisher = RynmeshStore(home=tmp_path/'publisher', network_dir=tmp_path/'mesh')
    consumer = RynmeshStore(home=tmp_path/'consumer', network_dir=tmp_path/'mesh')
    media = tmp_path/'source.txt'
    media.write_bytes(b'verified fixture content')
    published = publisher.publish_content(media, title='Fixture', transcript='safe fixture content')
    content_id = published['content_id']
    manifest = publisher.get_local_manifest(content_id)
    data = {'media': media.read_bytes(), 'manifest': manifest, 'calls': []}
    class Client:
        def __init__(self, *args, **kwargs): pass
        def node_info(self): return {'peer_id': publisher.peer_id}
        def get_content_manifest(self, item): return data['manifest']
        get_manifest = get_content_manifest
        def download_content(self, item, destination):
            path = Path(destination)
            data['calls'].append(path)
            assert not (path.parent/'manifest.json').exists()
            path.write_bytes(data['media'])
            if data.get('download_error'):
                raise OSError('download interrupted')
            return path
        download_media = download_content
    monkeypatch.setattr(peer_http, 'HttpPeerClient', Client)
    yield SimpleNamespace(store=consumer, publisher=publisher, id=content_id, data=data, directory=consumer._clip_dir(content_id))


@pytest.mark.parametrize('api', ['fetch_peer_full', 'fetch_peer_content_full'])
def test_T5_s13_i66_disallowed_manifest_writes_nothing(fetched, monkeypatch, api):
    monkeypatch.setattr(fetched.store, '_validate_peer_manifest', lambda _: ManifestValidation())
    with pytest.raises(StoreError, match='manifest_not_allowed'):
        getattr(fetched.store, api)('http://peer.test', fetched.id, expected_peer_id=fetched.publisher.peer_id)
    assert not fetched.data['calls']
    assert not fetched.directory.exists() or not list(fetched.directory.iterdir())


@pytest.mark.parametrize('api', ['fetch_peer_full', 'fetch_peer_content_full'])
def test_T6_s13_i66_hash_mismatch_has_no_manifest_or_partial(fetched, api):
    fetched.data['media'] = b'tampered bytes'
    with pytest.raises(StoreError, match='media_hash_mismatch'):
        getattr(fetched.store, api)('http://peer.test', fetched.id, expected_peer_id=fetched.publisher.peer_id)
    assert not (fetched.directory/'manifest.json').exists()
    assert not list(fetched.directory.glob('*.part'))
    assert not list(fetched.directory.glob('*.media'))


def test_T6_s13_i66_interrupted_download_cleans_partial(fetched):
    fetched.data['download_error'] = True
    with pytest.raises(OSError, match='download interrupted'):
        fetched.store.fetch_peer_content_full('http://peer.test', fetched.id)
    assert not list(fetched.directory.glob('*.part'))
    assert not (fetched.directory/'manifest.json').exists()


def test_T6_s13_i66_success_publishes_hash_checked_media_then_atomic_manifest(fetched, monkeypatch):
    from rynmesh import atomic_io
    original = atomic_io.atomic_write_json
    writes = []
    def atomic(path, value, **kwargs):
        if Path(path).name == 'manifest.json':
            assert not list(fetched.directory.glob('*.part'))
            assert (fetched.directory/(fetched.directory.name+'.media')).read_bytes() == fetched.data['media']
            writes.append(Path(path))
        return original(path, value, **kwargs)
    monkeypatch.setattr(atomic_io, 'atomic_write_json', atomic)
    result = fetched.store.fetch_peer_content_full('http://peer.test', fetched.id, expected_peer_id=fetched.publisher.peer_id)
    assert fetched.data['calls'][0].name.endswith('.media.part')
    assert Path(result['media_path']).read_bytes() == fetched.data['media']
    assert json.loads((fetched.directory/'manifest.json').read_text()) == fetched.data['manifest'].to_dict()
    assert len(writes) == 1
    assert not list(fetched.directory.glob('*.part'))


def test_T5_s13_i66_real_invalid_signature_is_rejected_without_files(fetched):
    from rynmesh.crypto import SignedPayload
    data = fetched.data['manifest'].to_dict()
    data['signature'] = ('A' if data['signature'][0] != 'A' else 'B') + data['signature'][1:]
    fetched.data['manifest'] = SignedPayload.from_dict(data)
    with pytest.raises(StoreError, match='manifest_signature_invalid'):
        fetched.store.fetch_peer_content_full('http://peer.test', fetched.id)
    assert not fetched.data['calls']
    assert not fetched.directory.exists()

