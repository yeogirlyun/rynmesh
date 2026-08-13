"""Daily recap: a PDF digest by email.

Groups the day's digest into the top few items per source, renders a
typographic PDF, and sends it as an attachment with an HTML body whose links
point back at the reader's own node.

Every link is built from a single base URL so the same recap works whether the
node is reached at http://127.0.0.1:8791 (this machine) or at a personal
hostname (remote access). Nothing here assumes a tunnel exists.
"""
from __future__ import annotations

import os
import smtplib
import ssl
from dataclasses import dataclass
from datetime import UTC, datetime
from email.message import EmailMessage
from typing import Any, Callable, Mapping, Sequence

__all__ = ["RecapError", "build_recap", "render_pdf", "compose_email", "send_email",
           "pdf_available"]

PER_SOURCE_DEFAULT = 4
PER_SOURCE_MAX = 5

INK = (0.09, 0.12, 0.17)
RULE = (0.85, 0.88, 0.92)
HEADER_BG = (0.055, 0.083, 0.149)
HEADER_SUB = (0.72, 0.78, 0.87)
MUTED = (0.42, 0.47, 0.55)
GREEN = (0.00, 0.60, 0.42)
WHITE = (1.0, 1.0, 1.0)

KIND_LABEL = {
    "video": "Watch",
    "audio": "Listen",
    "image": "View",
    "report": "Read",
    "document": "Read",
}


class RecapError(RuntimeError):
    pass


@dataclass
class RecapConfig:
    to_address: str = ""
    from_address: str = ""
    smtp_host: str = ""
    smtp_port: int = 587
    smtp_user: str = ""
    smtp_password: str = ""
    use_tls: bool = True
    base_url: str = ""
    per_source: int = PER_SOURCE_DEFAULT

    @classmethod
    def from_settings(cls, settings: Mapping[str, Any], *, port: int) -> "RecapConfig":
        raw = dict(settings.get("recap", {}) or {})
        return cls(
            to_address=str(raw.get("to_address", "")),
            from_address=str(raw.get("from_address", "") or raw.get("smtp_user", "")),
            smtp_host=str(raw.get("smtp_host", "")),
            smtp_port=int(raw.get("smtp_port", 587) or 587),
            smtp_user=str(raw.get("smtp_user", "")),
            smtp_password=str(raw.get("smtp_password", "")),
            use_tls=bool(raw.get("use_tls", True)),
            base_url=str(raw.get("base_url", "") or f"http://127.0.0.1:{port}"),
            per_source=max(1, min(PER_SOURCE_MAX, int(raw.get("per_source", PER_SOURCE_DEFAULT) or PER_SOURCE_DEFAULT))),
        )


def build_recap(
    digest: Mapping[str, Any], *, per_source: int = PER_SOURCE_DEFAULT, now_unix: float
) -> dict[str, Any]:
    """Top N items per source, strongest first, with the day's briefing."""
    per_source = max(1, min(PER_SOURCE_MAX, per_source))
    groups: dict[str, dict[str, Any]] = {}
    for item in digest.get("items", []):
        key = str(item.get("source_id") or item.get("source_title", ""))
        group = groups.setdefault(
            key,
            {
                "source_title": str(item.get("source_title", "")),
                "source_kind": str(item.get("source_kind", "")),
                "content_kind": str(item.get("content_kind", "document")),
                "items": [],
            },
        )
        if len(group["items"]) < per_source:
            group["items"].append(item)

    # Strongest section first, so the recap opens on the day's best material.
    sections = sorted(
        groups.values(),
        key=lambda g: -max((float(i.get("score", 0)) for i in g["items"]), default=0.0),
    )
    return {
        "generated_at_unix": float(now_unix),
        "date": datetime.fromtimestamp(now_unix, UTC).strftime("%A, %d %B %Y"),
        "brief": str(digest.get("brief", "")),
        "sections": sections,
        "item_count": sum(len(s["items"]) for s in sections),
        "source_count": len(sections),
    }


def pdf_available() -> bool:
    """reportlab is optional: without it the recap still sends, just no PDF."""
    try:
        import reportlab  # noqa: F401
    except ImportError:
        return False
    return True


def _sanitize(text: str) -> str:
    """Base PDF fonts are Latin-1; anything outside it renders as a tofu box.

    Map the typography that actually shows up in feed titles, then drop the
    rest rather than emitting boxes. (Same lesson as clawpad's invoice PDFs.)
    """
    for bad, good in (
        ("\u2018", "'"), ("\u2019", "'"), ("\u201c", '"'), ("\u201d", '"'),
        ("\u2013", "-"), ("\u2014", "-"), ("\u2026", "..."), ("\u2022", "-"),
        ("\u00a0", " "), ("\u2192", "->"), ("\u00ab", '"'), ("\u00bb", '"'),
    ):
        text = text.replace(bad, good)
    return text.encode("latin-1", "ignore").decode("latin-1")


def _wrap(canvas_obj: Any, text: str, font: str, size: float, width: float) -> list[str]:
    """Wrap using reportlab's real font metrics, not a character estimate."""
    words = _sanitize(text).split()
    lines: list[str] = []
    current = ""
    for word in words:
        candidate = f"{current} {word}".strip()
        if canvas_obj.stringWidth(candidate, font, size) <= width or not current:
            current = candidate
        else:
            lines.append(current)
            current = word
    if current:
        lines.append(current)
    return lines or [""]


def render_pdf(recap: Mapping[str, Any], *, base_url: str, node_name: str = "") -> bytes:
    """A typographic recap. Returns b"" when reportlab isn't installed."""
    if not pdf_available():
        return b""
    import io

    from reportlab.lib.pagesizes import A4
    from reportlab.pdfgen import canvas as pdf_canvas

    buffer = io.BytesIO()
    canvas_obj = pdf_canvas.Canvas(buffer, pagesize=A4)
    canvas_obj.setTitle(f"Ryn Daily Recap - {recap['date']}")
    canvas_obj.setAuthor("Ryn")
    width, height = A4
    margin = 54.0
    content_width = width - 2 * margin
    base = base_url.rstrip("/")
    reader_url = f"{base}/digest"

    state = {"y": height}

    def ensure(space: float) -> None:
        if state["y"] - space < margin:
            canvas_obj.showPage()
            state["y"] = height - margin

    def line(text: str, font: str, size: float, color: tuple[float, float, float],
             leading: float, *, indent: float = 0.0, link: str = "", after: float = 0.0) -> None:
        for chunk in _wrap(canvas_obj, text, font, size, content_width - indent):
            ensure(leading)
            canvas_obj.setFont(font, size)
            canvas_obj.setFillColorRGB(*color)
            baseline = state["y"] - size
            canvas_obj.drawString(margin + indent, baseline, chunk)
            if link:
                canvas_obj.linkURL(
                    link,
                    (margin + indent, baseline - 2,
                     margin + indent + canvas_obj.stringWidth(chunk, font, size), baseline + size),
                    relative=0, thickness=0,
                )
            state["y"] -= leading
        state["y"] -= after

    def rule(after: float = 11.0) -> None:
        ensure(6)
        canvas_obj.setStrokeColorRGB(*RULE)
        canvas_obj.setLineWidth(0.7)
        canvas_obj.line(margin, state["y"], width - margin, state["y"])
        state["y"] -= after

    # header band
    canvas_obj.setFillColorRGB(*HEADER_BG)
    canvas_obj.rect(0, height - 104, width, 104, stroke=0, fill=1)
    state["y"] = height - 30
    line("RYN - DAILY RECAP", "Helvetica-Bold", 9, GREEN, 13)
    line(str(recap["date"]), "Helvetica-Bold", 20, WHITE, 25)
    subtitle = f"{recap['item_count']} picks from {recap['source_count']} sources"
    if node_name:
        subtitle += f"  -  {node_name}"
    line(subtitle, "Helvetica", 9.5, HEADER_SUB, 14)
    # Start the body clear of the band; relying on accumulated leading let the
    # first heading straddle the band's edge.
    state["y"] = height - 104 - 26

    if recap.get("brief"):
        line("TODAY'S BRIEFING", "Helvetica-Bold", 8.5, GREEN, 13, after=3)
        for raw in str(recap["brief"]).splitlines():
            entry = raw.strip().lstrip("-*\u2022").strip()
            if entry:
                line(entry, "Helvetica", 10, INK, 14.5, indent=12, after=2)
        rule()

    for section in recap["sections"]:
        ensure(78)  # never orphan a heading from its first item
        line(str(section["source_title"]).upper(), "Helvetica-Bold", 9, GREEN, 14, after=3)
        for item in section["items"]:
            kind = str(item.get("content_kind", "document"))
            link = str(item.get("link", ""))
            line(str(item.get("title", "")), "Helvetica-Bold", 11, INK, 14.5, link=link, after=1)
            summary = str(item.get("ai_summary") or item.get("summary") or "")
            if summary:
                line(summary[:260], "Helvetica", 9.5, MUTED, 13, after=1)
            line(f"{KIND_LABEL.get(kind, 'Open')} on your node", "Helvetica-Oblique", 8.5,
                 GREEN, 12, link=reader_url, after=8)
        rule()

    line("Open your node to read, watch, or listen - and tell Ryn what you want more of.",
         "Helvetica", 9, MUTED, 12.5, after=2)
    line(reader_url, "Helvetica-Bold", 9, GREEN, 12, link=reader_url)

    canvas_obj.showPage()
    canvas_obj.save()
    return buffer.getvalue()


def compose_email(
    recap: Mapping[str, Any], *, config: RecapConfig, pdf: bytes, node_name: str = ""
) -> EmailMessage:
    message = EmailMessage()
    message["Subject"] = f"Ryn recap — {recap['date']}"
    message["From"] = config.from_address or config.smtp_user
    message["To"] = config.to_address
    base = config.base_url.rstrip("/")

    lines = [f"Ryn Daily Recap — {recap['date']}", ""]
    if recap.get("brief"):
        lines += [recap["brief"], ""]
    for section in recap["sections"]:
        lines.append(section["source_title"])
        for item in section["items"]:
            lines.append(f"  - {item.get('title','')}  {item.get('link','')}")
        lines.append("")
    lines.append(f"Open your node: {base}/digest")
    message.set_content("\n".join(lines))

    rows = []
    for section in recap["sections"]:
        entries = "".join(
            f'<tr><td style="padding:6px 0 10px">'
            f'<a href="{_esc(str(item.get("link","")))}" '
            f'style="color:#0f1726;font:600 15px/1.35 -apple-system,Segoe UI,sans-serif;'
            f'text-decoration:none">{_esc(str(item.get("title","")))}</a>'
            f'<div style="color:#5c6b7f;font:400 13px/1.5 -apple-system,Segoe UI,sans-serif;'
            f'margin-top:3px">{_esc(str(item.get("ai_summary") or item.get("summary") or "")[:200])}</div>'
            f"</td></tr>"
            for item in section["items"]
        )
        rows.append(
            f'<tr><td style="padding:18px 0 4px;color:#00996b;'
            f'font:700 11px/1 -apple-system,Segoe UI,sans-serif;letter-spacing:.08em">'
            f'{_esc(str(section["source_title"]).upper())}</td></tr>'
            f'<tr><td><table width="100%" cellpadding="0" cellspacing="0">{entries}</table></td></tr>'
        )

    brief_html = ""
    if recap.get("brief"):
        items = "".join(
            f"<li style='margin:0 0 6px'>{_esc(line.strip().lstrip('-•').strip())}</li>"
            for line in str(recap["brief"]).splitlines() if line.strip()
        )
        brief_html = (
            '<tr><td style="padding:16px 0 6px"><ul style="margin:0;padding-left:18px;'
            'color:#0f1726;font:400 14px/1.55 -apple-system,Segoe UI,sans-serif">'
            f"{items}</ul></td></tr>"
        )

    message.add_alternative(
        f"""<html><body style="margin:0;background:#f5f7fa;padding:24px">
<table width="100%" cellpadding="0" cellspacing="0"><tr><td align="center">
<table width="600" cellpadding="0" cellspacing="0"
       style="background:#fff;border-radius:14px;overflow:hidden">
  <tr><td style="background:#0e1526;padding:26px 30px">
    <div style="color:#00d084;font:700 10px/1 -apple-system,Segoe UI,sans-serif;
                letter-spacing:.14em">RYN · DAILY RECAP</div>
    <div style="color:#fff;font:700 22px/1.25 -apple-system,Segoe UI,sans-serif;
                margin-top:8px">{_esc(str(recap["date"]))}</div>
    <div style="color:#b8c6da;font:400 13px/1.5 -apple-system,Segoe UI,sans-serif;
                margin-top:6px">{recap["item_count"]} picks from {recap["source_count"]} sources
      {(" · " + _esc(node_name)) if node_name else ""}</div>
  </td></tr>
  <tr><td style="padding:0 30px 26px">
    <table width="100%" cellpadding="0" cellspacing="0">{brief_html}{"".join(rows)}</table>
    <div style="margin-top:24px;text-align:center">
      <a href="{_esc(base)}/digest"
         style="display:inline-block;background:#00996b;color:#fff;text-decoration:none;
                padding:12px 26px;border-radius:8px;
                font:600 14px/1 -apple-system,Segoe UI,sans-serif">Open your Ryn node</a>
    </div>
    <div style="margin-top:14px;color:#8595a8;text-align:center;
                font:400 11.5px/1.6 -apple-system,Segoe UI,sans-serif">
      The full recap is attached as a PDF. Everything was gathered and ranked on your own
      machine.
    </div>
  </td></tr>
</table></td></tr></table></body></html>""",
        subtype="html",
    )
    if pdf:
        stamp = datetime.fromtimestamp(recap["generated_at_unix"], UTC).strftime("%Y-%m-%d")
        message.add_attachment(
            pdf, maintype="application", subtype="pdf", filename=f"ryn-recap-{stamp}.pdf"
        )
    return message


def _esc(text: str) -> str:
    return (
        text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")
    )


def send_email(
    message: EmailMessage,
    *,
    config: RecapConfig,
    smtp_factory: Callable[..., Any] | None = None,
) -> dict[str, Any]:
    if not config.smtp_host:
        raise RecapError("recap_smtp_not_configured")
    if not config.to_address:
        raise RecapError("recap_recipient_missing")
    factory = smtp_factory or (
        smtplib.SMTP_SSL if (config.smtp_port == 465 and config.use_tls) else smtplib.SMTP
    )
    try:
        with factory(config.smtp_host, config.smtp_port, timeout=30) as server:
            if config.use_tls and config.smtp_port != 465:
                server.starttls(context=ssl.create_default_context())
            if config.smtp_user:
                server.login(config.smtp_user, config.smtp_password)
            server.send_message(message)
    except RecapError:
        raise
    except Exception as exc:
        raise RecapError(f"recap_send_failed: {exc}") from exc
    return {"ok": True, "to": config.to_address, "subject": message["Subject"]}


def env_default_base_url(port: int) -> str:
    return os.environ.get("RYNMESH_PUBLIC_URL", "").strip() or f"http://127.0.0.1:{port}"


def _sections_are_capped(sections: Sequence[Mapping[str, Any]], cap: int) -> bool:
    return all(len(section["items"]) <= cap for section in sections)
