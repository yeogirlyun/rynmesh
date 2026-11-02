import base64
import io
from collections import deque

import pytest
from PIL import Image

from test_agent_local_tools import local, execute
from rynmesh.agent_runtime import workspace_artifacts
from rynmesh.agent_runtime.store import AgentError, AgentToolRejected
from rynmesh.llm_package import codex_image, codex_session
from rynmesh.llm_package.adapters import AdapterError


def png():
    output = io.BytesIO()
    Image.new("RGBA", (32, 32), (0, 80, 255, 128)).save(output, "PNG")
    return output.getvalue()


def test_private_image_session_does_not_enable_reasoning_tools():
    for enabled in (False, True):
        with codex_session.private_workspace(copy_auth=False, image_generation=enabled) as (root, env):
            config = (root / "codex/config.toml").read_text()
            assert f"image_generation = {str(enabled).lower()}" in config
            assert f"code_mode_host = {str(enabled).lower()}" in config
            assert "shell_tool = false" in config and "apps = false" in config
            assert "web_search = \"disabled\"" in config


def test_only_actual_completed_generation_is_accepted(monkeypatch):
    instances = []
    class RPC:
        def __init__(self, *args, **kwargs):
            assert kwargs["image_generation"]
            self.pending = deque([
                {"method": "item/completed", "params": {"threadId": "other", "turnId": "turn", "item": {"type": "imageGeneration", "result": "bad"}}},
                {"method": "item/completed", "params": {"threadId": "thread", "turnId": "turn", "item": {"type": "imageGeneration", "status": "completed", "result": base64.b64encode(png()).decode()}}},
            ])
            self.closed = False
            instances.append(self)
        def initialize(self): pass
        def call(self, method, args):
            if method == "model/list": return {"data": [{"id": "available-model", "isDefault": True, "inputModalities": ["text", "image"]}]}
            return {"turn": {"id": "turn"}}
        def close(self): self.closed = True
    monkeypatch.setattr(codex_image, "CodexRPC", RPC)
    monkeypatch.setattr(codex_image, "_start_thread", lambda *a, **k: "thread")
    monkeypatch.setattr(codex_image.CLIAgentAdapter, "_command_path", lambda self: "codex")
    raw, info = codex_image.generate("logo")
    assert raw == png() and info["width"] == 32 and instances[0].closed


@pytest.mark.parametrize("value", [None, "", "not an image", base64.b64encode(b"pretend PNG").decode()])
def test_invalid_image_rejected(value):
    with pytest.raises(AdapterError): codex_image.decode_image(value)


def test_generated_image_receipt_replay_and_preview(local, monkeypatch):
    tools, context, workspace, _ = local
    calls = []
    monkeypatch.setattr(codex_image.CLIAgentAdapter, "health", lambda self: {"ok": True})
    monkeypatch.setattr(codex_image, "generate", lambda *a, **k: (calls.append(a) or png(), {"width": 32, "height": 32}))
    args = {"prompt": "blue M logo", "path": "logo.png"}
    preview = tools.preview("ryn_image_generate", args, context)
    result = execute(tools, context, "ryn_image_generate", args)
    assert (workspace / "logo.png").read_bytes() == png()
    replay = tools.execute("ryn_image_generate", args, operation_id="op", context=context, expected_preview=preview, cancelled=lambda: False)
    assert replay == result and len(calls) == 1
    receipt = {"tool": "ryn_image_generate", "arguments": args, "status": "confirmed", "result": result, "operation_id": "op", "id": "receipt"}
    files = workspace_artifacts.history({"receipts": [receipt]})
    assert len(files) == 1 and next(iter(files.values()))["sha256"] == result["sha256"]
    assert tools.verify(receipt, context, lambda: False)["status"] == "passed"
    (workspace / "logo.png").write_bytes(b"changed")
    assert tools.verify(receipt, context, lambda: False)["status"] != "passed"
    with pytest.raises(AgentToolRejected): tools.preview("ryn_image_generate", args, context)


def test_cancelled_generation_never_writes(local, monkeypatch):
    tools, context, workspace, _ = local
    monkeypatch.setattr(codex_image.CLIAgentAdapter, "health", lambda self: {"ok": True})
    monkeypatch.setattr(codex_image, "generate", lambda *a, **k: (png(), {}))
    with pytest.raises(AgentToolRejected):
        tools.perform("ryn_image_generate", {"prompt": "logo", "path": "logo.png"}, context, lambda: True)
    assert not (workspace / "logo.png").exists()


def test_output_must_be_new_png_inside_workspace(local, monkeypatch):
    tools, context, _, _ = local
    monkeypatch.setattr(codex_image.CLIAgentAdapter, "health", lambda self: {"ok": True})
    for path in ("../outside.png", "logo.svg", "missing/logo.png"):
        with pytest.raises(AgentToolRejected):
            tools.preview("ryn_image_generate", {"prompt": "logo", "path": path}, context)


def test_image_tool_respects_existing_permission_modes():
    from rynmesh.agent_runtime.execution_policy import decide
    call = {"name": "ryn_image_generate", "arguments": {"prompt": "logo", "path": "logo.png"}}
    for mode, action in (("workspace", "execute"), ("read_only", "deny"), ("confirm_changes", "ask")):
        assert decide(call, {"effect": "write"}, {"permission_mode": mode}, {"permission_mode": mode}).action == action
