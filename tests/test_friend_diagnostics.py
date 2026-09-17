import threading
import time
from concurrent.futures import ThreadPoolExecutor
from urllib.error import HTTPError, URLError

import pytest
from fastapi.testclient import TestClient
from test_friends import Mesh, _node

from rynmesh.crypto import canonical_json, sign_payload
from rynmesh.friends.diagnostics import PATH, FriendDiagnostics, failure_code, respond
from rynmesh.friends.service import FriendError
from rynmesh.peer_http import create_app
from rynmesh.store import RynmeshStore


def mesh_nodes(tmp_path, count):
    mesh = Mesh()
    old_post = mesh.post
    wires = []

    def post(endpoint, path, payload, headers=None, **kwargs):
        if path != PATH:
            return old_post(endpoint, path, payload, headers, **kwargs)
        if endpoint not in mesh.online:
            raise OSError('PRIVATE_DIAGNOSTIC_MARKER')
        wires.append(payload)
        return respond(mesh.nodes[endpoint], payload, headers)

    mesh.post = post
    nodes = [_node(tmp_path, f'node{i}', 18300 + i, mesh) for i in range(count)]
    for index, node in enumerate(nodes):
        for other in nodes[index + 1:]:
            other.join(node.create_invite()['invite_uri'])
    return mesh, nodes, wires


@pytest.mark.parametrize('count', [3, 5])
def test_each_directed_link_in_small_mesh_failure_recovery_and_revocation(tmp_path, count, caplog):
    mesh, nodes, wires = mesh_nodes(tmp_path, count)
    diagnostics = [FriendDiagnostics(lambda node=node: node) for node in nodes]
    try:
        for checks in diagnostics:
            ids = [row['relationship_id'] for row in checks.snapshot()['links']]
            assert all(row['state'] == 'not_checked' for row in checks.snapshot()['links'])
            result = checks.run(ids)
            assert len(result['links']) == count - 1
            assert all(row['state'] == 'reachable' and row['latency_ms'] >= 0 for row in result['links'])
            assert result['scope'] == 'outbound_direct'
        mesh.online.remove(nodes[-1].endpoint)
        ids = [row['relationship_id'] for row in diagnostics[0].snapshot()['links']]
        broken = diagnostics[0].run(ids)['links']
        assert [row['peer_id'] for row in broken if row['state'] == 'unreachable'] == [nodes[-1].peer_id]
        assert sum(row['state'] == 'reachable' for row in broken) == count - 2
        mesh.online.add(nodes[-1].endpoint)
        assert all(row['state'] == 'reachable' for row in diagnostics[0].run(ids)['links'])
        rid = ids[0]
        nodes[0].revoke(rid)
        assert all(row['relationship_id'] != rid for row in diagnostics[0].snapshot()['links'])
        with pytest.raises(FriendError, match='active_friend_required'):
            diagnostics[0].run([rid])
        assert all(set(wire) == {'kind', 'from', 'to', 'relationship_id', 'challenge'} for wire in wires)
        assert 'PRIVATE_DIAGNOSTIC_MARKER' not in caplog.text + str(broken)
        assert not list(nodes[0].home.rglob('*diagnostic*.json'))
    finally:
        for checks in diagnostics:
            checks.close()


def test_identity_and_challenge_are_verified_and_bad_address_is_not_contacted(tmp_path):
    _, nodes, _ = mesh_nodes(tmp_path, 3)
    origin, target, impostor = nodes
    checks = FriendDiagnostics(lambda: origin)
    row = origin.store.relationship_for_peer(target.peer_id)
    original_post = origin.post_json

    def wrong_identity(endpoint, path, body, headers=None, **kwargs):
        payload = {**body, 'from': target.peer_id, 'to': origin.peer_id}
        return {'proof': sign_payload(payload, private_key_bytes=impostor.identity_private).to_dict()}

    try:
        origin.post_json = wrong_identity
        assert checks.probe(row)['state'] == 'identity_mismatch'
        captured = []

        def replay(endpoint, path, body, headers=None, **kwargs):
            if not captured:
                captured.append(original_post(endpoint, path, body, headers, **kwargs))
            return captured[0]

        origin.post_json = replay
        assert checks.probe(row)['state'] == 'reachable'
        assert checks.probe(row)['state'] == 'invalid_response'
        assert checks.probe({**row, 'endpoint': 'http://169.254.169.254'})['state'] == 'endpoint_rejected'
        assert checks.probe({**row, 'endpoint': ''})['state'] == 'endpoint_missing'
    finally:
        checks.close()


def test_concurrent_batch_deadline_does_not_spawn_unbounded_retries(tmp_path):
    _, nodes, _ = mesh_nodes(tmp_path, 5)
    origin = nodes[0]
    started = threading.Barrier(5)
    release = threading.Event()
    original_post = origin.post_json
    calls = []

    def gated(endpoint, path, body, headers=None, **kwargs):
        calls.append(endpoint)
        started.wait(timeout=3)
        assert release.wait(3)
        return original_post(endpoint, path, body, headers, **kwargs)

    origin.post_json = gated
    checks = FriendDiagnostics(lambda: origin, deadline=0.2)
    ids = [row['relationship_id'] for row in checks.snapshot()['links']]
    try:
        with ThreadPoolExecutor(max_workers=1) as executor:
            future = executor.submit(checks.run, ids)
            started.wait(timeout=3)  # All four probes really overlapped.
            result = future.result(timeout=2)
        assert len(calls) == 4 and result['running']
        assert all(row['state'] == 'timed_out' for row in result['links'])
        with pytest.raises(FriendError, match='friend_probe_busy'):
            checks.run(ids)
        assert len(calls) == 4
        release.set()
        deadline = time.monotonic() + 3
        while checks.snapshot()['running'] and time.monotonic() < deadline:
            time.sleep(0.01)
        assert not checks.snapshot()['running']
        origin.post_json = original_post
        assert all(row['state'] == 'reachable' for row in checks.run(ids)['links'])
    finally:
        release.set()
        checks.close()


def test_typed_errors_do_not_disclose_exception_details():
    assert failure_code(HTTPError('private', 404, 'PRIVATE', {}, None)) == 'unsupported'
    assert failure_code(HTTPError('private', 403, 'PRIVATE', {}, None)) == 'authentication_failed'
    assert failure_code(URLError(TimeoutError('PRIVATE'))) == 'timed_out'
    assert failure_code(OSError('PRIVATE')) == 'unreachable'


def test_owner_routes_request_bounds_and_peer_authentication(tmp_path, monkeypatch):
    monkeypatch.setenv('RYNMESH_AUTO_REGISTER', '0')
    monkeypatch.setenv('RYNMESH_DISABLE_DISCOVERY', '1')
    monkeypatch.setenv('RYNMESH_LOCAL_TOKEN', 'test-owner')
    store = RynmeshStore(home=tmp_path / 'http', network_dir=tmp_path / 'network')
    monkeypatch.setenv('RYNMESH_HOME', str(store.home))
    app = create_app(store)
    client = TestClient(app)
    local = '/api/local/friends/diagnostics'
    headers = {'x-ryn-local-token': 'test-owner'}
    try:
        assert client.get(local).status_code == 403
        response = client.get(local, headers=headers)
        assert response.status_code == 200 and response.json()['links'] == []
        assert response.headers['cache-control'] == 'no-store'
        assert client.post(local, headers=headers, json={'relationship_ids': ['a'] * 6}).status_code == 409
        assert client.post(local, headers=headers, content='x' * 1025).status_code == 413
        assert client.post(PATH, json={'kind': 'ryn.friend-probe.v1', 'from': 'x', 'to': store.peer_id,
            'relationship_id': 'a' * 32, 'challenge': 'b' * 32}).status_code == 403
        assert client.post(PATH, content='x' * 2049).status_code == 413
        assert canonical_json(response.json()).find(b'PRIVATE') == -1
    finally:
        app.state.friend_diagnostics.close()
