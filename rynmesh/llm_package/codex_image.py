"""Origin-side native Codex image generation, isolated from reasoning sessions."""
from __future__ import annotations

import base64
import binascii
import io

from .adapters import AdapterError
from .cli_adapter import CLIAgentAdapter
from .codex_session import CodexRPC, _start_thread

MAX_IMAGE_BYTES = 12 * 1024 * 1024


def decode_image(value):
    """Validate the actual output; prose and bundled skill icons are never output."""
    from PIL import Image

    if not isinstance(value, str) or not value or len(value) > MAX_IMAGE_BYTES * 4 // 3 + 4:
        raise AdapterError("Image generation returned no valid image")
    try:
        raw = base64.b64decode(value, validate=True)
        if len(raw) > MAX_IMAGE_BYTES:
            raise ValueError("image too large")
        with Image.open(io.BytesIO(raw)) as img:
            width, height = img.size
            if img.format != "PNG" or not (0 < width * height <= 32_000_000):
                raise ValueError("invalid image format or dimensions")
            img.verify()
    except (ValueError, OSError, binascii.Error, Image.DecompressionBombError) as exc:
        raise AdapterError("Image generation returned invalid PNG data") from exc
    return raw, {"width": width, "height": height, "mime_type": "image/png"}


def generate(prompt, *, transparent_background=False, cancelled=None, timeout=240):
    """Use the owner's Codex login. Never switch to a separately billed API."""
    executable = CLIAgentAdapter("codex_cli")._command_path()
    rpc = CodexRPC(executable, timeout=timeout, image_generation=True, cancelled=cancelled)
    try:
        rpc.initialize()
        models = rpc.call("model/list", {}).get("data", [])
        model = next((m.get("id") for m in models if m.get("isDefault") and "image" in m.get("inputModalities", [])), None)
        model = model or next((m.get("id") for m in models if "image" in m.get("inputModalities", [])), None)
        if not model:
            raise AdapterError("Codex image generation requires a supported signed-in model")
        thread = _start_thread(rpc, model=model, dynamic=[], instructions=(
            "Generate exactly one image with the native image_gen imagegen tool. "
            "Call it immediately, passing the user's image brief verbatim as the imagegen prompt. "
            "Do not add stylistic, layout, color or subject restrictions that are absent from the brief. "
            "No skills, shell, web, plugins or other tools. "
            "Do not substitute code or SVG. If unavailable, report the failure. "
            f"Set transparent_background to {str(bool(transparent_background)).lower()}."))
        response = rpc.call("turn/start", {"threadId": thread, "environments": [], "effort": "low",
            "input": [{"type": "text", "text": prompt, "text_elements": []}]})
        turn = response["turn"]["id"]
        while True:
            event = rpc.pending.popleft() if rpc.pending else rpc.receive()
            if "id" in event and "method" in event:
                rpc.reject(event)
                raise AdapterError("Unexpected image-generation execution request")
            params = event.get("params", {})
            if params.get("threadId") != thread:
                continue
            item = params.get("item", {})
            if event.get("method") == "item/completed" and params.get("turnId") == turn and item.get("type") == "imageGeneration":
                if item.get("status") != "completed":
                    raise AdapterError("Codex image generation failed; check image quota and connection")
                raw, info = decode_image(item.get("result"))
                return raw, {**info, "provider": "codex_native", "model": model,
                             "generation_id": item.get("id"),
                             "transparent_background": item.get("transparentBackground")}
            if event.get("method") == "turn/completed" and params.get("turn", {}).get("id") == turn:
                raise AdapterError("Codex did not return an image; check native image-generation availability")
            if event.get("method") == "error":
                raise AdapterError("Codex image generation failed")
    finally:
        rpc.close()
