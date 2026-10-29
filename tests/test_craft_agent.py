"""Real Agent/permission/receipt paths with a deterministic MCP boundary.

The fake engine tests routing and lifecycle, not DOCX fidelity. Pinned native
round trips have separate acceptance evidence and are not inferred from these
fixture bytes.
"""
import copy
import json
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey
from test_agent_integrations import node as node
from test_agent_runtime import Catalog, Model, advance, approve, assistant
from test_craft_plugins import manager as manager

from rynmesh.agent_runtime import tool_menu, workspace_artifacts
from rynmesh.agent_runtime.bridge import build_agent_integrations
from rynmesh.agent_runtime.runtime import AgentRuntime
from rynmesh.agent_runtime.store import AgentError, AgentStore, AgentToolRejected, encoded
from rynmesh.node_apps import craft_native
from rynmesh.node_apps.craft_agent import ACTION, INSTALL, READ, TOOLS

PREFIX = b"Ryn MCP boundary fixture; not a real DOCX\n"
# Minimal input schemas copied from the fixed WordCraft v0.6.1 MCP source.
SCHEMAS = {
    "new_document": ({"template": {"type": "string"}}, []),
    "open_document": ({"path": {"type": "string"}}, ["path"]),
    "save_document": ({"path": {"type": "string"}}, []),
    "type_text": ({"text": {"type": "string"}, "paragraphs": {"type": "boolean"}}, ["text"]),
    "select_text": ({"text": {"type": "string"}, "occurrence": {"type": "number"}}, ["text"]),
    "get_text": ({}, []),
    "inspect_document": ({}, []),
    "execute": ({"command": {"type": "string"}, "params": {"type": "object"}}, ["command"]),
}


class Backend:
    def __init__(self):
        self.ready = True
        self.instances, self.calls, self.installations = [], [], []
        self.fail_next = None

    def process(self, executable, workspace, app, *, cancelled, **_):
        backend = self

        class Process:
            def __init__(self):
                self.cancelled = cancelled
                self.closed = threading.Event()
                self.failure = None
                self.text = ""
                self.process = SimpleNamespace(poll=lambda: 0 if self.closed.is_set() else None)

            def check(self, active=None):
                assert not self.closed.is_set(), "adapter reused a closed MCP session"
                if self.cancelled() or active and active():
                    raise craft_native.CraftError("agent_cancelled")

            def initialize(self, *, cancelled=None):
                self.check(cancelled)
                return {"protocolVersion": "2025-06-18", "capabilities": {"tools": {}}}

            def tools_list(self, *, cancelled=None):
                self.check(cancelled)
                return {"tools": [{"name": name, "description": "Fixture " + name,
                                    "inputSchema": {"type": "object", "properties": copy.deepcopy(properties),
                                                    "required": required[:]}}
                                   for name, (properties, required) in SCHEMAS.items()]}

            def tools_call(self, name, args, *, cancelled=None):
                self.check(cancelled)
                backend.calls.append((self, name, copy.deepcopy(args)))
                if name == "new_document":
                    self.text = ""
                    value = {"created": True}
                elif name == "type_text":
                    self.text += args["text"]
                    value = {"characters": len(self.text)}
                elif name == "save_document":
                    path = Path(args["path"])
                    assert not path.exists(), "source/output overwrite reached MCP"
                    path.write_bytes(PREFIX + self.text.encode("utf-8"))
                    value = {"saved": True}
                elif name == "open_document":
                    raw = Path(args["path"]).read_bytes()
                    assert raw.startswith(PREFIX)
                    self.text = raw[len(PREFIX):].decode("utf-8")
                    value = {"opened": True}
                elif name == "get_text":
                    value = {"text": self.text}
                elif name == "inspect_document":
                    value = {"characters": len(self.text), "blocks": [{"text": self.text}]}
                elif name == "select_text":
                    value = {"selected": args["text"] in self.text}
                else:
                    raise AssertionError("unreviewed command reached MCP: " + name)
                if backend.fail_next == name:
                    backend.fail_next = None
                    raise craft_native.CraftError("craft_mcp_timeout")
                return {"content": [{"type": "text", "text": json.dumps(value, ensure_ascii=False)}], "isError": False}

            def close(self):
                self.closed.set()

        process = Process()
        self.instances.append(process)
        return process


@pytest.fixture
def craft(tmp_path, node, manager, monkeypatch):  # noqa: F811 - imported pytest fixture parameter
    app, store = node
    backend = Backend()
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    executable = tmp_path / "wordcraft-cli.exe"
    executable.write_bytes(b"fixture only; never executed")

    def status(cache, app):
        supported = app == "wordcraft"
        ready = supported and backend.ready
        return {"app": app, "platform": "windows-x64", "supported": supported,
                "platform_supported": True, "installed": ready, "verified": ready,
                "state": "ready" if ready else "missing"}

    def install(cache, app, **_):
        backend.installations.append(app)
        backend.ready = True
        return {"app": app, "state": "ready"}

    monkeypatch.setattr(craft_native, "runtime_status", status)
    monkeypatch.setattr(craft_native, "installed_runtime", lambda *_: executable)
    monkeypatch.setattr(craft_native, "install_runtime", install)
    monkeypatch.setattr(craft_native, "McpProcess", backend.process)
    manager.install("wordcraft")
    app.state.craft_plugins = manager
    _, tools, _ = build_agent_integrations(app, store, store.home)
    key = X25519PrivateKey.generate()
    home = tmp_path / "agent"
    created = []

    def runtime(*responses, mode="workspace", allowed=None):
        model = Model(*responses)
        value = AgentRuntime(AgentStore(home, key), model, tools, Catalog(), consumer_node_id=store.peer_id)
        value.update_settings({"enabled": True, "model_id": "local:test", "permission_mode": mode,
                               "workspace_root": str(workspace), "desktop_access": False,
                               "allowed_write_tools": allowed or [], "max_steps": 40})
        created.append(value)
        return value, model

    value = SimpleNamespace(manager=manager, tools=tools, backend=backend, workspace=workspace,
                            app=app, node_store=store, home=home, key=key, runtime=runtime)
    yield value
    tools.craft.close()
    tools.local.processes.close()


def _args(tool, arguments=None):
    return {"app": "wordcraft", "tool": tool, "arguments": arguments or {}}


def _data(messages, call_id):
    return json.loads(next(message["content"] for message in messages
                           if message["role"] == "tool" and message.get("tool_call_id") == call_id))


def _context(task):
    return {"task_id": task["id"], "local_execution": task["local_execution"]}


def _read(craft, context, tool="get_text", arguments=None, *, operation="read"):
    return craft.tools.execute(READ, _args(tool, arguments), context=context,
                               operation_id=operation, cancelled=lambda: False)


def _act(craft, context, tool, arguments=None, *, operation=None, preview=None):
    args = _args(tool, arguments)
    preview = craft.tools.preview(ACTION, args, context=context) if preview is None else preview
    return craft.tools.execute(ACTION, args, context=context, operation_id=operation or tool,
                               expected_preview=preview, cancelled=lambda: False)


@pytest.mark.parametrize("mode", ["workspace", "automatic", "confirm_changes"])
def test_discovery_edit_save_readback_and_artifact_follow_real_runtime(craft, mode):
    runtime, model = craft.runtime(
        assistant(name=tool_menu.NAME, args={"load": [TOOLS, ACTION, READ]}, call_id="load-craft"),
        assistant(name=TOOLS, args={"app": "wordcraft"}, call_id="discover"),
        assistant(name=TOOLS, args={"app": "wordcraft", "tool": "type_text"}, call_id="schema"),
        assistant(name=ACTION, args=_args("new_document"), call_id="new"),
        assistant(name=ACTION, args=_args("type_text", {"text": "内容来自实际工具回传"}), call_id="edit"),
        assistant(name=ACTION, args=_args("save_document", {"path": "report.docx"}), call_id="save"),
        assistant(name=READ, args=_args("get_text"), call_id="readback"),
        assistant("已保存 report.docx。"), mode=mode, allowed=[ACTION])
    task = runtime.create({"message": "用 Ryn Word 写文档并保存 report.docx"})
    approvals = []
    for _ in range(35):
        runtime.run_once()
        row = runtime.get(task["id"])
        for session in craft.tools.craft.sessions.values():
            if not session["process"].closed.is_set():
                assert not session["process"].cancelled(), "completed runtime-step token killed a persistent document"
        if row["status"] == "waiting_approval":
            assert mode == "confirm_changes"
            pending = row["pending_approval"]
            approvals.append(pending["arguments"]["tool"])
            assert pending["preview"]["session_kind"] == "task_headless_document"
            approve(runtime, task["id"])
        if row["status"] == "completed":
            break
    done = runtime.get(task["id"])
    assert done["status"] == "completed", done.get("error")
    assert approvals == (["new_document", "type_text", "save_document"] if mode == "confirm_changes" else [])
    messages = model.calls[-1][1]
    discovery = _data(messages, "discover")
    assert discovery["session_kind"] == "task_headless_document" and not discovery["unsaved_editor_window_access"]
    assert "execute" not in {row["name"] for row in discovery["tools"]}
    schema = _data(messages, "schema")["tools"][0]["inputSchema"]
    assert schema["required"] == ["text"] and schema["additionalProperties"] is False
    assert _data(messages, "readback")["data"]["text"] == "内容来自实际工具回传"
    saved = _data(messages, "save")
    assert saved["files"][0]["validation"]["status"] == "reopened"
    assert not done["local_execution"]["desktop_access"]
    files = runtime.artifacts(task["id"])["files"]
    assert [file["name"] for file in files] == ["report.docx"]
    persisted = runtime.store.read()["tasks"][task["id"]]
    artifact = workspace_artifacts.read(persisted, files[0]["id"])
    assert artifact["status"] == "current" and artifact["bytes"] == PREFIX + "内容来自实际工具回传".encode()
    assert sum(name == "save_document" for _, name, _ in craft.backend.calls) == 1


def test_component_setup_always_requires_exact_approval_and_read_only_cannot_edit(craft):
    craft.backend.ready = False
    runtime, model = craft.runtime(
        assistant(name=tool_menu.NAME, args={"load": [TOOLS, INSTALL]}),
        assistant(name=TOOLS, args={"app": "wordcraft"}, call_id="absent"),
        assistant(name=INSTALL, args={"app": "wordcraft"}, call_id="install"),
        assistant("组件已安装。"), mode="automatic", allowed=[INSTALL])
    task = runtime.create({"message": "启用 Word 的 Agent 文档组件"})
    advance(runtime, 10)
    waiting = runtime.get(task["id"])
    assert waiting["status"] == "waiting_approval" and not craft.backend.installations
    assert _data(model.calls[-1][1], "absent")["download_started"] is False
    pending = waiting["pending_approval"]
    assert pending["preview"]["asset"].endswith("-windows-x64-portable.zip")
    assert len(pending["preview"]["sha256"]) == 64
    approve(runtime, task["id"])
    advance(runtime, 5)
    assert runtime.get(task["id"])["status"] == "completed" and craft.backend.installations == ["wordcraft"]

    runtime, _ = craft.runtime(
        assistant(name=tool_menu.NAME, args={"load": [ACTION]}),
        assistant(name=ACTION, args=_args("new_document")), assistant("无法修改。"), mode="read_only")
    read_only = runtime.create({"message": "读取模式下创建文档"})
    before = len(craft.backend.calls)
    advance(runtime, 7)
    assert len(craft.backend.calls) == before
    errors = [event.get("result", {}).get("error") for event in runtime.get(read_only["id"])["events"]]
    assert "agent_permission_denied" in errors


def test_workspace_and_command_boundaries_reject_before_mcp(craft, tmp_path):
    runtime, _ = craft.runtime()
    task = runtime.create({"message": "检查文档工具边界"})
    context = _context(task)
    _act(craft, context, "new_document")
    before = len(craft.backend.calls)
    for name, arguments in [("execute", {"command": "file.save", "params": {}}),
                             ("open_document", {"path": str(tmp_path / "outside.docx")}),
                             ("type_text", {"text": "x", "path": "ignored-hidden-path"}),
                             ("save_document", {})]:
        with pytest.raises(AgentToolRejected):
            craft.tools.preview(ACTION, _args(name, arguments), context=context)
    with pytest.raises(AgentToolRejected, match="read_only_tool_required"):
        _read(craft, context, "new_document")
    source = craft.workspace / "source.docx"
    source.write_bytes(PREFIX + b"original")
    with pytest.raises(AgentToolRejected, match="new_output_path"):
        craft.tools.preview(ACTION, _args("save_document", {"path": "source.docx"}), context=context)
    assert len(craft.backend.calls) == before and source.read_bytes() == PREFIX + b"original"


def test_stale_session_and_changed_input_previews_cannot_execute(craft):
    runtime, _ = craft.runtime()
    task = runtime.create({"message": "只执行审查过的文档版本"})
    context = _context(task)
    _act(craft, context, "new_document")
    stale = craft.tools.preview(ACTION, _args("type_text", {"text": "stale"}), context=context)
    _act(craft, context, "type_text", {"text": "current"}, operation="current")
    with pytest.raises(AgentToolRejected, match="preview_changed"):
        _act(craft, context, "type_text", {"text": "stale"}, operation="stale", preview=stale)
    assert _read(craft, context)["data"]["text"] == "current"
    source = craft.workspace / "input.docx"
    source.write_bytes(PREFIX + b"first version")
    args = _args("open_document", {"path": "input.docx"})
    preview = craft.tools.preview(ACTION, args, context=context)
    source.write_bytes(PREFIX + b"changed externally")
    with pytest.raises(AgentToolRejected, match="preview_changed"):
        _act(craft, context, "open_document", {"path": "input.docx"}, operation="changed-input", preview=preview)
    assert craft.tools.resource_writes.ledger.get("stale") is None
    assert craft.tools.resource_writes.ledger.get("changed-input") is None
    assert not any(name == "open_document" for _, name, _ in craft.backend.calls)
    craft.manager.set_enabled("wordcraft", False)
    before = len(craft.backend.calls)
    with pytest.raises(AgentToolRejected, match="plugin_disabled|agent_unknown_tool|agent_craft_tool_not_supported"):
        _read(craft, context)
    assert len(craft.backend.calls) == before


def test_document_sessions_are_task_owned_and_closed_sessions_require_explicit_reopen(craft):
    runtime, _ = craft.runtime()
    first = runtime.create({"message": "第一个文档"})
    first_context = _context(first)
    _act(craft, first_context, "new_document", operation="first-new")
    _act(craft, first_context, "type_text", {"text": "first task private text"}, operation="first-edit")
    first_session = craft.tools.craft.sessions[(first["id"], "wordcraft")]
    second = runtime.create({"message": "第二个文档"})
    second_context = _context(second)
    assert _read(craft, second_context, operation="second-empty")["data"]["text"] == ""
    _act(craft, second_context, "new_document", operation="second-new")
    _act(craft, second_context, "type_text", {"text": "second task text"}, operation="second-edit")
    assert _read(craft, first_context, operation="first-read")["data"]["text"] == "first task private text"
    assert _read(craft, second_context, operation="second-read")["data"]["text"] == "second task text"
    first_session["process"].close()
    with pytest.raises(AgentToolRejected, match="session_expired_reopen_saved_file"):
        craft.tools.execute(TOOLS, {"app": "wordcraft"}, context=first_context,
                            operation_id="first-after-loss", cancelled=lambda: False)
    fresh = craft.tools.execute(TOOLS, {"app": "wordcraft"}, context=first_context,
                                operation_id="first-fresh", cancelled=lambda: False)
    assert fresh["session_id"] != first_session["id"]
    assert _read(craft, first_context, operation="first-fresh-empty")["data"]["text"] == ""
    assert _read(craft, second_context, operation="second-preserved")["data"]["text"] == "second task text"


def test_confirmed_save_replays_from_encrypted_ledger_and_unknown_write_never_replays(craft):
    runtime, _ = craft.runtime()
    task = runtime.create({"message": "保存文档并测试重启回执"})
    context = _context(task)
    _act(craft, context, "new_document")
    _act(craft, context, "type_text", {"text": "receipt content"})
    args = _args("save_document", {"path": "once.docx"})
    preview = craft.tools.preview(ACTION, args, context=context)
    saved = _act(craft, context, "save_document", {"path": "once.docx"}, operation="save-once", preview=preview)
    assert _act(craft, context, "save_document", {"path": "once.docx"}, operation="save-once", preview=preview) == saved
    assert sum(name == "save_document" for _, name, _ in craft.backend.calls) == 1

    args = _args("type_text", {"text": "uncertain"})
    unknown_preview = craft.tools.preview(ACTION, args, context=context)
    craft.backend.fail_next = "type_text"
    with pytest.raises(AgentError, match="write_outcome_unknown"):
        _act(craft, context, "type_text", {"text": "uncertain"}, operation="unknown-write", preview=unknown_preview)
    before = len(craft.backend.calls)
    craft.tools.craft.close()
    _, restored_tools, _ = build_agent_integrations(craft.app, craft.node_store, craft.node_store.home)
    AgentRuntime(AgentStore(craft.home, craft.key), Model(), restored_tools, Catalog())
    try:
        replay = restored_tools.execute(ACTION, _args("save_document", {"path": "once.docx"}),
            context=context, operation_id="save-once", expected_preview=preview, cancelled=lambda: False)
        assert replay == saved
        with pytest.raises(AgentError, match="write_outcome_unknown"):
            restored_tools.execute(ACTION, args, context=context, operation_id="unknown-write",
                expected_preview=unknown_preview, cancelled=lambda: False)
        assert len(craft.backend.calls) == before
        ledger = restored_tools.resource_writes.ledger
        assert ledger.get("save-once")["status"] == "confirmed"
        assert ledger.get("unknown-write")["status"] == "dispatched"
    finally:
        restored_tools.craft.close()
        restored_tools.local.processes.close()


def test_export_never_overwrites_a_file_created_while_the_native_save_is_running(craft, monkeypatch):
    runtime, _ = craft.runtime()
    context = _context(runtime.create({"message": "将文档另存为新文件"}))
    _act(craft, context, "new_document")
    _act(craft, context, "type_text", {"text": "new content"})
    publish = craft.tools.craft._publish
    target = craft.workspace / "raced.docx"

    def raced(source, destination):
        assert destination == target
        target.write_bytes(b"Concurrent external document; preserve these bytes")
        publish(source, destination)

    monkeypatch.setattr(craft.tools.craft, "_publish", raced)
    result = _act(craft, context, "save_document", {"path": target.name}, operation="raced-save")
    assert result["ok"] is False and result["files"] == []
    assert target.read_bytes() == b"Concurrent external document; preserve these bytes"
    assert craft.tools.resource_writes.ledger.get("raced-save")["status"] == "confirmed"


def test_large_chinese_readbacks_page_by_bytes_without_dropping_text(craft):
    runtime, _ = craft.runtime()
    context = _context(runtime.create({"message": "读取长中文文档"}))
    text = "中文内容" * 4200
    _act(craft, context, "new_document")
    _act(craft, context, "type_text", {"text": text})
    offset, pages = 0, []
    while offset is not None:
        result = craft.tools.execute(READ, {**_args("get_text"), "offset": offset}, context=context,
                                    operation_id="read-long", cancelled=lambda: False)
        assert len(encoded(result)) <= 28000
        pages.append(result["data_text"])
        offset = result["next_offset"]
    assert json.loads("".join(pages))["text"] == text


def test_post_save_validation_failure_keeps_the_write_unknown_and_cannot_replay(craft, monkeypatch):
    runtime, _ = craft.runtime()
    context = _context(runtime.create({"message": "保存后验证失败也不能重放"}))
    _act(craft, context, "new_document")
    args = _args("save_document", {"path": "saved-before-check-failed.docx"})
    preview = craft.tools.preview(ACTION, args, context=context)

    def failed(*_):
        raise OSError("fixture readback failure after the export exists")

    monkeypatch.setattr(craft.tools.craft, "_reopen", failed)
    for _ in range(2):
        with pytest.raises(AgentError, match="write_outcome_unknown"):
            _act(craft, context, "save_document", args["arguments"], operation="post-save-failed", preview=preview)
    assert (craft.workspace / args["arguments"]["path"]).is_file()
    assert sum(name == "save_document" for _, name, _ in craft.backend.calls) == 1
    assert craft.tools.resource_writes.ledger.get("post-save-failed")["status"] == "dispatched"


def test_craft_tools_discovery_includes_compact_schema_and_normalizes_alias_keys(craft):
    runtime, _ = craft.runtime()
    context = _context(runtime.create({"message": "测试工具Schema内联与别名自动归一化"}))
    discovery = craft.tools.execute(TOOLS, {"app": "wordcraft"}, context=context,
                                    operation_id="discover-all", cancelled=lambda: False)
    by_name = {item["name"]: item for item in discovery["tools"]}
    assert "inputSchema" in by_name["type_text"]
    assert by_name["type_text"]["inputSchema"]["required"] == ["text"]
    assert "text" in by_name["type_text"]["inputSchema"]["properties"]

    _act(craft, context, "new_document", operation="alias-new")
    _act(craft, context, "type_text", {"query": "通过query别名写入"}, operation="alias-type")
    selected = _act(craft, context, "select_text", {"query": "别名"}, operation="alias-select")
    assert selected["data"]["selected"] is True
    saved = _act(craft, context, "save_document", {"file_path": "aliased.docx"}, operation="alias-save")
    assert saved["ok"] is True and (craft.workspace / "aliased.docx").is_file()

    with pytest.raises(AgentToolRejected) as exc_info:
        craft.tools.preview(ACTION, _args("type_text", {"unknown_field": 123}), context=context)
    assert set(exc_info.value.details.get("properties") or []) == {"paragraphs", "text"}
    assert exc_info.value.correction

