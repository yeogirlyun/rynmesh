"""Tests for S08-I7: HTTP retry, session expiry recovery, 4xx dispatch semantics, OAuth 401/403, and session reuse."""

from __future__ import annotations

import base64
import hashlib
import json
import secrets
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlencode, urlsplit

import httpx
import pytest

from rynmesh.agent_runtime.extensions import ExtensionStore, ExtensionToolAdapter
from rynmesh.agent_runtime.extensions import mcp_transport as mt
from rynmesh.agent_runtime.extensions.mcp_oauth import McpOAuthManager
from rynmesh.agent_runtime.extensions.mcp_transport import (
    McpSessionManager,
    McpTransportError,
    StdioMcpSession,
    StreamableHttpMcpSession,
)
from rynmesh.agent_runtime.extensions.store import compute_model_tool_name
from rynmesh.agent_runtime.operation_ledger import OperationLedger
from rynmesh.agent_runtime.store import AgentToolRejected, encoded
from rynmesh.services import peer_box


class _QuietHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *_args: Any) -> None:
        return

    def do_DELETE(self) -> None:
        self.send_response(204)
        self.send_header("Content-Length", "0")
        self.end_headers()


class _LocalOAuthMcpFixture:
    """Self-contained loopback OAuth AS + Streamable HTTP MCP server for T5 and T7."""

    def __init__(self) -> None:
        self.codes: dict[str, dict[str, Any]] = {}
        self.access: dict[str, dict[str, Any]] = {}
        self.refresh: dict[str, dict[str, Any]] = {}
        self.refresh_count = 0
        self.init_count = 0
        self.tool_calls: list[tuple[str, str]] = []
        self.tool_responder: Any = None
        fixture = self

        class Handler(_QuietHandler):
            def do_GET(self) -> None:
                self._serve()

            def do_POST(self) -> None:
                self._serve()

            def _serve(self) -> None:
                size = int(self.headers.get("Content-Length", "0"))
                raw = self.rfile.read(size) if size else b""
                ctype = self.headers.get("Content-Type", "")
                if "application/json" in ctype and raw:
                    body = json.loads(raw.decode("utf-8"))
                elif raw:
                    body = {k: v[0] for k, v in parse_qs(raw.decode("utf-8")).items()}
                else:
                    body = {}
                status, extra_headers, payload_obj = fixture._handle(
                    self.command, self.path, dict(self.headers), body
                )
                payload = (
                    json.dumps(payload_obj).encode("utf-8")
                    if payload_obj is not None
                    else b""
                )
                self.send_response(status)
                merged = {
                    "Content-Type": "application/json",
                    "Content-Length": str(len(payload)),
                    **extra_headers,
                }
                for hk, hv in merged.items():
                    if hv is not None:
                        self.send_header(hk, hv)
                self.end_headers()
                if payload:
                    self.wfile.write(payload)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        self.origin = f"http://127.0.0.1:{self.server.server_port}"
        self.resource = f"{self.origin}/mcp"
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)

    def complete_redirect(self, auth_url: str) -> dict[str, str]:
        query = {k: v[0] for k, v in parse_qs(urlsplit(auth_url).query).items()}
        code = "code-" + secrets.token_hex(6)
        self.codes[code] = query
        cb_url = query["redirect_uri"] + "?" + urlencode(
            {"code": code, "state": query["state"], "iss": self.origin}
        )
        resp = httpx.get(cb_url, trust_env=False)
        assert resp.status_code == 200
        return query

    def _handle(
        self,
        command: str,
        path: str,
        headers: dict[str, str],
        body: dict[str, Any],
    ) -> tuple[int, dict[str, str | None], Any]:
        if command == "GET":
            if path in {"/.well-known/oauth-protected-resource/mcp", "/.well-known/oauth-protected-resource"}:
                return (
                    200,
                    {},
                    {
                        "resource": self.resource,
                        "authorization_servers": [self.origin],
                        "scopes_supported": ["tools.read"],
                    },
                )
            if "oauth-authorization-server" in path:
                return (
                    200,
                    {},
                    {
                        "issuer": self.origin,
                        "authorization_endpoint": f"{self.origin}/authorize",
                        "token_endpoint": f"{self.origin}/token",
                        "revocation_endpoint": f"{self.origin}/revoke",
                        "code_challenge_methods_supported": ["S256"],
                        "token_endpoint_auth_methods_supported": ["none"],
                        "authorization_response_iss_parameter_supported": True,
                    },
                )
        if path == "/token":
            if body.get("grant_type") == "authorization_code":
                params = self.codes.pop(str(body.get("code", "")), None)
                verifier = str(body.get("code_verifier", "")).encode("ascii")
                digest = base64.urlsafe_b64encode(hashlib.sha256(verifier).digest()).decode("ascii").rstrip("=")
                if not params or digest != params["code_challenge"]:
                    return 400, {}, {"error": "invalid_grant"}
            else:
                self.refresh_count += 1
                params = self.refresh.pop(str(body.get("refresh_token", "")), None)
                if not params:
                    return 400, {}, {"error": "invalid_grant"}
            acc = f"tok-{len(self.access) + 1}-{secrets.token_hex(4)}"
            ref = f"ref-{self.refresh_count + 1}-{secrets.token_hex(4)}"
            self.access[acc] = params
            self.refresh[ref] = params
            return (
                200,
                {},
                {
                    "access_token": acc,
                    "refresh_token": ref,
                    "expires_in": 3600,
                    "token_type": "Bearer",
                    "scope": params.get("scope", "tools.read"),
                    "resource": params["resource"],
                },
            )
        if path == "/revoke":
            self.refresh.pop(str(body.get("token", "")), None)
            return 200, {}, {}
        if path == "/mcp":
            ident = body.get("id")
            method = body.get("method")
            auth_hdr = headers.get("Authorization") or headers.get("authorization") or ""
            tok = auth_hdr.removeprefix("Bearer ").strip()
            if ident is None:
                return 202, {}, None
            if method == "tools/call":
                tname = str((body.get("params") or {}).get("name") or "")
                self.tool_calls.append((tname, tok))
            if tok not in self.access:
                return (
                    401,
                    {"WWW-Authenticate": 'Bearer error="invalid_token"'},
                    {"error": "unauthorized"},
                )
            if method == "initialize":
                self.init_count += 1
                return (
                    200,
                    {"Mcp-Session-Id": f"oauth-sess-{self.init_count}"},
                    {
                        "jsonrpc": "2.0",
                        "id": ident,
                        "result": {
                            "protocolVersion": "2025-03-26",
                            "capabilities": {"tools": {}},
                            "serverInfo": {"name": "oauth-mcp", "version": "1"},
                        },
                    },
                )
            if method == "tools/list":
                return (
                    200,
                    {},
                    {
                        "jsonrpc": "2.0",
                        "id": ident,
                        "result": {
                            "tools": [
                                {"name": "restricted_tool", "inputSchema": {"type": "object"}},
                                {"name": "allowed_tool", "inputSchema": {"type": "object"}},
                            ]
                        },
                    },
                )
            if method == "tools/call":
                tname = str((body.get("params") or {}).get("name") or "")
                if self.tool_responder is not None:
                    custom = self.tool_responder(tname, tok, ident)
                    if custom is not None:
                        return custom
                return (
                    200,
                    {},
                    {
                        "jsonrpc": "2.0",
                        "id": ident,
                        "result": {
                            "content": [{"type": "text", "text": f"ok:{tname}"}],
                            "isError": False,
                        },
                    },
                )
        return 404, {}, {"error": "not_found"}


def _make_adapter_stack(
    tmp_path: Path,
    server_cfg: dict[str, Any],
    discovered_tools: list[dict[str, Any]],
) -> tuple[ExtensionStore, McpSessionManager, ExtensionToolAdapter, OperationLedger, dict[str, Any]]:
    key = peer_box.load_or_create_messaging_key(tmp_path / "messaging.x25519")
    ext_store = ExtensionStore(tmp_path / "agent", key)
    workspace = tmp_path / "workspace"
    workspace.mkdir(parents=True, exist_ok=True)
    created = ext_store.upsert_mcp_server(server_cfg)
    ext_store.record_mcp_discovery(
        server_cfg["id"],
        expected_revision=created["revision"],
        expected_config_generation=created["config_generation"],
        expected_instance_id=created["instance_id"],
        status="ready",
        discovered_tools=discovered_tools,
    )
    srv = ext_store.get_mcp_server_internal(server_cfg["id"], require_enabled=True)

    class _LocalStub:
        def policy(self, _context: dict[str, Any]) -> dict[str, Any]:
            return {"desktop_access": False, "workspace_root": str(workspace)}

    ledger = OperationLedger(tmp_path)
    manager = McpSessionManager(ext_store)
    adapter = ExtensionToolAdapter(
        _LocalStub(),
        lambda: ledger,
        extension_store=ext_store,
        session_manager=manager,
    )
    ctx = {
        "task_id": "task-s08-i7",
        "local_execution": {"desktop_access": False, "workspace_root": str(workspace)},
    }
    return ext_store, manager, adapter, ledger, ctx


# T1: HTTP fake server: initialize returns 503 first then 200 -> succeeds; plus _is_retryable checks
def test_t1_http_initialize_503_then_200_and_retryable_classification() -> None:
    assert hasattr(mt, "_is_retryable"), "_is_retryable must be defined in mcp_transport"
    is_retryable = mt._is_retryable

    for status in (408, 429, 500, 502, 503, 504):
        assert is_retryable(McpTransportError("agent_mcp_http_error", dispatched=True, http_status=status)) is True
    assert is_retryable(McpTransportError("agent_mcp_process_disconnected", dispatched=False)) is True

    for non_retry_status in (400, 401, 403, 404):
        assert is_retryable(McpTransportError("agent_mcp_http_error", dispatched=False, http_status=non_retry_status)) is False
    assert is_retryable(McpTransportError("agent_mcp_auth_required", dispatched=False, http_status=401)) is False
    assert is_retryable(McpTransportError("agent_mcp_process_disconnected", dispatched=True)) is False
    assert is_retryable(McpTransportError("agent_mcp_timeout", dispatched=True)) is False

    counts = {"initialize": 0, "auth_init": 0, "call": 0}

    class RetryInitHandler(_QuietHandler):
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
            if self.path == "/auth-401":
                counts["auth_init"] += 1
                self.send_response(401)
                self.send_header("WWW-Authenticate", 'Bearer error="invalid_token"')
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            if method == "initialize":
                counts["initialize"] += 1
                if counts["initialize"] == 1:
                    err = b"service warming up"
                    self.send_response(503)
                    self.send_header("Content-Type", "text/plain")
                    self.send_header("Content-Length", str(len(err)))
                    self.end_headers()
                    self.wfile.write(err)
                    return
                body = json.dumps(
                    {
                        "jsonrpc": "2.0",
                        "id": ident,
                        "result": {
                            "protocolVersion": "2025-03-26",
                            "capabilities": {"tools": {}},
                            "serverInfo": {"name": "init-retry", "version": "1"},
                        },
                    }
                ).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return
            counts["call"] += 1
            self.send_response(503)
            self.send_header("Content-Length", "0")
            self.end_headers()

    http_srv = ThreadingHTTPServer(("127.0.0.1", 0), RetryInitHandler)
    http_srv.daemon_threads = True
    thread = threading.Thread(target=http_srv.serve_forever, daemon=True)
    thread.start()
    try:
        sess = StreamableHttpMcpSession(
            {
                "id": "t1-srv",
                "url": f"http://127.0.0.1:{http_srv.server_port}/mcp",
                "startup_timeout_ms": 5000,
                "tool_timeout_ms": 5000,
            }
        )
        try:
            t0 = time.monotonic()
            info = sess.initialize()
            elapsed = time.monotonic() - t0
            assert info["protocolVersion"] == "2025-03-26"
            assert counts["initialize"] == 2
            assert elapsed >= 0.2

            # tools/call must NOT retry on 503
            with pytest.raises(McpTransportError) as call_exc:
                sess.call_tool("any_tool", {})
            assert call_exc.value.http_status == 503
            assert counts["call"] == 1
        finally:
            sess.close()

        # auth_required must NOT retry
        auth_sess = StreamableHttpMcpSession(
            {
                "id": "t1-auth",
                "url": f"http://127.0.0.1:{http_srv.server_port}/auth-401",
                "startup_timeout_ms": 5000,
                "tool_timeout_ms": 5000,
            }
        )
        try:
            with pytest.raises(McpTransportError) as auth_exc:
                auth_sess.initialize()
            assert str(auth_exc.value) == "agent_mcp_auth_required"
            assert counts["auth_init"] == 1
        finally:
            auth_sess.close()
    finally:
        http_srv.shutdown()
        http_srv.server_close()
        thread.join(timeout=2)


# T2: tools/list 429 with Retry-After + shared startup_timeout deadline + pre-send disconnect retry
def test_t2_tools_list_429_respects_retry_after_and_startup_deadline(monkeypatch: pytest.MonkeyPatch) -> None:
    timestamps: list[float] = []
    budget_calls = {"count": 0}

    class RateLimitedListHandler(_QuietHandler):
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
                            "serverInfo": {"name": "rl-srv", "version": "1"},
                        },
                    }
                ).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return
            if self.path == "/over-budget":
                budget_calls["count"] += 1
                self.send_response(429)
                self.send_header("Retry-After", "5")
                self.send_header("Content-Length", "0")
                self.end_headers()
                return

            timestamps.append(time.monotonic())
            if len(timestamps) == 1:
                self.send_response(429)
                self.send_header("Retry-After", "1")
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            body = json.dumps(
                {
                    "jsonrpc": "2.0",
                    "id": ident,
                    "result": {
                        "tools": [{"name": "listed_ok", "inputSchema": {"type": "object"}}]
                    },
                }
            ).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    http_srv = ThreadingHTTPServer(("127.0.0.1", 0), RateLimitedListHandler)
    http_srv.daemon_threads = True
    thread = threading.Thread(target=http_srv.serve_forever, daemon=True)
    thread.start()
    try:
        sess = StreamableHttpMcpSession(
            {
                "id": "t2-srv",
                "url": f"http://127.0.0.1:{http_srv.server_port}/mcp",
                "startup_timeout_ms": 5000,
                "tool_timeout_ms": 5000,
            }
        )
        try:
            sess.initialize()
            tools = sess.list_all_tools()
            assert [t["name"] for t in tools] == ["listed_ok"]
            assert len(timestamps) == 2
            assert (timestamps[1] - timestamps[0]) >= 0.9
        finally:
            sess.close()

        # Retry-After exceeding startup_timeout deadline does not wait 5s or retry early
        short_sess = StreamableHttpMcpSession(
            {
                "id": "t2-budget",
                "url": f"http://127.0.0.1:{http_srv.server_port}/over-budget",
                "startup_timeout_ms": 1000,
                "tool_timeout_ms": 5000,
            }
        )
        try:
            short_sess.initialize()
            t0 = time.monotonic()
            with pytest.raises(McpTransportError) as over_exc:
                short_sess.list_all_tools()
            elapsed = time.monotonic() - t0
            assert over_exc.value.http_status == 429
            assert budget_calls["count"] == 1
            assert elapsed < 1.0
        finally:
            short_sess.close()

        # Pre-send httpx.ConnectError sets dispatched=False and is retried by list_all_tools
        conn_sess = StreamableHttpMcpSession(
            {
                "id": "t2-conn",
                "url": f"http://127.0.0.1:{http_srv.server_port}/mcp",
                "startup_timeout_ms": 5000,
                "tool_timeout_ms": 5000,
            }
        )
        try:
            conn_sess.initialize()
            orig_stream = conn_sess._client.stream
            flaky_state = {"failed_once": False}

            def _flaky_stream(*args: Any, **kwargs: Any):
                if not flaky_state["failed_once"]:
                    flaky_state["failed_once"] = True
                    raise httpx.ConnectError("transient connect refusal")
                return orig_stream(*args, **kwargs)

            monkeypatch.setattr(conn_sess._client, "stream", _flaky_stream)
            recovered_tools = conn_sess.list_all_tools()
            assert [t["name"] for t in recovered_tools] == ["listed_ok"]
            assert flaky_state["failed_once"] is True
        finally:
            conn_sess.close()
    finally:
        http_srv.shutdown()
        http_srv.server_close()
        thread.join(timeout=2)


# T3: tools/call 404 with session_id -> agent_mcp_session_expired -> McpSessionManager rebuilds session and succeeds
def test_t3_tools_call_404_with_session_rebuilds_once_and_succeeds(tmp_path: Path) -> None:
    state: dict[str, Any] = {
        "init_count": 0,
        "expired_sessions": set(),
        "call_sessions": [],
    }

    class SessionExpiryHandler(_QuietHandler):
        def do_POST(self) -> None:
            length = int(self.headers.get("Content-Length", "0"))
            req = json.loads(self.rfile.read(length))
            ident = req.get("id")
            method = req.get("method")
            sess_id = self.headers.get("Mcp-Session-Id")
            if ident is None:
                self.send_response(202)
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            if self.path == "/stateless-404":
                if method == "initialize":
                    body = json.dumps(
                        {
                            "jsonrpc": "2.0",
                            "id": ident,
                            "result": {
                                "protocolVersion": "2025-03-26",
                                "capabilities": {"tools": {}},
                                "serverInfo": {"name": "stateless", "version": "1"},
                            },
                        }
                    ).encode("utf-8")
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                    return
                self.send_response(404)
                self.send_header("Content-Length", "0")
                self.end_headers()
                return

            if method == "initialize":
                state["init_count"] += 1
                new_sid = f"sess-v{state['init_count']}"
                body = json.dumps(
                    {
                        "jsonrpc": "2.0",
                        "id": ident,
                        "result": {
                            "protocolVersion": "2025-03-26",
                            "capabilities": {"tools": {}},
                            "serverInfo": {"name": "exp-srv", "version": "1"},
                        },
                    }
                ).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Mcp-Session-Id", new_sid)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return

            state["call_sessions"].append(sess_id)
            if sess_id in state["expired_sessions"]:
                err = b"session expired"
                self.send_response(404)
                self.send_header("Content-Type", "text/plain")
                self.send_header("Content-Length", str(len(err)))
                self.end_headers()
                self.wfile.write(err)
                return

            marker = ((req.get("params") or {}).get("arguments") or {}).get("marker")
            body = json.dumps(
                {
                    "jsonrpc": "2.0",
                    "id": ident,
                    "result": {
                        "content": [
                            {
                                "type": "text",
                                "text": json.dumps({"session_id": sess_id, "marker": marker}),
                            }
                        ],
                        "isError": False,
                    },
                }
            ).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Mcp-Session-Id", sess_id or "")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    http_srv = ThreadingHTTPServer(("127.0.0.1", 0), SessionExpiryHandler)
    http_srv.daemon_threads = True
    thread = threading.Thread(target=http_srv.serve_forever, daemon=True)
    thread.start()

    key = peer_box.load_or_create_messaging_key(tmp_path / "messaging.key")
    store = ExtensionStore(tmp_path / "agent", key)
    store.upsert_mcp_server(
        {
            "id": "exp-http",
            "transport": "streamable_http",
            "url": f"http://127.0.0.1:{http_srv.server_port}/mcp",
            "enabled": True,
            "startup_timeout_ms": 5000,
            "tool_timeout_ms": 5000,
        }
    )
    srv = store.get_mcp_server_internal("exp-http", require_enabled=True)
    manager = McpSessionManager(store)
    try:
        res1 = manager.call_tool(
            "exp-http",
            "echo",
            {"marker": "call-1"},
            expected_revision=srv["revision"],
            expected_config_generation=srv["config_generation"],
            expected_instance_id=srv["instance_id"],
        )
        payload1 = json.loads(res1["content"][0]["text"])
        assert payload1 == {"session_id": "sess-v1", "marker": "call-1"}
        old_sess = manager._sessions[("exp-http", None)]
        assert isinstance(old_sess, StreamableHttpMcpSession)
        assert old_sess.session_id == "sess-v1"

        # Expire sess-v1 on server; direct session.call_tool must raise agent_mcp_session_expired (dispatched=False),
        # and manager.call_tool must discard old_sess, initialize sess-v2, and retry once to succeed.
        state["expired_sessions"].add("sess-v1")
        res2 = manager.call_tool(
            "exp-http",
            "echo",
            {"marker": "call-2"},
            expected_revision=srv["revision"],
            expected_config_generation=srv["config_generation"],
            expected_instance_id=srv["instance_id"],
        )
        payload2 = json.loads(res2["content"][0]["text"])
        assert payload2 == {"session_id": "sess-v2", "marker": "call-2"}
        assert old_sess.closed.is_set()
        new_sess = manager._sessions[("exp-http", None)]
        assert new_sess is not old_sess
        assert state["init_count"] == 2
        assert state["call_sessions"] == ["sess-v1", "sess-v1", "sess-v2"]

        # Verify direct session raises agent_mcp_session_expired with dispatched=False on 404 with session_id
        state["expired_sessions"].add("sess-v2")
        with pytest.raises(McpTransportError) as direct_exc:
            new_sess.call_tool("echo", {"marker": "direct-expired"})
        assert str(direct_exc.value) == "agent_mcp_session_expired"
        assert direct_exc.value.dispatched is False
        assert direct_exc.value.http_status == 404

        # Verify 404 WITHOUT session_id raises agent_mcp_http_error (not session_expired) with dispatched=False
        stateless = StreamableHttpMcpSession(
            {
                "id": "stateless",
                "url": f"http://127.0.0.1:{http_srv.server_port}/stateless-404",
                "startup_timeout_ms": 5000,
                "tool_timeout_ms": 5000,
            }
        )
        try:
            stateless.initialize()
            assert stateless.session_id is None
            with pytest.raises(McpTransportError) as sl_exc:
                stateless.call_tool("echo", {})
            assert str(sl_exc.value) == "agent_mcp_http_error"
            assert sl_exc.value.dispatched is False
            assert sl_exc.value.http_status == 404
        finally:
            stateless.close()
    finally:
        manager.close()
        http_srv.shutdown()
        http_srv.server_close()
        thread.join(timeout=2)


# T4: tools/call 400 -> dispatched=False, write ledger & tool result marked not_executed, HTTP session preserved
def test_t4_tools_call_400_marks_not_executed_and_preserves_http_session(tmp_path: Path) -> None:
    state = {"init_count": 0, "call_count": 0}

    class Status400Handler(_QuietHandler):
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
                state["init_count"] += 1
                body = json.dumps(
                    {
                        "jsonrpc": "2.0",
                        "id": ident,
                        "result": {
                            "protocolVersion": "2025-03-26",
                            "capabilities": {"tools": {}},
                            "serverInfo": {"name": "s400", "version": "1"},
                        },
                    }
                ).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Mcp-Session-Id", f"s400-{state['init_count']}")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return

            state["call_count"] += 1
            force_status = ((req.get("params") or {}).get("arguments") or {}).get("status")
            if force_status:
                msg = f"forced-{force_status}".encode("utf-8")
                self.send_response(int(force_status))
                self.send_header("Content-Type", "text/plain")
                self.send_header("Content-Length", str(len(msg)))
                self.end_headers()
                self.wfile.write(msg)
                return

            body = json.dumps(
                {
                    "jsonrpc": "2.0",
                    "id": ident,
                    "result": {
                        "content": [{"type": "text", "text": "mutated-ok"}],
                        "isError": False,
                    },
                }
            ).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    http_srv = ThreadingHTTPServer(("127.0.0.1", 0), Status400Handler)
    http_srv.daemon_threads = True
    thread = threading.Thread(target=http_srv.serve_forever, daemon=True)
    thread.start()

    _, manager, adapter, ledger, base_ctx = _make_adapter_stack(
        tmp_path,
        {
            "id": "s400-srv",
            "name": "S400 Server",
            "transport": "streamable_http",
            "url": f"http://127.0.0.1:{http_srv.server_port}/mcp",
            "enabled": True,
            "startup_timeout_ms": 5000,
            "tool_timeout_ms": 5000,
        },
        [
            {
                "name": "write_item",
                "inputSchema": {
                    "type": "object",
                    "properties": {"status": {"type": "integer"}},
                },
            }
        ],
    )
    model_name = compute_model_tool_name("s400-srv", "write_item")
    try:
        binding = adapter.metadata(model_name)["extension_binding"]
        ctx = {**base_ctx, "extension_binding": binding}
        args_400 = {"status": 400}
        preview_400 = adapter.preview(model_name, args_400, context=ctx)

        with pytest.raises(AgentToolRejected) as rej_info:
            adapter.execute(
                model_name,
                args_400,
                operation_id="op-http-400",
                context=ctx,
                expected_preview=preview_400,
            )
        assert str(rej_info.value) == "agent_mcp_http_error"
        row_400 = ledger.get("op-http-400")
        assert row_400 is not None
        assert row_400["status"] == "not_executed"
        rec_400 = adapter.reconcile(
            "op-http-400",
            {
                **ctx,
                "operation": {
                    "tool": model_name,
                    "arguments_sha256": hashlib.sha256(encoded(args_400)).hexdigest(),
                    "extension_binding": binding,
                },
            },
        )
        assert rec_400["outcome"] == "not_executed"
        assert rec_400["receipt"]["result"]["not_executed"] is True

        # 408 and 429 must still keep dispatched=True
        srv_internal = adapter.store.get_mcp_server_internal("s400-srv", require_enabled=True)
        for ambig_code in (408, 429):
            with pytest.raises(McpTransportError) as ambig_exc:
                manager.call_tool(
                    "s400-srv",
                    "write_item",
                    {"status": ambig_code},
                    expected_revision=srv_internal["revision"],
                    expected_config_generation=srv_internal["config_generation"],
                    expected_instance_id=srv_internal["instance_id"],
                )
            assert ambig_exc.value.dispatched is True
            assert ambig_exc.value.http_status == ambig_code

        # Same HTTP session remained open across 400/408/429 (phase=="http_status") and succeeds on next call
        args_ok: dict[str, Any] = {}
        preview_ok = adapter.preview(model_name, args_ok, context=ctx)
        ok_res = adapter.execute(
            model_name,
            args_ok,
            operation_id="op-http-200",
            context=ctx,
            expected_preview=preview_ok,
        )
        assert ok_res["text"] == "mutated-ok"
        assert state["init_count"] == 1
    finally:
        manager.close()
        http_srv.shutdown()
        http_srv.server_close()
        thread.join(timeout=2)


# T5: tools/call 401 refreshes OAuth token once and succeeds; 403 insufficient_scope records challenge without retry
def test_t5_tools_call_401_refreshes_oauth_token_once_and_403_insufficient_scope_records_challenge(
    tmp_path: Path,
) -> None:
    fx = _LocalOAuthMcpFixture()
    key = peer_box.load_or_create_messaging_key(tmp_path / "messaging.x25519")
    store = ExtensionStore(tmp_path / "agent", key)
    store.upsert_mcp_server(
        {
            "id": "oauth-srv",
            "transport": "streamable_http",
            "url": fx.resource,
            "enabled": True,
            "startup_timeout_ms": 5000,
            "tool_timeout_ms": 5000,
            "auth": {"mode": "oauth", "client_id": "test-client", "scopes": ["tools.read"]},
        }
    )
    oauth = McpOAuthManager(store)
    started = oauth.start_login("oauth-srv", store.get_mcp_server_internal("oauth-srv")["revision"])
    fx.complete_redirect(started["authorization_url"])
    deadline = time.monotonic() + 5.0
    while oauth.login_status("oauth-srv", started["auth_id"])["state"] != "connected" and time.monotonic() < deadline:
        time.sleep(0.01)

    srv = store.get_mcp_server_internal("oauth-srv", require_enabled=True)
    initial_token = oauth.access_token(srv)
    assert isinstance(initial_token, str) and initial_token in fx.access

    session = StreamableHttpMcpSession(srv, oauth=oauth)
    try:
        session.initialize()
        # Expire initial_token on resource server AFTER initialize so tools/call hits 401
        fx.access.pop(initial_token, None)
        res = session.call_tool("allowed_tool", {})
        assert res["content"][0]["text"] == "ok:allowed_tool"
        assert fx.refresh_count == 1
        assert len(fx.tool_calls) == 2
        assert fx.tool_calls[0][1] == initial_token
        assert fx.tool_calls[1][1] != initial_token

        # 403 with Bearer error="insufficient_scope" must NOT retry/refresh, and must record challenge
        def _insufficient_scope(tname: str, _tok: str, _ident: int):
            if tname == "restricted_tool":
                return (
                    403,
                    {
                        "WWW-Authenticate": 'Bearer error="insufficient_scope", scope="tools.read tools.write"'
                    },
                    {"error": "insufficient_scope"},
                )
            return None

        fx.tool_responder = _insufficient_scope
        with pytest.raises(McpTransportError) as scope_exc:
            session.call_tool("restricted_tool", {})
        assert str(scope_exc.value) == "agent_mcp_auth_required"
        assert scope_exc.value.dispatched is False
        assert scope_exc.value.auth_challenge.get("error") == "insufficient_scope"
        assert fx.refresh_count == 1
        assert len(fx.tool_calls) == 3
        cred_row = oauth.credentials.read()["servers"][srv["instance_id"]]
        assert cred_row["error"] == "agent_mcp_auth_required"
        assert cred_row["challenge"]["error"] == "insufficient_scope"
        assert "tools.write" in cred_row["challenge"]["scope"]
    finally:
        session.close()
        oauth.close()
        fx.close()


# T6: stdio fake server returns -32000 business error on 1st call, 2nd call reuses the exact same pid and succeeds
def test_t6_stdio_rpc_business_error_preserves_session_and_reuses_same_pid(tmp_path: Path) -> None:
    script = tmp_path / "stdio_rpc_err.py"
    script.write_text(
        "import json, os, sys\n"
        "calls = 0\n"
        "for raw in sys.stdin:\n"
        "    line = raw.strip()\n"
        "    if not line:\n"
        "        continue\n"
        "    req = json.loads(line)\n"
        "    ident = req.get('id')\n"
        "    if ident is None:\n"
        "        continue\n"
        "    method = req.get('method')\n"
        "    if method == 'initialize':\n"
        "        res = {'protocolVersion': '2025-03-26', 'capabilities': {'tools': {}}, 'serverInfo': {'name': 'rpc-err', 'version': '1'}}\n"
        "        print(json.dumps({'jsonrpc': '2.0', 'id': ident, 'result': res}), flush=True)\n"
        "    elif method == 'tools/call':\n"
        "        calls += 1\n"
        "        if calls == 1:\n"
        "            print(json.dumps({'jsonrpc': '2.0', 'id': ident, 'error': {'code': -32000, 'message': 'domain validation failed'}}), flush=True)\n"
        "        else:\n"
        "            payload = {'pid': os.getpid(), 'calls': calls}\n"
        "            print(json.dumps({'jsonrpc': '2.0', 'id': ident, 'result': {'content': [{'type': 'text', 'text': json.dumps(payload)}]}}), flush=True)\n",
        encoding="utf-8",
    )

    key = peer_box.load_or_create_messaging_key(tmp_path / "messaging.key")
    store = ExtensionStore(tmp_path / "agent", key)
    store.upsert_mcp_server(
        {
            "id": "stdio-biz",
            "command": sys.executable,
            "args": ["-u", str(script)],
            "enabled": True,
            "startup_timeout_ms": 5000,
            "tool_timeout_ms": 5000,
        }
    )
    srv = store.get_mcp_server_internal("stdio-biz", require_enabled=True)
    manager = McpSessionManager(store)
    try:
        with pytest.raises(McpTransportError) as err1:
            manager.call_tool(
                "stdio-biz",
                "do_work",
                {},
                expected_revision=srv["revision"],
                expected_config_generation=srv["config_generation"],
                expected_instance_id=srv["instance_id"],
            )
        assert str(err1.value) == "agent_mcp_rpc_error"
        assert err1.value.rpc_code == -32000
        assert err1.value.phase == "rpc_error"

        sess1 = manager._sessions.get(("stdio-biz", None))
        assert isinstance(sess1, StdioMcpSession)
        assert sess1.is_usable()
        pid_after_first = sess1.process.pid

        res2 = manager.call_tool(
            "stdio-biz",
            "do_work",
            {},
            expected_revision=srv["revision"],
            expected_config_generation=srv["config_generation"],
            expected_instance_id=srv["instance_id"],
        )
        sess2 = manager._sessions.get(("stdio-biz", None))
        assert sess2 is sess1
        assert sess2.process.pid == pid_after_first
        data2 = json.loads(res2["content"][0]["text"])
        assert data2["calls"] == 2
        assert isinstance(data2["pid"], int)
        assert err1.value.session_pid == pid_after_first
    finally:
        manager.close()


# T7: tools/call receives plain 403 (no challenge header) -> agent_mcp_http_forbidden, afterwards other tools on same server do not report auth_required
def test_t7_tools_call_plain_403_returns_forbidden_without_polluting_oauth_or_closing_session(
    tmp_path: Path,
) -> None:
    fx = _LocalOAuthMcpFixture()

    def _forbid_restricted(tname: str, _tok: str, _ident: int):
        if tname == "restricted_tool":
            # Plain 403 with NO WWW-Authenticate challenge header
            return 403, {}, {"error": "forbidden_for_role"}
        return None

    fx.tool_responder = _forbid_restricted

    ext_store, manager, adapter, ledger, base_ctx = _make_adapter_stack(
        tmp_path,
        {
            "id": "corp-http",
            "name": "Corp HTTP",
            "transport": "streamable_http",
            "url": fx.resource,
            "enabled": True,
            "startup_timeout_ms": 5000,
            "tool_timeout_ms": 5000,
            "auth": {"mode": "oauth", "client_id": "corp-client", "scopes": ["tools.read"]},
            "read_only_tools": ["allowed_tool"],
        },
        [
            {"name": "restricted_tool", "inputSchema": {"type": "object"}},
            {"name": "allowed_tool", "inputSchema": {"type": "object"}},
        ],
    )
    started = manager.oauth.start_login(
        "corp-http", ext_store.get_mcp_server_internal("corp-http")["revision"]
    )
    fx.complete_redirect(started["authorization_url"])
    deadline = time.monotonic() + 5.0
    while manager.oauth.login_status("corp-http", started["auth_id"])["state"] != "connected" and time.monotonic() < deadline:
        time.sleep(0.01)

    srv_after_login = ext_store.get_mcp_server_internal("corp-http", require_enabled=True)
    # Re-mark allowed_tool read-only on latest revision so we test both write and read paths
    patched = ext_store.upsert_mcp_server(
        {"read_only_tools": ["allowed_tool"]},
        server_id="corp-http",
        expected_revision=srv_after_login["revision"],
    )
    srv = ext_store.get_mcp_server_internal("corp-http", require_enabled=True)
    assert patched["revision"] == srv["revision"]

    restricted_model = compute_model_tool_name("corp-http", "restricted_tool")
    allowed_model = compute_model_tool_name("corp-http", "allowed_tool")
    inits_before = fx.init_count

    try:
        r_bind = adapter.metadata(restricted_model)["extension_binding"]
        r_ctx = {**base_ctx, "extension_binding": r_bind}
        r_preview = adapter.preview(restricted_model, {}, context=r_ctx)

        with pytest.raises(AgentToolRejected) as exc_403:
            adapter.execute(
                restricted_model,
                {},
                operation_id="op-plain-403",
                context=r_ctx,
                expected_preview=r_preview,
            )
        assert str(exc_403.value) == "agent_mcp_http_forbidden"
        row_403 = ledger.get("op-plain-403")
        assert row_403 is not None and row_403["status"] == "not_executed"

        # OAuth credential row must NOT be polluted by plain 403
        assert manager.oauth.public_status(srv["instance_id"])["state"] == "logged_in"
        cred_row = manager.oauth.credentials.read()["servers"][srv["instance_id"]]
        assert "error" not in cred_row
        assert not cred_row.get("challenge")

        # Subsequent call to another tool on the same server succeeds on the same session without needing login
        a_bind = adapter.metadata(allowed_model)["extension_binding"]
        a_ctx = {**base_ctx, "extension_binding": a_bind}
        ok_out = adapter.execute(
            allowed_model,
            {},
            operation_id="op-after-403",
            context=a_ctx,
        )
        assert ok_out["text"] == "ok:allowed_tool"
        assert fx.init_count == inits_before + 1
    finally:
        manager.close()
        fx.close()
