"""Owns the consumer-side net.egress (VPN) connection lifecycle for the local node.

Status is always derived from a live SOCKS-port probe plus a cached session
descriptor (``<store.home>/egress/<region>.json``) so it survives app/daemon
restarts — the tunnel is an independent ``ssh -f`` process.
"""
from __future__ import annotations

import json
import os
import signal
import subprocess
import time
from pathlib import Path
from typing import Any, Callable

from rynmesh.services import net_egress_client as nec

CLOUDFLARE_TRACE = "https://www.cloudflare.com/cdn-cgi/trace"
_LEGACY_PROFILE_DIR = os.path.expanduser("~/.avaryn-sz-chrome")
_NEW_PROFILE_DIR = os.path.expanduser("~/.rynmesh-sz-chrome")
# Keep using a pre-existing legacy Chrome profile (cookies/logins) if present.
DEFAULT_PROFILE_DIR = (
    _LEGACY_PROFILE_DIR
    if os.path.isdir(_LEGACY_PROFILE_DIR) and not os.path.isdir(_NEW_PROFILE_DIR)
    else _NEW_PROFILE_DIR
)

CN_SITES = [
    "https://tv.cctv.com/live/cctv1/",
    "https://tv.cctv.com/live/cctv5/",
    "https://www.miguvideo.com/",
    "https://www.bilibili.com/",
    "https://www.iqiyi.com/",
    "https://v.qq.com/",
    "https://www.youku.com/",
    "https://www.mgtv.com/",
]


def _connected_iso(val):
    if val is None:
        return None
    if isinstance(val, (int, float)):
        from datetime import UTC, datetime
        return datetime.fromtimestamp(val, UTC).isoformat()
    return str(val)


def _uptime_seconds(val):
    if val is None:
        return None
    try:
        from datetime import UTC, datetime
        if isinstance(val, (int, float)):
            start = float(val)
        else:
            start = datetime.fromisoformat(str(val).replace("Z", "+00:00")).timestamp()
        return max(0, int(datetime.now(UTC).timestamp() - start))
    except Exception:
        return None


def _port_listening(port: int) -> bool:
    if not port:
        return False
    try:
        subprocess.run(
            ["lsof", "-nP", f"-iTCP:{port}", "-sTCP:LISTEN"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True, timeout=3,
        )
        return True
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, FileNotFoundError):
        return False


def _socks_reachable(host: str, port: int) -> bool:
    """TCP-connect probe for a (possibly remote) SOCKS endpoint, e.g. the overlay
    exit at overlay_ip:socks_port. Used for nebula-socks where there is no local
    listener to lsof."""
    if not host or not port:
        return False
    import socket
    try:
        with socket.create_connection((host, int(port)), timeout=4):
            return True
    except OSError:
        return False


def _verify_loc(port: int, host: str = "127.0.0.1") -> dict[str, Any]:
    try:
        out = subprocess.run(
            ["curl", "--socks5-hostname", f"{host}:{port}", "-s", "--max-time", "12", CLOUDFLARE_TRACE],
            capture_output=True, text=True, timeout=20,
        ).stdout
    except (subprocess.SubprocessError, FileNotFoundError):
        return {"ip": None, "loc": ""}
    ip, loc = "", ""
    for line in out.splitlines():
        if line.startswith("ip="):
            ip = line[3:]
        elif line.startswith("loc="):
            loc = line[4:]
    return {"ip": ip or None, "loc": loc}


def _kill_tunnel(port: int, gateway: str) -> None:
    if not port:
        return
    pattern = rf"ssh.*-D (127\.0\.0\.1:)?{port} .*{gateway}"
    try:
        res = subprocess.run(["pgrep", "-f", pattern], capture_output=True, text=True, timeout=3)
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return
    for pid in res.stdout.split():
        try:
            os.kill(int(pid), signal.SIGTERM)
        except (ValueError, ProcessLookupError, PermissionError):
            pass


class EgressController:
    def __init__(
        self,
        store: Any,
        *,
        broker: Callable[[str, str], dict[str, Any]] | None = None,
        run_vpn: Callable[..., None] | None = None,
        port_listening: Callable[[int], bool] | None = None,
        verify_loc: Callable[[int], dict[str, Any]] | None = None,
        kill_tunnel: Callable[[int, str], None] | None = None,
        close_browser: Callable[[], None] | None = None,
        profile_dir: str | None = None,
        now: Callable[[], float] | None = None,
    ) -> None:
        self.store = store
        self._broker = broker or self._default_broker
        self._run_vpn = run_vpn or self._default_run_vpn
        self._port_listening = port_listening or _port_listening
        self._verify_loc = verify_loc or _verify_loc
        self._kill_tunnel = kill_tunnel or _kill_tunnel
        self._close_browser = close_browser or self._default_close_browser
        self._profile_dir = profile_dir or DEFAULT_PROFILE_DIR
        self._now = now or time.time

    def _default_close_browser(self) -> None:
        # Close ONLY the dedicated Shenzhen-profile Chrome (matched by its user-data-dir),
        # so a fresh launch starts with a clean window. The user's normal Chrome is untouched.
        try:
            subprocess.run(["pkill", "-f", self._profile_dir], check=False, timeout=5)
        except (FileNotFoundError, subprocess.TimeoutExpired):
            return
        time.sleep(1.2)  # let Chrome release the profile lock before relaunch

    # --- default wiring (overridable for tests) ---
    def _default_broker(self, region: str, provider_peer_id: str) -> dict[str, Any]:
        return nec.request_session(self.store, provider_peer_id=provider_peer_id, region=region)

    def _default_run_vpn(self, session: dict[str, Any], *, mode: str, url: str = "", urls: list[str] | None = None) -> None:
        nec.connect(session, mode=mode, url=url, urls=urls,
                    extra_env={"RYNMESH_VPN_PROFILE_DIR": self._profile_dir})

    # --- cache ---
    def _cache_path(self, region: str) -> Path:
        return Path(self.store.home) / "egress" / f"{region}.json"

    def _read_cache(self, region: str) -> dict[str, Any]:
        path = self._cache_path(region)
        if not path.exists():
            return {}
        try:
            return json.loads(path.read_text())
        except (json.JSONDecodeError, OSError):
            return {}

    def _write_cache(self, region: str, cache: dict[str, Any]) -> None:
        path = self._cache_path(region)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(cache, indent=2))

    def _clear_cache(self, region: str) -> None:
        try:
            self._cache_path(region).unlink()
        except FileNotFoundError:
            pass

    # --- status builders ---
    def _disconnected(self, region: str) -> dict[str, Any]:
        return {
            "region": region, "connected": False, "providerPeerId": "", "providerNodeName": "",
            "socksPort": None, "exitIp": None, "loc": "", "locVerified": False,
            "ttlExpiresAt": "", "priceCredits": 0.0, "lastError": None,
            "connectedAt": None, "uptimeSeconds": None,
        }

    def _error(self, region: str, message: str) -> dict[str, Any]:
        st = self._disconnected(region)
        st["lastError"] = message
        return st

    def _status_from_cache(self, region: str, cache: dict[str, Any]) -> dict[str, Any]:
        loc = cache.get("loc", "") or ""
        return {
            "region": region,
            "connected": True,
            "providerPeerId": cache.get("provider_peer_id", "") or "",
            "providerNodeName": cache.get("provider_node_name", "") or "",
            "socksPort": cache.get("socks_port"),
            "exitIp": cache.get("exit_ip"),
            "loc": loc,
            "locVerified": bool(loc) and loc == region,
            "ttlExpiresAt": cache.get("ttl_expires_at", "") or "",
            "priceCredits": float(cache.get("price_credits") or 0.0),
            "lastError": None,
            "connectedAt": cache.get("connected_at_iso") or _connected_iso(cache.get("connected_at")),
            "uptimeSeconds": _uptime_seconds(cache.get("connected_at")),
        }

    # --- public API ---
    def connect(self, region: str = "CN", provider_peer_id: str = "") -> dict[str, Any]:
        try:
            session = self._broker(region, provider_peer_id)
        except Exception as exc:  # broker/network/provider failure
            return self._error(region, f"broker_failed: {exc}")

        transport = str(session.get("transport", "ssh-socks5") or "ssh-socks5")
        try:
            env = nec.build_vpn_env(session)
            gateway = env.get("RYNMESH_VPN_GATEWAY", "")
            port = int(env.get("RYNMESH_VPN_PORT") or session.get("socks_port") or 0)
        except Exception as exc:  # malformed/partial session descriptor
            return self._error(region, f"invalid_session: {exc}")
        if not port:
            return self._error(region, "session missing socks_port")
        # nebula-socks: the SOCKS exit is REMOTE on the overlay IP — there is no local
        # tunnel/listener; reachability is a TCP probe to overlay_ip:socks_port.
        socks_host = str(session.get("overlay_ip", "")).strip() if transport == "nebula-socks" else "127.0.0.1"

        try:
            self._run_vpn(session, mode="up")
        except Exception as exc:
            return self._error(region, f"tunnel_up_failed: {exc}")

        if transport == "nebula-socks":
            reachable = _socks_reachable(socks_host, port)
        else:
            reachable = self._port_listening(port)
        if not reachable:
            if transport != "nebula-socks":
                self._kill_tunnel(port, gateway)  # don't leak the ssh -f we just spawned
            return self._error(
                region,
                "overlay SOCKS not reachable (is nebula up?)"
                if transport == "nebula-socks" else "tunnel not listening after bring-up",
            )

        trace = self._verify_loc(port, socks_host) if transport == "nebula-socks" else self._verify_loc(port)
        cache = {
            "region": region,
            "provider_peer_id": provider_peer_id or session.get("provider_peer_id", ""),
            "provider_node_name": session.get("provider_node_name") or session.get("exit_host") or session.get("overlay_ip", ""),
            "exit_host": session.get("exit_host", ""),
            "transport": transport,
            "socks_host": socks_host,
            "gateway": gateway,
            "socks_port": port,
            "exit_ip": trace.get("ip"),
            "loc": trace.get("loc", ""),
            "ttl_expires_at": session.get("expires_at", ""),
            "price_credits": float(session.get("price_credits") or 0.0),
            "connected_at": self._now(),
            "session": dict(session),  # raw descriptor for relaunch
        }
        try:
            self._write_cache(region, cache)
        except OSError as exc:
            return self._error(region, f"cache_write_failed: {exc}")
        return self._status_from_cache(region, cache)

    def launch(self, region: str = "CN", urls: list[str] | None = None) -> dict[str, Any]:
        sites = list(urls) if urls else list(CN_SITES)
        for u in sites:
            if not u.startswith(("http://", "https://")):
                return self._error(region, "invalid_url")
        st = self.status(region)
        if not st["connected"]:
            return self._error(region, "not_connected")
        session = self._read_cache(region).get("session") or {}
        self._close_browser()  # close any prior Shenzhen-profile tabs first
        try:
            self._run_vpn(session, mode="chrome", urls=sites)
        except Exception as exc:
            return self._error(region, f"launch_failed: {exc}")
        return {"launched": True, "count": len(sites)}

    def disconnect(self, region: str = "CN") -> dict[str, Any]:
        cache = self._read_cache(region)
        if cache:
            self._kill_tunnel(int(cache.get("socks_port") or 0), cache.get("gateway", ""))
            self._clear_cache(region)
        return self._disconnected(region)

    def status(self, region: str = "CN") -> dict[str, Any]:
        cache = self._read_cache(region)
        if not cache:
            return self._disconnected(region)
        port = int(cache.get("socks_port") or 0)
        transport = str(cache.get("transport", "ssh-socks5") or "ssh-socks5")
        if transport == "nebula-socks":
            ok = _socks_reachable(str(cache.get("socks_host", "")), port)
        else:
            ok = bool(port) and self._port_listening(port)
        if ok:
            return self._status_from_cache(region, cache)
        self._clear_cache(region)
        return self._disconnected(region)
