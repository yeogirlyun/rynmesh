"""Optional real Codex binary + local fake model. No cloud requests or billing."""
from __future__ import annotations

import json
import os
import shutil
import threading
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from rynmesh.llm_package import codex_session
from rynmesh.llm_package.cli_adapter import CLIAgentAdapter
from rynmesh.llm_package.process_guard import terminate


@pytest.mark.skipif(os.environ.get("RYNMESH_TEST_CODEX_BINARY") != "1", reason="explicit local Codex binary test")
@pytest.mark.parametrize("attack", [False, True])
def test_real_codex_exposes_no_environment_tools(monkeypatch, tmp_path, attack):
    executable = shutil.which("codex")
    if not executable:
        pytest.skip("Codex CLI not installed")
    captured = []
    marker = tmp_path / "must-not-exist.txt"
    persisted_content = []
    original_close = codex_session.CodexRPC.close

    def audited_close(rpc):
        try:
            terminate(rpc.process)
            rpc.reader.join(timeout=2)
            for file in rpc.root.rglob("*"):
                if file.is_file() and file.stat().st_size <= 8 * 1024**2:
                    if b"RYN_PRIVACY_SENTINEL_LOCAL_TEST" in file.read_bytes():
                        persisted_content.append(file.name)
        finally:
            original_close(rpc)

    monkeypatch.setattr(codex_session.CodexRPC, "close", audited_close)

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            captured.append(body)
            item = {"type": "function_call", "id": "fc_mock", "call_id": "mock-call",
                    "name": "ryn_client_0", "arguments": '{"command":"echo origin-only"}'}
            if attack and len(captured) == 1:
                item.update(name="exec_command", arguments=json.dumps({"cmd": f'echo unsafe > "{marker}"'}))
            response = {"id": "resp_mock", "object": "response", "status": "completed", "output": [item],
                        "usage": {"input_tokens": 5, "output_tokens": 5, "total_tokens": 10}}
            values = [
                {"type": "response.created", "response": {**response, "status": "in_progress", "output": []}},
                {"type": "response.output_item.added", "output_index": 0, "item": {**item, "arguments": ""}},
                {"type": "response.function_call_arguments.delta", "output_index": 0, "item_id": "fc_mock", "delta": item["arguments"]},
                {"type": "response.output_item.done", "output_index": 0, "item": item},
                {"type": "response.completed", "response": response},
            ]
            payload = "".join("event: " + v["type"] + "\ndata: " + json.dumps(v) + "\n\n" for v in values).encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    original = codex_session.private_workspace
    roots = []

    @contextmanager
    def workspace():
        with original() as (root, env):
            roots.append(root)
            # This test must never contact the real model or load real auth.
            (root / "codex/auth.json").unlink(missing_ok=True)
            env.pop("OPENAI_API_KEY", None)
            config = root / "codex/config.toml"
            config.write_text('model = "mock"\nmodel_provider = "local_test"\n' + config.read_text() +
                f'\n[model_providers.local_test]\nname = "local fake model"\nbase_url = "http://127.0.0.1:{server.server_port}/v1"\nwire_api = "responses"\nrequires_openai_auth = false\nsupports_websockets = false\n', encoding="utf-8")
            yield root, env

    monkeypatch.setattr(codex_session, "private_workspace", workspace)
    try:
        result = CLIAgentAdapter("codex_cli", executable=executable).chat({
            "messages": [{"role": "user", "content": "RYN_PRIVACY_SENTINEL_LOCAL_TEST: request the origin tool"}], "max_tokens": 64,
            "tools": [{"type": "function", "function": {"name": "run_command", "parameters": {
                "type": "object", "properties": {"command": {"type": "string"}}, "required": ["command"]}}}],
        }, task_id="live-local", timeout_s=30)
        assert result["message"]["tool_calls"][0]["function"]["name"] == "run_command"
        assert captured
        tools = captured[0].get("tools", [])
        names = [tool.get("name") for tool in tools]
        # Codex includes these inert orchestration tools even with no environment.
        # User-input requests are rejected; skills have no host environment.
        assert set(names) <= {"request_user_input", "skills", "ryn_client_0"}, names
        assert "ryn_client_0" in names
        assert not marker.exists()
        if attack:
            assert len(captured) == 2  # unsupported tool never reaches an executor
    finally:
        server.shutdown()
        server.server_close()
    assert all(not root.exists() for root in roots)
    assert not persisted_content, persisted_content
