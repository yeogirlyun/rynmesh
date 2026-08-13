"""Consumer side of the `net.egress` VPN service (MVP).

Submits an `open_session` work order to a provider node, polls for the session
descriptor, and wires the local ``rynmesh-vpn`` primitive to tunnel browser
traffic out through the provider's region. See
``docs/RYNMESH_VPN_EGRESS_RUNBOOK.md``.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import time
from typing import Any

from rynmesh.services.net_egress import CAPABILITY, OPERATION
from rynmesh.store import RynmeshStore


class NetEgressClientError(RuntimeError):
    pass


def _resolve_provider(store: RynmeshStore, provider_peer_id: str, network_id: str) -> str:
    provider = str(provider_peer_id or "").strip()
    if provider:
        return provider
    # Prefer a peer that actually advertises the net.egress capability.
    try:
        caps = store.list_job_capacities(
            capability=CAPABILITY, network_id=network_id
        ).get("capacities", [])
    except Exception:
        caps = []
    for cap in caps:
        pid = str(cap.get("peer_id", "") or "").strip()
        if pid and pid != store.peer_id:
            return pid
    # Fallback: any non-self discovered peer. discover_peers returns a dict keyed by
    # peer_id (older callers assumed a list of records — handle both shapes).
    peers = store.discover_peers(network_id=network_id) or {}
    if isinstance(peers, dict):
        candidate_ids = list(peers.keys())
    else:
        candidate_ids = [
            p if isinstance(p, str) else str((p or {}).get("peer_id", "") or "")
            for p in peers
        ]
    for pid in candidate_ids:
        pid = str(pid).strip()
        if pid and pid != store.peer_id:
            return pid
    raise NetEgressClientError(
        "no net.egress provider found; ensure a provider node is registered"
    )


def request_session(
    store: RynmeshStore,
    *,
    provider_peer_id: str = "",
    region: str = "HK",
    ttl_s: int = 3600,
    network_id: str = "rynmesh-main",
    timeout_s: float = 30.0,
    poll_interval_s: float = 1.0,
) -> dict[str, Any]:
    """Submit an open_session order and return the parsed session descriptor."""
    provider = _resolve_provider(store, provider_peer_id, network_id)
    submitted = store.submit_work_order(
        provider_peer_id=provider,
        capability=CAPABILITY,
        operation=OPERATION,
        params={"region": region, "ttl_s": int(ttl_s), "client_peer_id": store.peer_id},
        network_id=network_id,
    )
    wo_id = str(submitted.get("order", {}).get("work_order_id", "")).strip()
    if not wo_id:
        raise NetEgressClientError(f"work order not accepted: {submitted}")

    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        results = store.list_work_results(
            work_order_id=wo_id, network_id=network_id, requester_peer_id=store.peer_id
        ).get("work_results", [])
        for result in results:
            status = str(result.get("status", ""))
            if status == "completed":
                return _parse_session(result.get("message", ""))
            if status == "failed":
                raise NetEgressClientError(f"egress provider failed: {result.get('message','')}")
        time.sleep(poll_interval_s)
    raise NetEgressClientError(
        f"timed out after {timeout_s:g}s waiting for egress session (work_order={wo_id})"
    )


def _parse_session(message: Any) -> dict[str, Any]:
    try:
        session = json.loads(str(message))
    except (TypeError, ValueError) as exc:
        raise NetEgressClientError(f"unparseable session message: {message!r}") from exc
    if session.get("kind") != "net.egress.session":
        raise NetEgressClientError(f"unexpected result payload: {session!r}")
    return session


def build_vpn_env(session: dict[str, Any]) -> dict[str, str]:
    """Pure mapping from a session descriptor to rynmesh-vpn env overrides."""
    transport = str(session.get("transport", "ssh-socks5") or "ssh-socks5")
    if transport == "nebula-socks":
        host = str(session.get("overlay_ip", "")).strip()
        if not host:
            raise NetEgressClientError("nebula-socks session missing overlay_ip")
        env = {"RYNMESH_VPN_TRANSPORT": "nebula-socks", "RYNMESH_VPN_SOCKS_HOST": host}
        if session.get("socks_port"):
            env["RYNMESH_VPN_PORT"] = str(session["socks_port"])
        return env
    if transport == "hysteria2":
        host = str(session.get("exit_host", "")).strip()
        if not host:
            raise NetEgressClientError("session missing exit_host")
        port = session.get("udp_port")
        if not port:
            raise NetEgressClientError("hysteria2 session missing udp_port")
        env = {
            "RYNMESH_VPN_TRANSPORT": "hysteria2",
            "RYNMESH_VPN_HYSTERIA_SERVER": f"{host}:{int(port)}",
            "RYNMESH_VPN_HYSTERIA_AUTH": str(session.get("auth", "")),
            "RYNMESH_VPN_HYSTERIA_SNI": str(session.get("sni", "")),
            "RYNMESH_VPN_HYSTERIA_UP_MBPS": str(session.get("up_mbps", "")),
            "RYNMESH_VPN_HYSTERIA_DOWN_MBPS": str(session.get("down_mbps", "")),
        }
        if session.get("obfs"):
            env["RYNMESH_VPN_HYSTERIA_OBFS"] = str(session["obfs"])
        if session.get("tls_insecure"):
            env["RYNMESH_VPN_HYSTERIA_INSECURE"] = "1"
        if session.get("tls_pin"):
            env["RYNMESH_VPN_HYSTERIA_PIN"] = str(session["tls_pin"])
        if session.get("socks_port"):
            env["RYNMESH_VPN_PORT"] = str(session["socks_port"])
        return env
    host = str(session.get("exit_host", "")).strip()
    user = str(session.get("exit_user", "")).strip()
    if not host:
        raise NetEgressClientError("session missing exit_host")
    gateway = f"{user}@{host}" if user else host
    env = {"RYNMESH_VPN_GATEWAY": gateway}
    if session.get("socks_port"):
        env["RYNMESH_VPN_PORT"] = str(session["socks_port"])
    return env


def _vpn_cmd() -> str:
    cmd = shutil.which("rynmesh-vpn")
    if cmd:
        return cmd
    # Fall back to the script bundled with this package (always present in a
    # normal install; PATH lookup above lets operators override it).
    bundled = os.path.join(os.path.dirname(__file__), "rynmesh-vpn")
    if os.path.isfile(bundled):
        return bundled
    raise NetEgressClientError(
        "rynmesh-vpn not found — reinstall the rynmesh package so the bundled "
        "VPN data-plane script is available."
    )


def connect(
    session: dict[str, Any],
    *,
    mode: str = "chrome",
    url: str = "",
    urls: list[str] | None = None,
    dry_run: bool = False,
    extra_env: dict[str, str] | None = None,
) -> list[str]:
    """Wire rynmesh-vpn to the brokered session. Returns the argv used/planned."""
    env_overrides = build_vpn_env(session)
    if extra_env:
        env_overrides = {**env_overrides, **extra_env}
    argv = [_vpn_cmd() if not dry_run else "rynmesh-vpn", mode]
    if mode == "chrome":
        site_args = list(urls) if urls else ([url] if url else [])
        argv.extend(site_args)
    if dry_run:
        return argv
    env = {**os.environ, **env_overrides}
    subprocess.run(argv, env=env, check=False)
    return argv


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="rynmesh net.egress consumer (VPN via the mesh)")
    ap.add_argument("--provider", default="", help="provider peer_id (omit to auto-pick the other node)")
    ap.add_argument("--region", default="HK")
    ap.add_argument("--ttl-s", type=int, default=3600)
    ap.add_argument("--network-id", default=os.environ.get("RYNMESH_NETWORK_ID", "rynmesh-main"))
    ap.add_argument("--mode", choices=["chrome", "up", "status"], default="chrome")
    ap.add_argument("--url", default="", help="URL to open when --mode chrome")
    ap.add_argument("--timeout-s", type=float, default=30.0)
    ap.add_argument("--dry-run", action="store_true", help="resolve a session and print the plan; don't tunnel")
    args = ap.parse_args(argv)

    store = RynmeshStore()
    session = request_session(
        store,
        provider_peer_id=args.provider,
        region=args.region,
        ttl_s=args.ttl_s,
        network_id=args.network_id,
        timeout_s=args.timeout_s,
    )
    print(json.dumps({"session": session, "vpn_env": build_vpn_env(session)}, indent=2))
    plan = connect(session, mode=args.mode, url=args.url, dry_run=args.dry_run)
    print(("DRY-RUN " if args.dry_run else "") + "rynmesh-vpn " + " ".join(plan[1:]))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
