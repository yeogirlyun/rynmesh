"""Create a public alpha network invitation; never launch nodes or mint funds."""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import sys
from pathlib import Path

from rynmesh.crypto import canonical_json, sha256_bytes
from rynmesh.friends.crypto import validate_endpoint

from .protocol import COVENANT, VERSION, amount, genesis, require, signature


def identity(endpoint: str, *, allow_loopback: bool):
    from rynmesh.peer_http import HttpPeerClient

    validate_endpoint(endpoint, allow_loopback=allow_loopback)
    secret = os.environ.get("RYNMESH_NETWORK_KEY", "").strip()
    headers = (
        {"x-ryn-auth": hashlib.sha256(("rynmesh-net-key:" + secret).encode()).hexdigest()}
        if secret
        else {}
    )
    signed = signature(
        HttpPeerClient(endpoint, timeout_s=5).post_json(
            "/api/peer/exchange/identity", {}, headers=headers, max_bytes=4096
        )
    )
    require(signed.payload.get("version") == VERSION and signed.payload.get("kind") == "identity")
    require(signed.public_key == signed.payload.get("peer_id"))
    return {
        "peer_id": signed.public_key,
        "endpoint": endpoint,
        "encryption_key": signed.payload["encryption_key"],
    }


def build(args):
    validators = []
    for endpoint in args.validator:
        peer = identity(endpoint, allow_loopback=args.allow_loopback)
        validators.append({k: peer[k] for k in ("peer_id", "endpoint")})
    judges = []
    for endpoint, model, fee in args.judge:
        peer = identity(endpoint, allow_loopback=args.allow_loopback)
        judges.append({**peer, "model": model, "fee": amount(fee)})
    return genesis(
        {
            "version": VERSION,
            "name": args.name,
            "validators": validators,
            "judges": judges,
            "issuance_limit": amount(args.issuance_limit),
            "work_reward": amount(args.work_reward),
            "appeal_window_s": args.appeal_window,
            "covenant": COVENANT,
        }
    )


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--name", required=True)
    parser.add_argument(
        "--validator",
        action="append",
        default=[],
        metavar="ENDPOINT",
        help="Four to eight distinct validator nodes, repeated in roster order",
    )
    parser.add_argument(
        "--judge",
        action="append",
        nargs=3,
        default=[],
        metavar=("ENDPOINT", "MODEL", "FEE"),
        help="Zero or six to twelve independent node identities and distinct exact Ollama model names; fee in Ryncoin",
    )
    parser.add_argument(
        "--issuance-limit", default="100", help="Published alpha issuance cap in Ryncoin"
    )
    parser.add_argument(
        "--work-reward", default="1", help="One-time verified registry registration reward"
    )
    parser.add_argument(
        "--appeal-window",
        type=int,
        default=86400,
        help="Seconds to appeal each decision, minimum 60",
    )
    parser.add_argument(
        "--allow-loopback", action="store_true", help="For isolated localhost acceptance only"
    )
    parser.add_argument(
        "--output",
        type=Path,
        required=True,
        help="New public JSON file, existing files are never overwritten",
    )
    args = parser.parse_args(argv)
    try:
        manifest = build(args)
        raw = canonical_json(manifest)
        with args.output.open("x", encoding="utf-8") as stream:
            stream.write(json.dumps(manifest, indent=2) + "\n")
        print("Verify these public identities with their operators before joining:")
        print(json.dumps(manifest, indent=2))
        print("Network fingerprint:", sha256_bytes(raw))
        print(
            "Public invitation: rynmesh-exchange://network/"
            + base64.urlsafe_b64encode(raw).decode()
        )
    except (OSError, ValueError, RuntimeError) as exc:
        print(f"Could not create network invitation: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
