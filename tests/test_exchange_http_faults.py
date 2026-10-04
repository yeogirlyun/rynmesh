"""Foreground HTTP fault acceptance; every temporary server stops on exit."""

from __future__ import annotations

import asyncio
import secrets
import socket
import threading
import time
from types import SimpleNamespace

import uvicorn
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey
from fastapi import FastAPI
from starlette.responses import JSONResponse
from test_exchange import new_id

from rynmesh.exchange.protocol import COVENANT, VERSION
from rynmesh.exchange.routes import install_exchange
from rynmesh.exchange.service import Exchange
from rynmesh.peer_http import HttpPeerClient


def test_slow_unreachable_validator_does_not_block_three_healthy_votes(tmp_path):
    nodes, sockets, servers, threads, endpoints = [], [], [], [], []
    blackhole = threading.Event()
    try:
        for index in range(6):
            sock = socket.socket()
            sock.bind(("127.0.0.1", 0))
            sock.listen(128)
            sockets.append(sock)
            endpoints.append(f"http://127.0.0.1:{sock.getsockname()[1]}")
            node = Exchange(
                tmp_path / str(index),
                secrets.token_bytes(32),
                X25519PrivateKey.generate(),
                allow_loopback=True,
            )
            nodes.append(node)
            app = FastAPI()
            if index == 0:

                @app.middleware("http")
                async def delay_requests(request, call_next):
                    if blackhole.is_set():
                        # Exceeds the real four-second transport timeout, unlike instant OSError mocks.
                        await asyncio.sleep(4.5)
                        return JSONResponse({"detail": "blackholed"}, status_code=503)
                    return await call_next(request)

            install_exchange(
                app,
                store=SimpleNamespace(
                    home=tmp_path / str(index),
                    private_key_bytes=node.signing_key,
                    peer_id=node.peer_id,
                ),
                messaging_key=node.messaging_key,
                local_control=lambda request: None,
            )
            app.state.exchange = node
            server = uvicorn.Server(
                uvicorn.Config(app, log_level="error", access_log=False, lifespan="off")
            )
            servers.append(server)
            thread = threading.Thread(target=server.run, kwargs={"sockets": [sock]})
            threads.append(thread)
            thread.start()
        deadline = time.monotonic() + 10
        while not all(s.started for s in servers):
            if time.monotonic() >= deadline:
                raise AssertionError("HTTP fixtures did not start")
            time.sleep(0.01)
        config = {
            "version": VERSION,
            "name": "Slow peer acceptance",
            "validators": [
                {"peer_id": nodes[i].peer_id, "endpoint": endpoints[i]} for i in range(4)
            ],
            "judges": [],
            "issuance_limit": 1000000,
            "work_reward": 1000000,
            "appeal_window_s": 60,
            "covenant": COVENANT,
        }
        for node in nodes:
            node.configure(config)
        nodes[4].action("profile", {"label": "Buyer"}, new_id())
        nodes[5].action("profile", {"label": "Provider"}, new_id())
        assert nodes[4].ledger.height == 1
        nodes[4].sync()
        blackhole.set()
        op = new_id()
        started = time.monotonic()
        result = HttpPeerClient(endpoints[4], timeout_s=60).post_json(
            "/api/local/exchange/action",
            {
                "action": "listing",
                "value": {
                    "kind": "request",
                    "category": "written work",
                    "title": "Still available",
                    "description": "A slow nonproposer does not prevent quorum.",
                    "price": "0",
                },
                "operation_id": op,
                "actor": nodes[4].actor,
            },
            max_bytes=3 * 1024 * 1024,
        )
        assert result["committed"] is True and time.monotonic() - started < 40
        assert all(op in node.ledger.state["operations"] for node in nodes[1:5])
        assert all(node.ledger.height == 3 for node in nodes[1:5])
    finally:
        blackhole.clear()
        for server in servers:
            server.should_exit = True
        for thread in threads:
            thread.join(timeout=10)
        for node in nodes:
            node.ledger.close()
        for sock in sockets:
            sock.close()
        assert all(not thread.is_alive() for thread in threads)
