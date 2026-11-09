"""The same conversation reaches a local model and a shared model the same way (P3).

Each scenario is sent once through the local API route and once through a
shared (peer) route of the same model. The text the model receives must be
identical; images must be byte-identical on the v2 carrier, and on the older
v1 carrier may only differ by the documented compression of oversized images.
"""
from __future__ import annotations

import base64
import copy
import io
import json
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from PIL import Image
from test_agent_image_carrier_profile import ANSWER, PNG_3_46MIB, _HTTPResponse, _make_bridge

from rynmesh.agent_runtime import image_capabilities as caps
from rynmesh.agent_runtime.input_attachments import attach_to_message, parse_attachments, provider_content

V1_LIMIT = 128 * 1024


def _png(size=(64, 48), color=(20, 120, 200)):
    buf = io.BytesIO()
    Image.new("RGB", size, color).save(buf, "PNG")
    return buf.getvalue()


def _gif():
    buf = io.BytesIO()
    frames = [Image.new("RGB", (40, 30), color) for color in ((255, 0, 0), (0, 0, 255))]
    frames[0].save(buf, "GIF", save_all=True, append_images=frames[1:])
    return buf.getvalue()


def _owner_message(tmp_path, text, files):
    """Build the owner message exactly as the runtime does (attachments, excerpts, paths)."""
    prepared = parse_attachments([{"name": name, "media_type": mime, "data_base64": base64.b64encode(raw).decode()}
                                  for name, mime, raw in files]) if files else None
    store = SimpleNamespace(default_workspace=lambda: tmp_path)
    message = attach_to_message(store, {"id": "agent_" + "0" * 32, "local_execution": {"workspace_root": str(tmp_path)}},
                                {"role": "user", "content": text}, prepared)
    return [{"role": "user", "content": provider_content(message)}]


def _raw_image_parts(messages):
    out = []
    for message in messages:
        if isinstance(message.get("content"), list):
            for part in message["content"]:
                if part.get("type") == "image_url":
                    out.append(base64.b64decode(part["image_url"]["url"].split(",", 1)[1]))
    return out


def _text_parts(messages):
    out = []
    for message in messages:
        content = message.get("content")
        if isinstance(content, str):
            out.append(content)
        elif isinstance(content, list):
            out.extend(part["text"] for part in content if part.get("type") == "text")
    return out


class _LocalCapture:
    def __init__(self):
        self.bodies = []

    def __call__(self, request, *args, **kwargs):
        body = json.loads(request.data)
        self.bodies.append(body)
        return _HTTPResponse("openai_compatible", bool(body.get("stream")))


def _send_local(messages):
    bridge, _, row, _ = _make_bridge("openai_compatible")
    capture = _LocalCapture()
    with patch("urllib.request.urlopen", capture):
        bridge.complete(row["id"], copy.deepcopy(messages), [], lambda: False)
    return [m for m in capture.bodies[0]["messages"] if m["role"] == "user"]


def _send_shared(messages, *capabilities):
    bridge, _, row, services = _make_bridge()
    row.update(source="peer", peer_id="sharing-computer", capabilities=list(capabilities))
    row.update(caps.catalog_image_fields(row))
    sent = []

    async def execute_order(request, *args, **kwargs):
        sent.append(copy.deepcopy(request))
        return {"state": "succeeded", "message": {"role": "assistant", "content": ANSWER}, "finish_reason": "stop",
                "reported_usage": {"input_tokens": 3, "output_tokens": 1}}

    services["execute_order"] = execute_order
    services["cancel_order"] = lambda _: None
    bridge.complete(row["id"], copy.deepcopy(messages), [], lambda: False)
    return [m for m in sent[0]["chat"]["messages"] if m["role"] == "user"]


SCENARIOS = {
    "plain text": ("把这段话翻译成英文：付款已到账。", []),
    "csv attachment": ("核对这张表。", [("付款.csv", "text/csv", "日期,金额\n2026-11-09,1200\n".encode("utf-8"))]),
    "txt attachment": ("总结这份说明。", [("说明.txt", "text/plain", ("第一条。\n" * 50).encode("utf-8"))]),
    "small image attachment": ("这张图里是什么？", [("shot.png", "image/png", _png())]),
    "gif attachment": ("这张动图第一帧是什么颜色？", [("anim.gif", "image/gif", _gif())]),
    "two images": ("比较两张图。", [("a.png", "image/png", _png(color=(200, 0, 0))), ("b.png", "image/png", _png(color=(0, 200, 0)))]),
}


@pytest.mark.parametrize("name", list(SCENARIOS))
def test_owner_messages_reach_local_and_shared_models_identically(tmp_path, name):
    text, files = SCENARIOS[name]
    messages = _owner_message(tmp_path, text, files)
    local = _send_local(messages)
    shared_v2 = _send_shared(messages, "image-input", "image-carrier:inline-v2")
    shared_v1 = _send_shared(messages)
    assert _text_parts(local) == _text_parts(shared_v2) == _text_parts(shared_v1)
    assert _raw_image_parts(local) == _raw_image_parts(shared_v2)
    for local_image, v1_image in zip(_raw_image_parts(local), _raw_image_parts(shared_v1), strict=True):
        assert v1_image == local_image or len(v1_image) <= V1_LIMIT


def test_full_size_screenshot_is_identical_on_v2_and_bounded_on_v1():
    messages = [{"role": "user", "content": [{"type": "text", "text": "看这个截图"},
                 {"type": "image_url", "image_url": {"url": "data:image/png;base64," + base64.b64encode(PNG_3_46MIB).decode()}}]}]
    local = _send_local(messages)
    assert _raw_image_parts(local) == [PNG_3_46MIB]
    assert _raw_image_parts(_send_shared(messages, "image-input", "image-carrier:inline-v2")) == [PNG_3_46MIB]
    (v1,) = _raw_image_parts(_send_shared(messages))
    assert len(v1) <= V1_LIMIT and Image.open(io.BytesIO(v1)).size[0] >= 768


def test_declared_text_only_shared_model_matches_a_text_only_local_model(tmp_path):
    messages = _owner_message(tmp_path, "这张图里是什么？", [("shot.png", "image/png", _png())])
    local_text_only = caps.evaluate_image_capability(
        {"id": "local/x", "source": "local_api", "adapter": "openai_compatible", "agent_supported": True,
         "model_settings": {"overrides": {"image_input": "unsupported"}}})
    shared_text_only = caps.evaluate_image_capability(
        {"id": "peer/x", "source": "peer", "adapter": "openai_compatible", "agent_supported": True,
         "capabilities": ["image-unsupported"]})
    assert local_text_only["supports_image_input"] is shared_text_only["supports_image_input"] is False
    noted, omitted, latest = caps.omit_images(messages)
    assert omitted == 1 and latest and _raw_image_parts(noted) == []
