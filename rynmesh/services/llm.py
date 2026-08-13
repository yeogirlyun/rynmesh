"""rynmesh-llm-worker — LLM-completion service.

Operation: rynmesh.llm.complete
Params:    {"prompt": str, "max_tokens": int = 64}
Result:    JSON-encoded in result message:
           {"text": str, "tokens": int, "backend": str, "model": str}

Default backend: deterministic stdlib stub keyed off the prompt hash. The
purpose of the stub is to exercise the protocol path (work order ->
provider -> signed result) without GPUs or API keys. Override via:

    RYNMESH_LLM_BACKEND=openai      # uses OPENAI_API_KEY + OPENAI_MODEL
    RYNMESH_LLM_BACKEND=ollama      # uses RYNMESH_LLM_OLLAMA_URL/MODEL
    RYNMESH_LLM_BACKEND=anthropic   # uses ANTHROPIC_API_KEY + model

Stub output is intentionally identifiable as a stub so a consumer agent
can tell the network has reached it but no real model is wired yet.
"""
from __future__ import annotations

import hashlib
import json
import os
from typing import Any

from ._base import ServiceWorker

CAPABILITY = "rynmesh.llm.complete"
OPERATION = "rynmesh.llm.complete"

_STUB_WORDS = (
    "agent", "mesh", "credit", "trust", "value", "content", "service",
    "provider", "consumer", "verify", "receipt", "propagate", "earn",
    "stake", "anchor", "explore", "weight", "carve", "saturate", "signal",
)


def llm_stub_complete(prompt: str, max_tokens: int = 64) -> str:
    n = max(8, min(256, int(max_tokens or 64)))
    h = hashlib.sha256(prompt.encode("utf-8", errors="replace")).digest()
    out: list[str] = []
    for i in range(n):
        out.append(_STUB_WORDS[h[i % len(h)] % len(_STUB_WORDS)])
        if i % 8 == 7:
            out.append(".")
        h = hashlib.sha256(h + bytes([i & 0xFF])).digest()
    return " ".join(out)


class LLMWorker(ServiceWorker):
    capability = CAPABILITY
    operation = OPERATION

    def execute(self, params: dict[str, Any]) -> dict[str, Any]:
        prompt = str(params.get("prompt", ""))
        max_tokens = int(params.get("max_tokens", 64) or 64)
        backend = os.environ.get("RYNMESH_LLM_BACKEND", "stub").strip().lower()
        if backend == "stub":
            text = llm_stub_complete(prompt, max_tokens=max_tokens)
            model = "rynmesh.llm.stub.v0"
        else:
            # Real backends are deliberately not implemented in stdlib; surface
            # a clear error rather than silently degrade.
            raise RuntimeError(
                f"RYNMESH_LLM_BACKEND={backend!r} not wired in stdlib build; "
                "install + import the backend in a downstream worker subclass"
            )
        payload = {
            "text": text,
            "prompt_echo": prompt[:200],
            "tokens": len(text.split()),
            "backend": backend,
            "model": model,
        }
        return {"message": json.dumps(payload)}


def main(argv: list[str] | None = None) -> int:
    import argparse
    ap = argparse.ArgumentParser(description="rynmesh LLM completion worker")
    ap.add_argument("--poll-interval-s", type=float, default=2.0)
    ap.add_argument("--network-id", default=os.environ.get("RYNMESH_NETWORK_ID", "rynmesh-main"))
    args = ap.parse_args(argv)
    LLMWorker().serve_forever(network_id=args.network_id, poll_interval_s=args.poll_interval_s)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
