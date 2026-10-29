"""Non-activating Windows desktop activity indicator, owned by the Ryn peer.

This is a display projection only: it never injects input or grants permission.
The transparent window is click-through, excluded from capture, and follows
the real task lifecycle (including pauses and errors).

Visual design follows the Stitch "Ryn Computer Use · HUD & Ambient Halo Design System":
- 44px feathered per-pixel alpha ambient screen-edge glow (no hard stepped lines).
- Top-center dark obsidian glassmorphic floating capsule HUD with pulsing status orb,
  brand title badge, phase-tinted active status chip, and pause hint.
"""
from __future__ import annotations

import math
import os
import threading
import time
from functools import lru_cache

from PIL import Image, ImageDraw, ImageFilter, ImageFont

HALO_BREADTH = 44
STRIP_SIDE_W = 52
STRIP_BOT_H = 52
STRIP_TOP_H = 116

ESCAPE_STOP_MESSAGE = (
    "Computer Use was stopped by the user with the physical Escape key. "
    "Stop your work, do not call further Computer Use tools in this turn, "
    "and send a final message noting that the user stopped Computer Use."
)

# Phase visual tokens aligned with the Stitch Design System:
# (primary_rgb, secondary_rgb, corner_rgb, chip_text_rgb, icon_kind)
PHASE_THEMES: dict[str, dict[str, object]] = {
    "正在查看屏幕": {
        "primary": (56, 189, 248),     # Electric Cyan #38BDF8
        "secondary": (245, 158, 11),   # Amber Gold #F59E0B
        "corner": (99, 102, 241),      # Indigo #6366F1
        "chip_text": (186, 230, 253),  # Crisp Ice Cyan #BAE6FD
        "icon": "eye",
    },
    "正在操作窗口": {
        "primary": (245, 158, 11),     # Amber Gold #F59E0B
        "secondary": (251, 146, 60),   # Coral Orange #FB923C
        "corner": (56, 189, 248),      # Electric Cyan #38BDF8
        "chip_text": (254, 240, 138),  # Crisp Bright Gold #FEF08A
        "icon": "cursor",
    },
    "正在思考下一步": {
        "primary": (168, 85, 247),     # Neural Violet #A855F7
        "secondary": (56, 189, 248),    # Electric Cyan #38BDF8
        "corner": (245, 158, 11),      # Amber Gold #F59E0B
        "chip_text": (243, 232, 255),  # Crisp Bright Lavender #F3E8FF
        "icon": "sparkle",
    },
}


def _resolve_phase_theme(phase: str) -> dict[str, object]:
    if phase in PHASE_THEMES:
        return PHASE_THEMES[phase]
    if "操作" in phase or "点击" in phase or "输入" in phase:
        return PHASE_THEMES["正在操作窗口"]
    if "思考" in phase or "规划" in phase:
        return PHASE_THEMES["正在思考下一步"]
    return PHASE_THEMES["正在查看屏幕"]


@lru_cache(maxsize=16)
def _load_font(size: int, bold: bool = False) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    candidates = (
        [
            "C:/Windows/Fonts/msyhbd.ttc",
            "C:/Windows/Fonts/segoeuib.ttf",
            "C:/Windows/Fonts/simhei.ttf",
        ]
        if bold
        else [
            "C:/Windows/Fonts/msyh.ttc",
            "C:/Windows/Fonts/segoeui.ttf",
            "C:/Windows/Fonts/simsun.ttc",
        ]
    )
    for path in candidates:
        if os.path.exists(path):
            try:
                return ImageFont.truetype(path, size)
            except OSError:
                continue
    return ImageFont.load_default()


def _measure_text(draw: ImageDraw.ImageDraw, text: str, font: ImageFont.ImageFont) -> tuple[int, int]:
    bbox = draw.textbbox((0, 0), text, font=font)
    return max(1, int(bbox[2] - bbox[0])), max(1, int(bbox[3] - bbox[1]))


def _draw_phase_icon(
    draw: ImageDraw.ImageDraw,
    kind: str,
    cx: float,
    cy: float,
    radius: float,
    color: tuple[int, int, int, int],
) -> None:
    if kind == "eye":
        # Smooth almond contour + pupil circle
        w = radius * 1.15
        h = radius * 0.72
        steps = 20
        upper = [
            (
                cx - w + (2.0 * w * i / steps),
                cy - h * math.sin(math.pi * i / steps),
            )
            for i in range(steps + 1)
        ]
        lower = [
            (
                cx + w - (2.0 * w * i / steps),
                cy + h * math.sin(math.pi * i / steps),
            )
            for i in range(1, steps)
        ]
        pts = upper + lower + [upper[0]]
        draw.line(pts, fill=color, width=max(2, int(radius * 0.24)), joint="curve")
        pr = radius * 0.34
        draw.ellipse([cx - pr, cy - pr, cx + pr, cy + pr], fill=color)
    elif kind == "cursor":
        # Precision actuation pointer arrow
        r = radius * 0.95
        pts = [
            (cx - r * 0.65, cy - r * 0.85),
            (cx + r * 0.85, cy - r * 0.10),
            (cx + r * 0.08, cy + r * 0.14),
            (cx - r * 0.15, cy + r * 0.90),
        ]
        draw.polygon(pts, fill=color)
    else:
        # 4-point neural cognition sparkle star
        r_out = radius * 1.0
        r_in = radius * 0.32
        pts = []
        for idx in range(8):
            ang = idx * (math.pi / 4.0) - math.pi / 2.0
            rad = r_out if idx % 2 == 0 else r_in
            pts.append((cx + math.cos(ang) * rad, cy + math.sin(ang) * rad))
        draw.polygon(pts, fill=color)


@lru_cache(maxsize=24)
def render_hud_capsule(phase: str, pulse_step: int = 0) -> Image.Image:
    """Render the Codex-style high-clarity floating HUD capsule.

    Vector geometry (capsule body, outer glow, status orb, phase chip, 3D Esc keycap)
    is rendered at 2x supersampling and downsampled to 1x for smooth anti-aliased edges,
    while TrueType CJK/Latin typography is rasterized directly at native 1x resolution
    for maximum pixel-grid sharpness and contrast.
    """
    theme = _resolve_phase_theme(phase)
    pri_r, pri_g, pri_b = theme["primary"]  # type: ignore[misc]
    chip_r, chip_g, chip_b = theme["chip_text"]  # type: ignore[misc]
    icon_kind = str(theme["icon"])

    # Native 1x bold fonts for crisp pixel-grid-aligned CJK and keycap rendering
    font_title = _load_font(17, bold=True)
    font_chip = _load_font(15, bold=True)
    font_kbd = _load_font(14, bold=True)
    font_hint = _load_font(16, bold=True)

    title_text = "Ryn 正在控制电脑"
    chip_text = phase or "正在查看屏幕"
    kbd_text = "Esc"
    hint_text = "退出控制"

    dummy = Image.new("RGBA", (8, 8), (0, 0, 0, 0))
    ddraw = ImageDraw.Draw(dummy)
    title_w, _ = _measure_text(ddraw, title_text, font_title)
    chip_text_w, _ = _measure_text(ddraw, chip_text, font_chip)
    kbd_w, _ = _measure_text(ddraw, kbd_text, font_kbd)
    hint_w, _ = _measure_text(ddraw, hint_text, font_hint)

    # 1x layout metrics (generous spacing, zero clutter, ultra-high legibility)
    pad_x = 22
    orb_slot_w = 22
    gap_sm = 10
    gap_md = 14
    chip_icon_w = 18
    chip_pad_x = 13
    chip_box_w = chip_pad_x * 2 + chip_icon_w + 6 + chip_text_w
    divider_gap = 16
    kbd_pad_x = 10
    kbd_box_w = max(44, kbd_w + kbd_pad_x * 2)

    pill_w_1x = (
        pad_x
        + orb_slot_w
        + gap_sm
        + title_w
        + gap_md
        + chip_box_w
        + divider_gap
        + 2
        + divider_gap
        + kbd_box_w
        + gap_sm
        + hint_w
        + pad_x
    )
    pill_h_1x = 54
    margin_x_1x = 24
    margin_top_1x = 12
    margin_bot_1x = 30

    out_w = pill_w_1x + margin_x_1x * 2
    out_h = pill_h_1x + margin_top_1x + margin_bot_1x

    # 2x supersampled vector canvas for smooth curves, shadows, and keycap bevels
    scale = 2
    canvas_w = out_w * scale
    canvas_h = out_h * scale
    pill_x0 = margin_x_1x * scale
    pill_y0 = margin_top_1x * scale
    pill_w = pill_w_1x * scale
    pill_h = pill_h_1x * scale
    pill_x1 = pill_x0 + pill_w
    pill_y1 = pill_y0 + pill_h
    pill_radius = pill_h // 2

    # 1. Outer ambient phase glow + deep elevation shadow
    shadow_layer = Image.new("RGBA", (canvas_w, canvas_h), (0, 0, 0, 0))
    sdraw = ImageDraw.Draw(shadow_layer)
    sdraw.rounded_rectangle(
        [pill_x0 + 2 * scale, pill_y0 + 6 * scale, pill_x1 - 2 * scale, pill_y1 + 9 * scale],
        radius=pill_radius,
        fill=(0, 0, 0, 195),
    )
    pulse_norm = 0.5 + 0.5 * math.sin(pulse_step * (math.pi / 4.0))
    glow_alpha = int(84 + 32 * pulse_norm)
    sdraw.rounded_rectangle(
        [pill_x0 - 2 * scale, pill_y0 - 2 * scale, pill_x1 + 2 * scale, pill_y1 + 2 * scale],
        radius=pill_radius + 2 * scale,
        outline=(pri_r, pri_g, pri_b, glow_alpha),
        width=5 * scale,
    )
    shadow_layer = shadow_layer.filter(ImageFilter.GaussianBlur(radius=8 * scale))

    # 2. High-opacity deep obsidian capsule body (97% opaque so background never muddies text)
    body_layer = Image.new("RGBA", (canvas_w, canvas_h), (0, 0, 0, 0))
    bdraw = ImageDraw.Draw(body_layer)
    bdraw.rounded_rectangle(
        [pill_x0, pill_y0, pill_x1, pill_y1],
        radius=pill_radius,
        fill=(8, 12, 20, 248),
    )

    # Subtle top-left phase sheen inside the capsule
    sheen = Image.new("RGBA", (canvas_w, canvas_h), (0, 0, 0, 0))
    sh_draw = ImageDraw.Draw(sheen)
    sh_draw.ellipse(
        [
            pill_x0 - 16 * scale,
            pill_y0 - 16 * scale,
            pill_x0 + int(pill_w * 0.45),
            pill_y1 + 16 * scale,
        ],
        fill=(pri_r, pri_g, pri_b, 28),
    )
    sheen = sheen.filter(ImageFilter.GaussianBlur(radius=12 * scale))
    mask = Image.new("L", (canvas_w, canvas_h), 0)
    ImageDraw.Draw(mask).rounded_rectangle(
        [pill_x0, pill_y0, pill_x1, pill_y1],
        radius=pill_radius,
        fill=255,
    )
    body_layer = Image.composite(Image.alpha_composite(body_layer, sheen), body_layer, mask)

    bdraw = ImageDraw.Draw(body_layer)
    # Inner specular highlight rim
    bdraw.rounded_rectangle(
        [pill_x0 + 1 * scale, pill_y0 + 1 * scale, pill_x1 - 1 * scale, pill_y1 - 1 * scale],
        radius=pill_radius - 1 * scale,
        outline=(255, 255, 255, 48),
        width=1 * scale,
    )
    # Crisp outer phase-tinted luminous border
    rim_alpha = int(165 + 40 * pulse_norm)
    bdraw.rounded_rectangle(
        [pill_x0, pill_y0, pill_x1, pill_y1],
        radius=pill_radius,
        outline=(pri_r, pri_g, pri_b, rim_alpha),
        width=2 * scale,
    )

    canvas_2x = Image.alpha_composite(shadow_layer, body_layer)
    draw_2x = ImageDraw.Draw(canvas_2x)

    cy_1x = margin_top_1x + pill_h_1x / 2.0
    cy_2x = cy_1x * scale
    x_1x = float(margin_x_1x + pad_x)

    # 3. Left: Pulsing concentric status orb (2x vector)
    orb_cx_1x = x_1x + orb_slot_w / 2.0
    orb_cx_2x = orb_cx_1x * scale
    ripple_r = (8.5 + 3.2 * pulse_norm) * scale
    ripple_alpha = int(95 * (1.0 - 0.40 * pulse_norm))
    draw_2x.ellipse(
        [orb_cx_2x - ripple_r, cy_2x - ripple_r, orb_cx_2x + ripple_r, cy_2x + ripple_r],
        fill=(pri_r, pri_g, pri_b, ripple_alpha),
        outline=(pri_r, pri_g, pri_b, min(255, ripple_alpha + 55)),
        width=1 * scale,
    )
    mid_r = 6.2 * scale
    draw_2x.ellipse(
        [orb_cx_2x - mid_r, cy_2x - mid_r, orb_cx_2x + mid_r, cy_2x + mid_r],
        fill=(pri_r, pri_g, pri_b, 165),
    )
    core_r = 4.6 * scale
    draw_2x.ellipse(
        [orb_cx_2x - core_r, cy_2x - core_r, orb_cx_2x + core_r, cy_2x + core_r],
        fill=(pri_r, pri_g, pri_b, 255),
    )
    spec_r = 1.6 * scale
    draw_2x.ellipse(
        [
            orb_cx_2x - spec_r - 0.9 * scale,
            cy_2x - spec_r - 0.9 * scale,
            orb_cx_2x + spec_r - 0.9 * scale,
            cy_2x + spec_r - 0.9 * scale,
        ],
        fill=(255, 255, 255, 245),
    )
    x_1x += orb_slot_w + gap_sm

    # Record 1x position for primary title ("Ryn 正在控制电脑")
    title_x_1x = x_1x
    x_1x += title_w + gap_md

    # 4. Active Phase Pill Box + Phase Icon (2x vector)
    chip_x_1x = x_1x
    chip_h_1x = 32
    chip_y0_2x = (cy_1x - chip_h_1x / 2.0) * scale
    chip_y1_2x = (cy_1x + chip_h_1x / 2.0) * scale
    draw_2x.rounded_rectangle(
        [
            chip_x_1x * scale,
            chip_y0_2x,
            (chip_x_1x + chip_box_w) * scale,
            chip_y1_2x,
        ],
        radius=(chip_h_1x * scale) // 2,
        fill=(int(pri_r * 0.22), int(pri_g * 0.22), int(pri_b * 0.22), 240),
        outline=(pri_r, pri_g, pri_b, 175),
        width=int(1.5 * scale),
    )
    icon_cx_2x = (chip_x_1x + chip_pad_x + chip_icon_w / 2.0) * scale
    _draw_phase_icon(draw_2x, icon_kind, icon_cx_2x, cy_2x, 6.2 * scale, (chip_r, chip_g, chip_b, 255))
    chip_text_x_1x = chip_x_1x + chip_pad_x + chip_icon_w + 6
    x_1x += chip_box_w + divider_gap

    # 5. Crisp Vertical Divider (2x vector)
    div_x_2x = (x_1x + 1.0) * scale
    div_h_2x = 24 * scale
    draw_2x.line(
        [(div_x_2x, cy_2x - div_h_2x / 2.0), (div_x_2x, cy_2x + div_h_2x / 2.0)],
        fill=(255, 255, 255, 76),
        width=int(1.5 * scale),
    )
    x_1x += 2 + divider_gap

    # 6. Right: Codex-style 3D [Esc] physical keycap badge (2x vector)
    kbd_x_1x = x_1x
    kbd_h_1x = 30
    kbd_x0_2x = kbd_x_1x * scale
    kbd_x1_2x = (kbd_x_1x + kbd_box_w) * scale
    kbd_y0_2x = (cy_1x - kbd_h_1x / 2.0) * scale
    kbd_y1_2x = (cy_1x + kbd_h_1x / 2.0) * scale
    kbd_rad_2x = 7 * scale
    # Keycap bottom 3D bevel shadow
    draw_2x.rounded_rectangle(
        [kbd_x0_2x, kbd_y0_2x + 2 * scale, kbd_x1_2x, kbd_y1_2x + 2 * scale],
        radius=kbd_rad_2x,
        fill=(2, 6, 14, 240),
    )
    # Keycap main surface
    draw_2x.rounded_rectangle(
        [kbd_x0_2x, kbd_y0_2x, kbd_x1_2x, kbd_y1_2x],
        radius=kbd_rad_2x,
        fill=(30, 41, 59, 252),
        outline=(148, 163, 184, 225),
        width=int(1.5 * scale),
    )
    # Keycap top specular inner edge
    draw_2x.line(
        [
            (kbd_x0_2x + 5 * scale, kbd_y0_2x + 1.5 * scale),
            (kbd_x1_2x - 5 * scale, kbd_y0_2x + 1.5 * scale),
        ],
        fill=(255, 255, 255, 95),
        width=1 * scale,
    )
    kbd_center_x_1x = kbd_x_1x + kbd_box_w / 2.0
    x_1x += kbd_box_w + gap_sm
    hint_x_1x = x_1x

    # Downsample vector shapes from 2x -> 1x
    out_img = canvas_2x.resize((out_w, out_h), Image.Resampling.LANCZOS)
    draw_1x = ImageDraw.Draw(out_img)

    # 7. Render all text directly at native 1x resolution with 1px dark shadow for maximum sharpness
    def _draw_crisp_text(
        pos: tuple[float, float],
        text: str,
        font: ImageFont.ImageFont,
        fill: tuple[int, int, int, int],
        anchor: str = "lm",
    ) -> None:
        px, py = round(pos[0]), round(pos[1])
        draw_1x.text((px, py + 1), text, font=font, fill=(0, 0, 0, 215), anchor=anchor)
        draw_1x.text((px, py), text, font=font, fill=fill, anchor=anchor)

    _draw_crisp_text(
        (title_x_1x, cy_1x),
        title_text,
        font_title,
        (255, 255, 255, 255),
        anchor="lm",
    )
    _draw_crisp_text(
        (chip_text_x_1x, cy_1x),
        chip_text,
        font_chip,
        (chip_r, chip_g, chip_b, 255),
        anchor="lm",
    )
    _draw_crisp_text(
        (kbd_center_x_1x, cy_1x),
        kbd_text,
        font_kbd,
        (255, 255, 255, 255),
        anchor="mm",
    )
    _draw_crisp_text(
        (hint_x_1x, cy_1x),
        hint_text,
        font_hint,
        (248, 250, 252, 255),
        anchor="lm",
    )

    return out_img


def _halo_alpha_lut(breadth: int = HALO_BREADTH, max_alpha: int = 88) -> list[int]:
    """Smooth cubic-bezier + exponential inward falloff LUT (0px -> breadth px)."""
    lut: list[int] = []
    for d in range(breadth):
        t = 1.0 - (d / float(breadth))
        # Smooth cubic-bezier falloff with extra soft core near d=0..10
        aura = (t ** 2.25) * (3.0 - 2.0 * t)
        rim = 0.32 * ((max(0.0, 1.0 - d / 11.0)) ** 2.0)
        val = min(1.0, (aura * 0.76) + rim)
        lut.append(max(0, min(255, int(round(max_alpha * val)))))
    return lut


@lru_cache(maxsize=16)
def _build_halo_strips(
    width: int,
    height: int,
    phase: str,
    pulse_step: int = 0,
) -> tuple[Image.Image, Image.Image, Image.Image, Image.Image]:
    """Build (top_strip, bottom_strip, left_strip, right_strip) with 44px feathered glow."""
    theme = _resolve_phase_theme(phase)
    pri = theme["primary"]      # type: ignore[assignment]
    sec = theme["secondary"]    # type: ignore[assignment]
    cor = theme["corner"]       # type: ignore[assignment]

    pulse_norm = 0.5 + 0.5 * math.sin(pulse_step * (math.pi / 4.0))
    max_alpha = int(78 + 18 * pulse_norm)
    lut = _halo_alpha_lut(HALO_BREADTH, max_alpha=max_alpha)

    top_h = min(STRIP_TOP_H, height // 2)
    bot_h = min(STRIP_BOT_H, height - top_h)
    mid_h = max(1, height - top_h - bot_h)
    side_w = min(STRIP_SIDE_W, width // 2)

    # Create smooth horizontal spectrum gradient across screen width (corner -> sec -> pri -> sec -> corner)
    horiz_stops = [cor, sec, pri, sec, cor]
    horiz_small = Image.new("RGB", (64, 1))
    for x in range(64):
        pos = (x / 63.0) * (len(horiz_stops) - 1)
        idx = min(len(horiz_stops) - 2, int(pos))
        frac = pos - idx
        c0 = horiz_stops[idx]
        c1 = horiz_stops[idx + 1]
        r = int(c0[0] + (c1[0] - c0[0]) * frac)
        g = int(c0[1] + (c1[1] - c0[1]) * frac)
        b = int(c0[2] + (c1[2] - c0[2]) * frac)
        horiz_small.putpixel((x, 0), (r, g, b))

    vert_stops = [cor, sec, pri, sec, cor]
    vert_small = Image.new("RGB", (1, 64))
    for y in range(64):
        pos = (y / 63.0) * (len(vert_stops) - 1)
        idx = min(len(vert_stops) - 2, int(pos))
        frac = pos - idx
        c0 = vert_stops[idx]
        c1 = vert_stops[idx + 1]
        r = int(c0[0] + (c1[0] - c0[0]) * frac)
        g = int(c0[1] + (c1[1] - c0[1]) * frac)
        b = int(c0[2] + (c1[2] - c0[2]) * frac)
        vert_small.putpixel((0, y), (r, g, b))

    # 1D vertical alpha profile for top and bottom strips
    top_alpha_1d = Image.new("L", (1, top_h), 0)
    for y in range(min(HALO_BREADTH, top_h)):
        top_alpha_1d.putpixel((0, y), lut[y])
    top_alpha = top_alpha_1d.resize((width, top_h), Image.Resampling.NEAREST)

    bot_alpha_1d = Image.new("L", (1, bot_h), 0)
    for d in range(min(HALO_BREADTH, bot_h)):
        bot_alpha_1d.putpixel((0, bot_h - 1 - d), lut[d])
    bot_alpha = bot_alpha_1d.resize((width, bot_h), Image.Resampling.NEAREST)

    # 1D horizontal alpha profile for left and right strips
    left_alpha_1d = Image.new("L", (side_w, 1), 0)
    for x in range(min(HALO_BREADTH, side_w)):
        left_alpha_1d.putpixel((x, 0), lut[x])

    right_alpha_1d = Image.new("L", (side_w, 1), 0)
    for d in range(min(HALO_BREADTH, side_w)):
        right_alpha_1d.putpixel((side_w - 1 - d, 0), lut[d])

    # Smooth C-infinity corner union: alpha_norm(x, y) = 1 - (1 - u(x)) * (1 - v(y))
    # Eliminates any 45-degree diagonal miter line at the 4 corners.
    def _corner_patch(w_patch: int, h_patch: int, flip_x: bool, flip_y: bool) -> Image.Image:
        patch = Image.new("L", (w_patch, h_patch), 0)
        px = patch.load()
        for yy in range(h_patch):
            dy = (h_patch - 1 - yy) if flip_y else yy
            vy = (lut[dy] / float(max(1, max_alpha))) if dy < HALO_BREADTH else 0.0
            for xx in range(w_patch):
                dx = (w_patch - 1 - xx) if flip_x else xx
                ux = (lut[dx] / float(max(1, max_alpha))) if dx < HALO_BREADTH else 0.0
                combined = 1.0 - (1.0 - ux) * (1.0 - vy)
                px[xx, yy] = max(0, min(255, int(round(max_alpha * combined))))
        return patch

    tl_corner = _corner_patch(side_w, top_h, flip_x=False, flip_y=False)
    tr_corner = _corner_patch(side_w, top_h, flip_x=True, flip_y=False)
    bl_corner = _corner_patch(side_w, bot_h, flip_x=False, flip_y=True)
    br_corner = _corner_patch(side_w, bot_h, flip_x=True, flip_y=True)

    top_alpha.paste(tl_corner, (0, 0))
    top_alpha.paste(tr_corner, (width - side_w, 0))
    bot_alpha.paste(bl_corner, (0, 0))
    bot_alpha.paste(br_corner, (width - side_w, 0))

    # Top strip RGB + top-center ambient flare behind HUD capsule
    top_rgb = horiz_small.resize((width, top_h), Image.Resampling.BILINEAR)
    top_strip = top_rgb.convert("RGBA")
    top_strip.putalpha(top_alpha)

    flare_w = min(width, 680)
    flare = Image.new("RGBA", (flare_w, top_h), (0, 0, 0, 0))
    fdraw = ImageDraw.Draw(flare)
    fdraw.ellipse(
        [int(flare_w * 0.12), -28, int(flare_w * 0.88), int(top_h * 0.78)],
        fill=(pri[0], pri[1], pri[2], int(52 + 16 * pulse_norm)),
    )
    flare = flare.filter(ImageFilter.GaussianBlur(radius=14))
    flare_x = (width - flare_w) // 2
    top_strip.alpha_composite(flare, (flare_x, 0))

    # Composite the Floating Glass Capsule HUD onto top_strip
    hud = render_hud_capsule(phase, pulse_step=pulse_step)
    hud_x = (width - hud.width) // 2
    hud_y = 2
    if hud_x >= 0 and hud_y + hud.height <= top_h:
        top_strip.alpha_composite(hud, (hud_x, hud_y))
    else:
        top_strip.alpha_composite(
            hud.crop((0, 0, min(hud.width, width), min(hud.height, top_h - hud_y))),
            (max(0, hud_x), hud_y),
        )

    # Bottom strip
    bot_rgb = horiz_small.resize((width, bot_h), Image.Resampling.BILINEAR)
    bot_strip = bot_rgb.convert("RGBA")
    bot_strip.putalpha(bot_alpha)

    # Left & Right strips
    side_rgb = vert_small.resize((side_w, mid_h), Image.Resampling.BILINEAR)
    left_strip = side_rgb.convert("RGBA")
    left_strip.putalpha(left_alpha_1d.resize((side_w, mid_h), Image.Resampling.NEAREST))

    right_strip = side_rgb.convert("RGBA")
    right_strip.putalpha(right_alpha_1d.resize((side_w, mid_h), Image.Resampling.NEAREST))

    return top_strip, bot_strip, left_strip, right_strip


def render_overlay_frame(
    width: int = 1920,
    height: int = 1080,
    phase: str = "正在查看屏幕",
    pulse_step: int = 0,
    backdrop: Image.Image | None = None,
) -> Image.Image:
    """Assemble a full-resolution RGBA frame of the Computer Use overlay (for previews & tests)."""
    base = (
        backdrop.convert("RGBA").resize((width, height), Image.Resampling.LANCZOS)
        if backdrop is not None
        else Image.new("RGBA", (width, height), (0, 0, 0, 0))
    )
    top_s, bot_s, left_s, right_s = _build_halo_strips(width, height, phase, pulse_step=pulse_step)
    base.alpha_composite(top_s, (0, 0))
    base.alpha_composite(bot_s, (0, height - bot_s.height))
    base.alpha_composite(left_s, (0, top_s.height))
    base.alpha_composite(right_s, (width - right_s.width, top_s.height))
    return base


class ComputerOverlay:
    def __init__(self, on_escape=None):
        self._lock = threading.Lock()
        self._tasks = {}
        self._thread = None
        self._closed = threading.Event()
        self.error = None
        self.on_escape = on_escape
        self._escape_count = 0
        self._last_escaped_tasks: list[str] = []
        self._suppress_escape_until = 0.0
        self._poll_escape_in_tests = False

    def suppress_escape(self, duration: float = 0.8) -> None:
        """Temporarily suppress physical Escape detection while Ryn itself injects an Escape key."""
        with self._lock:
            until = time.monotonic() + max(0.0, float(duration))
            if until > self._suppress_escape_until:
                self._suppress_escape_until = until

    def trigger_escape(self) -> list[str]:
        """Stop active computer control immediately (invoked on physical Esc key press)."""
        with self._lock:
            if not self._tasks:
                return []
            escaped_ids = list(self._tasks.keys())
            self._tasks.clear()
            self._escape_count += 1
            self._last_escaped_tasks = list(escaped_ids)
            callback = self.on_escape
        if callable(callback):
            try:
                callback(escaped_ids)
            except Exception:
                pass
        return escaped_ids

    def touch(self, task_id, phase="正在查看屏幕"):
        if os.name != "nt" or not task_id or self._closed.is_set():
            return
        with self._lock:
            self._tasks[task_id] = phase
            if self._thread is None:
                self._thread = threading.Thread(
                    target=self._run,
                    name="ryn-computer-indicator",
                    daemon=True,
                )
                self._thread.start()

    def sync(self, states):
        with self._lock:
            for task_id in list(self._tasks):
                state = states.get(task_id) or {}
                status = state.get("status")
                if status not in {"running", "queued"} or state.get("pending_control"):
                    self._tasks.pop(task_id, None)
                elif state.get("kind") == "model":
                    self._tasks[task_id] = "正在思考下一步"

    def forget(self, task_id):
        with self._lock:
            self._tasks.pop(task_id, None)

    def close(self):
        self._closed.set()
        with self._lock:
            self._tasks.clear()

    def _active_phase(self) -> str:
        with self._lock:
            return next(reversed(self._tasks.values()), "") if self._tasks else ""

    def _text(self):
        phase = self._active_phase()
        return f"Ryn 正在控制电脑 · {phase} · 按 Esc 退出控制" if phase else ""

    def _run(self):
        try:
            self._windows_loop()
        except Exception as exc:
            # A display failure must never change the outcome of an input call.
            self.error = type(exc).__name__

    def _windows_loop(self):
        import ctypes as c
        from ctypes import wintypes as w

        u = c.WinDLL("user32", use_last_error=True)
        g = c.WinDLL("gdi32", use_last_error=True)
        k = c.WinDLL("kernel32", use_last_error=True)
        proc_type = c.WINFUNCTYPE(c.c_ssize_t, w.HWND, w.UINT, w.WPARAM, w.LPARAM)

        class WndClass(c.Structure):
            _fields_ = [
                ("style", w.UINT),
                ("proc", proc_type),
                ("cls_extra", c.c_int),
                ("wnd_extra", c.c_int),
                ("instance", w.HINSTANCE),
                ("icon", w.HICON),
                ("cursor", w.HANDLE),
                ("background", w.HBRUSH),
                ("menu", w.LPCWSTR),
                ("name", w.LPCWSTR),
            ]

        class Point(c.Structure):
            _fields_ = [("x", c.c_long), ("y", c.c_long)]

        class Size(c.Structure):
            _fields_ = [("cx", c.c_long), ("cy", c.c_long)]

        class BlendFunction(c.Structure):
            _fields_ = [
                ("BlendOp", c.c_ubyte),
                ("BlendFlags", c.c_ubyte),
                ("SourceConstantAlpha", c.c_ubyte),
                ("AlphaFormat", c.c_ubyte),
            ]

        class BitmapInfoHeader(c.Structure):
            _fields_ = [
                ("biSize", w.DWORD),
                ("biWidth", c.c_long),
                ("biHeight", c.c_long),
                ("biPlanes", w.WORD),
                ("biBitCount", w.WORD),
                ("biCompression", w.DWORD),
                ("biSizeImage", w.DWORD),
                ("biXPelsPerMeter", c.c_long),
                ("biYPelsPerMeter", c.c_long),
                ("biClrUsed", w.DWORD),
                ("biClrImportant", w.DWORD),
            ]

        class BitmapInfo(c.Structure):
            _fields_ = [("bmiHeader", BitmapInfoHeader), ("bmiColors", w.DWORD * 3)]

        def sig(lib, name, restype, *args):
            fn = getattr(lib, name)
            fn.restype, fn.argtypes = restype, list(args)
            return fn

        sig(k, "GetModuleHandleW", w.HMODULE, w.LPCWSTR)
        sig(u, "RegisterClassW", w.ATOM, c.POINTER(WndClass))
        sig(
            u,
            "CreateWindowExW",
            w.HWND,
            w.DWORD,
            w.LPCWSTR,
            w.LPCWSTR,
            w.DWORD,
            c.c_int,
            c.c_int,
            c.c_int,
            c.c_int,
            w.HWND,
            w.HMENU,
            w.HINSTANCE,
            c.c_void_p,
        )
        sig(u, "DefWindowProcW", c.c_ssize_t, w.HWND, w.UINT, w.WPARAM, w.LPARAM)
        sig(u, "SetWindowDisplayAffinity", w.BOOL, w.HWND, w.DWORD)
        sig(
            u,
            "SetWindowPos",
            w.BOOL,
            w.HWND,
            w.HWND,
            c.c_int,
            c.c_int,
            c.c_int,
            c.c_int,
            w.UINT,
        )
        sig(u, "ShowWindow", w.BOOL, w.HWND, c.c_int)
        sig(
            u,
            "UpdateLayeredWindow",
            w.BOOL,
            w.HWND,
            w.HDC,
            c.POINTER(Point),
            c.POINTER(Size),
            w.HDC,
            c.POINTER(Point),
            w.DWORD,
            c.POINTER(BlendFunction),
            w.DWORD,
        )
        sig(u, "GetDC", w.HDC, w.HWND)
        sig(u, "ReleaseDC", c.c_int, w.HWND, w.HDC)
        sig(u, "PeekMessageW", w.BOOL, c.POINTER(w.MSG), w.HWND, w.UINT, w.UINT, w.UINT)
        sig(u, "TranslateMessage", w.BOOL, c.POINTER(w.MSG))
        sig(u, "DispatchMessageW", c.c_ssize_t, c.POINTER(w.MSG))
        sig(u, "DestroyWindow", w.BOOL, w.HWND)
        sig(u, "UnregisterClassW", w.BOOL, w.LPCWSTR, w.HINSTANCE)
        sig(u, "GetSystemMetrics", c.c_int, c.c_int)
        sig(u, "GetAsyncKeyState", c.c_short, c.c_int)
        sig(g, "CreateCompatibleDC", w.HDC, w.HDC)
        sig(g, "DeleteDC", w.BOOL, w.HDC)
        sig(
            g,
            "CreateDIBSection",
            w.HBITMAP,
            w.HDC,
            c.POINTER(BitmapInfo),
            w.UINT,
            c.POINTER(c.c_void_p),
            w.HANDLE,
            w.DWORD,
        )
        sig(g, "SelectObject", w.HANDLE, w.HDC, w.HANDLE)
        sig(g, "DeleteObject", w.BOOL, w.HANDLE)

        @proc_type
        def wnd_proc(hwnd, msg, wp, lp):
            if msg == 0x84:  # WM_NCHITTEST / HTTRANSPARENT
                return -1
            if msg == 0x21:  # WM_MOUSEACTIVATE / MA_NOACTIVATE
                return 3
            return u.DefWindowProcW(hwnd, msg, wp, lp)

        instance = k.GetModuleHandleW(None)
        name = f"RynComputerIndicator_{os.getpid()}_{id(self)}"
        wc = WndClass(0, wnd_proc, 0, 0, instance, None, None, None, None, name)
        if not u.RegisterClassW(c.byref(wc)):
            raise c.WinError(c.get_last_error())

        hwnd = None
        mem_dc = None
        dib_bmp = None
        old_bmp = None
        dib_bits = c.c_void_p(0)
        dib_size = (0, 0)

        def _release_dib():
            nonlocal dib_bmp, old_bmp, dib_bits, dib_size
            if mem_dc and old_bmp:
                g.SelectObject(mem_dc, old_bmp)
                old_bmp = None
            if dib_bmp:
                g.DeleteObject(dib_bmp)
                dib_bmp = None
            dib_bits = c.c_void_p(0)
            dib_size = (0, 0)

        def _ensure_dib(width: int, height: int):
            nonlocal dib_bmp, old_bmp, dib_bits, dib_size
            if dib_bmp and dib_size == (width, height) and dib_bits.value:
                return
            _release_dib()
            bmi = BitmapInfo()
            bmi.bmiHeader.biSize = c.sizeof(BitmapInfoHeader)
            bmi.bmiHeader.biWidth = width
            bmi.bmiHeader.biHeight = -height  # top-down 32-bit BGRA
            bmi.bmiHeader.biPlanes = 1
            bmi.bmiHeader.biBitCount = 32
            bmi.bmiHeader.biCompression = 0  # BI_RGB
            bmi.bmiHeader.biSizeImage = width * height * 4
            bits = c.c_void_p(0)
            bmp = g.CreateDIBSection(mem_dc, c.byref(bmi), 0, c.byref(bits), None, 0)
            if not bmp or not bits.value:
                raise c.WinError(c.get_last_error())
            dib_bmp = bmp
            dib_bits = bits
            dib_size = (width, height)
            old_bmp = g.SelectObject(mem_dc, dib_bmp)

        def _paint_layered(width: int, height: int, phase: str, pulse_step: int):
            _ensure_dib(width, height)
            base_addr = int(dib_bits.value)
            stride = width * 4

            top_s, bot_s, left_s, right_s = _build_halo_strips(
                width,
                height,
                phase,
                pulse_step=pulse_step,
            )
            top_bytes = top_s.convert("RGBa").tobytes("raw", "BGRa")
            bot_bytes = bot_s.convert("RGBa").tobytes("raw", "BGRa")
            left_bytes = left_s.convert("RGBa").tobytes("raw", "BGRa")
            right_bytes = right_s.convert("RGBa").tobytes("raw", "BGRa")

            # 1. Copy contiguous top strip (includes top halo + corners + HUD capsule)
            c.memmove(base_addr, top_bytes, len(top_bytes))
            # 2. Copy contiguous bottom strip (includes bottom halo + corners)
            bot_offset = (height - bot_s.height) * stride
            c.memmove(base_addr + bot_offset, bot_bytes, len(bot_bytes))

            # 3. Copy left and right strips row by row
            lw_bytes = left_s.width * 4
            rw_bytes = right_s.width * 4
            right_x_offset = (width - right_s.width) * 4
            top_h = top_s.height
            for row_idx in range(left_s.height):
                row_addr = base_addr + (top_h + row_idx) * stride
                src_l_off = row_idx * lw_bytes
                src_r_off = row_idx * rw_bytes
                c.memmove(row_addr, left_bytes[src_l_off : src_l_off + lw_bytes], lw_bytes)
                c.memmove(
                    row_addr + right_x_offset,
                    right_bytes[src_r_off : src_r_off + rw_bytes],
                    rw_bytes,
                )

            pt_dst = Point(0, 0)
            pt_src = Point(0, 0)
            sz = Size(width, height)
            blend = BlendFunction(0x00, 0, 255, 0x01)  # AC_SRC_OVER | AC_SRC_ALPHA
            screen_dc = u.GetDC(None)
            try:
                u.UpdateLayeredWindow(
                    hwnd,
                    screen_dc,
                    c.byref(pt_dst),
                    c.byref(sz),
                    mem_dc,
                    c.byref(pt_src),
                    0,
                    c.byref(blend),
                    0x02,  # ULW_ALPHA
                )
            finally:
                if screen_dc:
                    u.ReleaseDC(None, screen_dc)

        try:
            # LAYERED | TRANSPARENT | TOOLWINDOW | NOACTIVATE. Never takes focus.
            hwnd = u.CreateWindowExW(
                0x080800A0,
                name,
                "Ryn 电脑控制",
                0x80000000,
                0,
                0,
                1,
                1,
                None,
                None,
                instance,
                None,
            )
            if not hwnd:
                raise c.WinError(c.get_last_error())
            u.SetWindowDisplayAffinity(hwnd, 0x11)  # WDA_EXCLUDEFROMCAPTURE

            screen_dc = u.GetDC(None)
            try:
                mem_dc = g.CreateCompatibleDC(screen_dc)
            finally:
                if screen_dc:
                    u.ReleaseDC(None, screen_dc)
            if not mem_dc:
                raise c.WinError(c.get_last_error())

            previous = None
            visible = False
            was_active = False
            esc_was_down = False
            while not self._closed.is_set():
                phase = self._active_phase()
                if phase:
                    if not os.environ.get("PYTEST_CURRENT_TEST") or self._poll_escape_in_tests:
                        raw_esc = int(u.GetAsyncKeyState(0x1B))  # VK_ESCAPE
                        is_down = bool(raw_esc & 0x8000)
                        was_pressed_bit = bool(raw_esc & 0x0001)
                        with self._lock:
                            suppressed = time.monotonic() < self._suppress_escape_until
                        if not was_active or suppressed:
                            # Prime key state on activation or during self-injected Escape
                            esc_was_down = is_down
                        elif (is_down and not esc_was_down) or (not esc_was_down and was_pressed_bit):
                            esc_was_down = is_down
                            self.trigger_escape()
                            phase = ""
                        else:
                            esc_was_down = is_down
                    was_active = bool(phase)
                else:
                    was_active = False
                    esc_was_down = False

                size = (max(320, u.GetSystemMetrics(0)), max(240, u.GetSystemMetrics(1)))
                pulse_step = int(time.monotonic() * 2.5) % 8 if phase else 0
                current = (phase, size, pulse_step)
                if current != previous:
                    if phase:
                        _paint_layered(size[0], size[1], phase, pulse_step)
                        if not visible:
                            u.SetWindowPos(hwnd, w.HWND(-1), 0, 0, size[0], size[1], 0x10 | 0x40)
                            visible = True
                    elif visible:
                        u.ShowWindow(hwnd, 0)
                        visible = False
                    previous = current
                msg = w.MSG()
                while u.PeekMessageW(c.byref(msg), None, 0, 0, 1):
                    u.TranslateMessage(c.byref(msg))
                    u.DispatchMessageW(c.byref(msg))
                self._closed.wait(0.03 if visible else 0.08)
        finally:
            _release_dib()
            if mem_dc:
                g.DeleteDC(mem_dc)
            if hwnd:
                u.DestroyWindow(hwnd)
            u.UnregisterClassW(name, instance)
