import json
import time

import httpx
import pytest

from rynmesh import cloud_drives as cloud
from rynmesh.nas import NasError, NasPlugin
from rynmesh.onedrive import OneDriveConnector, transfer_url


def connector(handler):
    value = OneDriveConnector({"protocol": "onedrive", "oauth": {"client_id": "app", "access_token": "private-token", "refresh_token": "refresh", "expires_at": time.time() + 3600}}, lambda: None)
    value._http = httpx.Client(transport=httpx.MockTransport(handler))
    return value


@pytest.mark.parametrize("verification_uri", ["https://microsoft.com/devicelogin", "https://login.microsoft.com/device"])
def test_device_code_polling_encrypted_save_and_refresh_rotation(tmp_path, monkeypatch, verification_uri):
    plugin = NasPlugin(tmp_path)
    plugin.enable(True)
    plugin.cloud.configure("onedrive", {"client_id": "app", "client_secret": "ignored"})
    assert plugin.state["cloud_apps"]["onedrive"]["client_secret"] == ""
    calls = []
    def request(method, url, **kwargs):
        calls.append((method, url, kwargs))
        assert "client_secret" not in kwargs["data"]
        if url.endswith("/devicecode"):
            assert kwargs["data"]["scope"] == "offline_access https://graph.microsoft.com/Files.Read"
            return {"device_code": "private-device", "user_code": "DISPLAY", "verification_uri": verification_uri, "interval": 5, "expires_in": 900}
        if len(calls) == 2:
            return {"error": "slow_down"}
        return {"access_token": "private-access", "refresh_token": "private-refresh", "expires_in": 3600, "scope": "Files.Read"}
    monkeypatch.setattr(cloud, "provider_request", request)
    monkeypatch.setattr(OneDriveConnector, "list", lambda self, path: [])
    auth = plugin.cloud.start("onedrive", {"name": "Microsoft"})
    assert "private-device" not in json.dumps(auth)
    assert plugin.cloud.poll(auth["id"])["state"] == "waiting" and len(calls) == 1
    item = plugin.cloud.pending[auth["id"]]
    item["next_poll"] = 0
    plugin.cloud.poll(auth["id"])
    assert item["interval"] == 10 and item["next_poll"] > time.time() + 9
    item["next_poll"] = 0
    result = plugin.cloud.poll(auth["id"])
    assert result["state"] == "connected"
    assert "private-" not in json.dumps(plugin.status())
    assert b"private-" not in (plugin.directory / "state.enc").read_bytes()
    restored = NasPlugin(tmp_path)
    source = restored.source(result["source_id"])
    source["oauth"]["expires_at"] = 0
    with restored.connection(source) as adapter:
        assert adapter.token() == "private-access"
    assert calls[-1][2]["data"]["grant_type"] == "refresh_token"
    with pytest.raises(NasError, match="reauthorization"):
        restored.update_source(source["id"], {"writable": True})


@pytest.mark.parametrize("url", ["https://login.microsoft.com.evil.example/device", "http://login.microsoft.com/device", "https://user@login.microsoft.com/device", "https://login.microsoft.com:8443/device"])
def test_device_code_rejects_untrusted_verification_url(tmp_path, monkeypatch, url):
    plugin = NasPlugin(tmp_path)
    plugin.enable(True)
    plugin.cloud.configure("onedrive", {"client_id": "app"})
    monkeypatch.setattr(cloud, "provider_request", lambda *args, **kwargs: {
        "device_code": "private-device", "user_code": "DISPLAY", "verification_uri": url,
    })
    with pytest.raises(NasError, match="drive_invalid_authorization_url"):
        plugin.cloud.start("onedrive", {"name": "Microsoft"})


def test_missing_write_consent_and_cancelled_authorization_are_not_saved(tmp_path):
    plugin = NasPlugin(tmp_path)
    plugin.enable(True)
    item = {"provider": "onedrive", "state": "waiting", "expires": time.time() + 300, "writable": True}
    with pytest.raises(NasError, match="permission_missing"):
        plugin.cloud._finish(item, {"access_token": "a", "refresh_token": "r", "scope": "Files.Read"})
    item["state"] = "cancelled"
    with pytest.raises(NasError, match="expired"):
        plugin.cloud._finish(item, {"access_token": "a", "refresh_token": "r", "scope": "Files.ReadWrite"})
    assert not plugin.state["sources"]


def test_graph_listing_pagination_and_cursor_validation():
    calls = []
    def response(request):
        calls.append(request)
        assert request.headers["authorization"] == "Bearer private-token"
        if "$skiptoken" in request.url.params:
            return httpx.Response(200, json={"value": [{"id": "A!2", "name": "doc.txt", "file": {"mimeType": "text/plain"}, "size": 3}]})
        assert request.url.params["$top"] == "200"
        return httpx.Response(200, json={"value": [{"id": "A!1", "name": "Folder", "folder": {}}, {"id": "skip", "name": "Shared", "remoteItem": {}, "folder": {}}], "@odata.nextLink": "https://graph.microsoft.com/v1.0/me/drive/root/children?$skiptoken=next"})
    value = connector(response)
    try:
        entries = value.list("")
        assert [e["name"] for e in entries] == ["Folder", "doc.txt"]
        assert entries[0]["directory"] and entries[1]["path"] == "A!2"
        for cursor in ["https://evil.example/v1.0/me/drive/root/children", "https://graph.microsoft.com@evil.example/v1.0/me/drive/root/children", "http://graph.microsoft.com/v1.0/me/drive/root/children"]:
            with pytest.raises(NasError):
                value.list_page("", cursor)
        assert len(calls) == 2
    finally:
        value.close()


@pytest.mark.parametrize("url", ["http://files.1drv.com/x", "https://evil1drv.com/x", "https://127.0.0.1/x", "https://u@files.1drv.com/x", "https://files.1drv.com:444/x", "https://files.1drv.com.evil.example/x", "https://my.microsoftpersonalcontent.com.evil.example/x", "https://evil.microsoftpersonalcontent.com/x", "http://my.microsoftpersonalcontent.com/x", "https://u@my.microsoftpersonalcontent.com/x", "https://my.microsoftpersonalcontent.com:444/x"])
def test_download_and_upload_urls_reject_untrusted_hosts(url):
    with pytest.raises(NasError, match="invalid_download_url"):
        transfer_url(url)


@pytest.mark.parametrize("host", ["public.files.1drv.com", "my.microsoftpersonalcontent.com"])
def test_download_is_bounded_and_never_forwards_authorization(monkeypatch, host):
    real_client = httpx.Client
    seen = []
    value = connector(lambda request: httpx.Response(200, json={"size": 2, "@microsoft.graph.downloadUrl": f"https://{host}/download?private=token"}))
    def download(request):
        seen.append(request)
        assert "authorization" not in request.headers
        return httpx.Response(200, content=b"too large")
    monkeypatch.setattr(httpx, "Client", lambda **kwargs: real_client(transport=httpx.MockTransport(download), **kwargs))
    try:
        with pytest.raises(NasError, match="file_too_large"):
            value.read("A!1", 3)
        assert len(seen) == 1
    finally:
        value.close()


@pytest.mark.parametrize("host", ["sn3302.up.1drv.com", "my.microsoftpersonalcontent.com"])
def test_upload_uses_fail_conflict_and_bounded_sequential_chunks(monkeypatch, host):
    real_client = httpx.Client
    content = b"x" * (10 * 320 * 1024 + 7)
    ranges = []
    def graph(request):
        body = json.loads(request.content)
        assert body["item"]["@microsoft.graph.conflictBehavior"] == "fail"
        assert "name" not in body["item"]
        assert request.url.path.endswith("/P!1:/报告.txt:/createUploadSession")
        return httpx.Response(200, json={"uploadUrl": f"https://{host}/up/session"})
    value = connector(graph)
    def upload(request):
        assert request.method == "PUT" and "authorization" not in request.headers
        ranges.append(request.headers["content-range"])
        return httpx.Response(201 if len(ranges) == 2 else 202, json={"id": "NEW!1"} if len(ranges) == 2 else {"nextExpectedRanges": ["3276800-"]})
    monkeypatch.setattr(httpx, "Client", lambda **kwargs: real_client(transport=httpx.MockTransport(upload), **kwargs))
    try:
        assert value.write("P!1/报告.txt", content)["id"] == "NEW!1"
        assert ranges == [f"bytes 0-3276799/{len(content)}", f"bytes 3276800-{len(content)-1}/{len(content)}"]
    finally:
        value.close()


def test_conflict_does_not_replace_file_or_leak_transfer_url(monkeypatch):
    real_client = httpx.Client
    value = connector(lambda request: httpx.Response(200, json={"uploadUrl": "https://sn3302.up.1drv.com/up/private-session"}))
    calls = []
    def response(request):
        calls.append(request.method)
        return httpx.Response(409 if request.method == "PUT" else 204, json={})
    monkeypatch.setattr(httpx, "Client", lambda **kwargs: real_client(transport=httpx.MockTransport(response), **kwargs))
    try:
        with pytest.raises(NasError, match="file_exists") as exc:
            value.write("already.txt", b"new")
        assert "private-session" not in str(exc.value)
        assert calls == ["PUT", "DELETE"]
    finally:
        value.close()
