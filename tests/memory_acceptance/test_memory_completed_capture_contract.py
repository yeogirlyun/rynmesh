"""Opt-in automatic capture starts after an actual successful owner turn ends."""
from test_memory_integration_contract import (
    finish,
    packed,
    tick_memory_workers,
)
from test_memory_integration_contract import (
    integrated as integrated,
)
from test_memory_integration_contract import (
    prohibit_network as prohibit_network,
)


def test_automatic_worker_waits_for_genuine_completion_before_extracting(integrated):
    env = integrated
    env.api.write("PUT", "/settings", {
        "capture_enabled": True,
        "extraction_destination": env.catalog.destination(env.catalog.row),
    })
    task_id = env.api.create_task("我的机身是富士 X-T5。")
    assert env.runtime.store.read()["tasks"][task_id]["status"] == "queued"
    before_calls = len(env.bridge.requests)
    tick_memory_workers(env)
    assert len(env.bridge.requests) == before_calls, "Queued owner text must not trigger automatic extraction"
    state = env.runtime.store.read()["memory"]
    assert not state["jobs"] and not state["candidates"]

    original_complete = env.bridge.complete
    running_seen = []
    def complete_while_running(model_id, messages, tool_specs, cancelled, on_event=None, **options):
        row = env.runtime.store.read()["tasks"][task_id]
        assert row["status"] == "running" and row.get("in_flight")
        running_seen.append(True)
        count = len(env.bridge.requests)
        tick_memory_workers(env)
        assert len(env.bridge.requests) == count, "A front model call in flight must not start a background extraction"
        state = env.runtime.store.read()["memory"]
        assert not state["jobs"] and not state["candidates"]
        return original_complete(model_id, messages, tool_specs, cancelled, on_event=on_event, **options)

    env.bridge.complete = complete_while_running
    done = finish(env, task_id)
    env.bridge.complete = original_complete
    assert done["status"] == "completed" and running_seen
    tick_memory_workers(env)
    requests = [request for request in env.bridge.requests if request["extractor"]]
    assert len(requests) == 1
    snapshot = env.api.snapshot()
    assert not snapshot["entries"] and len(snapshot["candidates"]) == 1
    assert "X-T5" in packed(snapshot["candidates"])

