"""Tests for S05-I19: NAS 6000-entry directory pagination (T1) and WebDAV fault tolerance / percent-encoding (T4, T5, T6)."""
from __future__ import annotations

from types import SimpleNamespace
import httpx
import pytest

from rynmesh.agent_runtime.tools import RynTools
from rynmesh.nas import NasError, NasPlugin, SMBConnector, WebDAVConnector, relative_path


def _smb_config(**kwargs) -> dict:
    return {
        "name": "Archive NAS",
        "system": "fnos",
        "protocol": "smb",
        "host": "nas.example.test",
        "share": "archive",
        "port": 445,
        "root": "",
        "username": "archiver",
        "password": "secret-password",
        "writable": False,
        "allow_ai": True,
        **kwargs,
    }


def test_t1_6000_entries_paginates_completely_without_duplicates(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    """T1: A 6000-entry directory is fetched completely across 500-item sorted pages without duplicates on both SMB and WebDAV (and through ResourceTools)."""
    total_count = 6000
    expected_names = [f"item_{idx:04d}.txt" for idx in range(total_count)]
    # Return entries in reverse order from the underlying server to verify name-sorted cursor pagination
    raw_names = list(reversed(expected_names))

    # 1) SMBConnector.list_page over 6000 items
    import smbclient

    monkeypatch.setattr(smbclient, "register_session", lambda *a, **kw: None)
    monkeypatch.setattr(smbclient, "reset_connection_cache", lambda **kw: None)
    monkeypatch.setattr(
        smbclient,
        "scandir",
        lambda *a, **kw: [
            SimpleNamespace(name=name, path=rf"\\nas.example.test\archive\{name}") for name in raw_names
        ],
    )
    monkeypatch.setattr(
        smbclient,
        "stat",
        lambda _p, **_kw: SimpleNamespace(st_file_attributes=0x20, st_size=12, st_mtime=100),
    )

    smb = SMBConnector(_smb_config())
    with pytest.raises(NasError, match="directory_too_large"):
        smb.list("")

    smb_collected: list[str] = []
    cursor = ""
    pages = 0
    while True:
        page = smb.list_page("", cursor)
        pages += 1
        assert len(page["entries"]) <= 500
        smb_collected.extend(entry["name"] for entry in page["entries"])
        cursor = page["next_cursor"]
        if not cursor:
            break
    assert pages == 12
    assert len(smb_collected) == total_count
    assert len(set(smb_collected)) == total_count
    assert smb_collected == expected_names

    # 2) WebDAVConnector.list_page + NasPlugin + ResourceTools over 6000 items
    xml_items = [
        '<d:response><d:href>/dav/</d:href><d:propstat><d:prop><d:resourcetype><d:collection/></d:resourcetype></d:prop></d:propstat></d:response>'
    ]
    for name in raw_names:
        xml_items.append(
            f"<d:response><d:href>/dav/{name}</d:href>"
            "<d:propstat><d:prop><d:getcontentlength>7</d:getcontentlength></d:prop></d:propstat></d:response>"
        )
    xml_body = ('<d:multistatus xmlns:d="DAV:">' + "".join(xml_items) + "</d:multistatus>").encode("utf-8")

    def make_dav(cfg: dict) -> WebDAVConnector:
        conn = WebDAVConnector(cfg)
        conn.client.close()
        conn.client = httpx.Client(
            transport=httpx.MockTransport(lambda _req: httpx.Response(207, content=xml_body)),
            auth=(cfg["username"], cfg["password"]),
            follow_redirects=False,
        )
        return conn

    plugin = NasPlugin(tmp_path / "nas_home")
    plugin.connectors["webdav"] = make_dav
    plugin.enable(True)
    # Bypass configure's connector.list("") 5000-item UI check by adding source after initial empty check or testing operate("list_page") directly
    init_body = [b'<d:multistatus xmlns:d="DAV:"><d:response><d:href>/dav/</d:href><d:propstat><d:prop><d:resourcetype><d:collection/></d:resourcetype></d:prop></d:propstat></d:response></d:multistatus>']

    def make_configurable_dav(cfg: dict) -> WebDAVConnector:
        conn = WebDAVConnector(cfg)
        conn.client.close()
        conn.client = httpx.Client(
            transport=httpx.MockTransport(lambda _req: httpx.Response(207, content=init_body[0])),
            auth=(cfg["username"], cfg["password"]),
            follow_redirects=False,
        )
        return conn

    plugin.connectors["webdav"] = make_configurable_dav
    source = plugin.configure(
        {
            "name": "Big WebDAV",
            "system": "generic",
            "protocol": "webdav",
            "url": "http://dav.example.test/dav",
            "username": "u",
            "password": "p",
            "allow_ai": True,
            "writable": False,
        }
    )
    init_body[0] = xml_body

    dav_collected: list[str] = []
    cursor = ""
    dav_pages = 0
    while True:
        page = plugin.operate(source["id"], "list_page", "", cursor)
        dav_pages += 1
        assert len(page["entries"]) <= 500
        dav_collected.extend(entry["name"] for entry in page["entries"])
        cursor = page["next_cursor"]
        if not cursor:
            break
    assert dav_pages == 12
    assert dav_collected == expected_names

    # Verify agent ResourceTools can list the 6000-entry directory without directory_too_large
    tools = RynTools(SimpleNamespace(state=SimpleNamespace(nas_plugin=plugin)), SimpleNamespace(), {}, None)
    destination = {
        "catalog_model_id": "m1",
        "provider_peer_id": "p1",
        "source_id": "s1",
        "destination_revision": "r1",
    }
    scope = tools.prepare_scope(
        {
            "resources": [{"connection_id": source["id"], "root_path": "", "actions": ["list", "read_ai"]}],
            "allow_model_data": True,
        },
        owner_id="owner",
        consumer_node_id="node",
        model_destination=destination,
    )
    ctx = {
        **scope,
        "task_id": "t-6000",
        "scope_revision": 1,
        "owner_id": "owner",
        "consumer_node_id": "node",
        "model_destination": destination,
        "resource_origins": [],
    }
    root_ref = scope["resource_grants"][0]["root_resource_ref"]
    first_agent_page = tools.execute(
        "ryn_list_resources",
        {"resource_ref": root_ref},
        operation_id="op-list-1",
        context=ctx,
        cancelled=lambda: False,
    )
    assert [e["name"] for e in first_agent_page["entries"]] == expected_names[:10]
    assert first_agent_page["truncated"] is True
    assert first_agent_page["next_cursor"]


def test_t4_webdav_skips_bad_href_and_defaults_empty_contentlength_to_zero(tmp_path) -> None:
    """T4 (G05-38): A WebDAV multistatus response with ':' / control-char href and empty getcontentlength skips only the bad entry (skipped=1) and sets empty size to 0."""
    multistatus = b"""<d:multistatus xmlns:d="DAV:">
      <d:response>
        <d:href>/dav/work/</d:href>
        <d:propstat><d:prop><d:resourcetype><d:collection/></d:resourcetype></d:prop></d:propstat>
      </d:response>
      <d:response>
        <d:href>/dav/work/bad:name%07.txt</d:href>
        <d:propstat><d:prop><d:getcontentlength>10</d:getcontentlength></d:prop></d:propstat>
      </d:response>
      <d:response>
        <d:href>/dav/work/empty_length.txt</d:href>
        <d:propstat><d:prop><d:getcontentlength></d:getcontentlength></d:prop></d:propstat>
      </d:response>
      <d:response>
        <d:href>/dav/work/a%2520b.txt</d:href>
        <d:propstat><d:prop><d:getcontentlength>25</d:getcontentlength></d:prop></d:propstat>
      </d:response>
    </d:multistatus>"""

    def make_dav(cfg: dict) -> WebDAVConnector:
        conn = WebDAVConnector(cfg)
        conn.client.close()
        conn.client = httpx.Client(
            transport=httpx.MockTransport(lambda _req: httpx.Response(207, content=multistatus)),
            auth=(cfg["username"], cfg["password"]),
            follow_redirects=False,
        )
        return conn

    plugin = NasPlugin(tmp_path / "t4_home")
    plugin.connectors["webdav"] = make_dav
    plugin.enable(True)
    source = plugin.configure(
        {
            "name": "Fault Tolerant DAV",
            "system": "generic",
            "protocol": "webdav",
            "url": "http://dav.example.test/dav",
            "root": "work",
            "username": "u",
            "password": "p",
            "allow_ai": True,
            "writable": False,
        }
    )

    direct_conn = make_dav(
        {"url": "http://dav.example.test/dav", "root": "work", "username": "u", "password": "p"}
    )
    try:
        items = direct_conn.list("")
        assert isinstance(items, list)
        assert direct_conn.last_skipped == 1
        by_name = {item["name"]: item for item in items}
        # Single unquote: /dav/work/a%2520b.txt -> a%20b.txt (never double-decoded to 'a b.txt')
        assert set(by_name) == {"empty_length.txt", "a%20b.txt"}
        assert by_name["empty_length.txt"]["size"] == 0
        assert by_name["a%20b.txt"]["size"] == 25
    finally:
        direct_conn.close()

    page = plugin.operate(source["id"], "list_page", "")
    assert page["skipped"] == 1
    assert [e["name"] for e in page["entries"]] == ["a%20b.txt", "empty_length.txt"]

    ui_listing = plugin.listing(source["id"])
    assert ui_listing["skipped"] == 1


def test_t5_percent_in_filename_downloads_with_percent25_encoded_url(tmp_path) -> None:
    """T5: Downloading a file named 'a%20b.txt' encodes '%' to '%25' so the request URL contains '%2520'."""
    seen_urls: list[str] = []

    def respond(request: httpx.Request) -> httpx.Response:
        seen_urls.append(str(request.url))
        if request.method == "PROPFIND":
            return httpx.Response(
                207,
                content=b'<d:multistatus xmlns:d="DAV:"><d:response><d:href>/dav/work/</d:href><d:propstat><d:prop><d:resourcetype><d:collection/></d:resourcetype></d:prop></d:propstat></d:response></d:multistatus>',
            )
        assert request.method == "GET"
        return httpx.Response(200, content=b"literal-percent-content")

    def make_dav(cfg: dict) -> WebDAVConnector:
        conn = WebDAVConnector(cfg)
        conn.client.close()
        conn.client = httpx.Client(
            transport=httpx.MockTransport(respond),
            auth=(cfg["username"], cfg["password"]),
            follow_redirects=False,
        )
        return conn

    plugin = NasPlugin(tmp_path / "t5_home")
    plugin.connectors["webdav"] = make_dav
    plugin.enable(True)
    source = plugin.configure(
        {
            "name": "Percent DAV",
            "system": "generic",
            "protocol": "webdav",
            "url": "http://dav.example.test/dav",
            "root": "work",
            "username": "u",
            "password": "p",
            "allow_ai": True,
            "writable": False,
        }
    )
    seen_urls.clear()
    downloaded = plugin.operate(source["id"], "read", "a%20b.txt")
    assert downloaded == b"literal-percent-content"
    assert len(seen_urls) == 1
    assert seen_urls[0].endswith("/dav/work/a%2520b.txt")


def test_t6_dotdot_traversal_paths_still_rejected() -> None:
    """T6: Paths containing '..' are rejected by both relative_path and WebDAVConnector.url."""
    conn = WebDAVConnector(
        {"url": "http://dav.example.test/dav", "root": "work", "username": "u", "password": "p"}
    )
    try:
        for bad in ("..", "../secret", "dir/../secret", "a/b/.."):
            with pytest.raises(NasError, match="invalid_path"):
                relative_path(bad)
            with pytest.raises(NasError, match="invalid_path"):
                conn.url(bad)
    finally:
        conn.close()
