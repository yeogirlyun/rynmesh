"""Origin-only tools and fail-closed provider isolation, without paid inference."""
from __future__ import annotations

import copy
import json
import os
import re
import shutil
import sys
from collections import deque
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from rynmesh.llm_package import codex_session
from rynmesh.llm_package.adapters import AdapterError
from rynmesh.llm_package.api import install_inference_api
from rynmesh.llm_package.cli_adapter import CLIAgentAdapter
from rynmesh.llm_package.manifest import LLMPackageManifest
from rynmesh.llm_package.process_guard import communicate_bounded, spawn, terminate
from rynmesh.llm_package.routes import ProviderService, _expires
from rynmesh.llm_package.safety import InferenceBodyLimit, SharedSlots
from rynmesh.llm_package.task_balance import TaskBalanceLedger
from rynmesh.llm_package.task_protocol import TaskOrderStore, open_task, seal_task
from rynmesh.services import peer_box
from rynmesh.store import RynmeshStore

TOOLS = [{"type": "function", "function": {"name": "run_command", "description": "Run at origin",
          "parameters": {"type": "object", "properties": {"command": {"type": "string"}},
                         "required": ["command"], "additionalProperties": False}, "strict": True}}]


@pytest.fixture
def fake_rpc(monkeypatch, tmp_path):
    instances = []

    class RPC:
        attack = None

        def __init__(self, *args):
            self.root = tmp_path
            self.pending = deque()
            self.process = SimpleNamespace()
            self.calls, self.rejections, self.closed = [], [], False
            instances.append(self)

        def initialize(self):
            pass

        def call(self, method, params):
            self.calls.append((method, copy.deepcopy(params)))
            if method == "thread/start":
                return {"thread": {"id": "ephemeral"}, "model": "mock"}
            assert method == "turn/start"
            history = json.loads(params["input"][0]["text"])["messages"]
            if self.attack:
                self.pending.append(self.attack)
            elif history[-1]["role"] != "tool":
                self.pending.append({"id": 88, "method": "item/tool/call", "params": {
                    "threadId": "ephemeral", "turnId": "turn", "tool": "ryn_client_0",
                    "arguments": {"command": "echo origin-only"}}})
            else:
                self.pending.extend([
                    {"method": "item/completed", "params": {"threadId": "ephemeral", "turnId": "turn",
                      "item": {"type": "agentMessage", "text": "origin result received"}}},
                    {"method": "turn/completed", "params": {"threadId": "ephemeral",
                      "turn": {"id": "turn", "status": "completed"}}}])
            return {"turn": {"id": "turn"}}

        def receive(self):
            raise AdapterError("Codex app-server disconnected")

        def reject(self, value):
            self.rejections.append(value)

        def close(self):
            self.closed = True

    monkeypatch.setattr(codex_session, "CodexRPC", RPC)
    monkeypatch.setattr(CLIAgentAdapter, "_command_path", lambda _: "mock-codex")
    return RPC, instances


def test_codex_tools_return_to_origin_and_next_turn_uses_client_history(fake_rpc):
    _, instances = fake_rpc
    adapter = CLIAgentAdapter("codex_cli")
    body = {"messages": [{"role": "user", "content": "run a test"}], "tools": TOOLS, "max_tokens": 64}
    result = adapter.chat(body, task_id="one", timeout_s=2)
    call = result["message"]["tool_calls"][0]
    assert result["finish_reason"] == "tool_calls"
    assert call["function"]["name"] == "run_command"
    assert json.loads(call["function"]["arguments"]) == {"command": "echo origin-only"}
    body["messages"] += [result["message"], {"role": "tool", "tool_call_id": call["id"], "content": "ran at zcode"}]
    final = adapter.chat(body, task_id="two", timeout_s=2)
    assert final["text"] == "origin result received"
    assert adapter._running == {}
    for instance in instances:
        assert instance.closed
        assert [method for method, _ in instance.calls] == ["thread/start", "turn/start"]
        assert instance.calls[0][1]["ephemeral"] is True
        assert instance.calls[0][1]["environments"] == instance.calls[1][1]["environments"] == []
        assert not instance.rejections


@pytest.mark.parametrize("attack", [
    {"id": 7, "method": "item/commandExecution/requestApproval", "params": {}},
    {"id": 7, "method": "item/tool/call", "params": {"threadId": "other", "turnId": "turn", "tool": "ryn_client_0", "arguments": {}}},
    {"id": 7, "method": "item/tool/call", "params": {"threadId": "ephemeral", "turnId": "turn", "tool": "exec_command", "arguments": {}}},
    {"method": "item/started", "params": {"threadId": "ephemeral", "turnId": "turn", "item": {"type": "commandExecution"}}},
])
def test_no_execution_fallback_on_unexpected_provider_tool(fake_rpc, attack):
    rpc, instances = fake_rpc
    rpc.attack = attack
    with pytest.raises(AdapterError):
        CLIAgentAdapter("codex_cli").chat({"messages": [{"role": "user", "content": "test"}], "tools": TOOLS}, task_id="denied", timeout_s=1)
    assert instances[0].closed


@pytest.mark.parametrize("protocol", ["chat", "responses", "messages"])
@pytest.mark.parametrize("stream", [False, True])
def test_zcode_api_a_encrypted_b_codex_tool_response(fake_rpc, tmp_path, protocol, stream):
    # Actual A API -> signed encrypted request -> B provider -> CLI adapter.
    a = RynmeshStore(home=tmp_path / "a", network_dir=tmp_path / "net")
    b = RynmeshStore(home=tmp_path / "b", network_dir=tmp_path / "net")
    ak = peer_box.load_or_create_messaging_key(tmp_path / "ak")
    bk = peer_box.load_or_create_messaging_key(tmp_path / "bk")
    b.personal_space = SimpleNamespace(enforces_ai=lambda: True, allows_ai=lambda _: True)
    manifest = LLMPackageManifest(package_id="codex-cli", mode="codex_cli", adapter="codex_cli",
        public_model_alias="Codex CLI", context_window=32768, max_output_tokens=4096)
    records = TaskOrderStore(tmp_path / "provider-records")
    provider = ProviderService(manifest=manifest, adapter=CLIAgentAdapter("codex_cli"),
        store=b, task_store=records, balance=TaskBalanceLedger(tmp_path / "balance.json"), messaging_key=bk)
    requests = []

    async def execute(order, emit):
        requests.append(copy.deepcopy(order))
        task_id = order["task_id"]
        request = seal_task(body={"task_id": task_id, "service_id": "codex-cli", "chat": order["chat"], "max_tokens": order["max_tokens"],
            "max_amount": 1, "reply_messaging_pub": peer_box.public_key_b64(ak)}, task_id=task_id, kind="llm_request",
            sender_peer_id=a.peer_id, recipient_peer_id=b.peer_id, sender_signing_key=a.private_key_bytes,
            recipient_messaging_pub=peer_box.public_key_b64(bk), expires_at=_expires(300)).to_dict()

        def on_event(envelope):
            _, value = open_task(envelope, recipient_peer_id=a.peer_id, recipient_messaging_key=ak, expected_kind="llm_stream")
            emit(value["chunk"])

        import asyncio
        envelope = await asyncio.to_thread(provider.handle, request, on_event if emit else None)
        _, result = open_task(envelope, recipient_peer_id=a.peer_id, recipient_messaging_key=ak, expected_kind="llm_response")
        return result

    app = FastAPI()
    discovered = [{"peer_id": b.peer_id, "online": True, "chat_protocol": "rynmesh.chat.v1", "service": manifest.public_dict()}]
    install_inference_api(app, home=tmp_path / "a", store=a, active_manager=lambda: None,
        discover=lambda _: discovered, execute_order=execute, cancel_order=lambda _: None)
    with TestClient(app) as client:
        key = client.post("/api/local/llm/api-keys", json={"name": "zcode"}).json()["key"]
        model = client.get("/api/local/llm/api-access").json()["targets"][0]["id"]
        body = {"model": model, "stream": stream}
        if protocol == "responses":
            body.update(input="run origin tool", tools=[{"type": "function", **TOOLS[0]["function"]}], max_output_tokens=64)
            path = "/v1/responses"
        elif protocol == "messages":
            body.update(messages=[{"role": "user", "content": "run origin tool"}], max_tokens=64,
                        tools=[{"name": "run_command", "input_schema": TOOLS[0]["function"]["parameters"]}])
            path = "/v1/messages"
        else:
            body.update(messages=[{"role": "user", "content": "run origin tool"}], tools=TOOLS, max_tokens=64)
            path = "/v1/chat/completions"
        response = client.post(path, json=body, headers={"Authorization": "Bearer " + key})
        assert response.status_code == 200, response.text
        assert "run_command" in response.text and "origin-only" in response.text
        call_id = re.search(r"call_[0-9a-f]{32}", response.text).group()
        arguments = '{"command":"echo origin-only"}'
        if protocol == "responses":
            body["input"] = [{"role": "user", "content": "run origin tool"},
                {"type": "function_call", "call_id": call_id, "name": "run_command", "arguments": arguments},
                {"type": "function_call_output", "call_id": call_id, "output": "ran on zcode"}]
        elif protocol == "messages":
            body["messages"] += [{"role": "assistant", "content": [{"type": "tool_use", "id": call_id,
                "name": "run_command", "input": json.loads(arguments)}]},
                {"role": "user", "content": [{"type": "tool_result", "tool_use_id": call_id, "content": "ran on zcode"}]}]
        else:
            body["messages"] += [{"role": "assistant", "content": None, "tool_calls": [
                {"id": call_id, "type": "function", "function": {"name": "run_command", "arguments": arguments}}]},
                {"role": "tool", "tool_call_id": call_id, "content": "ran on zcode"}]
        followup = client.post(path, json=body, headers={"Authorization": "Bearer " + key})
        assert followup.status_code == 200, followup.text
        assert "origin result received" in followup.text
        assert requests[0]["_no_retention"] is True
        assert requests[0]["transport"] == "p2p"
        for file in (tmp_path / "provider-records").glob("*.json"):
            value = json.loads(file.read_text())
            assert "encrypted_response" not in value
            assert "origin tool" not in file.read_text()


def test_provider_retry_cache_is_memory_only_and_expires(tmp_path, monkeypatch):
    store = TaskOrderStore(tmp_path)
    store.use_memory_responses()
    store.claim(task_id="task", bindings={"peer": "p"})
    store.transition(task_id="task", state="failed", encrypted_response={"ciphertext": "SECRET"})
    assert store.get("task")["encrypted_response"]["ciphertext"] == "SECRET"
    assert "SECRET" not in (tmp_path / "task.json").read_text()
    import rynmesh.llm_package.task_protocol as protocol
    now = protocol.time.monotonic()
    monkeypatch.setattr(protocol.time, "monotonic", lambda: now + 61)
    assert "encrypted_response" not in store.get("task")
    assert "encrypted_response" not in TaskOrderStore(tmp_path).get("task")


def test_private_cli_home_does_not_inherit_config_and_is_removed(tmp_path, monkeypatch):
    owner = tmp_path / "owner"
    owner.mkdir()
    (owner / "config.toml").write_text('notify = ["bad-command"]')
    (owner / "auth.json").write_text('{"test": "credential"}')
    monkeypatch.setenv("CODEX_HOME", str(owner))
    monkeypatch.setenv("PRIVATE_DATABASE_PASSWORD", "never-inherit")
    with codex_session.private_workspace() as (root, env):
        config = (root / "codex/config.toml").read_text()
        assert "bad-command" not in config and "shell_tool = false" in config
        assert "PRIVATE_DATABASE_PASSWORD" not in env
        assert json.loads((root / "codex/auth.json").read_text())["test"] == "credential"
    assert not root.exists()
    assert (owner / "auth.json").exists()


def test_global_capacity_and_disk_reserve(tmp_path, monkeypatch):
    guards = [SharedSlots(3, tmp_path) for _ in range(5)]
    try:
        assert all(guard.acquire() for guard in guards[:4])
        assert not guards[4].acquire()
    finally:
        for guard in guards[:4]:
            guard.release()
    monkeypatch.setattr(shutil, "disk_usage", lambda _: SimpleNamespace(free=1))
    assert not guards[0].acquire()


def test_http_chunked_body_limit_and_no_store():
    app = FastAPI()
    app.add_middleware(InferenceBodyLimit)
    with TestClient(app) as client:
        response = client.post("/v1/chat/completions", content=iter([b"a" * 600000, b"b" * 600000]))
        assert response.status_code == 413
        assert response.headers["cache-control"] == "no-store"


def test_os_process_guard_terminates_process(tmp_path):
    process = spawn([sys.executable, "-c", "import time; print('started', flush=True); time.sleep(60)"],
                    cwd=str(tmp_path), env=dict(os.environ), timeout=2)
    assert process.stdout.readline().strip() == b"started"
    terminate(process)
    assert process.poll() is not None
    process.stdout.close()
    process.stdin.close()


@pytest.mark.parametrize("program,reason", [
    ("import time; time.sleep(60)", "timed out"),
    ("import sys; sys.stdout.buffer.write(b'x' * (9 * 1024**2)); sys.stdout.flush()", "output limit"),
])
def test_process_timeout_and_output_limits_kill_worker(tmp_path, program, reason):
    process = spawn([sys.executable, "-c", program], cwd=str(tmp_path), env=dict(os.environ), timeout=2)
    with pytest.raises(RuntimeError, match=reason):
        communicate_bounded(process, b"", timeout=1, root=tmp_path)
    assert process.poll() is not None


def test_stale_workspace_cleanup_only_removes_marked_owned_directory(tmp_path):
    import time
    stale = tmp_path / "session-old"
    stale.mkdir()
    marker = stale / ".ryn-private-session"
    marker.touch()
    os.utime(marker, (time.time() - 4000,) * 2)
    unrelated = tmp_path / "session-user"
    unrelated.mkdir()
    recent = tmp_path / "session-current"
    recent.mkdir()
    (recent / ".ryn-private-session").touch()
    codex_session.cleanup_stale_workspaces(tmp_path)
    assert not stale.exists()
    assert unrelated.exists() and recent.exists()


def test_admission_stops_when_memory_is_low(tmp_path, monkeypatch):
    from rynmesh.llm_package import safety
    monkeypatch.setattr(safety, "_pressure_checked", 0)
    monkeypatch.setattr(safety, "_pressure_ok", True)
    monkeypatch.setattr(safety, "_available_memory", lambda: 1024)
    monkeypatch.setattr(safety.shutil, "which", lambda _: None)
    assert not SharedSlots(1, tmp_path).acquire()


@pytest.mark.parametrize("schema", [{"$ref": "https://attacker.invalid/schema"}, {"type": "nonexistent"}])
def test_invalid_or_external_tool_schemas_never_start_cli(fake_rpc, schema):
    with pytest.raises(ValueError):
        CLIAgentAdapter("codex_cli").chat({"messages": [{"role": "user", "content": "hi"}],
            "tools": [{"type": "function", "function": {"name": "run", "parameters": schema}}]}, task_id="schema", timeout_s=1)
    assert not fake_rpc[1]


def test_wrong_tool_arguments_are_not_forwarded(fake_rpc):
    rpc, instances = fake_rpc
    rpc.attack = {"id": 7, "method": "item/tool/call", "params": {
        "threadId": "ephemeral", "turnId": "turn", "tool": "ryn_client_0", "arguments": {"command": 123}}}
    with pytest.raises(ValueError, match="schema"):
        CLIAgentAdapter("codex_cli").chat({"messages": [{"role": "user", "content": "hi"}], "tools": TOOLS}, task_id="bad-args", timeout_s=1)
    assert instances[0].closed
