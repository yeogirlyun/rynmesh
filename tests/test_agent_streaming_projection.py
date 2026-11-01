"""Live text reaches polling clients without an encrypted write per token."""
from types import SimpleNamespace

import pytest
from test_agent_runtime import assistant, make_runtime

from rynmesh.agent_runtime.store import AgentError, AgentStore


def frozen_clock(monkeypatch):
    clock = [1000.0]
    timer = SimpleNamespace(monotonic=lambda: clock[0])
    monkeypatch.setattr("rynmesh.agent_runtime.loop.time", timer)
    monkeypatch.setattr("rynmesh.agent_runtime.runtime.time", timer)
    return clock


def test_live_text_changes_without_a_durable_revision_or_commit(tmp_path, monkeypatch):
    runtime, model, _, key = make_runtime(tmp_path)
    created = runtime.create({"message": "Inspect the disk."})
    clock = frozen_clock(monkeypatch)
    commits = []
    transaction = runtime.store.transaction

    def persist(mutate):
        result = transaction(mutate)
        commits.append(None)
        return result

    monkeypatch.setattr(runtime.store, "transaction", persist)

    def complete(model_id, messages, specs, cancelled, on_event):
        on_event({"type": "text_delta", "text": "我"})
        first = runtime.get(created["id"])
        count = len(commits)
        for piece in ("先", "检查", "一下磁盘。"):
            clock[0] += 0.02
            on_event({"type": "text_delta", "text": piece})
        update = runtime.get(created["id"], first["revision"])
        assert update["unchanged"] and update["revision"] == first["revision"]
        assert update["streaming"] == {"step": 1, "context_epoch": 0, "text": "我先检查一下磁盘。"}
        assert len(commits) == count
        persisted = AgentStore(tmp_path, key).read()["tasks"][created["id"]]
        assert [item["text"] for item in persisted["events"] if item["type"] == "text_delta"] == ["我"]
        assert "streaming" not in persisted
        assert first["control_revision"] == persisted["control_revision"]
        return assistant("我先检查一下磁盘。")

    model.complete = complete
    runtime.run_once()
    done = runtime.get(created["id"])
    assert done["status"] == "completed" and done["result"] == "我先检查一下磁盘。"
    assert done["streaming"] is None and not runtime._streams
    assert runtime.get(created["id"], done["revision"])["streaming"] is None
    assert "".join(item["text"] for item in done["events"] if item["type"] == "text_delta") == done["result"]


@pytest.mark.parametrize("stop", ["pause", "cancel", "error"])
def test_live_text_is_hidden_after_owner_control_or_model_failure(tmp_path, monkeypatch, stop):
    runtime, model, _, _ = make_runtime(tmp_path)
    created = runtime.create({"message": "Inspect the disk."})
    frozen_clock(monkeypatch)

    def complete(model_id, messages, specs, cancelled, on_event):
        on_event({"type": "text_delta", "text": "Current observation."})
        on_event({"type": "text_delta", "text": " Uncommitted tail."})
        assert runtime.get(created["id"])["streaming"]["text"].endswith("Uncommitted tail.")
        if stop == "error":
            raise AgentError("agent_inference_failed")
        runtime.action(created["id"], {"action": stop})
        assert runtime.get(created["id"])["streaming"] is None
        return assistant("Late answer.")

    model.complete = complete
    runtime.run_once()
    done = runtime.get(created["id"])
    assert done["streaming"] is None and not runtime._streams
    assert not any(item["type"] == "assistant" for item in done["events"])


def test_late_stream_cleanup_cannot_remove_a_successor_request(tmp_path):
    runtime, _, _, _ = make_runtime(tmp_path)
    selected = {"id": "task", "context_epoch": 0, "_run_control_revision": 1}
    first = runtime._start_stream(selected, 1)
    second = runtime._start_stream(selected, 2)
    runtime._append_stream("task", second, "New response")
    runtime._append_stream("task", first, "Stale response")
    runtime._clear_stream("task", first)
    row = {"id": "task", "status": "running", "steps": 2, "context_epoch": 0,
           "control_revision": 1, "in_flight": {"kind": "model", "dispatched": True}}
    assert runtime._stream_snapshot(row) == {"step": 2, "context_epoch": 0, "text": "New response"}
    for changes in ({"control_revision": 2}, {"context_epoch": 1}, {"steps": 3},
                    {"in_flight": {"kind": "tool", "dispatched": True}}, {"status": "paused"}):
        assert runtime._stream_snapshot({**row, **changes}) is None
    runtime._clear_stream("task", second)
    assert not runtime._streams
