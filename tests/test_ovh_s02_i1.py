"""S02-I1: owner-stop context survives the real worker/continuation path."""
import copy
import threading

import pytest

from test_agent_runtime import advance, assistant, make_runtime
from test_agent_conversation_controls import _run, send
from rynmesh.agent_runtime.runtime import AgentRuntime
from rynmesh.agent_runtime.store import AgentError, AgentStore


@pytest.mark.parametrize("enabled", [True, False])
def test_t1_t2_t3_stop_context_and_partial_text_reach_next_model_request(tmp_path, enabled):
    runtime, model, tools, key = make_runtime(tmp_path)
    if not enabled:
        runtime.update_settings({"interrupt_message_enabled": False})
    entered, release = threading.Event(), threading.Event()
    requests, failures = [], []

    def inference(model_id, messages, specs, cancelled, on_event):
        requests.append(copy.deepcopy(messages))
        if len(requests) > 1:
            return assistant("Continued.")
        on_event({"type": "text_delta", "text": "PARTIAL_RESPONSE"})
        entered.set()
        assert release.wait(10)
        assert cancelled()
        return assistant("DISCARD_LATE_RESPONSE")

    model.complete = inference
    row = runtime.create({"message": "First request"})
    worker = threading.Thread(target=_run, args=(runtime, failures))
    worker.start()
    try:
        assert entered.wait(10)
        paused = runtime.action(row["id"], {"action": "stop"})
        partial = next(m for m in paused["messages"] if m["content"] == "PARTIAL_RESPONSE")
        assert partial["interrupted"] is True
        notes = [m for m in paused["messages"] if m.get("ui", {}).get("kind") == "turn_aborted"]
        assert len(notes) == int(enabled)
        if enabled:
            assert notes[0]["role"] == "user" and notes[0]["ui"]["hidden"] is True
            assert notes[0]["context_epoch"] == paused.get("context_epoch", 0)
            assert "<turn_aborted>" in notes[0]["content"]
        assert any(e["type"] == "paused" for e in paused["events"])
    finally:
        release.set()
        worker.join(10)
    assert not worker.is_alive() and not failures
    # Prove encrypted persistence and normal continuation retain the note.
    restarted = AgentRuntime(AgentStore(tmp_path, key), model, tools, runtime.catalog)
    restarted.reconcile()
    send(restarted, row["id"], "Continue")
    advance(restarted)
    contents = [m.get("content", "") for m in requests[-1]]
    assert "PARTIAL_RESPONSE" in contents
    assert "DISCARD_LATE_RESPONSE" not in contents
    assert sum("<turn_aborted>" in text for text in contents) == int(enabled)
    if enabled:
        note = next(text for text in contents if "<turn_aborted>" in text)
        assert all(word in note.lower() for word in ("intentionally", "background", "partially", "read-only"))


def test_t1_repeated_stop_replaces_only_trailing_note(tmp_path):
    runtime, _, _, _ = make_runtime(tmp_path, assistant("Done"))
    row = runtime.create({"message": "Request"})
    first = runtime.action(row["id"], {"action": "stop"})
    second = runtime.action(row["id"], {"action": "stop"})
    notes = [m for m in second["messages"] if m.get("ui", {}).get("kind") == "turn_aborted"]
    assert len(notes) == 1
    assert len(second["messages"]) == len(first["messages"])
    send(runtime, row["id"], "Next request")
    third = runtime.action(row["id"], {"action": "stop"})
    assert sum(m.get("ui", {}).get("kind") == "turn_aborted" for m in third["messages"]) == 2


def test_t2_setting_defaults_on_and_rejects_non_boolean(tmp_path):
    runtime, _, _, key = make_runtime(tmp_path)
    assert runtime.settings()["interrupt_message_enabled"] is True
    for value in (0, "false", None):
        with pytest.raises(AgentError, match="agent_invalid_settings"):
            runtime.update_settings({"interrupt_message_enabled": value})
    runtime.update_settings({"interrupt_message_enabled": False})
    restarted = AgentRuntime(AgentStore(tmp_path, key), runtime.model, runtime.tools, runtime.catalog)
    assert restarted.settings()["interrupt_message_enabled"] is False


def test_t1_hidden_note_does_not_shift_edited_owner_turn_index(tmp_path):
    runtime, _, _, _ = make_runtime(tmp_path, assistant("Done"))
    row = runtime.create({"message": "First"})
    runtime.action(row["id"], {"action": "stop"})
    continued = send(runtime, row["id"], "Second")
    edited = send(runtime, row["id"], "Edited second", edit_message_index=len(continued["messages"]) - 1)
    assert edited["events"][-1]["type"] == "message_edited"
    assert edited["events"][-1]["user_turn_index"] == 1
