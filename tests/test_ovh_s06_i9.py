"""S06-I9: real supervised PDF extraction through web open and cache."""
import io
from pathlib import Path

import httpx
import pytest
from pypdf import PdfWriter
from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject

from rynmesh.agent_runtime import local_specs, web_search
from rynmesh.agent_runtime.web_search import MAX_FETCH_BYTES, WebSearchEngine
from rynmesh.services.document_extract import extract_document

NOTICE = "PDF text could not be extracted; download it with ryn_download_file and read it with document tools."


def pdf(*, blank=False):
    writer = PdfWriter()
    for number in (1, 2):
        page = writer.add_blank_page(width=600, height=800)
        if blank:
            continue
        stream = DecodedStreamObject()
        stream.set_data(f"BT /F1 12 Tf 10 750 Td (Text from PDF page {number}) Tj ET".encode())
        page[NameObject("/Contents")] = writer._add_object(stream)
        font = DictionaryObject({NameObject("/Type"): NameObject("/Font"),
                                 NameObject("/Subtype"): NameObject("/Type1"),
                                 NameObject("/BaseFont"): NameObject("/Helvetica")})
        page[NameObject("/Resources")] = DictionaryObject({NameObject("/Font"):
            DictionaryObject({NameObject("/F1"): writer._add_object(font)})})
    output = io.BytesIO()
    writer.write(output)
    return output.getvalue()


def make_engine(tmp_path, data, mime):
    calls = []
    def fetch(url, domains):
        calls.append(url)
        return data, url, mime
    return WebSearchEngine(tmp_path / "cache", http_fetcher=fetch,
                           dns_resolver=lambda host: ["93.184.216.34"]), calls


@pytest.mark.parametrize("url,mime", [
    ("https://example.com/download", "application/pdf"),
    ("https://example.com/paper.pdf?version=1", "text/plain"),
    ("https://example.com/paper.pdf", "application/octet-stream"),
])
def test_T1_two_page_pdf_uses_real_child_markers_and_persistent_cache(tmp_path, monkeypatch, url, mime):
    monkeypatch.delenv("RYNMESH_DOC_EXTRACT_DISABLE_PDF", raising=False)
    data = pdf()
    target, fetches = make_engine(tmp_path, data, mime)
    observed = []
    def observe(path, **options):
        assert Path(path).read_bytes() == data
        observed.append((Path(path), options))
        return extract_document(path, **options)
    monkeypatch.setattr(web_search, "extract_document", observe, raising=False)
    first = target.execute({"open": url}, context={"task_id": "first"})
    lines = [row["text"] for row in first["page"]["lines"]]
    assert lines[0].startswith("# ") and lines[1].startswith("Source:")
    assert lines[2] == "PDF pages: 2/2/0"
    for number in (1, 2):
        assert f"--- page {number} ---" in lines
        assert f"Text from PDF page {number}" in lines
    assert len(observed) == 1 and not observed[0][0].exists()
    assert observed[0][1]["page_markers"] is True
    assert observed[0][1]["max_input_bytes"] == MAX_FETCH_BYTES
    # A fresh engine exercises the disk cache, not only the task's in-memory refs.
    fresh, second_fetches = make_engine(tmp_path, b"must not refetch", mime)
    second = fresh.execute({"open": url}, context={"task_id": "second"})
    assert second["page"]["cached"] is True
    assert second["page"]["lines"] == first["page"]["lines"]
    assert len(fetches) == 1 and second_fetches == [] and len(observed) == 1


def test_T1_real_http_octet_stream_pdf_is_not_rejected_before_signature_read(tmp_path, monkeypatch):
    data = pdf()
    target = WebSearchEngine(tmp_path / "cache", dns_resolver=lambda _: ["93.184.216.34"])
    real_client = httpx.Client
    monkeypatch.setattr(web_search.httpx, "Client", lambda **options: real_client(
        **options, transport=httpx.MockTransport(lambda request: httpx.Response(200,
            headers={"content-type": "application/octet-stream"}, stream=httpx.ByteStream(data)))))
    monkeypatch.setattr(target, "_proxy_active", lambda url: False)
    result = target.execute({"open": "https://example.com/paper.pdf"})
    assert "Text from PDF page 2" in result["page"]["content"]


def test_T1_old_pdf_html_cache_is_refetched(tmp_path):
    target, calls = make_engine(tmp_path, pdf(), "application/pdf")
    url = "https://example.com/paper.pdf"
    import time
    target.reader_cache.put(url, {"title": "Old PDF parsed as HTML", "url": url,
                                 "blocks": [{"type": "p", "text": "%PDF-old binary"}]}, now=time.time())
    result = target.execute({"open": url})
    assert "Text from PDF page 2" in result["page"]["content"]
    assert calls == [url]


@pytest.mark.parametrize("reason", ["disabled", "scanned", "broken"])
def test_T2_T3_pdf_failures_return_only_download_guidance_and_clean_temp(tmp_path, monkeypatch, reason):
    data = pdf(blank=reason == "scanned") if reason != "broken" else b"%PDF-1.7\nnot a PDF"
    if reason == "disabled":
        monkeypatch.setenv("RYNMESH_DOC_EXTRACT_DISABLE_PDF", "1")
    else:
        monkeypatch.delenv("RYNMESH_DOC_EXTRACT_DISABLE_PDF", raising=False)
    observed = []
    def observe(path, **options):
        observed.append(Path(path))
        return extract_document(path, **options)
    monkeypatch.setattr(web_search, "extract_document", observe, raising=False)
    def no_html(*args, **kwargs):
        raise AssertionError("PDF bytes must not enter HTML extraction")
    monkeypatch.setattr(web_search, "extract_readable", no_html)
    target, _ = make_engine(tmp_path, data, "application/pdf")
    result = target.execute({"open": "https://example.com/document.pdf"})
    assert [row["text"] for row in result["page"]["lines"]] == [NOTICE]
    assert observed and all(not path.exists() for path in observed)
    cached = target.execute({"open": "https://example.com/document.pdf"}, context={"task_id": "cached"})
    assert [row["text"] for row in cached["page"]["lines"]] == [NOTICE]
    assert cached["page"]["cached"] is True


def test_T1_page_markers_are_opt_in_and_count_toward_output_cap(tmp_path, monkeypatch):
    monkeypatch.delenv("RYNMESH_DOC_EXTRACT_DISABLE_PDF", raising=False)
    path = tmp_path / "import.pdf"
    path.write_bytes(pdf())
    before = extract_document(path)
    assert "--- page" not in before["text"]
    marked = extract_document(path, page_markers=True, max_output_chars=50)
    assert "--- page 1 ---" in marked["text"]
    assert len(marked["text"]) <= 50 and marked["status"] == "truncated"
    assert extract_document(path)["text"] == before["text"]


def test_T4_html_remains_readable_and_tool_description_routes_interactive_work(tmp_path, monkeypatch):
    html = b"<html><title>Ordinary HTML</title><article><p>This HTML article remains readable with enough ordinary prose.</p></article></html>"
    target, _ = make_engine(tmp_path, html, "text/html")
    def no_document(*args, **kwargs):
        raise AssertionError("HTML must not start a PDF worker")
    monkeypatch.setattr(web_search, "extract_document", no_document, raising=False)
    result = target.execute({"open": "https://example.com/article"})
    assert "Ordinary HTML" in result["page"]["content"]
    assert "This HTML article remains readable" in result["page"]["content"]
    description = local_specs.SPECS["ryn_web_search"][0]
    assert "PDF links return extracted text lines." in description
    assert "ryn_browser_open/read/action" in description
