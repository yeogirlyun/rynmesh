#!/usr/bin/env python3
"""One-time, isolated HTTP acceptance. All temporary servers stop before exit.

Uses controlled model outputs to test mechanics; does not certify AI neutrality.
"""

from __future__ import annotations

import argparse
import json
import secrets
import socket
import tempfile
import threading
import time
import uuid
from pathlib import Path
from types import SimpleNamespace

import uvicorn
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey
from fastapi import FastAPI, HTTPException

from rynmesh.exchange.protocol import COVENANT, UNITS, VERSION
from rynmesh.exchange.routes import install_exchange
from rynmesh.exchange.service import Exchange
from rynmesh.peer_http import HttpPeerClient
from rynmesh.services.peer_box import public_key_b64


def new_id():
    return uuid.uuid4().hex


class ControlledModel:
    def __init__(self, model, calls):
        self.model, self.calls = model, calls

    def generate(self, prompt, *, system, max_tokens):
        self.calls.append(self.model)
        assert json.loads(prompt)["verified_delivery"] == "A private translated paragraph."
        return json.dumps(
            {"share_bps": 5000, "reason_code": "partial_delivery", "uncertain": False}
        )


def run(root: Path):
    nodes, sockets, servers, threads, endpoints, calls = [], [], [], [], [], []
    token = secrets.token_hex(32)
    try:
        for index in range(12):
            sock = socket.socket()
            sock.bind(("127.0.0.1", 0))
            sock.listen(128)
            sockets.append(sock)
            endpoint = f"http://127.0.0.1:{sock.getsockname()[1]}"
            endpoints.append(endpoint)
            node = Exchange(
                root / str(index),
                secrets.token_bytes(32),
                X25519PrivateKey.generate(),
                allow_loopback=True,
                model_factory=lambda name: ControlledModel(name, calls),
            )
            nodes.append(node)
            app = FastAPI()

            def guard(request):
                if request.headers.get("x-acceptance-owner") != token:
                    raise HTTPException(403)

            store = SimpleNamespace(
                home=root / str(index), private_key_bytes=node.signing_key, peer_id=node.peer_id
            )
            install_exchange(
                app, store=store, messaging_key=node.messaging_key, local_control=guard
            )
            app.state.exchange = node
            server = uvicorn.Server(
                uvicorn.Config(app, log_level="error", lifespan="off", access_log=False)
            )
            servers.append(server)
            thread = threading.Thread(target=server.run, kwargs={"sockets": [sock]})
            threads.append(thread)
            thread.start()
        deadline = time.monotonic() + 10
        while not all(server.started for server in servers):
            if time.monotonic() > deadline:
                raise RuntimeError("HTTP acceptance startup timed out")
            time.sleep(0.02)
        manifest = {
            "version": VERSION,
            "name": "Isolated HTTP acceptance",
            "validators": [
                {"peer_id": nodes[i].peer_id, "endpoint": endpoints[i]} for i in range(4)
            ],
            "judges": [
                {
                    "peer_id": nodes[i].peer_id,
                    "endpoint": endpoints[i],
                    "encryption_key": public_key_b64(nodes[i].messaging_key),
                    "model": f"controlled-model-{i}",
                    "fee": 1000,
                }
                for i in range(6, 12)
            ],
            "issuance_limit": 100 * UNITS,
            "work_reward": UNITS,
            "appeal_window_s": 60,
            "covenant": COVENANT,
        }

        def control(index, body):
            return HttpPeerClient(endpoints[index], timeout_s=90).post_json(
                "/api/local/exchange/action",
                body,
                headers={"x-acceptance-owner": token},
                max_bytes=3 * 1024 * 1024,
            )

        def action(index, name, value, op=None):
            return control(index, {"action": name, "value": value, "operation_id": op or new_id()})

        for index in range(12):
            control(index, {"action": "configure", "manifest": manifest})
            control(index, {"action": "options", "value": {"registry": True, "judge": True}})
        for index in (4, 5):
            action(index, "profile", {"label": f"Participant {index}"})
            action(index, "reward", {"job_id": new_id(), "endpoint": endpoints[index]})

        def order(buyer, provider, price, title):
            listing = new_id()
            action(
                buyer,
                "listing",
                {
                    "kind": "request",
                    "category": "digital work",
                    "title": title,
                    "description": "A small useful service.",
                    "price": price,
                },
                listing,
            )
            proposal = new_id()
            action(
                provider,
                "propose",
                {"listing_id": listing, "scope": "Deliver a checked paragraph.", "price": price},
                proposal,
            )
            status = control(buyer, {"action": "refresh"})
            terms = next(p for p in status["proposals"] if p["id"] == proposal)
            op = new_id()
            action(buyer, "agree", {"proposal_id": proposal, "terms_hash": terms["terms_hash"]}, op)
            action(provider, "deliver", {"order_id": op, "body": "A private translated paragraph."})
            control(buyer, {"action": "refresh"})
            assert (
                control(buyer, {"action": "delivery", "order_id": op})["body"]
                == "A private translated paragraph."
            )
            return op

        first = order(4, 5, "0.25", "Translate a paragraph")
        accepted = new_id()
        action(
            4,
            "accept",
            {
                "order_id": first,
                "delivery_hash": nodes[4].ledger.state["orders"][first]["delivery_hash"],
            },
            accepted,
        )
        height = nodes[4].ledger.height
        action(
            4,
            "accept",
            {
                "order_id": first,
                "delivery_hash": nodes[4].ledger.state["orders"][first]["delivery_hash"],
            },
            accepted,
        )
        assert nodes[4].ledger.height == height
        second = order(5, 4, "0.5", "Spend earnings on editing")
        action(
            5,
            "accept",
            {
                "order_id": second,
                "delivery_hash": nodes[5].ledger.state["orders"][second]["delivery_hash"],
            },
        )
        third = order(4, 5, "0.1", "Resolve partial delivery")
        action(4, "dispute", {"order_id": third})
        for index in (4, 5):
            action(
                index,
                "evidence",
                {"order_id": third, "body": "Only half the accepted scope was delivered."},
            )
        action(4, "rule", {"order_id": third})
        for index in (4, 5):
            action(index, "waive", {"order_id": third})
        action(4, "finalize", {"order_id": third})
        for index in range(12):
            control(index, {"action": "refresh"})
        state = nodes[4].ledger.state
        assert all(node.ledger.head == nodes[4].ledger.head for node in nodes)
        assert state["orders"][third]["paid"] == 50000
        assert sum(a["available"] + a["held"] for a in state["accounts"].values()) == 2 * UNITS
        for path in root.rglob("ledger.sqlite3*"):
            assert b"A private translated paragraph." not in path.read_bytes()
            assert b"Only half the accepted scope" not in path.read_bytes()
        return {
            "result": "passed",
            "transport": "real localhost HTTP through owner and peer routes",
            "nodes": len(nodes),
            "validator_quorum": "3 of 4",
            "judges": "3 controlled model adapters",
            "flow": [
                "free profiles",
                "independent registry verification",
                "internal issuance",
                "request",
                "proposal",
                "quoted agreement",
                "encrypted delivery",
                "acceptance",
                "idempotent retry",
                "spend earnings",
                "private evidence",
                "partial-price dispute",
                "waivers",
                "settlement",
            ],
            "issued_minor": state["issued"],
            "price_paid_on_dispute_minor": state["orders"][third]["paid"],
            "ledger_height": nodes[4].ledger.height,
            "all_replicas_agree": True,
            "private_plaintext_in_sqlite": False,
            "real_model_neutrality_evaluated": False,
        }
    finally:
        for server in servers:
            server.should_exit = True
        for thread in threads:
            thread.join(timeout=10)
        for sock in sockets:
            sock.close()
        for node in nodes:
            node.ledger.close()
        if any(thread.is_alive() for thread in threads):
            raise RuntimeError("Acceptance server failed to stop")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    with tempfile.TemporaryDirectory(prefix="rynmesh-exchange-acceptance-") as folder:
        result = run(Path(folder))
    output = json.dumps(result, indent=2)
    if args.output:
        args.output.write_text(output + "\n")
    print(output)


if __name__ == "__main__":
    main()
