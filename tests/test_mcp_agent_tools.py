"""MCP agent-gateway tools: digest/read-later/watchers direct, messaging via daemon."""
from __future__ import annotations

import pytest

from rynmesh import mcp_server
from rynmesh.services import digest as digest_module
from rynmesh.store import RynmeshStore

RSS = (b'<rss version="2.0"><channel><title>Feed</title>'
       b'<item><title>Item</title><link>https://e.com/1</link></item>'
       b'</channel></rss>')
PAGE = b"<html><head><title>Page</title></head><body><p>text</p></body></html>"


@pytest.fixture
def store(tmp_path, monkeypatch):
    monkeypatch.setenv("RYNMESH_HOME", str(tmp_path / "node"))
    monkeypatch.setenv("RYNMESH_AUTO_REGISTER", "0")
    # digest tools construct DigestService with the module default fetcher
    monkeypatch.setattr(digest_module, "default_fetcher",
                        lambda url, timeout_s: RSS if "feed" in url else PAGE)
    return RynmeshStore()


def test_digest_tools_roundtrip(store):
    added = mcp_server._dispatch_tool(store, "rynmesh_digest_add_source",
                                      {"url": "https://e.com/feed"})
    assert added["title"] == "Feed"

    digest = mcp_server._dispatch_tool(store, "rynmesh_digest_get", {"limit": 5})
    assert digest["items"] and digest["items"][0]["title"] == "Item"

    result = mcp_server._dispatch_tool(store, "rynmesh_digest_feedback",
                                       {"item_id": digest["items"][0]["item_id"],
                                        "action": "up"})
    assert result["ok"] is True

    refreshed = mcp_server._dispatch_tool(store, "rynmesh_digest_refresh", {})
    assert "digest" in refreshed and "refresh" in refreshed


def test_readlater_and_watcher_tools(store):
    saved = mcp_server._dispatch_tool(store, "rynmesh_readlater_save",
                                      {"url": "https://e.com/article"})
    assert saved["title"] == "Page"
    watcher = mcp_server._dispatch_tool(store, "rynmesh_watcher_add",
                                        {"url": "https://e.com/thing", "note": "n"})
    assert watcher["note"] == "n"


def test_messaging_tools_proxy_daemon(store, monkeypatch):
    calls = {}

    def fake_daemon(path, payload=None, *, method="GET"):
        calls["path"], calls["payload"] = path, payload
        if "send" in path:
            return {"msg_id": "m1"}
        return [{"text": "hi", "dir": "in"}, {"text": "yo", "dir": "out"}]

    monkeypatch.setattr(mcp_server, "_daemon_json", fake_daemon)
    sent = mcp_server._dispatch_tool(store, "rynmesh_send_message",
                                     {"peer_id": "p/1+x=", "text": "hello"})
    assert sent == {"msg_id": "m1"}
    assert calls["payload"]["peer_id"] == "p/1+x="

    history = mcp_server._dispatch_tool(store, "rynmesh_read_messages",
                                        {"peer_id": "p/1+x=", "limit": 1})
    assert history["messages"] == [{"text": "yo", "dir": "out"}]
    assert "peer_id=p%2F1%2Bx%3D" in calls["path"]  # base64 ids must be quoted


def test_daemon_unreachable_gives_actionable_error(store, monkeypatch):
    monkeypatch.setenv("RYNMESH_PEER_PORT", "1")   # nothing listens there
    with pytest.raises(RuntimeError, match="rynmesh-peer"):
        mcp_server._dispatch_tool(store, "rynmesh_send_message",
                                  {"peer_id": "x", "text": "y"})


def test_tool_schemas_registered():
    names = {tool["name"] for tool in mcp_server.TOOLS}
    for expected in ["rynmesh_digest_get", "rynmesh_digest_refresh",
                     "rynmesh_digest_add_source", "rynmesh_digest_feedback",
                     "rynmesh_readlater_save", "rynmesh_watcher_add",
                     "rynmesh_send_message", "rynmesh_read_messages"]:
        assert expected in names
