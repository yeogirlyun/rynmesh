"""Plugin tools the Agent is given must match what the plugin offers today.

The failure this file pins: a company center added approval lookup tools, an
already signed-in Ryn never re-read the list, and the Agent told the owner the
feature did not exist. See docs/PLUGIN_TOOL_CATALOG_FIX_PLAN_ZH.md.
"""
from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey

from rynmesh.agent_runtime import tool_menu
from rynmesh.agent_runtime.extensions import ExtensionToolAdapter
from rynmesh.agent_runtime.local_tools import LocalTools
from rynmesh.agent_runtime.operation_ledger import OperationLedger
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
