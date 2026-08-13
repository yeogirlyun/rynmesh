"""rynmesh-embeddings-worker — text embedding service.

Operation: rynmesh.embedding.compute
Params:    {"text": str, "dim": int = 256}
Result:    JSON-encoded in result message:
           {"vector": list[float], "dim": int, "backend": str, "model": str}

Default backend: deterministic stdlib hash-based vector, L2-normalized.
Real backend swap via RYNMESH_EMBED_BACKEND={sentence-transformers,openai,...}.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
from typing import Any

from ._base import ServiceWorker

CAPABILITY = "rynmesh.embedding.compute"
OPERATION = "rynmesh.embedding.compute"


def embed_stub(text: str, dim: int = 256) -> list[float]:
    """Deterministic L2-normalized vector keyed off the text — usable as a
    placeholder while real backends are absent. Same text -> same vector."""
    dim = max(16, min(2048, int(dim or 256)))
    raw: list[float] = []
    h = hashlib.sha256(text.encode("utf-8", errors="replace")).digest()
    counter = 0
    while len(raw) < dim:
        for i in range(0, len(h) - 1, 2):
            if len(raw) >= dim:
                break
            v = int.from_bytes(h[i:i + 2], "big")
            raw.append((v / 65535.0) * 2.0 - 1.0)
        counter += 1
        h = hashlib.sha256(h + counter.to_bytes(4, "big")).digest()
    norm = math.sqrt(sum(x * x for x in raw)) or 1.0
    return [x / norm for x in raw]


class EmbeddingsWorker(ServiceWorker):
    capability = CAPABILITY
    operation = OPERATION

    def execute(self, params: dict[str, Any]) -> dict[str, Any]:
        text = str(params.get("text", ""))
        dim = int(params.get("dim", 256) or 256)
        backend = os.environ.get("RYNMESH_EMBED_BACKEND", "stub").strip().lower()
        if backend == "stub":
            vec = embed_stub(text, dim=dim)
            model = "rynmesh.embed.stub.v0"
        else:
            raise RuntimeError(
                f"RYNMESH_EMBED_BACKEND={backend!r} not wired in stdlib build"
            )
        payload = {
            "vector": vec,
            "dim": len(vec),
            "backend": backend,
            "model": model,
        }
        return {"message": json.dumps(payload)}


def main(argv: list[str] | None = None) -> int:
    import argparse
    ap = argparse.ArgumentParser(description="rynmesh embeddings worker")
    ap.add_argument("--poll-interval-s", type=float, default=2.0)
    ap.add_argument("--network-id", default=os.environ.get("RYNMESH_NETWORK_ID", "rynmesh-main"))
    args = ap.parse_args(argv)
    EmbeddingsWorker().serve_forever(
        network_id=args.network_id, poll_interval_s=args.poll_interval_s,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
