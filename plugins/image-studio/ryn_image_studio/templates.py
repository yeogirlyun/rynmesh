"""Composition and copy presets for image jobs."""

from __future__ import annotations

import json
from typing import Any

from . import sizes

SCENES = {
    "banner": {
        "label": "横幅",
        "composition": "商品放在一侧，约占画面 40%，另一侧留出干净的文案区，主标题最醒目，四周留边。",
        "purpose": "banner",
        "size": sizes.PURPOSES["banner"]["size"],
    },
    "main": {
        "label": "商品主图",
        "composition": "商品居中并占画面主体，完整呈现轮廓和关键细节，背景简洁，四周留出安全边距。",
        "purpose": "main",
        "size": sizes.PURPOSES["main"]["size"],
    },
    "poster": {
        "label": "海报",
        "composition": "竖向安排主视觉和文案区，主标题形成第一阅读焦点，副标题与角标分层排列。",
        "purpose": "poster",
        "size": sizes.PURPOSES["poster"]["size"],
    },
    "detail": {
        "label": "详情页头图",
        "composition": "以商品或关键功能为主视觉，留出整洁的卖点文案区，边缘留白方便衔接下面的详情内容。",
        "purpose": "banner",
        "size": sizes.PURPOSES["banner"]["size"],
    },
    "social": {
        "label": "社媒配图",
        "composition": "主体和主标题集中在画面中央的安全区，在手机小屏上保持清晰，边缘留白以适应裁切。",
        "purpose": "story",
        "size": sizes.PURPOSES["story"]["size"],
    },
}
for _scene, _preset in SCENES.items():
    _preset["sizes"] = [{"width": width, "height": height} for width, height in sizes.SCENE_SIZES[_scene]]

STYLES = {
    "minimal": {"label": "简约", "description": "减少装饰，用克制的配色、清晰的排版和充分留白。"},
    "tech": {"label": "科技", "description": "用清晰的几何线条、冷色和有层次的光影表现科技感。"},
    "festive": {"label": "节日", "description": "使用与需求对应的节日色彩和少量装饰，保留清晰的主体。"},
    "promo": {"label": "促销", "description": "用鲜明的色彩和清晰的视觉层级突出活动信息。"},
    "premium": {"label": "高级", "description": "采用细腻的材质、克制的色调和柔和光影突出质感。"},
    "fresh": {"label": "清新", "description": "采用明亮柔和的色彩、自然光和轻盈的留白。"},
}

PURPOSE_SCENES = {"main": "main", "poster": "poster", "story": "social", "banner": "banner"}
COPY_FIELDS = {"title": ("主标题", 40), "subtitle": ("副标题", 60), "badge": ("角标", 20)}


def job_scene(row: dict[str, Any]) -> str | None:
    return row.get("scene") or PURPOSE_SCENES.get(row.get("purpose"))


def job_copy(row: dict[str, Any]) -> dict[str, str]:
    copy = json.loads(row.get("copy_json") or "{}")
    words = row["words"].strip()
    if words:
        copy.setdefault("title", words)
    return copy
