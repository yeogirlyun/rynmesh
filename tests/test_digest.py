"""Daily Digest service: feed parsing, source resolution, ranking, endpoints."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from rynmesh.peer_http import create_app
from rynmesh.services.digest import (
    DEFAULT_DISCOVERY_SOURCES,
    DigestError,
    DigestService,
    parse_feed,
    resolve_source,
)
from rynmesh.store import RynmeshStore

NOW = 1_800_000_000.0
DAY = 86_400.0

RSS = b"""<?xml version="1.0"?>
<rss version="2.0"><channel>
  <title>Example Blog</title>
  <item>
    <title>Post &amp; One</title>
    <link>https://example.com/one</link>
    <description>&lt;p&gt;Hello &lt;b&gt;world&lt;/b&gt;&lt;/p&gt;</description>
    <pubDate>Wed, 29 Jul 2026 12:00:00 GMT</pubDate>
  </item>
  <item>
    <title>No link, dropped</title>
    <description>orphan</description>
  </item>
</channel></rss>"""

ATOM_YT = b"""<?xml version="1.0"?>
<feed xmlns="http://www.w3.org/2005/Atom" xmlns:media="http://search.yahoo.com/mrss/">
  <title>Some Channel</title>
  <entry>
    <title>Video A</title>
    <link rel="alternate" href="https://www.youtube.com/watch?v=abc123"/>
    <published>2026-07-28T09:30:00+00:00</published>
    <author><name>Some Channel</name></author>
    <media:group>
      <media:thumbnail url="https://i.ytimg.com/vi/abc123/hq.jpg"/>
      <media:description>A great video</media:description>
    </media:group>
  </entry>
</feed>"""

RSS_MEDIA = b"""<?xml version="1.0"?>
<rss version="2.0" xmlns:media="http://search.yahoo.com/mrss/"><channel>
  <title>Media Feed</title>
  <item>
    <title>Audio episode</title>
    <link>https://example.com/episode</link>
    <description>&lt;p&gt;Listen now&lt;/p&gt;</description>
    <enclosure url="https://cdn.example.com/episode.mp3" type="audio/mpeg" />
  </item>
  <item>
    <title>Comic</title>
    <link>https://example.com/comic</link>
    <description>&lt;img src="https://cdn.example.com/comic.png" /&gt; A comic</description>
  </item>
</channel></rss>"""


def make_fetcher(responses):
    calls = []

    def fetch(url, timeout_s):
        calls.append(url)
        for prefix, payload in responses.items():
            if url.startswith(prefix):
                if isinstance(payload, Exception):
                    raise payload
                return payload
        raise DigestError(f"feed_unreachable: no fixture for {url}")

    fetch.calls = calls
    return fetch


# ------------------------------------------------------------- parsing ----


def test_parse_rss_strips_html_and_drops_linkless():
    title, entries = parse_feed(RSS)
    assert title == "Example Blog"
    assert len(entries) == 1
    assert entries[0]["title"] == "Post & One"
    assert entries[0]["summary"] == "Hello  world"
    assert entries[0]["published_unix"] > 0


def test_parse_atom_youtube_extracts_thumbnail_and_author():
    title, entries = parse_feed(ATOM_YT)
    assert title == "Some Channel"
    assert entries[0]["link"] == "https://www.youtube.com/watch?v=abc123"
    assert entries[0]["thumbnail"].endswith("hq.jpg")
    assert entries[0]["author"] == "Some Channel"


def test_parse_feed_preserves_playable_audio_and_embedded_images():
    _, entries = parse_feed(RSS_MEDIA)
    assert entries[0]["media_url"] == "https://cdn.example.com/episode.mp3"
    assert entries[0]["content_type"] == "audio/mpeg"
    assert entries[1]["thumbnail"] == "https://cdn.example.com/comic.png"


def test_parse_rejects_non_feed():
    with pytest.raises(DigestError):
        parse_feed(b"<html><body>not a feed</body></html>")
    with pytest.raises(DigestError):
        parse_feed(b"totally not xml")


# ------------------------------------------------------------ resolving ----


def test_resolve_subreddit_shorthand():
    fetch = make_fetcher({"https://www.reddit.com/r/selfhosted/.rss": RSS})
    source = resolve_source("r/selfhosted", fetch)
    assert source["kind"] == "reddit"
    assert source["feed_url"].endswith("/r/selfhosted/.rss")
    assert source["title"] == "Example Blog"


def test_resolve_youtube_handle_via_page_lookup():
    page = b'<html>..."channelId":"UCabcdefghij12345"...</html>'
    fetch = make_fetcher(
        {
            "https://www.youtube.com/@somechannel": page,
            "https://www.youtube.com/feeds/videos.xml?channel_id=UCabcdefghij12345": ATOM_YT,
        }
    )
    source = resolve_source("@somechannel", fetch)
    assert source["kind"] == "youtube"
    assert "channel_id=UCabcdefghij12345" in source["feed_url"]


def test_resolve_youtube_prefers_own_channel_over_mentions():
    # Regression: channel pages mention many OTHER channels' "channelId" before
    # the page's own id — the page's own id is "externalId" (live-verified).
    page = b'..."channelId":"UCdecoy00000decoy"... more html ..."externalId":"UCowner0000owner1"...'
    fetch = make_fetcher(
        {
            "https://www.youtube.com/@real": page,
            "https://www.youtube.com/feeds/videos.xml?channel_id=UCowner0000owner1": ATOM_YT,
        }
    )
    source = resolve_source("@real", fetch)
    assert "UCowner0000owner1" in source["feed_url"]


def test_resolve_youtube_channel_url_directly():
    fetch = make_fetcher(
        {"https://www.youtube.com/feeds/videos.xml?channel_id=UCzzzzzzzzzz": ATOM_YT}
    )
    source = resolve_source("https://www.youtube.com/channel/UCzzzzzzzzzz", fetch)
    assert source["kind"] == "youtube"
    assert fetch.calls == ["https://www.youtube.com/feeds/videos.xml?channel_id=UCzzzzzzzzzz"]


def test_resolve_plain_rss_and_unreachable():
    fetch = make_fetcher({"https://example.com/feed.xml": RSS})
    source = resolve_source("https://example.com/feed.xml", fetch)
    assert source["kind"] == "rss"
    with pytest.raises(DigestError, match="feed_unreachable"):
        resolve_source("https://down.example.com/feed", fetch)


# -------------------------------------------------------------- service ----


def _rss_with(now_offset_items):
    items = "".join(
        f"<item><title>{title}</title><link>https://ex.com/{title}</link>"
        f"<pubDate>{when}</pubDate></item>"
        for title, when in now_offset_items
    )
    return f'<rss version="2.0"><channel><title>T</title>{items}</channel></rss>'.encode()


def _service(tmp_path, responses):
    return DigestService(tmp_path, fetcher=make_fetcher(responses))


def test_add_refresh_build_roundtrip(tmp_path):
    service = _service(tmp_path, {"https://a.example/feed": RSS})
    source = service.add_source("https://a.example/feed")
    assert service.list_sources()[0]["id"] == source["id"]
    with pytest.raises(DigestError, match="source_already_added"):
        service.add_source("https://a.example/feed")

    # Add-time validation fetch seeds the item store — items exist pre-refresh
    # (and rate-limited hosts aren't hit twice back-to-back).
    assert service.build(now_unix=NOW)["items"]

    result = service.refresh()
    assert result["new_items"] == 0  # everything already seeded at add time
    assert result["sources"][0]["ok"] is True

    digest = service.build(now_unix=NOW)
    assert len(digest["items"]) == 1
    assert digest["items"][0]["link"] == "https://example.com/one"
    assert digest["items"][0]["reasons"]
    assert service.last_digest()["items"][0]["item_id"] == digest["items"][0]["item_id"]


def test_default_discovery_builds_real_unread_recommendations_without_setup(tmp_path):
    def fetch(url, timeout_s):
        slug = url.split("//", 1)[-1].replace("/", "-").replace("?", "-")
        return RSS.replace(b"Example Blog", slug.encode()).replace(
            b"https://example.com/one",
            f"https://content.example/{slug}".encode(),
        )

    service = DigestService(tmp_path, fetcher=fetch, bootstrap_defaults=True)
    assert len(service.list_sources()) == len(DEFAULT_DISCOVERY_SOURCES)
    assert all(source["builtin"] is True for source in service.list_sources())

    result = service.proactive_refresh(now_unix=NOW)
    assert result["status"]["phase"] == "ready"
    assert result["status"]["item_count"] > 0
    assert result["status"]["unread_count"] > 0
    assert {"video", "audio", "image", "document"}.issubset(set(result["status"]["formats"]))

    recommendations = service.recommendation_items()
    assert recommendations
    assert all(item["external"] is True for item in recommendations)
    assert all(item["external_url"].startswith("https://") for item in recommendations)
    assert any(item["content_kind"] == "audio" for item in recommendations)
    assert service.mark_discovery_seen(now_unix=NOW + 1)["unread_count"] == 0


def test_removed_default_source_stays_disabled(tmp_path):
    service = DigestService(tmp_path, fetcher=lambda *_: RSS, bootstrap_defaults=True)
    source_id = DEFAULT_DISCOVERY_SOURCES[0]["id"]
    assert service.remove_source(source_id) is True
    service.ensure_default_sources()
    assert source_id not in {source["id"] for source in service.list_sources()}


def test_refresh_survives_one_bad_source(tmp_path):
    service = _service(
        tmp_path,
        {"https://good.example/feed": RSS, "https://bad.example/feed": RuntimeError("boom")},
    )
    service.add_source("https://good.example/feed")
    # sneak the bad source in past add-time validation
    sources = service.list_sources()
    sources.append(
        {
            "id": "badbadbadbadbad1",
            "kind": "rss",
            "feed_url": "https://bad.example/feed",
            "title": "Bad",
            "tags": ["rss"],
            "weight": 1.0,
        }
    )
    service._save("sources.json", sources)

    result = service.refresh()
    by_ok = {entry["ok"] for entry in result["sources"]}
    assert by_ok == {True, False}
    good = next(entry for entry in result["sources"] if entry["ok"])
    assert good["item_count"] == 1  # seeded at add; bad feed didn't kill the run


def test_feedback_moves_ranking_and_suppresses_seen(tmp_path):
    fresh = "Thu, 30 Jul 2026 00:00:00 GMT"
    service = _service(
        tmp_path,
        {
            "https://a.example/feed": _rss_with([("a1", fresh), ("a2", fresh)]),
            "https://b.example/feed": _rss_with([("b1", fresh), ("b2", fresh)]),
        },
    )
    service.add_source("https://a.example/feed")
    service.add_source("https://b.example/feed")
    service.refresh()

    first = service.build(now_unix=NOW)
    a_item = next(i for i in first["items"] if i["link"].endswith("/a1"))
    b_item = next(i for i in first["items"] if i["link"].startswith("https://ex.com/b"))

    service.feedback(a_item["item_id"], "down")  # a1 seen + source A weight down
    service.feedback(b_item["item_id"], "up")  # b1 seen + source B weight up

    second = service.build(now_unix=NOW)
    links = [item["link"] for item in second["items"]]
    assert f"https://ex.com/{'a1'}" not in links  # seen items never resurface
    assert links[0].startswith("https://ex.com/b")  # upvoted source outranks downvoted
    top = second["items"][0]
    assert "source you like" in top["reasons"]


def test_feedback_rejects_unknown_item_and_action(tmp_path):
    service = _service(tmp_path, {"https://a.example/feed": RSS})
    service.add_source("https://a.example/feed")
    service.refresh()
    with pytest.raises(DigestError):
        service.feedback("nope", "up")
    item_id = service.build(now_unix=NOW)["items"][0]["item_id"]
    with pytest.raises(DigestError):
        service.feedback(item_id, "meh")


def test_remove_source_drops_its_items(tmp_path):
    service = _service(tmp_path, {"https://a.example/feed": RSS})
    source = service.add_source("https://a.example/feed")
    service.refresh()
    assert service.remove_source(source["id"]) is True
    assert service.remove_source(source["id"]) is False
    assert service.build(now_unix=NOW)["items"] == []


# ------------------------------------------------------------- endpoints ----


def _client(tmp_path, monkeypatch):
    monkeypatch.setenv("RYNMESH_HOME", str(tmp_path / "node"))
    monkeypatch.setenv("RYNMESH_AUTO_REGISTER", "0")
    monkeypatch.setenv("RYNMESH_ALLOW_REMOTE_CONTROL", "1")
    # Never resolve a real model provider (a live local Ollama would make
    # these tests run actual inference).
    monkeypatch.setenv("RYNMESH_MODEL_PROVIDER", "none")
    store = RynmeshStore()
    app = create_app(store)
    app.state.digest_service.fetcher = make_fetcher({"https://a.example/feed": RSS})
    return TestClient(app)


def test_endpoint_source_lifecycle_and_digest(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)

    assert client.get("/api/local/sources").json() == []
    added = client.post("/api/local/sources", json={"url": "https://a.example/feed"})
    assert added.status_code == 200
    source_id = added.json()["id"]

    bad = client.post("/api/local/sources", json={"url": "https://a.example/feed"})
    assert bad.status_code == 400

    refreshed = client.post("/api/local/digest/refresh")
    assert refreshed.status_code == 200
    assert refreshed.json()["digest"]["items"]

    digest = client.get("/api/local/digest")
    assert digest.status_code == 200
    item_id = digest.json()["items"][0]["item_id"]

    fb = client.post("/api/local/digest/feedback", json={"item_id": item_id, "action": "up"})
    assert fb.status_code == 200

    gone = client.delete(f"/api/local/sources/{source_id}")
    assert gone.status_code == 200
    assert client.get("/api/local/sources").json() == []


def test_endpoint_digest_empty_before_any_refresh(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    response = client.get("/api/local/digest")
    assert response.status_code == 200
    assert response.json()["items"] == []


def test_endpoint_add_source_error_is_400(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    response = client.post("/api/local/sources", json={"url": "https://nofixture.example/x"})
    assert response.status_code == 400
    assert "feed_unreachable" in response.json()["detail"]


# ---------------------------------------------------- read-later & watchers ----

PAGE_V1 = b"""<html><head><title>A Page</title>
<meta property="og:description" content="What this page is about."></head>
<body><script>junk()</script><p>version one body</p></body></html>"""
PAGE_V2 = PAGE_V1.replace(b"version one body", b"version two body")


def test_save_link_extracts_meta_and_ranks_fresh(tmp_path):
    service = _service(tmp_path, {"https://a.example/feed": RSS, "https://ex.com/article": PAGE_V1})
    service.add_source("https://a.example/feed")
    result = service.save_link("https://ex.com/article", now_unix=NOW)
    assert result["title"] == "A Page"
    assert "about" in result["summary"]

    digest = service.build(now_unix=NOW)
    top = digest["items"][0]
    assert top["link"] == "https://ex.com/article"  # fresh + boosted weight wins
    assert top["source_kind"] == "saved"

    with pytest.raises(DigestError, match="readlater_url_invalid"):
        service.save_link("not-a-url", now_unix=NOW)


def test_watcher_fires_on_change_and_every_change(tmp_path):
    pages = {"value": PAGE_V1}

    def fetch(url, timeout_s):
        if url == "https://shop.example/item":
            return pages["value"]
        raise DigestError("no fixture")

    service = DigestService(tmp_path, fetcher=fetch)
    watcher = service.add_watcher("https://shop.example/item", note="price drop")
    assert watcher["title"] == "A Page"
    with pytest.raises(DigestError, match="watcher_already_added"):
        service.add_watcher("https://shop.example/item")

    assert service.check_watchers(now_unix=NOW) == 0  # unchanged
    pages["value"] = PAGE_V2
    assert service.check_watchers(now_unix=NOW) == 1  # changed -> item
    items = service.build(now_unix=NOW)["items"]
    assert any(i["title"] == "Changed: price drop" for i in items)

    # a further change produces a NEW item (unique id per change)
    pages["value"] = PAGE_V2.replace(b"two", b"three")
    assert service.check_watchers(now_unix=NOW + 60) == 1
    changed = [
        i for i in service.build(now_unix=NOW + 60)["items"] if i["title"].startswith("Changed:")
    ]
    assert len(changed) == 2

    assert service.remove_watcher(watcher["id"]) is True
    assert service.list_watchers() == []


def test_page_hash_ignores_markup_noise(tmp_path):
    from rynmesh.services.digest import _page_hash

    a = b"<html><body  data-ts='123'><p>same   text</p></body></html>"
    b = b"<html><body data-ts='999'><p>same text</p></body></html>"
    assert _page_hash(a) == _page_hash(b)


def test_local_sources_skipped_on_refresh(tmp_path):
    service = _service(tmp_path, {"https://ex.com/article": PAGE_V1})
    service.save_link("https://ex.com/article", now_unix=NOW)
    result = service.refresh()  # must not try to fetch "local:readlater"
    entry = result["sources"][0]
    assert entry["ok"] is True and entry["item_count"] == 1


def test_endpoint_readlater_and_watchers(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    client.app.state.digest_service.fetcher = make_fetcher(
        {"https://ex.com/article": PAGE_V1, "https://shop.example/item": PAGE_V1}
    )
    saved = client.post("/api/local/readlater", json={"url": "https://ex.com/article"})
    assert saved.status_code == 200 and saved.json()["title"] == "A Page"
    assert client.post("/api/local/readlater", json={"url": "bad"}).status_code == 400

    added = client.post(
        "/api/local/watchers", json={"url": "https://shop.example/item", "note": "restock"}
    )
    assert added.status_code == 200
    watcher_id = added.json()["id"]
    assert client.get("/api/local/watchers").json()[0]["note"] == "restock"
    assert client.delete(f"/api/local/watchers/{watcher_id}").status_code == 200
    assert client.delete(f"/api/local/watchers/{watcher_id}").status_code == 404
