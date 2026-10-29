"""Transport contracts and optional model diagnostics, separate from admission."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

from ..atomic_io import atomic_write_json, read_json
from ..file_transactions import file_transaction


def client_tools_contract(adapter, capabilities=()):
    """Declared bridge support; never run capability discovery or model inference."""
    return (adapter != "claude_cli" and "api-text-only" not in capabilities
            and (adapter in {"openai_compatible", "openai_chat", "openai_responses", "anthropic_messages",
                             "ollama", "llama_cpp", "codex_cli", "antigravity_cli"}
                 or "client-tools" in capabilities))


class ToolCapabilities:
    def __init__(self, home):
        self.path = Path(home) / "llm" / "tool-capabilities.json"
        self.lock = self.path.with_suffix(".lock")

    @staticmethod
    def fingerprint(manifest):
        # Persist a digest only; endpoint credentials/paths never leave this process.
        config = manifest.to_dict()
        config.pop("capabilities", None)
        if manifest.adapter == "codex_cli":
            config["client_tool_bridge_revision"] = "structured-proposals-v1"
        if manifest.adapter in {"codex_cli", "claude_cli", "antigravity_cli"}:
            from .cli_adapter import CLIAgentAdapter
            try:
                executable = Path(CLIAgentAdapter(manifest.adapter)._command_path())
                stat = executable.stat()
                config["cli_revision"] = [str(executable), stat.st_mtime_ns, stat.st_size]
            except (OSError, RuntimeError):
                config["cli_revision"] = "unavailable"
        return hashlib.sha256(json.dumps(config, sort_keys=True).encode()).hexdigest()

    def _read(self):
        try:
            value = read_json(self.path, max_bytes=128 * 1024)
            return value if isinstance(value, dict) else {}
        except (OSError, ValueError):
            return {}

    def support(self, manifest, model=""):
        key = manifest.package_id + ("/model/" + model if model else "")
        with file_transaction(self.lock):
            row = self._read().get(key, {})
        return row.get("tool_support", "unknown") if row.get("fingerprint") == self.fingerprint(manifest) else "unknown"

    def save(self, manifest, support, model=""):
        key = manifest.package_id + ("/model/" + model if model else "")
        if support not in {"supported", "unsupported", "unknown"}:
            raise ValueError("invalid_tool_support")
        with file_transaction(self.lock):
            rows = self._read()
            rows[key] = {"fingerprint": self.fingerprint(manifest), "tool_support": support}
            # Local sources keep their own evidence; this file tracks only managed services.
            if len(rows) > 128:
                rows.pop(next(iter(rows)))
            atomic_write_json(self.path, rows, max_bytes=128 * 1024)
