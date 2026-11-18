"""S13-I41: untrusted bytes must stay inert through real HTTP routes."""
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from rynmesh.background_workers import BackgroundWorkerRegistry
from rynmesh.offline_reading.routes import install_offline_reading
from rynmesh.peer_http import create_app
from rynmesh.services.messaging_store import MessagingStore
from rynmesh.store import RynmeshStore
from test_offline_reading import fixture as offline_fixture, row, png

CSP = "sandbox; default-src 'none'"
PAYLOAD = b"<script>fetch('/api/local/node/status')</script>"


@pytest.fixture
def node(tmp_path, monkeypatch):
    monkeypatch.setenv("RYNMESH_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("RYNMESH_AUTO_REGISTER", "0")
    monkeypatch.setenv("RYNMESH_DISABLE_DISCOVERY", "1")
    monkeypatch.setenv("RYNMESH_LOCAL_TOKEN", "attachment-test-owner")
    store = RynmeshStore(home=tmp_path / "home", network_dir=tmp_path / "network")
    app = create_app(store)
    with TestClient(app, headers={"X-Ryn-Local-Token": "attachment-test-owner"}) as client:
        yield app, client, store


def attachment(node, mime):
    _, client, store = node
    messages = MessagingStore(store.home)
    messages.append("peer", {"msg_id": "message", "dir": "in", "from": "peer", "to": store.peer_id,
                             "attachment": {"filename": "remote-file", "mime": mime}})
    messages.save_attachment("message", PAYLOAD)
    return client.get("/api/local/messages/attachment/message", params={"peer_id": "peer"})


def assert_inert(response):
    assert response.status_code == 200, response.text
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["content-security-policy"] == CSP


@pytest.mark.parametrize("mime", ["text/html", "image/svg+xml", "application/xhtml+xml",
                                  "application/javascript", "text/xml", "application/xml",
                                  "audio/example+xml", "video/javascript", "application/unknown",
                                  "image/png; note=x\r\nX-Test: injected"])
def test_T1_active_or_unknown_attachment_forces_download(node, mime):
    response = attachment(node, mime)
    assert response.headers["content-type"] == "application/octet-stream"
    assert response.headers["content-disposition"] == "attachment"
    assert response.content == PAYLOAD
    assert_inert(response)


@pytest.mark.parametrize("mime", ["image/png", "image/jpeg", "image/gif", "image/webp",
                                  "application/pdf", "text/plain", "audio/mpeg", "video/mp4",
                                  "text/plain; charset=gbk"])
def test_T2_passive_attachments_stay_inline_and_inert(node, mime):
    response = attachment(node, mime)
    assert response.headers["content-type"].split(";")[0] == mime.split(";")[0]
    if "charset=" in mime:
        assert response.headers["content-type"] == mime
    assert response.headers.get("content-disposition", "inline") == "inline"
    assert response.content == PAYLOAD
    assert_inert(response)


@pytest.mark.parametrize("suffix", ["body", "preview-bytes", "bytes"])
def test_T2_content_body_preview_and_full_bytes_are_inert(node, tmp_path, suffix):
    _, client, store = node
    source = tmp_path / "document.html"
    source.write_bytes(PAYLOAD)
    published = store.publish_content(source, title="Peer document", transcript="Document",
                                      content_type="text/html", content_kind="document")
    response = client.get(f"/api/local/content/{published['content_id']}/{suffix}")
    assert_inert(response)
    if suffix == "body":
        assert response.headers["content-type"].startswith("application/json")
        assert response.json()["text"] == PAYLOAD.decode()
    else:
        assert response.headers["content-type"] == "application/octet-stream"
        assert response.headers["content-disposition"] == "attachment"


def test_T2_friend_attachment_adds_sandbox_to_existing_download(node):
    app, client, _ = node
    messages = app.state.friends.service.messages
    messages.append("peer", {"msg_id": "friend-message", "dir": "in", "from": "peer",
                             "attachment": {"filename": "remote.html", "mime": "text/html"}})
    messages.save_attachment("friend-message", PAYLOAD)
    response = client.get("/api/local/friends/peer/attachments/friend-message")
    assert response.content == PAYLOAD
    assert response.headers["content-disposition"] == "attachment"
    assert_inert(response)


def test_T2_offline_remote_image_gets_sandbox_without_breaking_image(tmp_path):
    f = offline_fixture(tmp_path)
    app = FastAPI()
    f.service = install_offline_reading(app, home=tmp_path, messaging_key=f.key,
        consumption=lambda: f.consumption, imports=lambda: f.imports, native=lambda: None,
        local_control=lambda request: None, workers=BackgroundWorkerRegistry(), fetch=f.sources.fetch)
    f.service.request(f.item["item_id"])
    assert f.service.run_once()
    record = row(f)
    with TestClient(app) as client:
        response = client.get(f"/api/local/offline-reading/copies/{record['key']}/{record['current']['job_id']}/images/0")
    assert response.content == png()
    assert response.headers["content-type"] == "image/png"
    assert_inert(response)
