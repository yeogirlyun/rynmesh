"""CLI service contract without invoking a user's paid CLI account."""

from __future__ import annotations

import json
import threading
import time
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from rynmesh.llm_package.adapters import AdapterError
from rynmesh.llm_package.api import install_inference_api
from rynmesh.llm_package.cli_adapter import CLIAgentAdapter
from rynmesh.llm_package.manifest import LLMPackageManifest, save_manifest
from rynmesh.llm_package.routes import install_llm_routes
from rynmesh.services import peer_box
from rynmesh.store import RynmeshStore


class FakeProcess:
    def __init__(self, args, *, output: str, **kwargs):
        self.args = args
        self.kwargs = kwargs
        self.output = output
        self.returncode = 0
        self.input = ""

    def communicate(self, prompt=None, timeout=None):
        self.input = prompt or ""
        return self.output, ""


def test_codex_desktop_discovery_without_shell_path(tmp_path, monkeypatch):
    binary = tmp_path / "OpenAI" / "Codex" / "bin" / "version" / "codex.exe"
    binary.parent.mkdir(parents=True)
    binary.write_bytes(b"test")
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    monkeypatch.setattr("rynmesh.llm_package.cli_adapter.shutil.which", lambda _: None)
    assert CLIAgentAdapter("codex_cli")._command_path() == str(binary)


@pytest.mark.parametrize("location", [
    "/opt/homebrew/bin/codex", "/usr/local/bin/codex",
    "/External Disk/My tools/Renamed AI.app/Contents/Resources/codex",
    "/test-home/.local/bin/codex", "/test-home/.npm-global/bin/codex",
])
def test_macos_cli_discovery_without_shell_path(monkeypatch, location):
    from pathlib import Path

    from rynmesh.llm_package import cli_adapter
    monkeypatch.setattr(cli_adapter.sys, "platform", "darwin")
    monkeypatch.delenv("LOCALAPPDATA", raising=False)
    monkeypatch.setattr(cli_adapter.shutil, "which", lambda _: None)
    monkeypatch.setattr(cli_adapter, "_mac_codex_apps", lambda _: (Path("/External Disk/My tools/Renamed AI.app"),))
    monkeypatch.setattr(Path, "home", lambda: Path("/test-home"))
    monkeypatch.setattr(Path, "is_file", lambda path: path == Path(location))
    monkeypatch.setattr(cli_adapter.os, "access", lambda path, mode: path == Path(location))
    assert CLIAgentAdapter("codex_cli")._command_path() == str(Path(location))


def test_macos_discovery_rejects_nonexecutable_and_preserves_explicit_path(monkeypatch):
    from pathlib import Path

    from rynmesh.llm_package import cli_adapter
    monkeypatch.setattr(cli_adapter.sys, "platform", "darwin")
    monkeypatch.delenv("LOCALAPPDATA", raising=False)
    monkeypatch.setattr(cli_adapter.shutil, "which", lambda _: None)
    monkeypatch.setattr(cli_adapter, "_mac_codex_apps", lambda _: ())
    monkeypatch.setattr(Path, "is_file", lambda _: True)
    monkeypatch.setattr(cli_adapter.os, "access", lambda path, mode: False)
    with pytest.raises(AdapterError, match="not installed"):
        CLIAgentAdapter("codex_cli")._command_path()
    monkeypatch.setattr(cli_adapter.os, "access", lambda path, mode: True)
    with pytest.raises(AdapterError, match="not installed"):
        CLIAgentAdapter("codex_cli", executable="/custom/codex")._command_path()
    monkeypatch.setattr(cli_adapter.shutil, "which", lambda _: "/explicit/codex")
    assert CLIAgentAdapter("codex_cli")._command_path() == "/explicit/codex"


def test_macos_application_registry_lookup_and_failure(monkeypatch):
    from pathlib import Path

    from rynmesh.llm_package import cli_adapter
    calls = []
    def run(args, **kwargs):
        calls.append((args, kwargs))
        return SimpleNamespace(stdout=json.dumps(["/Volumes/Apps/AI renamed.app", "relative", None]))
    monkeypatch.setattr(cli_adapter.subprocess, "run", run)
    cli_adapter._mac_codex_apps.cache_clear()
    assert cli_adapter._mac_codex_apps(1) == (Path("/Volumes/Apps/AI renamed.app"),)
    assert cli_adapter._mac_codex_apps(1) == cli_adapter._mac_codex_apps(1)
    assert len(calls) == 1
    assert "URLForApplicationWithBundleIdentifier" in calls[0][0][-1]
    assert calls[0][1]["timeout"] == 3
    monkeypatch.setattr(cli_adapter.subprocess, "run", lambda *a, **k: (_ for _ in ()).throw(OSError("unavailable")))
    assert cli_adapter._mac_codex_apps(2) == ()
    cli_adapter._mac_codex_apps.cache_clear()


@pytest.mark.parametrize("kind", ["claude_cli"])
def test_cli_adapter_invokes_text_only_in_isolated_working_directory(monkeypatch, kind):
    output = (json.dumps({"type": "item.completed", "item": {"type": "agent_message", "text": "ready"}}) + "\n" +
              json.dumps({"type": "turn.completed", "usage": {"input_tokens": 5, "output_tokens": 1}})) if kind == "codex_cli" else json.dumps({"result": "ready", "usage": {"input_tokens": 5, "output_tokens": 1}})
    created = []

    def spawn(args, **kwargs):
        process = FakeProcess(args, output=output, **kwargs)
        created.append(process)
        return process

    monkeypatch.setattr("rynmesh.llm_package.cli_adapter.shutil.which", lambda _: "C:/bin/agent.exe")
    monkeypatch.setattr("rynmesh.llm_package.cli_adapter.spawn", spawn)
    def communicate(process, data, **_):
        process.input = data.decode()
        return process.output.encode()
    monkeypatch.setattr("rynmesh.llm_package.cli_adapter.communicate_bounded", communicate)
    adapter = CLIAgentAdapter(kind)
    result = adapter.chat({"messages": [{"role": "user", "content": "Hello"}], "max_tokens": 16}, task_id="task", timeout_s=1)
    process = created[0]
    assert result["text"] == "ready"
    assert result["input_tokens"] == 5 and result["output_tokens"] == 1
    assert process.kwargs["cwd"].startswith(str(__import__("tempfile").gettempdir()))
    assert "Hello" in process.input
    assert "Hello" not in process.args
    if kind == "codex_cli":
        assert "read-only" in process.args and "--ephemeral" in process.args
        assert "--ignore-rules" in process.args
    else:
        assert process.args[process.args.index("--tools") + 1] == ""
        assert "--no-session-persistence" in process.args


def test_cli_adapter_rejects_client_tools_before_process_start(monkeypatch):
    monkeypatch.setattr("rynmesh.llm_package.cli_adapter.subprocess.Popen", lambda *_, **__: pytest.fail("must not start CLI"))
    adapter = CLIAgentAdapter("claude_cli")
    with pytest.raises(AdapterError, match="tool calls"):
        adapter.chat({"messages": [{"role": "user", "content": "hi"}], "tools": [{"type": "function", "function": {"name": "tool"}}]}, task_id="one", timeout_s=1)
    with pytest.raises(ValueError, match="text content"):
        adapter.chat({"messages": [{"role": "user", "content": [{"type": "image_url"}]}]}, task_id="two", timeout_s=1)


def test_cli_cancellation_before_start_does_not_spawn_process(monkeypatch):
    monkeypatch.setattr("rynmesh.llm_package.cli_adapter.shutil.which", lambda _: "C:/bin/agent.exe")
    monkeypatch.setattr("rynmesh.llm_package.cli_adapter.subprocess.Popen", lambda *_, **__: pytest.fail("must not start CLI"))
    adapter = CLIAgentAdapter("codex_cli")
    adapter.cancel("cancelled")
    with pytest.raises(AdapterError, match="task_cancelled"):
        adapter.infer(prompt="hi", max_tokens=8, task_id="cancelled", timeout_s=1)
    assert adapter._cancelled == set()


def test_cli_is_a_distinct_model_in_local_api_and_rejects_unsupported_features(tmp_path):
    class Adapter:
        calls = 0

        def health(self):
            return {"ok": True}

        def chat(self, body, **_):
            self.calls += 1
            return {"text": "ready", "message": {"role": "assistant", "content": "ready"},
                    "finish_reason": "stop", "input_tokens": 5, "output_tokens": 1, "duration_ms": 1}

    adapter = Adapter()
    manager = SimpleNamespace(
        adapter=adapter,
        manifest=LLMPackageManifest(package_id="codex-cli", mode="codex_cli", adapter="codex_cli",
                                    public_model_alias="Codex CLI", runtime="external",
                                    capabilities=["text"], context_window=16000, max_output_tokens=512),
        _slots=threading.BoundedSemaphore(1), _lock=threading.Lock(), _running=0,
    )
    app = FastAPI()
    install_inference_api(app, home=tmp_path, store=SimpleNamespace(peer_id="self"),
                          active_manager=lambda: None, local_manager_for=lambda service_id: manager if service_id == "codex-cli" else None,
                          discover=lambda _: [], execute_order=None, cancel_order=None)
    client = TestClient(app)
    key = client.post("/api/local/llm/api-keys", json={"name": "laptop app", "output_token_limit": 100}).json()["key"]
    client.headers["Authorization"] = "Bearer " + key
    assert [item["id"] for item in client.get("/v1/models").json()["data"]] == ["local/codex-cli"]
    payload = {"model": "local/codex-cli", "messages": [{"role": "user", "content": "hi"}], "max_tokens": 12}
    assert client.post("/v1/chat/completions", json=payload).json()["choices"][0]["message"]["content"] == "ready"
    assert client.post("/v1/chat/completions", json={**payload, "tools": [{"type": "function", "function": {"name": "run", "parameters": {}}}]}).status_code == 200
    assert adapter.calls == 2


def test_cli_sharing_requires_personal_space_ai_policy_and_publishes_plural_catalog(tmp_path, monkeypatch):
    import rynmesh.llm_package.routes as routes

    home = tmp_path / "node"
    store = RynmeshStore(home=home, network_dir=tmp_path / "network")
    messaging_key = peer_box.load_or_create_messaging_key(home / "messaging.x25519")
    app = FastAPI()
    install_llm_routes(app, store=store, home=home, messaging_key=messaging_key,
                       resolve_endpoint=lambda _: "", resolve_pubkey=lambda _: "")
    manifest = LLMPackageManifest(package_id="codex-cli", mode="codex_cli", adapter="codex_cli",
                                  runtime="external", public_model_alias="Codex CLI", capabilities=["text"])
    path = home / "llm" / "codex-cli" / "manifest.json"
    save_manifest(manifest, path)
    monkeypatch.setattr(routes, "connect_cli", lambda **_: {"manifest": str(path)})
    monkeypatch.setattr("rynmesh.llm_package.cli_adapter.shutil.which", lambda _: "C:/bin/codex.exe")
    monkeypatch.setattr(CLIAgentAdapter, "infer", lambda *_, **__: {
        "text": "local CLI connected", "input_tokens": 4, "output_tokens": 3, "duration_ms": 1})
    with TestClient(app) as client:
        assert client.post("/api/local/llm/cli-services/codex_cli/setup").status_code == 200
        # Desktop use works before enabling personal-space sharing and never
        # attempts a P2P connection back to itself.
        local = client.get("/api/local/llm/services").json()["services"][0]
        assert local["local_only"] is True and local["online"] is True
        assert local["accepting_orders"] is False
        order = client.post("/api/local/llm/orders/async", json={
            "provider_peer_id": store.peer_id, "service_id": "codex-cli",
            "prompt": "private desktop test", "max_tokens": 32, "transport": "p2p"}).json()
        result = {}
        for _ in range(100):
            result = client.get("/api/local/llm/orders/" + order["task_id"]).json()
            if result["state"] in {"succeeded", "failed"}:
                break
            time.sleep(0.02)
        assert result["state"] == "succeeded", result
        assert result["output"] == "local CLI connected"
        assert result["transport"] == "local_process"
        persistent = client.post("/api/local/llm/orders", json={
            "provider_peer_id": store.peer_id, "service_id": "codex-cli", "prompt": "hello",
            "max_tokens": 32, "conversation_id": "ryn-chat-1", "cli_model": "model-from-cli"})
        assert persistent.status_code == 400
        assert not (home / "llm" / "codex-conversations").exists()
        persisted = (home / "llm" / "consumer-orders" / (order["task_id"] + ".json")).read_text()
        assert "private desktop test" not in persisted and "local CLI connected" not in persisted
        assert client.get("/api/local/llm/cli-services").json()["personal_space_ready"] is False
        assert client.put("/api/local/llm/cli-services/codex_cli/sharing", json={"enabled": True}).status_code == 409
        store.personal_space.create("Home")
        assert client.get("/api/local/llm/cli-services").json()["personal_space_ready"] is False
        store.personal_space.set_policy("space")
        assert client.get("/api/local/llm/cli-services").json()["personal_space_ready"] is True
        enabled = client.put("/api/local/llm/cli-services/codex_cli/sharing", json={"enabled": True})
        assert enabled.status_code == 200, enabled.text
        published = client.get("/api/local/llm/services").json()["services"]
        assert len(published) == 1
        assert published[0]["service"]["package_id"] == "codex-cli"
        assert published[0]["service"]["adapter"] == "codex_cli"
        assert client.get("/api/local/llm/api-access").json()["models"][0]["id"] == "local/codex-cli"


def test_remote_cli_is_exposed_through_laptop_api_with_strict_p2p(tmp_path):
    calls = []
    discovered = [{"peer_id": "home-peer", "node_name": "Home PC", "online": True,
                   "chat_protocol": "rynmesh.chat.v1", "service": {
                       "package_id": "claude-cli", "model_alias": "Claude Code", "adapter": "claude_cli",
                       "capabilities": ["text-generation", "api-text-only"],
                       "context_window": 32768, "max_output_tokens": 4096}}]

    async def execute(order, _emit):
        calls.append(order)
        return {"state": "succeeded", "text": "来自家里节点", "output": "来自家里节点",
                "message": {"role": "assistant", "content": "来自家里节点"},
                "finish_reason": "stop", "input_tokens": 12, "output_tokens": 8, "duration_ms": 10}

    app = FastAPI()
    install_inference_api(app, home=tmp_path, store=SimpleNamespace(peer_id="laptop-peer"),
                          active_manager=lambda: None, discover=lambda _: discovered,
                          execute_order=execute, cancel_order=lambda _: None)
    client = TestClient(app)
    key = client.post("/api/local/llm/api-keys", json={"name": "IDE"}).json()["key"]
    target = client.get("/api/local/llm/api-access").json()["targets"][0]
    assert target["rynmesh"]["adapter"] == "claude_cli"
    assert client.put("/api/local/llm/model-aliases/home-claude", json={"target": target["id"]}).status_code == 200
    client.headers["Authorization"] = "Bearer " + key
    response = client.post("/v1/messages", json={"model": "home-claude", "max_tokens": 64,
                                                  "messages": [{"role": "user", "content": "你好"}]})
    assert response.status_code == 200, response.text
    assert response.json()["content"][0]["text"] == "来自家里节点"
    assert calls[0]["provider_peer_id"] == "home-peer"
    assert calls[0]["service_id"] == "claude-cli"
    assert calls[0]["transport"] == "p2p"
