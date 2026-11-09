"""A remote plugin's new tools reach the Agent without a manual re-test."""
from __future__ import annotations

import threading
from types import SimpleNamespace

from rynmesh.agent_runtime.extensions import mcp_transport
from rynmesh.agent_runtime.extensions.mcp_transport import McpSessionManager, _catalog_signature


class _Store:
    def __init__(self, server):
        self.server = server
        self.recorded = []

    def list_enabled_mcp_servers_internal(self):
        return [dict(self.server)]

    def get_mcp_server_internal(self, server_id, require_enabled=False, **kwargs):
        return dict(self.server)

    def record_mcp_discovery(self, server_id, **kwargs):
        self.recorded.append(kwargs)
        return {}


def _manager(store, tools, *, fail=False):
    manager = McpSessionManager.__new__(McpSessionManager)
    manager.store = store
    manager.oauth = SimpleNamespace(public_status=lambda _instance: {"state": "logged_in"})
    manager._lock = threading.RLock()
    manager._catalog_checked = {}
    manager._catalog_refreshing = set()

    class _Session:
        def list_all_tools(self):
            if fail:
                raise RuntimeError("network down")
            return tools

        def close(self):
            pass

    manager._create_session = lambda server, **kwargs: _Session()
    return manager


def _stored(tool):
    return {"name": tool["name"], "description": tool.get("description", ""),
            "schema_sha256": mcp_transport._canon_sha256(tool.get("inputSchema") or {"type": "object"}),
            "annotations": {"readOnlyHint": bool((tool.get("annotations") or {}).get("readOnlyHint"))}}


SERVER = {"id": "company", "transport": "streamable_http", "status": "ready", "instance_id": "i", "revision": 3,
          "config_generation": 1}
OLD = [{"name": "query_cash", "description": "Cash", "inputSchema": {"type": "object"}}]
NEW = OLD + [{"name": "wework_approval_get", "description": "Approval detail", "inputSchema": {"type": "object"},
              "annotations": {"readOnlyHint": True}}]


def test_new_remote_tools_are_recorded_and_unchanged_lists_are_left_alone():
    store = _Store({**SERVER, "discovered_tools": [_stored(t) for t in OLD]})
    manager = _manager(store, NEW)
    assert manager._refresh_catalog("company") is True
    assert [t["name"] for t in store.recorded[0]["discovered_tools"]] == ["query_cash", "wework_approval_get"]
    unchanged = _Store({**SERVER, "discovered_tools": [_stored(t) for t in NEW]})
    assert _manager(unchanged, NEW)._refresh_catalog("company") is False and unchanged.recorded == []


def test_failed_refresh_keeps_the_last_good_catalog():
    store = _Store({**SERVER, "discovered_tools": [_stored(t) for t in OLD]})
    assert _manager(store, NEW, fail=True)._refresh_catalog("company") is False
    assert store.recorded == []


def test_refresh_is_rate_limited_and_skips_signed_out_or_local_servers():
    store = _Store({**SERVER, "discovered_tools": []})
    manager = _manager(store, OLD)
    finished = threading.Event()

    def fake_refresh(server_id):
        with manager._lock:
            manager._catalog_refreshing.discard(server_id)  # What the real refresh does when it ends.
        finished.set()
        return True

    manager._refresh_catalog = fake_refresh
    assert manager.refresh_tool_catalogs() == ["company"]
    assert finished.wait(5)
    assert manager.refresh_tool_catalogs() == []  # Within five minutes.
    finished.clear()
    assert manager.refresh_tool_catalogs(force=True) == ["company"]
    assert finished.wait(5)
    signed_out = _manager(_Store({**SERVER}), OLD)
    signed_out.oauth = SimpleNamespace(public_status=lambda _instance: {"state": "logged_out"})
    assert signed_out.refresh_tool_catalogs() == []
    local = _manager(_Store({**SERVER, "transport": "stdio"}), OLD)
    assert local.refresh_tool_catalogs() == []


def test_signature_matches_between_raw_and_stored_tools():
    assert _catalog_signature(NEW, stored=False) == _catalog_signature([_stored(t) for t in NEW], stored=True)
