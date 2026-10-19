"""Native wire failures pass through the real Agent bridge/runtime exactly once."""
from __future__ import annotations

import json
import threading
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
from test_agent_integrations import node  # noqa: F401

from rynmesh.agent_runtime.bridge import build_agent_integrations
from rynmesh.agent_runtime.runtime import AgentRuntime
from rynmesh.agent_runtime.store import AgentStore
from rynmesh.llm_package.adapters import AdapterError
from rynmesh.llm_package.protocol_adapter import NativeProtocolAdapter
from rynmesh.services import peer_box


@contextmanager
def upstream(protocol, mode, failure):
    requests = []
    arguments = {"model_id": "must_not_be_changed", "enabled": True}
    if protocol == "openai_responses":
        raw = {"id": "response_one", "status": "incomplete" if failure == "truncated" else "failed",
               "error": None if failure == "truncated" else {"message": "PRIVATE_PROVIDER_ERROR"},
               "incomplete_details": {"reason": "max_output_tokens"},
               "output": [{"type": "function_call", "id": "fc_one", "status": "completed", "call_id": "would_mutate",
                           "name": "ryn_set_model_sharing", "arguments": json.dumps(arguments)}],
               "usage": {"input_tokens": 17, "output_tokens": 9}}
        events = [{"type": "response.created", "response": {"id": "response_one", "status": "in_progress",
                   "usage": {"input_tokens": 17, "output_tokens": 1}}},
                  {"type": "response.output_item.added", "output_index": 0, "item": {**raw["output"][0], "status": "in_progress"}},
                  {"type": "response.incomplete" if failure == "truncated" else "response.failed", "response": raw}]
    else:
        raw = {"id": "message_one", "type": "message", "role": "assistant",
               "content": [{"type": "tool_use", "id": "would_mutate", "name": "ryn_set_model_sharing", "input": arguments}],
               "stop_reason": "max_tokens" if failure == "truncated" else "pause_turn",
               "usage": {"input_tokens": 17, "output_tokens": 9}}
        events = [{"type": "message_start", "message": {"id": "message_one", "type": "message", "role": "assistant",
                   "content": [], "usage": {"input_tokens": 17, "output_tokens": 1}}},
                  {"type": "content_block_start", "index": 0, "content_block": raw["content"][0]},
                  {"type": "content_block_stop", "index": 0},
                  {"type": "message_delta", "delta": {"stop_reason": raw["stop_reason"]}, "usage": {"output_tokens": 9}},
                  {"type": "message_stop"}]

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass

        def do_POST(self):
            requests.append({"path": self.path, "body": json.loads(self.rfile.read(int(self.headers["Content-Length"])))})
            if failure.startswith("http_"):
                payload = json.dumps({"error": {"message": "PRIVATE_PROVIDER_ERROR"}}).encode()
                self.send_response(int(failure.removeprefix("http_")))
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)
                return
            payload = ("".join("data: " + json.dumps(event) + "\n\n" for event in events).encode()
                       if mode == "sse" else json.dumps(raw).encode())
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream" if mode == "sse" else "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}/v1", requests
    finally:
        server.shutdown()
        server.server_close()
        worker.join(2)


@pytest.mark.parametrize("protocol", ["openai_responses", "anthropic_messages"])
@pytest.mark.parametrize("mode", ["json", "sse"])
@pytest.mark.parametrize("failure", ["truncated", "unknown_stop", "http_400", "http_422"])
def test_native_protocol_failure_is_terminal_without_second_model_request(node, monkeypatch, protocol, mode, failure):  # noqa: F811
    app, store = node
    with upstream(protocol, mode, failure) as (base_url, requests):
        sources = app.state.llm_agent_services["source_store"]
        record = sources.prepare({"name": "Native protocol fixture", "kind": "local", "protocol": protocol,
                                  "base_url": base_url, "models": ["native-fixture"]})
        record.update(status="ready", tool_support={"native-fixture": "supported"})
        sources.save(record)
        model_id = "local/" + sources.model_service_id(record, "native-fixture")
        bridge, tools, catalog = build_agent_integrations(app, store, store.home)
        if mode == "sse":
            resolve = catalog.resolve
            monkeypatch.setattr(catalog, "resolve", lambda model_id, **kwargs: {**resolve(model_id, **kwargs), "streaming": True})
        runtime = AgentRuntime(AgentStore(store.home / "native-protocol-retry-agent",
            peer_box.load_or_create_messaging_key(store.home / "messaging.x25519")), bridge, tools, catalog)
        runtime.update_settings({"enabled": True, "model_id": model_id})
        task = runtime.create({"message": "Inspect configuration without changing sharing."})
        source_before = sources.path.read_bytes()
        for _ in range(4):
            runtime.run_once()
        row = runtime.get(task["id"])
        stored = runtime.store.read()["tasks"][task["id"]]
        assert row["status"] == "failed", row
        assert len(requests) == 1
        assert requests[0]["path"] == ("/v1/responses" if protocol == "openai_responses" else "/v1/messages")
        assert row["error"] == ("agent_model_protocol_unsupported" if failure.startswith("http_") else "agent_model_protocol_invalid")
        assert not any(event["type"] == "model_retry_scheduled" for event in row["events"])
        assert not any(event["type"] == "tool_call" for event in row["events"])
        assert not stored.get("operation_receipts") and not stored["queue"]
        assert sources.path.read_bytes() == source_before
        budget_record = stored["budget"]["records"][0]
        if failure.startswith("http_"):
            assert budget_record["input_tokens"] is None and budget_record["output_tokens"] is None
            assert budget_record["charged_tokens"] == budget_record["input_reservation"] + budget_record["output_reservation"]
        else:
            assert budget_record["input_tokens"] == 17 and budget_record["output_tokens"] == 9
            assert budget_record["charged_tokens"] == 26
        assert "PRIVATE_PROVIDER_ERROR" not in json.dumps(row)


@pytest.mark.parametrize("protocol,upper", [("anthropic_messages", 1), ("openai_responses", 2)])
def test_invalid_temperature_defaults_fail_before_any_http_request(monkeypatch, protocol, upper):
    requests = []
    def unexpected_http(*args, **kwargs):
        requests.append(True)
        raise AssertionError("invalid native parameters must not leave the process")
    monkeypatch.setattr("urllib.request.urlopen", unexpected_http)
    for temperature in (upper + .1, False, float("nan")):
        adapter = NativeProtocolAdapter(protocol=protocol, base_url="http://127.0.0.1:1", model="native-fixture",
                                        request_defaults={"temperature": temperature})
        with pytest.raises(AdapterError) as caught:
            adapter.chat({"messages": [{"role": "user", "content": "Inspect"}], "max_tokens": 20},
                         task_id="invalid-temperature", timeout_s=3)
        assert caught.value.code == "source_protocol_parameters_unsupported"
    assert requests == []
