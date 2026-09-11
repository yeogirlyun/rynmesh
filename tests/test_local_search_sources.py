from types import SimpleNamespace

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
from test_ask_history import sample
from test_friends import _pair

from rynmesh.ask_ryn.store import ConversationStore
from rynmesh.atomic_io import atomic_write_json, read_json
from rynmesh.background_workers import BackgroundWorkerRegistry
from rynmesh.local_search.index import LocalSearchIndex, SearchError
from rynmesh.local_search.routes import install_local_search
from rynmesh.local_search.sources import LocalSearchSources
from rynmesh.services import peer_box
from rynmesh.services.consumption import ConsumptionStore
from rynmesh.services.library_imports import LibraryImportStore
from rynmesh.services.reader import ReaderCache


def sources(tmp_path):
    mesh, alice, bob = _pair(tmp_path)
    history = ConsumptionStore(alice.home / "consumption.json")
    imports = LibraryImportStore(alice.home / "library-imports")
    reader = ReaderCache(alice.home / "reader", ttl_s=1)
    conversations = ConversationStore(alice.home / "ask", alice.messaging_private)
    adapter = LocalSearchSources(consumption=lambda: history, imports=lambda: imports,
        reader=lambda: reader, friends=lambda: alice, conversations=lambda: conversations)
    return adapter, history, imports, reader, conversations, alice, bob, mesh


def test_real_local_stores_search_all_types_without_fetching_and_revalidate_open(tmp_path):
    adapter, history, imports, reader, conversations, alice, bob, mesh = sources(tmp_path)
    article = {"item_id": "article-one", "title": "Saved-title", "source_title": "Notebook", "link": "https://example.test/story"}
    history.record(article, "bookmark")
    history.record(article, "opened")
    reader.put(article["link"], {"blocks": [{"text": "春天山谷 Python"}]}, now=1)
    imported = imports.save(b"Private local paper", filename="paper.txt", mime="text/plain")
    bob.send_content_card(alice.peer_id, {"title": "Friend-card", "source": "Bob journal"}, card_id="a" * 32)
    bob.send_message(alice.peer_id, text="Friend-message", message_id="b" * 32)
    conversation = sample()
    conversations.save(conversation, expected_revision=0)
    mesh.online.clear()  # Searching must not need a peer, registry or model.
    def forbidden(*args, **kwargs):
        raise AssertionError("Search attempted a remote request")
    alice.post_json = forbidden
    engine = LocalSearchIndex(alice.home / "local-search", messaging_key=alice.messaging_private, source=adapter.snapshot)
    engine.rebuild()
    for query, kind in (("山谷 Python", "saved"), ("山谷", "history"), ("Friend-card", "share"),
                        ("Friend-message", "chat"), ("秘密文章正文", "chat"), ("Private local paper", "saved")):
        result = engine.query(query, kind=kind)
        assert result["total"] == 1 and not result["partial"]
        assert result["results"][0]["targets"][0]["href"].startswith(("/search?", "/friends?", "/ask?"))
    assert len(engine.query("山谷")["results"]) == 1
    before = engine.query("Friend-card")["results"][0]
    assert engine.resolve(before["id"])["body_state"] == "not_downloaded"
    assert engine.query("nonexistent-downloaded-body")["total"] == 0
    alice.revoke(alice.store.relationship_for_peer(bob.peer_id)["relationship_id"], notify=False)
    assert engine.query("Friend-card")["total"] == engine.query("Friend-message")["total"] == 0
    with pytest.raises(SearchError, match="result_unavailable"):
        engine.resolve(before["id"])
    assert engine.query("Private local paper")["total"] == 1
    imports.remove(imported["import_id"])
    assert engine.query("Private local paper")["total"] == 0
    conversations.remove(conversation["id"], expected_revision=1)
    assert engine.query("秘密文章正文")["total"] == 0


def test_same_verified_article_merges_sources_but_a_different_revision_does_not(tmp_path):
    adapter, history, imports, reader, _, alice, bob, _ = sources(tmp_path)
    url = "https://example.test/article"
    article = {"item_id": "same", "title": "Shared exact body", "source_title": "Journal", "link": url}
    history.record(article, "opened")
    reader.put(url, {"blocks": [{"text": "Same verified article"}]}, now=1)
    saved = imports.save(b"Same verified article", filename="article.txt", mime="text/plain", source={"source_url": url})
    bob.send_content_card(alice.peer_id, {"title": "A card", "source_url": url}, card_id="c" * 32)
    alice.store.patch_card("c" * 32, {"fetched_library_id": "import:" + saved["import_id"], "fetch_state": "fetched"})
    engine = LocalSearchIndex(alice.home / "local-search", messaging_key=alice.messaging_private, source=adapter.snapshot)
    engine.rebuild()
    result = engine.query("Same verified article")
    assert result["total"] == 1
    assert set(result["results"][0]["kinds"]) == {"saved", "history", "share"}
    assert len(result["results"][0]["targets"]) == 2
    metadata_path = imports.root / saved["import_id"] / "metadata.json"
    metadata = read_json(metadata_path)
    atomic_write_json(metadata_path, {**metadata, "extraction_status": "truncated"})
    engine.rebuild()
    assert engine.query("Same verified article")["results"][0]["text_truncated"]
    imports.save(b"New revision", filename="article.txt", mime="text/plain", source={"source_url": url})
    engine.rebuild()
    assert engine.query("New revision")["total"] == 1
    assert engine.query("Same verified article")["total"] == 1
    # The explicit copy survives friendship removal, with share provenance gone.
    alice.revoke(alice.store.relationship_for_peer(bob.peer_id)["relationship_id"], notify=False)
    engine.rebuild()
    assert set(engine.query("Same verified article")["results"][0]["kinds"]) == {"saved", "history"}


def test_routes_auth_reinstallation_current_source_and_safe_post_logging(tmp_path, caplog):
    adapter, _, _, _, conversations, alice, _, _ = sources(tmp_path)
    conversations.save(sample(), expected_revision=0)
    workers = BackgroundWorkerRegistry()
    app = FastAPI()
    def guard(request):
        if request.headers.get("x-owner") != "yes":
            raise HTTPException(401)
    def install(root):
        return install_local_search(app, store=SimpleNamespace(home=root), home=tmp_path / "wrong-home",
            workers=workers, messaging_key=alice.messaging_private, local_control=guard, source=adapter.snapshot)
    engine = install(alice.home)
    install(alice.home)
    assert len([route for route in app.routes if route.path == "/api/local/search/query"]) == 1
    assert [spec.name for spec in workers.specs()] == ["local-search.index"]
    spec = workers.specs()[0]
    assert spec.initial_delay_s == 1 and spec.policy.busy_delay_s == 1
    client = TestClient(app)
    for path, method in (("status", "get"), ("query", "post"), ("rebuild", "post"), ("open?identifier=x", "get")):
        assert getattr(client, method)("/api/local/search/" + path).status_code == 401
    owner = {"x-owner": "yes"}
    assert client.post("/api/local/search/rebuild", headers=owner).status_code == 200
    marker = "秘密文章正文"
    result = client.post("/api/local/search/query", headers=owner, json={"query": marker})
    assert result.status_code == 200 and result.json()["total"] == 1
    status = client.get("/api/local/search/status", headers=owner).json()
    assert set(status) == {"version", "state", "error_code", "indexed_count", "updated_at", "worker"}
    assert marker not in caplog.text and marker not in str(status)
    replacement = install(tmp_path / "replacement")
    assert replacement.path != engine.path and replacement.status()["state"] == "needs_rebuild"
    workers.specs()[0].run_once()
    assert replacement.path.is_file() and not (tmp_path / "wrong-home" / "local-search").exists()
    assert client.post("/api/local/search/query", headers=owner, json={"query": marker}).json()["total"] == 1
    assert client.post("/api/local/search/query", headers=owner, json={}).status_code == 400
    assert client.post("/api/local/search/query", headers=owner, content=b"x" * 8193).status_code == 413
    assert peer_box.public_key_b64(alice.messaging_private) not in str(status)
