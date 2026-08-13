"""Synthetic test/sim content generators.

Real, viewer-compatible bytes for the three content kinds the testbed
exercises: PNG image, WAV audio clip, Markdown document. Each is
deterministic per seed (reproducible runs), stdlib-only (no Pillow,
no codec), and small enough that hundreds of items fit comfortably in
a container's local store.

Not for production publishing — these are unambiguously synthetic
fillers for sim, testbed, and demo flows. Production content is the
node owner's actual work.
"""
from __future__ import annotations

import hashlib
import io
import math
import struct
import wave

from rynmesh.services.image import make_rgba_png

__all__ = [
    "image_bytes",
    "audio_bytes",
    "document_bytes",
]


# ---------------------------------------------------------------- image ----
def image_bytes(seed: str, width: int = 96, height: int = 96) -> bytes:
    """PNG keyed off seed: gradient with an inverted-color subject patch.

    Real RGBA-8 PNG; ~few-KB for the default 96x96. Deterministic.
    """
    width = max(8, min(2048, int(width)))
    height = max(8, min(2048, int(height)))
    h = hashlib.sha256(seed.encode("utf-8", errors="replace")).digest()
    fg = (h[0], h[1], h[2])
    bg = (h[3], h[4], h[5])
    rx = h[6] % max(1, width // 2)
    ry = h[7] % max(1, height // 2)
    rw = max(4, h[8] % max(5, width // 3))
    rh = max(4, h[9] % max(5, height // 3))
    pixels = bytearray()
    denom = max(1, (width - 1) + (height - 1))
    for y in range(height):
        for x in range(width):
            t = (x + y) / denom
            r = int(round(bg[0] * (1 - t) + fg[0] * t))
            g = int(round(bg[1] * (1 - t) + fg[1] * t))
            b = int(round(bg[2] * (1 - t) + fg[2] * t))
            if rx <= x < rx + rw and ry <= y < ry + rh:
                r, g, b = 255 - r, 255 - g, 255 - b
            pixels.extend((r, g, b, 255))
    return make_rgba_png(width, height, bytes(pixels))


# ---------------------------------------------------------------- audio ----
def audio_bytes(seed: str, duration_s: float = 1.0, sample_rate: int = 8000) -> bytes:
    """16-bit mono PCM WAV with a sine tone (frequency seeded) and an
    attack–decay envelope. Real WAV file; ~16KB at defaults. Plays in
    any audio app."""
    duration_s = max(0.1, min(10.0, float(duration_s)))
    sample_rate = max(4000, min(48000, int(sample_rate)))
    h = hashlib.sha256(seed.encode("utf-8", errors="replace")).digest()
    freq_hz = 220.0 + (h[0] % 8) * 55.0   # 220 .. 605 Hz
    n_samples = int(sample_rate * duration_s)
    frames = bytearray()
    for i in range(n_samples):
        t = i / sample_rate
        attack = min(1.0, t * 6.0)
        decay = max(0.0, 1.0 - (t / duration_s) ** 2)
        env = attack * decay
        val = int(round(32767 * 0.5 * env * math.sin(2 * math.pi * freq_hz * t)))
        if val > 32767:
            val = 32767
        if val < -32768:
            val = -32768
        frames.extend(struct.pack("<h", val))
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sample_rate)
        w.writeframes(bytes(frames))
    return buf.getvalue()


# ------------------------------------------------------------- document ----
_WORDS = (
    "agent", "mesh", "credit", "trust", "value", "content", "service",
    "provider", "consumer", "verify", "receipt", "propagate", "earn",
    "stake", "anchor", "explore", "weight", "carve", "saturate", "signal",
    "registry", "relay", "publish", "discover", "validation", "ranking",
    "reputation", "fairness", "decentralized", "peer",
)


def document_bytes(seed: str, paragraphs: int = 3) -> bytes:
    """Markdown document, multi-paragraph, deterministic from seed."""
    paragraphs = max(1, min(20, int(paragraphs)))
    h = hashlib.sha256(seed.encode("utf-8", errors="replace")).digest()
    title = " ".join(_WORDS[h[i] % len(_WORDS)].capitalize() for i in range(3))
    out: list[str] = [f"# {title}", "", f"seed: `{seed}`", ""]
    for p in range(paragraphs):
        ph = hashlib.sha256(h + bytes([p])).digest()
        word_count = 20 + (ph[0] % 30)
        words = [_WORDS[ph[(i + 1) % len(ph)] % len(_WORDS)] for i in range(word_count)]
        out.append(" ".join(words) + ".")
        out.append("")
    return "\n".join(out).encode("utf-8")
