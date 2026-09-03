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
from contextlib import contextmanager
from typing import Any

import pytest

pytest.importorskip("cryptography")
pytest.importorskip("fastapi")

NETWORK_KEY = "two-node-mailbox-key"
NETWORK_ID = "mailboxnet"
KIND = "friend.invite.accept.v1"
PEER_MESSAGE_KIND = "peer.message.v1"


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _node(tmp_path, name: str, monkeypatch):
    """A node app on its own home, built with the environment as it stands."""
    from rynmesh.peer_http import create_app
    from rynmesh.store import RynmeshStore

    home = tmp_path / name
    monkeypatch.setenv("RYNMESH_HOME", str(home))
    store = RynmeshStore(home=home, network_dir=tmp_path / f"{name}-net")
    return create_app(store), store


@contextmanager
def _registry_server(tmp_path, monkeypatch):
    """A real registry on a loopback port, with the node env pointed at it.

    Yields the `FilePeerRegistry` behind it so a test can inspect the spool on
    disk. Nodes are given no peer endpoint: they are reachable only through the
    registry, which is exactly the case the mailbox exists for.
    """

    uvicorn = pytest.importorskip("uvicorn")
    from rynmesh.registry import FilePeerRegistry
    from rynmesh.registry_http import create_app as create_registry_app

    port = _free_port()
    monkeypatch.setenv("RYNMESH_NETWORK_KEY", NETWORK_KEY)
    monkeypatch.setenv("RYNMESH_REGISTRY_URL", f"http://127.0.0.1:{port}")
    monkeypatch.setenv("RYNMESH_NETWORK_ID", NETWORK_ID)
    monkeypatch.setenv("RYNMESH_AUTO_REGISTER", "1")
    monkeypatch.setenv("RYNMESH_ALLOW_REMOTE_CONTROL", "1")
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
        yield registry, port
    finally:
        server.should_exit = True
        thread.join(timeout=10)


def test_two_nodes_exchange_mail_through_a_registry_server(tmp_path, monkeypatch) -> None:
    from fastapi.testclient import TestClient

    with _registry_server(tmp_path, monkeypatch) as (registry, port):
        alice_app, alice_store = _node(tmp_path, "alice", monkeypatch)
        bob_app, bob_store = _node(tmp_path, "bob", monkeypatch)

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
            # `create_app` also registers the peer-message relay handler.
            assert status["handlers"] == sorted([KIND, PEER_MESSAGE_KIND])
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


def test_a_peer_message_survives_a_dead_direct_transport(tmp_path, monkeypatch) -> None:
    """Alice's chat message reaches Bob through the mailbox, not the wire.

    `RYNMESH_MESSAGING_FORCE_MAILBOX=1` makes Alice's direct transport report
    failure without trying, so the only route left is store-and-forward.
    """

    from fastapi.testclient import TestClient

    monkeypatch.setenv("RYNMESH_MESSAGING_FORCE_MAILBOX", "1")
    with _registry_server(tmp_path, monkeypatch) as (registry, _port):
        alice_app, alice_store = _node(tmp_path, "alice", monkeypatch)
        bob_app, bob_store = _node(tmp_path, "bob", monkeypatch)

        with TestClient(alice_app) as alice_client, TestClient(bob_app) as bob_client:
            sent = alice_client.post(
                "/api/local/messages/send",
                json={"peer_id": bob_store.peer_id, "text": "carried by the mailbox"},
            ).json()
            assert sent["delivered"] is False
            assert sent["via"] == "mailbox"

            assert bob_app.state.mailbox.poll_once() == 1

            history = bob_client.get(
                "/api/local/messages", params={"peer_id": alice_store.peer_id}
            ).json()
            assert [(item["dir"], item["text"], item["from"]) for item in history] == [
                ("in", "carried by the mailbox", alice_store.peer_id)
            ]
            # Bob decrypted it, so the plaintext never travelled: the registry
            # only ever held the sealed envelope, and the box is drained now.
            assert bob_app.state.mailbox.poll_once() == 0
            assert sorted((registry.root / "mailbox").rglob("*.json")) == []

            assert bob_client.get("/api/local/mailbox/status").json()["handled_total"] == 1
            assert alice_client.get(
                "/api/local/messages", params={"peer_id": bob_store.peer_id}
            ).json()[0]["via"] == "mailbox"


def test_a_relayed_header_cannot_claim_another_sender(tmp_path, monkeypatch) -> None:
    """A depositor may only relay messages that say they are from itself."""

    from fastapi.testclient import TestClient

    from rynmesh.mailbox_routes import PEER_MESSAGE_KIND as KIND_UNDER_TEST

    monkeypatch.setenv("RYNMESH_MESSAGING_FORCE_MAILBOX", "1")
    with _registry_server(tmp_path, monkeypatch) as (registry, _port):
        alice_app, alice_store = _node(tmp_path, "alice", monkeypatch)
        bob_app, bob_store = _node(tmp_path, "bob", monkeypatch)
        carol_app, carol_store = _node(tmp_path, "carol", monkeypatch)

        with TestClient(alice_app), TestClient(bob_app) as bob_client, TestClient(carol_app):
            # Carol seals a well-formed peer-message header that names Alice as
            # the sender, and deposits it in Bob's box under her own signature.
            carol_app.state.mailbox.deposit(
                bob_store.peer_id,
                KIND_UNDER_TEST,
                {"v": 1, "from": alice_store.peer_id, "to": bob_store.peer_id,
                 "nonce": "AAAAAAAAAAAAAAAA", "ciphertext": "AAAA", "from_pub": "AAAA"},
            )
            assert carol_store.peer_id != alice_store.peer_id

            # Handler raises -> three attempts, then a drop; nothing is written
            # to Bob's history. The drop's ack rides the fourth poll.
            for _ in range(4):
                bob_app.state.mailbox.poll_once()
            status = bob_client.get("/api/local/mailbox/status").json()
            assert status["handled_total"] == 0
            assert status["dropped_total"] == 1
            assert bob_client.get(
                "/api/local/messages", params={"peer_id": alice_store.peer_id}
            ).json() == []
            assert sorted((registry.root / "mailbox").rglob("*.json")) == []
