"""Delegated Microsoft Graph access to the signed-in user's OneDrive."""
import re
from urllib.parse import quote, urlsplit

import httpx

from .cloud_drives import CloudConnector
from .nas import MAX_ENTRIES, MAX_FILE, NasError

GRAPH = "https://graph.microsoft.com/v1.0"


def transfer_url(value):
    """Preauthenticated URLs are usable only on Microsoft file-service hosts."""
    try:
        parsed = urlsplit(value)
        host = parsed.hostname or ""
        valid = host in {"api.onedrive.com", "my.microsoftpersonalcontent.com"} or any(host.endswith("." + domain) for domain in (
            "1drv.com", "1drvusercontent.com", "sharepoint.com", "storage.live.com",
        ))
        if parsed.scheme != "https" or not valid or parsed.username or parsed.password or parsed.port not in (None, 443) or parsed.fragment:
            raise ValueError()
        return value
    except (TypeError, ValueError):
        raise NasError("drive_invalid_download_url", 502) from None


def graph_response(response):
    if response.status_code in (401, 403):
        raise NasError("drive_authorization_required", 401)
    if response.status_code == 404:
        raise NasError("file_not_found", 404)
    if response.status_code in (409, 412):
        raise NasError("file_exists", 409)
    if response.status_code == 429:
        raise NasError("drive_rate_limited", 429)
    if response.status_code >= 300:
        raise NasError("drive_provider_request_failed", 502)
    value = response.json()
    if not isinstance(value, dict):
        raise NasError("drive_invalid_response", 502)
    return value


class OneDriveConnector(CloudConnector):
    @staticmethod
    def item_path(path):
        if not path:
            return "/me/drive/root"
        item_id = path.rsplit("/", 1)[-1]
        if not re.fullmatch(r"[A-Za-z0-9_!\-]+", item_id):
            raise NasError("invalid_drive_file_id")
        return "/me/drive/items/" + quote(item_id, safe="")

    def request(self, method, path, **kwargs):
        try:
            return graph_response(self.http.request(method, GRAPH + path,
                headers={"Authorization": "Bearer " + self.token()}, **kwargs))
        except NasError:
            raise
        except Exception:
            raise NasError("drive_provider_unreachable", 502) from None

    def list_page(self, path, cursor=""):
        endpoint = self.item_path(path) + "/children"
        params = {"$top": 200, "$select": "id,name,size,lastModifiedDateTime,file,folder,remoteItem,deleted"}
        if cursor:
            parsed = urlsplit(cursor)
            if parsed.scheme != "https" or parsed.netloc != "graph.microsoft.com" or not parsed.path.startswith("/v1.0/") or not parsed.path.endswith("/children") or parsed.fragment:
                raise NasError("drive_invalid_response", 502)
            endpoint = parsed.path[len("/v1.0"):] + ("?" + parsed.query if parsed.query else "")
            params = None
        value = self.request("GET", endpoint, params=params)
        entries = []
        for item in value.get("value", []):
            # Shared shortcuts can point at another drive; this connector stays in /me/drive.
            if "remoteItem" in item or "deleted" in item or not ("file" in item or "folder" in item):
                continue
            item_id = str(item.get("id", ""))
            self.item_path(item_id)
            entries.append({"name": item["name"], "path": (path + "/" if path else "") + item_id,
                            "directory": "folder" in item, "size": int(item.get("size", 0)),
                            "modified": item.get("lastModifiedDateTime", ""), "mime_type": item.get("file", {}).get("mimeType", "")})
        return {"entries": entries, "next_cursor": value.get("@odata.nextLink", "")}

    def list(self, path):
        entries, cursor, seen = [], "", set()
        while True:
            page = self.list_page(path, cursor)
            entries.extend(page["entries"])
            if len(entries) > MAX_ENTRIES:
                raise NasError("directory_too_large", 413)
            cursor = page["next_cursor"]
            if not cursor:
                return entries
            if cursor in seen:
                raise NasError("drive_invalid_response", 502)
            seen.add(cursor)

    def read(self, path, limit):
        metadata = self.request("GET", self.item_path(path))
        if int(metadata.get("size", 0)) > limit:
            raise NasError("file_too_large", 413)
        url = transfer_url(metadata.get("@microsoft.graph.downloadUrl", ""))
        try:
            # Never forward a Graph bearer token to a download host, including redirects.
            with httpx.Client(timeout=60, follow_redirects=False) as client:
                for _ in range(5):
                    with client.stream("GET", transfer_url(url)) as response:
                        if response.status_code in (301, 302, 303, 307, 308):
                            url = response.headers.get("location", "")
                            continue
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

    def destination(self, path):
        parent, _, name = path.rpartition("/")
        if not name or any(c in name for c in '\\/:*?"<>|') or name.endswith((" ", ".")):
            raise NasError("invalid_path")
        return self.item_path(parent), name

    def mkdir(self, path):
        parent, name = self.destination(path)
        return self.request("POST", parent + "/children", json={"name": name, "folder": {}, "@microsoft.graph.conflictBehavior": "fail"})

    def write(self, path, data):
        parent, name = self.destination(path)
        if len(data) > MAX_FILE:
            raise NasError("file_too_large", 413)
        if not data:
            # Empty upload-session fragments are not valid; do not risk a replacing PUT.
            raise NasError("onedrive_empty_upload", 400)
        # The destination URL already supplies the name. Migrated personal drives
        # reject a redundant item.name with invalidRequest; retain conflict protection.
        session = self.request("POST", parent + ":/" + quote(name, safe="") + ":/createUploadSession",
                               json={"item": {"@microsoft.graph.conflictBehavior": "fail"}})
        url = transfer_url(session.get("uploadUrl", ""))
        complete = False
        try:
            with httpx.Client(timeout=60, follow_redirects=False) as client:
                try:
                    chunk_size = 10 * 320 * 1024
                    for start in range(0, len(data), chunk_size):
                        part = data[start:start + chunk_size]
                        response = client.put(url, content=part, headers={
                            "Content-Range": f"bytes {start}-{start + len(part) - 1}/{len(data)}",
                            "Content-Type": "application/octet-stream",
                        })
                        result = graph_response(response)
                        last = start + len(part) == len(data)
                        if last:
                            if response.status_code not in (200, 201) or not result.get("id"):
                                raise NasError("drive_provider_request_failed", 502)
                            complete = True
                            return result
                        if response.status_code != 202:
                            raise NasError("drive_provider_request_failed", 502)
                finally:
                    if not complete:
                        try:
                            client.delete(url)  # Discard only this unfinished temporary upload.
                        except Exception:
                            pass
        except NasError:
            raise
        except Exception:
            raise NasError("drive_provider_request_failed", 502) from None
