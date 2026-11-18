"""Compose the image brief in the order its attachments are sent."""

from __future__ import annotations

import json
from typing import Any

from . import sizes, templates

REFERENCE_INSTRUCTIONS = {
    "material": "素材：画面里要出现它，外形、颜色、logo、上面的文字和结构保持一致，不要重新设计；可以调整角度、大小、位置和光影。",
    "style": "风格参考：只参考配色、构图和氛围；里面的物体、文字、logo 不要出现在成品里。",
    "source": "上一版成品，在它的基础上修改，没提到的部分保持不变。",
}


def read_references(raw: str) -> list[dict[str, str]]:
    return [{"id": ref, "role": "material"} if isinstance(ref, str) else ref
            for ref in json.loads(raw)]


def reference_roles(row: dict[str, Any]) -> list[str]:
    roles = ["source"] if row.get("source_job") else []
    roles.extend(ref["role"] for ref in read_references(row["refs"]))
    return roles


def compose(row: dict[str, Any]) -> str:
    parts = [f"一张{sizes.ratio_phrase(row['width'], row['height'])}的图片。", row["brief"].strip()]
    scene = templates.job_scene(row)
    if scene:
        parts.append(f"场景构图：{templates.SCENES[scene]['composition']}")
    style = row.get("style")
    if style:
        preset = templates.STYLES[style]
        parts.append(f"风格（{preset['label']}）：{preset['description']}")
    copy = templates.job_copy(row)
    copy_lines = [f"{label}：「{copy[field]}」" for field, (label, _) in templates.COPY_FIELDS.items()
                  if copy.get(field)]
    if copy_lines:
        parts.append("；".join(copy_lines) + "。这些文字要一字不差，画面里不要出现其它文字。")
    for index, role in enumerate(reference_roles(row), 1):
        parts.append(f"第 {index} 张图是{REFERENCE_INSTRUCTIONS[role]}")
    return "\n".join(p for p in parts if p)
