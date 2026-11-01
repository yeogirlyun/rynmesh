"""Real UDP sockets must be reusable after bounded or cancelled ICE cleanup."""

import asyncio
import socket

import aioice
import pytest

from rynmesh.llm_package import p2p


@pytest.mark.parametrize("fixed", [True, False])
@pytest.mark.parametrize("cancel_close", [False, True])
def test_pending_datagrams_do_not_survive_connection_cleanup(fixed, cancel_close):
    async def scenario():
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
            probe.bind(("127.0.0.1", 0))
            port = probe.getsockname()[1]
        connection = (
            p2p._FixedPortConnection(bind_port=port, ice_controlling=True, stun_server=None)
            if fixed else aioice.Connection(ice_controlling=True, stun_server=None)
        )
        candidates = await connection.get_component_candidates(1, ["127.0.0.1"])
        port = candidates[0].port
        transport = connection._protocols[0].transport
        udp_socket = transport.get_extra_info("socket")
        try:
            # On Windows Proactor this leaves an actual pending write and queue.
            # A graceful close alone waits for them; a timed-out close must abort.
            for _ in range(64):
                transport.sendto(b"pending" * 16, ("127.0.0.1", 9))
            if cancel_close:
                closing = asyncio.create_task(p2p._close_connection(connection))
                await asyncio.sleep(0)
                closing.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await closing
            else:
                await p2p._close_connection(connection)
            assert udp_socket.fileno() == -1
            await p2p._close_connection(connection)
            assert udp_socket.fileno() == -1
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as replacement:
                replacement.bind(("127.0.0.1", port))
            if fixed:
                following = p2p._FixedPortConnection(
                    bind_port=port, ice_controlling=True, stun_server=None,
                )
                try:
                    assert await following.get_component_candidates(1, ["127.0.0.1"])
                finally:
                    await p2p._close_connection(following)
        finally:
            # Keep a regression failure from leaking sockets into later tests.
            transport.abort()
            await asyncio.sleep(0)

    asyncio.run(scenario())


def test_successful_fixed_port_stream_keeps_tail_and_reuses_ports(monkeypatch):
    monkeypatch.setenv("RYNMESH_P2P_STUN", "off")
    monkeypatch.delenv("RYNMESH_P2P_REQUIRE_PUBLIC", raising=False)
    monkeypatch.delenv("RYNMESH_P2P_REQUIRE_DISTINCT_PUBLIC", raising=False)
    monkeypatch.setattr(aioice.ice, "get_host_addresses", lambda **kwargs: ["127.0.0.1"])
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as left:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as right:
            left.bind(("127.0.0.1", 0))
            right.bind(("127.0.0.1", 0))
            ports = {True: left.getsockname()[1], False: right.getsockname()[1]}
    monkeypatch.setenv("RYNMESH_P2P_BIND_PORT", str(ports[True]))
    connections = []

    def create(*, controlling):
        connection = p2p._FixedPortConnection(
            bind_port=ports[controlling], ice_controlling=controlling, stun_server=None,
        )
        connections.append(connection)
        return connection

    monkeypatch.setattr(p2p, "new_connection", create)

    async def scenario():
        for request_id in ["first", "following"]:
            provider_tasks = []
            events = []

            async def publish(offer, expected_id=request_id, tasks=provider_tasks):
                answer = asyncio.get_running_loop().create_future()

                def handle(request, emit):
                    assert request == {"id": expected_id}
                    for index in range(3):
                        emit({"payload": {"kind": "llm_stream"}, "index": index,
                              "text": "stream" * 1000})
                    return {"payload": {"kind": "llm_response"}, "id": expected_id,
                            "text": "terminal" * 1000}

                tasks.append(asyncio.create_task(p2p.provider_exchange(
                    offer=offer, publish_answer=answer.set_result,
                    handle_request=lambda _: pytest.fail("stream handler expected"),
                    handle_stream_request=handle, timeout_s=5,
                )))
                return await answer

            response, _ = await p2p.consumer_exchange(
                signed_request={"id": request_id}, publish_offer=publish,
                timeout_s=5, on_event=events.append,
            )
            await asyncio.gather(*provider_tasks)
            assert response == {"payload": {"kind": "llm_response"}, "id": request_id,
                                "text": "terminal" * 1000}
            assert events == [{"payload": {"kind": "llm_stream"}, "index": index,
                               "text": "stream" * 1000} for index in range(3)]
            for port in ports.values():
                with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as replacement:
                    replacement.bind(("127.0.0.1", port))

    try:
        asyncio.run(scenario())
    finally:
        for connection in connections:
            for protocol in connection._protocols:
                protocol.transport.abort()
