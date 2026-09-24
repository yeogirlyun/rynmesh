"""Optional NAS plugin: bounded file operations behind the local control boundary.

System presets share protocol connectors. They do not claim proprietary API support.
Credentials stay on the node that connects to the NAS, encrypted in its private state.
"""

from __future__ import annotations

import errno
import io
import json
import os
import re
import threading
import uuid
from contextlib import contextmanager
from pathlib import Path
from urllib.parse import quote, unquote, urlsplit
from xml.etree import ElementTree

from cryptography.fernet import Fernet

MAX_FILE = 64 * 1024 * 1024
MAX_PREVIEW = 256 * 1024
MAX_IMAGE = 20 * 1024 * 1024
MAX_IMAGE_PIXELS = 40_000_000
MAX_ENTRIES = 5000
SYSTEMS = [
    {
        "id": key,
        "name": name,
        "protocols": ["smb", "webdav"] if dav else ["smb"],
        "integration": "Standard file protocols; proprietary system APIs are not enabled.",
    }
    for key, name, dav in [
        ("fnos", "Feiniu fnOS", True),
        ("synology", "Synology DSM", True),
        ("qnap", "QNAP", True),
        ("truenas", "TrueNAS", False),
        ("unraid", "Unraid", False),
        ("omv", "OpenMediaVault", False),
        ("asustor", "ASUSTOR ADM", True),
        ("terramaster", "TerraMaster TOS", True),
        ("generic", "Other NAS", True),
    ]
]


class NasError(Exception):
    def __init__(self, code, status=400):
        super().__init__(code)
        self.status = status


def relative_path(value):
    if not isinstance(value, str) or len(value) > 2048:
        raise NasError("invalid_path")
    if value.startswith(("/", "\\")) or "\\" in value or ":" in value:
        raise NasError("invalid_path")
    if any(ord(c) < 32 for c in value) or any(p in {".", ".."} for p in value.split("/")):
        raise NasError("invalid_path")
    return "/".join(p for p in value.split("/") if p)


class SMBConnector:
    def __init__(self, config):
        import smbclient

        self.api = smbclient
        self.cache = {}  # Never share authenticated SMB sessions between NAS instances.
        self.config = config
        self.root = "\\\\" + config["host"] + "\\" + config["share"]
        if config["root"]:
            self.root += "\\" + config["root"].replace("/", "\\")
        smbclient.register_session(
            config["host"],
            username=config["username"],
            password=config["password"],
            port=config["port"],
            connection_timeout=10,
            connection_cache=self.cache,
        )

    def path(self, path):
        current = self.root
        # Disallow links/reparse points, including configured subdirectories.
        parts = self.config["root"].split("/") if self.config["root"] else []
        base = "\\\\" + self.config["host"] + "\\" + self.config["share"]
        for part in parts + (path.split("/") if path else []):
            base += "\\" + part
            try:
                st = self.api.stat(base, follow_symlinks=False, **self.kw)
                if st.st_file_attributes & 0x400:
                    raise NasError("links_not_allowed", 403)
            except OSError as exc:
                # smbprotocol raises SMBOSError with errno, not the builtin subclasses.
                if exc.errno == errno.ENOENT:
                    break
                raise
        return current + ("\\" + path.replace("/", "\\") if path else "")

    @property
    def kw(self):
        return {"connection_cache": self.cache, "port": self.config["port"]}

    def list(self, path):
        result = []
        for entry in self.api.scandir(self.path(path), **self.kw):
            # SMBDirEntry.stat() loses a non-default port in smbprotocol 1.17.
            st = self.api.stat(entry.path, follow_symlinks=False, **self.kw)
            if st.st_file_attributes & 0x400:
                continue
            if len(result) >= MAX_ENTRIES:
                raise NasError("directory_too_large", 413)
            name = entry.name
            child = relative_path((path + "/" if path else "") + name)
            result.append(
                {
                    "name": name,
                    "path": child,
                    "directory": bool(st.st_file_attributes & 0x10),
                    "size": st.st_size,
                    "modified": st.st_mtime,
                }
            )
        return result

    def read(self, path, limit):
        with self.api.open_file(self.path(path), mode="rb", **self.kw) as f:
            data = f.read(limit + 1)
        if len(data) > limit:
            raise NasError("file_too_large", 413)
        return data

    def write(self, path, data):
        destination = self.path(path)
        temporary = destination + ".ryn-upload-" + uuid.uuid4().hex
        # Publish only after the full write. SMB rename refuses an existing destination.
        try:
            with self.api.open_file(temporary, mode="xb", **self.kw) as f:
                f.write(data)
            self.api.rename(temporary, destination, **self.kw)
        finally:
            try:
                self.api.remove(temporary, **self.kw)
            except OSError as exc:
                if exc.errno != errno.ENOENT:
                    raise

    def mkdir(self, path):
        self.api.mkdir(self.path(path), **self.kw)

    def close(self):
        self.api.reset_connection_cache(connection_cache=self.cache)


class WebDAVConnector:
    def __init__(self, config):
        import httpx

        self.root = config["url"].rstrip("/") + "/"
        if config["root"]:
            self.root += quote(config["root"], safe="/") + "/"
        self.client = httpx.Client(
            auth=(config["username"], config["password"]),
            timeout=20,
            follow_redirects=False,
            trust_env=False,
        )

    def url(self, path):
        # A second decode by a server must not turn user input into traversal.
        if "%" in path:
            raise NasError("encoded_path_not_supported")
        return self.root + quote(path, safe="/")

    def request(self, method, path, *, limit=MAX_FILE, **kwargs):
        with self.client.stream(method, self.url(path), **kwargs) as response:
            if response.status_code in (401, 403):
                raise NasError("nas_access_denied", 403)
            if response.status_code == 404:
                raise NasError("file_not_found", 404)
            if response.status_code in (409, 412):
                raise NasError("file_exists_or_parent_missing", 409)
            if not 200 <= response.status_code < 300:
                raise NasError("nas_request_failed", 502)
            data = bytearray()
            for chunk in response.iter_bytes():
                data.extend(chunk)
                if len(data) > limit:
                    raise NasError("file_too_large", 413)
            return bytes(data)

    def list(self, path):
        body = self.request(
            "PROPFIND", path + ("/" if path else ""), headers={"Depth": "1"}, limit=4 * 1024 * 1024
        )
        if b"<!DOCTYPE" in body.upper() or b"<!ENTITY" in body.upper():
            raise NasError("invalid_directory_response", 502)
        tree = ElementTree.fromstring(body)
        root_path = unquote(urlsplit(self.root).path).rstrip("/") + "/"
        result = []
        for item in tree.findall("{DAV:}response"):
            href = item.findtext("{DAV:}href", "")
            decoded = unquote(urlsplit(href).path).rstrip("/")
            if decoded == root_path.rstrip("/"):
                continue
            if not decoded.startswith(root_path):
                continue
            child = relative_path(decoded[len(root_path) :])
            if child == path or child.rpartition("/")[0] != path:
                continue
            prop = item.find("{DAV:}propstat/{DAV:}prop")
            if prop is None:
                continue
            if len(result) >= MAX_ENTRIES:
                raise NasError("directory_too_large", 413)
            result.append(
                {
                    "name": child.rsplit("/", 1)[-1],
                    "path": child,
                    "directory": prop.find("{DAV:}resourcetype/{DAV:}collection") is not None,
                    "size": int(prop.findtext("{DAV:}getcontentlength", "0")),
                    "modified": prop.findtext("{DAV:}getlastmodified", ""),
                }
            )
        return result

    def read(self, path, limit):
        return self.request("GET", path, limit=limit)

    def write(self, path, data):
        self.request("PUT", path, content=data, headers={"If-None-Match": "*"})

    def mkdir(self, path):
        self.request("MKCOL", path)

    def close(self):
        self.client.close()


class NasPlugin:
    def __init__(self, home):
        self.directory = Path(home) / "plugins" / "nas"
        self.lock = threading.RLock()
        self.state = {"enabled": False, "sources": []}
        self.connectors = {"smb": SMBConnector, "webdav": WebDAVConnector}
        self.cipher = None
        state_path = self.directory / "state.enc"
        if state_path.exists():
            self.cipher = Fernet((self.directory / "key").read_bytes())
            self.state = json.loads(self.cipher.decrypt(state_path.read_bytes()))

    def save(self):
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        if self.cipher is None:
            key = Fernet.generate_key()
            with (self.directory / "key").open("xb") as handle:
                handle.write(key)
            os.chmod(self.directory / "key", 0o600)
            self.cipher = Fernet(key)
        data = self.cipher.encrypt(json.dumps(self.state).encode())
        temp = self.directory / (uuid.uuid4().hex + ".tmp")
        temp.write_bytes(data)
        os.chmod(temp, 0o600)
        temp.replace(self.directory / "state.enc")

    def status(self):
        with self.lock:
            return {
                "enabled": self.state["enabled"],
                "systems": SYSTEMS,
                "max_file_bytes": MAX_FILE,
                "sources": [
                    {k: v for k, v in s.items() if k != "password"} for s in self.state["sources"]
                ],
            }

    def enable(self, enabled):
        if not isinstance(enabled, bool):
            raise NasError("invalid_enabled")
        with self.lock:
            self.state["enabled"] = enabled
            self.save()
            return self.status()

    def require_enabled(self):
        if not self.state["enabled"]:
            raise NasError("nas_plugin_disabled", 409)

    def configure(self, body):
        system = next((s for s in SYSTEMS if s["id"] == body.get("system")), None)
        protocol = body.get("protocol", "smb")
        if not system or protocol not in system["protocols"]:
            raise NasError("unsupported_system_or_protocol")
        name = str(body.get("name", "")).strip()
        if not name or len(name) > 80:
            raise NasError("invalid_name")
        config = {
            "id": uuid.uuid4().hex,
            "name": name,
            "system": system["id"],
            "protocol": protocol,
            "root": relative_path(body.get("root", "")),
            "username": str(body.get("username", "")),
            "password": str(body.get("password", "")),
            "writable": body.get("writable") is True,
            "allow_ai": body.get("allow_ai") is True,
        }
        if not config["username"] or not config["password"]:
            raise NasError("credentials_required")
        if protocol == "smb":
            host = str(body.get("host", "")).strip()
            share = str(body.get("share", "")).strip()
            if not re.fullmatch(r"[a-zA-Z0-9_.-]{1,253}", host):
                raise NasError("invalid_host")
            if (
                not share
                or len(share) > 255
                or "/" in share
                or "\\" in share
                or ":" in share
                or any(ord(c) < 32 for c in share)
                or share in {".", ".."}
            ):
                raise NasError("invalid_share")
            try:
                port = int(body.get("port", 445))
            except (TypeError, ValueError) as exc:
                raise NasError("invalid_port") from exc
            if not 1 <= port <= 65535:
                raise NasError("invalid_port")
            config.update(host=host, share=share, port=port)
        else:
            url = str(body.get("url", "")).strip()
            parsed = urlsplit(url)
            if (
                parsed.scheme not in {"http", "https"}
                or not parsed.hostname
                or parsed.username
                or parsed.password
                or parsed.query
                or parsed.fragment
                or "%" in parsed.path
                or "\\" in url
            ):
                raise NasError("invalid_webdav_url")
            relative_path(parsed.path.lstrip("/"))
            config["url"] = url.rstrip("/")
        with self.lock:
            self.require_enabled()
            if len(self.state["sources"]) >= 32:
                raise NasError("too_many_sources")
            with self.connection(config) as connector:
                connector.list("")  # Only save a connection that has passed a real read check.
            self.state["sources"].append(config)
            self.save()
            return {k: v for k, v in config.items() if k != "password"}

    @contextmanager
    def connection(self, config):
        connector = None
        try:
            connector = self.connectors[config["protocol"]](config)
            yield connector
        except NasError:
            raise
        except FileNotFoundError as exc:
            raise NasError("file_not_found", 404) from exc
        except FileExistsError as exc:
            raise NasError("file_exists", 409) from exc
        except PermissionError as exc:
            raise NasError("nas_access_denied", 403) from exc
        except OSError as exc:
            code, status = {
                errno.ENOENT: ("file_not_found", 404),
                errno.EEXIST: ("file_exists", 409),
                errno.EACCES: ("nas_access_denied", 403),
                errno.EPERM: ("nas_access_denied", 403),
            }.get(exc.errno, ("nas_connection_failed", 502))
            raise NasError(code, status) from exc
        except ImportError as exc:
            raise NasError("connector_dependency_missing", 503) from exc
        except Exception as exc:
            # Do not expose library exceptions: they may contain URLs or credentials.
            raise NasError("nas_connection_failed", 502) from exc
        finally:
            if connector:
                try:
                    connector.close()
                except Exception:
                    pass  # Cleanup must not expose credential-bearing transport errors.

    def source(self, source_id):
        self.require_enabled()
        source = next((s for s in self.state["sources"] if s["id"] == source_id), None)
        if not source:
            raise NasError("nas_not_found", 404)
        return source

    def remove(self, source_id):
        with self.lock:
            self.source(source_id)
            self.state["sources"] = [s for s in self.state["sources"] if s["id"] != source_id]
            self.save()

    def operate(self, source_id, action, path="", data=b""):
        path = relative_path(path)
        with self.lock:
            source = self.source(source_id)
            if action in {"write", "mkdir"}:
                if not source["writable"]:
                    raise NasError("nas_read_only", 403)
                if not path:
                    raise NasError("file_path_required")
            if len(data) > MAX_FILE:
                raise NasError("file_too_large", 413)
            if action == "ai" and not source["allow_ai"]:
                raise NasError("nas_ai_not_allowed", 403)
            with self.connection(source) as connector:
                if action == "list":
                    return sorted(
                        connector.list(path),
                        key=lambda e: (not e["directory"], e["name"].casefold()),
                    )
                if action in {"image", "thumbnail"}:
                    return image_preview(connector.read(path, MAX_IMAGE), action == "thumbnail")
                if action in {"read", "preview", "ai"}:
                    raw = connector.read(path, MAX_FILE if action == "read" else MAX_PREVIEW)
                    if action == "read":
                        return raw
                    try:
                        text = raw.decode("utf-8-sig")
                    except UnicodeError as exc:
                        raise NasError("preview_requires_utf8_text", 415) from exc
                    if "\x00" in text:
                        raise NasError("preview_requires_utf8_text", 415)
                    return {"text": text, "name": path.rsplit("/", 1)[-1]}
                if action == "write":
                    connector.write(path, data)
                elif action == "mkdir":
                    connector.mkdir(path)
                else:
                    raise NasError("unsupported_operation")
                return {"ok": True}


def image_preview(raw, thumbnail=False):
    """Decode only raster formats and re-encode bounded previews without metadata."""
    from PIL import Image, ImageOps, UnidentifiedImageError

    try:
        with Image.open(io.BytesIO(raw), formats=["JPEG", "PNG", "WEBP"]) as image:
            if image.width * image.height > MAX_IMAGE_PIXELS:
                raise NasError("image_exceeds_40_megapixels", 413)
            image = ImageOps.exif_transpose(image)
            width, height = image.size
            image.thumbnail((160, 120) if thumbnail else (2560, 2560))
            output = io.BytesIO()
            image.convert("RGBA" if image.has_transparency_data else "RGB").save(
                output, format="WEBP", quality=85
            )
            return output.getvalue(), width, height
    except (Image.DecompressionBombError, Image.DecompressionBombWarning) as exc:
        raise NasError("image_exceeds_40_megapixels", 413) from exc
    except (UnidentifiedImageError, OSError, ValueError) as exc:
        raise NasError("preview_requires_valid_jpeg_png_or_webp", 415) from exc


def mount_nas(app, home, local_control):
    from fastapi import APIRouter, Depends, HTTPException, Request
    from fastapi.responses import Response, StreamingResponse

    # Resolve annotations when FastAPI inspects the locally declared routes.
    globals()["Request"] = Request
    plugin = NasPlugin(home)
    app.state.nas_plugin = plugin
    router = APIRouter(prefix="/api/local/plugins/nas", dependencies=[Depends(local_control)])

    def run(fn, *args):
        try:
            return fn(*args)
        except NasError as exc:
            raise HTTPException(exc.status, str(exc)) from exc

    @router.get("")
    def status():
        return plugin.status()

    @router.put("")
    def enabled(body: dict):
        return run(plugin.enable, body.get("enabled"))

    @router.post("/sources")
    def add(body: dict):
        return run(plugin.configure, body)

    @router.delete("/sources/{source_id}")
    def remove(source_id: str):
        run(plugin.remove, source_id)
        return {"ok": True}

    @router.get("/sources/{source_id}/files")
    def listing(source_id: str, path: str = ""):
        return {"entries": run(plugin.operate, source_id, "list", path)}

    @router.get("/sources/{source_id}/content")
    def content(source_id: str, path: str, mode: str = "download"):
        if mode in {"image", "thumbnail"}:
            data, width, height = run(plugin.operate, source_id, mode, path)
            return Response(
                data,
                media_type="image/webp",
                headers={
                    "Cache-Control": "no-store",
                    "X-Content-Type-Options": "nosniff",
                    "X-Image-Width": str(width),
                    "X-Image-Height": str(height),
                },
            )
        if mode in {"preview", "ai"}:
            return run(plugin.operate, source_id, mode, path)
        if mode != "download":
            raise HTTPException(400, "invalid_mode")
        data = run(plugin.operate, source_id, "read", path)
        return StreamingResponse(
            io.BytesIO(data),
            media_type="application/octet-stream",
            headers={
                "Content-Disposition": "attachment; filename*=UTF-8''"
                + quote(path.rsplit("/", 1)[-1]),
                "Cache-Control": "no-store",
                "X-Content-Type-Options": "nosniff",
            },
        )

    @router.put("/sources/{source_id}/content")
    async def upload(source_id: str, path: str, request: Request):
        data = bytearray()
        async for chunk in request.stream():
            data.extend(chunk)
            if len(data) > MAX_FILE:
                raise HTTPException(413, "file_too_large")
        from starlette.concurrency import run_in_threadpool

        return await run_in_threadpool(run, plugin.operate, source_id, "write", path, bytes(data))

    @router.post("/sources/{source_id}/folders")
    def mkdir(source_id: str, body: dict):
        return run(plugin.operate, source_id, "mkdir", body.get("path", ""))

    app.include_router(router)
