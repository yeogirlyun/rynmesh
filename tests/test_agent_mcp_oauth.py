"""Tests for S08-I52: OAuth credential self-healing without breaking unrelated MCP servers."""

from __future__ import annotations

import logging
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey

from rynmesh.agent_runtime.extensions import ExtensionStore, McpSessionManager
from rynmesh.agent_runtime.extensions.adapter import ExtensionToolAdapter
from rynmesh.agent_runtime.extensions.company_authorization import CompanyAuthorizationChannel
from rynmesh.agent_runtime.extensions.mcp_oauth import (
    VERSION,
    McpOAuthError,
    McpOAuthManager,
    McpOAuthStore,
)

_STDIO_ECHO_SCRIPT = """\
import json
import sys

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
        res = {
            "protocolVersion": "2025-03-26",
            "capabilities": {"tools": {}},
            "serverInfo": {"name": "stdio-echo", "version": "1.0.0"},
        }
    elif method == "tools/list":
        res = {
            "tools": [
                {
                    "name": "echo",
                    "description": "Echo message",
                    "inputSchema": {
                        "type": "object",
                        "properties": {"msg": {"type": "string"}},
                    },
                }
            ]
        }
    elif method == "tools/call":
        args = (req.get("params") or {}).get("arguments") or {}
        res = {
            "content": [{"type": "text", "text": f"echo:{args.get('msg', '')}"}],
            "isError": False,
        }
    else:
        res = {}
    sys.stdout.write(json.dumps({"jsonrpc": "2.0", "id": ident, "result": res}) + "\\n")
    sys.stdout.flush()
"""


def test_corrupt_mcp_oauth_recovers_preserves_stdio_and_marks_oauth_reset(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """T1: Corrupt mcp_oauth.json self-heals inside lock, creates .corrupt- backup,
    logs without raw content, lets McpSessionManager + stdio server work, and marks
    OAuth servers logged_out with error=agent_mcp_oauth_credentials_reset."""
    script_path = tmp_path / "stdio_echo.py"
    script_path.write_text(_STDIO_ECHO_SCRIPT, encoding="utf-8")

    store = ExtensionStore(tmp_path / "store")
    store.upsert_mcp_server(
        {
            "id": "stdio-echo",
            "name": "Stdio Echo",
            "transport": "stdio",
            "command": sys.executable,
            "args": [str(script_path)],
            "enabled": True,
        }
    )
    store.upsert_mcp_server(
        {
            "id": "remote-oauth",
            "name": "Remote OAuth",
            "transport": "streamable_http",
            "url": "http://127.0.0.1:19999/mcp",
            "enabled": True,
            "auth": {"mode": "oauth", "client_id": "desktop-app"},
        }
    )
    store.upsert_mcp_server(
        {
            "id": "remote-noauth",
            "name": "Remote NoAuth",
            "transport": "streamable_http",
            "url": "http://127.0.0.1:19998/mcp",
            "enabled": True,
            "auth": {"mode": "none"},
        }
    )

    oauth_path = store.root / "mcp_oauth.json"
    secret_marker = "SYNTHETIC_CORRUPT_PAYLOAD_MARKER_DO_NOT_LOG"
    oauth_path.write_bytes(f"{{not-valid-json:{secret_marker}\xff\xfe".encode("latin1"))

    caplog.set_level(logging.WARNING, logger="rynmesh.agent_runtime.diagnostic_log")
    manager = McpSessionManager(store)
    try:
        backups = sorted(store.root.glob("mcp_oauth.json.corrupt-*"))
        assert len(backups) == 1
        assert secret_marker.encode("latin1") in backups[0].read_bytes()

        # Diagnostic log recorded recovery without leaking corrupt file contents or paths
        joined_logs = "\n".join(rec.getMessage() for rec in caplog.records)
        assert "credentials_recovered" in joined_logs
        assert secret_marker not in joined_logs
        assert str(oauth_path) not in joined_logs

        # Stdio MCP server probe and tool call succeed normally
        initial_stdio = store.get_mcp_server_internal("stdio-echo")
        probe = manager.test_server("stdio-echo", expected_revision=initial_stdio["revision"])
        assert probe["test_result"]["status"] == "connected"
        stdio_srv = store.get_mcp_server_internal("stdio-echo")
        result = manager.call_tool(
            "stdio-echo",
            "echo",
            {"msg": "ok-after-recovery"},
            expected_revision=stdio_srv["revision"],
            expected_config_generation=stdio_srv["config_generation"],
            expected_instance_id=stdio_srv["instance_id"],
        )
        assert result["content"][0]["text"] == "echo:ok-after-recovery"

        # OAuth server reports state=logged_out and error=agent_mcp_oauth_credentials_reset
        oauth_srv = store.get_mcp_server_internal("remote-oauth")
        status = manager.oauth.public_status(oauth_srv["instance_id"])
        assert status["state"] == "logged_out"
        assert status["error"] == "agent_mcp_oauth_credentials_reset"

        # Server that never required auth stays not_required without reset error
        noauth_srv = store.get_mcp_server_internal("remote-noauth")
        noauth_status = manager.oauth.public_status(noauth_srv["instance_id"])
        assert noauth_status["state"] == "not_required"
        assert noauth_status["error"] is None

        # Overview surfaces the reset error on enabled OAuth servers
        adapter = ExtensionToolAdapter(
            SimpleNamespace(),
            lambda: None,
            extension_store=store,
            session_manager=manager,
        )
        overview = adapter.overview()
        remote_view = next(s for s in overview["mcp_servers"] if s["id"] == "remote-oauth")
        assert remote_view["oauth"]["state"] == "logged_out"
        assert remote_view["oauth"]["error"] == "agent_mcp_oauth_credentials_reset"
        assert remote_view["status"] == "NeedsAuth"
        assert remote_view["status_message"] == "agent_mcp_oauth_credentials_reset"
    finally:
        manager.close()


def test_wrong_key_or_schema_recovers_and_keeps_at_most_three_corrupt_backups(
    tmp_path: Path,
) -> None:
    """T2: Decryption failure (node key rotation) and schema mismatch also self-heal,
    retaining at most 3 mcp_oauth.json.corrupt-* backups."""
    store = ExtensionStore(tmp_path / "store")
    cred = McpOAuthStore(store)

    # 1) Sealed with a different node key -> self-heals
    other_store = ExtensionStore(tmp_path / "other", X25519PrivateKey.generate())
    other_cred = McpOAuthStore(other_store)
    other_cred.path = cred.path
    other_cred.lock = cred.lock
    other_cred._write({"version": VERSION, "servers": {"inst-1": {"tokens": {"access_token": "x"}}}})

    doc1 = cred.read()
    assert doc1["version"] == VERSION
    assert doc1["servers"] == {}
    assert isinstance(doc1.get("recovered_at"), str) and doc1["recovered_at"]

    # 2..5) Four more corruptions (wrong version, non-dict servers, bad JSON, missing fields)
    bad_payloads = [
        {"version": "wrong.v0", "servers": {}},
        {"version": VERSION, "servers": ["not-a-dict"]},
    ]
    for bad in bad_payloads:
        cred._write(bad)
        doc = cred.read()
        assert doc["servers"] == {}
        assert "recovered_at" in doc

    cred.path.write_text('{"version": "ryn.agent.mcp-oauth.v1"}', encoding="utf-8")
    assert cred.read()["servers"] == {}

    cred.path.write_bytes(b"corrupt-5")
    assert cred.read()["servers"] == {}

    backups = sorted(store.root.glob("mcp_oauth.json.corrupt-*"))
    assert len(backups) == 3


def test_transient_read_io_error_raises_storage_unavailable_without_renaming(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """T3: Transient OS read IO errors still raise agent_mcp_oauth_storage_unavailable
    and never rename or delete mcp_oauth.json."""
    store = ExtensionStore(tmp_path / "store")
    cred = McpOAuthStore(store)
    with cred.transaction() as servers:
        servers["inst-keep"] = {"tokens": {"access_token": "keep-me"}}

    original_bytes = cred.path.read_bytes()

    # Clear in-memory cache to force disk read, then simulate transient read OSError
    fresh_cred = McpOAuthStore(store)
    real_read_bytes = Path.read_bytes

    def fail_read_bytes(self_path: Path) -> bytes:
        if self_path == cred.path:
            raise PermissionError(13, "transient sharing violation")
        return real_read_bytes(self_path)

    monkeypatch.setattr(Path, "read_bytes", fail_read_bytes)

    with pytest.raises(McpOAuthError, match="agent_mcp_oauth_storage_unavailable"):
        fresh_cred.read()

    assert cred.path.exists()
    assert list(store.root.glob("mcp_oauth.json.corrupt-*")) == []

    # Once transient IO error clears, original credentials are intact
    monkeypatch.undo()
    restored = fresh_cred.read()
    assert restored["servers"]["inst-keep"]["tokens"]["access_token"] == "keep-me"
    assert cred.path.read_bytes() == original_bytes


def test_company_authorization_init_failure_logged_without_failing_manager(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """T4: Failure in company_authorization.sync_bindings/start during McpOAuthManager.__init__
    only logs diagnostics and does not abort manager construction."""
    store = ExtensionStore(tmp_path / "store")
    cred = McpOAuthStore(store)
    with cred.transaction(document=True) as doc:
        doc["company_authorization"] = {"service": "https://company.example"}

    def boom(self_chan: CompanyAuthorizationChannel) -> None:
        raise RuntimeError("company sync exploded")

    monkeypatch.setattr(CompanyAuthorizationChannel, "sync_bindings", boom)
    caplog.set_level(logging.WARNING, logger="rynmesh.agent_runtime.diagnostic_log")

    manager = McpOAuthManager(store)
    try:
        joined = "\n".join(rec.getMessage() for rec in caplog.records)
        assert "RuntimeError" in joined or "company" in joined
        assert "company sync exploded" not in joined
    finally:
        manager.close()


def test_company_plugin_server_without_oauth_or_token_stays_not_required(
    tmp_path: Path,
) -> None:
    """Rework: Company plugin server without auth.mode=oauth, without stored token,
    and without recovered_at must remain state='not_required' (not 'logged_out')."""
    store = ExtensionStore(tmp_path / "store")
    store.upsert_mcp_server(
        {
            "id": "company-ws-mcp",
            "name": "Company Workspace MCP",
            "plugin_id": "company-workspace",
            "transport": "streamable_http",
            "url": "http://127.0.0.1:19997/mcp",
            "enabled": True,
        }
    )
    manager = McpSessionManager(store)
    try:
        srv = store.get_mcp_server_internal("company-ws-mcp")
        status = manager.oauth.public_status(srv["instance_id"])
        assert status["state"] == "not_required"
        assert status["error"] is None

        adapter = ExtensionToolAdapter(
            SimpleNamespace(),
            lambda: None,
            extension_store=store,
            session_manager=manager,
        )
        overview = adapter.overview()
        view = next(s for s in overview["mcp_servers"] if s["id"] == "company-ws-mcp")
        assert view["oauth"]["state"] == "not_required"
        assert view["status"] != "NeedsAuth"
    finally:
        manager.close()


def test_recover_corrupt_write_failure_retries_persist_on_next_read(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Optional rework: If persisting recovered_at fails transiently during recovery,
    subsequent reads retain recovered_at and retry persisting it to disk."""
    from rynmesh.agent_runtime.extensions import mcp_oauth as mcp_oauth_mod

    store = ExtensionStore(tmp_path / "store")
    cred = McpOAuthStore(store)
    cred.path.write_bytes(b"corrupt-bytes-before-transient-write-failure")

    real_atomic_write = mcp_oauth_mod.atomic_write_json
    fail_writes = True

    def flaky_atomic_write(path: Path, payload: object, **kwargs: object) -> None:
        if fail_writes and path == cred.path:
            raise OSError("transient disk full")
        real_atomic_write(path, payload, **kwargs)

    monkeypatch.setattr(mcp_oauth_mod, "atomic_write_json", flaky_atomic_write)

    doc1 = cred.read()
    recovered_at = doc1.get("recovered_at")
    assert isinstance(recovered_at, str) and recovered_at
    assert not cred.path.exists()

    # Still failing to write on second read -> still preserves recovered_at in memory
    doc2 = cred.read()
    assert doc2.get("recovered_at") == recovered_at

    # Once disk write recovers, next read flushes recovered_at to disk for future restarts
    fail_writes = False
    doc3 = cred.read()
    assert doc3.get("recovered_at") == recovered_at
    assert cred.path.exists()

    restarted_cred = McpOAuthStore(store)
    assert restarted_cred.read().get("recovered_at") == recovered_at
