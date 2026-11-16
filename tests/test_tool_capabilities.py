"""Tests for S01-I37 Steps 7 & 8 (T7, T8, T9): ToolCapabilities stat cache, LRU eviction,
and bridge detection of Ollama/llama.cpp 'does not support tools' 400 errors."""
from __future__ import annotations

import io
import json
import threading
import urllib.error
from email.message import Message
from pathlib import Path
from types import SimpleNamespace

import pytest

from rynmesh.agent_runtime.bridge import AgentModelBridge
from rynmesh.agent_runtime.models import ModelCatalog
from rynmesh.agent_runtime.store import AgentError
from rynmesh.llm_package import tool_capabilities
from rynmesh.llm_package.adapters import OpenAICompatibleAdapter
from rynmesh.llm_package.cli_adapter import CLIAgentAdapter
from rynmesh.llm_package.manifest import LLMPackageManifest
from rynmesh.llm_package.tool_capabilities import ToolCapabilities, client_tools_contract


def _make_manifest(package_id: str, adapter: str = "ollama", model: str = "qwen-tiny") -> LLMPackageManifest:
    return LLMPackageManifest(
        package_id=package_id,
        mode="ollama" if adapter == "ollama" else "managed",
        adapter=adapter,
        runtime="external",
        public_model_alias=model,
        model=model,
        base_url="http://127.0.0.1:11434",
        capabilities=["text-generation"],
    )


def test_t7_cli_fingerprint_caches_executable_stat_for_30s(tmp_path, monkeypatch):
    """T7: ToolCapabilities.fingerprint 的 CLI 可执行文件 stat 在进程内缓存 30 秒。"""
    clock = [500.0]
    monkeypatch.setattr(tool_capabilities.time, "monotonic", lambda: clock[0])
    tool_capabilities._CLI_STAT_CACHE.clear()

    exe = tmp_path / "codex-bin"
    exe.write_bytes(b"v1-binary")
    stat_calls = {"n": 0}
    real_stat = Path.stat

    def counting_stat(self, *args, **kwargs):
        if self == exe:
            stat_calls["n"] += 1
        return real_stat(self, *args, **kwargs)

    monkeypatch.setattr(Path, "stat", counting_stat)
    monkeypatch.setattr(CLIAgentAdapter, "_command_path", lambda self: str(exe))

    manifest = LLMPackageManifest(
        package_id="codex-cli",
        mode="codex_cli",
        adapter="codex_cli",
        runtime="external",
        public_model_alias="Codex CLI",
    )
    fp1 = ToolCapabilities.fingerprint(manifest)
    fp2 = ToolCapabilities.fingerprint(manifest)
    assert fp1 == fp2
    assert stat_calls["n"] == 1

    # After 31 seconds, stat runs once again.
    clock[0] += 31.0
    fp3 = ToolCapabilities.fingerprint(manifest)
    assert fp3 == fp1
    assert stat_calls["n"] == 2


def test_t8_ollama_400_does_not_support_tools_saves_unsupported_and_disables_agent_model(tmp_path, monkeypatch):
    """T8: Ollama 保持声明支持，但上游 400 含 'does not support tools' 时记 unsupported、抛 agent_model_tools_unsupported，下次选模型时 agent 不可用。"""
    assert client_tools_contract("ollama") is True
    assert client_tools_contract("llama_cpp") is True

    manifest = _make_manifest("ollama-small", adapter="ollama", model="tiny-nothink:1b")
    caps_store = ToolCapabilities(tmp_path)
    adapter = OpenAICompatibleAdapter(base_url="http://127.0.0.1:11434", model="tiny-nothink:1b")

    def reject_tools(req, timeout=None):
        hdrs = Message()
        hdrs["Content-Type"] = "application/json"
        err_body = json.dumps({"error": {"message": "registry.ollama.ai/library/tiny-nothink:1b does not support tools", "type": "api_error"}}).encode("utf-8")
        raise urllib.error.HTTPError(req.full_url, 400, "Bad Request", hdrs, io.BytesIO(err_body))

    from rynmesh.llm_package import adapters as adapters_mod

    monkeypatch.setattr(adapters_mod, "model_urlopen", reject_tools)

    manager = SimpleNamespace(
        manifest=manifest,
        adapter=adapter,
        _slots=threading.BoundedSemaphore(1),
        _lock=threading.Lock(),
        _running=0,
    )
    store = SimpleNamespace(peer_id="self-node", home=tmp_path, llm_tool_capabilities=caps_store)

    def discover(_net):
        return [
            {
                "peer_id": "self-node",
                "online": True,
                "ready": True,
                "available": True,
                "chat_protocol": "rynmesh.chat.v1",
                "tool_support": caps_store.support(manifest),
                "service": {
                    "package_id": manifest.package_id,
                    "model_alias": manifest.public_model_alias,
                    "adapter": "ollama",
                    "runtime": "external",
                    "capabilities": ["text-generation", "client-tools"],
                    "context_window": 8192,
                    "max_output_tokens": 2048,
                },
            }
        ]

    services = {
        "source_store": SimpleNamespace(read=lambda: {}),
        "discover": discover,
        "provider_for": lambda sid: manager if sid == manifest.package_id else None,
    }
    catalog = ModelCatalog(services, store)
    bridge = AgentModelBridge(services, catalog)

    # Initially declared supported -> resolve succeeds.
    initial_row = catalog.resolve("local/ollama-small")
    assert initial_row["agent_supported"] is True

    # Calling bridge with tools triggers HTTP 400 'does not support tools' -> saves unsupported & raises agent_model_tools_unsupported.
    tool_spec = {
        "type": "function",
        "function": {"name": "ryn_file_list", "description": "List files", "parameters": {"type": "object", "properties": {}}},
    }
    with pytest.raises(AgentError) as caught:
        bridge.complete("local/ollama-small", [{"role": "user", "content": "list files"}], [tool_spec], lambda: False, None)
    assert str(caught.value) == "agent_model_tools_unsupported"
    assert caps_store.support(manifest) == "unsupported"

    # Next selection sees agent_supported=False and resolve rejects with agent_model_tools_unsupported.
    updated_row = catalog.get_model("local/ollama-small")
    assert updated_row["agent_supported"] is False
    assert updated_row["tool_support"] == "unsupported"
    with pytest.raises(ValueError, match="agent_model_tools_unsupported"):
        catalog.resolve("local/ollama-small")

    # Changing model fingerprint resets support back to 'unknown' so a swapped model can try again.
    manifest.model = "qwen3:8b"
    assert caps_store.support(manifest) == "unknown"


def test_t9_lru_eviction_moves_hit_key_to_end_and_preserves_order_on_disk(tmp_path):
    """T9: ToolCapabilities 超过 128 条时按最近使用淘汰（save 命中或 support 命中的键移到末尾，不因字母序写盘丢失顺序）。"""
    caps = ToolCapabilities(tmp_path)
    # Use reverse-alphabetical package IDs ('svc-999' down to 'svc-872') to prove disk JSON does not sort alphabetically.
    manifests = [_make_manifest(f"svc-{999 - i:03d}") for i in range(128)]
    for m in manifests:
        caps.save(m, "supported")

    # Touch the very first inserted entry ('svc-999') via save, and the second ('svc-998') via support().
    caps.save(manifests[0], "supported")
    assert caps.support(manifests[1]) == "supported"

    # Insert 2 new entries -> should evict manifests[2] ('svc-997') and manifests[3] ('svc-996'), keeping 0 and 1!
    caps.save(_make_manifest("svc-100"), "supported")
    caps.save(_make_manifest("svc-101"), "supported")

    reloaded = ToolCapabilities(tmp_path)
    assert reloaded.support(manifests[0]) == "supported"
    assert reloaded.support(manifests[1]) == "supported"
    assert reloaded.support(manifests[2]) == "unknown"
    assert reloaded.support(manifests[3]) == "unknown"
    assert reloaded.support(manifests[4]) == "supported"
