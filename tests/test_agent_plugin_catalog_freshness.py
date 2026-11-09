"""Plugin tools the Agent is given must match what the plugin offers today.

The failure this file pins: a company center added approval lookup tools, an
already signed-in Ryn never re-read the list, and the Agent told the owner the
feature did not exist. See docs/PLUGIN_TOOL_CATALOG_FIX_PLAN_ZH.md.
"""
from __future__ import annotations

import json
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace

import pytest
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey
from test_agent_extensions_runtime import _assistant, _Catalog, _ExtensionHarnessTools, _Model, _receipts

from rynmesh.agent_runtime import tool_menu
from rynmesh.agent_runtime.extensions import ExtensionToolAdapter, compute_model_tool_name, mcp_transport
from rynmesh.agent_runtime.local_tools import LocalTools
from rynmesh.agent_runtime.loop import AgentLoop
from rynmesh.agent_runtime.operation_ledger import OperationLedger
from rynmesh.agent_runtime.runtime import AgentRuntime
from rynmesh.agent_runtime.store import AgentStore

# Short public stand-ins for a finance plugin and a chat plugin. The chat tools
# share the words "企业微信" with the approval tools, which is what made the
# wrong answer plausible.
DASHBOARD_TOOLS = {
    "wework_approval_search": ("按期间（最多31日）、公司、申请人、流程类型或状态搜索企业微信付款、报销、合同和转账审批单。"
                               "已知单号直接用详情或附件工具。", {"startDate": "开始日期", "endDate": "结束日期"}),
    "wework_approval_get": ("按审批单号查询企业微信财务流程单详情、状态、金额和表单明细。无需日期。",
                            {"spNo": "企业微信审批单号"}),
    "wework_approval_attachments": ("查企业微信审批单有没有附件、几个附件、附件链接或付款凭证。直接传单号，不需要日期。",
                                    {"spNo": "企业微信审批单号"}),
    "journal_records": ("按本人权限查询当前日记账；单次日期含首尾最多 31 天，每页 20 行。",
                        {"startDate": "开始日期", "endDate": "结束日期"}),
    "balance_overview": ("按本人权限查询指定日期的公司总余额、在途资金及分币种汇总。", {"date": "日期"}),
    "pl_report": ("按本人权限查询盈亏报表；单次日期含首尾最多 31 天。", {"startDate": "开始日期"}),
    "data_explore": ("按数据集只读查询财务明细数据，例如银行流水、余额历史和日记账修改记录。", {"dataset": "数据集名称"}),
}
CHAT_TOOLS = {
    "wecom_accounts_sessions": ("列出本机已登录的企业微信账号和最近会话。", {}),
    "wecom_find_targets": ("按姓名或群名查找企业微信同事和群。", {"query": "姓名或群名"}),
    "wecom_search_messages": ("按关键词搜索企业微信聊天记录。", {"keyword": "关键词"}),
    "wecom_read_conversation": ("读取与某个同事或群的企业微信聊天记录。", {"target": "同事或群"}),
    "wecom_read_message": ("读取一条企业微信消息的详情。", {"message_id": "消息编号"}),
    "wecom_send": ("向企业微信同事或群发送文本消息，发送前需要主人确认。", {"target": "同事或群", "text": "消息内容"}),
}


def _tool(name, description, params):
    return {"name": name, "title": name, "description": description,
            "inputSchema": {"type": "object", "additionalProperties": False,
                            "properties": {key: {"type": "string", "description": text} for key, text in params.items()}},
            "annotations": {"readOnlyHint": name != "wecom_send"}}


@pytest.fixture
def adapter(tmp_path: Path):
    home = tmp_path / "home"
    home.mkdir()
    agent_store = AgentStore(home / "agent", X25519PrivateKey.generate())
    ledger = OperationLedger(home)
    local = LocalTools(SimpleNamespace(home=home), lambda: ledger)
    local.store = agent_store
    extensions = ExtensionToolAdapter(local, lambda: ledger)
    extensions.bind_agent_store(agent_store)
    yield extensions
    local.processes.close()
    extensions.close()


def _connect(extensions, server_id, name, tools):
    # A stdio entry that is never started: only the recorded catalog matters here.
    created = extensions.store.upsert_mcp_server({"id": server_id, "name": name, "transport": "stdio",
                                                  "command": sys.executable, "args": ["-c", "pass"], "enabled": True})
    extensions.store.record_mcp_discovery(
        server_id, expected_revision=created["revision"], expected_config_generation=created["config_generation"],
        expected_instance_id=created["instance_id"], status="ready",
        discovered_tools=[_tool(tool, *row) for tool, row in tools.items()])


def _search(extensions, query):
    specs = {spec["function"]["name"]: spec for spec in extensions.specs()}
    ranked = tool_menu._bm25_rank(specs, extensions.metadata, query, limit=8)
    return [extensions.metadata(name)["extension_binding"]["original_tool_name"] for name in ranked]


ROUTES = [
    ("企业微信流程 202611030016", "wework_approval_get"),
    ("查一下审批单 202611090012 的明细", "wework_approval_get"),
    ("上个月的付款审批有哪些", "wework_approval_search"),
    ("下载审批单的附件", "wework_approval_attachments"),
    ("11 月 5 日的日记账", "journal_records"),
    ("给同事发一条企业微信消息", "wecom_send"),
]


@pytest.mark.parametrize(("query", "expected"), ROUTES)
def test_owner_questions_find_the_right_plugin_tool(adapter, query, expected):
    _connect(adapter, "dashboard", "Dashboard", DASHBOARD_TOOLS)
    _connect(adapter, "workspace", "Workspace", CHAT_TOOLS)
    assert expected in _search(adapter, query)[:3]


def test_without_approval_tools_the_search_offers_only_chat_tools(adapter):
    """The incident's catalog: what the Agent saw before the list was refreshed."""
    _connect(adapter, "dashboard", "Dashboard",
             {name: row for name, row in DASHBOARD_TOOLS.items() if not name.startswith("wework_approval")})
    _connect(adapter, "workspace", "Workspace", CHAT_TOOLS)
    found = _search(adapter, "企业微信流程 202611030016")
    assert found and all(name.startswith("wecom_") for name in found[:3])


# ---------------------------------------------------------------------------
# A center whose tool list changes while Ryn is already signed in
# ---------------------------------------------------------------------------

# A loaded test machine can take seconds to open an HTTPS-capable client, so
# tests wait on the refresh itself instead of on wall-clock budgets.
SETTLE = 60

APPROVAL = _tool("wework_approval_get", *DASHBOARD_TOOLS["wework_approval_get"])
JOURNAL = _tool("journal_records", *DASHBOARD_TOOLS["journal_records"])


@pytest.fixture
def center():
    """A loopback MCP center. Its tool list, delay and call log are plain test state."""
    state = {"tools": [JOURNAL], "lists": 0, "calls": [], "list_delay": 0.0, "announce_change": False}

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *_args):
            pass

        def do_DELETE(self):
            self.send_response(204)
            self.send_header("Content-Length", "0")
            self.end_headers()

        def do_POST(self):
            req = json.loads(self.rfile.read(int(self.headers.get("Content-Length", "0"))))
            if req.get("id") is None:
                self.send_response(202)
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            method = req.get("method")
            events = []
            if method == "initialize":
                result = {"protocolVersion": "2025-03-26", "capabilities": {"tools": {"listChanged": True}},
                          "serverInfo": {"name": "center", "version": "1"}}
            elif method == "tools/list":
                state["lists"] += 1
                time.sleep(state["list_delay"])
                result = {"tools": state["tools"]}
            else:
                name = (req.get("params") or {}).get("name")
                state["calls"].append(name)
                result = {"content": [{"type": "text", "text": json.dumps({"tool": name, "status": "已通过"})}]}
                if state["announce_change"]:
                    events.append({"jsonrpc": "2.0", "method": mcp_transport.TOOLS_LIST_CHANGED})
            events.append({"jsonrpc": "2.0", "id": req["id"], "result": result})
            if len(events) > 1:
                body = "".join(f"event: message\ndata: {json.dumps(item)}\n\n" for item in events).encode("utf-8")
                kind = "text/event-stream"
            else:
                body, kind = json.dumps(events[0]).encode("utf-8"), "application/json"
            self.send_response(200)
            self.send_header("Content-Type", kind)
            self.send_header("Mcp-Session-Id", "center-1")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            try:
                self.wfile.write(body)
            except OSError:
                pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server.daemon_threads = True
    threading.Thread(target=server.serve_forever, daemon=True).start()
    state["url"] = f"http://127.0.0.1:{server.server_port}/mcp"
    yield state
    server.shutdown()


@pytest.fixture
def ext_runtime(tmp_path: Path):
    home = tmp_path / "node_home"
    workspace = tmp_path / "workspace"
    home.mkdir()
    workspace.mkdir()
    agent_store = AgentStore(home / "agent", X25519PrivateKey.generate())
    tools = _ExtensionHarnessTools(home, agent_store)
    model = _Model()
    runtime = AgentRuntime(agent_store, model, tools, _Catalog())
    runtime.update_settings({"enabled": True, "model_id": "local:test", "workspace_root": str(workspace),
                             "permission_mode": "automatic", "desktop_access": False})
    yield runtime, model, tools
    tools.close()


def _install_dashboard(extensions, url, *, auth=None, discover=True):
    store = extensions.store
    store.commit_imported_items(
        plugins=[{"id": "dash", "name": "Dashboard", "description": "查看日记账，按单号查询企业微信付款审批单。",
                  "mcp_server_ids": ["dash-mcp"], "skill_ids": []}],
        skills=[],
        mcp_servers=[{"id": "dash-mcp", "plugin_id": "dash", "name": "Dashboard", "transport": "streamable_http",
                      "url": url, "auth": auth or {"mode": "none", "client_id": None, "scopes": [], "callback_port": None},
                      "startup_timeout_ms": 20000, "tool_timeout_ms": 20000}])
    store.set_plugin_enabled("dash", True)
    if discover:
        current = store.get_mcp_server_internal("dash-mcp", require_enabled=False)
        probe = extensions.manager.test_server("dash-mcp", expected_revision=current["revision"])
        assert probe["test_result"]["status"] == "connected"
    return store.get_mcp_server_internal("dash-mcp", require_enabled=False)


def _tool_names(extensions):
    return {extensions.metadata(spec["function"]["name"])["extension_binding"]["original_tool_name"]
            for spec in extensions.specs() if spec["function"]["name"].startswith("mcp__")}


def test_a_center_tool_added_after_sign_in_answers_the_next_question(ext_runtime, center, monkeypatch):
    runtime, model, tools = ext_runtime
    _install_dashboard(tools.extensions, center["url"])
    # The center ships approval lookup later; this copy still holds its first list.
    center["tools"] = [JOURNAL, APPROVAL]
    monkeypatch.setattr(mcp_transport, "CATALOG_REFRESH_SECONDS", 0)  # that list is older than the interval
    monkeypatch.setattr(ExtensionToolAdapter, "TURN_CATALOG_WAIT_SECONDS", float(SETTLE))
    approval = compute_model_tool_name("dash-mcp", "wework_approval_get")
    model.responses = [
        _assistant(name="ryn_discover_tools", args={"query": "企业微信流程 审批单 详情"}, call_id="c_find"),
        _assistant(name=approval, args={"spNo": "202611030016"}, call_id="c_get"),
        _assistant(content="审批单 202611030016 已通过。"),
    ]
    created = runtime.create({"message": "企业微信流程 202611030016"})
    for _ in range(12):
        if runtime.get(created["id"])["status"] in {"completed", "failed", "waiting_approval", "waiting_input"}:
            break
        runtime.run_once()
    # The very first request already listed the new tool; it did not depend on a later background refresh.
    assert approval in json.dumps(model.calls[0][2], ensure_ascii=False)
    assert center["calls"] == ["wework_approval_get"]
    assert any(r["tool"] == approval and r["status"] == "confirmed" for r in _receipts(runtime, created["id"]))


def test_an_unchanged_list_keeps_every_revision(ext_runtime, center, monkeypatch):
    _, _, tools = ext_runtime
    before = _install_dashboard(tools.extensions, center["url"])
    lists = center["lists"]
    monkeypatch.setattr(mcp_transport, "CATALOG_REFRESH_SECONDS", 0)
    tools.extensions.refresh_catalogs_for_turn()
    assert tools.extensions.manager.wait_for_catalogs(None, SETTLE)
    after = tools.extensions.store.get_mcp_server_internal("dash-mcp", require_enabled=False)
    assert center["lists"] > lists  # the center was asked
    # An approval or call bound to this revision is not invalidated by a no-op check.
    assert (after["revision"], after["discovery_revision"]) == (before["revision"], before["discovery_revision"])


def test_a_recent_check_is_not_repeated(ext_runtime, center):
    _, _, tools = ext_runtime
    _install_dashboard(tools.extensions, center["url"])
    tools.extensions.refresh_catalogs_for_turn()
    assert tools.extensions.manager.wait_for_catalogs(None, SETTLE)
    lists = center["lists"]
    tools.extensions.refresh_catalogs_for_turn()
    tools.extensions.refresh_catalogs_for_turn()
    assert center["lists"] == lists


def test_a_slow_center_does_not_hold_the_turn_and_its_list_arrives_later(ext_runtime, center, monkeypatch):
    _, _, tools = ext_runtime
    _install_dashboard(tools.extensions, center["url"])
    center["tools"], center["list_delay"] = [JOURNAL, APPROVAL], 1.5
    monkeypatch.setattr(mcp_transport, "CATALOG_REFRESH_SECONDS", 0)
    started = time.monotonic()
    tools.extensions.refresh_catalogs_for_turn(wait_seconds=0.3)
    assert time.monotonic() - started < 1.2
    center["list_delay"] = 0.0
    assert "wework_approval_get" not in _tool_names(tools.extensions)  # this request uses the old list
    assert tools.extensions.manager.wait_for_catalogs(["dash-mcp"], SETTLE)
    assert "wework_approval_get" in _tool_names(tools.extensions)  # the next one has the new list


def test_a_failed_check_keeps_the_last_good_list(ext_runtime, center, monkeypatch):
    _, _, tools = ext_runtime
    _install_dashboard(tools.extensions, center["url"])
    center["tools"] = "not a list"  # the center answers garbage
    monkeypatch.setattr(mcp_transport, "CATALOG_REFRESH_SECONDS", 0)
    tools.extensions.refresh_catalogs_for_turn()
    assert tools.extensions.manager.wait_for_catalogs(None, SETTLE)
    server = tools.extensions.store.get_mcp_server_internal("dash-mcp", require_enabled=False)
    assert server["status"] == "ready" and "journal_records" in _tool_names(tools.extensions)


def test_tools_list_changed_rereads_the_list_without_waiting_for_the_interval(ext_runtime, center):
    _, _, tools = ext_runtime
    server = _install_dashboard(tools.extensions, center["url"])
    center["tools"], center["announce_change"] = [JOURNAL, APPROVAL], True
    lists = center["lists"]
    tools.extensions.manager.call_tool(
        "dash-mcp", "journal_records", {}, expected_revision=server["revision"],
        expected_config_generation=server["config_generation"], expected_instance_id=server["instance_id"],
        cancelled=lambda: False)
    assert tools.extensions.manager.wait_for_catalogs(["dash-mcp"], SETTLE)
    assert center["lists"] == lists + 1  # the notice itself re-read the list
    assert "wework_approval_get" in _tool_names(tools.extensions)


def test_a_signed_out_plugin_is_named_to_the_model_instead_of_hidden(ext_runtime, center):
    runtime, model, tools = ext_runtime
    _install_dashboard(tools.extensions, center["url"], discover=False,
                       auth={"mode": "oauth", "client_id": "ryn-desktop", "scopes": ["finance.read"], "callback_port": None})
    assert not _tool_names(tools.extensions)  # nothing callable while signed out
    [row] = tools.extensions.unavailable_plugins()
    assert row["plugin"] == "Dashboard" and row["unavailable"][0]["state"] == "authentication_required"
    model.responses = [_assistant(content="请先在插件页重新登录 Dashboard。")]
    runtime.create({"message": "企业微信流程 202611030016"})
    runtime.run_once()
    developer = "\n".join(str(m.get("content")) for m in model.calls[0][1] if m.get("role") in {"developer", "system"})
    assert "Installed plugins" in developer and "Dashboard" in developer and "sign-in needed" in developer
    assert "企业微信付款审批单" in developer  # what the plugin is for reaches the model


def test_the_plugin_index_is_identical_between_turns_and_bounded(ext_runtime, center):
    _, _, tools = ext_runtime
    _install_dashboard(tools.extensions, center["url"])
    first = tools.extensions.plugin_context_note()
    assert first == tools.extensions.plugin_context_note()
    assert "Dashboard" in first and "[Unavailable now" not in first
    assert len(first.encode("utf-8")) <= tools.extensions.PLUGIN_INDEX_BYTES + 200


def test_the_turn_start_check_runs_once_per_owner_message():
    calls = []
    host = SimpleNamespace(extensions=SimpleNamespace(refresh_catalogs_for_turn=lambda: calls.append(1)))
    check = AgentLoop._refresh_plugin_catalogs_at_turn_start
    check(host, {"messages": [{"role": "user", "content": "q1"}]})
    check(host, {"messages": [{"role": "user", "content": "q1"}, {"role": "assistant", "content": "", "tool_calls": []},
                              {"role": "tool", "content": "{}"}]})
    check(host, {"messages": [{"role": "user", "content": "q1"}, {"role": "assistant", "content": "a1"},
                              {"role": "user", "content": "q2"}]})
    assert calls == [1, 1]
