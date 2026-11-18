"""Output purposes and exact pixel sizes.

Codex image generation always requests size "auto"; the model follows the
aspect ratio written in the brief (verified: a 9:16 brief returned 941x1672).
The final file is center-cropped to the exact ratio and resized to the target.
"""

from __future__ import annotations

import io
from math import gcd
from typing import Any

PURPOSES: dict[str, dict[str, Any]] = {
    "main": {"label": "商品主图", "ratio": "1:1", "size": (1024, 1024)},
    "poster": {"label": "海报", "ratio": "3:4", "size": (1024, 1365)},
    "story": {"label": "竖版", "ratio": "9:16", "size": (1024, 1820)},
    "banner": {"label": "横幅", "ratio": "16:9", "size": (1820, 1024)},
}
SCENE_SIZES: dict[str, tuple[tuple[int, int], ...]] = {
    "banner": ((1920, 600), (1200, 400), (750, 390), PURPOSES["banner"]["size"]),
    "main": (PURPOSES["main"]["size"], (800, 800)),
    "poster": (PURPOSES["poster"]["size"],),
    "social": ((1080, 1080), (1080, 1350), (1080, 1920), PURPOSES["story"]["size"]),
    "detail": ((750, 1000), PURPOSES["banner"]["size"]),
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
    if width > 2 * height:
        return "横版，宽高比 2:1"
    if height > 2 * width:
        return "竖版，宽高比 1:2"
    # The simplest ratio within 0.5 % (1024x1820 is 9:16, not 256:455).
    target = width / height
    divisor = gcd(width, height)
    candidates = ((round(target * d), d) for d in range(1, 21))
    a, b = next(((a, b) for a, b in candidates if abs(a / b - target) <= target * 0.005),
                (width // divisor, height // divisor))
    shape = "竖版" if height > width else "横版" if width > height else "正方形"
    return f"{shape}，宽高比 {a}:{b}（{width}×{height} 像素）"


def crop_safe_area(width: int, height: int) -> str | None:
    """The centered band retained from the requested 2:1 or 1:2 composition."""
    if width > 2 * height:
        percent = 200 * height / width
        axis = "高度"
        band = f"画面中间高度 {percent:.4g}% 的横条里，上下只放可以裁掉的背景"
    elif height > 2 * width:
        percent = 200 * width / height
        axis = "宽度"
        band = f"画面中间宽度 {percent:.4g}% 的竖条里，左右只放可以裁掉的背景"
    else:
        return None
    inset = percent * 0.65
    return (f"成品会居中裁成 {width}×{height} 像素，所有商品和文字必须完整放在{band}。"
            f"请主动缩小主体和文字，让完整轮廓（包括顶部和底部）及所有文字都放进居中的留边区域，"
            f"整体最多占生成画面{axis}的 {inset:.4g}%。")


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
