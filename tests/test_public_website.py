from __future__ import annotations

from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urlparse

from rynmesh import __version__

ROOT = Path(__file__).resolve().parents[1]
WEBSITE = ROOT / "website"


class PageParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.links: list[str] = []
        self.images: list[str] = []
        self.titles: list[str] = []
        self._in_title = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attributes = dict(attrs)
        if tag == "a" and attributes.get("href"):
            self.links.append(attributes["href"] or "")
        if tag == "img" and attributes.get("src"):
            self.images.append(attributes["src"] or "")
        if tag == "title":
            self._in_title = True

    def handle_endtag(self, tag: str) -> None:
        if tag == "title":
            self._in_title = False

    def handle_data(self, data: str) -> None:
        if self._in_title and data.strip():
            self.titles.append(data.strip())


def html_pages() -> list[Path]:
    return sorted(WEBSITE.rglob("*.html"))


def local_target(page: Path, value: str) -> Path | None:
    parsed = urlparse(value)
    if parsed.scheme or parsed.netloc or value.startswith("mailto:"):
        return None
    path = parsed.path
    if not path or path.startswith("#"):
        return None
    if path.startswith("/"):
        target = WEBSITE / path.lstrip("/")
    else:
        target = page.parent / path
    if path.endswith("/"):
        target /= "index.html"
    return target.resolve()


def test_public_website_has_expected_entry_points() -> None:
    expected = [
        "index.html",
        "features/index.html",
        "status/index.html",
        "download/index.html",
        "contribute/index.html",
        "contribute/start/index.html",
        "contribute/task/index.html",
    ]
    for relative in expected:
        assert (WEBSITE / relative).is_file(), relative


def test_internal_links_and_images_resolve() -> None:
    failures: list[str] = []
    for page in html_pages():
        parser = PageParser()
        parser.feed(page.read_text(encoding="utf-8"))
        assert parser.titles, page
        for value in parser.links + parser.images:
            target = local_target(page, value)
            if target is not None and not target.exists():
                failures.append(f"{page.relative_to(ROOT)} -> {value}")
    assert not failures, "\n".join(failures)


def test_site_contains_no_stitch_placeholders_or_fabricated_claims() -> None:
    source = "\n".join(page.read_text(encoding="utf-8") for page in html_pages())
    forbidden = [
        'href="#"',
        "0xNull",
        "CipherNode",
        "Vectura",
        "HashGate",
        "@alex_dev",
        "@crypto_node",
        "github.com/rynmesh/ryn",
        "cryptographic multisig",
        "48-hour SLA",
        "© 2024",
    ]
    for value in forbidden:
        assert value not in source


def test_current_source_version_is_presented_accurately() -> None:
    home = (WEBSITE / "index.html").read_text(encoding="utf-8")
    assert f"v{__version__}" in home


def test_warm_ivory_palette_is_the_site_default() -> None:
    stylesheet = (WEBSITE / "assets/site.css").read_text(encoding="utf-8").lower()
    assert "color-scheme: light" in stylesheet
    assert "--bg: #f8f7f1" in stylesheet
    assert "--surface: #ffffff" in stylesheet
    assert "--ink: #1d2922" in stylesheet
    assert "--muted: #68736b" in stylesheet
    assert "--green: #19734b" in stylesheet


def test_public_pages_are_indexable() -> None:
    for page in html_pages():
        if page == WEBSITE / "contribute/task/index.html":
            continue
        source = page.read_text(encoding="utf-8").lower()
        assert 'content="noindex' not in source, page
