"""Real HTTP sockets: probe all directed links, stop one node, then recover."""
import socket
import threading
import time

import httpx
import pytest
import uvicorn

from rynmesh.peer_http import create_app
from rynmesh.store import RynmeshStore


class LiveNode:
    def __init__(self, app, sock):
        self.app = app
        self.port = sock.getsockname()[1]
        self.url = f'http://127.0.0.1:{self.port}'
        self.start(sock)

    def start(self, sock=None):
        if sock is None:
            sock = socket.socket()
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            sock.bind(('127.0.0.1', self.port))
        self.sock = sock
        self.server = uvicorn.Server(uvicorn.Config(self.app, log_level='error', access_log=False, lifespan='off'))
        self.thread = threading.Thread(target=self.server.run, kwargs={'sockets': [sock]}, daemon=True)
        self.thread.start()
        deadline = time.monotonic() + 5
        while not self.server.started and time.monotonic() < deadline:
            time.sleep(0.01)
        assert self.server.started

    def stop(self):
        self.server.should_exit = True
        self.thread.join(5)
        self.sock.close()
        assert not self.thread.is_alive()


@pytest.mark.parametrize('count', [3, 5])
def test_real_http_mesh_reports_one_failed_node_and_recovers_without_new_pairing(tmp_path, monkeypatch, count, caplog):
    monkeypatch.setenv('RYNMESH_AUTO_REGISTER', '0')
    monkeypatch.setenv('RYNMESH_DISABLE_DISCOVERY', '1')
    monkeypatch.setenv('RYNMESH_DEFAULT_DISCOVERY', '0')
    monkeypatch.setenv('RYNMESH_MODEL_PROVIDER', 'none')
    monkeypatch.setenv('RYNMESH_FRIEND_ALLOW_LOOPBACK', '1')
    monkeypatch.setenv('RYNMESH_LOCAL_TOKEN', 'diagnostic-test-owner')
    nodes = []
    try:
        for index in range(count):
            sock = socket.socket()
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            sock.bind(('127.0.0.1', 0))
            home = tmp_path / f'node-{index}'
            monkeypatch.setenv('RYNMESH_HOME', str(home))
            monkeypatch.setenv('RYNMESH_FRIEND_ENDPOINT', f'http://127.0.0.1:{sock.getsockname()[1]}')
            app = create_app(RynmeshStore(home=home, network_dir=tmp_path / 'network', node_name=f'node-{index}'))
            nodes.append(LiveNode(app, sock))
        with httpx.Client(timeout=15, headers={'x-ryn-local-token': 'diagnostic-test-owner'}) as client:
            for index, node in enumerate(nodes):
                for other in nodes[index + 1:]:
                    invitation = client.post(node.url + '/api/local/friends/invites', json={})
                    invitation.raise_for_status()
                    joined = client.post(other.url + '/api/local/friends/join', json={'invite_uri': invitation.json()['invite_uri']})
                    joined.raise_for_status()

            def check(node):
                snapshot = client.get(node.url + '/api/local/friends/diagnostics')
                snapshot.raise_for_status()
                ids = [row['relationship_id'] for row in snapshot.json()['links']]
                response = client.post(node.url + '/api/local/friends/diagnostics', json={'relationship_ids': ids})
                response.raise_for_status()
                assert response.headers['cache-control'] == 'no-store'
                return response.json()

            for node in nodes:
                result = check(node)
                assert len(result['links']) == count - 1
                assert all(row['state'] == 'reachable' for row in result['links'])
            failed_peer = nodes[-1].app.state.friends.service.peer_id
            nodes[-1].stop()
            result = check(nodes[0])
            assert [row['peer_id'] for row in result['links'] if row['state'] != 'reachable'] == [failed_peer]
            nodes[-1].start()
            assert all(row['state'] == 'reachable' for row in check(nodes[0])['links'])
            for node in nodes:
                for friend in node.app.state.friends.service.list_friends():
                    assert node.app.state.friends.service.history(friend['peer_id']) == []
            assert 'diagnostic-test-owner' not in caplog.text
    finally:
        for node in nodes:
            node.stop()
            node.app.state.friend_diagnostics.close()
