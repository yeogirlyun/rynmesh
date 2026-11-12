"""Keeps installer-provided local plugin runtimes ("service packs") current.

A distribution's installer registers runtimes in ``plugin-services/services.json``
(the desktop shell runs them) and may give each one an update source in
``plugin-services/updates.json``::

    {"schema": 1, "services": {"<id>": {
        "name": "<display name>" | {"zh-CN": "...", "en": "..."},
        "plugins": ["<plugin id>", ...],
        "url": "https://host/path/latest.json",
        "public_key": "<base64 Ed25519 publisher key>",
        "publisher": "<publisher label>" | {"zh-CN": "...", "en": "..."},
        "auth": {
            "peer_provider": "<plugin id>",
            "label": "<account label>" | {"zh-CN": "...", "en": "..."}
        }}}}

The publisher key only ever comes from that local file. The node checks the
signed release manifest hourly (15 minutes after a failure), downloads only
files whose content changed (content-addressed ``blobs/<sha256>`` next to the
manifest), verifies every file and stages the release in a new versioned
directory: the program, its plugin declarations and its skill files together.
The active runtime is never modified.

A staged release is switched in when Ryn starts, when the owner quits Ryn (if
"install when Ryn quits" is on) or when the owner chooses "Update now"; while
the runtime's plugins have tasks running, automatic switching is deferred and
status reports "waiting" (the owner can still click "Update now" to force an
immediate switch). The switch first applies the release's declarations, skills,
and read-only connection trust, then updates ``services.json``; the shell
restarts the runtime immediately. A release whose runtime does not report its
revision as healthy within 180 seconds is rolled back, with the previous
release's declarations and skills, and that revision is never retried. A release
never replaces a runtime of the same or a newer version, and is not downloaded
when it requires a newer Ryn.

``auth.peer_provider`` names a plugin whose sign-in authorizes the download; its
token is only ever sent to the origin it was issued for, and every download and
redirect must stay on the manifest's origin.
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import os
import re
import shutil
import ssl
import threading
import time
import urllib.error
import urllib.request
from contextlib import asynccontextmanager
from pathlib import Path, PurePosixPath
from urllib.parse import urljoin, urlsplit

import certifi
from fastapi import HTTPException, Request
from packaging.version import InvalidVersion, Version

from .. import __version__
from ..atomic_io import atomic_write_json
from ..crypto import SignatureError, SignedPayload, verify_signed_payload
from ..file_transactions import file_transaction

CHECK_INTERVAL = 60 * 60
RETRY_INTERVAL = 15 * 60
POLL_INTERVAL = 60
FAST_POLL = 2
VERIFY_TIMEOUT = 180
MANIFEST_LIMIT = 4 << 20
MAX_FILES = 20000
MAX_TOTAL = 4 << 30
RUNTIME_MANIFEST = "runtime-manifest.json"
DECLARATIONS = "ryn-declarations.json"
SKILLS = "ryn-skills"
SAFE_ID = re.compile(r"[A-Za-z0-9._-]{1,128}")
REVISION = re.compile(r"[0-9a-f]{16,64}")
SHA256 = re.compile(r"[0-9a-f]{64}")
RESERVED = {"con", "prn", "aux", "nul", *(f"com{i}" for i in range(1, 10)), *(f"lpt{i}" for i in range(1, 10))}
STATE_ERRORS = {"service_update_login_required": "login_required"}


class ServiceUpdateError(ValueError):
    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


def fail(code: str):
    raise ServiceUpdateError(code)


def https_url(value):
    if not isinstance(value, str) or len(value) > 4096 or "\\" in value or any(ord(c) < 33 for c in value):
        fail("service_update_invalid_url")
    try:
        url = urlsplit(value)
        url.port
    except ValueError:
        fail("service_update_invalid_url")
    if url.scheme != "https" or not url.hostname or url.username or url.password or url.fragment:
        fail("service_update_invalid_url")
    return url


def origin(value) -> tuple[str, int]:
    """Scheme is always https; an explicit :443 is the same origin as none."""
    url = https_url(value)
    return url.hostname.lower(), url.port or 443


def safe_path(value) -> str:
    """A relative file path inside a runtime directory, in canonical form."""
    if (not isinstance(value, str) or not value or len(value) > 512 or "\\" in value or ":" in value
            or value.startswith("/") or any(ord(c) < 32 for c in value)):
        fail("service_update_invalid_path")
    parts = PurePosixPath(value).parts
    if "/".join(parts) != value or any(p in ("", ".", "..") or p.endswith((" ", ".")) for p in parts):
        fail("service_update_invalid_path")
    name = parts[-1].lower()
    if any(p.lower().split(".")[0] in RESERVED for p in parts) or name == RUNTIME_MANIFEST:
        fail("service_update_invalid_path")
    if Path(name).suffix in {".db", ".key"} or name == ".env" or name.endswith("private.json"):
        fail("service_update_private_file")
    return value


def read_json(path: Path, limit: int):
    try:
        if path.stat().st_size > limit:
            fail("service_update_config_invalid")
        return json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        fail("service_update_config_invalid")


def localized_label(value):
    if value is None:
        return None
    if isinstance(value, str) and 0 < len(value) <= 120:
        return value
    if (isinstance(value, dict) and 0 < len(value) <= 8
            and all(isinstance(k, str) and len(k) <= 16 and isinstance(v, str) and 0 < len(v) <= 120
                    for k, v in value.items())):
        return dict(value)
    fail("service_update_config_invalid")


def validate_source(service_id, source) -> dict:
    allowed = {"url", "public_key", "auth", "name", "plugins", "publisher"}
    if (not SAFE_ID.fullmatch(str(service_id)) or not isinstance(source, dict)
            or set(source) - allowed):
        fail("service_update_config_invalid")
    https_url(source.get("url"))
    try:
        if len(base64.b64decode(source.get("public_key", ""), validate=True)) != 32:
            raise ValueError
    except (TypeError, ValueError):
        fail("service_update_invalid_public_key")
    auth = source.get("auth") or {}
    if not isinstance(auth, dict) or set(auth) - {"peer_provider", "label"}:
        fail("service_update_config_invalid")
    provider = auth.get("peer_provider")
    if provider is not None and not SAFE_ID.fullmatch(str(provider)):
        fail("service_update_config_invalid")
    auth_out = {}
    if provider is not None:
        auth_out["peer_provider"] = str(provider)
    if "label" in auth and auth["label"] is not None:
        auth_out["label"] = localized_label(auth["label"])
    plugins_raw = source.get("plugins")
    plugins = None
    if plugins_raw is not None:
        if (not isinstance(plugins_raw, list) or len(plugins_raw) > 100
                or any(not isinstance(p, str) or not SAFE_ID.fullmatch(p) for p in plugins_raw)):
            fail("service_update_config_invalid")
        plugins = list(plugins_raw)
    result = {
        "url": source["url"],
        "public_key": source["public_key"],
        "auth": auth_out,
        "name": localized_label(source.get("name")),
    }
    if plugins is not None:
        result["plugins"] = plugins
    if source.get("publisher") is not None:
        result["publisher"] = localized_label(source.get("publisher"))
    return result


def parse_version(value):
    try:
        return Version(value) if isinstance(value, str) and len(value) <= 64 else None
    except InvalidVersion:
        return None


def verify_manifest(raw, source, service_id) -> dict:
    """Return the verified release payload; everything else is rejected."""
    try:
        signed = SignedPayload.from_dict(raw)
        # The key comes from the local configuration, never from the manifest.
        if signed.public_key != source["public_key"]:
            fail("service_update_untrusted_signer")
        verify_signed_payload(signed)
    except ServiceUpdateError:
        raise
    except (SignatureError, KeyError, TypeError, ValueError, AttributeError):
        fail("service_update_bad_signature")
    p = signed.payload
    allowed = {"kind", "schema", "id", "version", "revision", "min_host", "program", "files",
               "declarations", "skills", "notes"}
    if (not isinstance(p, dict) or set(p) - allowed or p.get("kind") != "ryn.service.release"
            or p.get("schema") != 1 or p.get("id") != service_id):
        fail("service_update_invalid_manifest")
    if parse_version(p.get("version")) is None or (p.get("min_host") is not None and parse_version(p["min_host"]) is None):
        fail("service_update_invalid_manifest")
    if not isinstance(p.get("revision"), str) or not REVISION.fullmatch(p["revision"]):
        fail("service_update_invalid_manifest")
    files = p.get("files")
    if not isinstance(files, dict) or not 0 < len(files) <= MAX_FILES:
        fail("service_update_invalid_manifest")
    total, folded = 0, set()
    for path, meta in files.items():
        safe_path(path)
        if path.lower() in folded:  # one file per case-insensitive path on Windows
            fail("service_update_invalid_path")
        folded.add(path.lower())
        if (not isinstance(meta, dict) or set(meta) != {"sha256", "size"} or not isinstance(meta["sha256"], str)
                or not SHA256.fullmatch(meta["sha256"]) or type(meta["size"]) is not int or meta["size"] < 0):
            fail("service_update_invalid_manifest")
        total += meta["size"]
    if total > MAX_TOTAL:
        fail("service_update_too_large")
    if p.get("program") not in files or (p.get("declarations") is not None and (
            p["declarations"] not in files or not p["declarations"].endswith(".json"))):
        fail("service_update_invalid_manifest")
    if p.get("skills") is not None:
        safe_path(p["skills"])
    if p.get("notes") is not None and (not isinstance(p["notes"], str) or len(p["notes"]) > 4000):
        fail("service_update_invalid_manifest")
    return p


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(1 << 20):
            digest.update(chunk)
    return digest.hexdigest()


def default_health(port: int, path: str):
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}{path}", timeout=2) as response:
            body = response.read(65536)
            try:
                value = json.loads(body or b"{}")
                return response.status, value if isinstance(value, dict) else {}
            except ValueError:
                return response.status, {}
    except urllib.error.HTTPError as exc:
        return exc.code, {}
    except (OSError, ValueError):
        return None, {}


def read_preferences(path=None) -> dict:
    """The desktop's update switches; both default to on (as in the shell)."""
    value = {}
    path = path or os.environ.get("RYN_DESKTOP_UPDATE_PREFERENCES")
    if path:
        try:
            value = json.loads(Path(path).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            value = {}
    if not isinstance(value, dict):
        value = {}
    return {"auto_download": value.get("autoDownload") is not False,
            "install_on_quit": value.get("installOnQuit") is not False}


class ServiceUpdates:
    def __init__(self, root, *, token_source=None, extensions=None, health=default_health, activity=None,
                 preferences=read_preferences, opener_factory=None, clock=time.time, host_version=None,
                 verify_timeout=None):
        self.root = Path(root) if root else None
        self.token_source = token_source      # plugin id -> (token, resource)
        self.extensions = extensions          # () -> ExtensionStore or None
        self.health = health
        self.activity = activity or (lambda: None)  # () -> reason Ryn itself is busy, or None
        self.preferences = preferences
        self.clock = clock
        self.host_version = host_version or os.environ.get("RYN_SERVICE_UPDATE_HOST_VERSION") or __version__
        self.verify_timeout = (
            float(verify_timeout) if verify_timeout is not None
            else float(os.environ.get("RYN_SERVICE_UPDATE_VERIFY_TIMEOUT") or VERIFY_TIMEOUT)
        )
        self.opener_factory = opener_factory or self._opener
        self.lock = threading.RLock()
        self.working = threading.Lock()
        self.progress: dict[str, tuple[int, int]] = {}
        self._busy_cache: dict[str, tuple[float, str | None]] = {}
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._thread = None

    # -- files -------------------------------------------------------------
    @property
    def enabled(self):
        return self.root is not None and (self.root / "updates.json").is_file()

    def _lock_path(self):
        return self.root / ".install.lock"

    def sources(self) -> dict:
        if not self.enabled:
            return {}
        data = read_json(self.root / "updates.json", 65536)
        if (not isinstance(data, dict) or set(data) != {"schema", "services"} or data["schema"] != 1
                or not isinstance(data["services"], dict)):
            fail("service_update_config_invalid")
        return {sid: validate_source(sid, source) for sid, source in data["services"].items()}

    def _config(self) -> dict:
        path = self.root / "services.json"
        config = read_json(path, 65536) if path.exists() else {"version": 1, "services": []}
        if not isinstance(config, dict) or config.get("version") != 1 or not isinstance(config.get("services"), list):
            fail("service_update_config_invalid")
        return config

    def _entry(self, service_id, config=None):
        for service in (config or self._config())["services"]:
            if isinstance(service, dict) and service.get("id") == service_id:
                return service
        fail("service_update_service_missing")

    def _state(self) -> dict:
        path = self.root / "update-state.json"
        try:
            value = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
        except (OSError, ValueError):
            value = {}
        return value if isinstance(value, dict) else {}

    def _row(self, service_id) -> dict:
        row = self._state().get(service_id)
        return row if isinstance(row, dict) else {}

    def _put(self, service_id, **fields):
        with self.lock:
            state = self._state()
            row = state.setdefault(service_id, {})
            for key, value in fields.items():
                if value is None:
                    row.pop(key, None)
                else:
                    row[key] = value
            atomic_write_json(self.root / "update-state.json", state)
            return dict(row)

    def _dir_of(self, program) -> Path:
        return self.root / PurePosixPath(str(program).replace("\\", "/")).parent

    def _active_dir(self, entry) -> Path:
        return self._dir_of(entry["program"])

    @staticmethod
    def _runtime(directory: Path) -> dict:
        try:
            value = json.loads((directory / RUNTIME_MANIFEST).read_text(encoding="utf-8"))
            return value if isinstance(value, dict) else {}
        except (OSError, ValueError):
            return {}

    @staticmethod
    def _not_newer(current: dict, version) -> bool:
        """True when the installed runtime is the same or a newer version."""
        installed, offered = parse_version(current.get("version")), parse_version(version)
        return installed is not None and offered is not None and installed >= offered

    def _host_too_old(self, min_host) -> bool:
        required, running = parse_version(min_host), parse_version(self.host_version)
        return required is not None and running is not None and running < required

    # -- network -----------------------------------------------------------
    @staticmethod
    def _opener(allowed):
        context = ssl.create_default_context(cafile=certifi.where())
        extra = os.environ.get("RYN_SERVICE_UPDATE_CA_BUNDLE")  # test servers only
        if extra:
            context.load_verify_locations(cafile=extra)

        class Redirect(urllib.request.HTTPRedirectHandler):
            def redirect_request(self, req, fp, code, msg, headers, newurl):
                allowed(newurl)
                return super().redirect_request(req, fp, code, msg, headers, newurl)
        return urllib.request.build_opener(Redirect(), urllib.request.HTTPSHandler(context=context))

    def _token(self, source):
        provider = source["auth"].get("peer_provider")
        if not provider:
            return None
        if self.token_source is None:
            fail("service_update_login_required")
        try:
            token, resource = self.token_source(provider)
        except ServiceUpdateError:
            raise
        except Exception:
            fail("service_update_login_required")
        if not isinstance(token, str) or not token or any(c in token for c in "\r\n"):
            fail("service_update_login_required")
        # A sign-in is only ever presented to the server that issued it.
        try:
            same = origin(resource) == origin(source["url"])
        except ServiceUpdateError:
            same = False
        if not same:
            fail("service_update_auth_origin_mismatch")
        return token

    def _get(self, source, url, token, *, destination=None, limit=MANIFEST_LIMIT, expect=None):
        home = origin(source["url"])

        def allowed(target):
            try:
                same = origin(target) == home
            except ServiceUpdateError:
                same = False
            if not same:
                fail("service_update_download_origin_rejected")
        allowed(url)
        headers = {"User-Agent": "Ryn-Service-Updater/1", "Accept": "application/octet-stream, application/json"}
        if token:
            headers["Authorization"] = "Bearer " + token
        try:
            with self.opener_factory(allowed).open(urllib.request.Request(url, headers=headers), timeout=30) as response:
                if int(response.headers.get("Content-Length") or 0) > limit:
                    fail("service_update_download_too_large")
                digest, data, size = hashlib.sha256(), bytearray(), 0
                deadline = time.monotonic() + max(600, limit / (256 * 1024))
                output = destination.open("xb") if destination else None
                try:
                    while chunk := response.read(1 << 16):
                        size += len(chunk)
                        if size > limit or time.monotonic() > deadline:
                            fail("service_update_download_too_large")
                        digest.update(chunk)
                        if output:
                            output.write(chunk)
                        else:
                            data.extend(chunk)
                finally:
                    if output:
                        output.close()
                if expect and (digest.hexdigest() != expect[0] or size != expect[1]):
                    fail("service_update_checksum_mismatch")
                self._received = getattr(self, "_received", 0) + size
                return bytes(data)
        except urllib.error.HTTPError as exc:
            if exc.code in (401, 403):
                fail("service_update_login_required")
            fail("service_update_not_published" if exc.code == 404 else "service_update_server_error")
        except ServiceUpdateError:
            raise
        except (OSError, ValueError):
            fail("service_update_download_failed")

    # -- check and stage ---------------------------------------------------
    def check(self, service_id):
        """Fetch the manifest and stage a newer release; never touches the active runtime."""
        if not self.working.acquire(blocking=False):
            fail("service_update_busy")
        try:
            source = self.sources().get(service_id) or fail("service_update_not_configured")
            entry = self._entry(service_id)
            now = self.clock()
            before = self._row(service_id)
            if before.get("verify"):
                return before  # A switched release is being confirmed first.
            self._put(service_id, state="checking", last_check=now)
            try:
                token = self._token(source)
                raw = json.loads(self._get(source, source["url"], token))
                payload = verify_manifest(raw, source, service_id)
                active = self._active_dir(entry)
                current = self._runtime(active)
                row = self._row(service_id)
                done = dict(last_error=None, next_check=now + CHECK_INTERVAL, min_host=None)
                if current.get("source_commit") == payload["revision"] or self._not_newer(current, payload["version"]):
                    # Never a downgrade, also after a full package reinstall.
                    stale = (row.get("staged") or {}).get("revision") != payload["revision"]
                    return self._put(service_id, state="up_to_date", available_version=None,
                                     rolled_back=None, **({"staged": None} if stale else {}), **done)
                if payload["revision"] in row.get("quarantined", []):
                    return self._put(service_id, state="rolled_back", available_version=payload["version"],
                                     rolled_back={"version": payload["version"]}, next_check=now + CHECK_INTERVAL)
                if self._host_too_old(payload.get("min_host")):
                    return self._put(service_id, state="requires_host", available_version=payload["version"],
                                     last_error=None, next_check=now + CHECK_INTERVAL, min_host=payload["min_host"])
                staged = row.get("staged") or {}
                if staged.get("revision") == payload["revision"] and self._runtime(self.root / staged["directory"]).get(
                        "source_commit") == payload["revision"]:
                    busy_reason = self._busy(entry)
                    next_state = "waiting" if (row.get("requested") or busy_reason) else "ready"
                    return self._put(service_id, state=next_state, waiting_for=busy_reason, **done)
                self._put(service_id, state="downloading", available_version=payload["version"])
                self._received = 0
                directory = self._stage(service_id, source, token, payload, active)
                staged = {"version": payload["version"], "revision": payload["revision"], "directory": directory.name,
                          "program": payload["program"], "declarations": payload.get("declarations"),
                          "skills": payload.get("skills"), "notes": payload.get("notes"),
                          "min_host": payload.get("min_host"), "downloaded_bytes": self._received}
                busy_reason = self._busy(entry)
                next_state = "waiting" if busy_reason else "ready"
                return self._put(service_id, state=next_state, staged=staged, requested=None,
                                 waiting_for=busy_reason, rolled_back=None, **done)
            except Exception as exc:
                code = exc.code if isinstance(exc, ServiceUpdateError) else "service_update_failed"
                state = STATE_ERRORS.get(code, "error")
                staged = self._row(service_id).get("staged")
                if staged and state in ("error", "login_required"):
                    # A release already downloaded stays ready; this check is retried.
                    state = "waiting" if self._row(service_id).get("requested") else "ready"
                self._put(service_id, state=state, last_error=code, next_check=now + RETRY_INTERVAL)
                raise
            finally:
                self.progress.pop(service_id, None)
        finally:
            self.working.release()

    def _stage(self, service_id, source, token, payload, active: Path) -> Path:
        name = f"{service_id}-{payload['revision'][:16]}"
        target = self.root / name
        if target.resolve() == active.resolve():
            fail("service_update_invalid_manifest")
        if target.exists():
            if self._runtime(target).get("source_commit") == payload["revision"]:
                return target
            shutil.rmtree(target)
        staging = self.root / f".{name}.staging"
        shutil.rmtree(staging, ignore_errors=True)
        staging.mkdir(parents=True)
        base = urljoin(source["url"], "blobs/")
        total = sum(meta["size"] for meta in payload["files"].values())
        done = 0
        for path, meta in payload["files"].items():
            destination = staging.joinpath(*PurePosixPath(path).parts)
            destination.parent.mkdir(parents=True, exist_ok=True)
            reusable = active.joinpath(*PurePosixPath(path).parts)
            # Unchanged files come from the active runtime; every copy is verified.
            if reusable.is_file() and not reusable.is_symlink() and reusable.stat().st_size == meta["size"]:
                shutil.copy2(reusable, destination)
                if sha256_file(destination) == meta["sha256"]:
                    done += meta["size"]
                    self.progress[service_id] = (done, total)
                    continue
                destination.unlink()
            self._get(source, base + meta["sha256"], token, destination=destination,
                      limit=meta["size"], expect=(meta["sha256"], meta["size"]))
            done += meta["size"]
            self.progress[service_id] = (done, total)
        atomic_write_json(staging / RUNTIME_MANIFEST, {
            "schema": 1, "service": service_id, "source_commit": payload["revision"], "version": payload["version"],
            "files": {path: meta["sha256"] for path, meta in payload["files"].items()}})
        os.replace(staging, target)
        return target

    # -- switch, verify, roll back -------------------------------------------
    def _busy(self, entry) -> str | None:
        reason = self.activity()
        if reason:
            return reason
        if not self._expected_running(entry):
            return None
        status, body = self.health(int(entry["port"]), entry["health_path"])
        return "plugin_busy" if status == 200 and body.get("busy") is True else None

    def _busy_cached(self, service_id: str, entry: dict) -> str | None:
        reason = self.activity()
        if reason:
            return reason
        now = self.clock()
        cached = self._busy_cache.get(service_id)
        if cached and now - cached[0] < 1.5:
            return cached[1]
        if not self._expected_running(entry):
            self._busy_cache[service_id] = (now, None)
            return None
        status, body = self.health(int(entry["port"]), entry["health_path"])
        result = "plugin_busy" if status == 200 and body.get("busy") is True else None
        self._busy_cache[service_id] = (now, result)
        return result

    def apply(self, service_id, *, force=False):
        """Switch a staged release in. While the runtime is busy an automatic switch
        waits, unless the owner clicks Update now (``force=True``)."""
        with self.lock:
            row = self._row(service_id)
            staged = row.get("staged")
            if not staged:
                fail("service_update_not_ready")
            directory = self.root / staged["directory"]
            entry = self._entry(service_id)
            current = self._runtime(self._active_dir(entry))
            if current.get("source_commit") == staged["revision"] or self._not_newer(current, staged["version"]):
                # Something else (a full install) already moved the runtime forward.
                return self._put(service_id, staged=None, requested=None, waiting_for=None,
                                 state="up_to_date", available_version=None)
            if self._host_too_old(staged.get("min_host")):
                return self._put(service_id, state="requires_host", min_host=staged["min_host"],
                                 requested=None, waiting_for=None)
            if self._runtime(directory).get("source_commit") != staged["revision"]:
                self._put(service_id, staged=None, requested=None, waiting_for=None,
                          state="error", last_error="service_update_stage_missing")
                fail("service_update_stage_missing")
            if not force:
                reason = self._busy(entry)
                if reason:
                    self._wake.set()
                    return self._put(service_id, state="waiting", requested=True, waiting_for=reason)
            previous = entry["program"]
            try:
                applied = self._declarations(service_id, entry, directory, staged.get("declarations"), staged.get("skills"))
            except Exception:
                self._put(service_id, state="error", last_error="service_update_apply_failed", requested=None)
                fail("service_update_apply_failed")
            with file_transaction(self._lock_path()):
                config = self._config()
                live = self._entry(service_id, config)
                live["program"] = f"{staged['directory']}/{staged['program']}"
                if applied is not None:
                    live["mcp_servers"] = applied["managed_servers"]
                atomic_write_json(self.root / "services.json", config, indent=2)
            row = self._put(service_id, state="applying", staged=None, requested=None, waiting_for=None,
                            previous_program=previous, current_version=staged["version"], available_version=None,
                            verify={"revision": staged["revision"], "version": staged["version"],
                                    "notes": staged.get("notes"), "since": self.clock()},
                            last_error=None, rolled_back=None)
            self._wake.set()
            return row

    def _declarations(self, service_id, entry, directory: Path, declarations, skills):
        """Apply a release directory's declarations (none for runtimes without them)."""
        store = self.extensions() if self.extensions else None
        if store is None or not declarations:
            return None
        path = directory.joinpath(*PurePosixPath(declarations).parts)
        if not path.is_file():
            return None
        from .release_declarations import apply_release_declarations
        value = json.loads(path.read_text(encoding="utf-8"))
        skills_root = directory.joinpath(*PurePosixPath(skills).parts) if skills else None
        return apply_release_declarations(store, value, skills_root=skills_root, service_id=service_id,
                                          port=int(entry["port"]), plugin_ids=entry.get("plugins", []))

    def _expected_running(self, entry) -> bool:
        store = self.extensions() if self.extensions else None
        if store is None:
            return True
        try:
            plugins = store.overview().get("plugins", [])
        except Exception:
            return True
        return any(p.get("id") in entry.get("plugins", []) and p.get("enabled") for p in plugins)

    def verify(self, service_id):
        row = self._row(service_id)
        pending = row.get("verify")
        if not pending:
            return row
        entry = self._entry(service_id)
        if not self._expected_running(entry):
            return self._finish(service_id)
        status, body = self.health(int(entry["port"]), entry["health_path"])
        elapsed = self.clock() - pending["since"]
        reported = body.get("revision")
        if status == 200 and reported == pending["revision"]:
            return self._finish(service_id)
        if elapsed >= self.verify_timeout:
            return self.rollback(service_id, "service_update_unhealthy")
        return row

    def _finish(self, service_id):
        pending = self._row(service_id).get("verify") or {}
        row = self._put(service_id, state="up_to_date", verify=None,
                        last_notes=pending.get("notes"),
                        updated={"version": pending.get("version"), "notes": pending.get("notes"), "at": self.clock()})
        self._collect(service_id, keep={row.get("previous_program")})
        return row

    def rollback(self, service_id, reason="service_update_rolled_back"):
        with self.lock:
            row = self._row(service_id)
            previous = row.get("previous_program")
            if not previous:
                fail("service_update_no_previous")
            with file_transaction(self._lock_path()):
                config = self._config()
                entry = self._entry(service_id, config)
                failed = self._runtime(self._active_dir(entry))
                entry["program"] = previous
                back = self._dir_of(previous)
                try:  # The previous release's declarations and skills come back with it.
                    applied = self._declarations(service_id, entry, back, DECLARATIONS, SKILLS)
                    if applied is not None:
                        entry["mcp_servers"] = applied["managed_servers"]
                except Exception:
                    pass
                atomic_write_json(self.root / "services.json", config, indent=2)
            quarantined = list(dict.fromkeys([*row.get("quarantined", []),
                                              *([failed["source_commit"]] if failed.get("source_commit") else [])]))[-20:]
            return self._put(service_id, state="rolled_back", last_error=reason, verify=None, previous_program=None,
                             quarantined=quarantined, rolled_back={"version": failed.get("version")},
                             available_version=failed.get("version"),
                             current_version=self._runtime(back).get("version"))

    def retry(self, service_id):
        """Re-check the update server. Quarantined revisions are never retried;
        only a newer revision published later will stage."""
        with self.lock:
            self._put(service_id, last_error=None, next_check=None)
        return self.check(service_id)

    def acknowledge(self, service_id):
        return self._put(service_id, updated=None)

    def _collect(self, service_id, keep):
        """Remove runtimes other than the active and the previous one."""
        try:
            entry = self._entry(service_id)
        except ServiceUpdateError:
            return
        protected = {self._active_dir(entry).resolve()}
        staged = self._row(service_id).get("staged") or {}
        if staged.get("directory"):
            protected.add((self.root / staged["directory"]).resolve())
        for program in keep:
            if program:
                protected.add(self._dir_of(program).resolve())
        pattern = re.compile(re.escape(service_id) + r"-[0-9a-f]{16}")
        for child in self.root.iterdir():
            if child.is_dir() and pattern.fullmatch(child.name) and child.resolve() not in protected:
                shutil.rmtree(child, ignore_errors=True)

    # -- lifecycle -----------------------------------------------------------
    def status(self):
        rows = []
        if not self.enabled:
            return {"services": rows}
        try:
            sources, config = self.sources(), self._config()
        except ServiceUpdateError as exc:
            return {"services": rows, "last_error": exc.code}
        state = self._state()
        names = {}
        store = self.extensions() if self.extensions else None
        if store is not None:
            try:
                names = {p.get("id"): p.get("title") or p.get("name") or p.get("id")
                         for p in store.overview().get("plugins", [])}
            except Exception:
                names = {}
        preferences = self.preferences()
        for service_id, source in sources.items():
            try:
                entry = self._entry(service_id, config)
            except ServiceUpdateError:
                continue
            row = state.get(service_id) or {}
            current = self._runtime(self._active_dir(entry))
            staged = row.get("staged") or {}
            done, total = self.progress.get(service_id, (0, None))
            plugin_ids = list(source.get("plugins") or entry.get("plugins", []))
            effective_state = row.get("state", "idle")
            waiting_for = row.get("waiting_for") if row.get("requested") else None
            if staged and effective_state in ("ready", "waiting"):
                live_busy = self._busy_cached(service_id, entry)
                if live_busy:
                    effective_state = "waiting"
                    waiting_for = live_busy
                elif not row.get("requested"):
                    effective_state = "ready"
            rows.append({
                "id": service_id,
                "name": source.get("name") or service_id,
                "plugins": plugin_ids,
                "plugin_names": [names.get(p, p) for p in plugin_ids],
                "auth_provider": source["auth"].get("peer_provider"),
                "auth_label": source["auth"].get("label"),
                "publisher": source.get("publisher"),
                "state": effective_state,
                "current_version": current.get("version") or row.get("current_version"),
                "available_version": staged.get("version") or row.get("available_version"),
                "notes": staged.get("notes") or row.get("last_notes"),
                "min_host": row.get("min_host"),
                "host_version": self.host_version,
                "waiting_for": waiting_for,
                "rolled_back_version": (row.get("rolled_back") or {}).get("version"),
                "updated": row.get("updated"),
                "downloaded": done,
                "total": total,
                "last_error": row.get("last_error"),
                "last_check": row.get("last_check"),
            })
        return {"services": rows, "auto_download": preferences["auto_download"],
                "install_on_quit": preferences["install_on_quit"]}

    def recover(self):
        """At node start: clear partial downloads, switch staged releases in (unless busy),
        and give a switched runtime a fresh verification window."""
        if not self.enabled:
            return
        for child in self.root.iterdir():
            if child.is_dir() and child.name.startswith(".") and child.name.endswith(".staging"):
                shutil.rmtree(child, ignore_errors=True)
        for service_id, row in self._state().items():
            if not isinstance(row, dict):
                continue
            try:
                if row.get("verify"):
                    self._put(service_id, state="applying", verify={**row["verify"], "since": self.clock()})
                elif row.get("staged"):
                    self.apply(service_id)
            except ServiceUpdateError:
                continue

    def quit(self):
        """Ryn is quitting: with "install when Ryn quits" on, a staged release becomes
        the runtime of its next start (unless tasks are still reported active)."""
        if not self.enabled or not self.preferences()["install_on_quit"]:
            return self.status()
        for service_id, row in self._state().items():
            if isinstance(row, dict) and row.get("staged"):
                try:
                    self.apply(service_id)
                except ServiceUpdateError:
                    continue
        return self.status()

    def _due(self, row, now) -> bool:
        due = row.get("next_check")
        if not isinstance(due, (int, float)):
            last = row.get("last_check")
            due = float(last) + CHECK_INTERVAL if isinstance(last, (int, float)) else 0
        return now >= due and row.get("state") not in {"applying", "downloading", "checking"}

    def poll_once(self):
        try:
            sources = self.sources()
        except ServiceUpdateError:
            return
        now = self.clock()
        automatic = self.preferences()["auto_download"]
        for service_id in sources:
            try:
                row = self._row(service_id)
                if row.get("verify"):
                    self.verify(service_id)
                elif row.get("requested") and row.get("staged"):
                    self.apply(service_id)
                elif automatic and self._due(row, now):
                    self.check(service_id)
            except ServiceUpdateError:
                continue
            except Exception:  # never let one runtime stop the others
                continue

    def _hurried(self) -> bool:
        return any(isinstance(row, dict) and (row.get("verify") or row.get("requested"))
                   for row in self._state().values())

    def start(self):
        if not self.enabled or self._thread:
            return
        try:
            self.recover()
        except Exception:
            pass

        def loop():
            interval = 15
            while not self._stop.is_set():
                self._wake.wait(interval)
                self._wake.clear()
                if self._stop.is_set():
                    break
                self.poll_once()
                try:
                    interval = FAST_POLL if self._hurried() else POLL_INTERVAL
                except Exception:
                    interval = POLL_INTERVAL
        self._thread = threading.Thread(target=loop, name="service-updates", daemon=True)
        self._thread.start()

    def close(self):
        self._stop.set()
        self._wake.set()


def install_service_update_routes(app, *, local_control, token_source=None, extensions=None, activity=None):
    updates = ServiceUpdates(os.environ.get("RYN_PLUGIN_SERVICES_DIR") or None,
                             token_source=token_source, extensions=extensions, activity=activity)
    app.state.service_updates = updates
    previous_lifespan = app.router.lifespan_context

    @asynccontextmanager
    async def lifespan(instance):
        async with previous_lifespan(instance) as state:
            await asyncio.to_thread(updates.start)
            try:
                yield state
            finally:
                updates.close()

    app.router.lifespan_context = lifespan

    async def run(fn, *args, **kwargs):
        try:
            return await asyncio.to_thread(fn, *args, **kwargs)
        except ServiceUpdateError as exc:
            raise HTTPException(409, exc.code) from None

    async def known(service_id):
        if service_id not in await run(updates.sources):
            raise HTTPException(404, "service_update_not_configured")

    @app.get("/api/local/service-updates")
    async def service_update_status(request: Request):
        local_control(request)
        return await asyncio.to_thread(updates.status)

    @app.post("/api/local/service-updates/check")
    async def service_update_check(request: Request):
        local_control(request)
        for service_id in (await run(updates.sources)):
            try:
                await run(updates.check, service_id)
            except HTTPException:
                continue
        return await asyncio.to_thread(updates.status)

    @app.post("/api/local/service-updates/quit")
    async def service_update_quit(request: Request):
        local_control(request)
        return await run(updates.quit)

    @app.post("/api/local/service-updates/{service_id}/{action}")
    async def service_update_action(request: Request, service_id: str, action: str):
        local_control(request)
        await known(service_id)
        if action == "check":
            try:
                await asyncio.to_thread(updates.check, service_id)
            except ServiceUpdateError as exc:
                if exc.code not in STATE_ERRORS:
                    raise HTTPException(409, exc.code) from None
        elif action == "apply":
            await run(updates.apply, service_id, force=request.query_params.get("force") == "1")
        elif action == "verify":
            await run(updates.verify, service_id)
        elif action == "retry":
            try:
                await asyncio.to_thread(updates.retry, service_id)
            except ServiceUpdateError as exc:
                if exc.code not in STATE_ERRORS:
                    raise HTTPException(409, exc.code) from None
        elif action == "acknowledge":
            await run(updates.acknowledge, service_id)
        else:
            raise HTTPException(404, "service_update_unknown_action")
        return await asyncio.to_thread(updates.status)

    return updates
