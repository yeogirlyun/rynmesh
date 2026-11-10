"""A task reserves receipt capacity before any external dispatch."""
from __future__ import annotations

import threading
from types import SimpleNamespace

import pytest
from test_agent_runtime import advance, approve, assistant, make_runtime

from rynmesh.agent_runtime import store as storage
from rynmesh.agent_runtime.store import AgentError, encoded


def test_storage_headroom_failure_prevents_business_dispatch(tmp_path, monkeypatch):
    runtime, _, tools, _ = make_runtime(tmp_path, assistant(name="share_service", args={"service_id": "A"}))
    row = runtime.create({"message": "Share A"})
    advance(runtime, 2)
    approve(runtime, row["id"])
    current = len(encoded(runtime.store.read()))
    monkeypatch.setattr(storage, "MAX_PLAINTEXT", current + 64 * 1024)
    advance(runtime)
    assert not tools.calls and not tools.shared
    stored = runtime.store.read()["tasks"][row["id"]]
    assert stored["in_flight"] is None
    assert stored["receipts"][-1]["status"] == "not_executed"
    assert stored["receipts"][-1]["result"]["error"] == "agent_storage_limit"


def test_disk_space_failure_prevents_model_dispatch(tmp_path, monkeypatch):
    runtime, model, _, _ = make_runtime(tmp_path, assistant("answer"))
    row = runtime.create({"message": "Explain"})
    monkeypatch.setattr(storage.shutil, "disk_usage", lambda path: SimpleNamespace(free=0))
    advance(runtime)
    assert not model.calls
    assert runtime.get(row["id"])["error"] == "agent_storage_limit"


def test_concurrent_create_cannot_spend_an_inflight_receipt_reservation(tmp_path, monkeypatch):
    runtime, model, _, _ = make_runtime(tmp_path)
    entered, release = threading.Event(), threading.Event()
    def slow(*args):
        entered.set()
        assert release.wait(3)
        return assistant("Confirmed original result")
    model.complete = slow
    row = runtime.create({"message": "Explain"})
    worker = threading.Thread(target=runtime.run_once)
    worker.start()
    assert entered.wait(3)
    data = runtime.store.read()
    reservation = data["storage_reservations"][row["id"]]
    remaining = reservation["bytes"] - (len(encoded(data["tasks"][row["id"]])) - reservation["baseline"])
    monkeypatch.setattr(storage, "MAX_PLAINTEXT", len(encoded(data)) + remaining + 1000)
    with pytest.raises(AgentError, match="agent_storage_limit"):
        runtime.create({"message": "x" * 6000})
    release.set()
    worker.join(3)
    done = runtime.get(row["id"])
    assert done["status"] == "completed" and done["result"] == "Confirmed original result"
    assert not runtime.store.read()["storage_reservations"]
    assert len(runtime.list_tasks()) == 1
