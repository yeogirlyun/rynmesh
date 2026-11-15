"""Tests for S08-I21: stdio launch environment, plugin root/data vars, and persistent dirs."""

from __future__ import annotations

import base64
import io
import json
import os
import sys
import zipfile
from pathlib import Path

import pytest

from rynmesh.agent_runtime.extensions import (
    ExtensionImportManager,
    ExtensionStore,
    McpSessionManager,
    StdioMcpSession,
    compute_directory_manifest,
)
from rynmesh.agent_runtime.extensions.imports import _substitute_plugin_root_token
from rynmesh.agent_runtime.extensions.mcp_transport import _stdio_environment
from rynmesh.agent_runtime.store import AgentError

_MCP_ENV_SERVER_SCRIPT = """\
import json
import os
import pathlib
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
            "serverInfo": {"name": "env-probe", "version": "1.0.0"},
        }
    elif method == "tools/list":
        res = {
            "tools": [
                {
                    "name": "probe_env",
                    "description": "Inspect process environment and cwd",
                    "inputSchema": {
                        "type": "object",
                        "properties": {"touch_markers": {"type": "boolean"}},
                    },
                }
            ]
        }
    elif method == "tools/call":
        args = (req.get("params") or {}).get("arguments") or {}
        touch = bool(args.get("touch_markers"))
        cache_dir = os.environ.get("npm_config_cache", "")
        home_dir = os.environ.get("HOME", "")
        tmp_dir = os.environ.get("TMP", "")
        cache_existed = False
        home_existed = False
        tmp_existed = False
        if cache_dir:
            cp = pathlib.Path(cache_dir) / "npm-marker.txt"
            cache_existed = cp.exists()
            if touch:
                cp.parent.mkdir(parents=True, exist_ok=True)
                cp.write_text("cached", encoding="utf-8")
        if home_dir:
            hp = pathlib.Path(home_dir) / "home-marker.txt"
            home_existed = hp.exists()
            if touch:
                hp.parent.mkdir(parents=True, exist_ok=True)
                hp.write_text("homed", encoding="utf-8")
        if tmp_dir:
            tp = pathlib.Path(tmp_dir) / "tmp-marker.txt"
            tmp_existed = tp.exists()
            if touch:
                tp.parent.mkdir(parents=True, exist_ok=True)
                tp.write_text("tmp", encoding="utf-8")
        payload = {
            "cwd": os.getcwd(),
            "argv": sys.argv,
            "env": dict(os.environ),
            "markers_existed": {
                "cache": cache_existed,
                "home": home_existed,
                "tmp": tmp_existed,
            },
        }
        res = {"content": [{"type": "text", "text": json.dumps(payload)}]}
    else:
        res = {}
    print(json.dumps({"jsonrpc": "2.0", "id": ident, "result": res}), flush=True)
"""


def _parse_probe_payload(call_result: dict) -> dict:
    blocks = call_result.get("content") or []
    return json.loads(blocks[0]["text"])


def _manager_probe(
    manager: McpSessionManager,
    store: ExtensionStore,
    server_id: str,
    arguments: dict,
) -> dict:
    cur = store.get_mcp_server_internal(server_id, require_enabled=False)
    res = manager.test_server(server_id, expected_revision=cur["revision"])
    assert res["test_result"]["status"] == "connected"
    after = store.get_mcp_server_internal(server_id, require_enabled=True)
    call_res = manager.call_tool(
        server_id,
        "probe_env",
        arguments,
        expected_revision=after["revision"],
        expected_config_generation=after["config_generation"],
        expected_instance_id=after["instance_id"],
    )
    return _parse_probe_payload(call_res)


def _build_plugin_zip(files: dict[str, str]) -> str:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for rel_path, content in files.items():
            zf.writestr(rel_path, content)
    return base64.b64encode(buf.getvalue()).decode("ascii")


def test_stdio_env_whitelist_tz_and_relative_ca_paths_resolved(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    host_cwd = tmp_path / "host-cwd"
    host_cwd.mkdir()
    monkeypatch.chdir(host_cwd)

    monkeypatch.setenv("TZ", "Asia/Tokyo")
    monkeypatch.setenv("USERNAME", "ryn-local-user")
    monkeypatch.setenv("TERM", "xterm-256color")
    monkeypatch.setenv("SSL_CERT_FILE", "certs/custom-ca.pem")
    monkeypatch.setenv("SSL_CERT_DIR", "certs/ca-dir")
    monkeypatch.setenv("REQUESTS_CA_BUNDLE", "certs/requests-ca.pem")
    monkeypatch.setenv("NODE_EXTRA_CA_CERTS", "certs/node-ca.pem")

    script = tmp_path / "probe_server.py"
    script.write_text(_MCP_ENV_SERVER_SCRIPT, encoding="utf-8")

    store = ExtensionStore(tmp_path / "store")
    server_cwd = tmp_path / "other-cwd"
    server_cwd.mkdir()

    session = StdioMcpSession(
        {
            "id": "ca-probe",
            "command": sys.executable,
            "args": ["-u", str(script)],
            "cwd": str(server_cwd),
            "startup_timeout_ms": 5000,
            "tool_timeout_ms": 5000,
        },
        fallback_cwd=store.root / "mcp_runtime",
    )
    try:
        raw = session.call_tool("probe_env", {})
        data = _parse_probe_payload(raw)
        child_env = {k.upper(): v for k, v in data["env"].items()}
        assert child_env["TZ"] == "Asia/Tokyo"
        assert child_env["USERNAME"] == "ryn-local-user"
        assert child_env["TERM"] == "xterm-256color"

        for var_name, rel_target in (
            ("SSL_CERT_FILE", "certs/custom-ca.pem"),
            ("SSL_CERT_DIR", "certs/ca-dir"),
            ("REQUESTS_CA_BUNDLE", "certs/requests-ca.pem"),
            ("NODE_EXTRA_CA_CERTS", "certs/node-ca.pem"),
        ):
            val = child_env.get(var_name)
            assert val is not None, f"{var_name} missing in child env"
            assert Path(val).is_absolute(), f"{var_name} not absolute: {val}"
            assert Path(val) == Path(os.path.abspath(str(host_cwd / rel_target)))
    finally:
        session.close()


def test_plugin_root_and_plugin_data_token_substitution_and_traversal_guard(
    tmp_path: Path,
) -> None:
    store = ExtensionStore(tmp_path / "store")
    pkg_root = store.allocate_package_dir("plugin", "demo-plugin")
    (pkg_root / "bin").mkdir(parents=True, exist_ok=True)
    bin_x = pkg_root / "bin" / "x"
    bin_x.write_text("#!/bin/sh\n", encoding="utf-8")

    resolved_bin = _substitute_plugin_root_token("${PLUGIN_ROOT}/bin/x", pkg_root)
    assert Path(resolved_bin) == bin_x.resolve()

    expected_data_dir = (store.root / "plugin_data" / "demo-plugin").resolve()
    resolved_data = _substitute_plugin_root_token(
        "${PLUGIN_DATA}/state.sqlite", pkg_root, expected_data_dir
    )
    assert Path(resolved_data) == (expected_data_dir / "state.sqlite").resolve()

    # Infer plugin_data_dir from allocated store package_root even with 2 arguments
    resolved_data_inferred = _substitute_plugin_root_token("${PLUGIN_DATA}/cache", pkg_root)
    assert Path(resolved_data_inferred) == (expected_data_dir / "cache").resolve()

    with pytest.raises(AgentError) as exc_root:
        _substitute_plugin_root_token("${PLUGIN_ROOT}/../escape", pkg_root)
    assert exc_root.value.args == ("agent_extension_archive_traversal",)

    with pytest.raises(AgentError) as exc_data:
        _substitute_plugin_root_token(
            "${PLUGIN_DATA}/../escape", pkg_root, expected_data_dir
        )
    assert exc_data.value.args == ("agent_extension_archive_traversal",)


def test_plugin_stdio_default_cwd_is_pkg_root_manifest_hash_intact_and_plugin_env_protected(
    tmp_path: Path,
) -> None:
    store = ExtensionStore(tmp_path / "store")
    importer = ExtensionImportManager(store)

    zip_b64 = _build_plugin_zip(
        {
            ".claude-plugin/plugin.json": json.dumps(
                {
                    "id": "env-plugin",
                    "name": "Env Plugin",
                    "version": "1.0.0",
                    "description": "Plugin stdio environment test",
                }
            ),
            "bin/server.py": _MCP_ENV_SERVER_SCRIPT,
            ".mcp.json": json.dumps(
                {
                    "mcpServers": {
                        "worker": {
                            "command": sys.executable,
                            "args": ["-u", "${PLUGIN_ROOT}/bin/server.py", "${PLUGIN_DATA}/db"],
                            "env": {
                                "PLUGIN_ROOT": "/spoofed-root",
                                "PLUGIN_DATA": "/spoofed-data",
                                "SAFE_CUSTOM_VAR": "kept-value",
                            },
                        }
                    }
                }
            ),
        }
    )

    preview = importer.preview({"kind": "plugin", "zip_base64": zip_b64})
    assert preview["can_import"] is True
    ov0 = store.overview()
    committed = importer.commit(
        preview["preview_id"], expected_revision=ov0["revision"]
    )

    plugin_id = next(p["id"] for p in committed["plugins"] if p["id"] != "computer-use")
    server_id = next(m["id"] for m in committed["mcp_servers"] if m["id"] != "computer-use")
    internal_before = store.get_mcp_server_internal(server_id, require_enabled=False)

    # Find installed plugin package root and verify default cwd was set to package root
    pkg_dir = Path(internal_before["cwd"]).resolve()
    assert (pkg_dir / "bin" / "server.py").is_file()
    manifest_before = compute_directory_manifest(pkg_dir)

    # Enable plugin and server, then invoke via McpSessionManager
    p_view = next(p for p in store.overview()["plugins"] if p["id"] == plugin_id)
    store.set_plugin_enabled(plugin_id, True, expected_revision=p_view["revision"])
    s_view = next(s for s in store.overview()["mcp_servers"] if s["id"] == server_id)
    store.upsert_mcp_server(
        {"enabled": True}, server_id=server_id, expected_revision=s_view["revision"]
    )

    manager = McpSessionManager(store)
    try:
        data = _manager_probe(manager, store, server_id, {"touch_markers": True})
        child_env = {k.upper(): v for k, v in data["env"].items()}

        # Default cwd is plugin package root
        assert Path(data["cwd"]).resolve() == pkg_dir
        # Args substituted ${PLUGIN_ROOT} and ${PLUGIN_DATA}
        expected_data_dir = (store.root / "plugin_data" / plugin_id).resolve()
        assert Path(data["argv"][0]).resolve() == (pkg_dir / "bin" / "server.py").resolve()
        assert Path(data["argv"][1]).resolve() == (expected_data_dir / "db").resolve()

        # PLUGIN_ROOT and PLUGIN_DATA injected and NOT overridden by secret_env
        assert Path(child_env["PLUGIN_ROOT"]).resolve() == pkg_dir
        assert Path(child_env["PLUGIN_DATA"]).resolve() == expected_data_dir
        assert child_env["SAFE_CUSTOM_VAR"] == "kept-value"

        # Session temp dir is under store.root/mcp_runtime, NOT inside pkg_dir
        tmp_path_child = Path(child_env["TMP"]).resolve()
        assert (store.root / "mcp_runtime").resolve() in tmp_path_child.parents
        assert pkg_dir not in tmp_path_child.parents

        # While session is still running, plugin package manifest hash is intact
        manifest_during = compute_directory_manifest(pkg_dir)
        assert manifest_during["manifest_sha256"] == manifest_before["manifest_sha256"]
    finally:
        manager.close()

    manifest_after = compute_directory_manifest(pkg_dir)
    assert manifest_after["manifest_sha256"] == manifest_before["manifest_sha256"]


def test_stdio_proxy_and_node_path_passthrough(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HTTPS_PROXY", "http://127.0.0.1:8899")
    monkeypatch.setenv("HTTP_PROXY", "http://127.0.0.1:8898")
    monkeypatch.setenv("NO_PROXY", "localhost,127.0.0.1")
    monkeypatch.setenv("ALL_PROXY", "socks5://127.0.0.1:1080")
    monkeypatch.setenv("NODE_PATH", str(tmp_path / "custom_node_modules"))

    script = tmp_path / "probe_server.py"
    script.write_text(_MCP_ENV_SERVER_SCRIPT, encoding="utf-8")
    store = ExtensionStore(tmp_path / "store")

    session = StdioMcpSession(
        {
            "id": "proxy-probe",
            "command": sys.executable,
            "args": ["-u", str(script)],
            "startup_timeout_ms": 5000,
            "tool_timeout_ms": 5000,
        },
        fallback_cwd=store.root / "mcp_runtime",
    )
    try:
        data = _parse_probe_payload(session.call_tool("probe_env", {}))
        child_env = {k.upper(): v for k, v in data["env"].items()}
        assert child_env["HTTPS_PROXY"] == "http://127.0.0.1:8899"
        assert child_env["HTTP_PROXY"] == "http://127.0.0.1:8898"
        assert child_env["NO_PROXY"] == "localhost,127.0.0.1"
        assert child_env["ALL_PROXY"] == "socks5://127.0.0.1:1080"
        assert child_env["NODE_PATH"] == str(tmp_path / "custom_node_modules")
    finally:
        session.close()

    # Also verify lowercase proxy keys on POSIX-style case-sensitive mapping
    fake_os_env = {
        "PATH": os.environ.get("PATH", ""),
        "https_proxy": "http://127.0.0.1:9999",
        "no_proxy": "internal.local",
    }
    monkeypatch.setattr(os, "environ", fake_os_env)
    unit_env = _stdio_environment(str(tmp_path / "tmp"), None)
    assert unit_env.get("https_proxy") == "http://127.0.0.1:9999"
    assert unit_env.get("no_proxy") == "internal.local"


def test_persistent_npm_uv_pip_cache_and_home_retained_across_sessions(
    tmp_path: Path,
) -> None:
    script = tmp_path / "probe_server.py"
    script.write_text(_MCP_ENV_SERVER_SCRIPT, encoding="utf-8")
    store = ExtensionStore(tmp_path / "store")
    srv_cfg = {
        "id": "cached-srv",
        "command": sys.executable,
        "args": ["-u", str(script)],
        "startup_timeout_ms": 5000,
        "tool_timeout_ms": 5000,
    }

    expected_cache = (store.root / "mcp_cache" / "cached-srv").resolve()
    expected_home = (store.root / "mcp_home" / "cached-srv").resolve()

    unit_cache_env = _stdio_environment(
        str(tmp_path / "tmp"), None, cache_dir=str(expected_cache)
    )
    assert unit_cache_env["npm_config_cache"] == str(expected_cache)

    sess1 = StdioMcpSession(srv_cfg, fallback_cwd=store.root / "mcp_runtime")
    try:
        d1 = _parse_probe_payload(sess1.call_tool("probe_env", {"touch_markers": True}))
        env1 = {k.upper(): v for k, v in d1["env"].items()}
        assert d1["markers_existed"] == {"cache": False, "home": False, "tmp": False}
        assert Path(env1["NPM_CONFIG_CACHE"]).resolve() == expected_cache
        assert Path(env1["UV_CACHE_DIR"]).resolve() == expected_cache
        assert Path(env1["PIP_CACHE_DIR"]).resolve() == expected_cache
        assert Path(env1["HOME"]).resolve() == expected_home
        tmp1 = Path(env1["TMP"]).resolve()
    finally:
        sess1.close()

    assert not tmp1.exists()

    sess2 = StdioMcpSession(srv_cfg, fallback_cwd=store.root / "mcp_runtime")
    try:
        d2 = _parse_probe_payload(sess2.call_tool("probe_env", {"touch_markers": False}))
        env2 = {k.upper(): v for k, v in d2["env"].items()}
        assert d2["markers_existed"]["cache"] is True
        assert d2["markers_existed"]["home"] is True
        assert d2["markers_existed"]["tmp"] is False
        assert Path(env2["TMP"]).resolve() != tmp1
    finally:
        sess2.close()


def test_inherit_user_home_isolation_by_default_and_opt_in_owner_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    real_home = tmp_path / "real-owner-home"
    real_home.mkdir()
    monkeypatch.setenv("HOME", str(real_home))
    monkeypatch.setenv("USERPROFILE", str(real_home))

    script = tmp_path / "probe_server.py"
    script.write_text(_MCP_ENV_SERVER_SCRIPT, encoding="utf-8")
    store = ExtensionStore(tmp_path / "store")

    # External MCP import must NOT honor inherit_user_home (owner-only setting)
    importer = ExtensionImportManager(store)
    prev = importer.preview(
        {
            "kind": "mcp",
            "config_text": json.dumps(
                {
                    "mcpServers": {
                        "home-srv": {
                            "command": sys.executable,
                            "args": ["-u", str(script)],
                            "inherit_user_home": True,
                        }
                    }
                }
            ),
        }
    )
    ov0 = store.overview()
    committed = importer.commit(prev["preview_id"], expected_revision=ov0["revision"])
    srv_view = next(m for m in committed["mcp_servers"] if m["id"] == "home-srv")
    server_id = srv_view["id"]
    assert srv_view.get("inherit_user_home") is False

    # Enable server without setting inherit_user_home -> isolated persistent mcp_home
    srv_enabled = store.upsert_mcp_server(
        {"enabled": True}, server_id=server_id, expected_revision=srv_view["revision"]
    )
    assert srv_enabled["inherit_user_home"] is False

    manager = McpSessionManager(store)
    try:
        d_isolated = _manager_probe(manager, store, server_id, {})
        env_iso = {k.upper(): v for k, v in d_isolated["env"].items()}
        expected_priv_home = (store.root / "mcp_home" / server_id).resolve()
        assert Path(env_iso["HOME"]).resolve() == expected_priv_home
        assert Path(env_iso["USERPROFILE"]).resolve() == expected_priv_home
        assert Path(env_iso["APPDATA"]).resolve() == expected_priv_home
        assert Path(env_iso["LOCALAPPDATA"]).resolve() == expected_priv_home
        assert Path(env_iso["HOME"]).resolve() != real_home.resolve()

        # Owner explicitly enables inherit_user_home -> real user home is used, TMP stays isolated
        cur = next(s for s in store.overview()["mcp_servers"] if s["id"] == server_id)
        updated = store.upsert_mcp_server(
            {"inherit_user_home": True},
            server_id=server_id,
            expected_revision=cur["revision"],
        )
        assert updated["inherit_user_home"] is True
        manager.invalidate_server(server_id)

        d_real = _manager_probe(manager, store, server_id, {})
        env_real = {k.upper(): v for k, v in d_real["env"].items()}
        assert Path(env_real["HOME"]).resolve() == real_home.resolve()
        assert Path(env_real["USERPROFILE"]).resolve() == real_home.resolve()
        assert (store.root / "mcp_runtime").resolve() in Path(env_real["TMP"]).resolve().parents
    finally:
        manager.close()
