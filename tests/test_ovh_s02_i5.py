"""S02-I5: real owner controls disclose mid-turn intent to the next request."""
import copy
import threading

import pytest
from test_agent_runtime import Catalog, advance, assistant, fit_window, make_runtime
from rynmesh.agent_runtime.runtime import AgentRuntime
from rynmesh.agent_runtime.store import AgentStore

SUFFIX = "Cover every open request; answer status questions briefly, then continue."
NOTES = {
    "steer": "This owner message changes the goal: follow it, keep earlier requirements that it does not replace. " + SUFFIX,
    "information": "This owner message adds information: keep the original goal. " + SUFFIX,
}
FINAL_CHECK = "Before the final reply, check it answers the latest owner request and any earlier ones still open."


def _send(runtime, task_id, intent, message):
    row = runtime.get(task_id)
    return runtime.add_message(task_id, {"message": message, "intent": intent,
        "expected_control_revision": row["control_revision"], "expected_goal_revision": row["goal"]["revision"]})


@pytest.mark.parametrize("intent", ["steer", "information"])
@pytest.mark.parametrize("epoch", [0, 1])
def test_t1_running_owner_intent_reaches_model_once_after_settlement_and_restart(tmp_path, intent, epoch):
    runtime, model, tools, key = make_runtime(tmp_path)
    task = runtime.create({"message": "Finish the original request and preserve its constraints."})
    runtime.store.transaction(lambda data: data["tasks"][task["id"]].update(context_epoch=epoch))
    entered, release = threading.Event(), threading.Event()
    requests, failures = [], []

    def complete(*args, **kwargs):
        requests.append(copy.deepcopy(args[1]))
        if len(requests) == 1:
            entered.set()
            assert release.wait(15)
        return assistant("Continue the work." if len(requests) == 1 else "All requests addressed.")

    model.complete = complete

    def run():
        try:
            runtime.run_once()
        except BaseException as exc:
            failures.append(exc)

    worker = threading.Thread(target=run)
    worker.start()
    try:
        assert entered.wait(15)
        _send(runtime, task["id"], intent, "Owner mid-turn direction")
        pending = runtime.store.read()["tasks"][task["id"]]
        assert pending["pending_control"].get("mid_turn") is True
        assert not any(m.get("ui", {}).get("kind") == "runtime_note" for m in pending["messages"])
    finally:
        release.set()
        worker.join(15)
    assert not worker.is_alive() and not failures
    saved = runtime.store.read()["tasks"][task["id"]]
    notes = [m for m in saved["messages"] if m.get("ui", {}).get("kind") == "runtime_note"]
    assert len(notes) == 1
    assert notes[0]["content"] == NOTES[intent]
    assert notes[0]["role"] == "assistant" and notes[0]["ui"]["hidden"] is True
    assert notes[0]["context_epoch"] == epoch
    owner_index = next(i for i, m in enumerate(saved["messages"]) if m.get("content") == "Owner mid-turn direction")
    assert saved["messages"][owner_index + 1] == notes[0]
    restarted = AgentRuntime(AgentStore(tmp_path, key), model, tools, Catalog())
    restarted.reconcile()
    advance(restarted, 2)
    assert len(requests) == 2
    assert sum(m.get("content") == NOTES[intent] for m in requests[-1]) == 1
    assert not any(m.get("content") == NOTES["information" if intent == "steer" else "steer"] for m in requests[-1])
    assert next(m for m in requests[-1] if m.get("content") == NOTES[intent]) == {
        "role": "assistant", "content": NOTES[intent]}


@pytest.mark.parametrize("intent", ["steer", "information"])
def test_t1_idle_followup_does_not_create_mid_turn_note(tmp_path, intent):
    runtime, model, _, _ = make_runtime(tmp_path, assistant("First finished."), assistant("Followup finished."))
    task = runtime.create({"message": "First request"})
    advance(runtime)
    assert runtime.get(task["id"])["status"] == "completed"
    _send(runtime, task["id"], intent, "A separate followup")
    advance(runtime)
    saved = runtime.store.read()["tasks"][task["id"]]
    assert not any(m.get("ui", {}).get("kind") == "runtime_note" for m in saved["messages"])
    assert not any(m.get("content") in NOTES.values() for m in model.calls[-1][1])


@pytest.mark.parametrize("small_window", [False, True])
def test_t2_latest_request_final_check_is_in_real_checkpoint_only(tmp_path, small_window):
    runtime, _, _, _ = make_runtime(tmp_path)
    task = runtime.create({"message": "Complete every open request"})
    if small_window:
        fit_window(runtime, task["id"], 12000, small_window=True)
    row = runtime.store.read()["tasks"][task["id"]]
    assert not any(FINAL_CHECK in str(m.get("content")) for m in runtime._context(row))
    runtime.store.transaction(lambda data: data["tasks"][task["id"]].update(
        context_checkpoint={"through": 1, "content": "Next: continue.", "references": {}}))
    row = runtime.store.read()["tasks"][task["id"]]
    context = runtime._context(row)
    checkpoint = next(m["content"] for m in context if str(m.get("content")).startswith("Historical checkpoint"))
    assert FINAL_CHECK in checkpoint
    assert sum(FINAL_CHECK in str(m.get("content")) for m in context) == 1


def test_checkpoint_continuation_keeps_small_window_fixed_text_budget():
    from rynmesh.agent_runtime.context_compaction import CHECKPOINT_CONTINUATION

    assert len(CHECKPOINT_CONTINUATION.encode("utf-8")) <= 234
    assert CHECKPOINT_CONTINUATION.endswith(FINAL_CHECK)
