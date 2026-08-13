"""Article reader, steering, and format balance — the content experience."""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from rynmesh.peer_http import create_app
from rynmesh.services.digest import DigestService, parse_steering
from rynmesh.services.reader import ReaderCache, ReaderError, extract_readable, read_article
from rynmesh.store import RynmeshStore

ARTICLE = b"""<html><head>
<title>Fallback Title</title>
<meta property="og:title" content="How Compilers Work">
<meta name="author" content="Jane Dev">
<meta property="og:image" content="https://ex.com/lead.png">
</head><body>
<nav><a href="/x">Home</a><a href="/y">About</a></nav>
<div class="sidebar"><p>Subscribe to our newsletter for more content today!</p></div>
<article>
  <h1>How Compilers Work</h1>
  <p>A compiler translates source code into machine code through several
     distinct phases that each have a well defined responsibility.</p>
  <p>The lexer turns characters into tokens, and the parser turns those tokens
     into a tree that mirrors the grammar of the language.</p>
  <p>short</p>
  <script>tracker();</script>
</article>
<footer><p>Copyright notice and various other footer boilerplate text here.</p></footer>
</body></html>"""


# ------------------------------------------------------------- extraction ----

def test_extracts_body_and_drops_chrome():
    art = extract_readable(ARTICLE, url="https://ex.com/a")
    text = " ".join(b["text"] for b in art["blocks"])
    assert "lexer turns characters into tokens" in text
    assert "Subscribe to our newsletter" not in text   # sidebar dropped
    assert "footer boilerplate" not in text            # footer dropped
    assert "tracker()" not in text                     # script dropped
    assert "short" not in text                         # sub-threshold noise
    assert art["title"] == "How Compilers Work"        # og:title beats <title>
    assert art["byline"] == "Jane Dev"
    assert art["lead_image"].endswith("lead.png")
    assert art["word_count"] > 20


def test_pages_without_prose_still_return_something():
    # Link-dump / JS-rendered pages must not yield a blank reader panel.
    art = extract_readable(b"<html><body><div><h1>Daily Links</h1>"
                           b"<li>First link item</li><li>Second link item</li>"
                           b"</div></body></html>")
    assert any("link item" in b["text"] for b in art["blocks"])


def test_malformed_html_does_not_raise():
    art = extract_readable(b"<html><body><p>unclosed <b>tags <div></body>")
    assert isinstance(art["blocks"], list)


# ------------------------------------------------------------------ fetch ----

def test_read_article_caches_and_rejects_bad_urls(tmp_path):
    calls = []

    def fetch(url, timeout_s):
        calls.append(url)
        return ARTICLE

    cache = ReaderCache(tmp_path / "c")
    first = read_article("https://ex.com/a", fetcher=fetch, cache=cache, now=1000.0)
    second = read_article("https://ex.com/a", fetcher=fetch, cache=cache, now=1001.0)
    assert first["cached"] is False and second["cached"] is True
    assert len(calls) == 1                      # served from cache

    with pytest.raises(ReaderError, match="reader_url_invalid"):
        read_article("not-a-url", fetcher=fetch)

    def boom(url, timeout_s):
        raise OSError("dns")

    with pytest.raises(ReaderError, match="reader_unreachable"):
        read_article("https://ex.com/b", fetcher=boom)


# --------------------------------------------------------------- steering ----

def test_steering_splits_polarity_per_clause():
    parsed = parse_steering("more math explainers and rust, less politics, no crypto")
    assert "term:math" in parsed["interests"] and "term:rust" in parsed["interests"]
    assert "term:politics" in parsed["avoids"] and "term:crypto" in parsed["avoids"]
    assert not set(parsed["interests"]) & set(parsed["avoids"])


def test_steering_defaults_to_positive_without_a_cue():
    assert parse_steering("deep learning papers")["interests"] == [
        "term:deep", "term:learning", "term:papers"
    ]


def _feed(items):
    entries = "".join(
        f"<item><title>{t}</title><link>https://ex.com/{i}</link>"
        f"<description>{d}</description>"
        f"<pubDate>Mon, 03 Aug 2026 12:00:00 GMT</pubDate></item>"
        for i, (t, d) in enumerate(items)
    )
    return f'<rss version="2.0"><channel><title>F</title>{entries}</channel></rss>'.encode()


def test_steering_changes_the_next_slate(tmp_path):
    feed = _feed([("Rust compilers explained", "systems programming"),
                  ("Election polling roundup", "politics coverage")])
    service = DigestService(tmp_path, fetcher=lambda url, t: feed)
    service.add_source("https://ex.com/feed")

    service.steer("more rust, less politics")
    titles = [i["title"] for i in service.build(now_unix=1_800_000_000.0)["items"]]
    assert titles[0].startswith("Rust")           # steered item leads
    assert service.get_steering()["text"] == "more rust, less politics"


def test_more_like_this_learns_the_items_own_terms(tmp_path):
    feed = _feed([("Rust compilers explained", "systems programming")])
    service = DigestService(tmp_path, fetcher=lambda url, t: feed)
    service.add_source("https://ex.com/feed")
    item_id = service.build(now_unix=1_800_000_000.0)["items"][0]["item_id"]

    service.feedback(item_id, "more_like_this")
    affinity = service._load("prefs.json", {})["tag_affinity"]
    assert affinity.get("term:rust", 0) > 0       # subject, not just the feed
    assert affinity.get("term:compilers", 0) > 0


def test_a_high_volume_feed_cannot_own_the_slate(tmp_path):
    # 40 articles vs 2 videos: the videos must still appear.
    articles = _feed([(f"Article {n}", "text") for n in range(40)])
    videos = (b'<?xml version="1.0"?><feed xmlns="http://www.w3.org/2005/Atom">'
              b"<title>Chan</title>"
              b'<entry><title>Video A</title><link rel="alternate" href="https://yt/a"/>'
              b"<published>2026-08-03T12:00:00+00:00</published></entry>"
              b'<entry><title>Video B</title><link rel="alternate" href="https://yt/b"/>'
              b"<published>2026-08-03T11:00:00+00:00</published></entry></feed>")

    def fetch(url, timeout_s):
        return videos if "youtube" in url else articles

    service = DigestService(tmp_path, fetcher=fetch)
    service.add_source("https://ex.com/feed")
    service.add_source("https://www.youtube.com/feeds/videos.xml?channel_id=UCaaaaaaaaaaa")
    kinds = {i["content_kind"] for i in service.build(now_unix=1_800_000_000.0, limit=10)["items"]}
    assert "video" in kinds, kinds


# -------------------------------------------------------------- endpoints ----

def _client(tmp_path, monkeypatch):
    monkeypatch.setenv("RYNMESH_HOME", str(tmp_path / "node"))
    monkeypatch.setenv("RYNMESH_AUTO_REGISTER", "0")
    monkeypatch.setenv("RYNMESH_ALLOW_REMOTE_CONTROL", "1")
    monkeypatch.setenv("RYNMESH_MODEL_PROVIDER", "none")
    monkeypatch.setenv("RYNMESH_DISABLE_DISCOVERY", "1")
    app = create_app(RynmeshStore())
    app.state.digest_service.fetcher = lambda url, t: ARTICLE
    return TestClient(app)


def test_reader_endpoint_returns_blocks(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    body = client.get("/api/local/reader", params={"url": "https://ex.com/a"}).json()
    assert body["title"] == "How Compilers Work"
    assert any("lexer" in b["text"] for b in body["blocks"])


def test_reader_endpoint_rejects_bad_url(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    assert client.get("/api/local/reader", params={"url": "ftp://x"}).status_code == 400


def test_steer_endpoint_round_trip(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    posted = client.post("/api/local/digest/steer", json={"text": "more rust, less politics"})
    assert posted.status_code == 200
    assert "term:rust" in posted.json()["interests"]
    assert client.get("/api/local/digest/steer").json()["avoids"] == ["term:politics"]


def test_reddit_is_fetched_from_the_server_rendered_host():
    # www.reddit.com returns a bot-check page with no content; old.reddit.com
    # serves the same post as plain HTML.
    from rynmesh.services.reader import readable_url

    assert readable_url("https://www.reddit.com/r/science/comments/abc/x/") == \
        "https://old.reddit.com/r/science/comments/abc/x/"
    assert readable_url("https://example.com/post") == "https://example.com/post"


def test_read_article_uses_the_rewritten_host(tmp_path):
    seen = []

    def fetch(url, timeout_s):
        seen.append(url)
        return ARTICLE

    read_article("https://www.reddit.com/r/science/comments/abc/x/", fetcher=fetch)
    assert seen == ["https://old.reddit.com/r/science/comments/abc/x/"]


def test_void_tags_do_not_swallow_the_document():
    # <img>/<meta> inside a skipped <nav> never emit an end tag; counting them
    # left the skip depth unbalanced and discarded the rest of the page.
    html = (b"<html><body><nav><img src=x><br><input></nav>"
            b"<article><p>" + b"Real article prose that must survive the nav. " * 3 +
            b"</p></article></body></html>")
    art = extract_readable(html)
    assert art["word_count"] > 20, art


REDDIT_LINK_POST = (b'<html><body><div id="siteTable">'
                    b'<p class="title"><a class="title may-blank" '
                    b'href="https://journal.example/study">Study title here</a></p></div>'
                    b'<div class="commentarea"><div class="usertext-body"><p>'
                    b"Welcome to r/science ! This is a heavily moderated subreddit in order to "
                    b"keep the discussion on science and not on politics or pseudoscience here."
                    b"</p><p>I am a bot, and this action was performed automatically by us.</p>"
                    b"</div></div></body></html>")

TARGET_ARTICLE = (b"<html><head><meta property='og:title' content='Study title here'></head>"
                  b"<body><article><p>" +
                  b"The study followed a large cohort over several years and found a clear effect. " * 4 +
                  b"</p></article></body></html>")


def test_reddit_link_post_reads_the_linked_article_not_the_bot_comment():
    from rynmesh.services.reader import link_post_target

    assert link_post_target(REDDIT_LINK_POST, base_url="https://old.reddit.com/x") == \
        "https://journal.example/study"

    fetched = []

    def fetch(url, timeout_s):
        fetched.append(url)
        return TARGET_ARTICLE if "journal.example" in url else REDDIT_LINK_POST

    art = read_article("https://www.reddit.com/r/science/comments/a/b/", fetcher=fetch)
    assert fetched == ["https://old.reddit.com/r/science/comments/a/b/",
                       "https://journal.example/study"]
    assert art["source_url"] == "https://journal.example/study"
    body = " ".join(b["text"] for b in art["blocks"])
    assert "followed a large cohort" in body
    assert "I am a bot" not in body and "Welcome to r/science" not in body


def test_self_posts_keep_the_discussion_page():
    from rynmesh.services.reader import link_post_target

    self_post = REDDIT_LINK_POST.replace(b"https://journal.example/study",
                                         b"https://www.reddit.com/r/science/comments/a/b/")
    assert link_post_target(self_post, base_url="https://old.reddit.com/x") == ""


def test_sticky_bot_boilerplate_is_never_the_article():
    def fetch(url, timeout_s):
        return REDDIT_LINK_POST.replace(
            b'href="https://journal.example/study"', b'href="/r/science/comments/a/b/"')

    art = read_article("https://www.reddit.com/r/science/comments/a/b/", fetcher=fetch)
    body = " ".join(b["text"] for b in art["blocks"]).lower()
    assert "i am a bot" not in body
    assert "welcome to r/" not in body


def test_cache_ignores_entries_from_an_older_extractor(tmp_path):
    # An upgraded reader must not keep serving yesterday's bad extraction.
    import json as _json

    from rynmesh.services.reader import EXTRACTOR_VERSION

    cache = ReaderCache(tmp_path / "c")
    cache.put("https://ex.com/a", {"blocks": [], "word_count": 0}, now=1000.0)
    stale = cache._path("https://ex.com/a")
    payload = _json.loads(stale.read_text())
    assert payload["extractor"] == EXTRACTOR_VERSION
    payload["extractor"] = EXTRACTOR_VERSION - 1
    stale.write_text(_json.dumps(payload))
    assert cache.get("https://ex.com/a", now=1001.0) is None
