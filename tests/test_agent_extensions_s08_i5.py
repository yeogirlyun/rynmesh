"""Tests for S08-I5: MCP timeout defaults/caps, error details, pagination cap, and Degraded catalog."""

from __future__ import annotations

import json
import socket
import socketserver
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey

from rynmesh.agent_runtime.extensions import ExtensionToolAdapter
from rynmesh.agent_runtime.extensions.imports import ExtensionImportManager
from rynmesh.agent_runtime.extensions.mcp_transport import (
    McpSessionManager,
    McpTransportError,
)
from rynmesh.agent_runtime.extensions.store import ExtensionStore
from rynmesh.agent_runtime.local_tools import LocalTools
from rynmesh.agent_runtime.operation_ledger import OperationLedger
from rynmesh.agent_runtime.runtime import AgentRuntime
from rynmesh.agent_runtime.store import AgentStore
from rynmesh.services import peer_box


def _assistant_calls(*calls: tuple[str, dict[str, Any], str]) -> dict[str, Any]:
    return {
        "message": {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {
                    "id": call_id,
                    "type": "function",
                    "function": {"name": name, "arguments": json.dumps(args)},
                }
                for name, args, call_id in calls
            ],
        }
    }


def _assistant_text(text: str) -> dict[str, Any]:
    return {"message": {"role": "assistant", "content": text}}


def _receipts(runtime: AgentRuntime, task_id: str) -> list[dict[str, Any]]:
    return runtime.store.read()["tasks"][task_id].get("receipts", [])


class _Catalog:
    def list_models(self) -> list[dict[str, Any]]:
        return [
            {
                "id": "local:test",
                "name": "Test Model",
                "source": "local_api",
                "available": True,
                "agent_supported": True,
            }
        ]


class _Model:
    supports_image_input = True

    def __init__(self, *responses: Any) -> None:
        self.responses = list(responses)
        self.calls: list[tuple[Any, ...]] = []

    def complete(self, model_id, messages, tool_specs, cancelled, on_event, **kwargs):
        self.calls.append((model_id, messages, tool_specs, kwargs))
        if not self.responses:
            return _assistant_text("Done.")
        nxt = self.responses.pop(0)
        if callable(nxt):
            return nxt(model_id, messages, tool_specs, cancelled, on_event, **kwargs)
        return nxt


class _HarnessTools:
    def __init__(self, home: Path, agent_store: AgentStore) -> None:
        self.ledger = OperationLedger(home)
        node_store = SimpleNamespace(home=home)
        self.local = LocalTools(node_store, lambda: self.ledger)
        self.local.store = agent_store
        self.extensions = ExtensionToolAdapter(self.local, lambda: self.ledger)
        self.extensions.bind_agent_store(agent_store)

    def specs(self) -> list[dict[str, Any]]:
        from rynmesh.agent_runtime.local_specs import SPECS as LOCAL_SPECS

        local_specs = [
            {
                "type": "function",
                "function": {"name": name, "description": row[0], "parameters": row[3]},
            }
            for name, row in LOCAL_SPECS.items()
        ]
        return local_specs + self.extensions.specs()

    def metadata(self, name: str) -> dict[str, Any]:
        from rynmesh.agent_runtime.local_specs import NAMES as LOCAL_NAMES
        from rynmesh.agent_runtime.local_specs import SPECS as LOCAL_SPECS

        if self.extensions.handles(name):
            return self.extensions.metadata(name)
        if name in LOCAL_NAMES:
            description, effect, risk, _ = LOCAL_SPECS[name]
            return {
                "effect": effect,
                "title": description,
                "risk": risk,
                "scope_context_supported": True,
                "supports_preview": effect == "write",
                "preview_precondition": effect == "write",
            }
        raise ValueError("unknown_tool")

    def verify_extension_binding(self, call_or_binding: dict[str, Any]) -> dict[str, Any]:
        return self.extensions.verify_binding(call_or_binding)

    def preview(self, name: str, arguments: dict[str, Any], *, context: dict[str, Any] | None = None) -> dict[str, Any]:
        if self.extensions.handles(name):
            return self.extensions.preview(name, arguments, context=context)
        return self.local.preview(name, arguments, context)

    def execute(
        self,
        name: str,
        arguments: dict[str, Any],
        *,
        operation_id: str,
        cancelled,
        expected_preview: dict[str, Any] | None = None,
        context: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        if self.extensions.handles(name):
            return self.extensions.execute(
                name,
                arguments,
                operation_id=operation_id,
                context=context,
                expected_preview=expected_preview,
                cancelled=cancelled,
            )
        return self.local.execute(
            name,
            arguments,
            operation_id=operation_id,
            context=context,
            expected_preview=expected_preview,
            cancelled=cancelled,
        )

    def reconcile(self, operation_id: str, *, context: dict[str, Any], cancelled) -> dict[str, Any]:
        op = context.get("operation") or {}
        if self.extensions.handles(str(op.get("tool") or "")) or isinstance(op.get("extension_binding"), dict):
            return self.extensions.reconcile(operation_id, context)
        return self.local.reconcile(operation_id, context)

    def forget_task(self, task_id: str, *, operation_ids=()) -> None:
        self.local.forget_baselines(task_id)
        self.extensions.forget_task(task_id, operation_ids=operation_ids)

    def close(self) -> None:
        self.local.processes.close()
        self.extensions.close()


class _LoopbackHTTPServer(ThreadingHTTPServer):
    """Loopback test HTTP server that skips reverse-DNS getfqdn stalls on Windows VMs."""

    daemon_threads = True

    def server_bind(self) -> None:
        socketserver.TCPServer.server_bind(self)
        host, port = self.server_address[:2]
        self.server_name = str(host)
        self.server_port = int(port)

    def handle_error(self, request: Any, client_address: Any) -> None:
        pass


class _QuietHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *_args) -> None:
        pass

    def address_string(self) -> str:
        return str(self.client_address[0])

    def handle(self) -> None:
        try:
            super().handle()
        except OSError:
            self.close_connection = True


@pytest.fixture(autouse=True)
def _fast_loopback_fqdn(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(socket, "getfqdn", lambda _host="": "localhost")


@pytest.fixture
def s08_env(tmp_path: Path):
    home = tmp_path / "node_home"
    workspace = tmp_path / "workspace"
    home.mkdir()
    workspace.mkdir()
    key = X25519PrivateKey.generate()
    agent_store = AgentStore(home / "agent", key)
    tools = _HarnessTools(home, agent_store)
    model = _Model()
    runtime = AgentRuntime(agent_store, model, tools, _Catalog())
    runtime.update_settings(
        {
            "enabled": True,
            "model_id": "local:test",
            "workspace_root": str(workspace),
            "permission_mode": "automatic",
            "desktop_access": False,
        }
    )
    yield runtime, model, tools, workspace, home
    tools.close()


def test_store_timeout_defaults_clamping_and_existing_preservation(tmp_path: Path) -> None:
    from rynmesh.agent_runtime.extensions import store as ext_store_mod

    assert ext_store_mod.DEFAULT_STARTUP_TIMEOUT_MS == 30000
    assert ext_store_mod.MAX_STARTUP_TIMEOUT_MS == 300000
    assert ext_store_mod.DEFAULT_TOOL_TIMEOUT_MS == 300000
    assert ext_store_mod.MAX_TOOL_TIMEOUT_MS == 3600000

    key = peer_box.load_or_create_messaging_key(tmp_path / "messaging.key")
    store = ExtensionStore(tmp_path / "agent", key)

    ov = store.overview()
    cu = next(s for s in ov["mcp_servers"] if s["id"] == "computer-use")
    assert cu["startup_timeout_ms"] == 30000
    assert cu["tool_timeout_ms"] == 300000

    created = store.upsert_mcp_server(
        {
            "id": "default-srv",
            "name": "Default Server",
            "transport": "streamable_http",
            "url": "http://127.0.0.1:9999/mcp",
            "enabled": True,
        }
    )
    assert created["startup_timeout_ms"] == 30000
    assert created["tool_timeout_ms"] == 300000

    clamped_high = store.upsert_mcp_server(
        {
            "startup_timeout_ms": 999999,
            "tool_timeout_ms": 9999999,
        },
        server_id="default-srv",
        expected_revision=created["revision"],
    )
    assert clamped_high["startup_timeout_ms"] == 300000
    assert clamped_high["tool_timeout_ms"] == 3600000

    clamped_low = store.upsert_mcp_server(
        {
            "startup_timeout_ms": 100,
            "tool_timeout_ms": 200,
        },
        server_id="default-srv",
        expected_revision=clamped_high["revision"],
    )
    assert clamped_low["startup_timeout_ms"] == 1000
    assert clamped_low["tool_timeout_ms"] == 1000

    # Saved legacy values (e.g. 10000 / 30000) remain intact when editing unrelated fields
    legacy = store.upsert_mcp_server(
        {
            "id": "legacy-srv",
            "name": "Legacy",
            "transport": "streamable_http",
            "url": "http://127.0.0.1:9998/mcp",
            "startup_timeout_ms": 10000,
            "tool_timeout_ms": 30000,
        }
    )
    updated_legacy = store.upsert_mcp_server(
        {"name": "Legacy Renamed"},
        server_id="legacy-srv",
        expected_revision=legacy["revision"],
    )
    assert updated_legacy["startup_timeout_ms"] == 10000
    assert updated_legacy["tool_timeout_ms"] == 30000


def test_imports_tool_timeout_sec_900_and_defaults(tmp_path: Path) -> None:
    key = peer_box.load_or_create_messaging_key(tmp_path / "messaging.key")
    store = ExtensionStore(tmp_path / "agent", key)
    importer = ExtensionImportManager(store)

    toml_cfg = """
[mcp_servers.slow_analytics]
url = "http://127.0.0.1:8765/mcp"
tool_timeout_sec = 900

[mcp_servers.default_srv]
url = "http://127.0.0.1:8766/mcp"
"""
    preview = importer.preview(
        {
            "kind": "mcp",
            "filename": "config.toml",
            "config_text": toml_cfg,
        }
    )
    assert preview["can_import"] is True

    committed = importer.commit(
        preview["preview_id"],
        expected_revision=preview["expected_revision"],
    )
    committed_by_id = {srv["id"]: srv for srv in committed["mcp_servers"]}
    assert committed_by_id["slow_analytics"]["startup_timeout_ms"] == 30000
    assert committed_by_id["slow_analytics"]["tool_timeout_ms"] == 900000
    assert committed_by_id["default_srv"]["startup_timeout_ms"] == 30000
    assert committed_by_id["default_srv"]["tool_timeout_ms"] == 300000


def test_manager_call_tool_timeout_ms_takes_min_of_server_and_request(tmp_path: Path) -> None:
    stop_event = threading.Event()

    class SlowHttpHandler(_QuietHandler):
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
                res = {
                    "protocolVersion": "2025-03-26",
                    "capabilities": {"tools": {}},
                    "serverInfo": {"name": "slow-http", "version": "1"},
                }
            elif method == "tools/call":
                sleep_s = float(((req.get("params") or {}).get("arguments") or {}).get("sleep", 0.0))
                if sleep_s > 0:
                    stop_event.wait(sleep_s)
                res = {"content": [{"type": "text", "text": "ok"}]}
            else:
                res = {"tools": [{"name": "wait_op", "inputSchema": {"type": "object"}}]}
            body = json.dumps({"jsonrpc": "2.0", "id": ident, "result": res}).encode("utf-8")
            try:
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            except OSError:
                self.close_connection = True

    http_server = _LoopbackHTTPServer(("127.0.0.1", 0), SlowHttpHandler)
    thread = threading.Thread(target=http_server.serve_forever, daemon=True)
    thread.start()

    key = peer_box.load_or_create_messaging_key(tmp_path / "messaging.key")
    store = ExtensionStore(tmp_path / "agent", key)
    store.upsert_mcp_server(
        {
            "id": "min-timeout-srv",
            "transport": "streamable_http",
            "url": f"http://127.0.0.1:{http_server.server_port}/mcp",
            "enabled": True,
            "startup_timeout_ms": 3000,
            "tool_timeout_ms": 5000,
        }
    )
    srv = store.get_mcp_server_internal("min-timeout-srv", require_enabled=True)
    manager = McpSessionManager(store)
    try:
        # Request timeout (250ms) < server timeout (5000ms) -> 0.25s wins
        t0 = time.monotonic()
        with pytest.raises(McpTransportError) as exc_req:
            manager.call_tool(
                "min-timeout-srv",
                "wait_op",
                {"sleep": 1.0},
                expected_revision=srv["revision"],
                expected_config_generation=srv["config_generation"],
                expected_instance_id=srv["instance_id"],
                timeout_ms=250,
            )
        elapsed = time.monotonic() - t0
        assert str(exc_req.value) == "agent_mcp_timeout"
        assert elapsed < 1.5
        assert exc_req.value.details is not None
        assert exc_req.value.details["timeout_seconds"] == pytest.approx(0.25, abs=0.05)
        assert exc_req.value.details["method"] == "tools/call"
        assert exc_req.value.details["phase"] == "await_response"

        # Server timeout (1000ms) < request timeout (10000ms) -> 1.0s wins
        store.upsert_mcp_server(
            {"tool_timeout_ms": 1000},
            server_id="min-timeout-srv",
            expected_revision=srv["revision"],
        )
        manager.invalidate_server("min-timeout-srv")
        srv2 = store.get_mcp_server_internal("min-timeout-srv", require_enabled=True)
        t1 = time.monotonic()
        with pytest.raises(McpTransportError) as exc_srv:
            manager.call_tool(
                "min-timeout-srv",
                "wait_op",
                {"sleep": 2.0},
                expected_revision=srv2["revision"],
                expected_config_generation=srv2["config_generation"],
                expected_instance_id=srv2["instance_id"],
                timeout_ms=10000,
            )
        elapsed2 = time.monotonic() - t1
        assert str(exc_srv.value) == "agent_mcp_timeout"
        assert elapsed2 < 1.8
        assert exc_srv.value.details is not None
        assert exc_srv.value.details["timeout_seconds"] == pytest.approx(1.0, abs=0.05)
    finally:
        stop_event.set()
        manager.close()
        http_server.shutdown()
        http_server.server_close()
        thread.join(timeout=2)


def test_http_rpc_error_timeout_and_500_body_in_tool_result_details(s08_env) -> None:
    runtime, model, tools, _workspace, _home = s08_env
    secret_auth_header = "Bearer mock-secret-credential-xyz987"
    long_b64_secret = "dGhpcyBpcyBhIHZlcnkgbG9uZyBiYXNlNjQgc2VjcmV0IHRva2VuIDEyMzQ1Ng=="

    class ErrorModesHandler(_QuietHandler):
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
                body = json.dumps(
                    {
                        "jsonrpc": "2.0",
                        "id": ident,
                        "result": {
                            "protocolVersion": "2025-03-26",
                            "capabilities": {"tools": {}},
                            "serverInfo": {"name": "err-http", "version": "1"},
                        },
                    }
                ).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return
            if method == "tools/list":
                body = json.dumps(
                    {
                        "jsonrpc": "2.0",
                        "id": ident,
                        "result": {
                            "tools": [
                                {
                                    "name": "probe_mode",
                                    "description": "Trigger error modes",
                                    "inputSchema": {
                                        "type": "object",
                                        "properties": {"mode": {"type": "string"}},
                                        "required": ["mode"],
                                    },
                                }
                            ]
                        },
                    }
                ).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return

            mode = ((req.get("params") or {}).get("arguments") or {}).get("mode")
            if mode == "rpc_error":
                raw_msg = "Upstream\x00query\nfailed: " + ("detail-" * 100)
                body = json.dumps(
                    {
                        "jsonrpc": "2.0",
                        "id": ident,
                        "error": {"code": -32000, "message": raw_msg},
                    }
                ).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return
            if mode == "timeout":
                time.sleep(1.5)
                return
            if mode == "http_500":
                auth_echo = self.headers.get("Authorization", "")
                err_text = (
                    f"Gateway 500\x07 error; echoed_auth={auth_echo}; "
                    f"raw_b64={long_b64_secret}; "
                    + ("E" * 1500)
                )
                raw = err_text.encode("utf-8")
                self.send_response(500)
                self.send_header("Content-Type", "text/plain; charset=utf-8")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                try:
                    self.wfile.write(raw)
                except OSError:
                    pass

    http_server = _LoopbackHTTPServer(("127.0.0.1", 0), ErrorModesHandler)
    thread = threading.Thread(target=http_server.serve_forever, daemon=True)
    thread.start()

    try:
        ext_store = tools.extensions.store
        ov = ext_store.overview()
        srv = ext_store.upsert_mcp_server(
            {
                "id": "err-srv",
                "name": "Error Server",
                "transport": "streamable_http",
                "url": f"http://127.0.0.1:{http_server.server_port}/mcp",
                "enabled": True,
                "startup_timeout_ms": 3000,
                "tool_timeout_ms": 1000,
                "secret_headers": {"Authorization": secret_auth_header},
                "read_only_tools": ["probe_mode"],
            },
            expected_revision=ov["revision"],
        )
        probed = tools.extensions.manager.test_server("err-srv", expected_revision=srv["revision"])
        tool_name = probed["server"]["tools"][0]["model_tool_name"]

        model.responses = [
            _assistant_calls(("ryn_discover_tools", {"load": [tool_name]}, "c_disc")),
            _assistant_calls((tool_name, {"mode": "rpc_error"}, "c_rpc")),
            _assistant_calls((tool_name, {"mode": "timeout"}, "c_to")),
            _assistant_calls((tool_name, {"mode": "http_500"}, "c_500")),
            _assistant_text("Finished."),
        ]
        task = runtime.create({"message": "Test MCP error details"})
        for _ in range(12):
            if runtime.store.read()["tasks"][task["id"]]["status"] == "completed":
                break
            runtime.run_once()

        receipts = [r for r in _receipts(runtime, task["id"]) if r["tool"] == tool_name]
        assert len(receipts) == 3

        # 1. JSON-RPC error message cleaned & truncated <= 500 chars
        rpc_res = receipts[0]["result"]
        assert rpc_res["error"] == "agent_mcp_rpc_error"
        assert "details" in rpc_res
        rpc_msg = rpc_res["details"]["message"]
        assert "Upstream query failed:" in rpc_msg
        assert "\x00" not in rpc_msg and "\n" not in rpc_msg
        assert len(rpc_msg) <= 500

        # 2. Timeout error carries phase, timeout_seconds, method
        to_res = receipts[1]["result"]
        assert to_res["error"] == "agent_mcp_timeout"
        assert "details" in to_res
        assert to_res["details"]["timeout_seconds"] == pytest.approx(1.0, abs=0.05)
        assert to_res["details"]["phase"] == "await_response"
        assert to_res["details"]["method"] == "tools/call"

        # 3. HTTP 500 carries http_status and truncated http_body without Authorization or base64 secret
        http_res = receipts[2]["result"]
        assert http_res["error"] == "agent_mcp_http_error"
        assert "details" in http_res
        assert http_res["details"]["http_status"] == 500
        http_body = http_res["details"]["http_body"]
        assert "Gateway 500" in http_body
        assert "EEE" in http_body
        assert "\x07" not in http_body
        assert len(http_body) <= 1000
        assert "mock-secret-credential-xyz987" not in http_body
        assert long_b64_secret not in http_body
    finally:
        http_server.shutdown()
        http_server.server_close()
        thread.join(timeout=2)


def test_list_all_tools_pagination_20s_pages_and_degraded_catalog_preserved(
    s08_env, monkeypatch: pytest.MonkeyPatch
) -> None:
    runtime, model, tools, _workspace, _home = s08_env
    state: dict[str, Any] = {"mode": "two_pages_20s", "list_calls": 0}
    fake_clock = {"t": 1000.0}

    import rynmesh.agent_runtime.extensions.mcp_transport as transport_mod

    orig_monotonic = transport_mod.time.monotonic
    clock_enabled = {"active": False}

    def _controlled_monotonic() -> float:
        if clock_enabled["active"]:
            return fake_clock["t"]
        return orig_monotonic()

    monkeypatch.setattr(transport_mod.time, "monotonic", _controlled_monotonic)

    class PaginatedHttpHandler(_QuietHandler):
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
                res: dict[str, Any] = {
                    "protocolVersion": "2025-03-26",
                    "capabilities": {"tools": {}},
                    "serverInfo": {"name": "paged-http", "version": "1"},
                }
            elif method == "tools/list":
                state["list_calls"] = int(state["list_calls"]) + 1
                cursor = (req.get("params") or {}).get("cursor")
                if state["mode"] == "two_pages_20s":
                    # Advance simulated clock by 20s per page under 30s startup_timeout (total 40s > 30s, < 300s)
                    fake_clock["t"] += 20.0
                    if not cursor:
                        res = {
                            "tools": [{"name": "alpha_read", "inputSchema": {"type": "object"}}],
                            "nextCursor": "page-2",
                        }
                    else:
                        res = {
                            "tools": [{"name": "beta_read", "inputSchema": {"type": "object"}}],
                        }
                elif state["mode"] == "exceed_10x":
                    # Advance simulated clock by 25s per page for >12 pages (> 300s = 10 * 30s)
                    fake_clock["t"] += 25.0
                    page_num = int(cursor.split("-")[1]) if cursor else 1
                    res = {
                        "tools": [{"name": f"t_{page_num}", "inputSchema": {"type": "object"}}],
                        "nextCursor": f"p-{page_num + 1}",
                    }
                else:
                    # Real timeout during tools/list refresh
                    time.sleep(1.5)
                    return
            else:
                res = {"content": [{"type": "text", "text": "pong-from-degraded"}]}

            body = json.dumps({"jsonrpc": "2.0", "id": ident, "result": res}).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            try:
                self.wfile.write(body)
            except OSError:
                pass

    http_server = _LoopbackHTTPServer(("127.0.0.1", 0), PaginatedHttpHandler)
    thread = threading.Thread(target=http_server.serve_forever, daemon=True)
    thread.start()

    try:
        ext_store = tools.extensions.store
        ov = ext_store.overview()
        srv = ext_store.upsert_mcp_server(
            {
                "id": "paged-srv",
                "name": "Paged Server",
                "transport": "streamable_http",
                "url": f"http://127.0.0.1:{http_server.server_port}/mcp",
                "enabled": True,
                "startup_timeout_ms": 30000,
                "tool_timeout_ms": 5000,
                "read_only_tools": ["alpha_read", "beta_read"],
            },
            expected_revision=ov["revision"],
        )

        # 1. Two pages of tools/list each taking 20s with startup_timeout=30s -> succeeds!
        clock_enabled["active"] = True
        probed = tools.extensions.manager.test_server("paged-srv", expected_revision=srv["revision"])
        clock_enabled["active"] = False
        assert probed["server"]["status"] == "ready"
        assert probed["server"]["tools_count"] == 2

        # 2. Exceeding 10 * startup_timeout across pages fails with agent_mcp_timeout
        #    AND since paged-srv already has discovered_tools, status becomes Degraded (not Error)!
        state["mode"] = "exceed_10x"
        clock_enabled["active"] = True
        degraded_10x = tools.extensions.manager.test_server(
            "paged-srv", expected_revision=probed["server"]["revision"]
        )
        clock_enabled["active"] = False
        assert degraded_10x["test_result"]["error"] == "agent_mcp_timeout"
        assert degraded_10x["server"]["status"] == "Degraded"
        assert degraded_10x["server"]["tools_count"] == 2

        # 3. Real wall-clock timeout on tools/list also keeps Degraded and tools remain callable!
        fast_srv = ext_store.upsert_mcp_server(
            {"startup_timeout_ms": 1000},
            server_id="paged-srv",
            expected_revision=degraded_10x["server"]["revision"],
        )
        state["mode"] = "timeout_real"
        degraded_real = tools.extensions.manager.test_server(
            "paged-srv", expected_revision=fast_srv["revision"]
        )
        assert degraded_real["test_result"]["error"] == "agent_mcp_timeout"
        assert degraded_real["server"]["status"] == "Degraded"
        assert degraded_real["server"]["tools_count"] == 2

        alpha_tool_name = next(
            t["model_tool_name"]
            for t in degraded_real["server"]["tools"]
            if t["name"] == "alpha_read"
        )
        spec_names = {s["function"]["name"] for s in tools.extensions.specs()}
        assert alpha_tool_name in spec_names

        model.responses = [
            _assistant_calls(("ryn_discover_tools", {"load": [alpha_tool_name]}, "c_disc_deg")),
            _assistant_calls((alpha_tool_name, {}, "c_call_deg")),
            _assistant_text("Done degraded."),
        ]
        task = runtime.create({"message": "Use degraded server tool"})
        for _ in range(8):
            if runtime.store.read()["tasks"][task["id"]]["status"] == "completed":
                break
            runtime.run_once()

        call_receipts = [r for r in _receipts(runtime, task["id"]) if r["tool"] == alpha_tool_name]
        assert len(call_receipts) == 1
        assert call_receipts[0]["status"] == "confirmed"
        assert "pong-from-degraded" in json.dumps(call_receipts[0]["result"])
    finally:
        clock_enabled["active"] = False
        http_server.shutdown()
        http_server.server_close()
        thread.join(timeout=2)


def test_non_allowlisted_error_code_populates_details_cause_and_ledger(
    s08_env, monkeypatch: pytest.MonkeyPatch
) -> None:
    runtime, model, tools, _workspace, _home = s08_env
    ext_store = tools.extensions.store
    ov = ext_store.overview()
    ext_store.upsert_mcp_server(
        {
            "id": "cause-srv",
            "name": "Cause Server",
            "transport": "streamable_http",
            "url": "http://127.0.0.1:9991/mcp",
            "enabled": True,
            "read_only_tools": ["read_op"],
        },
        expected_revision=ov["revision"],
    )
    info = ext_store.get_mcp_server_internal("cause-srv", require_enabled=True)
    discovered = ext_store.record_mcp_discovery(
        "cause-srv",
        expected_revision=info["revision"],
        expected_config_generation=info["config_generation"],
        expected_instance_id=info["instance_id"],
        status="ready",
        discovered_tools=[
            {"name": "read_op", "inputSchema": {"type": "object"}},
            {"name": "write_op", "inputSchema": {"type": "object"}},
        ],
    )
    by_orig = {t["name"]: t["model_tool_name"] for t in discovered["tools"]}
    read_tool = by_orig["read_op"]
    write_tool = by_orig["write_op"]

    call_mode = {"step": 0}

    def _fake_call_tool(*_args, **_kwargs):
        call_mode["step"] += 1
        if call_mode["step"] == 1:
            # Read path, dispatched=True, non-allowlisted snake_case code
            raise McpTransportError(
                "agent_mcp_foo",
                dispatched=True,
                phase="http_status",
                http_status=502,
            )
        if call_mode["step"] == 2:
            # Read path, dispatched=False, unsafe raw string -> must become "other"
            raise McpTransportError(
                "Bad Raw Code! Bearer secret123",
                dispatched=False,
                phase="pre_dispatch",
            )
        # Write path pre-dispatch (before before_dispatch runs) -> agent_mcp_operation_not_executed
        raise McpTransportError(
            "agent_mcp_foo",
            dispatched=False,
            phase="pre_dispatch",
        )

    monkeypatch.setattr(tools.extensions.manager, "call_tool", _fake_call_tool)

    model.responses = [
        _assistant_calls(("ryn_discover_tools", {"load": [read_tool, write_tool]}, "c_disc")),
        _assistant_calls((read_tool, {}, "c_read_1")),
        _assistant_calls((read_tool, {}, "c_read_2")),
        _assistant_calls((write_tool, {}, "c_write_1")),
        _assistant_text("Done cause checks."),
    ]
    task = runtime.create({"message": "Test non-allowlisted cause"})
    for _ in range(12):
        if runtime.store.read()["tasks"][task["id"]]["status"] == "completed":
            break
        runtime.run_once()

    read_receipts = [r for r in _receipts(runtime, task["id"]) if r["tool"] == read_tool]
    assert len(read_receipts) == 2

    assert read_receipts[0]["result"]["error"] == "agent_mcp_transport_error"
    assert read_receipts[0]["result"]["details"]["cause"] == "agent_mcp_foo"
    assert read_receipts[0]["result"]["details"]["phase"] == "http_status"
    assert read_receipts[0]["result"]["details"]["http_status"] == 502

    assert read_receipts[1]["result"]["error"] == "agent_mcp_transport_error"
    assert read_receipts[1]["result"]["not_executed"] is True
    assert read_receipts[1]["result"]["details"]["cause"] == "other"
    assert "secret123" not in json.dumps(read_receipts[1]["result"])

    write_receipts = [r for r in _receipts(runtime, task["id"]) if r["tool"] == write_tool]
    assert len(write_receipts) == 1
    assert write_receipts[0]["result"]["error"] == "agent_mcp_operation_not_executed"
    assert write_receipts[0]["result"]["not_executed"] is True
    assert write_receipts[0]["result"]["details"]["cause"] == "agent_mcp_foo"

    op_id = write_receipts[0]["operation_id"]
    ledger_row = tools.ledger.get(op_id)
    assert ledger_row is not None
    assert ledger_row["status"] == "not_executed"
    assert ledger_row["error_result"]["cause"] == "agent_mcp_foo"


def test_env_http_headers_redacted_in_500_body_results_and_ledger(
    s08_env, monkeypatch: pytest.MonkeyPatch
) -> None:
    runtime, model, tools, _workspace, home = s08_env
    monkeypatch.setenv("TEST_S08_SHORT_API_KEY", "abc123")

    class EchoEnvHeader500Handler(_QuietHandler):
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
                body = json.dumps(
                    {
                        "jsonrpc": "2.0",
                        "id": ident,
                        "result": {
                            "protocolVersion": "2025-03-26",
                            "capabilities": {"tools": {}},
                            "serverInfo": {"name": "env-hdr-http", "version": "1"},
                        },
                    }
                ).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return
            if method == "tools/list":
                body = json.dumps(
                    {
                        "jsonrpc": "2.0",
                        "id": ident,
                        "result": {
                            "tools": [
                                {"name": "read_echo", "inputSchema": {"type": "object"}},
                                {"name": "write_echo", "inputSchema": {"type": "object"}},
                            ]
                        },
                    }
                ).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return

            api_key_val = self.headers.get("X-Api-Key", "")
            raw = f"500 Internal Error: rejected header X-Api-Key: {api_key_val}".encode("utf-8")
            self.send_response(500)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

    http_server = _LoopbackHTTPServer(("127.0.0.1", 0), EchoEnvHeader500Handler)
    thread = threading.Thread(target=http_server.serve_forever, daemon=True)
    thread.start()
    try:
        ext_store = tools.extensions.store
        ov = ext_store.overview()
        srv = ext_store.upsert_mcp_server(
            {
                "id": "env-hdr-srv",
                "name": "Env Header Server",
                "transport": "streamable_http",
                "url": f"http://127.0.0.1:{http_server.server_port}/mcp",
                "enabled": True,
                "startup_timeout_ms": 3000,
                "tool_timeout_ms": 3000,
                "env_http_headers": {"X-Api-Key": "TEST_S08_SHORT_API_KEY"},
                "read_only_tools": ["read_echo"],
            },
            expected_revision=ov["revision"],
        )
        probed = tools.extensions.manager.test_server("env-hdr-srv", expected_revision=srv["revision"])
        by_name = {t["name"]: t["model_tool_name"] for t in probed["server"]["tools"]}
        read_tool = by_name["read_echo"]
        write_tool = by_name["write_echo"]

        model.responses = [
            _assistant_calls(("ryn_discover_tools", {"load": [read_tool, write_tool]}, "c_disc_env")),
            _assistant_calls((read_tool, {}, "c_read_env")),
            _assistant_calls((write_tool, {}, "c_write_env")),
            _assistant_text("Done env header check."),
        ]
        task = runtime.create({"message": "Test env_http_headers redaction on 500"})
        for _ in range(12):
            if runtime.store.read()["tasks"][task["id"]]["status"] == "completed":
                break
            runtime.run_once()

        receipts = _receipts(runtime, task["id"])
        read_rec = next(r for r in receipts if r["tool"] == read_tool)
        write_rec = next(r for r in receipts if r["tool"] == write_tool)

        assert read_rec["result"]["error"] == "agent_mcp_http_error"
        assert read_rec["result"]["details"]["http_status"] == 500
        assert "X-Api-Key: [REDACTED]" in read_rec["result"]["details"]["http_body"]
        assert "abc123" not in json.dumps(read_rec)

        assert "abc123" not in json.dumps(write_rec)
        ledger_row = tools.ledger.get(write_rec["operation_id"])
        assert ledger_row is not None
        assert "abc123" not in json.dumps(ledger_row)
        for p in home.rglob("*.json"):
            assert "abc123" not in p.read_text(encoding="utf-8", errors="replace")
    finally:
        http_server.shutdown()
        http_server.server_close()
        thread.join(timeout=2)


def test_rpc_error_message_redacts_bearer_token_and_request_header_values(
    s08_env, monkeypatch: pytest.MonkeyPatch
) -> None:
    runtime, model, tools, _workspace, _home = s08_env
    monkeypatch.setenv("TEST_S08_RPC_HDR_KEY", "hdrSecret9")
    bearer_tok = "rpc-bearer-tok-987654"

    class RpcLeakHandler(_QuietHandler):
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
                res: dict[str, Any] = {
                    "protocolVersion": "2025-03-26",
                    "capabilities": {"tools": {}},
                    "serverInfo": {"name": "rpc-leak", "version": "1"},
                }
                body = json.dumps({"jsonrpc": "2.0", "id": ident, "result": res}).encode("utf-8")
            elif method == "tools/list":
                res = {"tools": [{"name": "rpc_op", "inputSchema": {"type": "object"}}]}
                body = json.dumps({"jsonrpc": "2.0", "id": ident, "result": res}).encode("utf-8")
            else:
                hdr_echo = self.headers.get("X-Custom-Auth", "")
                msg = f"Downstream denied Bearer {bearer_tok} and header={hdr_echo}"
                body = json.dumps(
                    {"jsonrpc": "2.0", "id": ident, "error": {"code": -32000, "message": msg}}
                ).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    http_server = _LoopbackHTTPServer(("127.0.0.1", 0), RpcLeakHandler)
    thread = threading.Thread(target=http_server.serve_forever, daemon=True)
    thread.start()
    try:
        ext_store = tools.extensions.store
        ov = ext_store.overview()
        srv = ext_store.upsert_mcp_server(
            {
                "id": "rpc-leak-srv",
                "name": "RPC Leak Server",
                "transport": "streamable_http",
                "url": f"http://127.0.0.1:{http_server.server_port}/mcp",
                "enabled": True,
                "startup_timeout_ms": 3000,
                "tool_timeout_ms": 3000,
                "env_http_headers": {"X-Custom-Auth": "TEST_S08_RPC_HDR_KEY"},
                "read_only_tools": ["rpc_op"],
            },
            expected_revision=ov["revision"],
        )
        probed = tools.extensions.manager.test_server("rpc-leak-srv", expected_revision=srv["revision"])
        tool_name = probed["server"]["tools"][0]["model_tool_name"]

        model.responses = [
            _assistant_calls(("ryn_discover_tools", {"load": [tool_name]}, "c_disc_rpc")),
            _assistant_calls((tool_name, {}, "c_call_rpc")),
            _assistant_text("Done rpc leak check."),
        ]
        task = runtime.create({"message": "Test rpc error message redaction"})
        for _ in range(10):
            if runtime.store.read()["tasks"][task["id"]]["status"] == "completed":
                break
            runtime.run_once()

        rec = next(r for r in _receipts(runtime, task["id"]) if r["tool"] == tool_name)
        assert rec["result"]["error"] == "agent_mcp_rpc_error"
        cleaned_msg = rec["result"]["details"]["message"]
        assert "Bearer [REDACTED]" in cleaned_msg
        assert "header=[REDACTED]" in cleaned_msg
        dumped = json.dumps(rec)
        assert bearer_tok not in dumped
        assert "hdrSecret9" not in dumped
    finally:
        http_server.shutdown()
        http_server.server_close()
        thread.join(timeout=2)


def test_http_401_and_403_do_not_read_response_body(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    stop_event = threading.Event()
    body_reads_by_status: list[int] = []
    orig_iter_bytes = httpx.Response.iter_bytes
    orig_read = httpx.Response.read

    def _spy_iter_bytes(self: httpx.Response, *args: Any, **kwargs: Any):
        body_reads_by_status.append(int(self.status_code))
        return orig_iter_bytes(self, *args, **kwargs)

    def _spy_read(self: httpx.Response, *args: Any, **kwargs: Any):
        body_reads_by_status.append(int(self.status_code))
        return orig_read(self, *args, **kwargs)

    monkeypatch.setattr(httpx.Response, "iter_bytes", _spy_iter_bytes)
    monkeypatch.setattr(httpx.Response, "read", _spy_read)

    class AuthStatusNoBodyHandler(_QuietHandler):
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
                body = json.dumps(
                    {
                        "jsonrpc": "2.0",
                        "id": ident,
                        "result": {
                            "protocolVersion": "2025-03-26",
                            "capabilities": {"tools": {}},
                            "serverInfo": {"name": "auth-no-body", "version": "1"},
                        },
                    }
                ).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return

            status_code = int(((req.get("params") or {}).get("arguments") or {}).get("status", 401))
            canary = b"UNREAD_401_403_BODY_CANARY"
            try:
                self.send_response(status_code)
                self.send_header("WWW-Authenticate", 'Bearer error="invalid_token"')
                self.send_header("Content-Type", "text/plain; charset=utf-8")
                self.send_header("Content-Length", str(len(canary)))
                self.end_headers()
                self.wfile.flush()
                # Delay body write: if client tried to read 401/403 body it would stall here
                stop_event.wait(2.0)
                self.wfile.write(canary)
            except OSError:
                self.close_connection = True

    http_server = _LoopbackHTTPServer(("127.0.0.1", 0), AuthStatusNoBodyHandler)
    thread = threading.Thread(target=http_server.serve_forever, daemon=True)
    thread.start()

    key = peer_box.load_or_create_messaging_key(tmp_path / "messaging.key")
    store = ExtensionStore(tmp_path / "agent", key)
    store.upsert_mcp_server(
        {
            "id": "auth-nobody-srv",
            "transport": "streamable_http",
            "url": f"http://127.0.0.1:{http_server.server_port}/mcp",
            "enabled": True,
            "startup_timeout_ms": 3000,
            "tool_timeout_ms": 5000,
            "auth": {"mode": "none"},
        }
    )
    srv = store.get_mcp_server_internal("auth-nobody-srv", require_enabled=True)
    manager = McpSessionManager(store)
    try:
        for code in (401, 403):
            t0 = time.monotonic()
            with pytest.raises(McpTransportError) as exc_info:
                manager.call_tool(
                    "auth-nobody-srv",
                    "check_auth",
                    {"status": code},
                    expected_revision=srv["revision"],
                    expected_config_generation=srv["config_generation"],
                    expected_instance_id=srv["instance_id"],
                )
            elapsed = time.monotonic() - t0
            assert str(exc_info.value) == "agent_mcp_auth_required"
            assert exc_info.value.http_status == code
            assert elapsed < 1.0
            assert exc_info.value.details is None or "http_body" not in exc_info.value.details
            assert "UNREAD_401_403_BODY_CANARY" not in json.dumps(exc_info.value.details)

        assert 401 not in body_reads_by_status
        assert 403 not in body_reads_by_status
    finally:
        stop_event.set()
        manager.close()
        http_server.shutdown()
        http_server.server_close()
        thread.join(timeout=2)
