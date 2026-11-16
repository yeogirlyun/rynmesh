"""Acceptance tests for S01-I19: slot queueing, immediate cancellation, Condition dispatch, and API concurrency=4."""
from __future__ import annotations

import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager

import pytest
from test_agent_integrations import add_source, node  # noqa: F401
from test_agent_runtime import Tools

from rynmesh.agent_runtime import bridge as bridge_module
from rynmesh.agent_runtime.bridge import build_agent_integrations
from rynmesh.agent_runtime.runtime import AgentRuntime
from rynmesh.agent_runtime.store import AgentError, AgentStore
from rynmesh.llm_package import model_settings
from rynmesh.llm_package.adapters import AdapterError
from rynmesh.llm_package.model_dispatch import ModelDispatchAdapter
from rynmesh.llm_package.sources import SourceStore
from rynmesh.services import peer_box


def _configure_max_concurrent(app, record, model_name: str, max_concurrent: int):
    sources = app.state.llm_agent_services["source_store"]
    updated = sources.configure_model(
        record["id"],
        {
            "model_id": model_name,
            "expected_rule_revision": record["model_settings"][model_name]["rule_revision"],
            "configuration": {"max_concurrent": max_concurrent},
        },
    )
    # Refresh provider service manifest so manager._slots matches new max_concurrent
    app.state.llm_agent_services["provider_for"](sources.model_service_id(record, model_name))
    return updated


def test_t1_concurrency_1_second_request_queues_past_2500ms_and_succeeds_without_retry(node, tmp_path, monkeypatch):  # noqa: F811
    """T1 (Changes 2, 4): Two concurrent requests on a max_concurrent=1 model queue >2.5s, emit model_queue once, and succeed without retry."""
    app, store = node
    record, model_id = add_source(app, models=["serial-model"])
    _configure_max_concurrent(app, record, "serial-model", 1)

    first_entered = threading.Event()
    call_order = []
    call_lock = threading.Lock()

    class _Resp:
        def __init__(self, text: str):
            self.headers = {"content-type": "application/json"}
            self._payload = json.dumps({
                "choices": [{"message": {"role": "assistant", "content": text}, "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 10, "completion_tokens": 5},
            }).encode()

        def read(self, _limit):
            return self._payload

        def close(self):
            self.closed = True

    @contextmanager
    def slow_first_urlopen(request, **kwargs):
        body = json.loads(request.data)
        user_msg = body["messages"][-1]["content"]
        with call_lock:
            idx = len(call_order)
            call_order.append(user_msg)
        if idx == 0:
            first_entered.set()
            # Hold the single slot for 3.2s (> old 2.5s bridge timeout and > 2.0s model_queue threshold)
            time.sleep(3.2)
            yield _Resp("first done")
        else:
            yield _Resp("second done")

    monkeypatch.setattr("urllib.request.urlopen", slow_first_urlopen)

    bridge, _, catalog = build_agent_integrations(app, store, store.home)
    agent_store = AgentStore(tmp_path / "agent", peer_box.load_or_create_messaging_key(store.home / "messaging.x25519"))
    runtime = AgentRuntime(agent_store, bridge, Tools(), catalog)
    runtime.update_settings({"enabled": True, "model_id": model_id, "verify_text_responses": False})

    observed_bridge_events = []
    orig_complete = bridge.complete

    def wrapped_complete(mid, messages, tool_specs, cancelled, on_event=None, **kwargs):
        def capture(ev):
            observed_bridge_events.append(ev)
            if on_event is not None:
                on_event(ev)
        return orig_complete(mid, messages, tool_specs, cancelled, capture, **kwargs)

    bridge.complete = wrapped_complete

    task2 = runtime.create({"message": "second task"})

    with ThreadPoolExecutor(max_workers=2) as pool:
        f1 = pool.submit(
            orig_complete,
            model_id,
            [{"role": "user", "content": "first blocker"}],
            [],
            lambda: False,
        )
        assert first_entered.wait(timeout=3.0)
        # Start task2 while f1 holds the single slot for ~2.7s
        f2 = pool.submit(runtime.run_once)
        res1 = f1.result(timeout=10.0)
        f2.result(timeout=10.0)

    assert res1["message"]["content"] == "first done"
    done2 = runtime.get(task2["id"])
    assert done2["status"] == "completed"
    assert done2["result"] == "second done"
    assert done2["steps"] == 1
    assert not any(ev["type"] == "model_retry_scheduled" for ev in done2["events"])

    queue_events = [ev for ev in observed_bridge_events if isinstance(ev, dict) and ev.get("type") == "model_queue"]
    assert len(queue_events) == 1
    assert queue_events[0]["reason"] == "service_slots"


def test_t1_model_dispatch_condition_queues_and_wakes_on_finish(node):  # noqa: F811
    """T1 / Change 4: ModelDispatchAdapter._start queues via threading.Condition and wakes immediately on _finish notify."""
    app, _ = node
    record, _ = add_source(app, models=["cond-model"])
    updated = _configure_max_concurrent(app, record, "cond-model", 1)
    sources = app.state.llm_agent_services["source_store"]
    raw_record = sources.read()[updated["id"]]

    entered = threading.Event()
    release = threading.Event()

    class FakeInner:
        supports_request_deadline = True

        def __init__(self):
            self.calls = []

        def chat(self, body, **kwargs):
            tid = kwargs.get("task_id")
            self.calls.append(tid)
            if tid == "req-1":
                entered.set()
                assert release.wait(timeout=5.0)
            return {"text": f"ok-{tid}", "input_tokens": 2, "output_tokens": 2}

    inner = FakeInner()
    gate = ModelDispatchAdapter(sources, raw_record, "cond-model", inner)

    with ThreadPoolExecutor(max_workers=2) as pool:
        f1 = pool.submit(gate.chat, {"messages": [], "max_tokens": 16}, task_id="req-1", timeout_s=10.0)
        assert entered.wait(timeout=2.0)
        f2 = pool.submit(gate.chat, {"messages": [], "max_tokens": 16}, task_id="req-2", timeout_s=10.0)
        time.sleep(0.15)
        assert not f2.done(), "second request should be waiting on Condition while first holds the slot"
        t_release = time.monotonic()
        release.set()
        r1 = f1.result(timeout=3.0)
        r2 = f2.result(timeout=3.0)
        wake_elapsed = time.monotonic() - t_release

    assert r1["text"] == "ok-req-1"
    assert r2["text"] == "ok-req-2"
    assert wake_elapsed < 1.0
    assert sources._active_models.get(gate.service_id, 0) == 0


def test_t2_cancelled_while_queued_exits_immediately_and_busy_carries_reason(node, monkeypatch):  # noqa: F811
    """T2 (Changes 2, 4): Cancelling while queued in bridge or ModelDispatchAdapter exits immediately; timeout raises agent_model_busy with reason in diagnostics."""
    app, store = node
    record, model_id = add_source(app, models=["cancel-model"])
    updated = _configure_max_concurrent(app, record, "cancel-model", 1)
    sources = app.state.llm_agent_services["source_store"]
    sid = sources.model_service_id(record, "cancel-model")
    manager = app.state.llm_agent_services["provider_for"](sid)
    bridge, _, _ = build_agent_integrations(app, store, store.home)

    # Part A: Bridge slot queue cancellation exits promptly (well under 0.5s, never waiting 120s)
    assert manager._slots.acquire() is True
    try:
        cancel_ev = threading.Event()
        with ThreadPoolExecutor(max_workers=1) as pool:
            fut = pool.submit(
                bridge.complete,
                model_id,
                [{"role": "user", "content": "queued"}],
                [],
                cancel_ev.is_set,
            )
            time.sleep(0.1)
            t0 = time.monotonic()
            cancel_ev.set()
            with pytest.raises(AgentError, match="agent_cancelled"):
                fut.result(timeout=2.0)
            assert time.monotonic() - t0 < 0.5

        # Part B: When slot remains occupied past slot wait cap, raises AgentError("agent_model_busy") with reason in diagnostics
        monkeypatch.setattr(bridge_module, "SLOT_WAIT_MAX_SECONDS", 0.25, raising=False)
        with pytest.raises(AgentError, match="agent_model_busy") as exc_info:
            bridge.complete(
                model_id,
                [{"role": "user", "content": "still busy"}],
                [],
                lambda: False,
            )
        assert getattr(exc_info.value, "diagnostics", {}).get("reason") == "service_slots"
    finally:
        manager._slots.release()

    # Part C: ModelDispatchAdapter queue cancellation exits immediately on cancel(task_id)
    raw_record = sources.read()[updated["id"]]
    entered = threading.Event()
    release = threading.Event()

    class BlockingInner:
        def chat(self, body, **kwargs):
            entered.set()
            release.wait(timeout=5.0)
            return {"text": "done"}

    gate = ModelDispatchAdapter(sources, raw_record, "cancel-model", BlockingInner())
    with ThreadPoolExecutor(max_workers=2) as pool:
        f1 = pool.submit(gate.chat, {"messages": [], "max_tokens": 16}, task_id="hold-1", timeout_s=10.0)
        assert entered.wait(timeout=2.0)
        try:
            f2 = pool.submit(gate.chat, {"messages": [], "max_tokens": 16}, task_id="queued-cancel", timeout_s=30.0)
            time.sleep(0.1)
            t0 = time.monotonic()
            gate.cancel("queued-cancel")
            with pytest.raises(AdapterError, match="task_cancelled"):
                f2.result(timeout=2.0)
            assert time.monotonic() - t0 < 0.5
        finally:
            release.set()
        f1.result(timeout=3.0)


def test_t8_api_source_defaults_max_concurrent_to_4_while_local_stays_1(tmp_path):
    """T8 (Changes 3, 5): Unknown vendor models default to max_concurrent=4 for kind=='api' and 1 for kind=='local'; known vendors keep table values."""
    store = SourceStore(tmp_path, b"test-private-key-s01-i19")

    api_rec = store.prepare({
        "name": "Custom Cloud API",
        "kind": "api",
        "base_url": "https://api.example.com/v1",
        "api_key": "sk-example",
        "models": ["custom-cloud-v1", "deepseek-flash"],
    })
    pub_api = store.save(api_rec)
    assert model_settings.defaults(api_rec, "custom-cloud-v1")["max_concurrent"] == 4
    assert pub_api["model_settings"]["custom-cloud-v1"]["effective"]["max_concurrent"] == 4
    # Known vendor prefix still uses KNOWN_VENDOR_MAX_CONCURRENT
    assert pub_api["model_settings"]["deepseek-flash"]["effective"]["max_concurrent"] == 2500

    local_rec = store.prepare({
        "name": "Local Loopback",
        "kind": "local",
        "base_url": "http://127.0.0.1:11434/v1",
        "models": ["custom-local-v1"],
    })
    pub_local = store.save(local_rec)
    assert model_settings.defaults(local_rec, "custom-local-v1")["max_concurrent"] == 1
    assert pub_local["model_settings"]["custom-local-v1"]["effective"]["max_concurrent"] == 1
