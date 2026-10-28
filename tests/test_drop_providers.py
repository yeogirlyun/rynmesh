"""Provider-scoped text messages between two real, isolated loopback nodes."""

from __future__ import annotations

import copy
import http.client
import ipaddress
import json
import socket

import pytest
from test_drop_engine import Node, link, wait_for

from rynmesh.drop import codec
from rynmesh.drop.engine import DropError
from rynmesh.drop.provider_targets import provider_target

RESOURCE = "https://provider.test/connect"
SECOND = "https://second.test/connect"


@pytest.fixture(autouse=True)
def loopback_only(monkeypatch):
    original = socket.socket.connect

    def connect(sock, address):
        if not isinstance(address, tuple) or not ipaddress.ip_address(address[0]).is_loopback:
            raise AssertionError("external network is disabled")
        return original(sock, address)

    monkeypatch.setattr(socket.socket, "connect", connect)


class Authority:
    def __init__(self, nodes):
        self.nodes = nodes
        self.active = {node.identity.peer_id for node in nodes}
        self.unavailable = set()
        self.calls = []

    def rows(self):
        # Intentionally retains revoked entries to exercise fail-closed live checks.
        return [{"peer_id": node.identity.peer_id, "messaging_pub": node.e.msg_pub,
                 "name": node.identity.node_name, "endpoints": [node.endpoint],
                 "member_id": str(index), "member_name": f"Member {index}",
                 "provider_resource": resource, "plugin_id": "test-plugin"}
                for resource in (RESOURCE, SECOND) for index, node in enumerate(self.nodes)]

    def authorize(self, caller, resource, sender, receiver, capability):
        self.calls.append((caller, resource, sender, receiver, capability))
        if (caller in self.unavailable or resource not in (RESOURCE, SECOND) or caller not in (sender, receiver)
                or sender not in self.active or receiver not in self.active or capability != "message.text"):
            raise RuntimeError("synthetic secret must never reach the UI")
        return {"allowed": True, "resource": resource, "from_peer_id": sender,
                "to_peer_id": receiver, "capability": capability}


@pytest.fixture
def provider_pair(tmp_path):
    a, b = Node(tmp_path, "alpha").start(), Node(tmp_path, "beta").start()
    authority = Authority([a, b])
    for node in (a, b):
        node.e._provider_peers = authority.rows
        node.e._provider_authorize = lambda resource, sender, receiver, capability, node=node: authority.authorize(
            node.identity.peer_id, resource, sender, receiver, capability)
    try:
        yield a, b, authority
    finally:
        a.stop()
        b.stop()


def target(node, resource=RESOURCE):
    return provider_target(resource, node.identity.peer_id)


def wire(a, b, *, resource=RESOURCE, message_id=None, text="sealed message"):
    secret = json.dumps({"text": text, "kind": "text"}).encode()
    payload = {"message_id": message_id or codec.new_id(), "from_pub": a.e.msg_pub,
               "provider_resource": resource,
               "sealed": codec.seal_for(a.msg_key, b.e.msg_pub, secret, codec.MESSAGE_INFO)}
    return codec.make_envelope("message", payload, sender=a.identity.peer_id, to=b.identity.peer_id,
                              private_key_bytes=a.identity.private_key_bytes)


def test_two_nodes_deliver_signed_encrypted_text_both_directions(provider_pair, monkeypatch):
    a, b, authority = provider_pair
    captured = []
    original = a.e._call

    def capture(endpoint, kind, peer_id, payload, **kwargs):
        captured.append(copy.deepcopy(payload))
        return original(endpoint, kind, peer_id, payload, **kwargs)

    monkeypatch.setattr(a.e, "_call", capture)
    peers = a.e.peers()
    entry = next(row for row in peers if row["peer_id"] == target(b))
    assert entry["relationship"] == "external" and entry["device_peer_id"] == b.identity.peer_id
    assert entry["file_sendable"] is False and entry["online"] is None
    assert a.e.relationship(b.identity.peer_id) == "other"
    sent = a.e.send_message({"peer_id": target(b), "text": "private alpha text", "client_id": "send-1"})
    assert sent["client_id"] == "send-1"
    wait_for(lambda: a.e.db.message(sent["message_id"])["status"] == "delivered")
    assert b.e.conversation(target(a))["items"][0]["message"]["text"] == "private alpha text"
    assert b.e.conversation(a.identity.peer_id)["items"] == []
    assert "private alpha text" not in json.dumps(captured)
    assert captured[0]["provider_resource"] == RESOURCE
    assert {row[0] for row in authority.calls} == {a.identity.peer_id, b.identity.peer_id}
    reply = b.e.send_message({"peer_id": target(a), "text": "reply"})
    wait_for(lambda: b.e.db.message(reply["message_id"])["status"] == "delivered")
    assert a.e.conversation(target(b))["items"][-1]["message"]["text"] == "reply"


@pytest.mark.parametrize("failure", ["revoked", "unavailable"])
def test_receiver_rechecks_membership_and_retry_cannot_bypass(provider_pair, failure):
    a, b, authority = provider_pair
    if failure == "revoked":
        authority.active.remove(a.identity.peer_id)
    else:
        authority.unavailable.add(b.identity.peer_id)
    if failure == "revoked":
        with pytest.raises(DropError, match="Message permission is unavailable"):
            a.e.send_message({"peer_id": target(b), "text": "denied"})
        with pytest.raises(DropError):
            b.e.handle_message(wire(a, b), "127.0.0.1")
    else:
        sent = a.e.send_message({"peer_id": target(b), "text": "denied"})
        wait_for(lambda: a.e.db.message(sent["message_id"])["status"] == "failed")
        assert "synthetic secret" not in json.dumps(a.e.message_api(a.e.db.message(sent["message_id"])))
        authority.active.remove(a.identity.peer_id)
        with pytest.raises(DropError):
            a.e.retry_message(sent["message_id"])
    assert b.e.conversation(target(a))["items"] == []


def test_forged_signature_key_and_unknown_resource_do_not_authorize(provider_pair):
    a, b, authority = provider_pair
    forged = wire(a, b)
    forged["payload"]["provider_resource"] = SECOND
    with pytest.raises(codec.DropProtocolError):
        b.e.handle_message(forged, "127.0.0.1")
    unknown = wire(a, b, resource="https://attacker.test/metadata")
    with pytest.raises(DropError):
        b.e.handle_message(unknown, "127.0.0.1")
    bad_key = wire(a, b)
    bad_key["payload"]["from_pub"] = b.e.msg_pub
    bad_key = codec.make_envelope("message", bad_key["payload"], sender=a.identity.peer_id,
                                 to=b.identity.peer_id, private_key_bytes=a.identity.private_key_bytes)
    with pytest.raises(DropError):
        b.e.handle_message(bad_key, "127.0.0.1")
    assert authority.calls == []
    assert b.e.db.conversation_peers() == []


def test_history_idempotency_and_receipts_are_source_scoped(provider_pair):
    a, b, authority = provider_pair
    message_id = codec.new_id()
    one = wire(a, b, message_id=message_id)
    b.e.handle_message(one, "127.0.0.1")
    b.e.handle_message(one, "127.0.0.1")
    b.e.handle_message(wire(a, b, resource=SECOND, message_id=message_id), "127.0.0.1")
    assert len(b.e.conversation(target(a))["items"]) == 1
    assert len(b.e.conversation(target(a, SECOND))["items"]) == 1
    assert b.e.conversation(a.identity.peer_id)["items"] == []
    for resource in (RESOURCE, SECOND):
        sent = a.e.send_message({"peer_id": target(b, resource), "text": resource, "client_id": "same-client"})
        wait_for(lambda sent=sent: a.e.db.message(sent["message_id"])["status"] == "delivered")
    assert len(a.e.db.messages(target(b))) == len(a.e.db.messages(target(b, SECOND))) == 1
    authority.active.remove(a.identity.peer_id)
    with pytest.raises(DropError):
        b.e.handle_message(one, "127.0.0.1")


def test_provider_never_grants_file_or_existing_friend_capabilities(provider_pair, tmp_path):
    a, b, authority = provider_pair
    path = tmp_path / "private.txt"
    path.write_text("local file")
    for operation in (a.e.preflight, a.e.create_transfer):
        with pytest.raises(DropError, match="only supports text"):
            operation({"peer_id": target(b), "paths": [str(path)]})
    # Even a separately trusted own peer cannot use the provider scope for files.
    link(a, b)
    offer = codec.make_envelope("offer", {"provider_resource": RESOURCE}, sender=a.identity.peer_id,
                               to=b.identity.peer_id, private_key_bytes=a.identity.private_key_bytes)
    with pytest.raises(DropError, match="only supports text"):
        b.e.handle_offer(offer, "127.0.0.1")
    assert authority.calls == []


@pytest.mark.parametrize("relationship", ["own", "friend"])
def test_private_messages_still_use_existing_trust_without_provider(provider_pair, relationship):
    a, b, authority = provider_pair
    link(a, b, own=relationship == "own")
    if relationship == "friend":
        a.e._friend_peers = lambda: {b.identity.peer_id: "beta"}
        b.e._friend_peers = lambda: {a.identity.peer_id: "alpha"}
    authority.unavailable.update(authority.active)
    sent = a.e.send_message({"peer_id": b.identity.peer_id, "text": "private channel"})
    wait_for(lambda: a.e.db.message(sent["message_id"])["status"] == "delivered")
    assert b.e.conversation(a.identity.peer_id)["items"][0]["message"]["text"] == "private channel"
    assert b.e.conversation(target(a))["items"] == []
    assert authority.calls == []


def test_unreachable_provider_message_fails_without_relay_or_automatic_resend(provider_pair):
    a, b, _authority = provider_pair
    b.server.should_exit = True
    b.thread.join(timeout=10)
    sent = a.e.send_message({"peer_id": target(b), "text": "unreachable"})
    wait_for(lambda: a.e.db.message(sent["message_id"])["status"] == "failed")
    assert a.e.db.outbox() == []
    assert a.e.capabilities()["relay"]["available"] is False


def test_signed_receipt_for_another_source_is_not_reported_delivered(provider_pair, monkeypatch):
    a, b, _authority = provider_pair
    original = b.e._respond

    def wrong_source(peer_id, payload, request):
        return original(peer_id, {**payload, "provider_resource": SECOND}, request)

    monkeypatch.setattr(b.e, "_respond", wrong_source)
    sent = a.e.send_message({"peer_id": target(b), "text": "requires exact receipt"})
    wait_for(lambda: a.e.db.message(sent["message_id"])["status"] == "failed")
    assert a.e.db.message(sent["message_id"])["delivered_at"] is None


def test_provider_probe_and_message_never_send_the_private_network_key(provider_pair, monkeypatch):
    a, b, _authority = provider_pair
    monkeypatch.setenv("RYNMESH_NETWORK_KEY", "private-network-value")
    captured = []
    original = http.client.HTTPConnection.request

    def request(connection, method, url, body=None, headers=None, **kwargs):
        captured.append({key.lower(): value for key, value in (headers or {}).items()})
        return original(connection, method, url, body=body, headers=headers or {}, **kwargs)

    monkeypatch.setattr(http.client.HTTPConnection, "request", request)
    sent = a.e.send_message({"peer_id": target(b), "text": "only this text"})
    wait_for(lambda: a.e.db.message(sent["message_id"])["status"] == "delivered")
    assert len(captured) >= 2
    assert all("x-ryn-auth" not in headers and "authorization" not in headers for headers in captured)


def test_provider_text_uses_existing_p2p_without_granting_file_routes(tmp_path, monkeypatch):
    pytest.importorskip("aioquic")
    ice = pytest.importorskip("aioice.ice")
    from test_drop_p2p import SignalBus, p2p_node

    monkeypatch.setenv("RYNMESH_P2P_NATIVE", "off")
    monkeypatch.setenv("RYNMESH_P2P_STUN", "off")
    monkeypatch.setattr(ice, "get_host_addresses", lambda **kwargs: ["127.0.0.1"])
    bus = SignalBus()
    a, b = p2p_node(tmp_path, "alpha", bus).start(), p2p_node(tmp_path, "beta", bus).start()
    authority = Authority([a, b])
    rows = authority.rows()
    for row in rows:
        row["endpoints"] = []
    for node in (a, b):
        node.e._provider_peers = lambda: rows
        node.e._provider_authorize = lambda resource, sender, receiver, capability, node=node: authority.authorize(
            node.identity.peer_id, resource, sender, receiver, capability)
    try:
        sent = a.e.send_message({"peer_id": target(b), "text": "p2p text"})
        wait_for(lambda: a.e.db.message(sent["message_id"])["status"] == "delivered", timeout=45)
        probe = a.e._provider_directory(RESOURCE).cached(b.identity.peer_id)
        assert probe.endpoint.startswith("ryn-p2p://")
        assert b.e.conversation(target(a))["items"][0]["message"]["text"] == "p2p text"
        assert a.e.relationship(b.identity.peer_id) == b.e.relationship(a.identity.peer_id) == "other"
        offer = codec.make_envelope("offer", {"provider_resource": RESOURCE}, sender=a.identity.peer_id,
                                   to=b.identity.peer_id, private_key_bytes=a.identity.private_key_bytes)
        status, response = a.e._provider_directory(RESOURCE).request(probe.endpoint, "POST", "/api/peer/drop/offer", offer)
        assert status == 403 and response["code"] == "capability_denied"
    finally:
        a.stop()
        b.stop()


def test_text_route_bounds_input_before_parsing(provider_pair):
    _a, b, authority = provider_pair
    connection = http.client.HTTPConnection("127.0.0.1", b.port, timeout=5)
    try:
        connection.request("POST", "/api/peer/drop/message", body=b" " * (256 * 1024 + 1),
                           headers={"Content-Type": "application/json"})
        response = connection.getresponse()
        assert response.status == 413
        assert json.loads(response.read())["code"] == "too_large"
        assert authority.calls == []
    finally:
        connection.close()


def test_provider_probe_rejects_a_signed_hello_with_a_different_bound_key(provider_pair):
    a, b, authority = provider_pair
    rows = authority.rows()
    for row in rows:
        if row["peer_id"] == b.identity.peer_id:
            row["messaging_pub"] = a.e.msg_pub
    a.e._provider_peers = lambda: rows
    with pytest.raises(DropError, match="identity changed"):
        a.e.provider_probe(RESOURCE, b.identity.peer_id, fresh=True)
    assert authority.calls == []


def test_revocation_after_probe_stops_the_message_before_transport(provider_pair, monkeypatch):
    a, b, authority = provider_pair
    original = a.e.provider_probe

    def revoke_after_probe(resource, peer_id, **kwargs):
        result = original(resource, peer_id, **kwargs)
        authority.active.remove(a.identity.peer_id)
        return result

    monkeypatch.setattr(a.e, "provider_probe", revoke_after_probe)
    sent = a.e.send_message({"peer_id": target(b), "text": "must not leave"})
    wait_for(lambda: a.e.db.message(sent["message_id"])["status"] == "failed")
    assert b.e.conversation(target(a))["items"] == []
