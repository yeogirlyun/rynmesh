"""Ryn Agent Extensions: Plugins, Skills, MCP servers, and Computer Use."""

from __future__ import annotations

from typing import Any

from .adapter import READ_SKILL_FILE_TOOL, ExtensionToolAdapter
from .imports import ExtensionImportManager, parse_skill_markdown
from .mcp_transport import (
    McpSessionManager,
    McpTransportError,
    StdioMcpSession,
    StreamableHttpMcpSession,
)
from .routes import install_extension_routes
from .store import (
    COMPUTER_USE_PLUGIN_ID,
    COMPUTER_USE_SERVER_ID,
    ExtensionStore,
    compute_directory_manifest,
    compute_model_tool_name,
)

__all__ = [
    "COMPUTER_USE_PLUGIN_ID",
    "COMPUTER_USE_SERVER_ID",
    "ComputerUseProvider",
    "ExtensionImportManager",
    "ExtensionStore",
    "ExtensionToolAdapter",
    "McpSessionManager",
    "McpTransportError",
    "READ_SKILL_FILE_TOOL",
    "StdioMcpSession",
    "StreamableHttpMcpSession",
    "compute_directory_manifest",
    "compute_model_tool_name",
    "install_extension_routes",
    "parse_skill_markdown",
]


def __getattr__(name: str) -> Any:
    if name == "ComputerUseProvider":
        from .computer_use_server import ComputerUseProvider

        return ComputerUseProvider
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
