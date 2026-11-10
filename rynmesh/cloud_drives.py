"""Cloud drive connectors behind the local NAS boundary.

OAuth clients and refresh tokens live only in NasPlugin's encrypted state.
Browser code receives opaque authorization IDs and public source metadata.
"""
from __future__ import annotations

import base64
import hashlib
import json
import secrets
import threading
import time
import uuid
from contextlib import nullcontext
from functools import lru_cache
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlsplit

import httpx

from .nas import MAX_ENTRIES, NasError, relative_path

GOOGLE_API = "https://www.googleapis.com/drive/v3"
BAIDU_API = "https://pan.baidu.com/rest/2.0/xpan"
PROVIDERS = ("google", "baidu", "onedrive")
MICROSOFT_AUTH = "https://login.microsoftonline.com/common/oauth2/v2.0"
FOLDER_MIME = "application/vnd.google-apps.folder"


@lru_cache(maxsize=2)
def callback_messages(language):
    locale = "zh-CN" if str(language).lower().startswith("zh") else "en"
    return json.loads((Path(__file__).with_name("locales") / f"cloud_drives.{locale}.json").read_text(encoding="utf-8"))


def provider_request(method, url, **kwargs):
    """Never propagate httpx errors, which can include token-bearing URLs."""
    oauth_errors = kwargs.pop("oauth_errors", False)
    shared_client = kwargs.pop("_client", None)
    try:
        with (nullcontext(shared_client) if shared_client else httpx.Client(timeout=30, follow_redirects=False)) as client:
            response = client.request(method, url, **kwargs)
        if response.status_code in (401, 403):
            raise NasError("drive_authorization_required", 401)
        if response.status_code == 429:
            raise NasError("drive_rate_limited", 429)
        if response.status_code >= 400 and not (oauth_errors and response.status_code == 400):
            raise NasError("drive_provider_request_failed", 502)
        data = response.json()
        if not isinstance(data, dict):
            raise NasError("drive_invalid_response", 502)
        return data
    except NasError:
        raise
    except Exception:
        raise NasError("drive_provider_unreachable", 502) from None


def bounded_download(url, *, headers=None, limit, baidu=False):
    try:
        with httpx.Client(timeout=60, follow_redirects=False) as client:
            for _ in range(5):
                parsed = urlsplit(url)
                host = parsed.hostname or ""
                allowed = host == "www.googleapis.com" if not baidu else (
                    host == "d.pcs.baidu.com" or host.endswith(".baidupcs.com") or host.endswith(".pcs.baidu.com")
                )
                if parsed.scheme != "https" or not allowed or parsed.username or parsed.password or parsed.port not in (None, 443):
                    raise NasError("drive_invalid_download_url", 502)
                with client.stream("GET", url, headers=headers) as response:
                    if response.status_code in (301, 302, 303, 307, 308):
                        url = response.headers.get("location", "")
                        continue
                    if response.status_code in (401, 403):
                        raise NasError("drive_authorization_required", 401)
                    if response.status_code != 200:
                        raise NasError("drive_download_failed", 502)
                    data = bytearray()
                    for chunk in response.iter_bytes(chunk_size=64 * 1024):
                        data.extend(chunk)
                        if len(data) > limit:
                            raise NasError("file_too_large", 413)
                    return bytes(data)
            raise NasError("drive_too_many_redirects", 502)
    except NasError:
        raise
    except Exception:
        raise NasError("drive_download_failed", 502) from None


class CloudConnector:
    def __init__(self, config, persist):
        self.config = config
        self.persist = persist
        self._http = None

    @property
    def http(self):
        if self._http is None:
            self._http = httpx.Client(timeout=httpx.Timeout(30, connect=8), follow_redirects=False,
                                      limits=httpx.Limits(max_connections=4, max_keepalive_connections=2, keepalive_expiry=60))
        return self._http

    def token(self):
        auth = self.config["oauth"]
        if float(auth.get("expires_at", 0)) > time.time() + 60:
            return auth["access_token"]
        if not auth.get("refresh_token"):
            raise NasError("drive_authorization_required", 401)
        provider = self.config["protocol"]
        url = {"google": "https://oauth2.googleapis.com/token", "baidu": "https://openapi.baidu.com/oauth/2.0/token", "onedrive": MICROSOFT_AUTH + "/token"}[provider]
        payload = {
            "grant_type": "refresh_token", "refresh_token": auth["refresh_token"],
            "client_id": auth["client_id"],
        }
        if provider != "onedrive":
            payload["client_secret"] = auth.get("client_secret", "")
        data = provider_request("POST", url, _client=self.http, oauth_errors=True, data=payload)
        if not data.get("access_token"):
            raise NasError("drive_authorization_required", 401)
        auth.update(access_token=data["access_token"], expires_at=time.time() + int(data.get("expires_in", 3600)))
        if data.get("refresh_token"):
            auth["refresh_token"] = data["refresh_token"]
        self.persist()
        return auth["access_token"]

    def close(self):
        if self._http is not None:
            self._http.close()
            self._http = None


class GoogleConnector(CloudConnector):
    def request(self, method, path, **kwargs):
        return provider_request(method, GOOGLE_API + path, _client=self.http, headers={"Authorization": "Bearer " + self.token()}, **kwargs)

    @staticmethod
    def file_id(path):
        value = path.rsplit("/", 1)[-1] if path else "root"
        if not value or any(c not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-" for c in value):
            raise NasError("invalid_drive_file_id")
        return value

    def list_page(self, path, cursor=""):
        params = {"q": f"'{self.file_id(path)}' in parents and trashed = false", "pageSize": 200,
                  "fields": "nextPageToken,files(id,name,mimeType,size,modifiedTime)", "supportsAllDrives": "true", "includeItemsFromAllDrives": "true"}
        if cursor:
            params["pageToken"] = cursor
        data = self.request("GET", "/files", params=params)
        entries = [{"name": item["name"], "path": (path + "/" if path else "") + item["id"],
                    "directory": item["mimeType"] == FOLDER_MIME, "size": int(item.get("size", 0)),
                    "modified": item.get("modifiedTime", ""), "mime_type": item.get("mimeType", "")}
                   for item in data.get("files", []) if item.get("mimeType") != "application/vnd.google-apps.shortcut"]
        return {"entries": entries, "next_cursor": data.get("nextPageToken", "")}

    def list(self, path):
        parent = self.file_id(path)
        page = ""
        entries = []
        while True:
            params = {"q": f"'{parent}' in parents and trashed = false", "pageSize": 1000,
                      "fields": "nextPageToken,files(id,name,mimeType,size,modifiedTime)", "supportsAllDrives": "true", "includeItemsFromAllDrives": "true"}
            if page:
                params["pageToken"] = page
            data = self.request("GET", "/files", params=params)
            for item in data.get("files", []):
                if item.get("mimeType") == "application/vnd.google-apps.shortcut":
                    continue
                entries.append({"name": item["name"], "path": (path + "/" if path else "") + item["id"],
                                "directory": item["mimeType"] == FOLDER_MIME, "size": int(item.get("size", 0)),
                                "modified": item.get("modifiedTime", ""), "mime_type": item.get("mimeType", "")})
                if len(entries) > MAX_ENTRIES:
                    raise NasError("directory_too_large", 413)
            page = data.get("nextPageToken", "")
            if not page:
                return entries

    def metadata(self, path):
        return self.request("GET", "/files/" + self.file_id(path), params={"fields": "name,mimeType", "supportsAllDrives": "true"})

    def read(self, path, limit):
        file_id = self.file_id(path)
        metadata = self.metadata(path)
        mime = metadata.get("mimeType", "")
        url = GOOGLE_API + "/files/" + file_id
        if mime.startswith("application/vnd.google-apps."):
            exports = {"application/vnd.google-apps.document": "text/plain", "application/vnd.google-apps.spreadsheet": "text/csv", "application/vnd.google-apps.presentation": "text/plain"}
            if mime not in exports:
                raise NasError("drive_file_export_not_supported", 415)
            url += "/export?" + urlencode({"mimeType": exports[mime]})
        else:
            url += "?alt=media&supportsAllDrives=true"
        return bounded_download(url, headers={"Authorization": "Bearer " + self.token()}, limit=limit)

    def _destination(self, path):
        parent, _, name = path.rpartition("/")
        if not name or name in {".", ".."}:
            raise NasError("invalid_path")
        # Do not silently overwrite files, including Drive's same-name siblings.
        if any(e["name"] == name for e in self.list(parent)):
            raise NasError("file_exists", 409)
        return self.file_id(parent), name

    def mkdir(self, path):
        parent, name = self._destination(path)
        return self.request("POST", "/files", json={"name": name, "mimeType": FOLDER_MIME, "parents": [parent]})

    def write(self, path, data):
        parent, name = self._destination(path)
        boundary = uuid.uuid4().hex
        metadata = json.dumps({"name": name, "parents": [parent]}).encode()
        content = (f"--{boundary}\r\nContent-Type: application/json; charset=UTF-8\r\n\r\n".encode() + metadata
                   + f"\r\n--{boundary}\r\nContent-Type: application/octet-stream\r\n\r\n".encode() + data + f"\r\n--{boundary}--\r\n".encode())
        return provider_request("POST", "https://www.googleapis.com/upload/drive/v3/files", _client=self.http, params={"uploadType": "multipart"},
                         headers={"Authorization": "Bearer " + self.token(), "Content-Type": "multipart/related; boundary=" + boundary}, content=content)


class BaiduConnector(CloudConnector):
    def request(self, method, endpoint, operation, **kwargs):
        params = {"method": operation, "access_token": self.token(), **kwargs.pop("params", {})}
        result = provider_request(method, BAIDU_API + endpoint, _client=self.http, params=params, **kwargs)
        error = result.get("errno", 0)
        if error in (110, 111, -6):
            raise NasError("drive_authorization_required", 401)
        if error in (-7, -8, -10):
            raise NasError("file_exists", 409)
        if error:
            raise NasError("drive_provider_error_" + str(int(error)), 502)
        return result

    def remote(self, path):
        return "/" + "/".join(p for p in (self.config.get("root", ""), relative_path(path)) if p)

    def list_page(self, path, cursor=""):
        try:
            start = int(cursor or 0)
        except ValueError:
            raise NasError("invalid_path") from None
        if start < 0 or start > MAX_ENTRIES:
            raise NasError("directory_too_large", 413)
        data = self.request("GET", "/file", "list", params={"dir": self.remote(path), "start": start, "limit": 200})
        batch = data.get("list", [])
        entries = [{"name": item["server_filename"], "path": relative_path((path + "/" if path else "") + item["server_filename"]),
                    "directory": bool(item["isdir"]), "size": int(item.get("size", 0)), "modified": item.get("server_mtime", 0),
                    "file_id": str(item["fs_id"])} for item in batch]
        return {"entries": entries, "next_cursor": str(start + len(batch)) if len(batch) == 200 else ""}

    def list(self, path):
        entries = []
        for start in range(0, MAX_ENTRIES + 1, 1000):
            data = self.request("GET", "/file", "list", params={"dir": self.remote(path), "start": start, "limit": 1000})
            batch = data.get("list", [])
            for item in batch:
                name = item["server_filename"]
                child = relative_path((path + "/" if path else "") + name)
                entries.append({"name": name, "path": child, "directory": bool(item["isdir"]), "size": int(item.get("size", 0)), "modified": item.get("server_mtime", 0), "file_id": str(item["fs_id"])})
            if len(entries) > MAX_ENTRIES:
                raise NasError("directory_too_large", 413)
            if len(batch) < 1000:
                return entries
        raise NasError("directory_too_large", 413)

    def read(self, path, limit):
        parent, _, name = path.rpartition("/")
        entry = next((e for e in self.list(parent) if e["name"] == name), None)
        if not entry:
            raise NasError("file_not_found", 404)
        if entry["size"] > limit:
            raise NasError("file_too_large", 413)
        meta = self.request("GET", "/multimedia", "filemetas", params={"fsids": json.dumps([int(entry["file_id"])]), "dlink": 1})
        items = meta.get("list", [])
        if not items or not items[0].get("dlink"):
            raise NasError("drive_download_unavailable", 403)
        url = items[0]["dlink"]
        url += ("&" if "?" in url else "?") + urlencode({"access_token": self.token()})
        return bounded_download(url, limit=limit, baidu=True, headers={"User-Agent": "pan.baidu.com"})

    def mkdir(self, path):
        self.request("POST", "/file", "create", data={"path": self.remote(path), "isdir": 1, "size": 0, "rtype": 0})

    def write(self, path, data):
        parent, _, name = path.rpartition("/")
        if any(e["name"] == name for e in self.list(parent)):
            raise NasError("file_exists", 409)
        chunks = [data[i:i + 4 * 1024 * 1024] for i in range(0, len(data), 4 * 1024 * 1024)] or [b""]
        hashes = [hashlib.md5(part).hexdigest() for part in chunks]
        common = {"path": self.remote(path), "size": len(data), "isdir": 0, "block_list": json.dumps(hashes), "rtype": 0}
        prepared = self.request("POST", "/file", "precreate", data={**common, "autoinit": 1})
        upload_id = prepared.get("uploadid")
        if not upload_id:
            raise NasError("drive_upload_failed", 502)
        for index in prepared.get("block_list", list(range(len(chunks)))):
            index = int(index)
            if not 0 <= index < len(chunks):
                raise NasError("drive_invalid_upload_part", 502)
            result = provider_request("POST", "https://d.pcs.baidu.com/rest/2.0/pcs/superfile2", params={
                "method": "upload", "access_token": self.token(), "type": "tmpfile", "path": self.remote(path), "uploadid": upload_id, "partseq": index,
            }, files={"file": ("chunk", chunks[index], "application/octet-stream")})
            if result.get("md5") != hashes[index]:
                raise NasError("drive_upload_checksum_failed", 502)
        self.request("POST", "/file", "create", data={**common, "uploadid": upload_id})


class CloudDrives:
    def __init__(self, plugin):
        self.plugin = plugin
        self.pending = {}
        self.lock = threading.RLock()

    def configured(self):
        apps = self.plugin.state.get("cloud_apps", {})
        return [{"id": p, "configured": bool(apps.get(p, {}).get("client_id")), "client_id": apps.get(p, {}).get("client_id", "")} for p in PROVIDERS]

    def configure(self, provider, body):
        if provider not in PROVIDERS:
            raise NasError("unsupported_drive_provider")
        client_id = str(body.get("client_id", "")).strip()
        client_secret = str(body.get("client_secret", "")).strip()
        if not client_id or len(client_id) > 512 or len(client_secret) > 512:
            raise NasError("invalid_oauth_client")
        with self.plugin.lock:
            previous = self.plugin.state.setdefault("cloud_apps", {}).get(provider, {})
            if not client_secret and client_id == previous.get("client_id"):
                client_secret = previous.get("client_secret", "")
            if provider == "baidu" and not client_secret:
                raise NasError("oauth_client_secret_required")
            if provider == "onedrive":
                client_secret = ""  # Public desktop application: no client secret.
            self.plugin.state["cloud_apps"][provider] = {"client_id": client_id, "client_secret": client_secret}
            self.plugin.save()
        return {"configured": True}

    def start(self, provider, body):
        with self.plugin.lock:
            self.plugin.require_enabled()
            app = dict(self.plugin.state.get("cloud_apps", {}).get(provider, {}))
            source_id = body.get("source_id")
            if source_id and self.plugin.source(source_id)["protocol"] != provider:
                raise NasError("drive_provider_mismatch")
        if provider not in PROVIDERS or not app.get("client_id"):
            raise NasError("oauth_app_not_configured", 409)
        name = str(body.get("name", "")).strip()
        if not name or len(name) > 80:
            raise NasError("invalid_name")
        with self.lock:
            self.pending = {k: v for k, v in self.pending.items() if v["expires"] > time.time()}
            if len(self.pending) >= 8:
                raise NasError("too_many_authorizations", 429)
            auth_id = secrets.token_urlsafe(32)
            item = {"provider": provider, "app": app, "name": name, "source_id": source_id, "writable": body.get("writable") is True,
                    "allow_ai": body.get("allow_ai") is True, "state": "waiting", "expires": time.time() + 600}
            self.pending[auth_id] = item
        if provider == "onedrive":
            scope = "offline_access https://graph.microsoft.com/" + ("Files.ReadWrite" if item["writable"] else "Files.Read")
            data = provider_request("POST", MICROSOFT_AUTH + "/devicecode", oauth_errors=True, data={"client_id": app["client_id"], "scope": scope})
            if data.get("error") in {"invalid_client", "unauthorized_client", "invalid_scope"}:
                raise NasError("onedrive_app_configuration", 400)
            if not data.get("device_code") or not data.get("user_code"):
                raise NasError("drive_authorization_failed", 400)
            verification = data.get("verification_uri", "")
            parsed = urlsplit(verification)
            if parsed.scheme != "https" or parsed.hostname not in {"microsoft.com", "www.microsoft.com", "login.microsoftonline.com", "login.microsoft.com"} or parsed.username or parsed.password or parsed.port not in (None, 443):
                raise NasError("drive_invalid_authorization_url", 502)
            expires = min(900, max(1, int(data.get("expires_in", 900))))
            interval = max(5, int(data.get("interval", 5)))
            item.update(device_code=data["device_code"], interval=interval, next_poll=time.time() + interval, expires=time.time() + expires)
            return {"id": auth_id, "url": verification, "user_code": data["user_code"], "expires_in": expires}
        if provider == "baidu":
            data = provider_request("GET", "https://openapi.baidu.com/oauth/2.0/device/code", params={"response_type": "device_code", "client_id": app["client_id"], "scope": "basic,netdisk"})
            if not data.get("device_code") or not data.get("user_code"):
                raise NasError("drive_authorization_failed", 400)
            verification = data.get("verification_url", "https://openapi.baidu.com/device")
            parsed = urlsplit(verification)
            if parsed.scheme != "https" or parsed.hostname != "openapi.baidu.com":
                raise NasError("drive_invalid_authorization_url", 502)
            item.update(device_code=data["device_code"], interval=max(5, int(data.get("interval", 5))), next_poll=0)
            return {"id": auth_id, "url": verification, "user_code": data["user_code"], "expires_in": 600}
        verifier = secrets.token_urlsafe(48)
        state = secrets.token_urlsafe(32)
        item.update(verifier=verifier, csrf=state)
        service = self
        messages = callback_messages("zh-CN" if str(body.get("language", "en")).lower().startswith("zh") else "en")

        class Callback(BaseHTTPRequestHandler):
            def log_message(self, *_):
                pass

            def reply(self, status, key):
                self.send_response(status)
                self.send_header("Content-Type", "text/plain; charset=utf-8")
                self.send_header("Cache-Control", "no-store")
                self.send_header("Referrer-Policy", "no-referrer")
                self.end_headers()
                self.wfile.write(messages[key].encode("utf-8"))

            def do_GET(self):
                query = parse_qs(urlsplit(self.path).query)
                if urlsplit(self.path).path != "/callback" or not secrets.compare_digest(query.get("state", [""])[0], state):
                    self.reply(400, "invalid_state")
                    return
                with service.lock:
                    if item["state"] != "waiting" or item["expires"] <= time.time():
                        self.reply(400, "expired")
                        return
                    item["state"] = "exchanging"
                try:
                    code = query.get("code", [""])[0]
                    if not code:
                        raise NasError("drive_authorization_denied", 400)
                    token = provider_request("POST", "https://oauth2.googleapis.com/token", data={
                        **app, "grant_type": "authorization_code", "code": code, "code_verifier": verifier, "redirect_uri": item["redirect_uri"],
                    })
                    service._finish(item, token)
                    message = "connected"
                except Exception:
                    item.update(state="failed", error="drive_authorization_failed")
                    message = "failed"
                self.reply(200, message)

        server = HTTPServer(("127.0.0.1", 0), Callback)
        server.timeout = 1
        item["redirect_uri"] = f"http://127.0.0.1:{server.server_port}/callback"

        def listen():
            try:
                while item["state"] == "waiting" and item["expires"] > time.time():
                    server.handle_request()
            finally:
                server.server_close()
        threading.Thread(target=listen, name="ryn-drive-oauth", daemon=True).start()
        scope = "https://www.googleapis.com/auth/drive" if item["writable"] else "https://www.googleapis.com/auth/drive.readonly"
        params = {"client_id": app["client_id"], "redirect_uri": item["redirect_uri"], "response_type": "code", "scope": scope,
                  "access_type": "offline", "prompt": "consent", "state": state, "code_challenge_method": "S256",
                  "code_challenge": base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()}
        return {"id": auth_id, "url": "https://accounts.google.com/o/oauth2/v2/auth?" + urlencode(params), "expires_in": 600}

    def _finish(self, item, token):
        with self.lock:
            if item["state"] not in {"waiting", "exchanging"} or item["expires"] <= time.time():
                raise NasError("drive_authorization_expired", 410)
            self._save_authorization(item, token)

    def _save_authorization(self, item, token):
        if not token.get("access_token") or not token.get("refresh_token"):
            raise NasError("drive_offline_authorization_required", 401)
        if item["provider"] == "onedrive":
            granted = {scope.rsplit("/", 1)[-1].lower() for scope in token.get("scope", "").split()}
            needed = {"files.readwrite", "files.readwrite.all"} if item["writable"] else {"files.read", "files.read.all", "files.readwrite", "files.readwrite.all"}
            if not granted.intersection(needed):
                raise NasError("drive_permission_missing", 403)
        config = {"id": item.get("source_id") or uuid.uuid4().hex, "name": item["name"], "system": item["provider"], "protocol": item["provider"],
                  "access_generation": uuid.uuid4().hex,
                  "root": "", "username": "", "writable": item["writable"], "allow_ai": item["allow_ai"], "write_authorized": item["writable"],
                  "oauth": {**item["app"], "access_token": token["access_token"], "refresh_token": token["refresh_token"], "expires_at": time.time() + int(token.get("expires_in", 3600))}}
        with self.plugin.lock:
            self.plugin.require_enabled()
            if item.get("source_id"):
                self.plugin.source(item["source_id"])  # A removed source must not be resurrected.
            elif len(self.plugin.state["sources"]) >= 32:
                raise NasError("too_many_sources")
        with self.plugin.connection(config) as connector:
            entries = connector.list("")
        with self.plugin.lock:
            self.plugin.require_enabled()
            if item.get("source_id"):
                self.plugin.source(item["source_id"])
            elif len(self.plugin.state["sources"]) >= 32:
                raise NasError("too_many_sources")
            self.plugin.state["sources"] = [s for s in self.plugin.state["sources"] if s["id"] != config["id"]] + [config]
            self.plugin.directory_cache.invalidate(config["id"])
            self.plugin.save()
            self.plugin.directory_cache.put(config["id"], "", entries)
        item.update(state="connected", source_id=config["id"])
        item.pop("app", None)
        item.pop("device_code", None)

    def poll(self, auth_id):
        with self.lock:
            item = self.pending.get(auth_id)
            if not item or item["expires"] <= time.time():
                raise NasError("drive_authorization_expired", 410)
            if item["provider"] in {"baidu", "onedrive"} and item["state"] == "waiting" and time.time() >= item["next_poll"]:
                item["next_poll"] = time.time() + item["interval"]
                try:
                    if item["provider"] == "onedrive":
                        data = provider_request("POST", MICROSOFT_AUTH + "/token", oauth_errors=True, data={
                            "grant_type": "urn:ietf:params:oauth:grant-type:device_code", "device_code": item["device_code"], "client_id": item["app"]["client_id"],
                        })
                    else:
                        data = provider_request("GET", "https://openapi.baidu.com/oauth/2.0/token", oauth_errors=True, params={
                            "grant_type": "device_token", "code": item["device_code"], **item["app"],
                        })
                    error = data.get("error")
                    if error in ("authorization_pending", "slow_down"):
                        if error == "slow_down":
                            item["interval"] += 5
                            item["next_poll"] = time.time() + item["interval"]
                    elif error:
                        item.update(state="failed", error="drive_authorization_denied")
                    else:
                        self._finish(item, data)
                except NasError as exc:
                    item.update(state="failed", error=str(exc))
            return {key: item[key] for key in ("state", "source_id", "error") if key in item}

    def cancel(self, auth_id):
        with self.lock:
            if auth_id in self.pending and self.pending[auth_id]["state"] in {"waiting", "exchanging"}:
                self.pending[auth_id]["state"] = "cancelled"
        return {"ok": True}
