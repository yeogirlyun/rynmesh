"""Unrelated packets cannot turn redundant ACKs into a retransmission storm."""

import asyncio
import time

import pytest

from rynmesh.llm_package import p2p


class Peer:
    def __init__(self):
        self.incoming = asyncio.Queue()
        self.sent = []

    async def send(self, packet):
        self.sent.append(packet)

    async def recv(self):
        return await self.incoming.get()


@pytest.mark.parametrize("unrelated", ["stale_ack", "invalid", "request_retransmit"])
def test_unrelated_packets_do_not_resend_current_message(unrelated):
    async def scenario():
        peer = Peer()
        request_id, request_frames = p2p._encode_frames(b'{"request":true}')
        original_send = peer.send

        async def send(packet):
            await original_send(packet)
            kind, mid, _, _, digest, _ = p2p._decode_header(packet)
            if kind != p2p._DATA or len(peer.sent) != 1:
                return
            noise = (b"invalid" if unrelated == "invalid" else request_frames[0]
                     if unrelated == "request_retransmit" else
                     p2p._HEADER.pack(p2p._MAGIC, p2p._ACK, request_id, 0, 0, digest))
            for _ in range(3):
                peer.incoming.put_nowait(noise)
            peer.incoming.put_nowait(p2p._HEADER.pack(p2p._MAGIC, p2p._ACK, mid, 0, 0, digest))

        peer.send = send
        await p2p.send_json(peer, {"response": True}, timeout_s=1, reack_message_id=request_id)
        kinds = [p2p._decode_header(packet)[0] for packet in peer.sent]
        assert kinds.count(p2p._DATA) == 1
        assert kinds.count(p2p._ACK) == (9 if unrelated == "request_retransmit" else 0)

    asyncio.run(scenario())


def test_missing_ack_still_retransmits_after_retry_window():
    async def scenario():
        peer = Peer()
        original_send = peer.send
        sent_at = []

        async def send(packet):
            await original_send(packet)
            sent_at.append(time.monotonic())
            if len(peer.sent) == 2:
                _, mid, _, _, digest, _ = p2p._decode_header(packet)
                peer.incoming.put_nowait(p2p._HEADER.pack(p2p._MAGIC, p2p._ACK, mid, 0, 0, digest))

        peer.send = send
        await p2p.send_json(peer, {"response": True}, timeout_s=3)
        assert len(peer.sent) == 2
        assert peer.sent[0] == peer.sent[1]
        assert sent_at[1] - sent_at[0] >= 1.4

    asyncio.run(scenario())


def test_continuous_stale_acks_do_not_extend_retry_or_total_deadline():
    async def scenario():
        peer = Peer()
        stale = p2p._HEADER.pack(p2p._MAGIC, p2p._ACK, b"s" * 16, 0, 0, b"d" * 32)

        async def recv():
            await asyncio.sleep(.02)
            return stale

        peer.recv = recv
        started = time.monotonic()
        with pytest.raises(p2p.P2PError, match="timed out waiting"):
            await asyncio.wait_for(p2p.send_json(peer, {"response": True}, timeout_s=1.7), 3)
        assert len(peer.sent) == 2
        assert time.monotonic() - started < 2.5

    asyncio.run(scenario())
