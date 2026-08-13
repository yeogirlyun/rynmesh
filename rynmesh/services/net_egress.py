"""`net.egress` — VPN egress as a Rynmesh node service (MVP).

A provider node (e.g. on the HK gateway) advertises the `net.egress` capability;
a consumer node submits an `open_session` work order and gets back a **session
descriptor** describing how to tunnel its (browser) traffic out through the
provider's region. The actual data-plane is the bundled `rynmesh-vpn` SSH-SOCKS5 primitive
(`rynmesh-vpn`) — this service only does the *brokering* (advertise → authorize →
return session) and a *simple* credit record. See
``docs/RYNMESH_VPN_EGRESS_RUNBOOK.md`` and
``docs/RYNMESH_NET_SERVICES_DESIGN.md``.

Per the design's layering contract: rynmesh orchestrates (who/where/credits);
`rynmesh-vpn` carries the bytes. This module never re-implements the tunnel.

Simplified for the MVP (deliberately):
- No ephemeral SSH cert issuance — the consumer uses its existing shared key
  (``~/.ssh/rynmesh_example_key``); the exit must already authorize it.
- Credits are recorded as a local JSONL ledger line, not yet the full signed
  ``credits.FileCreditLedger`` flow.
"""
from __future__ import annotations

import json
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from rynmesh.services._base import ServiceWorker

CAPABILITY = "net.egress"
OPERATION = "open_session"
DEFAULT_REGION = "HK"
DEFAULT_SOCKS_PORT = 1080
DEFAULT_TTL_S = 3600
DEFAULT_MAX_TTL_S = 6 * 3600
DEFAULT_PRICE_CREDITS = 1.0


class NetEgressConfigError(RuntimeError):
    """Provider is missing required egress configuration."""


def _provider_config() -> dict[str, Any]:
    exit_host = os.environ.get("RYNMESH_EGRESS_EXIT_HOST", "").strip()
    if not exit_host:
        raise NetEgressConfigError(
            "RYNMESH_EGRESS_EXIT_HOST is required (the SSH host whose IP becomes "
            "the exit, e.g. 203.0.113.10 for the HK gateway)."
        )
    return {
        "exit_host": exit_host,
        "exit_user": os.environ.get("RYNMESH_EGRESS_EXIT_USER", "rynmesh").strip(),
        "exit_port": int(os.environ.get("RYNMESH_EGRESS_EXIT_PORT", "22")),
        "region": os.environ.get("RYNMESH_EGRESS_REGION", DEFAULT_REGION).strip(),
        "socks_port": int(os.environ.get("RYNMESH_EGRESS_SOCKS_PORT", str(DEFAULT_SOCKS_PORT))),
        "max_ttl_s": int(os.environ.get("RYNMESH_EGRESS_MAX_TTL_S", str(DEFAULT_MAX_TTL_S))),
        "price_credits": float(
            os.environ.get("RYNMESH_EGRESS_PRICE_CREDITS", str(DEFAULT_PRICE_CREDITS))
        ),
        "transport": os.environ.get("RYNMESH_EGRESS_TRANSPORT", "ssh-socks5").strip(),
        "overlay_ip": os.environ.get("RYNMESH_EGRESS_OVERLAY_IP", "").strip(),
        "hysteria_host": os.environ.get("RYNMESH_EGRESS_HYSTERIA_HOST", "").strip(),
        "hysteria_udp_port": int(os.environ.get("RYNMESH_EGRESS_HYSTERIA_UDP_PORT", "443")),
        "hysteria_auth": os.environ.get("RYNMESH_EGRESS_HYSTERIA_AUTH", "").strip(),
        "hysteria_sni": os.environ.get("RYNMESH_EGRESS_HYSTERIA_SNI", "bing.com").strip(),
        "hysteria_obfs": os.environ.get("RYNMESH_EGRESS_HYSTERIA_OBFS", "").strip(),
        "hysteria_up_mbps": int(os.environ.get("RYNMESH_EGRESS_HYSTERIA_UP_MBPS", "20")),
        "hysteria_down_mbps": int(os.environ.get("RYNMESH_EGRESS_HYSTERIA_DOWN_MBPS", "100")),
        "hysteria_tls_insecure": os.environ.get("RYNMESH_EGRESS_HYSTERIA_TLS_INSECURE", "1").strip().lower() in ("1", "true", "yes"),
        "hysteria_tls_pin": os.environ.get("RYNMESH_EGRESS_HYSTERIA_TLS_PIN", "").strip(),
    }


def _ledger_path() -> Path:
    home = os.environ.get("RYNMESH_HOME", str(Path.home() / ".rynmesh"))
    return Path(home) / "egress_sessions.jsonl"


def record_egress_credit(entry: dict[str, Any]) -> None:
    """Append one egress session/credit line to the local JSONL ledger.

    MVP stand-in for ``credits.FileCreditLedger`` (signed events). Kept simple on
    purpose; the integration target is a signed ``service`` credit event that
    debits the consumer and credits the provider by ``price_credits``.
    """
    path = _ledger_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(entry, sort_keys=True) + "\n")


def build_session(params: dict[str, Any], config: dict[str, Any], *, now: datetime | None = None) -> dict[str, Any]:
    """Pure session-descriptor builder (no I/O) — the unit-testable core."""
    now = now or datetime.now(UTC)
    requested_region = str(params.get("region", "") or "").strip()
    if requested_region and requested_region.upper() != config["region"].upper():
        raise NetEgressConfigError(
            f"requested region {requested_region!r} not served here "
            f"(this exit is {config['region']!r})"
        )
    ttl_s = int(params.get("ttl_s", DEFAULT_TTL_S) or DEFAULT_TTL_S)
    ttl_s = max(60, min(ttl_s, config["max_ttl_s"]))
    expires_at = now + timedelta(seconds=ttl_s)
    base = {
        "kind": "net.egress.session",
        "region": config["region"],
        "ttl_s": ttl_s,
        "issued_at": now.isoformat(timespec="seconds"),
        "expires_at": expires_at.isoformat(timespec="seconds"),
        "price_credits": config["price_credits"],
    }
    transport = str(config.get("transport", "ssh-socks5") or "ssh-socks5")
    if transport == "nebula-socks":
        overlay = str(config.get("overlay_ip", "")).strip()
        if not overlay:
            raise NetEgressConfigError(
                "nebula-socks transport requires RYNMESH_EGRESS_OVERLAY_IP"
            )
        base.update({
            "transport": "nebula-socks",
            "overlay_ip": overlay,
            "socks_port": config["socks_port"],
            "auth": "nebula-cert",
            "note": (
                "Nebula overlay datapath. Ensure the node is enrolled in the overlay, "
                "then point the SZ Chrome profile at socks5://<overlay_ip>:<socks_port>."
            ),
        })
        return base
    if transport == "hysteria2":
        host = str(config.get("hysteria_host") or config["exit_host"]).strip()
        if not str(config.get("hysteria_auth", "")).strip():
            raise NetEgressConfigError(
                "hysteria2 transport requires RYNMESH_EGRESS_HYSTERIA_AUTH"
            )
        base.update({
            "transport": "hysteria2",
            "exit_host": host,
            "udp_port": int(config["hysteria_udp_port"]),
            "socks_port": config["socks_port"],
            "auth": config["hysteria_auth"],
            "auth_kind": "password",
            "sni": config["hysteria_sni"],
            "obfs": config["hysteria_obfs"],
            "up_mbps": int(config["hysteria_up_mbps"]),
            "down_mbps": int(config["hysteria_down_mbps"]),
            "tls_insecure": bool(config["hysteria_tls_insecure"]),
            "tls_pin": config["hysteria_tls_pin"],
            "note": (
                "Hysteria2 QUIC datapath. Consumer runs `hysteria client` exposing "
                "a local SOCKS5 on socks_port; point the SZ Chrome profile at it."
            ),
        })
        return base
    base.update({
        "transport": "ssh-socks5",
        "exit_host": config["exit_host"],
        "exit_user": config["exit_user"],
        "exit_port": config["exit_port"],
        "socks_port": config["socks_port"],
        "auth": "shared_key",
        "note": (
            "Set RYNMESH_VPN_GATEWAY=<exit_user>@<exit_host> and "
            "RYNMESH_VPN_PORT=<socks_port>, then run `rynmesh-vpn chrome`."
        ),
    })
    return base


class NetEgressWorker(ServiceWorker):
    capability = CAPABILITY
    operation = OPERATION

    def execute(self, params: dict[str, Any]) -> dict[str, Any]:
        config = _provider_config()
        session = build_session(params, config)
        record_egress_credit(
            {
                "ts": session["issued_at"],
                "event": "egress_session_opened",
                "consumer_peer_id": str(params.get("client_peer_id", "") or ""),
                "region": session["region"],
                "exit_host": session.get("exit_host") or session.get("overlay_ip", ""),
                "ttl_s": session["ttl_s"],
                "price_credits": session["price_credits"],
            }
        )
        return {"message": json.dumps(session)}


def main(argv: list[str] | None = None) -> int:
    import argparse

    ap = argparse.ArgumentParser(description="rynmesh net.egress provider worker")
    ap.add_argument("--poll-interval-s", type=float, default=2.0)
    ap.add_argument(
        "--network-id",
        default=os.environ.get("RYNMESH_NETWORK_ID", "rynmesh-main"),
    )
    args = ap.parse_args(argv)
    # Fail fast if misconfigured before entering the poll loop.
    _provider_config()
    NetEgressWorker().serve_forever(
        network_id=args.network_id, poll_interval_s=args.poll_interval_s
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
