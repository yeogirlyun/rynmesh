from __future__ import annotations

import pytest

from rynmesh.agent_runtime.extensions.computer_use_server import (
    ComputerUseProvider,
    dispatch_request,
)
from rynmesh.agent_runtime.local_desktop import Desktop
from rynmesh.agent_runtime.store import AgentToolRejected


def test_standalone_computer_use_server_metadata_and_fail_closed_calls() -> None:
    provider = ComputerUseProvider()

    # Handshake and discovery remain available for protocol metadata inspection.
    init_resp = dispatch_request(
        provider, {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}}
    )
    assert init_resp is not None
    assert init_resp["result"]["serverInfo"]["name"] == "Ryn Computer Use"

    ping_resp = dispatch_request(
        provider, {"jsonrpc": "2.0", "id": 2, "method": "ping"}
    )
    assert ping_resp == {"jsonrpc": "2.0", "id": 2, "result": {}}

    list_resp = dispatch_request(
        provider, {"jsonrpc": "2.0", "id": 3, "method": "tools/list"}
    )
    assert list_resp is not None
    tool_names = [t["name"] for t in list_resp["result"]["tools"]]
    assert tool_names == [
        "computer_observe_windows",
        "computer_read_window",
        "computer_act_window",
    ]

    # Direct provider methods fail closed with agent_computer_host_required.
    for fn, payload in [
        (provider.observe_windows, {}),
        (provider.read_window, {"hwnd": 100100}),
        (provider.act_window, {"hwnd": 100100, "action": "focus"}),
    ]:
        with pytest.raises(AgentToolRejected) as exc_info:
            fn(payload)
        assert str(exc_info.value) == "agent_computer_host_required"

    # JSON-RPC tools/call for all 3 builtin tools fails closed with -32602 agent_computer_host_required.
    for req_id, tool_name, arguments in [
        (4, "computer_observe_windows", {}),
        (5, "computer_read_window", {"hwnd": 100100}),
        (6, "computer_act_window", {"hwnd": 100100, "action": "focus"}),
    ]:
        call_resp = dispatch_request(
            provider,
            {
                "jsonrpc": "2.0",
                "id": req_id,
                "method": "tools/call",
                "params": {"name": tool_name, "arguments": arguments},
            },
        )
        assert call_resp == {
            "jsonrpc": "2.0",
            "id": req_id,
            "error": {"code": -32602, "message": "agent_computer_host_required"},
        }

    unknown_tool_resp = dispatch_request(
        provider,
        {
            "jsonrpc": "2.0",
            "id": 7,
            "method": "tools/call",
            "params": {"name": "nonexistent_tool", "arguments": {}},
        },
    )
    assert unknown_tool_resp == {
        "jsonrpc": "2.0",
        "id": 7,
        "error": {"code": -32602, "message": "Unknown tool"},
    }


def test_unbound_desktop_constructor_fails_closed() -> None:
    with pytest.raises(AgentToolRejected) as exc_info:
        Desktop()
    assert str(exc_info.value) == "agent_computer_host_required"
