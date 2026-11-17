"""S12-I19 source evidence and real HTTP bridge streaming/fallback."""
import copy
import io
import json
import urllib.error
import urllib.request
from contextlib import contextmanager

import pytest
from test_agent_source_drafts import setup, credential, draft, run, locator
from test_agent_integrations import node, add_source, fake_upstream
from rynmesh.agent_runtime.bridge import build_agent_integrations
from rynmesh.agent_runtime.store import AgentError
from rynmesh.llm_package.adapters import AdapterError


def test_t1_probe_records_each_outcome_timestamp_and_saves_only_explicit_test_evidence(setup, monkeypatch):
    tools, context, sources, config, _ = setup
    calls = []
    class Adapter:
        def __init__(self, model):
            self.model = model
        def cancel(self, task_id):
            return True
        def chat(self, body, *, task_id, timeout_s, on_event=None):
            calls.append((self.model, copy.deepcopy(body)))
            if body.get("stream"):
                assert body["max_tokens"] == 2
                if self.model == "reject":
                    raise AdapterError("stream unsupported", code="upstream_request_rejected",
                        diagnostics={"http_status": 400, "streaming_unsupported": True})
                if self.model == "offline":
                    raise AdapterError("network down", code="runtime_connection_failed")
                if self.model == "sse":
                    on_event({"choices": [{"delta": {"content": "OK"}}]})
            return {"text": "OK", "message": {"role": "assistant", "content": "OK"}}
    monkeypatch.setattr(sources, "adapter", lambda record, model="": Adapter(model))
    config = {**config, "models": ["sse", "json", "reject", "offline"]}
    credential(tools, context, config)
    prepared = draft(tools, context, config)
    preview = tools.preview("ryn_test_source_draft", locator(prepared), context=context)
    assert preview["probe_requests_maximum"] == 8
    tested = run(tools, context, "ryn_test_source_draft", locator(prepared), "test-stream")
    evidence = {item["model_id"]: item for item in tested["test"]["models"]}
    expected = {"sse": "supported", "json": "unsupported", "reject": "unsupported", "offline": "unknown"}
    assert {model: item["streaming_support"] for model, item in evidence.items()} == expected
    assert all(item["chat"] == "passed" and item["streaming_checked_at"] > 0 for item in evidence.values())
    saved = run(tools, context, "ryn_commit_source_draft", locator(prepared), "save-stream")
    assert sources.read()[saved["source_id"]]["streaming_support"] == expected
    assert len(calls) == 8
    verified = tools.verify({"tool": "ryn_commit_source_draft", "arguments": locator(prepared), "result": saved},
        [{"verifier": "saved_source_state"}], context=context, cancelled=lambda: False)
    assert verified["status"] == "passed" and len(calls) == 12
    assert len([body for _, body in calls if body.get("stream")]) == 4


def configured(node, support=None, models=None, protocol=None):
    app, store = node
    record, model_id = add_source(app, models=models)
    sources = app.state.llm_agent_services["source_store"]
    if support is not None:
        record["streaming_support"] = dict.fromkeys(record["models"], support)
        if protocol:
            record["protocol"] = protocol
        sources.save(record)
    bridge, _, catalog = build_agent_integrations(app, store, store.home)
    return bridge, catalog, sources, record, model_id


@pytest.mark.parametrize("support,expected", [(None, False), ("unknown", False), ("unsupported", False), ("supported", True)])
def test_t2_catalog_uses_stream_evidence_not_tool_support(node, support, expected):
    _, catalog, _, _, model_id = configured(node, support)
    row = catalog.resolve(model_id)
    assert row["streaming"] is expected and row["streaming_basis"]


def test_t3_verified_local_api_sends_stream_true_and_delivers_real_deltas(node, monkeypatch):
    bridge, _, _, _, model_id = configured(node, "supported")
    requests = fake_upstream(monkeypatch, [{"role": "assistant", "content": "Streaming reply"}])
    events = []
    response = bridge.complete(model_id, [{"role": "user", "content": "Reply briefly"}], [], lambda: False, events.append)
    assert requests[0]["stream"] is True
    assert any(e.get("type") == "text_delta" and e.get("text") == "Streaming reply" for e in events)
    assert response["message"]["content"] == "Streaming reply"


def test_fallback_before_output_is_once_nonstreaming_and_downgrades_only_this_model(node, monkeypatch):
    bridge, catalog, sources, record, model_id = configured(node, "supported", ["model-a", "model-b"])
    fake_upstream(monkeypatch, [{"role": "assistant", "content": "Fallback reply"},
        {"role": "assistant", "content": "Later reply"}], json_only=True)
    original = urllib.request.urlopen
    calls = []
    def urlopen(request, **kwargs):
        body = json.loads(request.data)
        calls.append(body)
        if len(calls) == 1:
            error = {"error": {"message": "stream is not supported", "param": "stream", "type": "invalid_request_error"}}
            raise urllib.error.HTTPError(request.full_url, 400, "Bad Request", {}, io.BytesIO(json.dumps(error).encode()))
        return original(request, **kwargs)
    monkeypatch.setattr(urllib.request, "urlopen", urlopen)
    events = []
    response = bridge.complete(model_id, [{"role": "user", "content": "Reply"}], [], lambda: False, events.append)
    assert response["message"]["content"] == "Fallback reply"
    assert len(calls) == 2 and calls[0]["stream"] is True and not calls[1].get("stream")
    assert sources.read()[record["id"]]["streaming_support"]["model-a"] == "unknown"
    assert sources.read()[record["id"]]["streaming_support"]["model-b"] == "supported"
    assert catalog.resolve(model_id)["streaming"] is False
    bridge.complete(model_id, [{"role": "user", "content": "Again"}], [], lambda: False, events.append)
    assert len(calls) == 3 and not calls[-1].get("stream")


def test_no_nonstreaming_resend_after_partial_text_output(node, monkeypatch):
    bridge, _, _, _, model_id = configured(node, "supported")
    calls = []
    class Response:
        headers = {"content-type": "text/event-stream"}
        def __init__(self):
            self.lines = [b'data: {"choices":[{"index":0,"delta":{"content":"partial"}}]}\n',
                b'data: {"error":{"type":"server_error","message":"stream failed"}}\n']
        def readline(self, limit):
            return self.lines.pop(0) if self.lines else b""
        def close(self):
            return None
    @contextmanager
    def urlopen(request, **kwargs):
        calls.append(json.loads(request.data))
        yield Response()
    monkeypatch.setattr(urllib.request, "urlopen", urlopen)
    events = []
    with pytest.raises(AgentError):
        bridge.complete(model_id, [{"role": "user", "content": "Reply"}], [], lambda: False, events.append)
    assert len(calls) == 1 and any(e.get("text") == "partial" for e in events)


def test_failed_chat_keeps_unknown_stream_evidence_without_an_extra_request(setup):
    tools, context, sources, config, requests = setup
    config = {**config, "models": ["broken"]}
    credential(tools, context, config)
    prepared = draft(tools, context, config)
    tested = run(tools, context, "ryn_test_source_draft", locator(prepared), "test-broken")
    evidence = tested["test"]["models"][0]
    assert evidence["streaming_support"] == "unknown" and evidence["streaming_checked_at"] is None
    assert len(requests) == 1


@pytest.mark.parametrize("error_param", ["stream", None])
def test_native_api_stream_rejection_also_falls_back_without_output(node, monkeypatch, error_param):
    bridge, _, sources, record, model_id = configured(node, "supported", protocol="openai_responses")
    calls = []
    class Response:
        headers = {"content-type": "application/json"}
        def __enter__(self):
            return self
        def __exit__(self, *args):
            self.close()
        def close(self):
            return None
        def read(self, limit):
            return json.dumps({"id": "response", "status": "completed", "output": [{"type": "message",
                "role": "assistant", "content": [{"type": "output_text", "text": "Native fallback"}]}]}).encode()
    def urlopen(request, **kwargs):
        calls.append(json.loads(request.data))
        if len(calls) == 1:
            error = {"error": {"message": "Streaming is unsupported", "param": error_param, "type": "invalid_request_error"}}
            raise urllib.error.HTTPError(request.full_url, 400, "Bad Request", {}, io.BytesIO(json.dumps(error).encode()))
        return Response()
    monkeypatch.setattr(urllib.request, "urlopen", urlopen)
    result = bridge.complete(model_id, [{"role": "user", "content": "Reply"}], [], lambda: False, lambda event: None)
    assert result["message"]["content"] == "Native fallback"
    assert len(calls) == 2 and calls[0]["stream"] is True and not calls[1].get("stream")
    assert sources.read()[record["id"]]["streaming_support"]["model-a"] == "unknown"
