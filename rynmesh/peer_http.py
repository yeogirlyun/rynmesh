"""Rynmesh direct HTTP peer transport.

Phase 3A uses plain HTTP between peers so the network can move beyond a shared
folder while preserving the same trust rule: every peer validates signed
manifests and content hashes locally before storing content bytes.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import socket
import subprocess
import sys
import time
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlencode, urlparse

from . import recommendation_service
from . import transport_plugins as _transport_plugins  # noqa: F401 — registers reality/meek/ech
from .credits import CreditEvent, CreditLedgerError
from .crypto import SignedPayload, canonical_json
from .recommendation_profile import RecommendationProfileStore, starter_items
from .registry import RegistryError
from .store import RynmeshStore, StoreError
from .transport import Transport, TransportError, get_transport
from .types import RYNMESH_VERSION


class PeerTransportError(RuntimeError):
    pass


MAX_JSON_BYTES = 2 * 1024 * 1024
MAX_POST_RESPONSE_BYTES = 8 * 1024 * 1024
MAX_PREVIEW_BYTES = 32 * 1024 * 1024
MAX_MEDIA_BYTES = 10 * 1024 * 1024 * 1024
BLOCKED_HOSTS = {
    "169.254.169.254",
    "metadata.google.internal",
    "metadata",
}
ITEM_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_:.-]{0,255}$")


def _desktop_lan_ip() -> str:
    """Resolve a useful LAN address without requiring network access."""
    if configured_ip := os.environ.get("RYNMESH_MACHINE_IP", "").strip():
        return configured_ip
    if sys.platform == "darwin":
        try:
            route = subprocess.run(
                ["/sbin/route", "-n", "get", "default"],
                capture_output=True,
                check=False,
                text=True,
                timeout=2,
            ).stdout
            interface = next(
                (
                    line.split(":", 1)[1].strip()
                    for line in route.splitlines()
                    if "interface:" in line
                ),
                "",
            )
            if interface:
                ip = subprocess.run(
                    ["/usr/sbin/ipconfig", "getifaddr", interface],
                    capture_output=True,
                    check=False,
                    text=True,
                    timeout=2,
                ).stdout.strip()
                if ip:
                    return ip
        except (OSError, subprocess.SubprocessError):
            pass
    try:
        for family, _, _, _, sockaddr in socket.getaddrinfo(socket.gethostname(), None):
            if family == socket.AF_INET and not sockaddr[0].startswith("127."):
                return str(sockaddr[0])
    except OSError:
        pass
    return "127.0.0.1"


def _desktop_node_name() -> str:
    configured = os.environ.get("RYNMESH_NODE_NAME", "").strip()
    if configured:
        return configured
    if sys.platform == "darwin":
        try:
            name = subprocess.run(
                ["/usr/sbin/scutil", "--get", "ComputerName"],
                capture_output=True,
                check=False,
                text=True,
                timeout=2,
            ).stdout.strip()
            if name:
                return name
        except (OSError, subprocess.SubprocessError):
            pass
    return socket.gethostname().split(".", 1)[0] or "ryn-node"


def apply_desktop_defaults() -> None:
    """Give packaged desktop nodes a complete, zero-configuration runtime.

    Every desktop entry point sets ``RYNMESH_DESKTOP_MODE=1``. Centralizing the
    remaining defaults here prevents an installer, login agent, or Tauri
    sidecar from silently creating a different node merely because it omitted
    one environment variable. Explicit operator values always win.
    """
    if os.environ.get("RYNMESH_DESKTOP_MODE", "").strip().lower() not in {
        "1",
        "true",
        "yes",
    }:
        return
    port = os.environ.get("RYNMESH_PEER_PORT", "8791").strip() or "8791"
    ip = _desktop_lan_ip()
    name = _desktop_node_name()
    os.environ.setdefault("RYNMESH_HOME", str(Path.home() / ".rynmesh"))
    os.environ.setdefault("RYNMESH_NODE_NAME", name)
    os.environ.setdefault("RYNMESH_MACHINE_NAME", name)
    os.environ.setdefault("RYNMESH_MACHINE_IP", ip)
    os.environ.setdefault("RYNMESH_NETWORK_ID", "rynmesh-main")
    os.environ.setdefault("RYNMESH_PEER_HOST", "0.0.0.0")
    os.environ.setdefault("RYNMESH_PEER_PORT", port)
    os.environ.setdefault("RYNMESH_PEER_PUBLIC_HOST", ip)
    os.environ.setdefault("RYNMESH_PEER_ENDPOINT", f"http://{ip}:{port}")
    os.environ.setdefault("RYNMESH_AUTO_REGISTER", "1")
    os.environ.setdefault("RYNMESH_REGISTRY_URL", "https://registry.rynmesh.ai")
    os.environ.setdefault("RYNMESH_RELAY_URL", os.environ["RYNMESH_REGISTRY_URL"])


def _base(url: str) -> str:
    cleaned = str(url or "").strip().rstrip("/")
    if not cleaned:
        raise PeerTransportError("peer_endpoint_required")
    parsed = urlparse(cleaned)
    if parsed.scheme not in {"http", "https"}:
        raise PeerTransportError("peer_endpoint_scheme_unsupported")
    if not parsed.hostname:
        raise PeerTransportError("peer_endpoint_host_required")
    if parsed.username or parsed.password or parsed.fragment:
        raise PeerTransportError("peer_endpoint_not_allowed")
    if _host_blocked(parsed.hostname):
        raise PeerTransportError("peer_endpoint_host_blocked")
    return cleaned


def _clip_path(clip_id: str, suffix: str) -> str:
    return f"/api/v1/clips/{quote(_validate_item_id(clip_id), safe='')}/{suffix}"


def _content_path(content_id: str, suffix: str) -> str:
    return f"/api/v1/content/{quote(_validate_item_id(content_id), safe='')}/{suffix}"


def _validate_item_id(value: str) -> str:
    item_id = str(value or "")
    if not ITEM_ID_PATTERN.fullmatch(item_id):
        raise PeerTransportError("invalid_item_id")
    return item_id


def _host_blocked(host: str) -> bool:
    normalized = host.strip("[]").lower()
    if normalized in BLOCKED_HOSTS:
        return True
    try:
        import ipaddress

        ip = ipaddress.ip_address(normalized)
    except ValueError:
        return False
    return ip.is_link_local


class HttpPeerClient:
    """Client for a Rynmesh peer HTTP endpoint.

    All wire I/O goes through a pluggable `Transport` (default: camouflaged
    stdlib HTTPS — see rynmesh.transport / docs/RYNMESH_TRANSPORT_CENSORSHIP.md)
    so peer traffic can be made censorship-resistant without touching this
    client's logic.
    """

    def __init__(
        self, endpoint: str, *, timeout_s: float = 20.0, transport: Transport | None = None
    ) -> None:
        self.endpoint = _base(endpoint)
        self.timeout_s = float(timeout_s)
        self.transport = transport or get_transport()

    def node_info(self) -> dict[str, Any]:
        return self._json("/api/v1/node")

    def list_clips(self) -> dict[str, Any]:
        return self._json("/api/v1/clips")

    def list_content(self) -> dict[str, Any]:
        return self._json("/api/v1/content")

    def credit_summary(self, *, peer_id: str = "", category: str = "global") -> dict[str, Any]:
        query = "?" + urlencode({"peer_id": peer_id, "category": category})
        return self._json("/api/v1/credits" + query)

    def credit_scoreboard(self, *, category: str = "global") -> dict[str, Any]:
        return self._json("/api/v1/credits/scoreboard?" + urlencode({"category": category}))

    def get_manifest(self, clip_id: str) -> SignedPayload:
        payload = self._json(_clip_path(clip_id, "manifest"))
        if not isinstance(payload, dict):
            raise PeerTransportError("manifest_response_not_object")
        return SignedPayload.from_dict(payload)

    def get_content_manifest(self, content_id: str) -> SignedPayload:
        payload = self._json(_content_path(content_id, "manifest"))
        if not isinstance(payload, dict):
            raise PeerTransportError("manifest_response_not_object")
        return SignedPayload.from_dict(payload)

    def get_preview(self, clip_id: str) -> bytes:
        return self._bytes(_clip_path(clip_id, "preview"), max_bytes=MAX_PREVIEW_BYTES)

    def get_content_preview(self, content_id: str) -> bytes:
        return self._bytes(_content_path(content_id, "preview"), max_bytes=MAX_PREVIEW_BYTES)

    def download_media(self, clip_id: str, destination: str | Path) -> Path:
        return self._download(_clip_path(clip_id, "media"), destination)

    def download_content(self, content_id: str, destination: str | Path) -> Path:
        return self._download(_content_path(content_id, "bytes"), destination)

    def post_json(
        self,
        path: str,
        payload: dict[str, Any],
        *,
        headers: dict[str, str] | None = None,
        max_response_bytes: int = MAX_JSON_BYTES,
    ) -> dict[str, Any]:
        if not path.startswith("/"):
            raise PeerTransportError("peer_path_invalid")
        if not 1 <= int(max_response_bytes) <= MAX_POST_RESPONSE_BYTES:
            raise PeerTransportError("peer_response_limit_invalid")
        sender = getattr(self.transport, "post_bytes", None)
        if not callable(sender):
            raise PeerTransportError("transport_post_unsupported")
        try:
            raw = sender(
                self.endpoint + path,
                canonical_json(payload),
                timeout_s=self.timeout_s,
                max_bytes=int(max_response_bytes),
                headers={"Content-Type": "application/json", **(headers or {})},
            )
            value = json.loads(raw.decode("utf-8") or "{}")
        except TransportError as exc:
            raise PeerTransportError(f"peer_http_error: {exc}") from exc
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise PeerTransportError("peer_invalid_json") from exc
        if not isinstance(value, dict):
            raise PeerTransportError("peer_response_not_object")
        return value

    def _download(self, path: str, destination: str | Path) -> Path:
        url = self.endpoint + path
        try:
            return self.transport.download(
                url, Path(destination), timeout_s=self.timeout_s, max_bytes=MAX_MEDIA_BYTES
            )
        except TransportError as exc:
            if exc.reason == "too_large":
                raise PeerTransportError("peer_media_too_large") from exc
            raise PeerTransportError(f"peer_http_error: {exc}") from exc

    def _json(self, path: str) -> dict[str, Any]:
        raw = self._bytes(path, max_bytes=MAX_JSON_BYTES).decode("utf-8")
        try:
            payload = json.loads(raw or "{}")
        except json.JSONDecodeError as exc:
            raise PeerTransportError(f"peer_invalid_json: {exc}") from exc
        if not isinstance(payload, dict):
            raise PeerTransportError("peer_response_not_object")
        return payload

    def _bytes(self, path: str, *, max_bytes: int) -> bytes:
        try:
            return self.transport.get_bytes(
                self.endpoint + path, timeout_s=self.timeout_s, max_bytes=max_bytes
            )
        except TransportError as exc:
            if exc.reason == "too_large":
                raise PeerTransportError("peer_response_too_large") from exc
            raise PeerTransportError(f"peer_http_error: {exc}") from exc


def create_app(store: RynmeshStore | None = None):
    try:
        from fastapi import FastAPI, HTTPException, Response
        from fastapi import Request as FastAPIRequest
        from fastapi.middleware.cors import CORSMiddleware
        from fastapi.responses import FileResponse
    except ImportError as exc:  # pragma: no cover
        raise ImportError("Rynmesh peer HTTP server requires `fastapi`") from exc
    globals()["FastAPIRequest"] = FastAPIRequest

    active_store = store or RynmeshStore()

    import asyncio as _asyncio
    import hashlib as _hashlib
    import subprocess as _subprocess
    import sys as _sys
    import urllib.request as _urlreq
    from datetime import UTC as _UTC
    from datetime import datetime as _dt
    from pathlib import Path as _Path

    from .services.updater import Updater
    from .settings_store import SettingsStore
    from .update_state import UpdateState

    _home = _Path(os.environ.get("RYNMESH_HOME", str(_Path.home() / ".rynmesh")))
    _settings = SettingsStore(_home / "settings.json")
    _recommendation_profile = RecommendationProfileStore(_home / "recommendation-profile.json")
    _stored_settings = _settings.get()
    if str(_stored_settings.get("node_name", "")).strip():
        active_store.node_name = str(_stored_settings["node_name"]).strip()

    def _apply_safety_policy(settings: dict[str, Any]) -> None:
        policy = str(settings.get("safety_policy", "standard"))
        active_store.policy.min_pass_receipts = 0 if policy == "permissive" else 1
        active_store.policy.allow_warnings = policy != "strict"

    def _fetch_timeout() -> float:
        configured = os.environ.get("RYNMESH_FETCH_TIMEOUT_S", "").strip()
        return float(configured or _settings.get()["fetch_timeout_s"])

    _apply_safety_policy(_stored_settings)
    _registry_url = os.environ.get("RYNMESH_REGISTRY_URL", "").strip()
    _pinned = {
        k.strip()
        for k in os.environ.get("RYNMESH_UPDATE_PUBLISHER_KEYS", "").split(",")
        if k.strip()
    }
    _wheel_dir = _home / "wheels"
    _wheel_dir.mkdir(parents=True, exist_ok=True)

    def _iso_now() -> str:
        return _dt.now(_UTC).isoformat()

    def _fetch_latest():
        if not _registry_url:
            return None
        with _urlreq.urlopen(f"{_registry_url}/api/v1/releases/latest", timeout=10) as r:
            data = json.loads(r.read().decode())
        return data or None

    def _download_wheel(sha256: str, filename: str) -> str:
        # Save under the manifest's real wheel filename (sanitized to a basename) so
        # pip accepts it — pip rejects an arbitrary {sha256}.whl name.
        safe = _Path(filename).name or f"{sha256}.whl"
        dest = _wheel_dir / safe
        with _urlreq.urlopen(f"{_registry_url}/api/v1/relay/blobs/{sha256}", timeout=120) as r:
            dest.write_bytes(r.read())
        return str(dest)

    def _sha256_of(path: str) -> str:
        return _hashlib.sha256(_Path(path).read_bytes()).hexdigest()

    def _pip_install(wheel_path: str) -> None:
        _subprocess.run(
            [_sys.executable, "-m", "pip", "install", "--upgrade", wheel_path], check=True
        )

    def _preflight(version: str) -> bool:
        r = _subprocess.run(
            [
                _sys.executable,
                "-c",
                "import importlib,importlib.metadata as m,sys; "
                "importlib.import_module('rynmesh.peer_http'); "
                "sys.exit(0 if m.version('rynmesh')==sys.argv[1] else 3)",
                version,
            ],
            capture_output=True,
        )
        return r.returncode == 0

    def _snapshot_current_wheel():
        keep = _wheel_dir / f"installed-{RYNMESH_VERSION}.whl"
        return str(keep) if keep.exists() else None

    def _record_installed(wheel_path: str, version: str) -> None:
        import shutil as _shutil

        try:
            _shutil.copy2(wheel_path, _wheel_dir / f"installed-{version}.whl")
        except OSError:
            pass

    def _reexec():
        os.execv(_sys.executable, [_sys.executable, "-m", "rynmesh.peer_http"])

    update_state = UpdateState(_home / "update-state.json")
    updater = Updater(
        fetch_latest=_fetch_latest,
        download_wheel=_download_wheel,
        sha256_of=_sha256_of,
        pip_install=_pip_install,
        preflight=_preflight,
        reexec=_reexec,
        snapshot_current_wheel=_snapshot_current_wheel,
        record_installed=_record_installed,
        pinned_pubkeys=_pinned,
        auto_update=lambda: bool(_settings.get().get("auto_update", True)),
        current_version=RYNMESH_VERSION,
        state=update_state,
        now=_iso_now,
        max_attempts=3,
    )

    @asynccontextmanager
    async def lifespan(lifespan_app):
        updater.on_startup()  # may os.execv away on crash-loop rollback
        llm_starter = getattr(lifespan_app.state, "llm_startup", None)
        if callable(llm_starter):
            try:
                await _asyncio.to_thread(llm_starter)
                lifespan_app.state.llm_startup_error = ""
            except Exception as exc:  # noqa: BLE001 - degraded startup is intentional
                lifespan_app.state.llm_startup_error = str(exc)
        if os.environ.get("RYNMESH_AUTO_REGISTER", "").strip().lower() in {"1", "true", "yes"}:
            network_id = (
                os.environ.get("RYNMESH_NETWORK_ID", "rynmesh-main").strip() or "rynmesh-main"
            )
            try:
                active_store.register_node(network_id=network_id)
                lifespan_app.state.registration_error = ""
            except RegistryError as exc:
                lifespan_app.state.registration_error = str(exc)

        async def _confirm_after_grace():
            # Confirm a pending update only after the daemon has stayed up for a grace
            # period — so a build that boots then crashes within the window is NOT
            # confirmed and the startup attempt-counter can still trigger rollback.
            grace = int(os.environ.get("RYNMESH_UPDATE_CONFIRM_S", "120") or 120)
            await _asyncio.sleep(grace)
            updater.mark_serving()

        async def _poll():
            interval = int(os.environ.get("RYNMESH_UPDATE_POLL_S", "1800") or 1800)
            while True:
                await _asyncio.sleep(interval)
                try:
                    res = await _asyncio.to_thread(updater.check)
                    if res.get("available") and updater.status()["autoUpdate"]:
                        await _asyncio.to_thread(updater.apply, updater.check_manifest())
                except Exception:
                    pass

        async def _recap_daily():
            """Send the recap once per day, at the configured UTC hour.

            Deliberately a poll rather than a timer: the machine sleeps, and a
            laptop that was closed at the send hour should still get its recap
            when it wakes rather than skipping the day.
            """
            await _asyncio.sleep(20)
            while True:
                try:
                    stored = dict(_settings.get().get("recap", {}) or {})
                    if stored.get("enabled") and stored.get("smtp_host"):
                        now = time.time()
                        hour = int(stored.get("send_hour_utc", 13))
                        last = float(stored.get("last_sent_unix", 0) or 0)
                        due = _dt.now(_UTC).hour >= hour and (now - last) > 20 * 3600
                        if due:
                            await _asyncio.to_thread(_send_recap_now)
                except Exception:
                    pass
                await _asyncio.sleep(900)

        async def _llm_relay_poll():
            # Poll fast while orders are flowing, back off when idle or when no
            # provider is configured, and surface persistent failures instead of
            # silently dropping them (a provider that cannot poll is offline in
            # every way that matters, yet used to look healthy).
            await _asyncio.sleep(1)
            interval = 1.0
            while True:
                worker = getattr(lifespan_app.state, "llm_relay_once", None)
                if worker is None:
                    await _asyncio.sleep(5)
                    continue
                try:
                    processed = await _asyncio.to_thread(worker)
                    lifespan_app.state.llm_relay_error = ""
                    interval = 1.0 if processed else min(interval * 1.5, 10.0)
                except Exception as exc:
                    lifespan_app.state.llm_relay_error = str(exc)
                    interval = min(max(interval, 1.0) * 2, 30.0)
                await _asyncio.sleep(interval)

        async def _llm_publish_refresh():
            # LLM discovery records are intentionally short lived. Keep a configured,
            # healthy provider visible without requiring an operator to republish it.
            await _asyncio.sleep(1)
            while True:
                publisher = getattr(lifespan_app.state, "llm_publish_once", None)
                if publisher is not None:
                    try:
                        await _asyncio.to_thread(publisher)
                        lifespan_app.state.llm_publication_error = ""
                    except Exception as exc:
                        # A registry or runtime outage must not take down the node;
                        # the Services screen still provides a manual retry.
                        lifespan_app.state.llm_publication_error = str(exc)
                await _asyncio.sleep(30)

        async def _discover():
            service = getattr(lifespan_app.state, "digest_service", None)
            if service is None or not service.bootstrap_defaults:
                return
            await _asyncio.sleep(0.35)
            while True:
                try:
                    await _asyncio.to_thread(
                        service.proactive_refresh,
                        now_unix=time.time(),
                        timeout_s=min(_fetch_timeout(), 10.0),
                    )
                    provider = _model_provider()
                    if provider is not None:
                        await _asyncio.to_thread(service.enrich_latest, provider)
                    audit = getattr(lifespan_app.state, "assistant_audit", None)
                    if audit is not None:
                        status = service.discovery_status()
                        audit.append(
                            "rec",
                            f"Background discovery prepared {status['item_count']} recommendations",
                            details={
                                "healthy_sources": status["healthy_sources"],
                                "failed_sources": status["failed_sources"],
                                "formats": status["formats"],
                                "ai_provider": getattr(provider, "id", None),
                                "network_access": True,
                            },
                        )
                except Exception:
                    pass
                status = service.discovery_status()
                delay = max(
                    60.0,
                    float(status.get("next_refresh_unix", 0.0) or 0.0) - time.time(),
                )
                await _asyncio.sleep(delay)

        confirm_task = _asyncio.create_task(_confirm_after_grace())
        poll_task = _asyncio.create_task(_poll())
        discovery_task = _asyncio.create_task(_discover())
        recap_task = _asyncio.create_task(_recap_daily())
        llm_relay_task = _asyncio.create_task(_llm_relay_poll())
        llm_publish_task = _asyncio.create_task(_llm_publish_refresh())
        yield
        confirm_task.cancel()
        poll_task.cancel()
        discovery_task.cancel()
        recap_task.cancel()
        llm_relay_task.cancel()
        llm_publish_task.cancel()
        llm_stopper = getattr(lifespan_app.state, "llm_shutdown", None)
        if callable(llm_stopper):
            try:
                await _asyncio.to_thread(llm_stopper)
            except Exception:  # noqa: BLE001 - shutdown must remain best effort
                pass

    app = FastAPI(title="Rynmesh Peer", version="0.1", lifespan=lifespan)
    started_at = time.monotonic()
    app.state.registration_error = ""
    app.state.llm_publication_error = ""
    app.state.publish_drafts = {}
    app.add_middleware(
        CORSMiddleware,
        allow_origins=[
            "http://127.0.0.1:5173",
            "http://localhost:5173",
            "tauri://localhost",
            "http://tauri.localhost",
            "https://tauri.localhost",
        ],
        allow_methods=["*"],
        allow_headers=["*"],
    )

    _local_token = os.environ.get("RYNMESH_LOCAL_TOKEN", "").strip()

    # Device-token auth for the control surface. A loopback socket is only
    # trusted when nothing proxied the request — cloudflared dials the origin
    # from 127.0.0.1, so without that rule a tunnel would hand node control to
    # the whole internet. See docs/superpowers/specs/2026-08-05-node-auth-design.md
    from rynmesh import node_auth as node_auth_mod

    node_auth = node_auth_mod.NodeAuth(
        home=Path(os.environ.get("RYNMESH_HOME", str(Path.home() / ".rynmesh"))),
        access_team_domain=os.environ.get("RYNMESH_ACCESS_TEAM_DOMAIN", "").strip(),
        access_audience=os.environ.get("RYNMESH_ACCESS_AUD", "").strip(),
    )
    _auth_open_paths = {"/api/local/auth/status", "/api/local/auth/unlock"}
    # Materialize the token at startup rather than on first use, so the owner
    # can always find it on disk to pair a phone or a second machine.
    try:
        node_auth.token()
    except OSError:
        pass
    # Prefetch the Access signing keys when the perimeter is configured, so the
    # first request doesn't pay a blocking network fetch inside the event loop.
    node_auth.warm_access_keys()

    @app.middleware("http")
    async def _guard_local_control_api(request: FastAPIRequest, call_next):
        path = request.url.path

        # Active-probe resistance: when a shared network key is configured, the
        # peer surface (/api/v1, /health) requires it. An unauthenticated probe
        # — e.g. a censor fingerprinting the port — gets an indistinguishable
        # generic 404, so the server does not reveal that it runs Rynmesh.
        # Opt-in: with no key set, peer APIs stay open (dev/LAN default).
        peer_key = os.environ.get("RYNMESH_NETWORK_KEY", "").strip()
        if peer_key and (
            path.startswith("/api/v1") or path.startswith("/api/peer") or path == "/health"
        ):
            import hashlib
            import hmac

            from fastapi.responses import JSONResponse

            expected = hashlib.sha256(("rynmesh-net-key:" + peer_key).encode("utf-8")).hexdigest()
            if not hmac.compare_digest(request.headers.get("x-ryn-auth", ""), expected):
                return JSONResponse({"detail": "Not Found"}, status_code=404)

        # /api/local is the private control surface. Peer APIs (/api/v1,
        # /health) stay open for P2P and the peer server may bind 0.0.0.0, so
        # this must never be reachable from the LAN or through a tunnel.
        #
        # Deny by default: everything under /api/local needs authorization
        # except the two unauthenticated auth routes below, so a route added
        # later is private without anyone remembering to gate it.
        if path.startswith("/api/local") and path not in _auth_open_paths:
            import hmac

            from fastapi.responses import JSONResponse

            client_host = (request.client.host if request.client else "") or ""

            if _local_token:
                # Per-launch token injected by the desktop shell; when set it
                # is the only accepted credential on this surface.
                if not hmac.compare_digest(
                    request.headers.get("x-ryn-local-token", ""), _local_token
                ):
                    return JSONResponse({"detail": "forbidden"}, status_code=403)
            else:
                decision = node_auth.authorize(
                    client_host=client_host,
                    headers=request.headers,
                    cookie=request.cookies.get(node_auth_mod.COOKIE_NAME, ""),
                )
                if not decision.allowed:
                    # 401, not 403: the client can fix this by unlocking, and
                    # the webapp keys its unlock prompt off this status.
                    return JSONResponse(
                        {"detail": "unauthorized", "unlock_required": True},
                        status_code=401,
                    )
        return await call_next(request)

    def route_id(value: str) -> str:
        try:
            return _validate_item_id(value)
        except PeerTransportError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    def control_network_id(value: str | None = None) -> str:
        return (
            str(value or os.environ.get("RYNMESH_NETWORK_ID", "rynmesh-main")).strip()
            or "rynmesh-main"
        )

    def local_control(request: FastAPIRequest) -> None:
        """Per-route re-check of the control gate.

        The middleware above is the real boundary; this is defense in depth for
        the ~60 routes that call it, so a change to the path prefix can't
        silently open them.
        """
        if os.environ.get("RYNMESH_ALLOW_REMOTE_CONTROL", "").strip() in {"1", "true", "yes"}:
            return
        if _local_token:
            # Already verified by the middleware, which is the only thing that
            # can accept this credential.
            return
        decision = node_auth.authorize(
            client_host=(request.client.host if request.client else "") or "",
            headers=request.headers,
            cookie=request.cookies.get(node_auth_mod.COOKIE_NAME, ""),
        )
        if not decision.allowed:
            raise HTTPException(status_code=401, detail="local_control_unauthorized")

    def first_http_endpoint(endpoints: Any) -> str:
        for endpoint in endpoints or []:
            value = str(endpoint)
            if value.startswith(("http://", "https://")):
                return value
        return ""

    def peer_slug(peer_id: str) -> str:
        return hashlib.sha256(peer_id.encode("utf-8")).hexdigest()[:16]

    def account_for(peer_id: str) -> dict[str, Any]:
        return active_store.credit_ledger.account(peer_id).to_dict()

    def peer_tier(peer_id: str) -> str:
        return active_store._resolve_identity_tier(peer_id).value

    def self_peer_record(network_id: str) -> dict[str, Any]:
        info = active_store.node_info()
        endpoint = str(info.get("peer_endpoint", "") or "")
        return {
            "peer_id": active_store.peer_id,
            "peer_slug": active_store.peer_slug,
            "node_name": info["node_name"],
            "endpoints": [endpoint] if endpoint else [],
            "network_id": network_id,
            "updated_at": "",
            "metadata": {
                "machine_name": info.get("machine_name", info["node_name"]),
                "hostname": info.get("hostname", ""),
                "primary_ip": info.get("primary_ip", ""),
                "ip_addresses": info.get("ip_addresses", []),
                "peer_endpoint": endpoint,
            },
        }

    def peer_item(record: dict[str, Any], *, is_self: bool = False) -> dict[str, Any]:
        peer_id = str(record.get("peer_id", "") or "")
        metadata = record.get("metadata", {})
        metadata = metadata if isinstance(metadata, dict) else {}
        account = account_for(peer_id)
        endpoint = first_http_endpoint(record.get("endpoints", []))
        return {
            "id": peer_id,
            "slug": str(record.get("peer_slug") or metadata.get("peer_slug") or peer_slug(peer_id)),
            "name": str(
                record.get("node_name") or metadata.get("machine_name") or peer_slug(peer_id)
            ),
            "endpoint": endpoint,
            "network": str(record.get("network_id", "")),
            "tier": peer_tier(peer_id),
            "credits": float(account["score"]),
            "weight": float(account["distribution_weight"]),
            "lastSeen": str(record.get("updated_at", "") or "local"),
            "served": 0,
            "fetched": 0,
            "trustedRoot": peer_id in active_store._trusted_root_ids(),
            "isSelf": is_self,
        }

    def discover_peer_items(network_id: str, *, include_self: bool = True) -> list[dict[str, Any]]:
        discovered = active_store.discover_peers(
            network_id=network_id,
            include_self=include_self,
            max_age_hours=float(os.environ.get("RYNMESH_DISCOVERY_MAX_AGE_HOURS", "24") or 24),
            use_cache_on_error=True,
        )
        records = list(discovered.get("peers", []))
        if include_self and not any(
            item.get("peer_id") == active_store.peer_id for item in records
        ):
            records.append(self_peer_record(network_id))
        items = [
            peer_item(item, is_self=item.get("peer_id") == active_store.peer_id) for item in records
        ]
        items.sort(key=lambda item: (not item.get("isSelf", False), item["name"], item["id"]))
        return items

    def ui_safety(outcome: str) -> str:
        return {
            "pass": "passed",
            "warn": "flagged",
            "block": "blocked",
            "passed": "passed",
            "flagged": "flagged",
            "blocked": "blocked",
        }.get(str(outcome or ""), "unscanned")

    def ui_kind(kind: str, content_type: str) -> str:
        value = str(kind or "").strip()
        if value:
            return value
        content_type = str(content_type or "")
        if content_type.startswith("image/"):
            return "image"
        if content_type.startswith("audio/"):
            return "audio"
        if content_type.startswith("video/"):
            return "video"
        if "pdf" in content_type or "text" in content_type:
            return "document"
        return "dataset"

    def human_size(size_bytes: Any) -> str:
        try:
            size = float(size_bytes)
        except (TypeError, ValueError):
            return ""
        units = ("B", "KB", "MB", "GB", "TB")
        index = 0
        while size >= 1024 and index < len(units) - 1:
            size /= 1024
            index += 1
        return f"{size:.1f} {units[index]}" if index else f"{int(size)} B"

    def file_hash(path: Path) -> str:
        digest = hashlib.sha256()
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
        return "sha256:" + digest.hexdigest()

    def content_item(
        raw: dict[str, Any], *, provider: dict[str, Any], fetched_ids: set[str]
    ) -> dict[str, Any]:
        content_id = str(raw.get("content_id") or raw.get("clip_id") or "")
        provider_peer_id = str(raw.get("provider_peer_id") or provider.get("peer_id") or "")
        publisher_peer_id = str(
            raw.get("publisher_peer_id") or raw.get("source_peer_id") or provider_peer_id
        )
        provider_account = account_for(provider_peer_id)
        publisher_account = account_for(publisher_peer_id)
        is_local_provider = provider_peer_id == active_store.peer_id
        if is_local_provider and publisher_peer_id == active_store.peer_id:
            fetch_status = "local"
        elif content_id in fetched_ids:
            fetch_status = "fetched_full"
        else:
            fetch_status = "discovered"
        return {
            "content_id": content_id,
            "manifest_hash": str(raw.get("manifest_hash", "")),
            "title": str(raw.get("title") or content_id),
            "description": str(raw.get("description", "")),
            "tags": list(raw.get("tags", [])) if isinstance(raw.get("tags", []), list) else [],
            "content_kind": ui_kind(
                str(raw.get("content_kind", "")), str(raw.get("content_type", ""))
            ),
            "content_type": str(raw.get("content_type", "")),
            "publisher_peer_id": publisher_peer_id,
            "provider_peer_id": provider_peer_id,
            "source_peer_name": str(
                provider.get("node_name", "") or provider.get("machine_name", "")
            ),
            "identity_tier": peer_tier(publisher_peer_id),
            "credit_score": float(publisher_account["score"]),
            "distribution_weight": max(
                float(provider_account["distribution_weight"]),
                float(publisher_account["distribution_weight"]),
            ),
            "safety_outcome": ui_safety(str(raw.get("safety_outcome", ""))),
            "safety_scanner_id": "rynmesh-keyword-safety",
            "safety_notes": "",
            "provenance_status": "signed" if raw.get("provenance_head_hash") else "unsigned",
            "provenance_head_hash": str(raw.get("provenance_head_hash") or "") or None,
            "fetch_status": fetch_status,
            "review_basis": "full" if fetch_status in {"local", "fetched_full"} else "metadata",
            "size": human_size(raw.get("size_bytes")),
            "published": str(raw.get("created_at", "")) or None,
        }

    def local_content_ids() -> set[str]:
        return {
            str(item.get("content_id") or item.get("clip_id") or "")
            for item in active_store.list_local_content().get("content", [])
        }

    def network_content(network_id: str) -> list[dict[str, Any]]:
        fetched_ids = local_content_ids()
        items: list[dict[str, Any]] = []
        self_record = self_peer_record(network_id)
        for raw in active_store.list_local_content().get("content", []):
            items.append(content_item(raw, provider=self_record, fetched_ids=fetched_ids))
        for peer in active_store.discover_peers(
            network_id=network_id,
            include_self=False,
            max_age_hours=float(os.environ.get("RYNMESH_DISCOVERY_MAX_AGE_HOURS", "24") or 24),
            use_cache_on_error=True,
        ).get("peers", []):
            endpoint = first_http_endpoint(peer.get("endpoints", []))
            if not endpoint:
                continue
            try:
                listing = active_store.list_peer_content(
                    endpoint,
                    timeout_s=_fetch_timeout(),
                )
            except (PeerTransportError, StoreError, OSError):
                continue
            provider = dict(peer)
            provider.update(dict(listing.get("peer", {})))
            for raw in listing.get("content", []):
                if isinstance(raw, dict):
                    items.append(content_item(raw, provider=provider, fetched_ids=fetched_ids))
        deduped: dict[tuple[str, str], dict[str, Any]] = {}
        for item in items:
            deduped[(item["content_id"], item["provider_peer_id"])] = item
        return sorted(
            deduped.values(),
            key=lambda item: (
                float(item.get("distribution_weight", 0)),
                str(item.get("published", "")),
            ),
            reverse=True,
        )

    def filtered_peers(
        items: list[dict[str, Any]], request: FastAPIRequest
    ) -> list[dict[str, Any]]:
        tier = str(request.query_params.get("tier", "all"))
        search = str(request.query_params.get("search", "")).strip().lower()
        if tier and tier != "all":
            items = [item for item in items if item.get("tier") == tier]
        if search:
            items = [
                item
                for item in items
                if search in str(item.get("name", "")).lower()
                or search in str(item.get("endpoint", "")).lower()
                or search in str(item.get("id", "")).lower()
            ]
        return items

    def filtered_content(
        items: list[dict[str, Any]], request: FastAPIRequest
    ) -> list[dict[str, Any]]:
        params = request.query_params
        source = str(params.get("source", "all"))
        kind = str(params.get("kind", "all"))
        safety = str(params.get("safety", "all"))
        tier = str(params.get("tier", "all"))
        provenance = str(params.get("provenance", "all"))
        search = str(params.get("search", "")).strip().lower()
        rank = str(params.get("rank", _settings.get()["rank_default"]))
        if source and source != "all":
            if source == "local":
                items = [item for item in items if item.get("fetch_status") == "local"]
            elif source == "fetched":
                items = [
                    item
                    for item in items
                    if item.get("fetch_status") in {"local", "preview_only", "fetched_full"}
                ]
            elif source == "discovered":
                items = [item for item in items if item.get("fetch_status") == "discovered"]
            else:
                items = [
                    item
                    for item in items
                    if item.get("publisher_peer_id") == source
                    or item.get("provider_peer_id") == source
                ]
        if kind and kind != "all":
            items = [item for item in items if item.get("content_kind") == kind]
        if safety and safety != "all":
            items = [item for item in items if item.get("safety_outcome") == safety]
        if tier and tier != "all":
            items = [item for item in items if item.get("identity_tier") == tier]
        if provenance and provenance != "all":
            items = [item for item in items if item.get("provenance_status") == provenance]
        if search:
            items = [
                item
                for item in items
                if search in str(item.get("title", "")).lower()
                or search in str(item.get("description", "")).lower()
                or any(search in str(tag).lower() for tag in item.get("tags", []))
            ]
        if rank == "newest":
            return sorted(items, key=lambda item: str(item.get("published", "")), reverse=True)
        if rank == "trusted":
            return sorted(items, key=lambda item: float(item.get("credit_score", 0)), reverse=True)
        if rank == "ai":
            return sorted(items, key=lambda item: float(item.get("ai_score", 0)), reverse=True)
        if rank == "novelty":
            return sorted(items, key=lambda item: float(item.get("novelty", 0)), reverse=True)
        return sorted(
            items, key=lambda item: float(item.get("distribution_weight", 0)), reverse=True
        )

    def registry_status(network_id: str) -> dict[str, Any]:
        info = active_store.node_info()["registry"]
        url = str(info.get("url") or info.get("path") or "")
        try:
            active_store.registry.list_peers(network_id=network_id, max_age_hours=24)
            status = "connected"
        except RegistryError as exc:
            status = "disconnected"
            app.state.registration_error = str(exc)
        return {"status": status, "url": url}

    def capacity_items(network_id: str, *, capability: str = "") -> list[dict[str, Any]]:
        capacities = active_store.list_job_capacities(
            network_id=network_id,
            capability=capability,
            max_age_hours=float(os.environ.get("RYNMESH_DISCOVERY_MAX_AGE_HOURS", "24") or 24),
        ).get("capacities", [])
        peer_names = {
            peer["id"]: peer["name"] for peer in discover_peer_items(network_id, include_self=True)
        }
        out: list[dict[str, Any]] = []
        for raw in capacities:
            item = dict(raw)
            item["provider_name"] = peer_names.get(
                str(item.get("peer_id", "")),
                str(item.get("node_name", "")),
            )
            out.append(item)
        out.sort(
            key=lambda item: (str(item.get("provider_name", "")), str(item.get("peer_id", "")))
        )
        return out

    def manifest_response(item_id: str) -> dict[str, Any]:
        try:
            return active_store.get_local_manifest(route_id(item_id)).to_dict()
        except (FileNotFoundError, StoreError) as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    def preview_response(item_id: str) -> Response:
        try:
            data = active_store.get_local_preview_bytes(route_id(item_id))
        except (FileNotFoundError, StoreError) as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        return Response(content=data, media_type="application/octet-stream")

    def bytes_response(item_id: str):
        try:
            content_path = active_store.get_local_content_path(route_id(item_id))
        except (FileNotFoundError, StoreError) as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        return FileResponse(content_path, media_type="application/octet-stream")

    @app.get("/health")
    def health() -> dict[str, Any]:
        return {
            "status": "ok",
            "peer_id": active_store.peer_id,
            "desktop_managed": os.environ.get("RYNMESH_DESKTOP_MODE", "").strip().lower()
            in {"1", "true", "yes"},
            "network_id": control_network_id(),
        }

    @app.get("/api/local/auth/status")
    def local_auth_status(request: FastAPIRequest) -> dict[str, Any]:
        """Whether this caller is already authorized, and how.

        Unauthenticated on purpose — it carries no secrets and the webapp needs
        it to decide whether to show the unlock prompt.
        """
        decision = node_auth.authorize(
            client_host=(request.client.host if request.client else "") or "",
            headers=request.headers,
            cookie=request.cookies.get(node_auth_mod.COOKIE_NAME, ""),
        )
        return {
            "authorized": decision.allowed,
            "via": decision.via,
            "remote": not node_auth_mod.is_loopback_addr(
                (request.client.host if request.client else "") or ""
            )
            or node_auth_mod.is_forwarded(request.headers),
        }

    @app.post("/api/local/auth/unlock")
    def local_auth_unlock(request: FastAPIRequest, body: dict[str, Any]) -> Any:
        """Exchange the device token for a session cookie."""
        from fastapi.responses import JSONResponse

        client_host = (request.client.host if request.client else "") or ""
        # Rate-limit against the real caller when proxied, so one tunnel client
        # can't hide behind the proxy's address.
        forwarded_for = request.headers.get("cf-connecting-ip") or request.headers.get(
            "x-forwarded-for", ""
        )
        client_key = (forwarded_for.split(",")[0].strip() or client_host) or "unknown"

        session = node_auth.unlock(str(body.get("token") or ""), client=client_key)
        if not session:
            if node_auth.is_rate_limited(client_key):
                return JSONResponse(
                    {"detail": "too_many_attempts"},
                    status_code=429,
                )
            return JSONResponse({"detail": "invalid_token"}, status_code=401)

        response = JSONResponse({"ok": True})
        response.set_cookie(
            node_auth_mod.COOKIE_NAME,
            session,
            httponly=True,
            samesite="lax",
            # Only mark Secure for genuinely remote callers: a plain-http
            # desktop origin would silently drop a Secure cookie.
            secure=node_auth_mod.is_forwarded(request.headers),
            max_age=int(node_auth.ttl_s),
            path="/",
        )
        return response

    @app.get("/api/local/auth/token")
    def local_auth_token(request: FastAPIRequest) -> dict[str, Any]:
        """Reveal the device token so the owner can pair another device.

        Gated: only an already-authorized caller (in practice, someone sitting
        at the machine) can read it.
        """
        local_control(request)
        return {"token": node_auth.token()}

    @app.post("/api/local/auth/rotate")
    def local_auth_rotate(request: FastAPIRequest) -> dict[str, Any]:
        local_control(request)
        return {"token": node_auth.rotate_token()}

    @app.get("/api/local/node/status")
    def local_node_status(request: FastAPIRequest) -> dict[str, Any]:
        local_control(request)
        network_id = control_network_id()
        local_items = active_store.list_local_content().get("content", [])
        return {
            "node_name": active_store.node_name,
            "peer_id": active_store.peer_id,
            "daemon_running": True,
            "desktop_managed": os.environ.get("RYNMESH_DESKTOP_MODE", "").strip().lower()
            in {"1", "true", "yes"},
            "registry": registry_status(network_id)["status"],
            "peer_count": max(0, len(discover_peer_items(network_id, include_self=True)) - 1),
            "local_items": sum(
                1 for item in local_items if item.get("publisher_peer_id") == active_store.peer_id
            ),
            "fetched_items": sum(
                1 for item in local_items if item.get("publisher_peer_id") != active_store.peer_id
            ),
            "pending_recs": 0,
            "version": f"ryn-node {RYNMESH_VERSION}",
            "uptime_seconds": int(time.monotonic() - started_at),
        }

    @app.get("/api/local/registry/status")
    def local_registry_status(request: FastAPIRequest) -> dict[str, Any]:
        local_control(request)
        return registry_status(control_network_id())

    @app.get("/api/local/jobs/capacity")
    def local_job_capacity(
        request: FastAPIRequest,
        capability: str = "",
        network_id: str = "",
    ) -> list[dict[str, Any]]:
        local_control(request)
        return capacity_items(control_network_id(network_id), capability=capability)

    @app.post("/api/local/jobs/capacity/register")
    async def local_register_job_capacity(request: FastAPIRequest) -> dict[str, Any]:
        local_control(request)
        body = await request.json()
        return active_store.register_job_capacity(
            capabilities=tuple(str(item) for item in body.get("capabilities", []) if str(item)),
            network_id=control_network_id(body.get("network_id")),
            capacity_units=int(body.get("capacity_units") or 1),
            max_concurrent=int(body.get("max_concurrent") or 1),
            price_credits=dict(body.get("price_credits", {}) or {}),
            polling_interval_sec=int(body.get("polling_interval_sec") or 30),
            metadata=dict(body.get("metadata", {}) or {}),
        )

    @app.post("/api/local/jobs/work-orders")
    async def local_submit_work_order(request: FastAPIRequest) -> dict[str, Any]:
        local_control(request)
        body = await request.json()
        return active_store.submit_work_order(
            provider_peer_id=str(body.get("provider_peer_id", "")),
            capability=str(body.get("capability", "")),
            operation=str(body.get("operation", "")),
            params=dict(body.get("params", {}) or {}),
            network_id=control_network_id(body.get("network_id")),
            input_content_ids=tuple(
                str(item) for item in body.get("input_content_ids", []) if str(item)
            ),
            max_credit_cost=float(body.get("max_credit_cost") or 0.0),
            idempotency_key=str(body.get("idempotency_key", "")),
            result_policy=dict(body.get("result_policy", {}) or {}),
            expires_at=str(body.get("expires_at", "")),
            expires_in_hours=float(body.get("expires_in_hours") or 6.0),
        )

    @app.get("/api/local/jobs/work-orders")
    def local_work_orders(
        request: FastAPIRequest,
        capability: str = "",
        status: str = "open",
        network_id: str = "",
    ) -> dict[str, Any]:
        local_control(request)
        return active_store.poll_work_orders(
            network_id=control_network_id(network_id),
            capability=capability,
            status=status or "open",
        )

    @app.get("/api/local/jobs/work-results")
    def local_work_results(
        request: FastAPIRequest,
        work_order_id: str = "",
        status: str = "",
        network_id: str = "",
    ) -> dict[str, Any]:
        local_control(request)
        return active_store.list_work_results(
            work_order_id=work_order_id,
            network_id=control_network_id(network_id),
            requester_peer_id=active_store.peer_id,
            status=status,
        )

    from .services.egress_control import EgressController

    egress_controller = EgressController(active_store)

    @app.post("/api/local/egress/connect")
    async def local_egress_connect(request: FastAPIRequest) -> dict[str, Any]:
        local_control(request)
        body = await request.json()
        return egress_controller.connect(
            region=str(body.get("region") or "CN"),
            provider_peer_id=str(body.get("provider_peer_id") or ""),
        )

    @app.get("/api/local/egress/status")
    def local_egress_status(request: FastAPIRequest, region: str = "CN") -> dict[str, Any]:
        local_control(request)
        return egress_controller.status(region=region or "CN")

    @app.post("/api/local/egress/launch")
    async def local_egress_launch(request: FastAPIRequest) -> dict[str, Any]:
        local_control(request)
        body = await request.json()
        urls = body.get("urls")
        urls = [str(u) for u in urls] if isinstance(urls, list) else None
        return await _asyncio.to_thread(
            egress_controller.launch, str(body.get("region") or "CN"), urls
        )

    @app.post("/api/local/egress/disconnect")
    async def local_egress_disconnect(request: FastAPIRequest) -> dict[str, Any]:
        local_control(request)
        body = await request.json()
        return egress_controller.disconnect(region=str(body.get("region") or "CN"))

    from .services.peer_health import PeerHealthProbe

    peer_health_probe = PeerHealthProbe()

    @app.post("/api/local/peers/health")
    async def local_peers_health(request: FastAPIRequest) -> list[dict[str, Any]]:
        local_control(request)
        items = discover_peer_items(control_network_id(), include_self=True)
        peers = [
            {
                "id": it["id"],
                "endpoint": it.get("endpoint", ""),
                "isSelf": bool(it.get("isSelf", False)),
            }
            for it in items
        ]
        return peer_health_probe.check(peers)

    @app.post("/api/local/peers/discover")
    async def local_discover_peers(request: FastAPIRequest) -> list[dict[str, Any]]:
        local_control(request)
        try:
            body = await request.json()
        except json.JSONDecodeError:
            body = {}
        network_id = control_network_id(body.get("network") or body.get("network_id"))
        active_store.register_node(network_id=network_id)
        return discover_peer_items(network_id, include_self=True)

    @app.get("/api/local/peers")
    def local_peers(request: FastAPIRequest) -> list[dict[str, Any]]:
        local_control(request)
        return filtered_peers(discover_peer_items(control_network_id(), include_self=True), request)

    @app.get("/api/local/content")
    def local_content(request: FastAPIRequest) -> list[dict[str, Any]]:
        local_control(request)
        return filtered_content(network_content(control_network_id()), request)

    @app.get("/api/local/content/{content_id}")
    def local_content_detail(content_id: str, request: FastAPIRequest) -> dict[str, Any]:
        local_control(request)
        content_id = route_id(content_id)
        for item in network_content(control_network_id()):
            if item["content_id"] == content_id:
                return item
        raise HTTPException(status_code=404, detail="content_not_found")

    @app.get("/api/local/content/{content_id}/body")
    def local_content_body(content_id: str, request: FastAPIRequest) -> dict[str, Any]:
        local_control(request)
        content_id = route_id(content_id)
        try:
            manifest = active_store.get_local_manifest(content_id)
            validation = active_store._validate_peer_manifest(manifest)
            content_path = active_store.get_local_content_path(content_id)
        except (FileNotFoundError, StoreError) as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        if validation.manifest is None:
            raise HTTPException(status_code=404, detail="manifest_not_found")
        content_type = validation.manifest.asset.media_type
        if not (
            content_type.startswith("text/")
            or content_type in {"application/json", "application/xml", "application/x-yaml"}
            or content_path.suffix.lower() in {".md", ".markdown", ".txt", ".json", ".yaml", ".yml"}
        ):
            raise HTTPException(status_code=415, detail="content_body_not_text")
        max_body_bytes = int(os.environ.get("RYNMESH_LOCAL_BODY_MAX_BYTES", str(1024 * 1024)))
        data = content_path.read_bytes()[: max_body_bytes + 1]
        truncated = len(data) > max_body_bytes
        if truncated:
            data = data[:max_body_bytes]
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise HTTPException(status_code=415, detail="content_body_not_utf8") from exc
        return {
            "ok": True,
            "content_id": content_id,
            "content_type": content_type,
            "size": human_size(content_path.stat().st_size),
            "truncated": truncated,
            "text": text,
        }

    @app.get("/api/local/content/{content_id}/preview-bytes")
    def local_content_preview_bytes(content_id: str, request: FastAPIRequest) -> Response:
        local_control(request)
        return preview_response(content_id)

    @app.get("/api/local/content/{content_id}/bytes")
    def local_content_bytes(content_id: str, request: FastAPIRequest):
        local_control(request)
        return bytes_response(content_id)

    @app.post("/api/local/content/{content_id}/fetch-preview")
    async def local_fetch_preview(content_id: str, request: FastAPIRequest) -> dict[str, Any]:
        local_control(request)
        body = await request.json()
        provider_peer_id = str(body.get("providerPeerId", ""))
        for peer in active_store.discover_peers(
            network_id=control_network_id(), include_self=False
        ).get("peers", []):
            if peer.get("peer_id") != provider_peer_id:
                continue
            endpoint = first_http_endpoint(peer.get("endpoints", []))
            result = active_store.fetch_peer_content_preview(
                endpoint,
                route_id(content_id),
                expected_peer_id=provider_peer_id,
                timeout_s=_fetch_timeout(),
            )
            return {"ok": True, "size": human_size(Path(result["preview_path"]).stat().st_size)}
        raise HTTPException(status_code=404, detail="provider_peer_not_found")

    @app.post("/api/local/content/{content_id}/fetch-full")
    async def local_fetch_full(content_id: str, request: FastAPIRequest) -> dict[str, Any]:
        local_control(request)
        body = await request.json()
        provider_peer_id = str(body.get("providerPeerId", ""))
        for peer in active_store.discover_peers(
            network_id=control_network_id(), include_self=False
        ).get("peers", []):
            if peer.get("peer_id") != provider_peer_id:
                continue
            endpoint = first_http_endpoint(peer.get("endpoints", []))
            result = active_store.fetch_peer_content_full(
                endpoint,
                route_id(content_id),
                expected_peer_id=provider_peer_id,
                timeout_s=_fetch_timeout(),
            )
            return {"ok": True, "size": human_size(Path(result["content_path"]).stat().st_size)}
        raise HTTPException(status_code=404, detail="provider_peer_not_found")

    @app.get("/api/local/provenance/{head_hash}")
    def local_provenance(head_hash: str, request: FastAPIRequest) -> list[dict[str, Any]]:
        local_control(request)
        if head_hash in {"", "none", "null"}:
            return []
        for raw in active_store.list_local_content().get("content", []):
            content_id = str(raw.get("content_id", ""))
            try:
                manifest = active_store.get_local_manifest(content_id)
            except (FileNotFoundError, StoreError):
                continue
            payload = manifest.payload
            if payload.get("provenance_head_hash") != head_hash:
                continue
            events = []
            for link in payload.get("provenance_chain", []):
                link_payload = dict(link.get("payload", {}))
                link_type = str(link_payload.get("link_type", "provenance"))
                events.append(
                    {
                        "t": str(link_payload.get("created_at", "")),
                        "label": link_type,
                        "actor": str(link_payload.get("issuer_peer_id", "")),
                        "kind": "scan" if link_type == "safety_scan" else "publish",
                        "hash": str(link.get("signature", ""))[:24],
                    }
                )
            return events
        return []

    from .services import ask as ask_service
    from .services import model_provider as model_provider_module
    from .services import recap as recap_service
    from .services.assistant_audit import AssistantAuditStore
    from .services.assistant_context import (
        MAX_CONTEXT_BYTES,
        AssistantContextError,
        AssistantContextStore,
    )
    from .services.assistant_router import AssistantRouteError, local_targets
    from .services.assistant_runs import AssistantRunService
    from .services.consumption import ConsumptionError, ConsumptionStore
    from .services.digest import DigestError, DigestService
    from .services.first_success import FirstSuccessStore
    from .services.library import LibraryError, LibraryService
    from .services.library_imports import MAX_IMPORT_BYTES, LibraryImportError, LibraryImportStore
    from .services.library_metadata import LibraryMetadataError, LibraryMetadataStore
    from .services.reader import ReaderCache, ReaderError, read_article

    desktop_discovery = os.environ.get("RYNMESH_DESKTOP_MODE", "").strip().lower() in {
        "1",
        "true",
        "yes",
    }
    discovery_override = os.environ.get("RYNMESH_DEFAULT_DISCOVERY", "").strip().lower()
    if discovery_override:
        desktop_discovery = discovery_override in {"1", "true", "yes"}
    app.state.digest_service = DigestService(
        active_store.home,
        bootstrap_defaults=desktop_discovery,
        profile_store=_recommendation_profile,
    )
    app.state.reader_cache = ReaderCache(active_store.home / "reader-cache")
    app.state.consumption_store = ConsumptionStore(active_store.home / "consumption.json")
    app.state.library_imports = LibraryImportStore(active_store.home / "library" / "imports")
    app.state.library_metadata = LibraryMetadataStore(active_store.home / "library" / "metadata.json")
    app.state.library_service = LibraryService(
        consumption=app.state.consumption_store,
        imports=app.state.library_imports,
        metadata=app.state.library_metadata,
    )
    app.state.library_enabled = os.environ.get("RYNMESH_LIBRARY_WORKSPACE_V1", "1") != "0"
    app.state.first_success_store = FirstSuccessStore(active_store.home / "first-success.json")
    app.state.first_success_enabled = os.environ.get("RYNMESH_FIRST_SUCCESS_V1_ENABLED", "1") != "0"
    app.state.assistant_audit = AssistantAuditStore(active_store.home / "assistant-audit.json")
    app.state.model_provider = None
    app.state.model_provider_checked_at = 0.0

    def _digest_service() -> DigestService:
        return app.state.digest_service

    def _library_service() -> LibraryService:
        return app.state.library_service

    def _audit() -> AssistantAuditStore:
        return app.state.assistant_audit

    def _first_success_status() -> dict[str, Any]:
        discovery = _digest_service().discovery_status()
        consumption = app.state.consumption_store.list()
        profile = _recommendation_profile.public()
        item_count = int(discovery.get("item_count", 0) or 0)
        existing = app.state.first_success_store.get()
        replay_since = float(existing.get("replay_started_at_unix", 0.0) or 0.0)
        opened = any(
            float(record.get("last_opened_unix", 0.0) or 0.0) >= replay_since
            for record in consumption
            if float(record.get("last_opened_unix", 0.0) or 0.0) > 0
        )
        signal = replay_since <= 0 and (
            any(bool(record.get("bookmarked")) for record in consumption)
            or bool(int(profile.get("feedback_count", 0) or 0))
        )
        stored = app.state.first_success_store.sync(
            node_ready=True,
            content_ready=item_count > 0,
            first_item_opened=opened,
            first_signal_recorded=signal,
        )
        milestones = dict(stored.get("milestones", {}))
        completed = bool(float(milestones.get("completed", 0.0) or 0.0))
        if completed:
            phase = "completed"
        elif bool(float(milestones.get("first_item_opened", 0.0) or 0.0)):
            phase = "awaiting_signal"
        elif item_count > 0:
            phase = "ready"
        elif str(discovery.get("phase", "")) == "error" or (
            int(discovery.get("source_count", 0) or 0) > 0
            and int(discovery.get("failed_sources", 0) or 0)
            >= int(discovery.get("source_count", 0) or 0)
        ):
            phase = "needs_action"
        else:
            phase = "checking_sources"
        return {
            "version": "ryn.first-success.v1",
            "phase": phase,
            "completed": completed,
            "dismissed": bool(stored.get("dismissed", False)),
            "node_ready": bool(float(milestones.get("node_ready", 0.0) or 0.0)),
            "content_ready": bool(float(milestones.get("content_ready", 0.0) or 0.0)),
            "item_count": item_count,
            "healthy_sources": int(discovery.get("healthy_sources", 0) or 0),
            "source_count": int(discovery.get("source_count", 0) or 0),
            "failed_sources": int(discovery.get("failed_sources", 0) or 0),
            "degraded": bool(discovery.get("degraded", False)),
            "using_cache": bool(int(discovery.get("cached_sources", 0) or 0)),
            "first_item_opened": bool(
                float(milestones.get("first_item_opened", 0.0) or 0.0)
            ),
            "first_signal_recorded": bool(
                float(milestones.get("first_signal_recorded", 0.0) or 0.0)
            ),
            "milestones": milestones,
            "safe_error": "discovery_unavailable" if phase == "needs_action" else None,
            "recoverable_actions": ["retry_discovery"] if phase == "needs_action" else [],
        }

    def _record_first_success(milestone: str) -> None:
        """A damaged progress file must never break the user's real action."""
        if not app.state.first_success_enabled:
            return
        try:
            app.state.first_success_store.record(milestone)
        except (OSError, TypeError, ValueError):
            try:
                _audit().append(
                    "verify",
                    "First-success progress could not be saved",
                    details={"code": "first_success_store_unavailable"},
                )
            except Exception:  # noqa: BLE001
                pass

    def _preferred_model() -> str:
        """The owner's explicit choice, if they've made one."""
        return str(_settings.get().get("ai_model", "") or "")

    def _cloud_model_allowed() -> bool:
        stored = _settings.get()
        return bool(stored.get("cloud_access")) and stored.get("ai_provider") == "cloud"

    def _model_provider():
        # Cache the resolved provider; while absent, re-probe at most every 30s
        # so starting Ollama (or exporting a key) is picked up without restart.
        # A changed model preference invalidates the cache immediately.
        provider = app.state.model_provider
        managed = getattr(app.state, "llm_provider", None)
        if getattr(provider, "id", "") == "managed-local" and managed is None:
            provider = app.state.model_provider = None
        if managed is not None:
            try:
                if managed.adapter.health().get("ok"):
                    app.state.model_provider = managed
                    return managed
            except Exception:  # noqa: BLE001 - fall through to other local providers
                pass
            if provider is managed:
                provider = app.state.model_provider = None
        preferred = _preferred_model()
        if provider is not None and preferred and getattr(provider, "model", "") != preferred:
            provider = app.state.model_provider = None
        if provider is None and time.time() - app.state.model_provider_checked_at > 30:
            app.state.model_provider_checked_at = time.time()
            try:
                app.state.model_provider = model_provider_module.resolve_provider(
                    preferred_model=preferred,
                    allow_cloud=_cloud_model_allowed(),
                )
            except model_provider_module.ModelProviderError:
                app.state.model_provider = None
        return app.state.model_provider

    def _reset_model_provider() -> None:
        app.state.model_provider = None
        app.state.model_provider_checked_at = 0.0

    def _assistant_search_contexts(query: str) -> list[dict[str, Any]]:
        words = [word for word in re.findall(r"[\w']+", query.lower()) if len(word) > 2]
        candidates: list[tuple[int, dict[str, Any]]] = []
        digest = _digest_service().last_digest() or {}
        for item in digest.get("items", []):
            text = " ".join([
                str(item.get("title", "")),
                str(item.get("ai_summary") or item.get("summary") or ""),
            ])
            score = sum(1 for word in words if word in text.lower())
            if score or not words:
                candidates.append((score, {
                    "title": str(item.get("title", "Result")),
                    "text": text[:1200],
                    "source_url": str(item.get("link", "")) or None,
                    "source_ref": None,
                }))
        for item in network_content(control_network_id()):
            text = " ".join([
                str(item.get("title", "")), str(item.get("description", "")),
                " ".join(str(tag) for tag in item.get("tags", [])),
            ])
            score = sum(1 for word in words if word in text.lower())
            if score:
                candidates.append((score, {
                    "title": str(item.get("title", "Result")),
                    "text": text[:1200],
                    "source_url": None,
                    "source_ref": str(item.get("content_id", "")) or None,
                }))
        candidates.sort(key=lambda item: -item[0])
        contexts: list[dict[str, Any]] = []
        for index, (_score, item) in enumerate(candidates[:6], start=1):
            text = str(item["text"])
            contexts.append({
                "version": "ryn.assistant-context.v1",
                "id": f"search_{index}_{hashlib.sha256(text.encode('utf-8')).hexdigest()[:12]}",
                "kind": "search-result",
                "title": item["title"],
                "source_url": item["source_url"],
                "source_ref": item["source_ref"],
                "content_hash": "sha256:" + hashlib.sha256(text.encode("utf-8")).hexdigest(),
                "text": text,
                "included_bytes": len(text.encode("utf-8")),
                "original_bytes": len(text.encode("utf-8")),
                "truncated": False,
            })
        return contexts

    def _assistant_targets() -> list[dict[str, Any]]:
        targets = local_targets(_model_provider)
        discover_services = getattr(app.state, "llm_discover_services", None)
        if not callable(discover_services):
            return targets
        try:
            services = discover_services(control_network_id())
        except Exception:  # noqa: BLE001
            return targets
        for record in services:
            service_record = dict(record.get("service") or {})
            peer_id = str(record.get("peer_id", ""))
            package_id = str(service_record.get("package_id", ""))
            if not peer_id or not package_id:
                continue
            targets.append({
                "key": "friend:" + hashlib.sha256(
                    f"{peer_id}\0{package_id}".encode("utf-8")
                ).hexdigest()[:24],
                "label": f"Friend device · {record.get('node_name') or peer_id[:12]}",
                "privacy_boundary": "friend",
                "provider_peer_id": peer_id,
                "package_id": package_id,
                "model_id": str(service_record.get("model_alias", "")),
                "online": bool(record.get("online", False)),
                "route_modes": ["remote_provider"],
                "pricing": service_record.get("pricing"),
            })
        return targets

    class _AssistantRemoteProvider:
        id = "friend"

        def __init__(self, target: dict[str, Any]):
            self.target = target
            self.model = str(target.get("model_id", ""))

        def generate(self, prompt: str, *, max_tokens: int, **_kwargs: Any) -> str:
            execute = getattr(app.state, "llm_execute_order", None)
            if not callable(execute):
                raise RuntimeError("friend provider is unavailable")
            result = _asyncio.run(execute({
                "network_id": control_network_id(),
                "provider_peer_id": self.target["provider_peer_id"],
                "service_id": self.target["package_id"],
                "prompt": prompt,
                "max_tokens": max_tokens,
                "transport": "auto",
            }))
            if result.get("state") not in {"succeeded", "completed"}:
                raise RuntimeError("friend provider did not complete")
            return str(result.get("output", ""))

    def _assistant_provider(target_key: str):
        target = next((item for item in _assistant_targets() if item["key"] == target_key), None)
        if target is None:
            return None
        if target["privacy_boundary"] == "device":
            return _model_provider()
        if target["privacy_boundary"] == "friend":
            return _AssistantRemoteProvider(target)
        return None

    app.state.assistant_enabled = os.environ.get("RYNMESH_ASSISTANT_WORKSPACE_V1", "1") != "0"
    app.state.assistant_contexts = AssistantContextStore(active_store.home / "assistant" / "contexts")
    app.state.assistant_runs = AssistantRunService(
        active_store.home / "assistant" / "runs",
        contexts=app.state.assistant_contexts,
        targets=_assistant_targets,
        provider=_assistant_provider,
        search_contexts=_assistant_search_contexts,
    )

    def _assistant_enabled() -> None:
        if not app.state.assistant_enabled:
            raise HTTPException(status_code=404, detail="assistant_workspace_disabled")

    @app.get("/api/local/assistant/targets")
    def local_assistant_targets(request: FastAPIRequest) -> dict[str, Any]:
        local_control(request)
        _assistant_enabled()
        return {"version": "ryn.assistant-targets.v1", "targets": app.state.assistant_runs.list_targets()}

    @app.post("/api/local/assistant/context/prepare")
    async def local_assistant_context_prepare(request: FastAPIRequest) -> dict[str, Any]:
        local_control(request)
        _assistant_enabled()
        body = dict(await request.json())
        source_kind = str(body.get("source_kind", ""))
        try:
            if source_kind == "content-item":
                content_id = route_id(str(body.get("content_id", "")))
                manifest = active_store.get_local_manifest(content_id)
                validation = active_store._validate_peer_manifest(manifest)
                if validation.manifest is None:
                    raise FileNotFoundError(content_id)
                path = active_store.get_local_content_path(content_id)
                media_type = str(validation.manifest.asset.media_type)
                body.update({
                    "title": str(validation.manifest.title or content_id),
                    "source_ref": content_id,
                    "kind": "pdf" if media_type == "application/pdf" else "content-item",
                })
                if body["kind"] == "pdf":
                    import base64

                    if path.stat().st_size > MAX_CONTEXT_BYTES:
                        raise AssistantContextError(
                            "assistant_context_too_large", "The selected PDF is too large."
                        )
                    body["data_base64"] = base64.b64encode(path.read_bytes()).decode("ascii")
                else:
                    if path.stat().st_size > MAX_CONTEXT_BYTES:
                        raise AssistantContextError(
                            "assistant_context_too_large", "The selected document is too large."
                        )
                    body["text"] = path.read_text(encoding="utf-8")
            elif source_kind == "reader-article":
                url = str(body.get("url", ""))
                article = read_article(
                    url, fetcher=_digest_service().fetcher, cache=app.state.reader_cache
                )
                body.update({
                    "kind": "reader-article",
                    "title": str(article.get("title", "Article")),
                    "source_url": url,
                    "text": "\n\n".join(
                        str(block.get("text", "")) for block in article.get("blocks", [])
                    ),
                })
            elif source_kind == "library-item":
                library_id = str(body.get("library_id", ""))
                item = _library_service().get(library_id)
                if not item["capabilities"]["ask"]:
                    raise AssistantContextError(
                        "assistant_context_missing", "That Library item is not available locally."
                    )
                if library_id.startswith("import:"):
                    text = app.state.library_imports.extracted_text(library_id.split(":", 1)[1])
                    source_url = ""
                else:
                    source_url = str(item.get("source", {}).get("url", ""))
                    article = read_article(
                        source_url, fetcher=_digest_service().fetcher, cache=app.state.reader_cache
                    )
                    text = "\n\n".join(
                        str(block.get("text", "")) for block in article.get("blocks", [])
                    )
                body.update({
                    "kind": "library-item", "title": item["title"],
                    "source_ref": library_id, "source_url": source_url, "text": text,
                })
            return app.state.assistant_contexts.prepare(body)
        except AssistantContextError as exc:
            raise HTTPException(status_code=400, detail=exc.code) from exc
        except (FileNotFoundError, StoreError, UnicodeDecodeError, LibraryError, LibraryImportError) as exc:
            raise HTTPException(status_code=404, detail="assistant_context_missing") from exc
        except ReaderError as exc:
            raise HTTPException(status_code=400, detail="assistant_context_missing") from exc

    @app.post("/api/local/assistant/runs")
    async def local_assistant_run_create(request: FastAPIRequest) -> dict[str, Any]:
        local_control(request)
        _assistant_enabled()
        try:
            return app.state.assistant_runs.create(dict(await request.json()))
        except AssistantRouteError as exc:
            raise HTTPException(status_code=409, detail=exc.code) from exc
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.get("/api/local/assistant/runs/{run_id}")
    def local_assistant_run_get(run_id: str, request: FastAPIRequest) -> dict[str, Any]:
        local_control(request)
        _assistant_enabled()
        try:
            return app.state.assistant_runs.get(run_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="assistant_run_not_found") from exc

    @app.post("/api/local/assistant/runs/{run_id}/cancel")
    def local_assistant_run_cancel(run_id: str, request: FastAPIRequest) -> dict[str, Any]:
        local_control(request)
        _assistant_enabled()
        try:
            return app.state.assistant_runs.cancel(run_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="assistant_run_not_found") from exc

    @app.get("/api/local/ai/status")
    def local_ai_status(request: FastAPIRequest) -> dict[str, Any]:
        local_control(request)
        provider = _model_provider()
        if provider is None:
            return {"provider": None, "model": None}
        return {"provider": provider.id, "model": provider.model}

    @app.get("/api/local/first-success")
    def local_first_success(request: FastAPIRequest) -> dict[str, Any]:
        local_control(request)
        if not app.state.first_success_enabled:
            raise HTTPException(status_code=404, detail="first_success_disabled")
        return _first_success_status()

    @app.post("/api/local/first-success/dismiss")
    def local_first_success_dismiss(request: FastAPIRequest) -> dict[str, Any]:
        local_control(request)
        if not app.state.first_success_enabled:
            raise HTTPException(status_code=404, detail="first_success_disabled")
        app.state.first_success_store.dismiss()
        return _first_success_status()

    @app.post("/api/local/first-success/reset")
    def local_first_success_reset(request: FastAPIRequest) -> dict[str, Any]:
        local_control(request)
        if not app.state.first_success_enabled:
            raise HTTPException(status_code=404, detail="first_success_disabled")
        app.state.first_success_store.reset()
        return _first_success_status()

    @app.get("/api/local/ai/models")
    def local_ai_models(request: FastAPIRequest) -> dict[str, Any]:
        local_control(request)
        provider = _model_provider()
        catalog = model_provider_module.list_local_models(
            current=getattr(provider, "model", "") if provider else ""
        )
        catalog["provider"] = provider.id if provider else None
        catalog["selected"] = _preferred_model()
        return catalog

    @app.post("/api/local/ai/model")
    async def local_ai_select_model(request: FastAPIRequest) -> dict[str, Any]:
        local_control(request)
        body = await request.json()
        model = str(body.get("model", "") or "").strip()
        if model:
            installed = {
                entry["name"] for entry in model_provider_module.OllamaProvider().installed()
            }
            if model not in installed:
                raise HTTPException(
                    status_code=400,
                    detail=f"model_not_installed: pull it first with `ollama pull {model}`",
                )
        # "" clears the choice and returns to automatic selection.
        _settings.patch({"ai_model": model})
        _reset_model_provider()
        provider = _model_provider()
        return {
            "ok": True,
            "selected": model,
            "provider": provider.id if provider else None,
            "model": getattr(provider, "model", None) if provider else None,
        }

    @app.get("/api/local/sources")
    def local_sources(request: FastAPIRequest) -> list[dict[str, Any]]:
        local_control(request)
        return _digest_service().list_sources()

    @app.post("/api/local/sources")
    async def local_sources_add(request: FastAPIRequest) -> dict[str, Any]:
        local_control(request)
        body = await request.json()
        tags = body.get("tags")
        try:
            return _digest_service().add_source(
                str(body.get("url", "")),
                tags=[str(tag) for tag in tags] if isinstance(tags, list) else None,
            )
        except DigestError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.delete("/api/local/sources/{source_id}")
    def local_sources_remove(source_id: str, request: FastAPIRequest) -> dict[str, Any]:
        local_control(request)
        if not _digest_service().remove_source(source_id):
            raise HTTPException(status_code=404, detail="source_not_found")
        return {"ok": True}

    @app.get("/api/local/digest")
    def local_digest(request: FastAPIRequest) -> dict[str, Any]:
        local_control(request)
        digest = _digest_service().last_digest()
        return digest or {"generated_at_unix": 0.0, "items": [], "sources": []}

    @app.get("/api/local/discovery/status")
    def local_discovery_status(request: FastAPIRequest) -> dict[str, Any]:
        local_control(request)
        return _digest_service().discovery_status()

    @app.post("/api/local/discovery/seen")
    def local_discovery_seen(request: FastAPIRequest) -> dict[str, Any]:
        local_control(request)
        return _digest_service().mark_discovery_seen(now_unix=time.time())

    @app.post("/api/local/digest/refresh")
    def local_digest_refresh(request: FastAPIRequest) -> dict[str, Any]:
        local_control(request)
        service = _digest_service()
        result = service.proactive_refresh(now_unix=time.time())
        provider = _model_provider()
        if provider is not None:
            result["digest"] = service.enrich_latest(provider)
        status = result["status"]
        _audit().append(
            "rec",
            f"Manual discovery prepared {status['item_count']} recommendations",
            details={
                "healthy_sources": status["healthy_sources"],
                "failed_sources": status["failed_sources"],
                "formats": status["formats"],
                "ai_provider": getattr(provider, "id", None),
                "network_access": True,
            },
        )
        return result

    @app.post("/api/local/readlater")
    async def local_readlater(request: FastAPIRequest) -> dict[str, Any]:
        local_control(request)
        body = await request.json()
        try:
            return _digest_service().save_link(str(body.get("url", "")), now_unix=time.time())
        except DigestError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.get("/api/local/watchers")
    def local_watchers(request: FastAPIRequest) -> list[dict[str, Any]]:
        local_control(request)
        return _digest_service().list_watchers()

    @app.post("/api/local/watchers")
    async def local_watchers_add(request: FastAPIRequest) -> dict[str, Any]:
        local_control(request)
        body = await request.json()
        try:
            return _digest_service().add_watcher(
                str(body.get("url", "")), note=str(body.get("note", ""))
            )
        except DigestError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.delete("/api/local/watchers/{watcher_id}")
    def local_watchers_remove(watcher_id: str, request: FastAPIRequest) -> dict[str, Any]:
        local_control(request)
        if not _digest_service().remove_watcher(watcher_id):
            raise HTTPException(status_code=404, detail="watcher_not_found")
        return {"ok": True}

    # ---- daily recap email --------------------------------------------------
    def _recap_config():
        return recap_service.RecapConfig.from_settings(_settings.get(), port=_public_port())

    def _public_port() -> int:
        return int(os.environ.get("RYNMESH_PEER_PORT", "8791") or 8791)

    def _build_recap_payload():
        service = _digest_service()
        digest = service.last_digest() or service.build(
            now_unix=time.time(), provider=_model_provider()
        )
        config = _recap_config()
        recap = recap_service.build_recap(
            digest, per_source=config.per_source, now_unix=time.time()
        )
        return recap, config

    @app.get("/api/local/recap/settings")
    def local_recap_settings(request: FastAPIRequest) -> dict[str, Any]:
        local_control(request)
        config = _recap_config()
        return {
            "to_address": config.to_address,
            "from_address": config.from_address,
            "smtp_host": config.smtp_host,
            "smtp_port": config.smtp_port,
            "smtp_user": config.smtp_user,
            "use_tls": config.use_tls,
            "base_url": config.base_url,
            "per_source": config.per_source,
            # never echo the password back
            "password_set": bool(config.smtp_password),
            "pdf_available": recap_service.pdf_available(),
            "send_hour_utc": int(_settings.get().get("recap", {}).get("send_hour_utc", 13)),
            "enabled": bool(_settings.get().get("recap", {}).get("enabled", False)),
            "last_sent_unix": float(_settings.get().get("recap", {}).get("last_sent_unix", 0)),
        }

    @app.patch("/api/local/recap/settings")
    async def local_recap_settings_update(request: FastAPIRequest) -> dict[str, Any]:
        local_control(request)
        body = await request.json()
        current = dict(_settings.get().get("recap", {}) or {})
        for key in (
            "to_address",
            "from_address",
            "smtp_host",
            "smtp_user",
            "base_url",
            "smtp_password",
        ):
            if key in body:
                current[key] = str(body[key])
        for key in ("smtp_port", "per_source", "send_hour_utc"):
            if key in body:
                current[key] = int(body[key])
        for key in ("use_tls", "enabled"):
            if key in body:
                current[key] = bool(body[key])
        _settings.patch({"recap": current})
        return local_recap_settings(request)

    @app.get("/api/local/recap/preview.pdf")
    def local_recap_preview(request: FastAPIRequest) -> Any:
        """The exact PDF the email would carry — check it before enabling."""
        local_control(request)
        recap, config = _build_recap_payload()
        pdf = recap_service.render_pdf(
            recap, base_url=config.base_url, node_name=active_store.node_name
        )
        if not pdf:
            raise HTTPException(
                status_code=503,
                detail="pdf_renderer_missing: install reportlab to attach a PDF",
            )
        return Response(content=pdf, media_type="application/pdf")

    def _send_recap_now() -> dict[str, Any]:
        """Shared by the endpoint and the daily scheduler."""
        recap, config = _build_recap_payload()
        pdf = recap_service.render_pdf(
            recap, base_url=config.base_url, node_name=active_store.node_name
        )
        message = recap_service.compose_email(
            recap, config=config, pdf=pdf, node_name=active_store.node_name
        )
        result = recap_service.send_email(message, config=config)
        stored = dict(_settings.get().get("recap", {}) or {})
        stored["last_sent_unix"] = time.time()
        _settings.patch({"recap": stored})
        return {**result, "items": recap["item_count"], "pdf_bytes": len(pdf)}

    @app.post("/api/local/recap/send")
    def local_recap_send(request: FastAPIRequest) -> dict[str, Any]:
        local_control(request)
        try:
            return _send_recap_now()
        except recap_service.RecapError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.get("/api/local/reader")
    def local_reader(request: FastAPIRequest, url: str = "") -> dict[str, Any]:
        """Fetch and extract an article node-side.

        Most publishers refuse to be framed, so the app cannot embed them; and
        doing the fetch here means the publisher sees the node, not the reader.
        """
        local_control(request)
        service = _digest_service()
        try:
            return read_article(url, fetcher=service.fetcher, cache=app.state.reader_cache)
        except ReaderError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.get("/api/local/consumption")
    def local_consumption(request: FastAPIRequest) -> list[dict[str, Any]]:
        local_control(request)
        return app.state.consumption_store.list()

    @app.post("/api/local/consumption")
    async def local_consumption_record(request: FastAPIRequest) -> dict[str, Any]:
        local_control(request)
        body = await request.json()
        try:
            record = app.state.consumption_store.record(
                body.get("item", {}),
                str(body.get("action", "")),
                progress=body.get("progress"),
            )
            action = str(body.get("action", ""))
            if action == "opened":
                _record_first_success("first_item_opened")
            elif action == "bookmark":
                _record_first_success("first_signal_recorded")
            if action in {"opened", "bookmark", "completed"}:
                _audit().append(
                    "fetch" if action == "opened" else "rec",
                    f"Content {action}",
                    details={"action": action, "stored_locally": True},
                    item_id=record["item_id"],
                )
            return record
        except ConsumptionError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.delete("/api/local/consumption")
    def local_consumption_clear(request: FastAPIRequest) -> dict[str, bool]:
        local_control(request)
        app.state.consumption_store.clear()
        _audit().append("verify", "Reading history cleared", details={"scope": "history"})
        return {"ok": True}

    def _library_enabled() -> None:
        if not app.state.library_enabled:
            raise HTTPException(status_code=404, detail="library_workspace_disabled")

    @app.get("/api/local/library")
    def local_library(
        request: FastAPIRequest,
        view: str = "all",
        query: str = "",
        kind: str = "",
        source: str = "",
        cursor: str = "",
        limit: int = 50,
    ) -> dict[str, Any]:
        local_control(request)
        _library_enabled()
        try:
            return _library_service().list(
                view=view, query=query, kind=kind, source=source, cursor=cursor, limit=limit
            )
        except LibraryError as exc:
            raise HTTPException(status_code=400, detail=exc.code) from exc

    @app.post("/api/local/library/imports")
    async def local_library_import(request: FastAPIRequest) -> dict[str, Any]:
        local_control(request)
        _library_enabled()
        try:
            content_length = int(request.headers.get("content-length", "0") or 0)
        except ValueError:
            content_length = 0
        if content_length > ((MAX_IMPORT_BYTES + 2) // 3) * 4 + 128 * 1024:
            raise HTTPException(status_code=413, detail="library_import_size_limit")
        try:
            imported = app.state.library_imports.import_bytes(dict(await request.json()))
            return _library_service().get(f"import:{imported['import_id']}")
        except LibraryImportError as exc:
            raise HTTPException(status_code=400, detail=exc.code) from exc

    @app.get("/api/local/library/imports/{import_id}/status")
    def local_library_import_status(import_id: str, request: FastAPIRequest) -> dict[str, Any]:
        local_control(request)
        _library_enabled()
        try:
            return app.state.library_imports.get(import_id)
        except LibraryImportError as exc:
            raise HTTPException(status_code=404, detail=exc.code) from exc

    @app.get("/api/local/library/imports/{import_id}/preview")
    def local_library_import_preview(import_id: str, request: FastAPIRequest) -> dict[str, Any]:
        local_control(request)
        _library_enabled()
        try:
            metadata = app.state.library_imports.get(import_id)
            text = app.state.library_imports.extracted_text(import_id)
            return {"import_id": import_id, "title": metadata["filename"], "text": text[:64 * 1024], "truncated": len(text) > 64 * 1024}
        except LibraryImportError as exc:
            raise HTTPException(status_code=404, detail=exc.code) from exc

    @app.delete("/api/local/library/imports/{import_id}")
    def local_library_import_delete(import_id: str, request: FastAPIRequest) -> dict[str, Any]:
        local_control(request)
        _library_enabled()
        try:
            if not app.state.library_imports.delete(import_id):
                raise LibraryImportError("library_import_not_found", "Import not found")
            return {"ok": True}
        except LibraryImportError as exc:
            raise HTTPException(status_code=404, detail=exc.code) from exc

    @app.post("/api/local/library/actions")
    async def local_library_actions(request: FastAPIRequest) -> dict[str, Any]:
        local_control(request)
        _library_enabled()
        try:
            return _library_service().actions(dict(await request.json()))
        except LibraryError as exc:
            raise HTTPException(status_code=400, detail=exc.code) from exc

    @app.get("/api/local/library/sources")
    def local_library_sources(request: FastAPIRequest) -> dict[str, Any]:
        local_control(request)
        _library_enabled()
        health = {
            str(item.get("id", "")): item
            for item in _digest_service().discovery_status().get("source_health", [])
        }
        sources = []
        for source_item in _digest_service().list_sources():
            source_id = str(source_item.get("id", ""))
            sources.append({**source_item, "paused": bool(source_item.get("paused")), "health": health.get(source_id, {})})
        return {"sources": sources, "watchers": _digest_service().list_watchers()}

    @app.patch("/api/local/library/sources/{source_id}")
    async def local_library_source_patch(source_id: str, request: FastAPIRequest) -> dict[str, Any]:
        local_control(request)
        _library_enabled()
        body = dict(await request.json())
        if set(body) != {"paused"}:
            raise HTTPException(status_code=400, detail="library_source_patch_invalid")
        try:
            return _digest_service().set_source_paused(source_id, bool(body["paused"]))
        except DigestError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @app.patch("/api/local/library/watchers/{watcher_id}")
    async def local_library_watcher_patch(watcher_id: str, request: FastAPIRequest) -> dict[str, Any]:
        local_control(request)
        _library_enabled()
        body = dict(await request.json())
        if set(body) != {"paused"}:
            raise HTTPException(status_code=400, detail="library_watcher_patch_invalid")
        try:
            return _digest_service().set_watcher_paused(watcher_id, bool(body["paused"]))
        except DigestError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @app.get("/api/local/library/{library_id:path}")
    def local_library_item(library_id: str, request: FastAPIRequest) -> dict[str, Any]:
        local_control(request)
        _library_enabled()
        try:
            return _library_service().get(library_id)
        except LibraryError as exc:
            raise HTTPException(status_code=404, detail=exc.code) from exc

    @app.patch("/api/local/library/{library_id:path}")
    async def local_library_item_patch(library_id: str, request: FastAPIRequest) -> dict[str, Any]:
        local_control(request)
        _library_enabled()
        try:
            return _library_service().patch(library_id, dict(await request.json()))
        except (LibraryError, LibraryMetadataError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.get("/api/local/digest/steer")
    def local_digest_steering(request: FastAPIRequest) -> dict[str, Any]:
        local_control(request)
        return _digest_service().get_steering()

    @app.post("/api/local/digest/steer")
    async def local_digest_steer(request: FastAPIRequest) -> dict[str, Any]:
        local_control(request)
        body = await request.json()
        result = _digest_service().steer(str(body.get("text", "")))
        _digest_service().build(now_unix=time.time())
        _audit().append(
            "rec",
            "Recommendation direction updated",
            details={"interests": result["interests"], "avoids": result["avoids"]},
        )
        return result

    @app.post("/api/local/digest/feedback")
    async def local_digest_feedback(request: FastAPIRequest) -> dict[str, Any]:
        local_control(request)
        body = await request.json()
        try:
            result = _digest_service().feedback(
                str(body.get("item_id", "")), str(body.get("action", ""))
            )
            _digest_service().build(now_unix=time.time())
            _audit().append(
                "rec",
                f"Recommendation feedback recorded: {body.get('action', '')}",
                details={"action": str(body.get("action", ""))},
                item_id=str(body.get("item_id", "")),
            )
            return result
        except DigestError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.post("/api/local/recommendations")
    async def local_recommendations(request: FastAPIRequest) -> list[dict[str, Any]]:
        local_control(request)
        try:
            body = await request.json()
        except json.JSONDecodeError:
            body = {}
        now_unix = time.time()
        items = network_content(control_network_id())
        items.extend(_digest_service().recommendation_items())
        has_starters = False
        if not any(
            str(item.get("fetch_status", "")) not in {"local", "fetched_full"}
            and str(item.get("safety_outcome", "")) != "blocked"
            for item in items
        ):
            items.extend(
                starter_items(
                    _recommendation_profile.get(),
                    seed_key=active_store.peer_id,
                    now_unix=now_unix,
                )
            )
            has_starters = True
        profile_signals = _recommendation_profile.signals()
        recommendations = recommendation_service.recommend_from_items(
            items,
            now_unix=now_unix,
            query=str(body.get("query", "") or ""),
            limit=int(body.get("limit", 6) or 6),
            profile=profile_signals,
        )
        if recommendations or body.get("query") or has_starters:
            return recommendations
        items.extend(
            starter_items(
                _recommendation_profile.get(),
                seed_key=active_store.peer_id,
                now_unix=now_unix,
            )
        )
        return recommendation_service.recommend_from_items(
            items,
            now_unix=now_unix,
            limit=int(body.get("limit", 6) or 6),
            profile=profile_signals,
        )

    @app.get("/api/local/recommendations/profile")
    def local_recommendations_profile(request: FastAPIRequest) -> dict[str, Any]:
        local_control(request)
        return _recommendation_profile.public()

    @app.patch("/api/local/recommendations/profile")
    async def local_recommendations_profile_update(request: FastAPIRequest) -> dict[str, Any]:
        local_control(request)
        body = await request.json()
        if not isinstance(body, dict):
            raise HTTPException(status_code=400, detail="recommendation_profile_not_object")
        profile = _recommendation_profile.patch(body)
        if "direction" in body:
            _digest_service().steer(profile["direction"])
        _digest_service().build(now_unix=time.time())
        _audit().append(
            "rec",
            "Recommendation profile updated",
            details={
                "topics": profile["topics"],
                "platforms": profile["platforms"],
                "has_direction": bool(profile["direction"]),
            },
        )
        return profile

    @app.post("/api/local/recommendations/feedback")
    async def local_recommendations_feedback(request: FastAPIRequest) -> dict[str, Any]:
        local_control(request)
        body = await request.json()
        content_id = str(body.get("contentId", "") or "")
        candidates = network_content(control_network_id())
        candidates.extend(_digest_service().recommendation_items())
        candidates.extend(
            starter_items(
                _recommendation_profile.get(),
                seed_key=active_store.peer_id,
                now_unix=time.time(),
            )
        )
        item = next(
            (candidate for candidate in candidates if candidate.get("content_id") == content_id),
            None,
        )
        if item is None:
            raise HTTPException(status_code=404, detail="recommendation_content_not_found")
        try:
            profile = _recommendation_profile.feedback(item, str(body.get("action", "")))
            if str(body.get("action", "")) != "neutral":
                _record_first_success("first_signal_recorded")
            _digest_service().build(now_unix=time.time())
            _audit().append(
                "rec",
                f"Recommendation feedback recorded: {body.get('action', '')}",
                details={"action": str(body.get("action", ""))},
                item_id=content_id,
            )
            return profile
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.post("/api/local/search-ask")
    async def local_search_ask(request: FastAPIRequest) -> dict[str, Any]:
        local_control(request)
        body = await request.json()
        query = str(body.get("text", "")).strip()
        digest = _digest_service().last_digest() or {}
        provider = _model_provider()
        result = await _asyncio.to_thread(
            ask_service.answer,
            query,
            provider=provider,
            digest_items=digest.get("items", []),
            content_items=network_content(control_network_id()),
        )
        _audit().append(
            "rec",
            "Search & Ask completed" if provider is not None else "Search & Ask had no model",
            details={
                "provider": getattr(provider, "id", None),
                "model": getattr(provider, "model", None),
                "network_access": getattr(provider, "id", None) == "anthropic",
                "query_length": len(query),
            },
        )
        return result

    @app.post("/api/local/publish/prepare")
    async def local_publish_prepare(request: FastAPIRequest) -> dict[str, Any]:
        local_control(request)
        body = await request.json()
        path = Path(str(body.get("path", ""))).expanduser()
        if not path.exists() or not path.is_file():
            raise HTTPException(status_code=404, detail="publish_file_not_found")
        draft_id = uuid.uuid4().hex
        app.state.publish_drafts[draft_id] = dict(body)
        preview = path.read_bytes()[: 256 * 1024]
        return {
            "draft_id": draft_id,
            "content_hash": file_hash(path),
            "preview_hash": "sha256:" + hashlib.sha256(preview).hexdigest(),
            "manifest_hash": "",
            "safety": {"outcome": "pending", "scanner": "rynmesh-keyword-safety"},
            "provenance_head": "",
        }

    @app.post("/api/local/publish/{draft_id}/confirm")
    def local_publish_confirm(draft_id: str, request: FastAPIRequest) -> dict[str, Any]:
        local_control(request)
        draft = app.state.publish_drafts.pop(draft_id, None)
        if draft is None:
            raise HTTPException(status_code=404, detail="publish_draft_not_found")
        result = active_store.publish_content(
            Path(str(draft.get("path", ""))).expanduser(),
            title=str(draft.get("title", "")),
            description=str(draft.get("description", "")),
            content_kind=str(draft.get("kind", "")),
            tags=tuple(str(tag) for tag in draft.get("tags", []) if str(tag)),
            model_id=str(draft.get("model_used", "rynmesh-webapp")),
        )
        if result.get("status") != "published":
            raise HTTPException(status_code=422, detail=result)
        return {"ok": True, "content_id": result["content_id"]}

    @app.get("/api/local/credits/scoreboard")
    def local_credit_scoreboard(request: FastAPIRequest) -> dict[str, Any]:
        local_control(request)
        peers = [
            {
                "id": item["id"],
                "name": item["name"],
                "credits": item["credits"],
                "weight": item["weight"],
            }
            for item in discover_peer_items(control_network_id(), include_self=True)
        ]
        return {"peers": peers}

    @app.get("/api/local/settings")
    def local_settings(request: FastAPIRequest) -> dict[str, Any]:
        local_control(request)
        info = active_store.node_info()
        registry = info["registry"]
        stored = _settings.get()
        provider = _model_provider()
        provider_id = str(getattr(provider, "id", "") or "")
        if provider is None:
            ai_provider = stored["ai_provider"]
        elif provider_id in {"ollama", "local"}:
            ai_provider = "local"
        else:
            ai_provider = "cloud"
        return {
            "node_name": active_store.node_name,
            "node_storage": str(active_store.home),
            "peer_http_host": os.environ.get("RYNMESH_PEER_HOST", "127.0.0.1"),
            "peer_http_port": int(os.environ.get("RYNMESH_PEER_PORT", "8791") or 8791),
            "public_endpoint": str(info.get("peer_endpoint", "")),
            "registry_url": str(registry.get("url") or registry.get("path") or ""),
            "trusted_roots": list(active_store.trusted_root_peer_ids),
            "safety_policy": stored["safety_policy"],
            "ai_provider": ai_provider,
            "ai_model": getattr(provider, "model", None) or stored["ai_model"] or "automatic",
            "cloud_access": bool(stored["cloud_access"]),
            "rank_default": stored["rank_default"],
            "publish_visibility": stored["publish_visibility"],
            "fetch_budget_mb": int(stored["fetch_budget_mb"]),
            "fetch_used_mb": 0,
            "fetch_timeout_s": int(stored["fetch_timeout_s"]),
            "onboarding_version": int(stored["onboarding_version"]),
            "notifications_enabled": bool(stored["notifications_enabled"]),
            "notification_frequency": stored["notification_frequency"],
            "notification_quiet_start": int(stored["notification_quiet_start"]),
            "notification_quiet_end": int(stored["notification_quiet_end"]),
            "auto_update": stored["auto_update"],
            "desktop_managed": os.environ.get("RYNMESH_DESKTOP_MODE", "").strip().lower()
            in {"1", "true", "yes"},
            "network_id": control_network_id(),
        }

    @app.patch("/api/local/settings")
    async def local_settings_update(request: FastAPIRequest) -> dict[str, Any]:
        local_control(request)
        body = await request.json()
        updated = _settings.patch(body if isinstance(body, dict) else {})
        if str(updated.get("node_name", "")).strip():
            active_store.node_name = str(updated["node_name"]).strip()
        _apply_safety_policy(updated)
        _reset_model_provider()
        return local_settings(request)

    @app.get("/api/local/updates/status")
    def local_updates_status(request: FastAPIRequest) -> dict[str, Any]:
        local_control(request)
        return updater.status()

    @app.post("/api/local/updates/check")
    async def local_updates_check(request: FastAPIRequest) -> dict[str, Any]:
        local_control(request)
        await _asyncio.to_thread(updater.check)
        return updater.status()

    @app.post("/api/local/updates/apply")
    async def local_updates_apply(request: FastAPIRequest) -> dict[str, Any]:
        local_control(request)
        await _asyncio.to_thread(updater.check)
        manifest = updater.check_manifest()
        return await _asyncio.to_thread(updater.apply, manifest)

    @app.get("/api/local/activity")
    def local_activity(request: FastAPIRequest) -> list[dict[str, Any]]:
        local_control(request)
        events = _audit().list(limit=200)
        for event in events:
            stamp = float(event.get("timestamp_unix", 0.0) or 0.0)
            event["t"] = _dt.fromtimestamp(stamp, _UTC).isoformat() if stamp else ""
        return events

    def _assistant_metadata(pattern: str, excluded: set[str]) -> list[dict[str, Any]]:
        records: list[dict[str, Any]] = []
        for path in (active_store.home / "assistant").glob(pattern):
            if path.name.endswith(".result.json"):
                continue
            try:
                record = dict(json.loads(path.read_text(encoding="utf-8")))
            except (OSError, TypeError, ValueError):
                continue
            records.append({key: value for key, value in record.items() if key not in excluded})
        return records

    @app.get("/api/local/privacy/status")
    def local_privacy_status(request: FastAPIRequest) -> dict[str, Any]:
        local_control(request)
        profile = _recommendation_profile.public()
        return {
            "storage_root": str(active_store.home),
            "reading_history_items": len(app.state.consumption_store.list()),
            "feedback_items": profile["feedback_count"],
            "learned_signals": profile["learned_signals"],
            "cached_discovery_items": _digest_service().cached_item_count(),
            "reader_cache_files": sum(
                1 for path in app.state.reader_cache.dir.glob("*.json") if path.is_file()
            ),
            "audit_events": len(_audit().list()),
            "assistant_run_records": sum(
                1 for path in (active_store.home / "assistant" / "runs").glob("run_*.json")
                if not path.name.endswith(".result.json")
            ),
            "assistant_context_records": sum(
                1 for path in (active_store.home / "assistant" / "contexts").glob("ctx_*.json")
            ),
            "library_import_records": len(app.state.library_imports.list()),
            "library_metadata_records": len(app.state.library_metadata.all()),
            "friend_relationships": len(_friends.list_friends()),
            "cloud_ai_enabled": bool(_settings.get().get("cloud_access", False)),
        }

    @app.get("/api/local/privacy/export")
    def local_privacy_export(request: FastAPIRequest) -> dict[str, Any]:
        local_control(request)
        stored = _settings.get()
        return {
            "version": 1,
            "exported_at": _iso_now(),
            "storage": "local",
            "recommendation_profile": _recommendation_profile.get(),
            "digest_preferences": _digest_service().personalization_data(),
            "reading_history": app.state.consumption_store.list(),
            "sources": _digest_service().list_sources(),
            "assistant_audit": _audit().list(),
            "assistant_workspace": {
                "runs": _assistant_metadata("runs/run_*.json", {"idempotency_key", "context_refs"}),
                "contexts": _assistant_metadata("contexts/ctx_*.json", {"conversation_id"}),
                "note": "Prompt, context bodies, model output, credentials, and absolute paths are excluded.",
            },
            "library": {
                "metadata": app.state.library_metadata.all(),
                "imports": app.state.library_imports.list(),
                "note": "Managed file bytes and extracted text are excluded from the default export.",
            },
            "friends": {
                "relationships": _friends.list_friends(),
                "content_cards": _friends.content_cards(),
                "note": "Relationship secrets, invitations, message bodies, and attachments are excluded.",
            },
            "first_success": app.state.first_success_store.get(),
            "privacy_settings": {
                "ai_provider": stored["ai_provider"],
                "ai_model": stored["ai_model"],
                "cloud_access": stored["cloud_access"],
                "notifications_enabled": stored["notifications_enabled"],
            },
        }

    @app.post("/api/local/privacy/erase")
    async def local_privacy_erase(request: FastAPIRequest) -> dict[str, Any]:
        local_control(request)
        body = await request.json()
        requested = body.get("scopes", []) if isinstance(body, dict) else []
        scopes = {str(scope) for scope in requested if str(scope)}
        allowed = {
            "history", "profile", "cache", "audit", "onboarding", "assistant",
            "library_metadata", "library_imports", "library_index", "friends",
        }
        if not scopes or not scopes.issubset(allowed):
            raise HTTPException(status_code=400, detail="privacy_erase_scopes_invalid")
        if "history" in scopes:
            app.state.consumption_store.clear()
        if "profile" in scopes:
            _recommendation_profile.clear()
            _digest_service().clear_personalization()
        if "cache" in scopes:
            _digest_service().clear_cached_content()
            app.state.reader_cache.clear()
        if "audit" in scopes:
            _audit().clear()
        if "onboarding" in scopes:
            app.state.first_success_store.reset()
        if "assistant" in scopes:
            assistant_root = (active_store.home / "assistant").resolve()
            if assistant_root.parent != active_store.home.resolve():
                raise HTTPException(status_code=409, detail="assistant_storage_root_invalid")
            for directory in (assistant_root / "contexts", assistant_root / "runs"):
                if directory.resolve().parent != assistant_root:
                    raise HTTPException(status_code=409, detail="assistant_storage_root_invalid")
                for path in directory.glob("*"):
                    if path.is_file() and path.resolve().parent == directory.resolve():
                        path.unlink(missing_ok=True)
        if "library_metadata" in scopes:
            app.state.library_metadata.clear()
        if "library_imports" in scopes:
            app.state.library_imports.clear()
        if "library_index" in scopes:
            index_path = active_store.home / "library" / "index.sqlite3"
            if index_path.resolve().parent == (active_store.home / "library").resolve():
                index_path.unlink(missing_ok=True)
        if "friends" in scopes:
            import shutil as _friend_shutil

            for relationship in _friends.list_friends():
                if relationship.get("status") == "active":
                    _friends.revoke(str(relationship["relationship_id"]), notify=False)
            for directory in (active_store.home / "friends", active_store.home / "messages"):
                resolved = directory.resolve()
                if resolved.parent != active_store.home.resolve():
                    raise HTTPException(status_code=409, detail="friend_storage_root_invalid")
                if resolved.exists():
                    _friend_shutil.rmtree(resolved)
        if "audit" not in scopes:
            _audit().append(
                "verify",
                "Personal assistant data erased",
                details={"scopes": sorted(scopes)},
            )
        return {"ok": True, "erased": sorted(scopes)}

    @app.get("/api/v1/node")
    def node_info() -> dict[str, Any]:
        return active_store.node_info()

    @app.get("/api/v1/peers")
    def peer_exchange(network_id: str = "rynmesh-main") -> dict[str, Any]:
        """Return this node's verified peer list as signed records.

        This is the gossip / peer-exchange endpoint: once a client has *any*
        reachable peer, it can call this to discover more peers without the
        central registry.  All records are the same Ed25519-signed objects the
        registry returns; clients MUST verify them before trusting.
        """
        try:
            result = active_store.discover_peers(network_id=network_id, include_self=True)
        except Exception:  # noqa: BLE001
            result = {"peers": [], "network_id": network_id, "source": "error"}
        # Re-serialize as raw SignedPayload dicts for wire compatibility.
        raw_peers: list[dict[str, Any]] = []
        for peer_item in result.get("peers", []):
            manifest = peer_item.get("manifest")
            if isinstance(manifest, dict):
                raw_peers.append(manifest)
        return {"peers": raw_peers, "network_id": network_id}

    @app.get("/api/v1/clips")
    def clips() -> dict[str, Any]:
        return active_store.list_local_clips()

    @app.get("/api/v1/content")
    def content() -> dict[str, Any]:
        return active_store.list_local_content()

    @app.get("/api/v1/credits")
    def credits(peer_id: str = "", category: str = "global") -> dict[str, Any]:
        return active_store.credit_summary(peer_id=peer_id or None, category=category)

    @app.get("/api/v1/credits/scoreboard")
    def credit_scoreboard(category: str = "global") -> dict[str, Any]:
        return active_store.credit_scoreboard(category=category)

    @app.post("/api/v1/credits/append")
    async def credit_append(request: FastAPIRequest) -> dict[str, Any]:
        """Accept a consumer-attested serve receipt (closes F1).

        Only the attestation kinds (preview_served, full_served) are
        accepted; the signed event is verified by the existing policy;
        self-attestation (issuer == subject) is rejected so providers
        cannot inflate themselves. Dedup handled by the ledger.
        """
        body = await request.json()
        try:
            signed = SignedPayload.from_dict(body)
            peeked = CreditEvent.from_payload(signed.payload)
        except Exception as exc:
            raise HTTPException(status_code=400, detail=f"invalid_payload: {exc}") from exc
        if peeked.kind not in ("preview_served", "full_served"):
            raise HTTPException(status_code=400, detail="kind_not_acceptable")
        if peeked.issuer_peer_id == peeked.subject_peer_id:
            raise HTTPException(status_code=400, detail="self_attestation_rejected")
        try:
            result = active_store.credit_ledger.append(signed)
        except CreditLedgerError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return {
            "ok": True,
            "status": result.get("status", ""),
            "event_id": result.get("event_id", ""),
        }

    @app.get("/api/v1/clips/{clip_id}/manifest")
    def manifest(clip_id: str) -> dict[str, Any]:
        return manifest_response(clip_id)

    @app.get("/api/v1/content/{content_id}/manifest")
    def content_manifest(content_id: str) -> dict[str, Any]:
        return manifest_response(content_id)

    @app.get("/api/v1/clips/{clip_id}/preview")
    def preview(clip_id: str) -> Response:
        return preview_response(clip_id)

    @app.get("/api/v1/content/{content_id}/preview")
    def content_preview(content_id: str) -> Response:
        return preview_response(content_id)

    @app.get("/api/v1/clips/{clip_id}/media")
    def media(clip_id: str):
        return bytes_response(clip_id)

    @app.get("/api/v1/content/{content_id}/bytes")
    def content_bytes(content_id: str):
        return bytes_response(content_id)

    # --- peer messaging (1:1 chat) ---
    from .services import peer_box as _peer_box
    from .services.messaging_store import MessagingStore as _MsgStore
    from .services.peer_messenger import PeerMessenger as _PeerMessenger

    _msg_priv = _peer_box.load_or_create_messaging_key(_home / "messaging.x25519")
    _msg_store = _MsgStore(_home)
    _pubkey_cache: dict[str, str] = {}  # peer_id -> x25519 pub (TOFU)
    _msg_subscribers: list = []  # asyncio.Queue per SSE client

    from .friends import FriendError as _FriendError
    from .friends import FriendService as _FriendService

    def _friend_post(
        endpoint: str,
        path: str,
        payload: dict[str, Any],
        headers: dict[str, str] | None,
        *,
        max_response_bytes: int = MAX_JSON_BYTES,
    ) -> dict[str, Any]:
        return HttpPeerClient(endpoint, timeout_s=15).post_json(
            path,
            payload,
            headers=headers,
            max_response_bytes=max_response_bytes,
        )

    def _friend_resolve_content(library_id: str) -> dict[str, Any] | None:
        if not library_id.startswith("import:"):
            return None
        import_id = library_id.split(":", 1)[1]
        metadata = app.state.library_imports.get(import_id)
        data = app.state.library_imports.read_bytes(import_id)
        return {
            "data": data,
            "content_id": "",
            "publisher_peer_id": active_store.peer_id,
            "manifest_ref": f"sha256:{metadata['sha256']}",
            "filename": metadata["filename"],
            "mime": metadata["mime"],
        }

    def _friend_import_content(resource: dict[str, Any]) -> dict[str, Any]:
        import base64 as _friend_base64

        imported = app.state.library_imports.import_bytes(
            {
                "filename": str(resource.get("filename", "shared-content")),
                "mime": str(resource.get("mime", "application/octet-stream")),
                "data_base64": _friend_base64.b64encode(bytes(resource.get("data", b""))).decode(
                    "ascii"
                ),
            }
        )
        return _library_service().get(f"import:{imported['import_id']}")

    _friend_endpoint = (
        os.environ.get("RYNMESH_PEER_ENDPOINT", "").strip()
        or str(active_store.node_info().get("peer_endpoint", ""))
    )
    _friends = _FriendService(
        home=active_store.home,
        peer_id=active_store.peer_id,
        node_name=active_store.node_name,
        endpoint=_friend_endpoint,
        identity_private=active_store.private_key_bytes,
        messaging_private=_msg_priv,
        post_json=_friend_post,
        resolve_content=_friend_resolve_content,
        import_content=_friend_import_content,
        allow_loopback=os.environ.get("RYNMESH_FRIEND_ALLOW_LOOPBACK", "").lower()
        in {"1", "true", "yes"},
    )
    app.state.friend_service = _friends
    _friend_accept_enabled = os.environ.get("RYNMESH_FRIEND_PAIRING_V1", "1") != "0"
    _invite_attempts: dict[str, list[float]] = {}

    def _resolve_endpoint(peer_id: str) -> str:
        discovered = (
            active_store.discover_peers(network_id=control_network_id(), include_self=False) or {}
        )
        for rec in discovered.get("peers", []):
            if rec.get("peer_id") == peer_id:
                ep = (
                    first_http_endpoint(rec.get("endpoints", []))
                    or (rec.get("metadata") or {}).get("peer_endpoint")
                    or ""
                )
                return str(ep).rstrip("/")
        return ""

    def _resolve_pubkey(peer_id: str) -> str:
        if peer_id in _pubkey_cache:
            return _pubkey_cache[peer_id]
        ep = _resolve_endpoint(peer_id)
        if not ep:
            raise RuntimeError(f"no endpoint for peer {peer_id}")
        from .transport import network_key_header

        req = _urlreq.Request(ep + "/api/peer/pubkey", headers=network_key_header())
        with _urlreq.urlopen(req, timeout=10) as resp:
            pub = json.load(resp)["x25519_pub"]
        _pubkey_cache[peer_id] = pub
        return pub

    def _transport(peer_id: str, header: dict) -> int:
        ep = _resolve_endpoint(peer_id)
        if not ep:
            return 0
        from .transport import network_key_header

        req = _urlreq.Request(
            ep + "/api/peer/msg",
            data=json.dumps(header).encode(),
            headers={"Content-Type": "application/json", **network_key_header()},
            method="POST",
        )
        with _urlreq.urlopen(req, timeout=15) as resp:
            return resp.status

    _messenger = _PeerMessenger(
        my_peer_id=active_store.peer_id,
        my_priv=_msg_priv,
        store=_msg_store,
        resolve_pubkey=_resolve_pubkey,
        transport=_transport,
        now=lambda: _dt.now(_UTC).isoformat(timespec="seconds"),
    )

    def _publish(record: dict) -> None:
        for q in list(_msg_subscribers):
            try:
                q.put_nowait(record)
            except Exception:
                pass

    def _friend_not_found() -> HTTPException:
        return HTTPException(status_code=404, detail="Not Found")

    async def _friend_wire(
        request: FastAPIRequest, path: str
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        try:
            length = int(request.headers.get("content-length", "0") or 0)
            if length > 8 * 1024 * 1024:
                raise ValueError("large")
            raw = await request.body()
            if len(raw) > 8 * 1024 * 1024:
                raise ValueError("large")
            relationship = _friends.verify_request(path=path, body=raw, headers=request.headers)
            value = json.loads(raw.decode("utf-8"))
            if not isinstance(value, dict):
                raise ValueError("body")
            return value, relationship
        except Exception as exc:
            raise _friend_not_found() from exc

    @app.post("/api/peer/friends/accept")
    async def peer_friend_accept(request: FastAPIRequest) -> dict[str, Any]:
        if not _friend_accept_enabled:
            raise _friend_not_found()
        client_host = (request.client.host if request.client else "") or "unknown"
        now = time.monotonic()
        recent = [stamp for stamp in _invite_attempts.get(client_host, []) if now - stamp < 60]
        if len(recent) >= 20:
            raise _friend_not_found()
        recent.append(now)
        _invite_attempts[client_host] = recent
        try:
            raw = await request.body()
            if len(raw) > 128 * 1024:
                raise ValueError("large")
            body = json.loads(raw.decode("utf-8"))
            if not isinstance(body, dict):
                raise ValueError("body")
            return _friends.accept(body)
        except Exception as exc:
            raise _friend_not_found() from exc

    @app.post("/api/peer/friends/message")
    async def peer_friend_message(request: FastAPIRequest) -> dict[str, Any]:
        body, _ = await _friend_wire(request, "/api/peer/friends/message")
        try:
            record = _friends.receive_message(body)
            _publish(record)
            return {"ok": True, "msg_id": record["msg_id"]}
        except Exception as exc:
            raise _friend_not_found() from exc

    @app.post("/api/peer/friends/content-card")
    async def peer_friend_content_card(request: FastAPIRequest) -> dict[str, Any]:
        body, _ = await _friend_wire(request, "/api/peer/friends/content-card")
        try:
            _friends.receive_content_card(body)
            return {"ok": True, "card_id": body.get("card_id")}
        except Exception as exc:
            raise _friend_not_found() from exc

    @app.post("/api/peer/friends/content-card/fetch")
    async def peer_friend_content_card_fetch(request: FastAPIRequest) -> dict[str, Any]:
        body, relationship = await _friend_wire(
            request, "/api/peer/friends/content-card/fetch"
        )
        try:
            return _friends.serve_content_card(body, relationship)
        except Exception as exc:
            raise _friend_not_found() from exc

    @app.post("/api/peer/friends/revoke")
    async def peer_friend_revoke(request: FastAPIRequest) -> dict[str, Any]:
        body, _ = await _friend_wire(request, "/api/peer/friends/revoke")
        _friends.receive_revoke(body)
        return {"ok": True}

    @app.post("/api/local/friends/invites")
    async def local_friend_invite(request: FastAPIRequest) -> dict[str, Any]:
        local_control(request)
        if not _friend_accept_enabled:
            raise HTTPException(status_code=503, detail="friend_pairing_disabled")
        body = await request.json()
        try:
            return _friends.create_invite(ttl_minutes=int(body.get("ttl_minutes", 15)))
        except Exception as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.post("/api/local/friends/invites/inspect")
    async def local_friend_inspect(request: FastAPIRequest) -> dict[str, Any]:
        local_control(request)
        try:
            return _friends.inspect_invite(str((await request.json()).get("invite_uri", "")))
        except Exception as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.get("/api/local/friends/invites")
    def local_friend_invites(request: FastAPIRequest) -> dict[str, Any]:
        local_control(request)
        return {"invites": _friends.list_invites()}

    @app.delete("/api/local/friends/invites/{invite_id}")
    def local_friend_cancel_invite(request: FastAPIRequest, invite_id: str) -> dict[str, Any]:
        local_control(request)
        try:
            return _friends.cancel_invite(invite_id)
        except _FriendError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @app.post("/api/local/friends/join")
    async def local_friend_join(request: FastAPIRequest) -> dict[str, Any]:
        local_control(request)
        if not _friend_accept_enabled:
            raise HTTPException(status_code=503, detail="friend_pairing_disabled")
        try:
            uri = str((await request.json()).get("invite_uri", ""))
            return await _asyncio.to_thread(_friends.join, uri)
        except Exception as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @app.get("/api/local/friends")
    def local_friends(request: FastAPIRequest) -> dict[str, Any]:
        local_control(request)
        return {"friends": _friends.list_friends()}

    @app.delete("/api/local/friends/{relationship_id}")
    async def local_friend_revoke(request: FastAPIRequest, relationship_id: str) -> dict[str, Any]:
        local_control(request)
        try:
            return await _asyncio.to_thread(_friends.revoke, relationship_id)
        except _FriendError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @app.post("/api/local/friends/{relationship_id}/retry")
    async def local_friend_retry_revocation(
        request: FastAPIRequest, relationship_id: str
    ) -> dict[str, Any]:
        local_control(request)
        try:
            return await _asyncio.to_thread(_friends.retry_revocation, relationship_id)
        except _FriendError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @app.post("/api/local/friends/messages/send")
    async def local_friend_send(request: FastAPIRequest) -> dict[str, Any]:
        local_control(request)
        body = await request.json()
        attachment = body.get("attachment")
        if attachment and attachment.get("bytes_b64"):
            import base64 as _friend_b64

            try:
                attachment = {
                    "filename": attachment.get("filename", "file"),
                    "mime": attachment.get("mime", "application/octet-stream"),
                    "bytes": _friend_b64.b64decode(attachment["bytes_b64"], validate=True),
                }
            except ValueError as exc:
                raise HTTPException(status_code=400, detail="attachment_invalid") from exc
        else:
            attachment = None
        try:
            return await _asyncio.to_thread(
                _friends.send_message,
                str(body.get("peer_id", "")),
                text=str(body.get("text", ""))[:20000],
                attachment=attachment,
            )
        except _FriendError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.get("/api/local/friends/messages")
    def local_friend_history(request: FastAPIRequest, peer_id: str) -> list[dict[str, Any]]:
        local_control(request)
        if not _friends.store.relationship_for_peer(peer_id):
            raise HTTPException(status_code=404, detail="active_friend_required")
        return _friends.history(peer_id)

    @app.post("/api/local/friends/messages/retry")
    async def local_friend_retry(request: FastAPIRequest) -> dict[str, int]:
        local_control(request)
        peer_id = str((await request.json()).get("peer_id", ""))
        return await _asyncio.to_thread(_friends.retry, peer_id)

    @app.post("/api/local/friends/content-cards")
    async def local_friend_send_card(request: FastAPIRequest) -> dict[str, Any]:
        local_control(request)
        body = await request.json()
        try:
            requested = dict(body.get("card") or {})
            item = _library_service().get(str(requested.get("library_id", "")))
            source = dict(item.get("source") or {})
            card = {
                "library_id": item["library_id"],
                "content_id": item.get("content_id") or "",
                "title": item["title"],
                "summary": item.get("summary", ""),
                "kind": item["kind"],
                "source": source.get("title", ""),
                "source_url": source.get("url", ""),
            }
            return await _asyncio.to_thread(
                _friends.send_content_card,
                str(body.get("peer_id", "")),
                card,
            )
        except (LibraryError, LibraryImportError, _FriendError) as exc:
            code = getattr(exc, "code", str(exc))
            raise HTTPException(status_code=400, detail=code) from exc

    @app.get("/api/local/friends/content-cards")
    def local_friend_cards(request: FastAPIRequest) -> dict[str, Any]:
        local_control(request)
        return {"cards": _friends.content_cards()}

    @app.post("/api/local/friends/content-cards/{card_id}/fetch")
    async def local_friend_fetch_card(card_id: str, request: FastAPIRequest) -> dict[str, Any]:
        local_control(request)
        try:
            return await _asyncio.to_thread(_friends.fetch_content_card, card_id)
        except (_FriendError, LibraryImportError) as exc:
            code = getattr(exc, "code", str(exc))
            raise HTTPException(status_code=409, detail=code) from exc

    @app.get("/api/peer/pubkey")
    def peer_pubkey() -> dict:
        return {"peer_id": active_store.peer_id, "x25519_pub": _peer_box.public_key_b64(_msg_priv)}

    @app.post("/api/peer/msg")
    async def peer_msg(request: FastAPIRequest) -> dict:
        header = await request.json()
        fp = header.get("from_pub")
        if fp and header.get("from"):
            _pubkey_cache.setdefault(str(header["from"]), str(fp))  # TOFU
        record = _messenger.receive(header)
        _publish(record)
        return {"ok": True, "msg_id": record["msg_id"]}

    @app.post("/api/local/messages/send")
    async def local_send(request: FastAPIRequest) -> dict:
        body = await request.json()
        peer_id = str(body.get("peer_id", ""))
        att = body.get("attachment")
        if att and att.get("bytes_b64"):
            import base64 as _b64

            att = {
                "filename": att.get("filename", "file"),
                "mime": att.get("mime", "application/octet-stream"),
                "bytes": _b64.b64decode(att["bytes_b64"]),
            }
        else:
            att = None
        return _messenger.send(peer_id, text=str(body.get("text", "")), attachment=att)

    @app.get("/api/local/messages/stream")
    async def local_stream():
        from fastapi.responses import StreamingResponse

        q: _asyncio.Queue = _asyncio.Queue()
        _msg_subscribers.append(q)

        async def gen():
            try:
                while True:
                    rec = await q.get()
                    yield f"data: {json.dumps(rec)}\n\n"
            finally:
                _msg_subscribers.remove(q)

        return StreamingResponse(gen(), media_type="text/event-stream")

    @app.get("/api/local/messages/attachment/{msg_id}")
    def local_attachment(msg_id: str, peer_id: str) -> Response:
        # peer_id is a query param: base64 peer ids contain '/', '+', '=' which
        # break path routing. msg_id is a uuid hex — safe in the path.
        mime = "application/octet-stream"
        for rec in _messenger.history(peer_id):
            if rec.get("msg_id") == msg_id:
                att = rec.get("attachment") or {}
                mime = str(att.get("mime") or mime)
                break
        try:
            data = _msg_store.load_attachment(msg_id)
        except FileNotFoundError as exc:
            raise HTTPException(status_code=404, detail="attachment not found") from exc
        return Response(content=data, media_type=mime)

    @app.get("/api/local/messages")
    def local_history(peer_id: str) -> list:
        # peer_id is a query param (not a path segment): base64 peer ids contain
        # '/' which Starlette can't route in a path.
        return _messenger.history(peer_id)

    # Private LLM tasks use discovery metadata from the existing registry, then
    # send signed end-to-end ciphertext directly between the two Ryn nodes.
    # The model runtime is never exposed as a peer endpoint.
    from .llm_package.routes import install_llm_routes as _install_llm_routes

    _install_llm_routes(
        app,
        store=active_store,
        home=active_store.home,
        messaging_key=_msg_priv,
        resolve_endpoint=_resolve_endpoint,
        resolve_pubkey=_resolve_pubkey,
    )

    # ---- bundled web UI -------------------------------------------------
    # A packaged install serves the built webapp from the node itself, so the
    # whole product is one process on one port — no dev server, no npm.
    # Mounted LAST so every /api route above wins.
    _mount_webui(app)

    return app


def webui_dir() -> "Path":
    """Directory of the bundled web UI, or a non-existent path in a source checkout."""
    override = os.environ.get("RYNMESH_WEBUI_DIR", "").strip()
    if override:
        return Path(override).expanduser()
    return Path(__file__).resolve().parent / "webui"


def _mount_webui(app: Any) -> bool:
    """Mount the built webapp at / with SPA fallback. Returns False if absent."""
    directory = webui_dir()
    if not (directory / "index.html").is_file():
        return False
    from fastapi.responses import FileResponse
    from fastapi.staticfiles import StaticFiles
    from starlette.exceptions import HTTPException as StarletteHTTPException

    def _no_store(response: Any) -> Any:
        # index.html names hash-versioned assets, so it must never be cached:
        # a stale shell keeps pointing at the previous build's bundle and the
        # app silently stays on the old version after an update. The hashed
        # assets themselves are immutable and cache freely.
        response.headers["Cache-Control"] = "no-cache, must-revalidate"
        return response

    class SPAStaticFiles(StaticFiles):
        async def get_response(self, path: str, scope: Any) -> Any:
            try:
                response = await super().get_response(path, scope)
            except StarletteHTTPException as exc:
                # Client-side routes (/digest, /peers, …) have no file on disk;
                # hand them index.html and let the router resolve them. Missing
                # assets must still 404 — otherwise a broken <script> src
                # silently returns HTML and the app fails with a parse error.
                request_path = str(scope.get("path") or path).lstrip("/")
                if exc.status_code == 404 and not request_path.startswith("assets/"):
                    return _no_store(FileResponse(directory / "index.html"))
                raise
            if path in ("", ".", "index.html"):
                return _no_store(response)
            return response

    app.mount("/", SPAStaticFiles(directory=str(directory), html=True), name="webui")
    return True


def main() -> int:
    apply_desktop_defaults()
    try:
        import uvicorn
    except ImportError as exc:  # pragma: no cover
        raise ImportError("Rynmesh peer HTTP server requires `uvicorn`") from exc

    host = os.environ.get("RYNMESH_PEER_HOST", "127.0.0.1")
    port = int(os.environ.get("RYNMESH_PEER_PORT", "8791") or 8791)
    app = create_app()
    if (webui_dir() / "index.html").is_file():
        print(f"Ryn node ready — open http://127.0.0.1:{port}", flush=True)
    uvicorn.run(app, host=host, port=port, log_level="warning")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
