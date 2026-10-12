import base64
import hashlib
import json
import time
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest

from rynmesh import cloud_drives as cloud
from rynmesh.nas import NasError, NasPlugin


@pytest.fixture
def plugin(tmp_path):
    value = NasPlugin(tmp_path)
    value.enable(True)
    for provider in cloud.PROVIDERS:
        value.cloud.configure(provider, {"client_id": provider + "-client", "client_secret": "private-secret"})
    return value


@pytest.mark.parametrize("language,expected", [("en", "Drive connected"), ("zh-CN", "网盘连接成功")])
def test_google_pkce_callback_state_and_encrypted_persistence(plugin, monkeypatch, language, expected):
    requests = []

    def request(method, url, **kwargs):
        requests.append((url, kwargs))
        if url.endswith("/token"):
            return {"access_token": "private-access", "refresh_token": "private-refresh", "expires_in": 3600}
        return {"files": []}

    monkeypatch.setattr(cloud, "provider_request", request)
    auth = plugin.cloud.start("google", {"name": "Google", "language": language})
    query = parse_qs(urlsplit(auth["url"]).query)
    redirect = query["redirect_uri"][0]
    assert query["scope"] == ["https://www.googleapis.com/auth/drive.readonly"]
    with httpx.Client(trust_env=False) as client:
        assert client.get(redirect, params={"state": "wrong", "code": "code"}).status_code == 400
        assert not requests
        response = client.get(redirect, params={"state": query["state"][0], "code": "code"})
        assert response.status_code == 200
        assert expected in response.text
    result = plugin.cloud.poll(auth["id"])
    assert result["state"] == "connected"
    verifier = requests[0][1]["data"]["code_verifier"]
    assert query["code_challenge"] == [base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()]
    serialized = json.dumps(plugin.status())
    assert "private-" not in serialized and "refresh_token" not in serialized
    assert b"private-" not in (plugin.directory / "state.enc").read_bytes()
    restored = NasPlugin(plugin.directory.parent.parent)
    assert restored.status()["sources"] == plugin.status()["sources"]
    assert plugin.state["sources"][0]["oauth"]["refresh_token"] == "private-refresh"
    # A second callback cannot add another source even if delivered by a race.
    with pytest.raises(NasError, match="expired"):
        plugin.cloud._finish(plugin.cloud.pending[auth["id"]], {"access_token": "x", "refresh_token": "y"})
    assert len(plugin.state["sources"]) == 1


def test_baidu_poll_throttle_and_reauthorize_replaces_source(plugin, monkeypatch):
    calls = []
    def request(method, url, **kwargs):
        calls.append(url)
        if url.endswith("device/code"):
            return {"device_code": "private-device", "user_code": "USER", "interval": 5}
        if url.endswith("/token"):
            if calls.count(url) == 1:
                return {"error": "authorization_pending"}
            return {"access_token": "access", "refresh_token": "refresh"}
        return {"errno": 0, "list": []}
    monkeypatch.setattr(cloud, "provider_request", request)
    auth = plugin.cloud.start("baidu", {"name": "Baidu", "writable": True})
    assert "private-device" not in json.dumps(auth)
    assert plugin.cloud.poll(auth["id"])["state"] == "waiting"
    before = len(calls)
    plugin.cloud.poll(auth["id"])
    assert len(calls) == before
    plugin.cloud.pending[auth["id"]]["next_poll"] = 0
    source_id = plugin.cloud.poll(auth["id"])["source_id"]
    auth2 = plugin.cloud.start("baidu", {"name": "Updated", "source_id": source_id})
    assert plugin.cloud.poll(auth2["id"])["source_id"] == source_id
    assert len(plugin.state["sources"]) == 1
    assert plugin.status()["sources"][0]["name"] == "Updated"


def test_cancel_cannot_commit_late_token_exchange(plugin, monkeypatch):
    monkeypatch.setattr(cloud, "provider_request", lambda *a, **k: {"device_code": "device", "user_code": "CODE"})
    auth = plugin.cloud.start("baidu", {"name": "Cancelled"})
    plugin.cloud.pending[auth["id"]]["state"] = "exchanging"
    plugin.cloud.cancel(auth["id"])
    with pytest.raises(NasError, match="expired"):
        plugin.cloud._finish(plugin.cloud.pending[auth["id"]], {"access_token": "access", "refresh_token": "refresh"})
    assert not plugin.state["sources"]


def test_refresh_rotation_pagination_and_overwrite_refusal(monkeypatch):
    config = {"protocol": "google", "oauth": {"expires_at": 0, "client_id": "id", "refresh_token": "old"}}
    persisted = []
    calls = []
    def request(method, url, **kwargs):
        calls.append((method, url, kwargs))
        if url.endswith("/token"):
            return {"access_token": "access", "refresh_token": "rotated", "expires_in": 3600}
        if kwargs.get("params", {}).get("pageToken"):
            return {"files": [{"id": "file2", "name": "two", "mimeType": "text/plain"}]}
        return {"files": [{"id": "file1", "name": "one", "mimeType": "text/plain"}], "nextPageToken": "next"}
    monkeypatch.setattr(cloud, "provider_request", request)
    connector = cloud.GoogleConnector(config, lambda: persisted.append(True))
    assert [e["name"] for e in connector.list("")] == ["one", "two"]
    assert config["oauth"]["refresh_token"] == "rotated" and persisted == [True]
    with pytest.raises(NasError, match="file_exists"):
        connector.write("one", b"not sent")
    assert not any("upload" in url for _, url, _ in calls)


def test_download_redirect_allowlist_and_size_cap(monkeypatch):
    real_client = httpx.Client
    seen = []
    def response(request):
        seen.append(str(request.url))
        if request.url.path == "/redirect":
            return httpx.Response(302, headers={"Location": "http://127.0.0.1/private"})
        return httpx.Response(200, content=b"x" * 100)
    monkeypatch.setattr(cloud.httpx, "Client", lambda **kwargs: real_client(transport=httpx.MockTransport(response), **kwargs))
    with pytest.raises(NasError, match="invalid_download_url"):
        cloud.bounded_download("https://www.googleapis.com/redirect", limit=1024)
    assert seen == ["https://www.googleapis.com/redirect"]
    with pytest.raises(NasError, match="file_too_large"):
        cloud.bounded_download("https://www.googleapis.com/large", limit=64)


def test_provider_exceptions_do_not_expose_token_urls(monkeypatch):
    class BrokenClient:
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def request(self, *args, **kwargs): raise RuntimeError("https://provider/?access_token=secret")
    monkeypatch.setattr(cloud.httpx, "Client", lambda **kwargs: BrokenClient())
    with pytest.raises(NasError) as error:
        cloud.provider_request("GET", "https://provider/?access_token=secret")
    assert "secret" not in str(error.value)


def test_google_write_permission_requires_new_consent(plugin):
    plugin.state["sources"].append({"id": "g", "name": "Google", "protocol": "google", "writable": False, "allow_ai": False})
    with pytest.raises(NasError, match="reauthorization"):
        plugin.update_source("g", {"writable": True})
    assert not plugin.source("g")["writable"]


def test_baidu_upload_uses_only_missing_chunks_and_checks_hashes(monkeypatch):
    content = b"a" * (4 * 1024 * 1024) + b"last"
    uploaded = []
    def request(method, url, **kwargs):
        operation = kwargs.get("params", {}).get("method")
        if operation == "list": return {"errno": 0, "list": []}
        if operation == "precreate": return {"errno": 0, "uploadid": "upload", "block_list": [1]}
        if operation == "upload":
            chunk = kwargs["files"]["file"][1]
            uploaded.append(chunk)
            return {"md5": hashlib.md5(chunk).hexdigest()}
        assert operation == "create"
        assert kwargs["data"]["rtype"] == 0
        return {"errno": 0}
    monkeypatch.setattr(cloud, "provider_request", request)
    connector = cloud.BaiduConnector({"protocol": "baidu", "oauth": {"access_token": "token", "expires_at": time.time() + 3600}}, lambda: None)
    connector.write("test.txt", content)
    assert uploaded == [b"last"]


def test_all_storage_error_codes_have_bilingual_messages():
    import ast
    from pathlib import Path
    root = Path(__file__).resolve().parents[1]
    codes = set()
    for name in ("nas.py", "cloud_drives.py", "onedrive.py"):
        tree = ast.parse((root / "rynmesh" / name).read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "NasError" and node.args:
                value = node.args[0]
                if isinstance(value, ast.Constant) and isinstance(value.value, str):
                    codes.add(value.value)
    for language in ("en", "zh-CN"):
        errors = json.loads((root / "webapp" / "src" / "locales" / f"drives.{language}.json").read_text(encoding="utf-8"))["errors"]
        assert codes <= errors.keys(), codes - errors.keys()
        assert all(errors[code].strip() for code in codes)
    assert cloud.callback_messages("zh-CN").keys() == cloud.callback_messages("en").keys()


@pytest.mark.parametrize("provider", ["google", "baidu"])
def test_first_directory_page_is_bounded_and_carries_continuation(monkeypatch, provider):
    calls = []
    def request(method, url, **kwargs):
        calls.append(kwargs)
        if provider == "google":
            return {"files": [{"id": "f1", "name": "one", "mimeType": "text/plain"}], "nextPageToken": "next"}
        return {"errno": 0, "list": [{"fs_id": n, "server_filename": str(n), "isdir": 0} for n in range(200)]}
    monkeypatch.setattr(cloud, "provider_request", request)
    config = {"protocol": provider, "root": "", "oauth": {"access_token": "test", "expires_at": time.time() + 3600}}
    connector = (cloud.GoogleConnector if provider == "google" else cloud.BaiduConnector)(config, lambda: None)
    try:
        page = connector.list_page("")
        assert page["next_cursor"] == ("next" if provider == "google" else "200")
        assert calls[0]["params"]["pageSize" if provider == "google" else "limit"] == 200
        connector.list_page("", page["next_cursor"])
        assert calls[1]["params"]["pageToken" if provider == "google" else "start"] == ("next" if provider == "google" else 200)
        assert calls[0]["_client"] is calls[1]["_client"]
    finally:
        connector.close()
