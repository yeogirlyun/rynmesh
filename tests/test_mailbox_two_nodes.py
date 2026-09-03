"""Two real nodes exchange mailbox mail through a real registry server.

Nothing is stubbed: a uvicorn registry on a loopback port, two `create_app`
nodes with their own homes and identities, a shared network key on the wire,
and auto-registration carrying each node's messaging key into its signed
record. This is the path a pairing message actually takes when neither peer
can reach the other's endpoint.
"""

from __future__ import annotations

import socket
import threading
import time
from typing import Any

import pytest

pytest.importorskip("cryptography")
pytest.importorskip("fastapi")

NETWORK_KEY = "two-node-mailbox-key"
NETWORK_ID = "mailboxnet"
KIND = "friend.invite.accept.v1"


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _node(tmp_path, name: str):
    """A node app on its own home, built with the environment as it stands."""
    import os

    from rynmesh.peer_http import create_app
    from rynmesh.store import RynmeshStore

    home = tmp_path / name
    os.environ["RYNMESH_HOME"] = str(home)
    store = RynmeshStore(home=home, network_dir=tmp_path / f"{name}-net")
    return create_app(store), store


def test_two_nodes_exchange_mail_through_a_registry_server(tmp_path, monkeypatch) -> None:
    uvicorn = pytest.importorskip("uvicorn")
    from fastapi.testclient import TestClient

    from rynmesh.registry import FilePeerRegistry
    from rynmesh.registry_http import create_app as create_registry_app

    port = _free_port()
    monkeypatch.setenv("RYNMESH_NETWORK_KEY", NETWORK_KEY)
    monkeypatch.setenv("RYNMESH_REGISTRY_URL", f"http://127.0.0.1:{port}")
    monkeypatch.setenv("RYNMESH_NETWORK_ID", NETWORK_ID)
    monkeypatch.setenv("RYNMESH_AUTO_REGISTER", "1")
    monkeypatch.setenv("RYNMESH_ALLOW_REMOTE_CONTROL", "1")
    # No peer endpoint: these two nodes are only reachable through the
    # registry, which is exactly the case the mailbox exists for.
    monkeypatch.delenv("RYNMESH_PEER_PORT", raising=False)
    monkeypatch.delenv("RYNMESH_PEER_ENDPOINT", raising=False)
    monkeypatch.delenv("RYNMESH_LOCAL_TOKEN", raising=False)

    registry = FilePeerRegistry(tmp_path / "registry")
    server = uvicorn.Server(
        uvicorn.Config(
            create_registry_app(registry), host="127.0.0.1", port=port, log_level="warning"
        )
    )
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    try:
        deadline = time.time() + 15
        while not server.started and time.time() < deadline:
            time.sleep(0.05)
        assert server.started, "registry server did not start"

        alice_app, alice_store = _node(tmp_path, "alice")
        bob_app, bob_store = _node(tmp_path, "bob")

        received: list[tuple[str, dict]] = []

        def handler(envelope: Any, body: dict) -> None:
            received.append((envelope.from_peer_id, body))

        bob_app.state.mailbox.register_handler(KIND, handler)

        with TestClient(alice_app) as alice_client, TestClient(bob_app) as bob_client:
            # Auto-registration ran in each lifespan; the messaging key is in
            # the signed record, which is the only way Alice can seal for Bob.
            peers = alice_store.discover_peers(network_id=NETWORK_ID, include_self=True)["peers"]
            advertised = {
                item["peer_id"]: (item.get("metadata") or {}).get("messaging_pub", "")
                for item in peers
            }
            assert advertised.get(bob_store.peer_id), "Bob's messaging key was not advertised"
            assert advertised.get(alice_store.peer_id)
            # Neither node published a reachable endpoint, so the direct
            # /api/peer/pubkey lookup cannot be what resolves the key below.
            assert not [
                endpoint
                for item in peers
                for endpoint in item.get("endpoints", [])
                if str(endpoint).startswith("http")
            ]

            receipt = alice_app.state.mailbox.deposit(
                bob_store.peer_id, KIND, {"invite_id": "xyz", "accepted": True}
            )
            assert receipt["message_id"]

            assert bob_app.state.mailbox.poll_once() == 1
            assert received == [(alice_store.peer_id, {"invite_id": "xyz", "accepted": True})]

            # The ack rides the next poll and empties the registry-side box.
            assert bob_app.state.mailbox.poll_once() == 0
            assert sorted((registry.root / "mailbox").rglob("*.json")) == []

            status = bob_client.get("/api/local/mailbox/status").json()
            assert status["handled_total"] == 1
            assert status["dropped_total"] == 0
            assert status["last_error"] == ""
            assert status["handlers"] == [KIND]
            assert status["worker"]["name"] == "mailbox.poll"

            # The peer surface really is keyed: the same registry refuses a
            # caller without the shared header.
            import urllib.error
            import urllib.request

            request = urllib.request.Request(
                f"http://127.0.0.1:{port}/api/v1/mailbox/poll", data=b"{}", method="POST"
            )
            with pytest.raises(urllib.error.HTTPError) as unkeyed:
                urllib.request.urlopen(request, timeout=10)
            assert unkeyed.value.code == 404

            assert alice_client.get("/api/local/mailbox/status").json()["handled_total"] == 0
    finally:
        server.should_exit = True
        thread.join(timeout=10)
