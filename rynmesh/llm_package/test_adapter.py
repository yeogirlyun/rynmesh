"""Deterministic OpenAI-compatible server for automated protocol tests only.

This server is deliberately labelled as a test adapter and must never be used
as evidence of non-mock inference. The real-inference Compose profile connects
the Provider to llama.cpp instead.
"""

from __future__ import annotations

import hashlib
import json
import os
import time
from typing import Any

from fastapi import FastAPI
from fastapi.responses import StreamingResponse

app = FastAPI(title="Rynmesh deterministic test LLM")


@app.get("/health")
def health() -> dict[str, Any]:
    return {"status": "ok", "test_only": True}


@app.get("/v1/models")
def models() -> dict[str, Any]:
    return {"object": "list", "data": [{"id": "rynmesh-test-adapter", "owned_by": "test-only"}]}


@app.post("/v1/chat/completions")
def complete(body: dict[str, Any]) -> Any:
    messages = body.get("messages") or []
    prompt = str(messages[-1].get("content") if messages else "")
    digest = hashlib.sha256(prompt.encode()).hexdigest()[:12]
    text = "rynmesh encrypted e2e ok " + digest
    usage = {
        "prompt_tokens": max(1, len(prompt) // 4),
        "completion_tokens": max(1, len(text) // 4),
        "total_tokens": max(2, len(prompt) // 4 + len(text) // 4),
    }
    streaming_enabled = os.environ.get("RYNMESH_TEST_ADAPTER_DISABLE_STREAM", "").strip().lower() \
        not in {"1", "true", "yes"}
    if body.get("stream") and streaming_enabled:
        parts = ("rynmesh encrypted ", "e2e ok ", digest)

        def events():
            for part in parts:
                event = {
                    "id": "test-" + digest,
                    "object": "chat.completion.chunk",
                    "model": "rynmesh-test-adapter",
                    "choices": [{"index": 0, "delta": {"content": part}}],
                }
                yield "data: " + json.dumps(event, separators=(",", ":")) + "\n\n"
                # Keep a measurable gap between first delta and terminal so the
                # two-node verifier proves live delivery rather than replay.
                time.sleep(0.25)
            yield "data: " + json.dumps(
                {"choices": [], "usage": usage}, separators=(",", ":"),
            ) + "\n\n"
            yield "data: [DONE]\n\n"

        return StreamingResponse(events(), media_type="text/event-stream")
    return {
        "id": "test-" + digest, "object": "chat.completion", "model": "rynmesh-test-adapter",
        "choices": [{"index": 0, "message": {"role": "assistant", "content": text},
                     "finish_reason": "stop"}],
        "usage": usage,
    }


def main() -> int:
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8080, log_level="warning")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
