"""Daily recap: grouping, PDF rendering, email composition, and sending."""
from __future__ import annotations

import re

import pytest
from fastapi.testclient import TestClient

from rynmesh.peer_http import create_app
from rynmesh.services.recap import (
    RecapConfig,
    RecapError,
    build_recap,
    compose_email,
    pdf_available,
    render_pdf,
    send_email,
)
from rynmesh.store import RynmeshStore

NOW = 1_800_000_000.0


def _digest(n_per_source=6):
    items = []
    for source, kind in (("Hacker News", "report"), ("YouTube · Chan", "video"),
                         ("NASA Podcast", "audio"), ("xkcd", "image")):
        for i in range(n_per_source):
            items.append({
                "item_id": f"{source}-{i}", "source_id": source, "source_title": source,
                "source_kind": "news", "content_kind": kind,
                "title": f"{source} item {i}", "link": f"https://ex.com/{source}/{i}".replace(" ", ""),
                "summary": "A summary.", "ai_summary": f"AI take on {source} {i}",
                "score": 1.0 - i * 0.1, "thumbnail": "", "media_url": "",
                "published_unix": NOW, "reasons": [], "tags": [],
            })
    return {"items": items, "brief": "- First bullet\n- Second bullet"}


# --------------------------------------------------------------- grouping ----

def test_caps_items_per_source_and_orders_by_best_score():
    recap = build_recap(_digest(), per_source=4, now_unix=NOW)
    assert recap["source_count"] == 4
    assert all(len(s["items"]) == 4 for s in recap["sections"])
    assert recap["item_count"] == 16
    # strongest section leads
    tops = [max(float(i["score"]) for i in s["items"]) for s in recap["sections"]]
    assert tops == sorted(tops, reverse=True)


def test_per_source_is_clamped_to_the_documented_range():
    assert all(len(s["items"]) <= 5 for s in build_recap(_digest(), per_source=99, now_unix=NOW)["sections"])
    assert all(len(s["items"]) == 1 for s in build_recap(_digest(), per_source=0, now_unix=NOW)["sections"])


def test_every_content_kind_survives_into_the_recap():
    kinds = {s["content_kind"] for s in build_recap(_digest(), now_unix=NOW)["sections"]}
    assert kinds == {"report", "video", "audio", "image"}


# -------------------------------------------------------------------- pdf ----

@pytest.mark.skipif(not pdf_available(), reason="reportlab not installed")
def test_pdf_is_valid_and_links_every_item():
    recap = build_recap(_digest(), per_source=4, now_unix=NOW)
    pdf = render_pdf(recap, base_url="https://me.rynmesh.ai", node_name="MS-1")
    assert pdf.startswith(b"%PDF-") and pdf.rstrip().endswith(b"%%EOF")
    uris = {m.decode("latin-1") for m in re.findall(rb"/URI \(([^)]+)\)", pdf)}
    # every item's own link, plus the node link
    assert "https://ex.com/HackerNews/0" in uris
    assert "https://me.rynmesh.ai/digest" in uris


@pytest.mark.skipif(not pdf_available(), reason="reportlab not installed")
def test_pdf_survives_non_latin1_titles():
    # Base PDF fonts are Latin-1; smart quotes and CJK must not raise.
    digest = _digest(1)
    digest["items"][0]["title"] = "“Smart” quotes — em dash … 한국어 テスト"
    pdf = render_pdf(build_recap(digest, now_unix=NOW), base_url="http://x")
    assert pdf.startswith(b"%PDF-")


def test_render_returns_empty_without_reportlab(monkeypatch):
    import rynmesh.services.recap as module

    monkeypatch.setattr(module, "pdf_available", lambda: False)
    assert module.render_pdf(build_recap(_digest(), now_unix=NOW), base_url="http://x") == b""


# ------------------------------------------------------------------ email ----

def _config(**over):
    base = dict(to_address="me@example.com", from_address="node@example.com",
                smtp_host="smtp.example.com", smtp_port=587, smtp_user="u",
                smtp_password="p", base_url="https://me.rynmesh.ai")
    base.update(over)
    return RecapConfig(**base)


def test_email_has_text_html_and_pdf_attachment():
    recap = build_recap(_digest(), per_source=3, now_unix=NOW)
    message = compose_email(recap, config=_config(), pdf=b"%PDF-1.4 fake", node_name="MS-1")
    assert message["To"] == "me@example.com"
    assert "Ryn recap" in message["Subject"]
    types = {part.get_content_type() for part in message.walk()}
    assert {"text/plain", "text/html", "application/pdf"} <= types
    attachment = next(p for p in message.walk() if p.get_content_type() == "application/pdf")
    assert attachment.get_filename().startswith("ryn-recap-")
    html = next(p for p in message.walk() if p.get_content_type() == "text/html")
    body = html.get_content()
    assert "https://me.rynmesh.ai/digest" in body      # link back to the node


def test_email_without_a_pdf_still_sends():
    message = compose_email(build_recap(_digest(), now_unix=NOW), config=_config(),
                            pdf=b"", node_name="")
    assert "application/pdf" not in {p.get_content_type() for p in message.walk()}


def test_html_escapes_item_titles():
    digest = _digest(1)
    digest["items"][0]["title"] = '<script>alert("x")</script>'
    message = compose_email(build_recap(digest, now_unix=NOW), config=_config(), pdf=b"")
    body = next(p for p in message.walk() if p.get_content_type() == "text/html").get_content()
    assert "<script>alert" not in body and "&lt;script&gt;" in body


class _FakeSMTP:
    instances: list["_FakeSMTP"] = []

    def __init__(self, host, port, timeout=None):
        self.host, self.port = host, port
        self.started_tls = False
        self.logged_in = None
        self.sent = None
        _FakeSMTP.instances.append(self)

    def __enter__(self): return self
    def __exit__(self, *exc): return False
    def starttls(self, context=None): self.started_tls = True
    def login(self, user, password): self.logged_in = (user, password)
    def send_message(self, message): self.sent = message


def test_send_uses_starttls_and_login():
    _FakeSMTP.instances.clear()
    message = compose_email(build_recap(_digest(), now_unix=NOW), config=_config(), pdf=b"")
    result = send_email(message, config=_config(), smtp_factory=_FakeSMTP)
    server = _FakeSMTP.instances[-1]
    assert result["ok"] and server.started_tls and server.logged_in == ("u", "p")
    assert server.sent is message


def test_send_requires_configuration():
    message = compose_email(build_recap(_digest(), now_unix=NOW), config=_config(), pdf=b"")
    with pytest.raises(RecapError, match="smtp_not_configured"):
        send_email(message, config=_config(smtp_host=""), smtp_factory=_FakeSMTP)
    with pytest.raises(RecapError, match="recipient_missing"):
        send_email(message, config=_config(to_address=""), smtp_factory=_FakeSMTP)


# -------------------------------------------------------------- endpoints ----

def _client(tmp_path, monkeypatch):
    monkeypatch.setenv("RYNMESH_HOME", str(tmp_path / "node"))
    monkeypatch.setenv("RYNMESH_AUTO_REGISTER", "0")
    monkeypatch.setenv("RYNMESH_ALLOW_REMOTE_CONTROL", "1")
    monkeypatch.setenv("RYNMESH_MODEL_PROVIDER", "none")
    monkeypatch.setenv("RYNMESH_DISABLE_DISCOVERY", "1")
    return TestClient(create_app(RynmeshStore()))


def test_settings_round_trip_never_echoes_the_password(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    client.patch("/api/local/recap/settings", json={
        "to_address": "me@example.com", "smtp_host": "smtp.example.com",
        "smtp_password": "hunter2", "per_source": 5, "enabled": True,
    })
    body = client.get("/api/local/recap/settings").json()
    assert body["to_address"] == "me@example.com"
    assert body["per_source"] == 5 and body["enabled"] is True
    assert body["password_set"] is True
    assert "smtp_password" not in body and "hunter2" not in str(body)


def test_send_without_smtp_is_a_clear_400(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    response = client.post("/api/local/recap/send")
    assert response.status_code == 400
    assert "smtp_not_configured" in response.json()["detail"]


def test_partial_settings_patch_keeps_the_stored_password(tmp_path, monkeypatch):
    # A UI that saves only the recipient must not wipe SMTP credentials.
    client = _client(tmp_path, monkeypatch)
    client.patch("/api/local/recap/settings",
                 json={"smtp_host": "smtp.example.com", "smtp_password": "hunter2"})
    client.patch("/api/local/recap/settings", json={"to_address": "me@example.com"})
    body = client.get("/api/local/recap/settings").json()
    assert body["smtp_host"] == "smtp.example.com"
    assert body["password_set"] is True
    assert body["to_address"] == "me@example.com"
