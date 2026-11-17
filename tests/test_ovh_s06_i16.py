"""S06-I16: real article extraction, paged opens and denial-cache handling."""
import time

import pytest

from rynmesh.agent_runtime.store import AgentToolRejected
from rynmesh.agent_runtime.web_search import WebSearchEngine, article_to_lines
from rynmesh.services import reader

URL = "https://example.com/article"
PROSE = "This article explains the product specifications and the prices in the table below."


def page(body):
    return f"<html><head><title>Product guide</title></head><body><article><p>{PROSE}</p>{body}</article></body></html>".encode()


def engine(tmp_path, fetch):
    return WebSearchEngine(tmp_path / "web", http_fetcher=fetch,
        dns_resolver=lambda hostname: ["93.184.216.34"])


def texts(result):
    return [line["text"] for line in result["page"]["lines"]]


@pytest.mark.parametrize("nested", [False, True])
def test_T1_three_rows_four_columns_survive_real_open(tmp_path, nested):
    rows = [["Plan", "Price", "Users", "Storage"], ["Basic", "10", "2", "50GB"], ["Pro", "30", "8", "200GB"]]
    html_rows = []
    for index, row in enumerate(rows):
        tag = "th" if index == 0 else "td"
        html_rows.append("<tr>\n" + "".join(f"<{tag}>" + (f"<div><p>{value}</p></div>" if nested else value) + f"</{tag}>\n" for value in row) + "</tr>")
    body = page("<table>" + "".join(html_rows) + "</table>" + f"<p>{PROSE}</p>")
    target = engine(tmp_path, lambda url, _: (body, url, "text/html"))
    result = target.execute({"open": URL})
    for row in rows:
        assert "| " + " | ".join(row) + " |" in texts(result)
    article = target.reader_cache.get(URL, now=time.time())
    assert [b["tag"] for b in article["blocks"]].count("tr") == 3


def test_empty_cells_do_not_shift_columns():
    body = page("<table><tr>\n<td></td><td>2</td><td></td><td>4</td></tr></table>")
    article = reader.extract_readable(body)
    row = next(line for line in article_to_lines(article) if line.startswith("|"))
    assert [cell.strip() for cell in row.split("|")[1:-1]] == ["", "2", "", "4"]


def test_T2_paragraph_500_can_be_opened_after_first_page_and_disk_cache(tmp_path):
    body = page("".join(f"<p>Paragraph {i:04d}: details long enough to remain in the readable article.</p>" for i in range(1, 601)))
    fetched = []
    def fetch(url, domains):
        fetched.append(url)
        return body, url, "text/html"
    target = engine(tmp_path, fetch)
    first = target.execute({"open": URL, "max_lines": 2})
    assert first["page"]["has_more"] is True
    later = target.execute({"open": first["page"]["ref_id"], "start_line": 500, "max_lines": 6})
    assert "Paragraph 0500" in "\n".join(texts(later))
    restored = engine(tmp_path, fetch).execute({"open": URL, "start_line": 500, "max_lines": 6})
    assert "Paragraph 0500" in "\n".join(texts(restored)) and restored["page"]["cached"] is True
    assert len(fetched) == 1
    assert len(target.reader_cache.get(URL, now=time.time())["blocks"]) == 601
    ui = reader.read_article(URL, fetcher=lambda url, timeout: body, cache=reader.ReaderCache(tmp_path / "ui"))
    assert len(ui["blocks"]) == 400 and ui["blocks_total"] == 601 and ui["truncated"] is True


@pytest.mark.parametrize("second", ["url", "ref", "other_task", "restart"])
def test_T3_access_gate_is_not_reused_in_memory_or_disk(tmp_path, second):
    denied = page('<form><input type="password"></form>')
    public = page("<p>The public article is available on the refreshed response.</p>")
    calls = []
    def fetch(url, domains):
        calls.append(url)
        return denied if len(calls) == 1 else public, url, "text/html"
    target = engine(tmp_path, fetch)
    first = target.execute({"open": URL}, context={"task_id": "one"})
    assert target.reader_cache.get(URL, now=time.time()) is None
    reopened = engine(tmp_path, fetch) if second == "restart" else target
    result = reopened.execute({"open": first["page"]["ref_id"] if second == "ref" else URL},
        context={"task_id": "two" if second == "other_task" else "one"})
    assert len(calls) == 2 and result["page"]["cached"] is False
    assert "public article" in "\n".join(texts(result))
    reopened.execute({"open": URL}, context={"task_id": "two" if second == "other_task" else "one"})
    assert len(calls) == 2  # Successful public extraction still caches normally.


def test_T3_old_cached_gate_is_ignored_and_refetched(tmp_path):
    calls = []
    def fetch(url, domains):
        calls.append(url)
        return page("<p>The refreshed article is public and complete.</p>"), url, "text/html"
    target = engine(tmp_path, fetch)
    target.reader_cache.put(URL, {"access_required": True, "blocks": [{"tag": "p", "text": "Old sign-in gate"}]}, now=time.time())
    result = target.execute({"open": URL})
    assert calls == [URL] and result["page"]["cached"] is False
    assert "Old sign-in gate" not in "\n".join(texts(result))


def test_cached_only_mode_does_not_serve_gate_or_go_online(tmp_path):
    calls = []
    target = engine(tmp_path, lambda url, domains: calls.append(url))
    target.reader_cache.put(URL, {"access_required": True, "blocks": [{"tag": "p", "text": "Sign in"}]}, now=time.time())
    with pytest.raises(AgentToolRejected, match="agent_web_search_cache_miss"):
        target.execute({"open": URL, "mode": "cached"})
    assert calls == []


def test_T3_table_rows_never_win_prose_selection_or_get_short_paragraph_filter():
    noise = "<div><table>" + "".join("<tr><td><div><p>" + "Unrelated price table content " * 20 + "</p></div></td></tr>" for _ in range(40)) + "</table></div>"
    body = noise.encode() + page("<table><tr><td>1</td><td>x</td></tr></table>")
    article = reader.extract_readable(body)
    assert any(b["tag"] == "tr" and b["text"] == "1 | x" for b in article["blocks"])
    assert all("Unrelated price table" not in b["text"] for b in article["blocks"])


def test_custom_block_limit_counts_all_readable_blocks_and_announces_stop():
    body = page("".join(f"<p>Paragraph {i} contains sufficient readable text for extraction.</p>" for i in range(10)))
    article = reader.extract_readable(body, max_blocks=3)
    assert len(article["blocks"]) == 3 and article["blocks_total"] == 11 and article["truncated"] is True
    assert article_to_lines(article)[-1] == "[Extraction stopped at 3 blocks]"
    assert reader.EXTRACTOR_VERSION == 4  # S06-I15 already performed the shared 3 -> 4 bump.


def test_web_limit_4000_and_pagination_cap_are_real(tmp_path):
    body = page("".join(f"<p>Paragraph {i:04d} has enough readable text to survive filtering.</p>" for i in range(5000)))
    target = engine(tmp_path, lambda url, domains: (body, url, "text/html"))
    first = target.execute({"open": URL, "max_lines": 1000})
    assert len(first["page"]["lines"]) <= 400
    cached = target.reader_cache.get(URL, now=time.time())
    assert len(cached["blocks"]) == 4000 and cached["blocks_total"] == 5001
    last = target.execute({"open": first["page"]["ref_id"], "start_line": first["page"]["total_lines"], "max_lines": 1})
    assert texts(last) == ["[Extraction stopped at 4000 blocks]"]


def test_fetch_and_extraction_truncation_notices_match_live_and_disk_cache(tmp_path):
    body = page(("<p>" + "Readable content " * 25 + "</p>") * 6000)
    assert len(body) > 2 * 1024 * 1024
    calls = []
    def fetch(url, domains):
        calls.append(url)
        return body, url, "text/html"
    target = engine(tmp_path, fetch)
    first = target.execute({"open": URL, "max_lines": 1})
    args = {"open": URL, "start_line": first["page"]["total_lines"] - 1, "max_lines": 2}
    expected = ["[Page truncated at 2 MiB]", "[Extraction stopped at 4000 blocks]"]
    assert texts(target.execute(args)) == expected
    cached = engine(tmp_path, fetch).execute(args)
    assert texts(cached) == expected and cached["page"]["cached"] is True
    assert calls == [URL]
