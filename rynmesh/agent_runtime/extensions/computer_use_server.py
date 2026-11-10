"""First-party STDIO MCP metadata stub for Windows Computer Use.

All live desktop observation and actions execute exclusively through the
host-managed `NativeComputerFacade` (`ryn-computer-worker` native bridge).
Standalone or direct `tools/call` invocation fails closed with
`agent_computer_host_required`.
"""

from __future__ import annotations

import json
import sys
from typing import Any

from ..store import AgentToolRejected
from .store import COMPUTER_USE_BUILTIN_TOOLS


class ComputerUseProvider:
    """Fail-closed standalone provider for the Computer Use MCP server."""

    def __init__(self) -> None:
        self._task_key = "mcp"

    def observe_windows(self, args: dict[str, Any]) -> dict[str, Any]:
        del args
        raise AgentToolRejected("agent_computer_host_required")

    def read_window(self, args: dict[str, Any]) -> dict[str, Any]:
        del args
        raise AgentToolRejected("agent_computer_host_required")

    def act_window(self, args: dict[str, Any]) -> dict[str, Any]:
        del args
        raise AgentToolRejected("agent_computer_host_required")


def dispatch_request(provider: ComputerUseProvider, request: dict[str, Any]) -> dict[str, Any] | None:
    ident = request.get("id")
    method = request.get("method")
    params = request.get("params") if isinstance(request.get("params"), dict) else {}
    if ident is None:
        return None
    if method == "initialize":
        return {
            "jsonrpc": "2.0",
            "id": ident,
            "result": {
                "protocolVersion": "2025-03-26",
                "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": {"name": "Ryn Computer Use", "version": "1.0.0"},
            },
        }
    if method == "ping":
        return {"jsonrpc": "2.0", "id": ident, "result": {}}
    if method == "tools/list":
        tools = [
            {
                "name": t["name"],
                "title": t["title"],
                "description": t["description"],
                "inputSchema": t["input_schema"],
                "annotations": t["annotations"],
            }
            for t in COMPUTER_USE_BUILTIN_TOOLS
        ]
        return {"jsonrpc": "2.0", "id": ident, "result": {"tools": tools}}
    if method == "tools/call":
        name = params.get("name")
        arguments = params.get("arguments") if isinstance(params.get("arguments"), dict) else {}
        try:
            if name == "computer_observe_windows":
                result = provider.observe_windows(arguments)
            elif name == "computer_read_window":
                result = provider.read_window(arguments)
            elif name == "computer_act_window":
                result = provider.act_window(arguments)
            else:
                return {
                    "jsonrpc": "2.0",
                    "id": ident,
                    "error": {"code": -32602, "message": "Unknown tool"},
                }
            return {"jsonrpc": "2.0", "id": ident, "result": result}
        except AgentToolRejected as exc:
            return {
                "jsonrpc": "2.0",
                "id": ident,
                "error": {"code": -32602, "message": str(exc)},
            }
        except Exception:
            return {
                "jsonrpc": "2.0",
                "id": ident,
                "error": {"code": -32603, "message": "agent_computer_action_failed"},
            }
    return {
        "jsonrpc": "2.0",
        "id": ident,
        "error": {"code": -32601, "message": "Unknown method"},
    }


def main() -> None:
    if hasattr(sys.stdin, "reconfigure"):
        sys.stdin.reconfigure(encoding="utf-8")
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    provider = ComputerUseProvider()
    for line in sys.stdin:
        stripped = line.strip()
        if not stripped:
            continue
        try:
            req = json.loads(stripped)
        except ValueError:
            continue
        if not isinstance(req, dict):
            continue
        resp = dispatch_request(provider, req)
        if resp is not None:
            print(json.dumps(resp, ensure_ascii=False, separators=(",", ":")), flush=True)


if __name__ == "__main__":
    main()
