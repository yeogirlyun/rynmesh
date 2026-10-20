"""Regression tests for Ryn Agent Extensions imports, TOML 3.10 parity, MCP transport, and Win32 Computer Use."""

from __future__ import annotations

import json
import sys
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

import pytest

from rynmesh.agent_runtime.extensions import ExtensionStore
from rynmesh.agent_runtime.extensions import imports as ext_imports
from rynmesh.agent_runtime.extensions.imports import ExtensionImportManager
from rynmesh.agent_runtime.extensions.mcp_transport import (
    McpSessionManager,
    McpTransportError,
    StdioMcpSession,
    StreamableHttpMcpSession,
)
from rynmesh.agent_runtime.store import AgentError, AgentToolRejected
from rynmesh.services import peer_box


def test_import_staging_tamper_tool_restrictions_and_duplicate_skills(tmp_path: Path) -> None:
    key = peer_box.load_or_create_messaging_key(tmp_path / "messaging.key")
    store = ExtensionStore(tmp_path / "agent", key)
    manager = ExtensionImportManager(store)

    # 1. Tampering with staged files after preview must be rejected on commit
    src_skill = tmp_path / "src-skill"
    (src_skill / "references").mkdir(parents=True)
    (src_skill / "SKILL.md").write_text(
        "---\nname: tamper-check\ndescription: Verify integrity\n---\nSee references/guide.md\n",
        encoding="utf-8",
    )
    (src_skill / "references" / "guide.md").write_text("SAFE_CONTENT\n", encoding="utf-8")

    preview = manager.preview({"kind": "skill", "source_type": "path", "path": str(src_skill)})
    staged_root = manager.staging_root / preview["preview_id"]
    (staged_root / "references" / "guide.md").write_text("TAMPERED_CONTENT\n", encoding="utf-8")

    with pytest.raises(AgentError) as exc_info:
        manager.commit(preview["preview_id"], expected_revision=preview["expected_revision"])
    assert exc_info.value.args == ("agent_extension_package_integrity_failed",)
    assert all(s["id"] != "tamper-check" for s in store.overview()["skills"])

    # 2. Imported MCP enabled_tools and disabled_tools are preserved; read_only_tools is ignored with warning
    mcp_cfg = {
        "mcpServers": {
            "restricted": {
                "command": sys.executable,
                "args": ["-V"],
                "enabled_tools": ["read_safe", "list_items"],
                "disabled_tools": ["delete_all"],
                "read_only_tools": ["read_safe"],
            }
        }
    }
    mcp_preview = manager.preview({"kind": "mcp", "config_text": json.dumps(mcp_cfg)})
    assert mcp_preview["mcp_servers"][0]["enabled_tools"] == ["list_items", "read_safe"]
    assert mcp_preview["mcp_servers"][0]["disabled_tools"] == ["delete_all"]
    assert any("read_only_tools" in w for w in mcp_preview["warnings"])

    manager.commit(mcp_preview["preview_id"], expected_revision=mcp_preview["expected_revision"])
    internal_srv = store.get_mcp_server_internal("restricted")
    assert internal_srv["enabled_tools"] == ["list_items", "read_safe"]
    assert internal_srv["disabled_tools"] == ["delete_all"]
    assert internal_srv["read_only_tools"] == []

    # 3. Three skills in one plugin sharing frontmatter name 'shared' get 3 unique IDs + warnings
    plugin_dir = tmp_path / "dup-plugin"
    (plugin_dir / ".codex-plugin").mkdir(parents=True)
    (plugin_dir / ".codex-plugin" / "plugin.json").write_text(
        json.dumps({"name": "dup-plugin", "version": "1.0.0"}),
        encoding="utf-8",
    )
    for folder in ("a", "b", "c"):
        sd = plugin_dir / "skills" / folder
        sd.mkdir(parents=True)
        (sd / "SKILL.md").write_text(
            f"---\nname: shared\ndescription: Skill {folder}\n---\nBody {folder}\n",
            encoding="utf-8",
        )

    dup_preview = manager.preview({"kind": "plugin", "source_type": "path", "path": str(plugin_dir)})
    preview_skill_ids = [s["id"] for s in dup_preview["skills"]]
    assert len(preview_skill_ids) == 3
    assert len(set(preview_skill_ids)) == 3
    assert len(dup_preview["warnings"]) >= 2

    committed = manager.commit(
        dup_preview["preview_id"], expected_revision=dup_preview["expected_revision"]
    )
    committed_ids = [
        s["id"] for s in committed["skills"] if s.get("plugin_id") == "dup-plugin"
    ]
    assert len(committed_ids) == 3
    assert len(set(committed_ids)) == 3


def test_python310_toml_fallback_and_valid_codex_syntax(tmp_path: Path) -> None:
    key = peer_box.load_or_create_messaging_key(tmp_path / "messaging.key")
    cases = {
        "trailing_comment": '[mcp_servers.fixture]\ncommand = "python" # interpreter\nargs = ["-V"]\n',
        "single_quoted_array": "[mcp_servers.fixture]\ncommand = 'python'\nargs = ['-u', 'server.py']\n",
        "quoted_dotted_server_id": '[mcp_servers."server.with.dot"]\ncommand = "python"\nargs = ["-V"]\n',
    }
    for name, text in cases.items():
        store = ExtensionStore(tmp_path / name, key)
        manager = ExtensionImportManager(store)
        normal_view = manager.preview({"kind": "mcp", "filename": "config.toml", "config_text": text})
        with patch.object(ext_imports, "tomllib", None):
            fallback_view = manager.preview(
                {"kind": "mcp", "filename": "config.toml", "config_text": text}
            )
        assert [row["id"] for row in normal_view["mcp_servers"]] == [
            row["id"] for row in fallback_view["mcp_servers"]
        ]
        assert normal_view["mcp_servers"][0]["args"] == fallback_view["mcp_servers"][0]["args"]


def test_http_open_sse_trickle_deadline_and_oversize_notification() -> None:
    class ProbeHandler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *_args) -> None:
            pass

        def do_DELETE(self) -> None:
            self.send_response(204)
            self.send_header("Content-Length", "0")
            self.end_headers()

        def do_POST(self) -> None:
            length = int(self.headers.get("Content-Length", "0"))
            request = json.loads(self.rfile.read(length))
            ident = request.get("id")
            method = request.get("method")
            if ident is None:
                body = (
                    b"x" * (4 * 1024 * 1024 + 4096)
                    if self.path == "/oversize-notification"
                    else b""
                )
                self.send_response(202)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                try:
                    self.wfile.write(body)
                except OSError:
                    pass
                return

            payload = {
                "jsonrpc": "2.0",
                "id": ident,
                "result": (
                    {
                        "protocolVersion": "2025-03-26",
                        "capabilities": {},
                        "serverInfo": {"name": "probe", "version": "1"},
                    }
                    if method == "initialize"
                    else {}
                ),
            }
            if self.path == "/open-sse":
                body = (
                    b": keepalive\n\nevent: message\ndata: "
                    + json.dumps(payload).encode("utf-8")
                    + b"\n\n"
                )
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Transfer-Encoding", "chunked")
                self.end_headers()
                try:
                    self.wfile.write(f"{len(body):x}\r\n".encode("ascii") + body + b"\r\n")
                    self.wfile.flush()
                    time.sleep(1.7)
                    self.wfile.write(b"0\r\n\r\n")
                    self.wfile.flush()
                except OSError:
                    pass
                return

            body = json.dumps(payload).encode("utf-8")
            if self.path == "/trickle-json":
                body += b" " * 8
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            if self.path == "/trickle-json":
                try:
                    self.wfile.write(body[:-8])
                    self.wfile.flush()
                    for char in body[-8:]:
                        time.sleep(0.25)
                        self.wfile.write(bytes([char]))
                        self.wfile.flush()
                except OSError:
                    pass
            else:
                self.wfile.write(body)

    server = ThreadingHTTPServer(("127.0.0.1", 0), ProbeHandler)
    server.daemon_threads = True
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        base = {"id": "probe", "startup_timeout_ms": 1000, "tool_timeout_ms": 1000}

        # 1. Open SSE stream returns immediately on matching JSON-RPC message without waiting for EOF
        sse_sess = StreamableHttpMcpSession(
            {**base, "url": f"http://127.0.0.1:{server.server_port}/open-sse"}
        )
        t0 = time.monotonic()
        try:
            info = sse_sess.initialize()
            elapsed = time.monotonic() - t0
            assert info["protocolVersion"] == "2025-03-26"
            assert elapsed < 0.9
        finally:
            sse_sess.close()

        # 2. Trickle JSON response enforces overall monotonic deadline
        trickle_sess = StreamableHttpMcpSession(
            {**base, "url": f"http://127.0.0.1:{server.server_port}/trickle-json"}
        )
        t1 = time.monotonic()
        try:
            with pytest.raises(McpTransportError) as exc_info:
                trickle_sess.initialize()
            elapsed_trickle = time.monotonic() - t1
            assert exc_info.value.args == ("agent_mcp_timeout",)
            assert elapsed_trickle < 1.6
        finally:
            trickle_sess.close()

        # 3. Oversized notifications/initialized body is rejected by MAX_OUTPUT_BYTES
        oversize_sess = StreamableHttpMcpSession(
            {**base, "url": f"http://127.0.0.1:{server.server_port}/oversize-notification"}
        )
        try:
            with pytest.raises(McpTransportError) as exc_info:
                oversize_sess.initialize()
            assert exc_info.value.args == ("agent_mcp_output_limit",)
            assert exc_info.value.dispatched is False
        finally:
            oversize_sess.close()
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_disable_during_slow_initialize_and_policy_schema_guards(tmp_path: Path) -> None:
    script = tmp_path / "slow_stdio.py"
    script.write_text(
        "import json, pathlib, sys, time\n"
        "root = pathlib.Path(sys.argv[1])\n"
        "root.mkdir(parents=True, exist_ok=True)\n"
        "for line in sys.stdin:\n"
        "    req = json.loads(line)\n"
        "    ident = req.get('id')\n"
        "    if ident is None:\n"
        "        continue\n"
        "    if req.get('method') == 'initialize':\n"
        "        (root / 'initializing').write_text('ready')\n"
        "        stop = time.monotonic() + 10\n"
        "        while not (root / 'release').exists() and time.monotonic() < stop:\n"
        "            time.sleep(0.01)\n"
        "        res = {'protocolVersion': '2025-03-26', 'capabilities': {}, 'serverInfo': {'name': 's', 'version': '1'}}\n"
        "    elif req.get('method') == 'tools/call':\n"
        "        c = root / 'calls'\n"
        "        n = int(c.read_text()) if c.exists() else 0\n"
        "        c.write_text(str(n + 1))\n"
        "        res = {'content': [{'type': 'text', 'text': 'dispatched'}]}\n"
        "    else:\n"
        "        res = {'tools': [{'name': 'write', 'inputSchema': {'type': 'object', 'properties': {'x': {'type': 'integer'}}}}]}\n"
        "    print(json.dumps({'jsonrpc': '2.0', 'id': ident, 'result': res}), flush=True)\n",
        encoding="utf-8",
    )

    key = peer_box.load_or_create_messaging_key(tmp_path / "messaging.key")
    store = ExtensionStore(tmp_path / "agent", key)
    race_root = tmp_path / "race-state"
    store.upsert_mcp_server(
        {
            "id": "race",
            "command": sys.executable,
            "args": ["-u", str(script), str(race_root)],
            "enabled": True,
            "startup_timeout_ms": 5000,
        }
    )
    initial = store.get_mcp_server_internal("race", require_enabled=True)
    manager = McpSessionManager(store)
    outcome: dict[str, object] = {}

    def _invoke() -> None:
        try:
            manager.call_tool(
                "race",
                "write",
                {},
                expected_revision=initial["revision"],
                expected_config_generation=initial["config_generation"],
                expected_instance_id=initial["instance_id"],
            )
            outcome["accepted"] = True
        except BaseException as exc:
            outcome["error"] = str(exc)
            outcome["dispatched"] = getattr(exc, "dispatched", None)

    worker = threading.Thread(target=_invoke, daemon=True)
    worker.start()
    try:
        deadline = time.monotonic() + 5.0
        while not (race_root / "initializing").exists() and time.monotonic() < deadline:
            time.sleep(0.01)
        assert (race_root / "initializing").exists()
        store.upsert_mcp_server(
            {"enabled": False}, server_id="race", expected_revision=initial["revision"]
        )
        (race_root / "release").write_text("go", encoding="utf-8")
        worker.join(timeout=5.0)
        assert not worker.is_alive()
        assert "accepted" not in outcome
        assert outcome.get("dispatched") is False
        assert not (race_root / "calls").exists()

        # Re-enable, discover tools, and test schema_sha256 binding + empty enabled_tools=[] allowlist
        curr = store.get_mcp_server_internal("race")
        reenabled = store.upsert_mcp_server(
            {"enabled": True}, server_id="race", expected_revision=curr["revision"]
        )
        probed = manager.test_server("race", expected_revision=reenabled["revision"])["server"]
        tool_row = probed["tools"][0]
        schema_sha = tool_row["schema_sha256"]
        internal_after_probe = store.get_mcp_server_internal("race", require_enabled=True)

        # Matching schema_sha256 succeeds
        ok_res = manager.call_tool(
            "race",
            "write",
            {},
            expected_revision=internal_after_probe["revision"],
            expected_config_generation=internal_after_probe["config_generation"],
            expected_instance_id=internal_after_probe["instance_id"],
            expected_schema_hash=schema_sha,
        )
        assert ok_res["content"][0]["text"] == "dispatched"
        assert int((race_root / "calls").read_text()) == 1

        # Mismatched schema_sha256 fails without dispatching
        with pytest.raises(AgentError) as exc_info:
            manager.call_tool(
                "race",
                "write",
                {},
                expected_revision=internal_after_probe["revision"],
                expected_config_generation=internal_after_probe["config_generation"],
                expected_instance_id=internal_after_probe["instance_id"],
                expected_schema_hash="0" * 64,
            )
        assert exc_info.value.args == ("agent_stale_revision",)
        assert getattr(exc_info.value, "dispatched", None) is False
        assert int((race_root / "calls").read_text()) == 1

        # Empty allowlist enabled_tools=[] rejects all tools without dispatching
        empty_allow = store.upsert_mcp_server(
            {"enabled_tools": []},
            server_id="race",
            expected_revision=internal_after_probe["revision"],
        )
        assert empty_allow["tools"][0]["enabled"] is False
        internal_empty = store.get_mcp_server_internal("race", require_enabled=True)
        with pytest.raises(AgentError) as exc_info:
            manager.call_tool(
                "race",
                "write",
                {},
                expected_revision=internal_empty["revision"],
                expected_config_generation=internal_empty["config_generation"],
                expected_instance_id=internal_empty["instance_id"],
                expected_schema_hash=schema_sha,
            )
        assert exc_info.value.args == ("agent_mcp_tool_disabled",)
        assert getattr(exc_info.value, "dispatched", None) is False
        assert int((race_root / "calls").read_text()) == 1
    finally:
        manager.close()


def test_stdio_before_dispatch_callback_prevents_write(tmp_path: Path) -> None:
    script = tmp_path / "mini_stdio.py"
    script.write_text(
        "import json, pathlib, sys\n"
        "root = pathlib.Path(sys.argv[1])\n"
        "for line in sys.stdin:\n"
        "    req = json.loads(line)\n"
        "    ident = req.get('id')\n"
        "    if ident is None:\n"
        "        continue\n"
        "    if req.get('method') == 'initialize':\n"
        "        res = {'protocolVersion': '2025-03-26', 'capabilities': {}, 'serverInfo': {'name': 'm', 'version': '1'}}\n"
        "    elif req.get('method') == 'tools/list':\n"
        "        res = {'tools': [{'name': 'echo', 'inputSchema': {'type': 'object'}}]}\n"
        "    else:\n"
        "        root.mkdir(parents=True, exist_ok=True)\n"
        "        (root / 'called').write_text('1')\n"
        "        res = {'content': [{'type': 'text', 'text': 'ok'}]}\n"
        "    print(json.dumps({'jsonrpc': '2.0', 'id': ident, 'result': res}), flush=True)\n",
        encoding="utf-8",
    )
    count_root = tmp_path / "stdio-pre-dispatch"
    session = StdioMcpSession(
        {
            "id": "fixture",
            "command": sys.executable,
            "args": ["-u", str(script), str(count_root)],
            "startup_timeout_ms": 3000,
            "tool_timeout_ms": 2000,
        },
        fallback_cwd=tmp_path,
    )
    try:
        tools = [row["name"] for row in session.list_all_tools()]
        assert "echo" in tools

        def _reject_before_send() -> None:
            raise AgentToolRejected("agent_computer_access_required")

        with pytest.raises(AgentToolRejected) as exc_info:
            session.call_tool(
                "echo",
                {"marker": "NEVER_SENT"},
                before_dispatch=_reject_before_send,
            )
        assert getattr(exc_info.value, "dispatched", None) is False
        assert not (count_root / "called").exists()
    finally:
        session.close()


def test_win32_native_edit_and_password_control_isolation(tmp_path: Path) -> None:
    from types import SimpleNamespace

    from rynmesh.agent_runtime.extensions.computer_use_server import (
        ComputerUseProvider,
        dispatch_request,
    )
    from rynmesh.agent_runtime.local_desktop import Desktop
    from rynmesh.agent_runtime.native_computer import bind_owner_native_computer

    # 1. Standalone ComputerUseProvider and unbound Desktop() fail closed with agent_computer_host_required
    with pytest.raises(AgentToolRejected, match="agent_computer_host_required"):
        Desktop()

    provider = ComputerUseProvider()
    for fn, payload in (
        (provider.observe_windows, {}),
        (provider.read_window, {"hwnd": 100100}),
        (provider.act_window, {"hwnd": 100100, "action": "set_value", "element_id": "el-1", "text": "x"}),
    ):
        with pytest.raises(AgentToolRejected, match="agent_computer_host_required"):
            fn(payload)

    rpc_resp = dispatch_request(
        provider,
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": "computer_act_window", "arguments": {"hwnd": 100100, "action": "focus"}},
        },
    )
    assert rpc_resp == {
        "jsonrpc": "2.0",
        "id": 1,
        "error": {"code": -32602, "message": "agent_computer_host_required"},
    }

    # 2. NativeComputerFacade enforces password value redaction and rejects password writes
    secret_canary = "SYNTHETIC_PASSWORD_CANARY_" + uuid.uuid4().hex[:8]
    wref = {
        "hwnd": "100100",
        "windowKey": "win_100100_1",
        "pid": 4242,
        "processStartKey": "4242:100",
        "generation": 1,
        "workerInstance": "worker_inst_pwd",
    }

    class _FakeBridge:
        owner_id = "owner_pwd_test"
        _client = SimpleNamespace(active_session_info=lambda: (1, "worker_inst_pwd", 4242))
        _lock = threading.Lock()
        _tasks: dict = {}

        def list_windows(self, **_kwargs):
            return {
                "capturedAt": "2026-10-19T12:00:00Z",
                "foregroundWindow": wref,
                "windows": [
                    {
                        "window": wref,
                        "title": "Isolated Control Window",
                        "className": "TestWnd",
                        "processName": "test.exe",
                        "visible": True,
                        "enabled": True,
                        "minimized": False,
                        "maximized": False,
                        "foreground": True,
                        "cloaked": False,
                        "captureProtected": False,
                        "bounds": {"left": 10, "top": 10, "right": 410, "bottom": 210, "width": 400, "height": 200},
                        "clientBounds": {"left": 10, "top": 10, "right": 410, "bottom": 210, "width": 400, "height": 200},
                        "dpi": 96,
                    }
                ],
            }

        def get_state(self, **_kwargs):
            return {
                "observationId": "obs_pwd_1",
                "stateToken": "st_pwd_1",
                "capturedAt": "2026-10-19T12:00:01Z",
                "window": wref,
                "geometry": {
                    "windowRect": {"left": 10, "top": 10, "right": 410, "bottom": 210, "width": 400, "height": 200},
                    "clientRect": {"left": 10, "top": 10, "right": 410, "bottom": 210, "width": 400, "height": 200},
                    "dpi": 96,
                    "scaleFactor": 1.0,
                },
                "foreground": {"isTargetWindow": True},
                "accessibility": {
                    "status": "ok",
                    "treeHash": "hash_pwd_1",
                    "truncated": False,
                    "selectedElementIndices": [],
                    "elements": [
                        {
                            "index": 0,
                            "runtimeId": "42.1001",
                            "name": "Normal Edit",
                            "controlType": "Edit",
                            "automationId": "edit_normal",
                            "className": "Edit",
                            "bounds": {"left": 20, "top": 20, "right": 220, "bottom": 45, "width": 200, "height": 25},
                            "enabled": True,
                            "offscreen": False,
                            "focusable": True,
                            "focused": False,
                            "password": False,
                            "value": "READ_MARKER",
                            "depth": 1,
                            "patterns": ["ValuePattern"],
                        },
                        {
                            "index": 1,
                            "runtimeId": "42.1002",
                            "name": "Password Edit",
                            "controlType": "Edit",
                            "automationId": "edit_password",
                            "className": "Edit",
                            "bounds": {"left": 20, "top": 60, "right": 220, "bottom": 85, "width": 200, "height": 25},
                            "enabled": True,
                            "offscreen": False,
                            "focusable": True,
                            "focused": False,
                            "password": True,
                            "value": secret_canary,
                            "depth": 1,
                            "patterns": ["ValuePattern"],
                        },
                    ],
                },
                "screenshots": [],
            }

    owner = SimpleNamespace(pub="owner_pwd_test")
    facade = bind_owner_native_computer(owner)
    facade._explicit_bridge = _FakeBridge()  # type: ignore[assignment]
    ctx = {"task_id": "task_pwd", "local_execution": {"workspace_root": str(tmp_path), "desktop_access": True}}
    binding = {
        "server_id": "computer-use",
        "server_name": "Ryn Computer Use",
        "original_tool_name": "computer_read_window",
        "model_tool_name": "mcp__computer_use__computer_read_window",
        "computer_access": True,
    }
    facade.execute_mcp_read("computer_observe_windows", {}, binding=binding, operation_id="op_obs", context=ctx)
    read_out = facade.execute_mcp_read(
        "computer_read_window",
        {"hwnd": 100100, "include_screenshot": False},
        binding=binding,
        operation_id="op_read",
        context=ctx,
    )
    elements = read_out["structuredContent"]["elements"]
    normal_row = next(row for row in elements if row["index"] == 0)
    pwd_row = next(row for row in elements if row["index"] == 1)
    assert normal_row["value"] == "READ_MARKER"
    assert normal_row["password"] is False
    assert pwd_row["password"] is True
    assert pwd_row["value"] == ""
    assert secret_canary not in json.dumps(read_out)

    # Normal control preview succeeds; password control preview fails closed with agent_computer_password_field
    ok_preview = facade.preview_local_action(
        {"window_id": 100100, "action": "set_value", "element_id": 0, "text": "WRITE_MARKER"},
        "task_pwd",
        context=ctx,
    )
    assert ok_preview["element"]["index"] == 0

    with pytest.raises(AgentToolRejected) as exc_info:
        facade.preview_local_action(
            {"window_id": 100100, "action": "set_value", "element_id": 1, "text": "SHOULD_REJECT"},
            "task_pwd",
            context=ctx,
        )
    assert exc_info.value.args == ("agent_computer_password_field",)


def test_stdio_reusable_session_survives_completed_request_cancellation_and_replaces_dead_cache(
    tmp_path: Path,
) -> None:
    script = tmp_path / "stateful_stdio.py"
    script.write_text(
        "import json, os, pathlib, sys, time\n"
        "state_dir = pathlib.Path(sys.argv[1])\n"
        "state_dir.mkdir(parents=True, exist_ok=True)\n"
        "seen = []\n"
        "for line in sys.stdin:\n"
        "    req = json.loads(line)\n"
        "    ident = req.get('id')\n"
        "    if ident is None:\n"
        "        continue\n"
        "    method = req.get('method')\n"
        "    if method == 'initialize':\n"
        "        res = {'protocolVersion': '2025-03-26', 'capabilities': {}, 'serverInfo': {'name': 'stateful', 'version': '1'}}\n"
        "    elif method == 'tools/list':\n"
        "        res = {'tools': [{'name': 'step', 'inputSchema': {'type': 'object'}}]}\n"
        "    elif method == 'tools/call':\n"
        "        args = (req.get('params') or {}).get('arguments') or {}\n"
        "        marker = str(args.get('marker', ''))\n"
        "        seen.append(marker)\n"
        "        (state_dir / 'dispatched.log').open('a', encoding='utf-8').write(marker + '\\n')\n"
        "        if args.get('hang'):\n"
        "            (state_dir / 'hanging').write_text(marker, encoding='utf-8')\n"
        "            time.sleep(10.0)\n"
        "        payload = {'pid': os.getpid(), 'count': len(seen), 'seen': list(seen)}\n"
        "        res = {'content': [{'type': 'text', 'text': json.dumps(payload)}]}\n"
        "    else:\n"
        "        res = {}\n"
        "    print(json.dumps({'jsonrpc': '2.0', 'id': ident, 'result': res}), flush=True)\n",
        encoding="utf-8",
    )

    key = peer_box.load_or_create_messaging_key(tmp_path / "messaging.key")
    store = ExtensionStore(tmp_path / "agent", key)
    state_dir = tmp_path / "stdio-state"
    store.upsert_mcp_server(
        {
            "id": "stateful-stdio",
            "command": sys.executable,
            "args": ["-u", str(script), str(state_dir)],
            "enabled": True,
            "startup_timeout_ms": 5000,
            "tool_timeout_ms": 5000,
        }
    )
    srv = store.get_mcp_server_internal("stateful-stdio", require_enabled=True)
    manager = McpSessionManager(store)

    class StepCancelProbe:
        def __init__(self, label: str) -> None:
            self.label = label
            self.active = True
            self.idle_checks = 0

        def __call__(self) -> bool:
            if not self.active:
                self.idle_checks += 1
                return True
            return False

    first_cancel = StepCancelProbe("step-1")
    second_cancel = StepCancelProbe("step-2")
    before_dispatch_order: list[str] = []

    try:
        res1 = manager.call_tool(
            "stateful-stdio",
            "step",
            {"marker": "step-1"},
            expected_revision=srv["revision"],
            expected_config_generation=srv["config_generation"],
            expected_instance_id=srv["instance_id"],
            session_scope="task-lifetime",
            before_dispatch=lambda: before_dispatch_order.append("step-1"),
            cancelled=first_cancel,
        )
        data1 = json.loads(res1["content"][0]["text"])
        sess1 = manager._sessions[("stateful-stdio", "task-lifetime")]
        assert isinstance(sess1, StdioMcpSession)
        assert sess1.cancelled is not first_cancel

        # Simulate step 1 completing in runtime so its step-scoped callback turns True
        first_cancel.active = False
        time.sleep(0.22)  # > 4x the 50ms _watch_loop interval

        assert first_cancel.idle_checks == 0
        assert sess1.process.poll() is None
        assert sess1.failure is None
        assert not sess1.closed.is_set()

        # Second call with a fresh valid request callback reuses the exact same process & session state
        res2 = manager.call_tool(
            "stateful-stdio",
            "step",
            {"marker": "step-2"},
            expected_revision=srv["revision"],
            expected_config_generation=srv["config_generation"],
            expected_instance_id=srv["instance_id"],
            session_scope="task-lifetime",
            before_dispatch=lambda: before_dispatch_order.append("step-2"),
            cancelled=second_cancel,
        )
        data2 = json.loads(res2["content"][0]["text"])
        sess2 = manager._sessions[("stateful-stdio", "task-lifetime")]
        assert sess2 is sess1
        assert data2["pid"] == data1["pid"]
        assert data2["count"] == 2
        assert data2["seen"] == ["step-1", "step-2"]
        assert before_dispatch_order == ["step-1", "step-2"]

        # Pre-cancelled caller before dispatch fails with dispatched=False without sending tools/call
        # or killing the healthy cached session
        with pytest.raises(McpTransportError) as pre_exc:
            manager.call_tool(
                "stateful-stdio",
                "step",
                {"marker": "never-dispatched"},
                expected_revision=srv["revision"],
                expected_config_generation=srv["config_generation"],
                expected_instance_id=srv["instance_id"],
                session_scope="task-lifetime",
                before_dispatch=lambda: before_dispatch_order.append("never-dispatched"),
                cancelled=lambda: True,
            )
        assert pre_exc.value.args == ("agent_cancelled",)
        assert pre_exc.value.dispatched is False
        assert "never-dispatched" not in (state_dir / "dispatched.log").read_text(encoding="utf-8")
        assert sess1.is_usable()

        res3 = manager.call_tool(
            "stateful-stdio",
            "step",
            {"marker": "step-3"},
            expected_revision=srv["revision"],
            expected_config_generation=srv["config_generation"],
            expected_instance_id=srv["instance_id"],
            session_scope="task-lifetime",
            cancelled=lambda: False,
        )
        data3 = json.loads(res3["content"][0]["text"])
        assert data3["pid"] == data1["pid"]
        assert data3["count"] == 3

        # If cached process dies unexpectedly while idle, next call detects dead session and spawns a new one
        sess1._terminate()
        deadline = time.monotonic() + 2.0
        while sess1.process.poll() is None and time.monotonic() < deadline:
            time.sleep(0.02)
        assert sess1.process.poll() is not None
        assert not sess1.is_usable()

        res4 = manager.call_tool(
            "stateful-stdio",
            "step",
            {"marker": "step-after-crash"},
            expected_revision=srv["revision"],
            expected_config_generation=srv["config_generation"],
            expected_instance_id=srv["instance_id"],
            session_scope="task-lifetime",
            cancelled=lambda: False,
        )
        data4 = json.loads(res4["content"][0]["text"])
        sess4 = manager._sessions[("stateful-stdio", "task-lifetime")]
        assert sess4 is not sess1
        assert data4["pid"] != data1["pid"]
        assert data4["count"] == 1
        assert data4["seen"] == ["step-after-crash"]

        # Live cancellation during an in-flight tools/call terminates the session promptly
        live_cancel = threading.Event()
        hang_outcome: dict[str, object] = {}

        def _run_hanging_call() -> None:
            try:
                manager.call_tool(
                    "stateful-stdio",
                    "step",
                    {"marker": "step-hang", "hang": True},
                    expected_revision=srv["revision"],
                    expected_config_generation=srv["config_generation"],
                    expected_instance_id=srv["instance_id"],
                    session_scope="task-lifetime",
                    cancelled=live_cancel.is_set,
                )
            except BaseException as exc:
                hang_outcome["error"] = exc

        worker = threading.Thread(target=_run_hanging_call, daemon=True)
        worker.start()
        wait_deadline = time.monotonic() + 3.0
        while not (state_dir / "hanging").exists() and time.monotonic() < wait_deadline:
            time.sleep(0.02)
        assert (state_dir / "hanging").exists()
        live_cancel.set()
        worker.join(timeout=2.0)
        assert not worker.is_alive()
        hang_err = hang_outcome.get("error")
        assert isinstance(hang_err, McpTransportError)
        assert hang_err.args == ("agent_cancelled",)
        assert hang_err.dispatched is True
    finally:
        manager.close()


def test_http_reusable_session_survives_completed_request_cancellation(tmp_path: Path) -> None:
    state: dict[str, object] = {"init_count": 0, "calls": []}

    class StatefulHttpHandler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *_args) -> None:
            pass

        def do_DELETE(self) -> None:
            self.send_response(204)
            self.send_header("Content-Length", "0")
            self.end_headers()

        def do_POST(self) -> None:
            length = int(self.headers.get("Content-Length", "0"))
            req = json.loads(self.rfile.read(length))
            ident = req.get("id")
            method = req.get("method")
            if ident is None:
                self.send_response(202)
                self.send_header("Content-Length", "0")
                self.end_headers()
                return

            if method == "initialize":
                state["init_count"] = int(state["init_count"]) + 1
                result = {
                    "protocolVersion": "2025-03-26",
                    "capabilities": {},
                    "serverInfo": {"name": "http-stateful", "version": "1"},
                }
            elif method == "tools/call":
                args = (req.get("params") or {}).get("arguments") or {}
                marker = str(args.get("marker", ""))
                calls_list = state["calls"]
                assert isinstance(calls_list, list)
                calls_list.append(marker)
                if args.get("slow"):
                    time.sleep(1.5)
                result = {
                    "content": [
                        {
                            "type": "text",
                            "text": json.dumps(
                                {
                                    "init_count": state["init_count"],
                                    "calls": list(calls_list),
                                    "session_id": self.headers.get("Mcp-Session-Id"),
                                }
                            ),
                        }
                    ]
                }
            else:
                result = {"tools": [{"name": "step", "inputSchema": {"type": "object"}}]}

            body = json.dumps({"jsonrpc": "2.0", "id": ident, "result": result}).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Mcp-Session-Id", "http-sess-1")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            try:
                self.wfile.write(body)
            except OSError:
                pass

    http_server = ThreadingHTTPServer(("127.0.0.1", 0), StatefulHttpHandler)
    http_server.daemon_threads = True
    thread = threading.Thread(target=http_server.serve_forever, daemon=True)
    thread.start()

    key = peer_box.load_or_create_messaging_key(tmp_path / "messaging.key")
    store = ExtensionStore(tmp_path / "agent", key)
    store.upsert_mcp_server(
        {
            "id": "stateful-http",
            "transport": "streamable_http",
            "url": f"http://127.0.0.1:{http_server.server_port}/mcp",
            "enabled": True,
            "startup_timeout_ms": 3000,
            "tool_timeout_ms": 3000,
        }
    )
    srv = store.get_mcp_server_internal("stateful-http", require_enabled=True)
    manager = McpSessionManager(store)

    first_done = {"cancelled": False}
    try:
        res1 = manager.call_tool(
            "stateful-http",
            "step",
            {"marker": "http-1"},
            expected_revision=srv["revision"],
            expected_config_generation=srv["config_generation"],
            expected_instance_id=srv["instance_id"],
            session_scope="http-task",
            cancelled=lambda: first_done["cancelled"],
        )
        data1 = json.loads(res1["content"][0]["text"])
        sess1 = manager._sessions[("stateful-http", "http-task")]
        assert isinstance(sess1, StreamableHttpMcpSession)
        assert data1["init_count"] == 1
        assert data1["calls"] == ["http-1"]

        # First request scope ends and its callback permanently returns True
        first_done["cancelled"] = True

        # Second request with active False callback reuses the same HTTP session without re-initializing
        res2 = manager.call_tool(
            "stateful-http",
            "step",
            {"marker": "http-2"},
            expected_revision=srv["revision"],
            expected_config_generation=srv["config_generation"],
            expected_instance_id=srv["instance_id"],
            session_scope="http-task",
            cancelled=lambda: False,
        )
        data2 = json.loads(res2["content"][0]["text"])
        sess2 = manager._sessions[("stateful-http", "http-task")]
        assert sess2 is sess1
        assert data2["init_count"] == 1
        assert data2["session_id"] == "http-sess-1"
        assert data2["calls"] == ["http-1", "http-2"]

        # Pre-cancelled request fails with dispatched=False before sending tools/call
        with pytest.raises(McpTransportError) as pre_exc:
            manager.call_tool(
                "stateful-http",
                "step",
                {"marker": "http-never"},
                expected_revision=srv["revision"],
                expected_config_generation=srv["config_generation"],
                expected_instance_id=srv["instance_id"],
                session_scope="http-task",
                cancelled=lambda: True,
            )
        assert pre_exc.value.args == ("agent_cancelled",)
        assert pre_exc.value.dispatched is False
        assert state["calls"] == ["http-1", "http-2"]
    finally:
        manager.close()
        http_server.shutdown()
        http_server.server_close()
        thread.join(timeout=2)


def test_concurrent_same_key_first_connect_preserves_winner_and_retires_init_candidates(
    tmp_path: Path,
) -> None:
    key = peer_box.load_or_create_messaging_key(tmp_path / "messaging.key")
    ext_store = ExtensionStore(tmp_path / "agent", key)

    script = tmp_path / "concurrent_init_server.py"
    fixture_dir = tmp_path / "fixture_state"
    fixture_dir.mkdir(parents=True, exist_ok=True)
    script.write_text(
        """import json, os, sys, time
from pathlib import Path

root = Path(sys.argv[1])
mode = sys.argv[2]
pid = os.getpid()
calls = 0

def wait_marker(name, timeout=10.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if (root / name).exists():
            return
        time.sleep(0.01)

for raw in sys.stdin:
    line = raw.strip()
    if not line:
        continue
    req = json.loads(line)
    ident = req.get("id")
    method = req.get("method")
    if ident is None:
        continue
    if method == "initialize":
        (root / f"init-{mode}-{pid}.json").write_text(json.dumps({"pid": pid}), encoding="utf-8")
        if mode.startswith("hold-init"):
            wait_marker(f"release-{mode}")
        res = {
            "protocolVersion": "2025-03-26",
            "capabilities": {"tools": {}},
            "serverInfo": {"name": "ConcurrentInit", "version": "1.0"},
        }
    elif method == "tools/call":
        calls += 1
        label = req["params"]["arguments"]["label"]
        (root / f"received-{label}.json").write_text(json.dumps({"pid": pid, "id": ident}), encoding="utf-8")
        if label == "A" and mode == "race":
            wait_marker("release-A")
        res = {
            "content": [{"type": "text", "text": label}],
            "structuredContent": {"label": label, "pid": pid, "calls": calls},
            "isError": False,
        }
    else:
        res = {"tools": []}
    print(json.dumps({"jsonrpc": "2.0", "id": ident, "result": res}), flush=True)
""",
        encoding="utf-8",
    )

    def _setup_server(server_id: str, mode: str) -> tuple[McpSessionManager, dict]:
        ov = ext_store.overview()
        ext_store.upsert_mcp_server(
            {
                "id": server_id,
                "name": server_id,
                "transport": "stdio",
                "command": sys.executable,
                "args": ["-u", str(script), str(fixture_dir), mode],
                "enabled": True,
                "read_only_tools": ["echo"],
            },
            expected_revision=ov["revision"],
        )
        info = ext_store.get_mcp_server_internal(server_id, require_enabled=True)
        ext_store.record_mcp_discovery(
            server_id,
            expected_revision=info["revision"],
            expected_config_generation=info["config_generation"],
            expected_instance_id=info["instance_id"],
            status="ready",
            discovered_tools=[
                {
                    "name": "echo",
                    "inputSchema": {
                        "type": "object",
                        "properties": {"label": {"type": "string"}},
                        "required": ["label"],
                        "additionalProperties": False,
                    },
                }
            ],
        )
        srv = ext_store.get_mcp_server_internal(server_id, require_enabled=True)
        return McpSessionManager(ext_store), srv

    # 1 & 2: Concurrent same-key first-connect race + same-task reuse + cross-task isolation + forget_task
    manager, srv = _setup_server("race-srv", "race")
    created_sessions: list[StdioMcpSession] = []
    a_init_done = threading.Event()
    b_created = threading.Event()
    orig_create = manager._create_session

    def _wrapped_create(server, **kwargs):
        label = threading.current_thread().name
        sess = orig_create(server, **kwargs)
        created_sessions.append(sess)
        if label == "B":
            b_created.set()
        orig_init = sess.initialize

        def _synced_init(*, cancelled=None):
            out = orig_init(cancelled=cancelled)
            if label == "A":
                a_init_done.set()
                b_created.wait(0.75)
            elif label == "B":
                deadline = time.monotonic() + 8.0
                while time.monotonic() < deadline and not (fixture_dir / "received-A.json").exists():
                    time.sleep(0.01)
            return out

        sess.initialize = _synced_init
        return sess

    manager._create_session = _wrapped_create
    outcomes: dict[str, dict] = {}

    def _run_call(label: str, scope: str = "task-A", cancelled=lambda: False):
        try:
            res = manager.call_tool(
                "race-srv",
                "echo",
                {"label": label},
                expected_revision=srv["revision"],
                expected_config_generation=srv["config_generation"],
                expected_instance_id=srv["instance_id"],
                expected_schema_hash=srv["discovered_tools"][0]["schema_sha256"],
                session_scope=scope,
                cancelled=cancelled,
            )
            outcomes[label] = {"ok": True, "res": res}
        except BaseException as exc:
            outcomes[label] = {"ok": False, "code": str(exc), "dispatched": getattr(exc, "dispatched", None)}

    try:
        t_a = threading.Thread(target=_run_call, args=("A", "task-A"), name="A", daemon=True)
        t_a.start()
        assert a_init_done.wait(8.0)
        t_b = threading.Thread(target=_run_call, args=("B", "task-A"), name="B", daemon=True)
        t_b.start()

        deadline = time.monotonic() + 8.0
        while time.monotonic() < deadline and not (fixture_dir / "received-A.json").exists():
            time.sleep(0.01)
        assert (fixture_dir / "received-A.json").exists()
        time.sleep(0.25)
        (fixture_dir / "release-A").write_text("ok", encoding="utf-8")

        t_a.join(10.0)
        t_b.join(10.0)
        assert outcomes["A"]["ok"] is True
        assert outcomes["B"]["ok"] is True
        pid_a = outcomes["A"]["res"]["structuredContent"]["pid"]
        pid_b = outcomes["B"]["res"]["structuredContent"]["pid"]
        assert pid_a == pid_b

        _run_call("C", "task-A")
        _run_call("D", "task-B")
        assert outcomes["C"]["ok"] is True
        assert outcomes["D"]["ok"] is True
        assert outcomes["C"]["res"]["structuredContent"]["pid"] == pid_a
        assert outcomes["D"]["res"]["structuredContent"]["pid"] != pid_a

        manager.forget_task("task-A")
        assert ("race-srv", "task-A") not in manager._sessions
        _run_call("E", "task-B")
        assert outcomes["E"]["ok"] is True
        assert outcomes["E"]["res"]["structuredContent"]["pid"] == outcomes["D"]["res"]["structuredContent"]["pid"]
    finally:
        (fixture_dir / "release-A").write_text("ok", encoding="utf-8")
        manager.close()
        for s in created_sessions:
            s.close()

    # 3: Cancelled waiter B during initialize does not poison or close owner A
    mgr_w, srv_w = _setup_server("srv-waiter", "hold-init-waiter")
    waiter_sessions: list[StdioMcpSession] = []
    orig_create_w = mgr_w._create_session

    def _track_w(server, **kwargs):
        s = orig_create_w(server, **kwargs)
        waiter_sessions.append(s)
        return s

    mgr_w._create_session = _track_w
    waiter_outcomes: dict[str, dict] = {}
    cancel_b = threading.Event()

    def _call_w(label: str, cancelled_fn):
        try:
            res = mgr_w.call_tool(
                "srv-waiter",
                "echo",
                {"label": label},
                expected_revision=srv_w["revision"],
                expected_config_generation=srv_w["config_generation"],
                expected_instance_id=srv_w["instance_id"],
                expected_schema_hash=srv_w["discovered_tools"][0]["schema_sha256"],
                session_scope="task-W",
                cancelled=cancelled_fn,
            )
            waiter_outcomes[label] = {"ok": True, "res": res}
        except BaseException as exc:
            waiter_outcomes[label] = {"ok": False, "code": str(exc), "dispatched": getattr(exc, "dispatched", None)}

    try:
        t_owner = threading.Thread(target=_call_w, args=("owner", lambda: False), daemon=True)
        t_owner.start()
        deadline = time.monotonic() + 8.0
        while time.monotonic() < deadline and not list(fixture_dir.glob("init-hold-init-waiter-*.json")):
            time.sleep(0.01)
        t_waiter = threading.Thread(target=_call_w, args=("waiter", cancel_b.is_set), daemon=True)
        t_waiter.start()
        time.sleep(0.15)
        cancel_b.set()
        (fixture_dir / "release-hold-init-waiter").write_text("ok", encoding="utf-8")
        t_owner.join(8.0)
        t_waiter.join(8.0)
        assert waiter_outcomes["owner"]["ok"] is True
        assert waiter_outcomes["waiter"]["ok"] is False
        assert waiter_outcomes["waiter"]["dispatched"] is False
        assert not (fixture_dir / "received-waiter.json").exists()
    finally:
        (fixture_dir / "release-hold-init-waiter").write_text("ok", encoding="utf-8")
        mgr_w.close()
        for s in waiter_sessions:
            s.close()

    # 4 & 5 & 6 & 7: Revocation, request cancellation, forget_task, and manager.close() during initialize
    req_cancel = threading.Event()
    for mode_suffix, action_fn, cancel_cb in (
        (
            "hold-init-revoke",
            lambda m, s: ext_store.upsert_mcp_server(
                {"enabled": False}, server_id=s["id"], expected_revision=s["revision"]
            ),
            lambda: False,
        ),
        ("hold-init-cancel", lambda _m, _s: req_cancel.set(), req_cancel.is_set),
        ("hold-init-forget", lambda m, _s: m.forget_task("task-retire"), lambda: False),
        ("hold-init-close", lambda m, _s: m.close(), lambda: False),
    ):
        mgr_r, srv_r = _setup_server(f"srv-{mode_suffix}", mode_suffix)
        retire_sessions: list[StdioMcpSession] = []
        orig_create_r = mgr_r._create_session

        def _track_create(server, **kwargs):
            s = orig_create_r(server, **kwargs)
            retire_sessions.append(s)
            return s

        mgr_r._create_session = _track_create
        retire_outcome: dict[str, object] = {}
        dispatched_flag = {"called": False}

        def _retiring_call(c_cb=cancel_cb):
            try:
                mgr_r.call_tool(
                    f"srv-{mode_suffix}",
                    "echo",
                    {"label": f"never-{mode_suffix}"},
                    expected_revision=srv_r["revision"],
                    expected_config_generation=srv_r["config_generation"],
                    expected_instance_id=srv_r["instance_id"],
                    expected_schema_hash=srv_r["discovered_tools"][0]["schema_sha256"],
                    session_scope="task-retire",
                    before_dispatch=lambda: dispatched_flag.update(called=True),
                    cancelled=c_cb,
                )
                retire_outcome["ok"] = True
            except BaseException as exc:
                retire_outcome["ok"] = False
                retire_outcome["dispatched"] = getattr(exc, "dispatched", None)

        try:
            t_r = threading.Thread(target=_retiring_call, daemon=True)
            t_r.start()
            deadline = time.monotonic() + 8.0
            while time.monotonic() < deadline and not list(fixture_dir.glob(f"init-{mode_suffix}-*.json")):
                time.sleep(0.01)
            assert list(fixture_dir.glob(f"init-{mode_suffix}-*.json"))
            action_fn(mgr_r, srv_r)
            (fixture_dir / f"release-{mode_suffix}").write_text("ok", encoding="utf-8")
            t_r.join(8.0)
            assert not t_r.is_alive()
            assert retire_outcome.get("ok") is False
            assert retire_outcome.get("dispatched") is False
            assert dispatched_flag["called"] is False
            assert not (fixture_dir / f"received-never-{mode_suffix}.json").exists()
            assert (f"srv-{mode_suffix}", "task-retire") not in mgr_r._sessions
        finally:
            (fixture_dir / f"release-{mode_suffix}").write_text("ok", encoding="utf-8")
            mgr_r.close()
            for s in retire_sessions:
                s.close()
