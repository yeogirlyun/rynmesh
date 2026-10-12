"""R05 controller mechanisms only; synthetic inference is never live evidence."""
import asyncio
import importlib.util
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from test_agent_peer_binding import paired  # noqa: F401
from test_agent_runtime import assistant, make_runtime

from rynmesh.agent_runtime.bridge import build_agent_integrations
from rynmesh.agent_runtime.runtime import AgentRuntime
from rynmesh.agent_runtime.store import AgentStore
from rynmesh.agent_runtime.tools import RynTools
from rynmesh.ai_access.routes import install_ai_access
from rynmesh.llm_package.routes import install_llm_routes
from rynmesh.nas import NasPlugin
from rynmesh.services import peer_box
from rynmesh.store import RynmeshStore


@pytest.fixture
def controller(monkeypatch):
    path = Path(__file__).resolve().parents[1] / "docs/acceptance/ryn-agent-delivery/real-model/run_r05.py"
    monkeypatch.syspath_prepend(str(path.parent))
    spec = importlib.util.spec_from_file_location("r05_controller", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_production_peer_stream_reaches_runtime_deltas_without_catalog_override(paired, monkeypatch, tmp_path):  # noqa: F811
    p = paired
    p.consumer.refresh(p.alice.peer_id)
    store = RynmeshStore(home=p.bob.store.root.parent, network_dir=tmp_path / "network")
    app = FastAPI()
    install_ai_access(app, home=store.home, local_control=lambda _: None, relationship=p.bob.store.relationship)
    app.state.ai_access.catalog = p.consumer
    install_llm_routes(app, store=store, home=store.home, messaging_key=p.bob.messaging_private,
                       resolve_endpoint=lambda _: "", resolve_pubkey=lambda _: "")
    observed = []
    def synthetic_stream(body, **options):
        observed.append(body)
        assert body["stream"] is True
        for _ in range(12):
            options["on_event"]({"choices": [{"delta": {"content": "x" * 512}}]})
        return {"text": "x" * 6144, "message": {"role": "assistant", "content": "x" * 6144},
                "finish_reason": "stop", "input_tokens": 10, "output_tokens": 20, "duration_ms": 1,
                "reported_usage": {"input_tokens": 10, "output_tokens": 20}}
    monkeypatch.setattr(p.providers["a"].adapter.inner, "chat", synthetic_stream)
    async def exchange(*, signed_request, **options):
        response = await asyncio.to_thread(p.providers["a"].handle, signed_request, options.get("on_event"))
        return response, {"transport": "signed_envelope_fixture", "relay_used": False}
    monkeypatch.setattr("rynmesh.llm_package.routes.consumer_exchange", exchange)
    try:
        bridge, tools, catalog = build_agent_integrations(app, store, store.home)
        selected = next(m for m in catalog.list_models() if m["source"] == "peer")
        assert selected["streaming"] and selected["destination_binding_supported"]
        runtime = AgentRuntime(AgentStore(store.home / "agent", p.bob.messaging_private), bridge, tools, catalog,
                               consumer_node_id=store.peer_id)
        app.state.agent_runtime = runtime
        runtime.update_settings({"enabled": True, "model_id": selected["id"], "max_context_chars": 100000})
        task = runtime.create({"message": "Synthetic stream plumbing only."})
        runtime.run_once()
        saved = runtime.get(task["id"])
        assert len([e for e in saved["events"] if e["type"] == "text_delta"]) == 12
        assert saved["control_revision"] == task["control_revision"]
        assert saved["event_cursor"] > task["event_cursor"] and len(observed) == 1
    finally:
        app.state.llm_shutdown()


def test_stream_controller_observes_real_runtime_control_sequence(controller, tmp_path):
    runtime, model, _, _ = make_runtime(tmp_path)
    finished, calls = threading.Event(), []
    def synthetic_stream(_model, _messages, _specs, cancelled, on_event):
        calls.append(True)
        for _ in range(15):
            if cancelled():
                break
            on_event({"type": "text_delta", "text": "fixture " * 80})
            time.sleep(.02)
        while not cancelled() and not finished.wait(.01):
            pass
        return assistant("Late synthetic answer")
    model.complete = synthetic_stream
    def request(method, path, body=None):
        if path.endswith("/models"):
            return {"models": [{"id": "local:test", "source": "peer", "streaming": True,
                "available": True, "agent_supported": True, "destination_binding_supported": True}]}
        if path.endswith("/settings"):
            return runtime.settings()
        if path.endswith("/tasks"):
            return runtime.create(body) if method == "POST" else {"tasks": runtime.list_tasks()}
        task_id = path.split("/tasks/")[1].split("/")[0]
        if "/events?after=" in path:
            return runtime.events(task_id, int(path.split("after=")[1]))
        if path.endswith("/actions"):
            return runtime.action(task_id, body)
        return runtime.get(task_id)
    def work():
        while not finished.is_set():
            runtime.run_once()
            finished.wait(.005)
    worker = threading.Thread(target=work, daemon=True)
    worker.start()
    try:
        result = controller.stream_controls(request, "local:test", timeout=5, poll=.005, settle_seconds=.04)
        assert "unmet_trigger" not in result
        assert result["text_delta_count_before_pause"] >= 10
        assert result["control"] == {"pause_accepted": True, "resume_accepted": True,
            "cancel_accepted": True, "cancelled_run_resumed_automatically": False}
        assert result["safe_checkpoint"]["run_state"] == "paused"
        assert result["task"]["status"] == "cancelled"
        assert result["metrics"]["dispatches_after_cancel"] == 0 and len(calls) == 2
    finally:
        finished.set()
        worker.join(3)


def test_stream_preflight_does_not_dispatch_for_local_nonstreaming_model(controller):
    calls = []
    def request(method, path, body=None):
        calls.append((method, path))
        if path.endswith("/models"):
            return {"models": [{"id": "local:model", "source": "local_api", "streaming": False}]}
        return {"tasks": []} if path.endswith("/tasks") else {"enabled": True}
    result = controller.stream_controls(request, "local:model")
    assert result["unmet_trigger"] == "authenticated_streaming_peer_model_required"
    assert all(method == "GET" for method, _ in calls)


def test_trace_retains_rejected_usage_without_reasoning_or_raw_exception(controller):
    from rynmesh.agent_runtime.store import AgentModelResponseError
    calls = []
    def reject(*args, **kwargs):
        raise AgentModelResponseError({"input_tokens": 7, "output_tokens": 3}, "length", "agent_model_output_limit")
    traced = controller.trace_complete(reject, calls)
    with pytest.raises(AgentModelResponseError):
        traced("fixture", [{"role": "assistant", "content": "visible", "reasoning_content": "PRIVATE_REASONING"}],
               [{"type": "function", "function": {"name": "read", "parameters": {}}}], lambda: False)
    assert calls[0]["usage"] == {"input_tokens": 7, "output_tokens": 3}
    assert calls[0]["finish_reason"] == "length" and calls[0]["tools"]
    assert "reasoning_content" not in calls[0]["request_messages"][0]


def test_actual_http_commit_cancel_release_retains_original_receipt_once(controller, tmp_path):
    home, root = tmp_path / "node", tmp_path / "dav"
    (root / "reports/acceptance").mkdir(parents=True)
    (root / ".ryn-acceptance-isolated").touch()
    with controller.CommitResponseDAV(root) as dav:
        nas = NasPlugin(home)
        nas.enable(True)
        source = nas.configure({"name": "docs-alpha", "system": "generic", "protocol": "webdav", "url": dav.url,
            "username": "fixture", "password": "fixture", "allow_ai": True, "writable": True})
        app, store = SimpleNamespace(state=SimpleNamespace(nas_plugin=nas)), SimpleNamespace(home=home)
        tools = RynTools(app, store, {}, None)
        class Planner:
            supports_destination_binding = True
            calls = 0
            def complete(self, *_args, **_options):
                self.calls += 1
                return assistant(name="ryn_create_text_file", args={"directory_ref": root_ref,
                    "name": "late-receipt.md", "content": controller.CONTENT})
        model = Planner()
        catalog = SimpleNamespace(list_models=lambda: [{"id": "fixture", "source": "local_api", "available": True,
            "agent_supported": True, "peer_id": "local", "source_id": "fixture", "destination_revision": "rev1"}])
        runtime = AgentRuntime(AgentStore(home / "agent", peer_box.load_or_create_messaging_key(home / "messaging.x25519")),
            model, tools, catalog, consumer_node_id="local")
        runtime.update_settings({"enabled": True, "model_id": "fixture", "max_context_chars": 100000})
        prompt = next(s for s in controller.load_case()["setup"]["subscenarios"] if s["id"] == "late_write_receipt")["prompt"]
        task = runtime.create({"message": prompt, "scope": {"resources": [{"connection_id": source["id"],
            "root_path": "reports/acceptance", "recursive": True, "actions": ["list", "read_ai", "create_file", "create_directory"]}],
            "allow_model_data": True}, "expected_destination_revision": "rev1"})
        root_ref = runtime.store.read()["tasks"][task["id"]]["scope"]["resource_grants"][0]["root_resource_ref"]
        try:
            result = controller.late_write_controls(runtime, task, dav, timeout=5)
            assert result["cancel_persisted_while_response_held"]
            assert result["response_barrier"]["commit_counter_ns"] < result["cancel_counter_ns"] < result["response_barrier"]["release_counter_ns"]
            assert result["checks"] == {"receipt_retained": True, "effective_writes": 1, "put_attempts": 1,
                "exact_file": True, "new_dispatches_after_cancel": 0, "cancelled": True}
            assert result["worker_finished"] and model.calls == 1
            assert result["response_barrier"]["original_response_count"] == 1
            assert not result["response_barrier"].get("hold_expired")
            assert result["artifact"]["sha256"] == result["response_barrier"]["sha256"]
            receipt = result["receipts"][0]
            assert tools.resource_writes.ledger.get(receipt["operation_id"])["status"] == "confirmed"
        finally:
            for source_id in list(nas.sessions):
                nas._close_session(source_id)
