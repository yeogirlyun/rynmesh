#!/usr/bin/env python3
"""LAN QA driver for a no-shared-filesystem Rynmesh mesh."""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from rynmesh.store import RynmeshStore  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description="Run Rynmesh LAN QA tasks.")
    subparsers = parser.add_subparsers(dest="command", required=True)

    publish = subparsers.add_parser("publish-sample", help="Publish local QA content.")
    publish.add_argument("--network-id", default="rynmesh-home-qa")
    publish.add_argument("--count", type=int, default=4)
    publish.add_argument("--category", default="qa")
    publish.add_argument("--register", action="store_true")

    mix = subparsers.add_parser("publish-mix", help="Publish a mix of synthetic image/audio/document content.")
    mix.add_argument("--network-id", default="rynmesh-home-qa")
    mix.add_argument("--images", type=int, default=2)
    mix.add_argument("--audios", type=int, default=2)
    mix.add_argument("--documents", type=int, default=2)
    mix.add_argument("--category", default="qa")
    mix.add_argument("--register", action="store_true")

    matrix = subparsers.add_parser("fetch-matrix", help="Discover peers and fetch sample content.")
    matrix.add_argument("--network-id", default="rynmesh-home-qa")
    matrix.add_argument("--max-age-hours", type=float, default=24.0)
    matrix.add_argument("--limit-per-peer", type=int, default=3)
    matrix.add_argument("--timeout-s", type=float, default=20.0)
    matrix.add_argument("--include-self", action="store_true")

    args = parser.parse_args()
    store = RynmeshStore()
    if args.command == "publish-sample":
        result = publish_sample(store, args)
    elif args.command == "publish-mix":
        result = publish_mix(store, args)
    else:
        result = fetch_matrix(store, args)
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


def publish_mix(store: RynmeshStore, args: argparse.Namespace) -> dict[str, Any]:
    from rynmesh.synthetic_content import audio_bytes, document_bytes, image_bytes

    generated_dir = store.home / "qa-generated" / store.node_name
    generated_dir.mkdir(parents=True, exist_ok=True)
    published: list[dict[str, Any]] = []
    spec = (
        ("image",    "image/png",     "png", image_bytes,    max(0, args.images)),
        ("audio",    "audio/wav",     "wav", audio_bytes,    max(0, args.audios)),
        ("document", "text/markdown", "md",  document_bytes, max(0, args.documents)),
    )
    ts = int(time.time())
    for kind, mime, ext, gen, count in spec:
        for i in range(count):
            seed = f"{store.node_name}:{kind}:{ts}:{i}"
            data = gen(seed)
            path = generated_dir / f"{store.node_name}-{kind}-{ts}-{i}.{ext}"
            path.write_bytes(data)
            published.append(
                store.publish_content(
                    path,
                    title=f"{store.node_name} {kind} sample {i}",
                    summary_text=f"safe synthetic {kind} content for testbed",
                    content_type=mime,
                    content_kind=kind,
                    category=args.category,
                    tags=("qa:mix", f"node:{store.node_name}", f"kind:{kind}"),
                )
            )
    registration = store.register_node(network_id=args.network_id) if args.register else None
    return {
        "node": store.node_info(),
        "network_id": args.network_id,
        "registration": registration,
        "mix_counts": {"images": args.images, "audios": args.audios, "documents": args.documents},
        "published": published,
    }


def publish_sample(store: RynmeshStore, args: argparse.Namespace) -> dict[str, Any]:
    generated_dir = store.home / "qa-generated" / store.node_name
    generated_dir.mkdir(parents=True, exist_ok=True)
    published: list[dict[str, Any]] = []
    for index in range(max(0, args.count)):
        path = generated_dir / f"{store.node_name}-{int(time.time())}-{index}.md"
        path.write_text(
            "\n".join(
                [
                    f"# Rynmesh QA sample {index}",
                    "",
                    f"node: {store.node_name}",
                    f"peer: {store.peer_id}",
                    f"created_at_unix: {time.time()}",
                    "",
                    "This file was generated for no-shared-filesystem LAN QA.",
                    "",
                ]
            ),
            encoding="utf-8",
        )
        published.append(
            store.publish_content(
                path,
                title=f"{store.node_name} QA sample {index}",
                summary_text="safe generated QA sample content",
                content_type="text/markdown",
                content_kind="document",
                category=args.category,
                tags=("qa:lan", f"node:{store.node_name}"),
            )
        )
    registration = store.register_node(network_id=args.network_id) if args.register else None
    return {
        "node": store.node_info(),
        "network_id": args.network_id,
        "registration": registration,
        "published": published,
    }


def fetch_matrix(store: RynmeshStore, args: argparse.Namespace) -> dict[str, Any]:
    discovered = store.discover_peers(
        network_id=args.network_id,
        include_self=args.include_self,
        max_age_hours=args.max_age_hours,
        use_cache_on_error=True,
    )
    attempts: list[dict[str, Any]] = []
    for peer in discovered["peers"]:
        endpoint = first_http_endpoint(peer.get("endpoints", []))
        if not endpoint:
            attempts.append({"peer_id": peer.get("peer_id", ""), "status": "no_http_endpoint"})
            continue
        try:
            listing = store.list_peer_content(endpoint, timeout_s=args.timeout_s)
            items = listing.get("content", [])[: max(0, args.limit_per_peer)]
            for item in items:
                content_id = str(item.get("content_id", "") or "")
                fetched = store.fetch_peer_content_full(
                    endpoint,
                    content_id,
                    expected_peer_id=str(peer.get("peer_id", "") or ""),
                    timeout_s=args.timeout_s,
                )
                attempts.append(
                    {
                        "peer_id": peer.get("peer_id", ""),
                        "endpoint": endpoint,
                        "content_id": content_id,
                        "status": "fetched",
                        "content_hash": fetched.get("content_hash", ""),
                        "content_path": fetched.get("content_path", ""),
                        "verified": True,
                    }
                )
        except Exception as exc:  # noqa: BLE001 - QA output should capture every failed peer.
            attempts.append(
                {
                    "peer_id": peer.get("peer_id", ""),
                    "endpoint": endpoint,
                    "status": "error",
                    "error": str(exc),
                }
            )
    return {
        "node": store.node_info(),
        "network_id": args.network_id,
        "discovery": discovered,
        "attempts": attempts,
        "fetched_count": sum(1 for item in attempts if item.get("status") == "fetched"),
    }


def first_http_endpoint(endpoints: list[str]) -> str:
    for endpoint in endpoints:
        if str(endpoint).startswith(("http://", "https://")):
            return str(endpoint)
    return ""


if __name__ == "__main__":
    raise SystemExit(main())
