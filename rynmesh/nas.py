"""Optional NAS plugin: bounded file operations behind the local control boundary.

System presets share protocol connectors. They do not claim proprietary API support.
Credentials stay on the node that connects to the NAS, encrypted in its private state.
"""

from __future__ import annotations

import errno
import io
import json
import logging
import os
import re
import shutil
import socket
import ssl
import tempfile
import threading
import time
import uuid
from contextlib import contextmanager, nullcontext
from pathlib import Path
from urllib.parse import quote, unquote, urlsplit
from xml.etree import ElementTree

from cryptography.fernet import Fernet, InvalidToken

from . import os_secret
from .atomic_io import AtomicIOError, atomic_write_bytes
from .private_permissions import ensure_private_file_acl, mark_key_protection

logger = logging.getLogger(__name__)

MAX_FILE = 64 * 1024 * 1024
STREAM_MAX_FILE = 4 * 1024 * 1024 * 1024
STREAM_CHUNK_BYTES = 1024 * 1024
SPOOL_MAX_BYTES = 8 * 1024 * 1024
MAX_CONFIGURABLE_FILE = 64 * 1024 * 1024 * 1024
CLOUD_PROTOCOLS = {"google", "baidu", "onedrive"}
MAX_PREVIEW = 256 * 1024
MAX_IMAGE = 20 * 1024 * 1024
MAX_IMAGE_PIXELS = 40_000_000
MAX_ENTRIES = 5000
PAGE_ENTRIES = 500
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
        self.last_skipped = 0
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

    def _collect_entries(self, path, *, max_entries=None):
        result = []
        skipped = 0
        for entry in self.api.scandir(self.path(path), **self.kw):
            # SMBDirEntry.stat() loses a non-default port in smbprotocol 1.17.
            st = self.api.stat(entry.path, follow_symlinks=False, **self.kw)
            if st.st_file_attributes & 0x400:
                continue
            name = entry.name
            try:
                child = relative_path((path + "/" if path else "") + name)
            except (NasError, ValueError):
                skipped += 1
                continue
            if max_entries is not None and len(result) >= max_entries:
                raise NasError("directory_too_large", 413)
            result.append(
                {
                    "name": name,
                    "path": child,
                    "directory": bool(st.st_file_attributes & 0x10),
                    "size": st.st_size,
                    "modified": st.st_mtime,
                }
            )
        self.last_skipped = skipped
        return result

    def list(self, path):
        return self._collect_entries(path, max_entries=MAX_ENTRIES)

    def list_page(self, path, cursor=""):
        cursor_text = cursor.decode("utf-8") if isinstance(cursor, bytes) else str(cursor or "")
        entries = sorted(self._collect_entries(path, max_entries=None), key=lambda e: e["name"])
        remaining = [e for e in entries if e["name"] > cursor_text] if cursor_text else entries
        page = remaining[:PAGE_ENTRIES]
        next_cursor = page[-1]["name"] if len(remaining) > PAGE_ENTRIES else ""
        return {"entries": page, "next_cursor": next_cursor, "skipped": self.last_skipped}

    def stat_size(self, path):
        st = self.api.stat(self.path(path), follow_symlinks=False, **self.kw)
        if st.st_file_attributes & 0x400:
            raise NasError("links_not_allowed", 403)
        if st.st_file_attributes & 0x10:
            raise NasError("file_not_found", 404)
        return int(st.st_size)

    def read(self, path, limit):
        with self.api.open_file(self.path(path), mode="rb", **self.kw) as f:
            data = f.read(limit + 1)
        if len(data) > limit:
            raise NasError("file_too_large", 413)
        return data

    def iter_read(self, path, chunk=STREAM_CHUNK_BYTES):
        handle = self.api.open_file(self.path(path), mode="rb", **self.kw)

        def _gen():
            try:
                while True:
                    piece = handle.read(chunk)
                    if not piece:
                        break
                    yield piece
            finally:
                handle.close()

        return _gen()

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

    def write_stream(self, path, chunks):
        destination = self.path(path)
        temporary = destination + ".ryn-upload-" + uuid.uuid4().hex
        try:
            with self.api.open_file(temporary, mode="xb", **self.kw) as f:
                for chunk in chunks:
                    if chunk:
                        f.write(chunk)
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


_SSL_CONTEXT_LOCK = threading.Lock()
_HTTP_ONLY_SSL_CONTEXT: ssl.SSLContext | None = None
_HTTPS_DEFAULT_SSL_CONTEXT: ssl.SSLContext | None = None


def _webdav_ssl_context(url: str) -> ssl.SSLContext:
    """Reuse thread-safe SSLContexts so one-shot WebDAV reads do not reload CA bundles per request."""
    global _HTTP_ONLY_SSL_CONTEXT, _HTTPS_DEFAULT_SSL_CONTEXT
    if url.startswith("http://"):
        if _HTTP_ONLY_SSL_CONTEXT is None:
            with _SSL_CONTEXT_LOCK:
                if _HTTP_ONLY_SSL_CONTEXT is None:
                    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
                    ctx.check_hostname = True
                    ctx.verify_mode = ssl.CERT_REQUIRED
                    _HTTP_ONLY_SSL_CONTEXT = ctx
        return _HTTP_ONLY_SSL_CONTEXT
    if _HTTPS_DEFAULT_SSL_CONTEXT is None:
        with _SSL_CONTEXT_LOCK:
            if _HTTPS_DEFAULT_SSL_CONTEXT is None:
                try:
                    import certifi

                    _HTTPS_DEFAULT_SSL_CONTEXT = ssl.create_default_context(cafile=certifi.where())
                except ImportError:
                    _HTTPS_DEFAULT_SSL_CONTEXT = ssl.create_default_context()
    return _HTTPS_DEFAULT_SSL_CONTEXT


class WebDAVConnector:
    def __init__(self, config):
        import httpx

        self.last_skipped = 0
        self.root = config["url"].rstrip("/") + "/"
        if config["root"]:
            self.root += quote(config["root"], safe="/") + "/"
        self.client = httpx.Client(
            auth=(config["username"], config["password"]),
            timeout=config.get("timeout", 20),
            verify=_webdav_ssl_context(self.root),
            follow_redirects=False,
            trust_env=False,
        )

    def url(self, path):
        if not isinstance(path, str):
            raise NasError("invalid_path")
        if path in ("", "/"):
            return self.root
        trailing_slash = path.endswith("/")
        stripped = path.rstrip("/")
        if not stripped:
            raise NasError("invalid_path")
        clean = relative_path(stripped)
        # relative_path already rejects ".."; quote(..., safe="/") encodes any
        # literal "%" into "%25", so one server decode restores the filename
        # without turning encoded sequences into path traversal.
        return self.root + quote(clean, safe="/") + ("/" if trailing_slash else "")

    @staticmethod
    def _check_status(response):
        if response.status_code in (401, 403):
            logging.getLogger("rynmesh.nas").warning("nas operation failed: HTTPStatusError")
            raise NasError("nas_auth_failed", response.status_code)
        if response.status_code == 404:
            raise NasError("file_not_found", 404)
        if response.status_code in (409, 412):
            raise NasError("file_exists_or_parent_missing", 409)
        if not 200 <= response.status_code < 300:
            raise NasError("nas_request_failed", 502)

    def request(self, method, path, *, limit=MAX_FILE, **kwargs):
        with self.client.stream(method, self.url(path), **kwargs) as response:
            self._check_status(response)
            data = bytearray()
            for chunk in response.iter_bytes():
                data.extend(chunk)
                if len(data) > limit:
                    raise NasError("file_too_large", 413)
            return bytes(data)

    def _collect_entries(self, path, *, max_entries=None, limit=4 * 1024 * 1024):
        clean_path = "" if path in ("", "/") else relative_path(path.rstrip("/") if isinstance(path, str) else path)
        body = self.request(
            "PROPFIND", "/" if not clean_path else (clean_path + "/"), headers={"Depth": "1"}, limit=limit
        )
        if b"<!DOCTYPE" in body.upper() or b"<!ENTITY" in body.upper():
            raise NasError("invalid_directory_response", 502)
        tree = ElementTree.fromstring(body)
        root_path = unquote(urlsplit(self.root).path).rstrip("/") + "/"
        result = []
        skipped = 0
        for item in tree.findall("{DAV:}response"):
            href = item.findtext("{DAV:}href", "")
            # Decode once only: an href containing "%2520" decodes to literal "%20".
            decoded = unquote(urlsplit(href).path).rstrip("/")
            if decoded == root_path.rstrip("/"):
                continue
            if not decoded.startswith(root_path):
                continue
            try:
                child = relative_path(decoded[len(root_path) :])
            except (NasError, ValueError):
                skipped += 1
                continue
            if child == clean_path or child.rpartition("/")[0] != clean_path:
                continue
            prop = item.find("{DAV:}propstat/{DAV:}prop")
            if prop is None:
                continue
            if max_entries is not None and len(result) >= max_entries:
                raise NasError("directory_too_large", 413)
            try:
                size = int(prop.findtext("{DAV:}getcontentlength", "0"))
            except (TypeError, ValueError):
                size = 0
            result.append(
                {
                    "name": child.rsplit("/", 1)[-1],
                    "path": child,
                    "directory": prop.find("{DAV:}resourcetype/{DAV:}collection") is not None,
                    "size": size,
                    "modified": prop.findtext("{DAV:}getlastmodified", ""),
                }
            )
        self.last_skipped = skipped
        return result

    def list(self, path):
        return self._collect_entries(path, max_entries=MAX_ENTRIES, limit=4 * 1024 * 1024)

    def list_page(self, path, cursor=""):
        cursor_text = cursor.decode("utf-8") if isinstance(cursor, bytes) else str(cursor or "")
        entries = sorted(
            self._collect_entries(path, max_entries=None, limit=16 * 1024 * 1024),
            key=lambda e: e["name"],
        )
        remaining = [e for e in entries if e["name"] > cursor_text] if cursor_text else entries
        page = remaining[:PAGE_ENTRIES]
        next_cursor = page[-1]["name"] if len(remaining) > PAGE_ENTRIES else ""
        return {"entries": page, "next_cursor": next_cursor, "skipped": self.last_skipped}

    def stat_size(self, path):
        try:
            body = self.request("PROPFIND", path, headers={"Depth": "0"}, limit=256 * 1024)
        except NasError as exc:
            if exc.status in (401, 403, 404):
                raise
            return None
        if b"<!DOCTYPE" in body.upper() or b"<!ENTITY" in body.upper():
            return None
        try:
            tree = ElementTree.fromstring(body)
        except ElementTree.ParseError:
            return None
        for item in tree.findall("{DAV:}response"):
            prop = item.find("{DAV:}propstat/{DAV:}prop")
            if prop is None:
                continue
            if prop.find("{DAV:}resourcetype/{DAV:}collection") is not None:
                continue
            raw_len = prop.findtext("{DAV:}getcontentlength")
            if raw_len is not None:
                try:
                    val = int(raw_len.strip())
                    if val >= 0:
                        return val
                except ValueError:
                    return None
        return None

    def read(self, path, limit):
        return self.request("GET", path, limit=limit)

    def iter_read(self, path, chunk=STREAM_CHUNK_BYTES):
        stream_cm = self.client.stream("GET", self.url(path))
        response = stream_cm.__enter__()
        try:
            self._check_status(response)
        except BaseException:
            stream_cm.__exit__(None, None, None)
            raise
        self.last_content_length = None
        raw_cl = response.headers.get("content-length")
        if raw_cl is not None:
            try:
                parsed_cl = int(raw_cl)
                if parsed_cl >= 0:
                    self.last_content_length = parsed_cl
            except ValueError:
                pass

        def _gen():
            try:
                for piece in response.iter_bytes(chunk):
                    if piece:
                        yield piece
            finally:
                stream_cm.__exit__(None, None, None)

        return _gen()

    def write(self, path, data):
        self.request("PUT", path, content=data, headers={"If-None-Match": "*"})

    def write_stream(self, path, chunks, *, content_length=None):
        headers = {"If-None-Match": "*"}
        if content_length is None:
            content_length = getattr(chunks, "content_length", None)
        if type(content_length) is int and content_length >= 0:
            headers["Content-Length"] = str(content_length)
        self.request("PUT", path, content=(chunk for chunk in chunks if chunk), headers=headers)

    def mkdir(self, path):
        self.request("MKCOL", path)

    def close(self):
        self.client.close()


_SMB_AUTH_NTSTATUS = {
    0xC000006D,  # STATUS_LOGON_FAILURE
    0xC000006A,  # STATUS_WRONG_PASSWORD
    0xC0000072,  # STATUS_ACCOUNT_DISABLED
    0xC0000234,  # STATUS_ACCOUNT_LOCKED_OUT
    0xC0000071,  # STATUS_PASSWORD_EXPIRED
    0xC0000193,  # STATUS_ACCOUNT_EXPIRED
    0xC000006E,  # STATUS_ACCOUNT_RESTRICTION
}


def _classify_nas_exception(exc: BaseException) -> tuple[str, int, bool]:
    """Map connector exceptions to closed NAS error codes without exposing URLs or credentials."""
    if type(exc).__name__ in {"SMBAuthenticationError", "LogonFailure"}:
        return "nas_auth_failed", 401, True
    if getattr(exc, "ntstatus", None) in _SMB_AUTH_NTSTATUS:
        return "nas_auth_failed", 401, True
    resp = getattr(exc, "response", None)
    status_code = getattr(resp, "status_code", None)
    if status_code in (401, 403):
        return "nas_auth_failed", int(status_code), True

    if isinstance(exc, FileNotFoundError):
        return "file_not_found", 404, False
    if isinstance(exc, FileExistsError):
        return "file_exists", 409, False
    if isinstance(exc, PermissionError):
        return "nas_access_denied", 403, False
    if isinstance(exc, ImportError):
        return "connector_dependency_missing", 503, False

    cur: BaseException | None = exc
    for _ in range(4):
        if cur is None:
            break
        if isinstance(cur, socket.gaierror):
            return "nas_dns_failed", 502, True
        if isinstance(cur, ConnectionRefusedError) or (
            isinstance(cur, OSError) and cur.errno == errno.ECONNREFUSED
        ):
            return "nas_unreachable", 502, True
        if isinstance(cur, (TimeoutError, socket.timeout)) or "Timeout" in type(cur).__name__:
            return "nas_timeout", 504, True
        cur = cur.__cause__ or cur.__context__

    if isinstance(exc, OSError):
        mapped = {
            errno.ENOENT: ("file_not_found", 404, False),
            errno.EEXIST: ("file_exists", 409, False),
            errno.EACCES: ("nas_access_denied", 403, False),
            errno.EPERM: ("nas_access_denied", 403, False),
        }.get(exc.errno)
        if mapped is not None:
            return mapped

    return "nas_connection_failed", 502, True


class _SizedChunkIterator:
    def __init__(self, file_obj, content_length: int, chunk_size: int = STREAM_CHUNK_BYTES):
        self._file_obj = file_obj
        self.content_length = content_length
        self._chunk_size = chunk_size

    def __iter__(self):
        self._file_obj.seek(0)
        remaining = self.content_length
        while remaining > 0:
            piece = self._file_obj.read(min(self._chunk_size, remaining))
            if not piece:
                break
            remaining -= len(piece)
            yield piece


class NasPlugin:
    def __init__(self, home):
        self.directory = Path(home) / "plugins" / "nas"
        self.tmp_dir = self.directory / "tmp"
        self._cleanup_tmp_dir()
        self.lock = threading.RLock()
        self.source_locks = {}
        self.listing_locks = {}
        self.sessions = {}
        self.state = {"enabled": False, "sources": [], "max_file_bytes": STREAM_MAX_FILE}
        self.connectors = {"smb": SMBConnector, "webdav": WebDAVConnector}
        self.cipher = None
        self.recovered_from_corruption = None
        state_path = self.directory / "state.enc"
        if state_path.exists():
            key_loaded = False
            try:
                cipher = self._load_cipher()
                key_loaded = True
                loaded = json.loads(cipher.decrypt(state_path.read_bytes()))
                if (
                    not isinstance(loaded, dict)
                    or not isinstance(loaded.get("enabled"), bool)
                    or not isinstance(loaded.get("sources"), list)
                ):
                    raise ValueError("invalid_nas_state")
                if "max_file_bytes" not in loaded:
                    loaded["max_file_bytes"] = STREAM_MAX_FILE
                elif not (
                    type(loaded["max_file_bytes"]) is int
                    and 1 <= loaded["max_file_bytes"] <= MAX_CONFIGURABLE_FILE
                ):
                    raise ValueError("invalid_nas_state")
                self.cipher = cipher
                self.state = loaded
            except (OSError, ValueError, InvalidToken, json.JSONDecodeError) as exc:
                ts = int(time.time())
                self._quarantine_file(state_path, ts)
                if not key_loaded:
                    self._quarantine_file(self.directory / "key.protected", ts)
                    self._quarantine_file(self.directory / "key", ts)
                self.state = {"enabled": False, "sources": [], "max_file_bytes": STREAM_MAX_FILE}
                self.cipher = None
                self.recovered_from_corruption = {"at": ts, "reason": type(exc).__name__}
                logger.warning(
                    "NAS state corrupted (%s); isolated corrupt files and reset state",
                    type(exc).__name__,
                )
        from .cloud_drives import BaiduConnector, CloudDrives, GoogleConnector
        from .onedrive import OneDriveConnector
        self.cloud = CloudDrives(self)
        self.connectors.update(google=lambda c: GoogleConnector(c, self.save),
                               baidu=lambda c: BaiduConnector(c, self.save),
                               onedrive=lambda c: OneDriveConnector(c, self.save))
        from .drive_cache import DirectoryCache
        self.directory_cache = DirectoryCache(self)

    def _cleanup_tmp_dir(self) -> None:
        if not self.tmp_dir.exists():
            return
        try:
            os.chmod(self.tmp_dir, 0o700)
        except OSError:
            pass
        try:
            children = list(self.tmp_dir.iterdir())
        except OSError:
            return
        for child in children:
            try:
                if child.is_dir() and not child.is_symlink():
                    shutil.rmtree(child, ignore_errors=True)
                else:
                    child.unlink(missing_ok=True)
            except OSError:
                pass

    def ensure_tmp_dir(self) -> Path:
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.tmp_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        try:
            os.chmod(self.tmp_dir, 0o700)
        except OSError:
            pass
        return self.tmp_dir

    def max_file_bytes(self) -> int:
        with self.lock:
            raw = self.state.get("max_file_bytes", STREAM_MAX_FILE)
            if type(raw) is int and 1 <= raw <= MAX_CONFIGURABLE_FILE:
                return raw
            return STREAM_MAX_FILE

    def _load_cipher(self):
        protected_path = self.directory / "key.protected"
        legacy_path = self.directory / "key"
        if protected_path.exists() or protected_path.is_symlink():
            raw = os_secret.unprotect(protected_path.read_bytes())
            cipher = Fernet(raw)
            ensure_private_file_acl(protected_path, track_key_status=True, logger=logger)
            if legacy_path.exists() or legacy_path.is_symlink():
                self._backup_legacy_key(legacy_path, int(time.time()))
            return cipher
        if legacy_path.exists() or legacy_path.is_symlink():
            raw = legacy_path.read_bytes()
            cipher = Fernet(raw)
            self._write_private(protected_path, os_secret.protect(raw))
            self._backup_legacy_key(legacy_path, int(time.time()))
            return cipher
        raise FileNotFoundError("nas_key_missing")

    @staticmethod
    def _backup_legacy_key(path: Path, ts: int) -> None:
        if not path.exists() and not path.is_symlink():
            return
        target = path.with_name(f"{path.name}.bak-{ts}")
        counter = 1
        while target.exists() or target.is_symlink():
            target = path.with_name(f"{path.name}.bak-{ts}-{counter}")
            counter += 1
        try:
            path.rename(target)
        except OSError as exc:
            logger.warning("Failed to rename migrated NAS legacy key (%s)", type(exc).__name__)

    @staticmethod
    def _quarantine_file(path: Path, ts: int) -> None:
        if not path.exists() and not path.is_symlink():
            return
        target = path.with_name(f"{path.name}.corrupt-{ts}")
        counter = 1
        while target.exists() or target.is_symlink():
            target = path.with_name(f"{path.name}.corrupt-{ts}-{counter}")
            counter += 1
        try:
            path.rename(target)
        except OSError as exc:
            logger.warning("Failed to isolate corrupt NAS file %s (%s)", path.name, type(exc).__name__)

    @staticmethod
    def public_source(source):
        # An allowlist prevents new token fields from leaking through status.
        allowed = {"id", "name", "system", "protocol", "root", "username", "host",
                   "share", "port", "url", "writable", "allow_ai", "write_authorized"}
        return {k: v for k, v in source.items() if k in allowed}

    def save(self):
        with self.lock:
            self._save()

    @staticmethod
    def _write_private(path: Path, data: bytes) -> None:
        try:
            atomic_write_bytes(path, data)
            mark_key_protection(path, weak=False)
        except AtomicIOError as exc:
            if os.name == "nt" and isinstance(exc.__cause__, OSError):
                atomic_write_bytes(path, data, mode=0o644)
                mark_key_protection(path, weak=True)
                logger.warning("Failed to restrict NAS Windows ACL (%s); wrote with fallback permissions", type(exc.__cause__).__name__)
            else:
                raise

    def _save(self):
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        if self.cipher is None:
            protected_path = self.directory / "key.protected"
            legacy_path = self.directory / "key"
            if (
                protected_path.exists()
                or protected_path.is_symlink()
                or legacy_path.exists()
                or legacy_path.is_symlink()
            ):
                try:
                    self.cipher = self._load_cipher()
                except (OSError, ValueError) as exc:
                    ts = int(time.time())
                    self._quarantine_file(protected_path, ts)
                    self._quarantine_file(legacy_path, ts)
                    logger.warning(
                        "NAS key corrupted (%s); isolated corrupt key and creating a new key",
                        type(exc).__name__,
                    )
            if self.cipher is None:
                key = Fernet.generate_key()
                self._write_private(protected_path, os_secret.protect(key))
                self.cipher = Fernet(key)
        data = self.cipher.encrypt(json.dumps(self.state).encode("utf-8"))
        self._write_private(self.directory / "state.enc", data)

    def status(self):
        with self.lock:
            stream_limit = self.max_file_bytes()
            cloud_limit = min(stream_limit, MAX_FILE)
            by_protocol = {
                "smb": stream_limit,
                "webdav": stream_limit,
                "google": cloud_limit,
                "baidu": cloud_limit,
                "onedrive": cloud_limit,
            }
            sources_out = []
            for s in self.state["sources"]:
                pub = self.public_source(s)
                pub["max_file_bytes"] = by_protocol.get(s.get("protocol", ""), stream_limit)
                sources_out.append(pub)
            result = {
                "enabled": self.state["enabled"],
                "systems": SYSTEMS + [
                    {"id": "google", "name": "Google Drive", "protocols": ["google"], "integration": "Official Drive API"},
                    {"id": "baidu", "name": "百度网盘", "protocols": ["baidu"], "integration": "Official Netdisk API"},
                    {"id": "onedrive", "name": "OneDrive", "protocols": ["onedrive"], "integration": "Microsoft Graph"},
                ],
                "cloud_providers": self.cloud.configured(),
                "max_file_bytes": stream_limit,
                "max_file_bytes_by_protocol": by_protocol,
                "browser_location": self.directory_cache.location if self.state["enabled"] else {},
                "sources": sources_out,
            }
            if self.recovered_from_corruption is not None:
                result["recovered_from_corruption"] = dict(self.recovered_from_corruption)
            return result

    def configure_plugin(self, body):
        if not isinstance(body, dict):
            raise NasError("invalid_enabled")
        has_enabled = "enabled" in body
        has_limit = "max_file_bytes" in body
        if not has_enabled and not has_limit:
            raise NasError("invalid_enabled")
        if has_enabled and not isinstance(body["enabled"], bool):
            raise NasError("invalid_enabled")
        if has_limit:
            limit = body["max_file_bytes"]
            if not (type(limit) is int and 1 <= limit <= MAX_CONFIGURABLE_FILE):
                raise NasError("invalid_max_file_bytes")
            with self.lock:
                self.state["max_file_bytes"] = limit
                self.save()
        if has_enabled:
            return self.enable(body["enabled"])
        return self.status()

    def enable(self, enabled):
        if not isinstance(enabled, bool):
            raise NasError("invalid_enabled")
        manager = getattr(self, "plugin_manager", None)
        if manager:
            from .node_apps.craft_host import CraftError

            try:
                manager.configure_nas(enabled)
            except CraftError as exc:
                raise NasError(str(exc), 409) from None
            return self.status()
        return self._enable(enabled)

    def _enable(self, enabled):
        if not isinstance(enabled, bool):
            raise NasError("invalid_enabled")
        with self.lock:
            if self.state["enabled"] != enabled:
                self.state["access_epoch"] = uuid.uuid4().hex
            self.state["enabled"] = enabled
            self.save()
            return self.status()

    def require_enabled(self):
        if not self.state["enabled"]:
            raise NasError("nas_plugin_disabled", 409)

    @contextmanager
    def access(self):
        """Acquire lifecycle authority before NAS/source locks, never inside them."""
        from .node_apps.craft_host import CraftError

        manager = getattr(self, "plugin_manager", None)
        try:
            with manager.lease("nas") if manager else nullcontext():
                yield
        except CraftError as exc:
            raise NasError(str(exc), 409) from None

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
            "access_generation": uuid.uuid4().hex,
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
            entries = connector.list("")  # Only save a connection that has passed a real read check.
        with self.lock:
            self.require_enabled()
            if len(self.state["sources"]) >= 32:
                raise NasError("too_many_sources")
            self.state["sources"].append(config)
            self.save()
            self.directory_cache.put(config["id"], "", entries)
            return {k: v for k, v in config.items() if k != "password"}

    @contextmanager
    def connection(self, config, reuse=False):
        connector = None
        failed = True
        try:
            session = None
            if reuse:
                with self.lock:
                    session = self.sessions.get(config["id"])
                if session and (session[0] is not config or time.monotonic() - session[2] > 60):
                    self._close_session(config["id"])
                    session = None
            connector = session[1] if session else self.connectors[config["protocol"]](config)
            if reuse:
                with self.lock:
                    self.sessions[config["id"]] = (config, connector, time.monotonic())
            yield connector
            failed = False
        except NasError:
            raise
        except Exception as exc:
            # Do not expose library exceptions: they may contain URLs or credentials.
            code, status, log_warning = _classify_nas_exception(exc)
            if log_warning:
                logging.getLogger("rynmesh.nas").warning("nas operation failed: %s", type(exc).__name__)
            raise NasError(code, status) from exc
        finally:
            if connector and (not reuse or failed):
                if reuse:
                    with self.lock:
                        self.sessions.pop(config["id"], None)
                try:
                    connector.close()
                except Exception:
                    pass  # Cleanup must not expose credential-bearing transport errors.

    def _close_session(self, source_id):
        with self.lock:
            session = self.sessions.pop(source_id, None)
        if session:
            try:
                session[1].close()
            except Exception:
                pass

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
            self.directory_cache.invalidate(source_id)
            self.save()
            self.listing_locks.pop(source_id, None)
            gate = self.source_locks.pop(source_id, None)
        if gate is None or gate.acquire(blocking=False):
            try:
                self._close_session(source_id)
            finally:
                if gate is not None:
                    gate.release()

    def browser_location(self, body):
        with self.lock:
            source_id = str(body.get("source_id", ""))
            self.source(source_id)
            path = relative_path(body.get("path", ""))
            names = body.get("names", {})
            if not isinstance(names, dict) or len(names) > 100 or any(not isinstance(k, str) or not isinstance(v, str) or len(k) > 2100 or len(v) > 512 for k, v in names.items()):
                raise NasError("invalid_path")
            self.directory_cache.location = {"source_id": source_id, "path": path, "names": names}
            self.directory_cache.flush()
            return {"ok": True}

    def listing(self, source_id, path="", cached=False, cursor="", scan=""):
        path = relative_path(path)
        if len(cursor) > 4096 or len(scan) > 64:
            raise NasError("invalid_path")
        started = time.time()
        with self.lock:
            self.source(source_id)
            if cached:
                return self.directory_cache.get(source_id, path) or {"entries": None, "updated_at": 0}
            list_lock = self.listing_locks.setdefault(source_id, threading.RLock())
        with list_lock:
            with self.lock:
                self.source(source_id)
                item = self.directory_cache.get(source_id, path)
                if cursor:
                    if not item or item.get("scan") != scan:
                        raise NasError("drive_listing_changed", 409)
                    if item.get("previous_cursor") == cursor:
                        return item
                    if item.get("next_cursor") != cursor:
                        raise NasError("drive_listing_changed", 409)
                elif item and item["updated_at"] >= started:
                    return item
            page = self.operate(source_id, "list_page", path, cursor)
            with self.lock:
                self.source(source_id)
                entries = (item["entries"] if cursor else []) + page["entries"]
                entries = list({entry["path"]: entry for entry in entries}.values())
                if len(entries) > MAX_ENTRIES or page.get("next_cursor") == cursor and cursor:
                    raise NasError("directory_too_large", 413)
                skipped = (item.get("skipped", 0) if cursor and item else 0) + int(page.get("skipped", 0) or 0)
                return self.directory_cache.put(
                    source_id,
                    path,
                    entries,
                    next_cursor=page.get("next_cursor", ""),
                    previous_cursor=cursor,
                    scan=scan if cursor else uuid.uuid4().hex,
                    skipped=skipped,
                )

    def update_source(self, source_id, body):
        with self.lock:
            source = self.source(source_id)
            name = str(body.get("name", source["name"])).strip()
            if not name or len(name) > 80:
                raise NasError("invalid_name")
            writable = body.get("writable", source["writable"])
            allow_ai = body.get("allow_ai", source["allow_ai"])
            if not isinstance(writable, bool) or not isinstance(allow_ai, bool):
                raise NasError("invalid_permissions")
            if writable and source["protocol"] in {"google", "onedrive"} and not source.get("write_authorized"):
                raise NasError("drive_write_requires_reauthorization", 409)
            if writable != source["writable"] or allow_ai != source["allow_ai"]:
                source["access_generation"] = uuid.uuid4().hex
            source.update(name=name, writable=writable, allow_ai=allow_ai)
            self.save()
            return self.public_source(source)

    def operate(self, source_id, action, path="", data=b""):
        with self.access():
            return self._operate(source_id, action, path, data)

    def _operate(self, source_id, action, path="", data=b""):
        path = relative_path(path)
        write_action = action in {"write", "mkdir"}
        with self.lock:
            self.source(source_id)
            gate = self.source_locks.setdefault(source_id, threading.RLock()) if write_action else None
        with gate if gate is not None else nullcontext():
            with self.lock:
                source = self.source(source_id)
                if write_action:
                    if not source["writable"]:
                        raise NasError("nas_read_only", 403)
                    if not path:
                        raise NasError("file_path_required")
                if len(data) > MAX_FILE:
                    raise NasError("file_too_large", 413)
                if action == "ai" and not source["allow_ai"]:
                    raise NasError("nas_ai_not_allowed", 403)
            try:
                with self.connection(source, reuse=write_action) as connector:
                    result = self._perform(connector, action, path, data)
                with self.lock:
                    if self.source(source_id) is not source:
                        raise NasError("nas_not_found", 404)
                    if action == "list":
                        self.directory_cache.put(source_id, path, result, skipped=getattr(connector, "last_skipped", 0))
                    elif write_action:
                        self.directory_cache.invalidate(source_id, path.rpartition("/")[0])
                return result
            except NasError as exc:
                if exc.status in {401, 403, 404}:
                    with self.lock:
                        self.directory_cache.invalidate(source_id, None if exc.status in {401, 403} else path)
                raise
            finally:
                with self.lock:
                    active = self.state["enabled"] and any(s is source for s in self.state["sources"])
                if not active:
                    self._close_session(source_id)

    def operate_stream(self, source_id, action, path=""):
        if action != "read":
            raise NasError("unsupported_operation")
        path = relative_path(path)
        with self.access():
            with self.lock:
                source = self.source(source_id)
                stream_limit = self.max_file_bytes()
            connector = None
            raw_iter = None
            try:
                connector = self.connectors[source["protocol"]](source)
                if hasattr(connector, "iter_read"):
                    known_size = connector.stat_size(path) if hasattr(connector, "stat_size") else None
                    if type(known_size) is int and known_size > stream_limit:
                        raise NasError("file_too_large", 413)
                    raw_iter = connector.iter_read(path, STREAM_CHUNK_BYTES)
                    if known_size is None:
                        fallback_len = getattr(connector, "last_content_length", None)
                        if type(fallback_len) is int and fallback_len >= 0:
                            known_size = fallback_len
                    if type(known_size) is int and known_size > stream_limit:
                        raise NasError("file_too_large", 413)
                    connector._stream_content_length = known_size
                else:
                    cloud_limit = min(stream_limit, MAX_FILE)
                    data = connector.read(path, cloud_limit)
                    with self.lock:
                        if self.source(source_id) is not source:
                            raise NasError("nas_not_found", 404)
                    try:
                        connector.close()
                    except Exception:
                        pass
                    connector._stream_content_length = len(data)
                    return connector, iter((data,))
            except NasError as exc:
                if raw_iter is not None and hasattr(raw_iter, "close"):
                    try:
                        raw_iter.close()
                    except Exception:
                        pass
                if connector is not None:
                    try:
                        connector.close()
                    except Exception:
                        pass
                if exc.status in {401, 403, 404}:
                    with self.lock:
                        self.directory_cache.invalidate(source_id, None if exc.status in {401, 403} else path)
                raise
            except Exception as exc:
                if raw_iter is not None and hasattr(raw_iter, "close"):
                    try:
                        raw_iter.close()
                    except Exception:
                        pass
                if connector is not None:
                    try:
                        connector.close()
                    except Exception:
                        pass
                code, status, log_warning = _classify_nas_exception(exc)
                if status in {401, 403, 404}:
                    with self.lock:
                        self.directory_cache.invalidate(source_id, None if status in {401, 403} else path)
                if log_warning:
                    logging.getLogger("rynmesh.nas").warning("nas operation failed: %s", type(exc).__name__)
                raise NasError(code, status) from exc

            def _stream():
                total = 0
                try:
                    for chunk in raw_iter:
                        with self.lock:
                            if not self.state["enabled"] or not any(s is source for s in self.state["sources"]):
                                raise NasError("nas_plugin_disabled", 409)
                        total += len(chunk)
                        if total > stream_limit:
                            raise NasError("file_too_large", 413)
                        yield chunk
                finally:
                    if hasattr(raw_iter, "close"):
                        try:
                            raw_iter.close()
                        except Exception:
                            pass
                    try:
                        connector.close()
                    except Exception:
                        pass

            return connector, _stream()

    def prepare_upload(self, source_id, path):
        path = relative_path(path)
        if not path:
            raise NasError("file_path_required")
        with self.access():
            with self.lock:
                source = self.source(source_id)
                if not source["writable"]:
                    raise NasError("nas_read_only", 403)
                protocol = source["protocol"]
                limit = self.max_file_bytes()
                if protocol in CLOUD_PROTOCOLS:
                    return min(limit, MAX_FILE)
                connector_cls = self.connectors.get(protocol)
                if isinstance(connector_cls, type) and not hasattr(connector_cls, "write_stream"):
                    return min(limit, MAX_FILE)
                return limit

    def operate_stream_write(self, source_id, path, file_obj, size=None):
        path = relative_path(path)
        if not path:
            raise NasError("file_path_required")
        if size is None:
            file_obj.seek(0, io.SEEK_END)
            size = file_obj.tell()
            file_obj.seek(0)
        with self.access():
            with self.lock:
                self.source(source_id)
                gate = self.source_locks.setdefault(source_id, threading.RLock())
            with gate:
                with self.lock:
                    source = self.source(source_id)
                    if not source["writable"]:
                        raise NasError("nas_read_only", 403)
                    stream_limit = self.max_file_bytes()
                try:
                    with self.connection(source, reuse=True) as connector:
                        if hasattr(connector, "write_stream"):
                            if size > stream_limit:
                                raise NasError("file_too_large", 413)
                            chunks = _SizedChunkIterator(file_obj, size, STREAM_CHUNK_BYTES)
                            created = connector.write_stream(path, chunks)
                            result = self._format_mutation_result(connector, "write", path, size, created)
                        else:
                            cloud_limit = min(stream_limit, MAX_FILE)
                            if size > cloud_limit:
                                raise NasError("file_too_large", 413)
                            file_obj.seek(0)
                            raw = file_obj.read(cloud_limit + 1)
                            if len(raw) > cloud_limit:
                                raise NasError("file_too_large", 413)
                            result = self._perform(connector, "write", path, raw)
                    with self.lock:
                        if self.source(source_id) is not source:
                            raise NasError("nas_not_found", 404)
                        self.directory_cache.invalidate(source_id, path.rpartition("/")[0])
                    return result
                except NasError as exc:
                    if exc.status in {401, 403, 404}:
                        with self.lock:
                            self.directory_cache.invalidate(source_id, None if exc.status in {401, 403} else path)
                    raise
                finally:
                    with self.lock:
                        active = self.state["enabled"] and any(s is source for s in self.state["sources"])
                    if not active:
                        self._close_session(source_id)

    @staticmethod
    def _perform(connector, action, path, data):
        if action == "list_page":
            cursor_arg = data.decode("utf-8") if isinstance(data, bytes) else data
            if hasattr(connector, "list_page"):
                page = connector.list_page(path, cursor_arg)
            else:
                page = {"entries": connector.list(path), "next_cursor": ""}
            page.setdefault("skipped", getattr(connector, "last_skipped", 0))
            return page
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
            created = connector.write(path, data)
        elif action == "mkdir":
            created = connector.mkdir(path)
        else:
            raise NasError("unsupported_operation")
        return NasPlugin._format_mutation_result(connector, action, path, len(data) if action == "write" else 0, created)

    @staticmethod
    def _format_mutation_result(connector, action, path, size, created):
        if getattr(connector, "config", {}).get("protocol") in {"google", "onedrive"}:
            if not isinstance(created, dict) or not created.get("id"):
                return {"ok": True}
            parent = path.rpartition("/")[0]
            actual_path = (parent + "/" if parent else "") + created["id"]
        else:
            actual_path = path
        return {
            "ok": True,
            "entry": {
                "name": path.rsplit("/", 1)[-1],
                "path": actual_path,
                "directory": action == "mkdir",
                "size": size if action == "write" else 0,
                "modified": time.time(),
            },
        }



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
        return run(plugin.configure_plugin, body)

    @router.post("/sources")
    def add(body: dict):
        return run(plugin.configure, body)

    @router.put("/cloud/apps/{provider}")
    def configure_cloud(provider: str, body: dict):
        return run(plugin.cloud.configure, provider, body)

    @router.post("/cloud/auth/{provider}")
    def authorize_cloud(provider: str, body: dict):
        return run(plugin.cloud.start, provider, body)

    @router.get("/cloud/auth-status/{auth_id}")
    def cloud_auth_status(auth_id: str):
        return run(plugin.cloud.poll, auth_id)

    @router.delete("/cloud/auth-status/{auth_id}")
    def cancel_cloud_auth(auth_id: str):
        return run(plugin.cloud.cancel, auth_id)

    @router.patch("/sources/{source_id}")
    def update_source(source_id: str, body: dict):
        return run(plugin.update_source, source_id, body)

    @router.delete("/sources/{source_id}")
    def remove(source_id: str):
        run(plugin.remove, source_id)
        return {"ok": True}

    @router.put("/browser-location")
    def browser_location(body: dict):
        return run(plugin.browser_location, body)

    @router.get("/sources/{source_id}/files")
    def listing(source_id: str, path: str = "", cached: bool = False, cursor: str = "", scan: str = ""):
        return run(plugin.listing, source_id, path, cached, cursor, scan)

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
        connector, stream = run(plugin.operate_stream, source_id, "read", path)
        headers = {
            "Content-Disposition": "attachment; filename*=UTF-8''"
            + quote(path.rsplit("/", 1)[-1]),
            "Cache-Control": "no-store",
            "X-Content-Type-Options": "nosniff",
        }
        content_length = getattr(connector, "_stream_content_length", None)
        if type(content_length) is int and content_length >= 0:
            headers["Content-Length"] = str(content_length)

        async def _body_stream():
            from starlette.concurrency import iterate_in_threadpool, run_in_threadpool

            try:
                async for chunk in iterate_in_threadpool(stream):
                    yield chunk
            finally:
                if hasattr(stream, "close"):
                    await run_in_threadpool(stream.close)

        return StreamingResponse(
            _body_stream(),
            media_type="application/octet-stream",
            headers=headers,
        )

    @router.put("/sources/{source_id}/content")
    async def upload(source_id: str, path: str, request: Request):
        from starlette.concurrency import run_in_threadpool

        limit = run(plugin.prepare_upload, source_id, path)
        declared = request.headers.get("content-length")
        if declared is not None:
            try:
                if int(declared) > limit:
                    raise HTTPException(413, "file_too_large")
            except ValueError:
                pass
        tmp_dir = run(plugin.ensure_tmp_dir)
        spool = tempfile.SpooledTemporaryFile(max_size=SPOOL_MAX_BYTES, mode="w+b", dir=str(tmp_dir))
        try:
            total = 0
            async for chunk in request.stream():
                if not chunk:
                    continue
                total += len(chunk)
                if total > limit:
                    raise HTTPException(413, "file_too_large")
                if total > SPOOL_MAX_BYTES:
                    await run_in_threadpool(spool.write, chunk)
                else:
                    spool.write(chunk)
            spool.seek(0)
            return await run_in_threadpool(run, plugin.operate_stream_write, source_id, path, spool, total)
        finally:
            spool.close()

    @router.post("/sources/{source_id}/folders")
    def mkdir(source_id: str, body: dict):
        return run(plugin.operate, source_id, "mkdir", body.get("path", ""))

    app.include_router(router)
