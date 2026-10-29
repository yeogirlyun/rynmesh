"""Real pixel encoding and nested model admission, with an offline API boundary."""
import base64
import io
import json

import pytest
from PIL import Image
from test_agent_integrations import add_source, call, fake_upstream
from test_agent_integrations import node as node
from test_agent_local_tools import local as local

from rynmesh.agent_runtime.bridge import build_agent_integrations
from rynmesh.agent_runtime.runtime import AgentRuntime
from rynmesh.agent_runtime.store import AgentStore, AgentToolRejected
from rynmesh.llm_package.chat import validate_chat
from rynmesh.services import peer_box


def test_image_tool_sends_actual_bounded_pixels_without_putting_base64_in_receipts(node, monkeypatch, tmp_path):  # noqa: F811 - imported pytest fixture
    app, owner = node
    _, model_id = add_source(app)
    bridge, tools, catalog = build_agent_integrations(app, owner, owner.home)
    runtime = AgentRuntime(AgentStore(owner.home / "vision-agent", peer_box.load_or_create_messaging_key(owner.home / "messaging.x25519")), bridge, tools, catalog)
    runtime.update_settings({"enabled": True, "model_id": model_id, "workspace_root": str(tmp_path)})
    Image.new("RGB", (900, 600), "red").save(tmp_path / "picture.png")
    observed = []
    def vision(request):
        part = request["messages"][-1]["content"][1]
        raw = base64.b64decode(part["image_url"]["url"].split(",", 1)[1])
        with Image.open(io.BytesIO(raw)) as picture:
            assert picture.size == (512, 341)
            red, green, blue = picture.getpixel((30, 30))
            assert red > 240 and green < 10 and blue < 10
        observed.append(True)
        return {"role": "assistant", "content": "A red rectangle is visible."}
    requests = fake_upstream(monkeypatch, [
        call("ryn_discover_tools", json.dumps({"load": ["ryn_image_inspect"]}), "discover"),
        call("ryn_image_inspect", json.dumps({"path": "picture.png"}), "image"), vision,
        {"role": "assistant", "content": "The image contains a red rectangle."}])
    task = runtime.create({"message": "Inspect the image."})
    for _ in range(5):
        runtime.run_once()
    stored = runtime.store.read()["tasks"][task["id"]]
    assert observed == [True]
    assert len(requests) == 4
    assert any(item["stage"] == "image_inspection" and item["status"] == "settled" for item in stored["budget"]["records"])
    assert "base64," not in json.dumps(stored)
    receipt = next(item for item in stored["receipts"] if item["tool"] == "ryn_image_inspect")
    assert receipt["result"]["observation"] == "model_visual_input"
    assert receipt["result"]["width"] == 900


def test_image_tool_cannot_read_outside_workspace_or_invent_vision_support(local):  # noqa: F811 - imported pytest fixture
    tools, context, root, _ = local
    Image.new("RGB", (10, 10), "blue").save(root / "picture.png")
    with pytest.raises(AgentToolRejected, match="requires_api_model"):
        tools.perform("ryn_image_inspect", {"path": "picture.png"}, context, lambda: False)
    with pytest.raises(AgentToolRejected, match="outside_workspace"):
        tools.perform("ryn_image_inspect", {"path": "../picture.png"}, context, lambda: False)


@pytest.mark.parametrize("url", ["https://example.com/image.png", "file:///private.png", "data:image/jpeg;base64,???"])
def test_chat_images_are_inline_only_and_do_not_allow_remote_fetch(url):
    with pytest.raises(ValueError):
        validate_chat({"messages": [{"role": "user", "content": [{"type": "image_url", "image_url": {"url": url}}]}]})
