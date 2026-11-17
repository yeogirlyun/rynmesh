"""Output purposes and exact pixel sizes.

Codex image generation always requests size "auto"; the model follows the
aspect ratio written in the brief (verified: a 9:16 brief returned 941x1672).
The final file is center-cropped to the exact ratio and resized to the target.
"""

from __future__ import annotations

import io
from typing import Any

PURPOSES: dict[str, dict[str, Any]] = {
    "main": {"label": "商品主图", "ratio": "1:1", "size": (1024, 1024)},
    "poster": {"label": "海报", "ratio": "3:4", "size": (1024, 1365)},
    "story": {"label": "竖版", "ratio": "9:16", "size": (1024, 1820)},
    "banner": {"label": "横幅", "ratio": "16:9", "size": (1820, 1024)},
}
MIN_SIDE, MAX_SIDE = 256, 2048
THUMB_SIDE = 384


class SizeError(ValueError):
    pass


def resolve(purpose: Any, width: Any = None, height: Any = None) -> tuple[str, int, int]:
    """(purpose, width, height); ``custom`` takes explicit bounded pixels."""
    if purpose in PURPOSES:
        w, h = PURPOSES[purpose]["size"]
        return purpose, w, h
    if purpose == "custom" and type(width) is int and type(height) is int \
            and MIN_SIDE <= width <= MAX_SIDE and MIN_SIDE <= height <= MAX_SIDE:
        return "custom", width, height
    raise SizeError("image_bad_size")


def ratio_phrase(width: int, height: int) -> str:
    """Words the model follows for orientation and ratio."""
    # The simplest ratio within 0.5 % (1024x1820 is 9:16, not 256:455).
    target = width / height
    a, b = min(((max(1, round(target * d)), d) for d in range(1, 21)),
               key=lambda ab: (abs(ab[0] / ab[1] - target) / target > 0.005, ab[1]))
    shape = "竖版" if height > width else "横版" if width > height else "正方形"
    return f"{shape}，宽高比 {a}:{b}（{width}×{height} 像素）"


def fit(raw: bytes, width: int, height: int) -> bytes:
    """Center-crop to the exact ratio, resize to width x height, PNG."""
    from PIL import Image

    with Image.open(io.BytesIO(raw)) as img:
        img.load()
        src_w, src_h = img.size
        target = width / height
        if src_w / src_h > target:
            crop_w = round(src_h * target)
            left = (src_w - crop_w) // 2
            box = (left, 0, left + crop_w, src_h)
        else:
            crop_h = round(src_w / target)
            top = (src_h - crop_h) // 2
            box = (0, top, src_w, top + crop_h)
        mode = "RGBA" if img.mode in ("RGBA", "LA", "P") else "RGB"
        out = img.convert(mode).crop(box).resize((width, height), Image.LANCZOS)
        buffer = io.BytesIO()
        out.save(buffer, format="PNG", optimize=True)
        return buffer.getvalue()


def thumbnail(png: bytes) -> bytes:
    """Small JPEG for lists; the full PNG crosses the network only when opened."""
    from PIL import Image

    with Image.open(io.BytesIO(png)) as img:
        img.load()
        rgb = Image.new("RGB", img.size, (255, 255, 255))
        rgb.paste(img.convert("RGBA"), mask=img.convert("RGBA").split()[-1])
        rgb.thumbnail((THUMB_SIDE, THUMB_SIDE), Image.LANCZOS)
        buffer = io.BytesIO()
        rgb.save(buffer, format="JPEG", quality=82, optimize=True)
        return buffer.getvalue()


def check_upload(raw: bytes, max_bytes: int) -> str:
    """Validate a reference image by content; returns its MIME type."""
    from PIL import Image

    if not raw or len(raw) > max_bytes:
        raise SizeError("image_upload_too_large")
    kinds = {b"\x89PNG\r\n\x1a\n": "image/png", b"\xff\xd8\xff": "image/jpeg"}
    mime = next((m for sig, m in kinds.items() if raw.startswith(sig)), None)
    if mime is None and raw[:4] == b"RIFF" and raw[8:12] == b"WEBP":
        mime = "image/webp"
    if mime is None:
        raise SizeError("image_upload_type")
    try:
        with Image.open(io.BytesIO(raw)) as img:
            w, h = img.size
            if not (16 <= w <= 8192 and 16 <= h <= 8192) or w * h > 40_000_000:
                raise SizeError("image_upload_dimensions")
            img.verify()
    except SizeError:
        raise
    except Exception:
        raise SizeError("image_upload_type") from None
    return mime


def as_png(raw: bytes) -> bytes:
    """Reference images go to Codex as PNG data URLs."""
    from PIL import Image

    with Image.open(io.BytesIO(raw)) as img:
        img.load()
        if max(img.size) > 2048:
            img.thumbnail((2048, 2048), Image.LANCZOS)
        buffer = io.BytesIO()
        img.convert("RGBA" if img.mode in ("RGBA", "LA", "P") else "RGB").save(buffer, format="PNG")
        return buffer.getvalue()
