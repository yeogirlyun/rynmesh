"""Signed, owner-configured Release/HTTPS updates for installed web plugins.

No package-supplied commands, new permissions, or native runtime replacements.
The active package is immutable; failed staging never changes the active version.
"""
from __future__ import annotations

import base64
import json
import os
import re
import ssl
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path
from urllib.parse import urlsplit

import certifi
from packaging.version import InvalidVersion, Version

from .. import __version__
from ..atomic_io import atomic_write_json
from ..crypto import SignedPayload, verify_signed_payload
from ..file_transactions import file_transaction
from . import craft_host
from .craft_catalog import CRAFT

Error = craft_host.CraftError
GITHUB_HOSTS = {"github.com", "release-assets.githubusercontent.com", "objects.githubusercontent.com"}
INTERVAL = 6 * 60 * 60


def https_url(value):
    if not isinstance(value, str) or len(value) > 4096:
        raise Error("update_invalid_url")
    try:
        url = urlsplit(value)
        if (url.scheme != "https" or not url.hostname or url.username or url.password
                or url.fragment or url.port not in (None, 443) or "\\" in value
                or any(ord(c) < 33 for c in value)):
            raise ValueError
    except ValueError:
        raise Error("update_invalid_url") from None
    return url


def validate_source(source):
    if not isinstance(source, dict) or set(source) - {"kind", "url", "public_key", "auto_update", "token_env"}:
        raise Error("update_invalid_source")
    value = {"token_env": "", "auto_update": False, **source}
    if value.get("kind") not in {"public", "company"} or type(value["auto_update"]) is not bool:
        raise Error("update_invalid_source")
    url = https_url(value.get("url"))
    if value["kind"] == "public" and (url.hostname != "github.com" or url.query
            or not re.fullmatch(r"/[^/]+/[^/]+/releases/(latest/download|download/[^/]+)/[^/]+\.json", url.path)):
        raise Error("update_requires_git_release")
    try:
        if len(base64.b64decode(value["public_key"], validate=True)) != 32:
            raise ValueError
    except (KeyError, TypeError, ValueError):
        raise Error("update_invalid_public_key") from None
    if not isinstance(value["token_env"], str) or (value["token_env"] and
            not re.fullmatch(r"RYN_PLUGIN_TOKEN_[A-Z0-9_]{1,64}", value["token_env"])):
        raise Error("update_invalid_token_reference")
    if value["kind"] == "public" and value["token_env"]:
        raise Error("update_public_token_forbidden")
    return value


def transfer(source, url, destination=None):
    """Bounded downloads; credentials never cross origins, even on redirects."""
    origin = https_url(source["url"])
    def allowed(target):
        parsed = https_url(target)
        if source["kind"] == "public":
            if parsed.hostname not in GITHUB_HOSTS:
                raise Error("update_download_origin_rejected")
        elif parsed.netloc != origin.netloc:
            raise Error("update_download_origin_rejected")
    allowed(url)
    class Redirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, req, fp, code, msg, headers, newurl):
            allowed(newurl)
            return super().redirect_request(req, fp, code, msg, headers, newurl)
    headers = {"User-Agent": "Ryn-Plugin-Updater/1", "Accept": "application/octet-stream"}
    token_name = source.get("token_env")
    if token_name:
        token = os.environ.get(token_name, "")
        if not token:
            raise Error("update_company_login_required")
        if "\n" in token or "\r" in token:
            raise Error("update_invalid_token")
        headers["Authorization"] = "Bearer " + token
    opener = urllib.request.build_opener(Redirect(), urllib.request.HTTPSHandler(
        context=ssl.create_default_context(cafile=certifi.where())))
    limit = craft_host.MAX_ARCHIVE if destination else 65536
    try:
        with opener.open(urllib.request.Request(url, headers=headers), timeout=30) as response:
            if int(response.headers.get("Content-Length") or 0) > limit:
                raise Error("update_download_too_large")
            data = bytearray()
            output = Path(destination).open("xb") if destination else None
            try:
                size = 0
                deadline = time.monotonic() + 600
                while chunk := response.read(65536):
                    size += len(chunk)
                    if size > limit or time.monotonic() > deadline:
                        raise Error("update_download_too_large")
                    if output:
                        output.write(chunk)
                    else:
                        data.extend(chunk)
            finally:
                if output:
                    output.close()
            return bytes(data)
    except urllib.error.HTTPError as exc:
        raise Error("update_not_published" if exc.code == 404 else f"update_http_{exc.code}") from None
    except Error:
        raise
    except (OSError, ValueError):
        raise Error("update_download_failed") from None


def verify_release(raw, source, slug, *, check_compatibility=True):
    try:
        signed = SignedPayload.from_dict(raw)
        if signed.public_key != source["public_key"]:
            raise ValueError("publisher")
        verify_signed_payload(signed)
        p = signed.payload
        if p.get("kind") != "ryn.plugin.release" or p.get("schema") != 1 or p.get("id") != slug:
            raise ValueError("identity")
        if not isinstance(p.get("version"), str) or not re.fullmatch(r"v?\d+\.\d+\.\d+(?:[-.][A-Za-z0-9]+)*", p["version"]):
            raise ValueError("version")
        version = Version(p["version"])
        if version.is_prerelease or version.is_devrelease:
            raise Error("update_prerelease_not_supported")
        if p.get("runtime") != "static-web" or p.get("permissions") != []:
            raise Error("update_requires_host_or_permissions")
        minimum, maximum = Version(p["min_host_version"]), Version(p["max_host_version"])
        if minimum > maximum:
            raise ValueError("host range")
        if check_compatibility and not (minimum <= Version(__version__) <= maximum):
            raise Error("update_incompatible_host")
        if not re.fullmatch(r"[0-9a-f]{64}", p.get("sha256", "")):
            raise ValueError("hash")
        if type(p.get("size")) is not int or not 0 < p["size"] <= craft_host.MAX_ARCHIVE:
            raise ValueError("size")
        url = https_url(p["url"])
        origin = https_url(source["url"])
        if source["kind"] == "public":
            # Only immutable release assets in the configured publisher's repository.
            repo = "/".join(origin.path.split("/")[:3])
            if url.hostname != "github.com" or not url.path.startswith(repo + "/releases/download/"):
                raise Error("update_download_origin_rejected")
        elif url.netloc != origin.netloc:
            raise Error("update_download_origin_rejected")
        return p
    except Error:
        raise
    except (KeyError, ValueError, TypeError, InvalidVersion):
        raise Error("update_manifest_invalid") from None


def release_spec(slug, payload):
    return {**CRAFT[slug], "tag": payload["version"], "web_sha256": payload["sha256"],
            "download_url": payload["url"]}


class PluginUpdates:
    def __init__(self, manager):
        self.manager = manager
        self.path = manager.state_path.with_name("plugin-updates.json")
        self.operation = threading.Lock()
        self.stop = threading.Event()
        self.worker = None
        self.load_error = None
        self.data = {"schema": 1, "plugins": {}}
        if self.path.exists():
            try:
                if self.path.stat().st_size > 1024 * 1024:
                    raise ValueError
                self.data = json.loads(self.path.read_text(encoding="utf-8"))
                if self.data["schema"] != 1 or not isinstance(self.data["plugins"], dict):
                    raise ValueError
                for slug, row in self.data["plugins"].items():
                    if slug not in CRAFT or not isinstance(row, dict):
                        raise ValueError
                    validate_source(row["source"])
            except (OSError, ValueError, KeyError, TypeError):
                self.data = {"schema": 1, "plugins": {}}
                self.load_error = "update_state_invalid"

    def save(self):
        if self.load_error:
            raise Error(self.load_error)
        atomic_write_json(self.path, self.data)

    def active_spec(self, slug, row, *, compatible=False):
        raw = row.get("update_release")
        if not raw:
            return None
        source = validate_source(row["update_source"])
        return release_spec(slug, verify_release(raw, source, slug, check_compatibility=compatible))

    def status(self):
        with self.manager.lock:
            return {"last_error": self.load_error, "plugins": [{"id": slug, "current_version": self.manager.version(slug),
                                  "can_rollback": bool(self.manager._row(slug).get("previous")),
                                  **{key: value for key, value in row.items() if key != "candidate"}}
                                for slug, row in self.data["plugins"].items()
                                if self.manager._read()["plugins"].get(slug)]}

    def configure(self, slug, source):
        if self.load_error:
            raise Error(self.load_error)
        if slug not in CRAFT or not CRAFT[slug]["web"]:
            raise Error("update_requires_main_app")
        source = validate_source(source)
        with self.manager._mutation(slug), self.manager.lock, file_transaction(self.manager.lock_path):
            if not self.manager._row(slug):
                raise Error("plugin_not_installed")
            old = self.data["plugins"].get(slug, {})
            same_publisher = {k: v for k, v in old.get("source", {}).items() if k != "auto_update"} == {k: v for k, v in source.items() if k != "auto_update"}
            self.data["plugins"][slug] = {**(old if same_publisher else {"state": "idle"}), "source": source}
            self.save()
        return self.status()

    def check(self, slug):
        if not self.operation.acquire(blocking=False):
            raise Error("plugin_busy")
        try:
            with self.manager.lock:
                row = self.data["plugins"].get(slug)
                if not row or not self.manager._row(slug):
                    raise Error("update_source_not_configured")
                source = dict(row["source"])
                row.update(state="checking", last_error=None, available_version=None)
                row.pop("candidate", None)
            try:
                raw = json.loads(transfer(source, source["url"]))
                payload = verify_release(raw, source, slug)
                with self.manager.lock:
                    if row is not self.data["plugins"].get(slug):
                        return self.status()
                    if Version(payload["version"]) > Version(self.manager.version(slug)):
                        if payload["version"] in row.get("quarantined", []):
                            raise Error("update_version_quarantined")
                        row.update(candidate=raw, available_version=payload["version"], state="available")
                    else:
                        row["state"] = "up_to_date"
            except Exception as exc:
                with self.manager.lock:
                    row.update(state="error", last_error=str(exc) if isinstance(exc, Error) else "update_check_failed")
            with self.manager.lock:
                row["last_check"] = int(time.time())
                self.save()
            return self.status()
        finally:
            self.operation.release()

    def recover(self):
        """At startup, restore a verified previous package if activation is no longer valid."""
        if self.load_error:
            return
        with self.manager.lock, file_transaction(self.manager.lock_path):
            state = self.manager._read()
            changed = False
            for slug, row in state["plugins"].items():
                if not row.get("update_release"):
                    continue
                try:
                    craft_host.installed(self.manager.cache, slug, spec=self.active_spec(slug, row, compatible=True))
                except Error as error:
                    previous = row.get("previous")
                    if not previous:
                        if str(error) == "update_incompatible_host":
                            row["enabled"] = False
                            self.data["plugins"].setdefault(slug, {"source": row["update_source"]}).update(state="error", last_error=str(error))
                            changed = True
                        continue
                    try:
                        craft_host.installed(self.manager.cache, slug, spec=self.active_spec(slug, previous, compatible=True))
                    except Error:
                        if str(error) == "update_incompatible_host":
                            row["enabled"] = False
                            self.data["plugins"].setdefault(slug, {"source": row["update_source"]}).update(state="error", last_error=str(error))
                            changed = True
                        continue
                    import secrets
                    state["plugins"][slug] = {**previous, "enabled": row["enabled"], "generation": secrets.token_hex(16)}
                    update = self.data["plugins"].setdefault(slug, {"source": row["update_source"]})
                    update.setdefault("quarantined", []).append(row["version"])
                    update.update(state="rolled_back", last_error="update_startup_verification_failed", available_version=None)
                    update.pop("candidate", None)
                    changed = True
            if changed:
                self.manager._write(state)
                self.save()

    def apply(self, slug):
        if not self.operation.acquire(blocking=False):
            raise Error("plugin_busy")
        try:
            with self.manager._mutation(slug):
                with self.manager.lock:
                    row = self.data["plugins"].get(slug, {})
                    source = dict(row.get("source", {}))
                    raw = row.get("candidate")
                    if not raw:
                        raise Error("update_not_available")
                    # A launched editor may contain unsaved documents. Never close it for an update.
                    if slug in self.manager.servers:
                        row["state"] = "waiting_for_idle"
                        self.save()
                        return self.status()
                    payload = verify_release(raw, source, slug)
                    if Version(payload["version"]) <= Version(self.manager.version(slug)):
                        raise Error("update_not_newer")
                    row["state"] = "downloading"
                    self.save()
                def download(url, path):
                    transfer(source, url, path)
                    if path.stat().st_size != payload["size"]:
                        raise Error("update_size_mismatch")
                try:
                    spec = release_spec(slug, payload)
                    craft_host.install(self.manager.cache, slug, spec=spec, download=download)
                    craft_host.installed(self.manager.cache, slug, spec=spec)
                    with self.manager.lock, file_transaction(self.manager.lock_path):
                        state = self.manager._read()
                        current = state["plugins"].get(slug)
                        if not current or self.manager.closed:
                            raise Error("plugin_not_installed")
                        import secrets
                        previous = {k: v for k, v in current.items() if k != "previous"}
                        current.update(version=payload["version"], update_release=raw, update_source=source,
                                       previous=previous, generation=secrets.token_hex(16))
                        self.manager._write(state)
                        row.update(state="updated", available_version=None, last_error=None)
                        row.pop("candidate", None)
                        self.save()
                except Exception as exc:
                    with self.manager.lock:
                        row.update(state="error", last_error=str(exc) if isinstance(exc, Error) else "update_install_failed")
                        if isinstance(exc, Error) and str(exc) in {"craft_checksum_mismatch", "craft_build_not_verified"}:
                            row.setdefault("quarantined", []).append(payload["version"])
                            row.pop("candidate", None)
                            row["available_version"] = None
                        self.save()
                    raise
            return self.status()
        finally:
            self.operation.release()

    def rollback(self, slug):
        with self.manager._mutation(slug), self.manager.lock, file_transaction(self.manager.lock_path):
            if slug in self.manager.servers:
                raise Error("plugin_busy")
            state = self.manager._read()
            current = state["plugins"].get(slug, {})
            previous = current.get("previous")
            if not previous:
                raise Error("update_no_previous_version")
            craft_host.installed(self.manager.cache, slug, spec=self.active_spec(slug, previous, compatible=True))
            import secrets
            state["plugins"][slug] = {**previous, "enabled": current["enabled"], "generation": secrets.token_hex(16)}
            self.manager._write(state)
            row = self.data["plugins"].setdefault(slug, {"source": current["update_source"]})
            row.setdefault("quarantined", []).append(current["version"])
            row.update(state="rolled_back", available_version=None)
            row.pop("candidate", None)
            self.save()
        return self.status()

    def poll_once(self):
        with self.manager.lock:
            ids = [slug for slug, row in self.data["plugins"].items()
                   if row.get("source", {}).get("auto_update") and self.manager._read()["plugins"].get(slug)]
        for slug in ids:
            if self.stop.is_set():
                return
            try:
                with self.manager.lock:
                    row = self.data["plugins"][slug]
                    due = time.time() - row.get("last_check", 0) >= INTERVAL
                if due:
                    self.check(slug)
                with self.manager.lock:
                    row = self.data["plugins"][slug]
                    pending = row.get("source", {}).get("auto_update") and row.get("state") in {"available", "waiting_for_idle"}
                if pending:
                    self.apply(slug)
            except Exception:
                pass  # persisted operation errors are visible in the owner-only status API

    def start(self):
        if self.load_error:
            return
        def loop():
            # No work in constructors. No downloads until explicitly enabled for an installed plugin.
            while not self.stop.wait(60):
                self.poll_once()
        self.worker = threading.Thread(target=loop, daemon=True, name="ryn-plugin-updates")
        self.worker.start()
