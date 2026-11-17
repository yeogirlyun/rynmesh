"""Native Codex image generation on this computer, with optional reference images.

Uses the owner's signed-in Codex (the same path as the agent's
``ryn_image_generate``); never a separately billed API. Reference images are
attached to the turn and the image tool is told to use them
(``num_last_images_to_include``), which Codex turns into an image edit.
"""

from __future__ import annotations

import base64
import json
import os
from pathlib import Path
from typing import Any, Callable

GENERATION_TIMEOUT_S = 300

FAILURES = {
    "codex_missing": "这台电脑没有安装 Codex。",
    "codex_signed_out": "这台电脑的 Codex 没有登录。",
    "quota_exhausted": "做图额度用完了，稍后或明天再试。",
    "content_rejected": "这个需求没有通过内容审核，换个描述试试。",
    "timeout": "生成超时了。",
    "cancelled": "已取消。",
    "interrupted": "做图电脑重启了，任务已中断。",
    "generation_failed": "没有生成成功。",
}


class GenerationError(Exception):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code if code in FAILURES else "generation_failed"


def readiness() -> dict[str, Any]:
    """Cheap local check: Codex present and a sign-in file exists. No network."""
    from rynmesh.llm_package.cli_adapter import CLIAgentAdapter

    try:
        executable = CLIAgentAdapter("codex_cli")._command_path()
    except Exception:
        executable = None
    if not executable:
        return {"ready": False, "code": "codex_missing"}
    auth = Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex") / "auth.json"
    if not auth.is_file():
        return {"ready": False, "code": "codex_signed_out"}
    return {"ready": True, "code": ""}


def _classify(text: str) -> str:
    value = text.lower()
    if any(k in value for k in ("usage_limit", "usage limit", "rate_limit", "rate limit", "quota", "insufficient")):
        return "quota_exhausted"
    if any(k in value for k in ("moderation", "safety", "content_policy", "content policy", "policy_violation", "blocked")):
        return "content_rejected"
    if any(k in value for k in ("unauthorized", "401", "not signed", "login", "auth")):
        return "codex_signed_out"
    if "timed out" in value or "timeout" in value:
        return "timeout"
    return "generation_failed"


def instructions(reference_count: int) -> str:
    base = ("Generate exactly one image with the native image_gen imagegen tool. "
            "Call it immediately, passing the user's image brief verbatim as the imagegen prompt. "
            "Do not add stylistic, layout, color or subject restrictions that are absent from the brief. "
            "No skills, shell, web, plugins or other tools. Do not substitute code or SVG. "
            "If unavailable, report the failure. Set transparent_background to false.")
    if reference_count:
        base += (f" The user attached {reference_count} reference image(s) before the brief; "
                 f"call imagegen with num_last_images_to_include set to {reference_count} so they are used.")
    return base


def generate(brief: str, *, references: list[bytes] | None = None,
             cancelled: Callable[[], bool] | None = None, timeout: int = GENERATION_TIMEOUT_S
             ) -> tuple[bytes, dict[str, Any]]:
    """One image as PNG bytes plus metadata; raises GenerationError with a stable code."""
    from rynmesh.llm_package.adapters import AdapterError
    from rynmesh.llm_package.cli_adapter import CLIAgentAdapter
    from rynmesh.llm_package.codex_image import decode_image
    from rynmesh.llm_package.codex_session import CodexRPC, _start_thread

    ready = readiness()
    if not ready["ready"]:
        raise GenerationError(ready["code"])
    references = list(references or [])
    executable = CLIAgentAdapter("codex_cli")._command_path()
    rpc = None
    try:
        rpc = CodexRPC(executable, timeout=timeout, image_generation=True, cancelled=cancelled)
        rpc.initialize()
        models = rpc.call("model/list", {}).get("data", [])
        model = next((m.get("id") for m in models if m.get("isDefault") and "image" in m.get("inputModalities", [])), None)
        model = model or next((m.get("id") for m in models if "image" in m.get("inputModalities", [])), None)
        if not model:
            raise GenerationError("codex_signed_out")
        thread = _start_thread(rpc, model=model, dynamic=[], instructions=instructions(len(references)))
        items: list[dict[str, Any]] = [{"type": "image", "url": "data:image/png;base64," + base64.b64encode(r).decode()}
                                       for r in references]
        items.append({"type": "text", "text": brief, "text_elements": []})
        turn = rpc.call("turn/start", {"threadId": thread, "environments": [], "effort": "low", "input": items})["turn"]["id"]
        while True:
            event = rpc.pending.popleft() if rpc.pending else rpc.receive()
            if "id" in event and "method" in event:
                rpc.reject(event)
                raise GenerationError("generation_failed")
            params = event.get("params", {})
            if params.get("threadId") != thread:
                continue
            item = params.get("item", {})
            if (event.get("method") == "item/completed" and params.get("turnId") == turn
                    and item.get("type") == "imageGeneration"):
                if item.get("status") != "completed":
                    detail = {k: v for k, v in item.items() if k != "result"}
                    raise GenerationError(_classify(json.dumps(detail, ensure_ascii=False)))
                raw, info = decode_image(item.get("result"))
                return raw, {**info, "model": model, "generation_id": item.get("id")}
            if event.get("method") == "turn/completed" and params.get("turn", {}).get("id") == turn:
                raise GenerationError(_classify(json.dumps(params.get("turn", {}), ensure_ascii=False)))
            if event.get("method") == "error":
                raise GenerationError(_classify(json.dumps(params, ensure_ascii=False)))
    except GenerationError:
        raise
    except AdapterError as exc:
        if getattr(exc, "code", "") == "cancelled" or (cancelled and cancelled()):
            raise GenerationError("cancelled") from None
        if getattr(exc, "code", "") == "inference_timeout":
            raise GenerationError("timeout") from None
        raise GenerationError(_classify(str(exc))) from None
    except Exception as exc:
        if cancelled and cancelled():
            raise GenerationError("cancelled") from None
        raise GenerationError(_classify(str(exc))) from None
    finally:
        if rpc is not None:
            try:
                rpc.close()
            except Exception:
                pass
