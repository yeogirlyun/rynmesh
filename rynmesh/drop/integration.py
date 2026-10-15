"""Wire device drop into the node daemon using its existing trust sources."""

from __future__ import annotations

from typing import Any, Callable

from fastapi import FastAPI

from .engine import DropEngine
from .routes import install_drop


def install_device_drop(app: FastAPI, *, store: Any, messaging_key: Any, personal_space: Any,
                        network_id: Callable[[], str], local_control: Callable) -> DropEngine:
    """Own devices come from the personal space, friends from the friends store.

    Nearby (LAN-discovered, unpaired) devices are handled inside the engine and
    need a verification code before anything they send is released.
    """

    def own_peers() -> dict[str, str]:
        status = personal_space.status()
        if status.get("membership") != "active":
            return {}
        return {
            str(m["peer_id"]): str(m.get("name") or "")
            for m in status.get("members", [])
            if m.get("peer_id") and m.get("peer_id") != store.peer_id and not m.get("removed")
        }

    def friend_rows() -> list[dict]:
        friends_state = getattr(app.state, "friends", None)
        if friends_state is None:
            return []
        return [row for row in friends_state.service.store.list_relationships()
                if row.get("status") == "active" and row.get("peer_id")]

    def friend_peers() -> dict[str, str]:
        return {str(row["peer_id"]): str(row.get("node_name") or "") for row in friend_rows()}

    def peer_endpoints(peer_id: str) -> list[str]:
        return [str(row.get("endpoint") or "") for row in friend_rows() if row.get("peer_id") == peer_id]

    engine = DropEngine(node=store, home=store.home, messaging_key=messaging_key, network_id=network_id,
                        own_peers=own_peers, friend_peers=friend_peers, peer_endpoints=peer_endpoints)
    return install_drop(app, engine=engine, local_control=local_control)
