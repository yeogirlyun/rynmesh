import json
import threading
from contextlib import ExitStack, contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
from test_device_erasure import job_id, setup

from rynmesh.background_workers import BackgroundWorkerRegistry
from rynmesh.device_erasure.routes import install_device_erasure
from rynmesh.device_erasure.service import PATH
from rynmesh.peer_http import HttpPeerClient


@contextmanager
def serve(service):
    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            try:
                assert self.path == PATH
                wire = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
                value = service.receive(wire)
                raw = json.dumps(value).encode()
                self.send_response(200)
            except Exception:
                raw = b'{}'
                self.send_response(403)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        def log_message(self, *args):
            pass
    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    try:
        yield f'http://127.0.0.1:{server.server_port}'
    finally:
        server.shutdown()
        server.server_close()
        worker.join(5)


def test_three_nodes_real_http_signed_sealed_approval_and_offline_receipts(tmp_path):
    _, (a, b, c), pairs, cleanups, _ = setup(tmp_path)
    with ExitStack() as stack:
        addresses = {service.pairing().identity['endpoint']: stack.enter_context(serve(service)) for service in (a, b, c)}
        # Use a closed loopback port for the offline target, then reconnect to
        # its live endpoint. Cryptographic identities remain the paired ones.
        real_c = addresses[c.pairing().identity['endpoint']]
        addresses[c.pairing().identity['endpoint']] = 'http://127.0.0.1:1'
        a.pairing().post_json = lambda endpoint, path, wire: HttpPeerClient(addresses[endpoint], timeout_s=1).post_json(path, wire, max_bytes=65536)
        job = job_id()
        a.begin(job, 'reading', pairs)
        a.exchange(job, pairs[0])
        assert not a.exchange(job, pairs[1])['remote_confirmed']
        incoming = b.status()['incoming'][0]['id']
        b.approve(incoming, b.preview(incoming)['review_token'])
        assert not a.exchange(job, pairs[0])['remote_confirmed']
        addresses[c.pairing().identity['endpoint']] = real_c
        a.exchange(job, pairs[1])
        incoming = c.status()['incoming'][0]['id']
        c.approve(incoming, c.preview(incoming)['review_token'])
        assert a.exchange(job, pairs[1])['remote_confirmed']
        assert cleanups['B'].source.list() == cleanups['C'].source.list() == []
        assert cleanups['A'].source.list()


def test_owner_only_control_and_unsigned_peer_requests(tmp_path):
    _, (a, b, _), pairs, _, _ = setup(tmp_path)
    app = FastAPI()
    app.state.device_sync = type('State', (), {'service': a.pairing()})()
    def guard(request):
        if request.headers.get('x-owner') != 'yes':
            raise HTTPException(403)
    install_device_erasure(app, home=tmp_path / 'A', messaging_key=a.pairing().messaging_key,
                          local_control=guard, workers=BackgroundWorkerRegistry())
    app.state.device_erasure = a
    with TestClient(app) as client:
        assert client.get('/api/local/device-erasure').status_code == 403
        assert client.post('/api/local/device-erasure/action', json={'action': 'approve'}).status_code == 403
        assert client.post(PATH, json={}).status_code == 403
        headers = {'x-owner': 'yes'}
        response = client.get('/api/local/device-erasure', headers=headers)
        assert response.headers['cache-control'] == 'no-store'
        assert client.post('/api/local/device-erasure/action', headers=headers, content=b'x' * 8193).status_code == 413
        value = {'action': 'begin', 'id': job_id(), 'category': 'reading', 'pair_ids': [pairs[0]]}
        assert client.post('/api/local/device-erasure/action', headers=headers, json=value).status_code == 200
        assert b.status()['incoming'] == []  # Owner request creation alone does not erase or contact.
