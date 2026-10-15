"""Native P2P transport (native/ryn_transport): hole punching + QUIC in Rust.

Real sockets on this machine, real probes, real quinn connections. Skipped
when the extension is not installed (pure-Python installs keep aioice).
"""

from __future__ import annotations

import asyncio
import time

import pytest

ryn_transport = pytest.importorskip("ryn_transport")

from rynmesh import native_transport  # noqa: E402
from rynmesh.llm_package import p2p  # noqa: E402
from rynmesh.llm_package.p2p_session import SessionPool  # noqa: E402

MIB = 1 << 20


@pytest.fixture(autouse=True)
def _local_only(monkeypatch):
    monkeypatch.setenv("RYNMESH_P2P_STUN", "off")
    monkeypatch.setenv("RYNMESH_P2P_NATIVE", "auto")
    for name in ("RYNMESH_P2P_BIND_PORT", "RYNMESH_P2P_REQUIRE_PUBLIC", "RYNMESH_P2P_REQUIRE_DISTINCT_PUBLIC"):
        monkeypatch.delenv(name, raising=False)


async def _pair(timeout_s: float = 10.0, *, tamper=None):
    server = await native_transport.open_session("server")
    client = await native_transport.open_session("client")
    server_info, client_info = native_transport.offer(server), native_transport.offer(client)
    if tamper:
        tamper(server_info, client_info)
    return await asyncio.gather(
        native_transport.connect(server, native_transport.remote_info(client_info), timeout_s),
        native_transport.connect(client, native_transport.remote_info(server_info), timeout_s),
        return_exceptions=True,
    )


def test_hole_punching_then_quic_moves_data_with_backpressure():
    async def scenario():
        server, client = await _pair()
        evidence = native_transport.evidence(client)
        assert evidence["transport"] == "ice_udp_direct" and evidence["relay_used"] is False
        assert evidence["native"] is True and evidence["data_plane"] == "quic"

        async def sink():
            stream = await server.accept_stream(10)
            reader = native_transport.StreamReader(stream)
            total = 0
            while chunk := await reader.read(1 << 20):
                total += len(chunk)
            await stream.write(str(total).encode())
            await stream.finish()

        task = asyncio.create_task(sink())
        stream = await client.open_stream()
        for _ in range(32):  # well past the 32 MiB stream window: writes must wait
            await stream.write(b"\x01" * MIB)
        await stream.finish()
        reply = await native_transport.StreamReader(stream).read()
        await task
        assert int(reply) == 32 * MIB
        assert client.stats()["congestion"] in {"cubic", "bbr"}
        client.close()
        await asyncio.wait_for(server.wait_closed(), 5)

    asyncio.run(scenario())


def test_client_refuses_a_certificate_other_than_the_signed_one():
    async def scenario():
        other = await native_transport.open_session("server")

        def swap(server_info, client_info):
            server_info["fingerprint"] = native_transport.offer(other)["fingerprint"]

        results = await _pair(5.0, tamper=swap)
        other.close()
        assert isinstance(results[1], Exception)  # the client side

    asyncio.run(scenario())


def test_probes_from_a_device_without_the_shared_secret_are_ignored():
    async def scenario():
        def wrong_secret(server_info, client_info):
            client_info["secret"] = "0" * 32

        results = await _pair(2.0, tamper=wrong_secret)
        assert all(isinstance(r, Exception) for r in results)
        assert any(isinstance(r, TimeoutError) for r in results)

    asyncio.run(scenario())


def test_remote_info_accepts_only_literal_unicast_candidates():
    good = {"protocol": native_transport.PROTOCOL, "secret": "a" * 32, "fingerprint": "b" * 64,
            "candidates": [{"type": "host", "addr": "192.168.1.5:5000"}, {"type": "srflx", "addr": "203.0.113.9:6000"}]}
    assert native_transport.remote_info(good)["candidates"][1]["addr"] == "203.0.113.9:6000"
    for bad in [
        {**good, "protocol": "other"},
        {**good, "secret": "xyz"},
        {**good, "fingerprint": "short"},
        {**good, "candidates": [{"type": "host", "addr": "example.com:5000"}]},
        {**good, "candidates": [{"type": "host", "addr": "224.0.0.1:5000"}]},
        {**good, "candidates": [{"type": "host", "addr": "0.0.0.0:5000"}]},
        {**good, "candidates": [{"type": "relay", "addr": "192.168.1.5:5000"}]},
        {**good, "candidates": [{"type": "host", "addr": "192.168.1.5:0"}]},
        {**good, "candidates": [{"type": "host", "addr": f"10.0.0.{i}:5000"} for i in range(40)]},
    ]:
        assert native_transport.remote_info(bad) is None, bad


def test_session_channel_carries_messages_and_answers_probes():
    async def scenario():
        server, client = await _pair()
        provider, consumer = await asyncio.gather(
            native_transport.NativeSessionChannel.open(server, initiator=False, timeout_s=5),
            native_transport.NativeSessionChannel.open(client, initiator=True, timeout_s=5),
        )
        await consumer.send({"task": "one"}, 5)
        assert await provider.receive(5) == {"task": "one"}
        await provider.send({"payload": {"kind": "llm_response"}}, 5)
        assert (await consumer.receive(5))["payload"]["kind"] == "llm_response"
        assert await consumer.probe() is True
        with pytest.raises(asyncio.TimeoutError):
            await consumer.receive(0.2)
        await consumer.close()
        with pytest.raises(p2p.P2PError):
            await provider.receive(5)
        assert await provider.probe() is False

    asyncio.run(scenario())


def _ai_round_trips(monkeypatch, *, provider_native: bool):
    pool = SessionPool()
    offers, answers = [], []

    if not provider_native:
        async def declines(*args, **kwargs):
            return None  # an older provider: no native transport

        monkeypatch.setattr(p2p, "_native_provider_session", declines)

    async def publish(offer):
        offers.append(offer)
        answer = asyncio.get_running_loop().create_future()

        def handler(body, emit):
            for n in range(5):
                emit({"payload": {"kind": "llm_stream"}, "n": n})
            return {"payload": {"kind": "llm_response"}, "task": body["task"]}

        def publish_answer(value):
            answers.append(value)
            answer.set_result(value)

        asyncio.create_task(p2p.provider_exchange(
            offer=offer, publish_answer=publish_answer,
            handle_request=handler, handle_stream_request=handler, timeout_s=5,
        ))
        return await answer

    try:
        evidences = []
        for task in ["one", "two"]:
            events = []
            response, evidence = asyncio.run(p2p.consumer_exchange(
                signed_request={"task": task}, publish_offer=publish, timeout_s=5,
                on_event=events.append, session_pool=pool, session_key="peer",
            ))
            assert response["task"] == task and [e["n"] for e in events] == list(range(5))
            evidences.append(evidence)
        return offers, answers, evidences
    finally:
        pool.close()


def test_ai_sessions_use_the_native_transport_when_both_sides_have_it(monkeypatch):
    offers, answers, evidences = _ai_round_trips(monkeypatch, provider_native=True)
    assert len(offers) == 1 and offers[0].native is not None and offers[0].candidates  # aioice still offered
    assert answers[0].native is not None and not answers[0].candidates
    assert evidences[0]["native"] is True and evidences[0]["transport"] == "ice_udp_direct"
    assert evidences[1]["connection_reused"] is True


def test_ai_sessions_fall_back_to_aioice_for_a_provider_without_it(monkeypatch):
    offers, answers, evidences = _ai_round_trips(monkeypatch, provider_native=False)
    assert offers[0].native is not None
    assert answers[0].native is None and answers[0].candidates
    assert "native" not in evidences[0] and evidences[0]["transport"] == "ice_udp_direct"


def test_failed_native_attempts_cool_down_per_peer(monkeypatch):
    monkeypatch.setattr(native_transport, "_failed_at", {})
    assert native_transport.allowed_for("peer")
    native_transport.note_failure("peer")
    assert not native_transport.allowed_for("peer") and native_transport.allowed_for("other")
    monkeypatch.setattr(native_transport, "FAILURE_COOLDOWN_S", 0.0)
    time.sleep(0.05)  # Windows monotonic clock ticks every ~15.6 ms
    assert native_transport.allowed_for("peer")


@pytest.mark.parametrize("family", ["ipv6", "ipv4"])
def test_one_dual_stack_socket_connects_over_either_address_family(family):
    def only(info):
        keep = [c for c in info["candidates"] if (":" in c["addr"].rpartition(":")[0]) == (family == "ipv6")]
        info["candidates"] = keep
        return keep

    async def scenario():
        probe = await native_transport.open_session("server")
        info = native_transport.offer(probe)
        probe.close()
        if family == "ipv6" and not info.get("dual_stack"):
            pytest.skip("IPv6 is not available on this host")

        def restrict(server_info, client_info):
            assert only(server_info) and only(client_info)

        server, client = await _pair(tamper=restrict)
        assert not isinstance(server, Exception) and not isinstance(client, Exception), (server, client)
        remote = native_transport.evidence(client)["remote"]["host"]
        assert (":" in remote) == (family == "ipv6")
        stream = await client.open_stream()
        await stream.write(b"ping")
        await stream.finish()
        incoming = await server.accept_stream(5)
        assert await native_transport.StreamReader(incoming).read() == b"ping"
        client.close()

    asyncio.run(scenario())
