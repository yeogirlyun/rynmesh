"""S06-I15: real web fetch/decode paths, streaming limits and cancellation."""
import codecs
import json
import time

import httpx
import pytest

from rynmesh.agent_runtime import web_search
from rynmesh.agent_runtime.store import AgentToolRejected
from rynmesh.agent_runtime.web_search import MAX_FETCH_BYTES, WebSearchEngine
from rynmesh.services import reader

URL = "https://example.com/article"
TEXT = "中文网页正文字符解码应当正确，不能出现乱码。" * 4


def engine(tmp_path, fetcher=None):
    return WebSearchEngine(tmp_path / "cache", http_fetcher=fetcher,
                           dns_resolver=lambda host: ["93.184.216.34"])


@pytest.mark.parametrize("kind", ["html", "fallback", "plain"])
def test_T1_gbk_header_reaches_all_page_paths(tmp_path, kind):
    markup = (f"<article><p>{TEXT}</p></article>" if kind == "html"
              else f"<div>{TEXT}</div>" if kind == "fallback" else TEXT)
    mime = "text/plain" if kind == "plain" else "text/html"
    fetch = lambda url, domains: (markup.encode("gbk"), url, mime + "; charset=gbk")
    result = engine(tmp_path, fetch).execute({"open": URL})
    assert TEXT in "".join(row["text"] for row in result["page"]["lines"])


@pytest.mark.parametrize("meta", [
    '<meta charset="gb2312">',
    '<meta content="text/html; charset=gb2312" http-equiv="Content-Type">',
])
def test_T2_meta_charset_without_header(tmp_path, meta):
    data = f"<html><head>{meta}</head><body><article><p>{TEXT}</p></article></body></html>".encode("gbk")
    result = engine(tmp_path, lambda url, _: (data, url, "text/html")).execute({"open": URL})
    assert TEXT in "".join(row["text"] for row in result["page"]["lines"])
    assert reader.extract_readable(data)["blocks"][0]["text"] == TEXT


def test_T3_undeclared_gbk_falls_back_to_gb18030(tmp_path):
    data = f"<article><p>{TEXT}</p></article>".encode("gbk")
    result = engine(tmp_path, lambda url, _: (data, url, "text/html")).execute({"open": URL})
    assert TEXT in "".join(row["text"] for row in result["page"]["lines"])


@pytest.mark.parametrize("encoding,bom", [
    ("utf-8", codecs.BOM_UTF8), ("utf-16-le", codecs.BOM_UTF16_LE),
    ("utf-16-be", codecs.BOM_UTF16_BE), ("utf-32-le", codecs.BOM_UTF32_LE),
    ("utf-32-be", codecs.BOM_UTF32_BE),
])
def test_T4_bom_wins_over_header_and_meta(encoding, bom):
    text = '<meta charset="gbk">' + TEXT
    assert reader.decode_html(bom + text.encode(encoding), "text/html; charset=big5") == text


@pytest.mark.parametrize("charset,text", [("big5", "繁體中文網頁測試"), ("shift_jis", "日本語のページ")])
def test_T1_other_declared_charsets(charset, text):
    assert reader.decode_html(text.encode(charset), f"text/html; CHARSET='{charset}'") == text


def test_T2_header_priority_invalid_names_and_meta_scan_limit():
    data = ('<meta charset="utf-8">' + TEXT).encode("gbk")
    assert reader.decode_html(data, "text/html; charset=gbk") == data.decode("gbk")
    meta = ('<meta charset="gbk">' + TEXT).encode("gbk")
    assert reader.decode_html(meta, "text/html; charset=not-a-codec") == meta.decode("gbk")
    assert reader.decode_html(b'<meta charset="not-a-codec">' + TEXT.encode()).endswith(TEXT)
    # A late declaration cannot override the strict UTF-8 fallback.
    late = b" " * 4096 + b'<meta charset="gbk">' + TEXT.encode()
    assert reader.decode_html(late).endswith(TEXT)


def test_T4_utf8_and_old_reader_cache_invalidation(tmp_path):
    data = f"<article><p>{TEXT}</p></article>".encode()
    assert reader.extract_readable(data)["blocks"][0]["text"] == TEXT
    assert reader.EXTRACTOR_VERSION == 4
    cache = reader.ReaderCache(tmp_path / "reader")
    cache.put(URL, {"blocks": []}, now=100)
    path = cache._path(URL)
    payload = json.loads(path.read_text())
    payload["extractor"] = 3
    path.write_text(json.dumps(payload))
    assert cache.get(URL, now=101) is None


class CountingStream(httpx.SyncByteStream):
    def __init__(self, chunks=160, on_chunk=None):
        self.chunks = chunks
        self.reads = 0
        self.closed = False
        self.on_chunk = on_chunk

    def __iter__(self):
        for _ in range(self.chunks):
            self.reads += 1
            if self.on_chunk:
                self.on_chunk()
            yield b"a" * (64 * 1024)

    def close(self):
        self.closed = True


def mock_http(monkeypatch, target, handler):
    real_client = httpx.Client
    monkeypatch.setattr(web_search.httpx, "Client",
                        lambda **kwargs: real_client(**kwargs, transport=httpx.MockTransport(handler)))
    # Exercise the production direct path, including approved-IP pinning.
    monkeypatch.setattr(target, "_proxy_active", lambda url: False)


def test_T4_ten_mib_stream_stops_at_two_mib_and_cached_lines_keep_marker(tmp_path, monkeypatch):
    target = engine(tmp_path)
    body = CountingStream()
    mock_http(monkeypatch, target, lambda request: httpx.Response(200,
              headers={"content-type": "text/plain"}, stream=body))
    first = target.execute({"open": URL}, context={"task_id": "one"})
    assert body.reads * 64 * 1024 == MAX_FETCH_BYTES
    assert body.closed
    assert first["page"]["lines"][-1]["text"] == "[Page truncated at 2 MiB]"
    cached = target.execute({"open": URL}, context={"task_id": "two"})
    assert cached["page"]["cached"] is True
    assert cached["page"]["lines"][-1]["text"] == "[Page truncated at 2 MiB]"


def test_T4_redirect_body_is_not_read_and_final_response_is_closed(tmp_path, monkeypatch):
    target = engine(tmp_path)
    redirect, final = CountingStream(), CountingStream(chunks=1)
    def handler(request):
        assert request.headers["host"] == "example.com"
        if request.url.path == "/article":
            return httpx.Response(302, headers={"location": "/final"}, stream=redirect)
        return httpx.Response(200, headers={"content-type": "text/plain"}, stream=final)
    mock_http(monkeypatch, target, handler)
    result = target._fetch_http_bytes(URL, [])
    assert redirect.reads == 0 and redirect.closed
    assert final.reads == 1 and final.closed
    assert result[1] == "https://example.com/final"
    assert result[3] is False


def test_T5_slow_stream_hits_total_deadline_without_reading_to_end(tmp_path, monkeypatch):
    target = engine(tmp_path)
    clock = [0.0]
    monkeypatch.setattr(web_search.time, "monotonic", lambda: clock[0])
    def slow_chunk():
        time.sleep(0.001)
        clock[0] += 11  # Accelerate elapsed time; retain a real slow stream boundary.
    body = CountingStream(on_chunk=slow_chunk)
    mock_http(monkeypatch, target, lambda request: httpx.Response(200,
              headers={"content-type": "text/plain"}, stream=body))
    with pytest.raises(AgentToolRejected, match="agent_web_search_fetch_failed") as caught:
        target.execute({"open": URL})
    assert caught.value.correction == "Fetch exceeded 30 seconds."
    assert body.reads == 3 and body.closed


@pytest.mark.parametrize("mime", ["image/png", "video/mp4", "audio/mpeg", "application/zip",
                                  "application/octet-stream", "application/x-msdownload"])
def test_T6_obvious_binary_is_rejected_before_body_read(tmp_path, monkeypatch, mime):
    target = engine(tmp_path)
    body = CountingStream()
    mock_http(monkeypatch, target, lambda request: httpx.Response(200,
              headers={"content-type": mime}, stream=body))
    with pytest.raises(AgentToolRejected, match="agent_web_search_unsupported_content_type") as caught:
        target.execute({"open": URL})
    assert "ryn_download_file" in caught.value.correction
    assert body.reads == 0 and body.closed


def test_T6_pdf_is_exempt_from_binary_block(tmp_path, monkeypatch):
    target = engine(tmp_path)
    body = CountingStream(chunks=1)
    mock_http(monkeypatch, target, lambda request: httpx.Response(200,
              headers={"content-type": "application/pdf"}, stream=body))
    result = target._fetch_http_bytes(URL, [])
    assert result[2] == "application/pdf" and body.closed


def test_T7_execute_cancellation_reaches_stream_and_closes_connection(tmp_path, monkeypatch):
    target = engine(tmp_path)
    body = CountingStream()
    mock_http(monkeypatch, target, lambda request: httpx.Response(200,
              headers={"content-type": "text/plain"}, stream=body))
    with pytest.raises(AgentToolRejected, match="agent_cancelled"):
        target.execute({"open": URL}, cancelled=lambda: body.reads >= 2)
    assert body.reads == 2 and body.closed
