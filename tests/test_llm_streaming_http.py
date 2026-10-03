"""Real TCP adapter -> provider -> consumer SSE, with real friend authorization.

The model is deterministic and deliberately gated. This proves incremental
delivery, not model quality, GPU performance or cross-NAT reachability.
"""
from __future__ import annotations

import json
import socket
import threading
import time
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import httpx
import pytest
import uvicorn

from rynmesh.llm_package.manifest import LLMPackageManifest, save_manifest
from rynmesh.peer_http import create_app
from rynmesh.store import RynmeshStore


@contextmanager
def serve(app, sock):
    server = uvicorn.Server(uvicorn.Config(app, log_level="error", access_log=False, lifespan="off"))
    thread = threading.Thread(target=server.run, kwargs={"sockets": [sock]}, daemon=True)
    thread.start()
    deadline = time.monotonic() + 5
    while not server.started and time.monotonic() < deadline:
        time.sleep(0.01)
    assert server.started
    try:
        yield
    finally:
        server.should_exit = True
        thread.join(5)
        sock.close()
        assert not thread.is_alive()


@pytest.mark.parametrize("retention", [0, 3600])
def test_real_http_friend_stream_archives_once_and_reconnects_without_new_inference(tmp_path, monkeypatch, caplog, retention):
    release = threading.Event()
    completed = threading.Event()
    requests = []

    class Runtime(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def do_GET(self):  # noqa: N802
            body = b'{"data":[{"id":"test-only"}]}'
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_POST(self):  # noqa: N802
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            probe = body["max_tokens"] == 2
            if not probe:
                requests.append(body)
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.end_headers()
            try:
                self.wfile.write(b'data: {"choices":[{"delta":{"content":"early "}}]}\n\n')
                self.wfile.flush()
                if not probe:
                    assert release.wait(10)
                self.wfile.write(b'data: {"choices":[{"delta":{"content":"final"},"finish_reason":"stop"}]}\n\n')
                self.wfile.write(b'data: {"choices":[],"usage":{"prompt_tokens":8,"completion_tokens":2}}\n\ndata: [DONE]\n\n')
                self.wfile.flush()
                if not probe:
                    completed.set()
            except (BrokenPipeError, ConnectionResetError):
                pass

    runtime = ThreadingHTTPServer(("127.0.0.1", 0), Runtime)
    runtime_thread = threading.Thread(target=runtime.serve_forever, daemon=True)
    runtime_thread.start()
    monkeypatch.setenv("RYNMESH_AUTO_REGISTER", "0")
    monkeypatch.setenv("RYNMESH_DISABLE_DISCOVERY", "1")
    monkeypatch.setenv("RYNMESH_MODEL_PROVIDER", "none")
    monkeypatch.setenv("RYNMESH_LOCAL_TOKEN", "stream-test-owner")
    monkeypatch.setenv("RYNMESH_FRIEND_ALLOW_LOOPBACK", "1")
    monkeypatch.setenv("RYNMESH_LLM_TRANSPORT", "auto")
    monkeypatch.delenv("RYNMESH_LLM_SERVICE_MANIFEST", raising=False)
    apps, sockets, urls = [], [], []
    try:
        for name in ("provider", "consumer"):
            sock = socket.socket()
            sock.bind(("127.0.0.1", 0))
            sockets.append(sock)
            url = f"http://127.0.0.1:{sock.getsockname()[1]}"
            urls.append(url)
            home = tmp_path / name
            store = RynmeshStore(home=home, node_name=name, network_dir=tmp_path / "network")
            if name == "provider":
                manifest = save_manifest(LLMPackageManifest(package_id="stream-model", mode="openai_compatible",
                    public_model_alias="Protocol test model", base_url=f"http://127.0.0.1:{runtime.server_port}",
                    model="test-only", capabilities=["text-generation", "streaming"], timeout_seconds=15), home / "llm" / "manifest.json")
                (home / "llm" / "provider-settings.json").write_text(json.dumps({"manifest": str(manifest), "publication_enabled": True}), encoding="utf-8")
            monkeypatch.setenv("RYNMESH_HOME", str(home))
            monkeypatch.setenv("RYNMESH_FRIEND_ENDPOINT", url)
            apps.append(create_app(store))
        provider, consumer = apps
        headers = {"x-ryn-local-token": "stream-test-owner"}
        with serve(provider, sockets[0]), serve(consumer, sockets[1]), httpx.Client(timeout=15, headers=headers) as client:
            invite = client.post(urls[0] + "/api/local/friends/invites", json={})
            assert invite.status_code == 200, invite.text
            joined = client.post(urls[1] + "/api/local/friends/join", json={"invite_uri": invite.json()["invite_uri"]})
            assert joined.status_code == 200, joined.text
            rid = joined.json()["relationship_id"]
            grant = client.put(urls[0] + f"/api/local/ai-access/stream-model/{rid}", json={"allowed": True, "expected_revision": 0})
            assert grant.status_code == 200, grant.text
            peer = provider.state.friends.service.peer_id
            catalog = client.post(urls[1] + "/api/local/ai-access/friend-services", json={"peer_id": peer})
            assert catalog.status_code == 200, catalog.text
            assert "stream-v1" in catalog.json()["services"][0]["delivery_protocols"]
            client.put(urls[1] + "/api/local/llm/privacy", json={"result_retention_seconds": retention}).raise_for_status()
            history = consumer.state.ask_ryn.conversations
            row = history.save({"id": "stream-chat", "title": "New chat", "serviceKey": peer + "::stream-model",
                "serviceName": "Protocol test model", "providerPeerId": peer, "networkId": "rynmesh-main",
                "createdAt": "2026-09-17T00:00:00Z", "updatedAt": "2026-09-17T00:00:00Z", "messages": []}, expected_revision=0)
            question = "PRIVATE_STREAM_HTTP_MARKER"
            preview = consumer.state.ask_ryn.context.preview(row, question)
            task_id = "task_" + "a" * 32
            request = {"task_id": task_id, "conversation_id": row["id"], "expected_revision": row["revision"],
                "question": question, "prompt_sha256": preview["prompt_sha256"], "ai_permission": preview["ai_permission"]}
            started = client.post(urls[1] + "/api/local/ask/runs", json=request)
            assert started.status_code == 200, started.text
            consumer.state.ask_ryn.runs.run_once()
            deadline = time.monotonic() + 5
            while not requests and time.monotonic() < deadline:
                time.sleep(0.01)
            assert len(requests) == 1 and requests[0]["stream"] is True
            assert requests[0]["messages"][0]["role"] == "system"
            path = urls[1] + f"/api/local/llm/orders/{task_id}/events"
            assert httpx.get(path, timeout=3).status_code in {401, 403}
            with client.stream("GET", path) as events:
                assert events.status_code == 200
                assert events.headers["cache-control"] == "no-store"
                lines = events.iter_lines()
                for line in lines:
                    if line == "event: delta":
                        first = json.loads(next(lines)[6:])
                        assert first == {"sequence": 0, "delta": "early "}
                        assert not completed.is_set()
                        break
                else:
                    pytest.fail("no delta before terminal")
            # Closing the browser subscription must not cancel the order.
            with client.stream("GET", path + "?after_sequence=0") as events:
                release.set()
                data = "\n".join(events.iter_lines())
                assert "event: complete" in data
                assert '"delta":"early "' not in data
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                consumer.state.ask_ryn.runs.run_once()
                if consumer.state.ask_ryn.runs.get(task_id)["state"] == "succeeded":
                    break
                time.sleep(0.01)
            assert history.get(row["id"])["messages"][-1]["content"] == "early final"
            assert client.post(urls[1] + "/api/local/ask/runs", json=request).json()["state"] == "succeeded"
            replay = client.get(path)
            assert "event: complete" in replay.text and "event: delta" not in replay.text
            balance = client.get(urls[1] + "/api/local/task-balance").json()
            assert [e["kind"] for e in balance["events"] if e.get("task_id") == task_id] == ["hold", "settle"]
            assert len(requests) == 1
            consumer.state.ask_ryn.orders.erase_results([task_id])
            assert "early" not in client.get(path).text
            assert "PRIVATE_STREAM_HTTP_MARKER" not in caplog.text
            for path_on_disk in (tmp_path / "consumer").rglob("*.json"):
                assert question not in path_on_disk.read_text(encoding="utf-8")
    finally:
        release.set()
        runtime.shutdown()
        runtime.server_close()
        runtime_thread.join(3)
        for sock in sockets:
            sock.close()
