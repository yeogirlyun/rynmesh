"""Wire the peer mailbox into a node app: client, poll worker, status route.

Kept out of ``peer_http`` so the node module does not grow another feature's
plumbing. ``install_mailbox`` is the whole surface; ``with_registry_fallback``
is the second half of messaging-key discovery, used to wrap the node's direct
``/api/peer/pubkey`` lookup so a peer behind a NAT is still reachable.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Callable

from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey
from fastapi import Request

from .background_workers import (
    BackgroundWorkerRegistry,
    BackgroundWorkerSpec,
    BackoffPolicy,
    WorkerRunResult,
)
from .mailbox_client import MailboxClient

MAILBOX_WORKER_NAME = "mailbox.poll"

MAILBOX_POLL_POLICY = BackoffPolicy(
    busy_delay_s=2.0,
    idle_initial_s=5.0,
    idle_multiplier=1.5,
    idle_max_s=60.0,
    error_multiplier=2.0,
    error_max_s=120.0,
)
MAILBOX_POLL_INITIAL_DELAY_S = 3.0


def install_mailbox(
    app: Any,
    *,
    store: Any,
    messaging_key: X25519PrivateKey,
    home: str | Path,
    resolve_pubkey: Callable[[str], str],
    workers: BackgroundWorkerRegistry,
    local_control: Callable[[Request], None] | None = None,
) -> MailboxClient:
    """Create the client, supervise its poll worker, expose its status."""

    client = MailboxClient(
        store=store,
        messaging_key=messaging_key,
        home=home,
        resolve_messaging_pub=resolve_pubkey,
    )
    app.state.mailbox = client
    app.state.mailbox_error = ""

    if getattr(store, "registry", None) is not None:
        # A node with no registry has nowhere to poll; registering the worker
        # anyway would just burn a task on a permanent no-op.
        workers.register(
            BackgroundWorkerSpec(
                name=MAILBOX_WORKER_NAME,
                # The supervisor reads activity from a bool/WorkerRunResult, so
                # the handled count has to be turned into one for the busy/idle
                # backoff to work at all.
                run_once=lambda: WorkerRunResult(activity=client.poll_once() > 0),
                initial_delay_s=MAILBOX_POLL_INITIAL_DELAY_S,
                policy=MAILBOX_POLL_POLICY,
                error_sink=lambda value: setattr(app.state, "mailbox_error", value),
            ),
            replace=True,
        )

    @app.get("/api/local/mailbox/status")
    def local_mailbox_status(request: Request) -> dict[str, Any]:
        # /api/local is gated by the node-auth middleware; this mirrors the
        # per-route re-check the routes in `peer_http` do on top of it.
        if local_control is not None:
            local_control(request)
        return {**client.status(), "worker": workers.status().get(MAILBOX_WORKER_NAME)}

    return client


def registry_messaging_pub(store: Any, peer_id: str, *, network_id: str) -> str:
    """The peer's advertised X25519 messaging key, from its registry record."""

    try:
        discovered = store.discover_peers(network_id=network_id, include_self=False) or {}
    except Exception:
        # Discovery is a best-effort fallback; its failure must surface as the
        # caller's original "cannot resolve this peer" error, not a new one.
        return ""
    for record in discovered.get("peers", []) or ():
        if record.get("peer_id") != peer_id:
            continue
        metadata = record.get("metadata") or {}
        return str(metadata.get("messaging_pub") or "") if isinstance(metadata, dict) else ""
    return ""


def with_registry_fallback(
    resolve: Callable[[str], str],
    *,
    store: Any,
    cache: dict[str, str],
    network_id: Callable[[], str],
) -> Callable[[str], str]:
    """Fall back to the registry record when a peer has no reachable endpoint.

    Direct ``/api/peer/pubkey`` is the trusted path and stays first. Two nodes
    behind NATs never have an endpoint for each other, and for them the only
    published copy of the messaging key is the signed registry record.
    """

    def resolve_pubkey(peer_id: str) -> str:
        cached = cache.get(peer_id)
        if cached:
            return cached
        try:
            return resolve(peer_id)
        except Exception:
            pub = registry_messaging_pub(store, peer_id, network_id=network_id())
            if not pub:
                raise
            cache[peer_id] = pub  # TOFU, exactly as the direct lookup caches
            return pub

    return resolve_pubkey


__all__ = [
    "MAILBOX_POLL_INITIAL_DELAY_S",
    "MAILBOX_POLL_POLICY",
    "MAILBOX_WORKER_NAME",
    "install_mailbox",
    "registry_messaging_pub",
    "with_registry_fallback",
]
