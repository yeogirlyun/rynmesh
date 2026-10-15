import asyncio
import time

import pytest

from rynmesh.llm_package import p2p
from rynmesh.llm_package.p2p_session import SessionChannel, SessionPool


class Wire:
    def __init__(self, latency=.0):
        self.queue = asyncio.Queue()
        self.other = None
        self.latency = latency
        self.drop_ack = False

    async def send(self, packet):
        kind = p2p._HEADER.unpack(packet[:p2p._HEADER.size])[1]
        if self.drop_ack and kind == p2p._ACK:
            self.drop_ack = False
            return
        asyncio.get_running_loop().call_later(self.latency, self.other.queue.put_nowait, packet)

    async def recv(self):
        return await self.queue.get()

    async def close(self):
        pass


def pair(latency=0):
    a, b = Wire(latency), Wire(latency)
    a.other, b.other = b, a
    return a, b


def test_pipeline_preserves_order_with_lost_ack_and_duplicate_frames():
    async def run():
        a, b = pair(.005)
        b.drop_ack = True
        left, right = SessionChannel(a), SessionChannel(b)
        try:
            senders = [asyncio.create_task(left.send({"n": n}, 2)) for n in range(8)]
            assert [await right.receive(2) for _ in range(8)] == [{"n": n} for n in range(8)]
            await asyncio.gather(*senders)
            assert right._incoming.empty()
            assert right._buffered == 0
            assert await left.probe()
        finally:
            await left.close()
            await right.close()
    asyncio.run(run())


def test_pipelining_reduces_round_trip_waits():
    async def run(parallel):
        a, b = pair(.015)
        left, right = SessionChannel(a), SessionChannel(b)
        async def consume():
            return [await right.receive(1) for _ in range(8)]
        try:
            started = time.monotonic()
            receiver = asyncio.create_task(consume())
            if parallel:
                await asyncio.gather(*(left.send({"n": n}, 2) for n in range(8)))
            else:
                for n in range(8):
                    await left.send({"n": n}, 2)
            duration = time.monotonic() - started
            assert await receiver == [{"n": n} for n in range(8)]
            return duration
        finally:
            await left.close()
            await right.close()
    serial = asyncio.run(run(False))
    pipelined = asyncio.run(run(True))
    assert pipelined < serial * .6


def test_real_ice_reused_across_request_event_loops(monkeypatch):
    monkeypatch.setenv("RYNMESH_P2P_STUN", "off")
    monkeypatch.delenv("RYNMESH_P2P_BIND_PORT", raising=False)
    monkeypatch.delenv("RYNMESH_P2P_REQUIRE_PUBLIC_NAT", raising=False)
    monkeypatch.delenv("RYNMESH_P2P_REQUIRE_DISTINCT_PUBLIC", raising=False)
    pool = SessionPool()
    offers = []
    received = []

    async def publish(offer):
        offers.append(offer)
        answer = asyncio.get_running_loop().create_future()

        def handler(body, emit):
            received.append(body["task"])
            for n in range(12):
                emit({"payload": {"kind": "llm_stream"}, "task": body["task"], "n": n})
            return {"payload": {"kind": "llm_response"}, "task": body["task"]}

        asyncio.create_task(p2p.provider_exchange(
            offer=offer, publish_answer=lambda value: answer.set_result(value),
            handle_request=handler, handle_stream_request=handler, timeout_s=5,
        ))
        return await answer

    try:
        for task in ["one", "two", "three"]:
            events = []
            response, evidence = asyncio.run(p2p.consumer_exchange(
                signed_request={"task": task}, publish_offer=publish, timeout_s=5,
                on_event=events.append, session_pool=pool, session_key="same-peer",
            ))
            assert response["task"] == task
            assert [e["n"] for e in events] == list(range(12))
            assert all(e["task"] == task for e in events)
            assert evidence["connection_reused"] == (task != "one")
        assert len(offers) == 1
        assert received == ["one", "two", "three"]
    finally:
        pool.close()


def test_failed_exchange_is_not_replayed():
    pool = SessionPool()
    invoked = []

    class Channel:
        async def close(self):
            pass

    async def factory():
        return Channel(), {}

    async def fail(*_):
        invoked.append(1)
        raise p2p.P2PError("already submitted")

    try:
        with pytest.raises(p2p.P2PError, match="already submitted"):
            asyncio.run(pool.run("peer", factory, fail))
        assert len(invoked) == 1
    finally:
        pool.close()


def test_slow_consumer_applies_backpressure_without_dropping_stream():
    async def run():
        a, b = pair()
        left, right = SessionChannel(a), SessionChannel(b)
        async def send():
            for start in range(0, 80, 8):
                await asyncio.gather(*(left.send({"n": n}, 2) for n in range(start, start + 8)))
        sender = asyncio.create_task(send())
        try:
            for n in range(80):
                await asyncio.sleep(.003)
                assert (await right.receive(1))["n"] == n
                assert right._incoming.qsize() <= 8
            await sender
            assert not right.closed
        finally:
            sender.cancel()
            await left.close()
            await right.close()
    asyncio.run(run())


def test_dead_cached_connection_is_rebuilt_before_sending_new_request():
    pool = SessionPool()
    built, delivered = [], []
    class Channel:
        last_used = 0
        closed = False
        async def probe(self): return False
        async def close(self): self.closed = True
    async def factory():
        channel = Channel()
        built.append(channel)
        return channel, {}
    async def exchange(channel, evidence):
        delivered.append(channel)
        return evidence
    try:
        for _ in range(2):
            assert not asyncio.run(pool.run("peer", factory, exchange))["connection_reused"]
        assert len(built) == len(delivered) == 2
        assert built[0].closed
    finally:
        pool.close()


def test_session_capacity_includes_connections_still_being_established():
    pool = SessionPool(max_sessions=1)
    async def run():
        entered = asyncio.Event()
        release = asyncio.Event()
        class Channel:
            last_used = 0
            async def close(self): pass
        async def factory():
            entered.set()
            await release.wait()
            return Channel(), {}
        async def exchange(*_): return True
        # Run the entire scenario on the owning loop, including its test events.
        first = asyncio.create_task(pool._run("one", factory, exchange))
        await entered.wait()
        with pytest.raises(p2p.P2PError, match="capacity"):
            await pool._run("two", factory, exchange)
        release.set()
        await first
    pool._start()
    try:
        asyncio.run_coroutine_threadsafe(run(), pool._loop).result(timeout=5)
    finally:
        pool.close()
