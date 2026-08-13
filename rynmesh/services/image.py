"""rynmesh-image-worker — image generation service.

Operation: rynmesh.image.generate
Params:    {"prompt": str, "width": int = 64, "height": int = 64}
Result:    JSON-encoded message: {"png_b64": str, "width": int, "height": int,
           "backend": str, "model": str}.

Default backend produces a deterministic ~few-KB PNG (a diagonal gradient
keyed off the prompt hash) using stdlib zlib + struct — a real, valid
PNG file any viewer can open. The point is to exercise the protocol with
verifiable, content-addressed bytes; not to compete with diffusion models.
Override via RYNMESH_IMAGE_BACKEND={pillow,sd,openai,...}; for outputs
larger than a few KB the worker should upload to the relay and return
the content_hash in result_content_ids (TODO when a real backend lands).
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import struct
import zlib
from typing import Any

from ._base import ServiceWorker

CAPABILITY = "rynmesh.image.generate"
OPERATION = "rynmesh.image.generate"


def _png_chunk(tag: bytes, data: bytes) -> bytes:
    crc = zlib.crc32(tag + data) & 0xFFFFFFFF
    return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", crc)


def make_rgba_png(width: int, height: int, rgba: bytes) -> bytes:
    """Encode raw RGBA bytes (width*height*4) as a minimal valid PNG."""
    if len(rgba) != width * height * 4:
        raise ValueError("rgba length must equal width * height * 4")
    sig = b"\x89PNG\r\n\x1a\n"
    # IHDR: 8-bit RGBA, no interlace.
    ihdr = struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0)
    rows = []
    stride = width * 4
    for y in range(height):
        rows.append(b"\x00" + rgba[y * stride:(y + 1) * stride])
    idat = zlib.compress(b"".join(rows), level=6)
    return sig + _png_chunk(b"IHDR", ihdr) + _png_chunk(b"IDAT", idat) + _png_chunk(b"IEND", b"")


def stub_image(prompt: str, width: int = 64, height: int = 64) -> bytes:
    """Diagonal gradient between black and a prompt-keyed color. Tiny PNG."""
    width = max(8, min(1024, int(width or 64)))
    height = max(8, min(1024, int(height or 64)))
    h = hashlib.sha256(prompt.encode("utf-8", errors="replace")).digest()
    r0, g0, b0 = h[0], h[1], h[2]
    pixels = bytearray()
    denom = max(1, (width - 1) + (height - 1))
    for y in range(height):
        for x in range(width):
            t = (x + y) / denom
            pr = int(round(r0 * (1.0 - t) + 255 * t))
            pg = int(round(g0 * (1.0 - t) + 255 * t))
            pb = int(round(b0 * (1.0 - t) + 255 * t))
            pixels.extend((pr, pg, pb, 255))
    return make_rgba_png(width, height, bytes(pixels))


class ImageWorker(ServiceWorker):
    capability = CAPABILITY
    operation = OPERATION

    def execute(self, params: dict[str, Any]) -> dict[str, Any]:
        prompt = str(params.get("prompt", ""))
        width = int(params.get("width", 64) or 64)
        height = int(params.get("height", 64) or 64)
        backend = os.environ.get("RYNMESH_IMAGE_BACKEND", "stub").strip().lower()
        if backend == "stub":
            png = stub_image(prompt, width=width, height=height)
            model = "rynmesh.image.stub.v0"
        else:
            raise RuntimeError(
                f"RYNMESH_IMAGE_BACKEND={backend!r} not wired in stdlib build"
            )
        payload = {
            "png_b64": base64.b64encode(png).decode("ascii"),
            "width": width,
            "height": height,
            "bytes": len(png),
            "backend": backend,
            "model": model,
        }
        return {"message": json.dumps(payload)}


def main(argv: list[str] | None = None) -> int:
    import argparse
    ap = argparse.ArgumentParser(description="rynmesh image worker")
    ap.add_argument("--poll-interval-s", type=float, default=2.0)
    ap.add_argument("--network-id", default=os.environ.get("RYNMESH_NETWORK_ID", "rynmesh-main"))
    args = ap.parse_args(argv)
    ImageWorker().serve_forever(network_id=args.network_id, poll_interval_s=args.poll_interval_s)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
