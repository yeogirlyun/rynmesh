"""A long conversation is condensed or continued, never refused."""
from __future__ import annotations

from test_agent_runtime import advance, assistant, make_runtime

from rynmesh.agent_runtime import event_compaction
from rynmesh.agent_runtime.store import encoded


def send(runtime, task_id, message):
    row = runtime.get(task_id)
    return runtime.add_message(task_id, {"message": message, "intent": "information",
                                         "expected_control_revision": row["control_revision"],
                                         "expected_goal_revision": row["goal"]["revision"]})


def _turns(count, tools_per_turn=30):
    events, seq = [], 0
    for turn in range(count):
        seq += 1
        events.append({"seq": seq, "type": "created" if turn == 0 else "information_added", "at": f"t{seq}"})
        for step in range(tools_per_turn):
            for kind in ("model_started", "tool_started", "tool_result"):
                seq += 1
                events.append({"seq": seq, "type": kind, "tool": "ryn_shell_execute", "at": f"t{seq}", "run_elapsed_seconds": seq})
        seq += 1
        events.append({"seq": seq, "type": "assistant", "content": f"answer {turn}", "at": f"t{seq}"})
        seq += 1
        events.append({"seq": seq, "type": "completed", "at": f"t{seq}"})
    return events


def test_old_turns_are_condensed_and_recent_turns_kept_whole():
    row = {"events": _turns(15)}
    removed = event_compaction.compact_old_turns(row)
    assert removed > 0
    compacted = [e for e in row["events"] if e["type"] == "turn_compacted"]
    assert len(compacted) == 5 and compacted[0]["operations"] == 30 and compacted[0]["tools"] == {"ryn_shell_execute": 30}
    assert [e["content"] for e in row["events"] if e["type"] == "assistant"] == [f"answer {i}" for i in range(15)]
    recent = row["events"][-(10 * 93):]
    assert sum(1 for e in recent if e["type"] == "tool_started") == 10 * 30
    seqs = [e["seq"] for e in row["events"]]
    assert seqs == sorted(seqs) and len(set(seqs)) == len(seqs)


def test_streamed_text_is_folded_once_the_reply_is_final():
    row = {"events": [{"seq": 1, "type": "model_started"}, {"seq": 2, "type": "text_delta", "text": "Hel"},
                      {"seq": 3, "type": "text_delta", "text": "lo"}, {"seq": 4, "type": "assistant", "content": "Hello"}]}
    assert event_compaction.fold_stream_deltas(row) == 2
    assert [e["type"] for e in row["events"]] == ["model_started", "assistant"]


def test_a_thousand_event_conversation_still_accepts_a_message(tmp_path):
    runtime, _, _, _ = make_runtime(tmp_path, assistant("First."), assistant("After condensing."))
    row = runtime.create({"message": "Start."})
    advance(runtime)

    def grow(data):
        current = data["tasks"][row["id"]]
        base = current["event_cursor"]
        for event in _turns(14, tools_per_turn=25)[1:]:
            current["event_cursor"] = base + event["seq"]
            current["events"].append({**event, "seq": current["event_cursor"]})
    runtime.store.transaction(grow)
    assert len(runtime.store.read()["tasks"][row["id"]]["events"]) >= 1000
    send(runtime, row["id"], "Still there?")
    advance(runtime)
    current = runtime.get(row["id"])
    assert current["result"] == "After condensing." and current["id"] == row["id"]
    assert len(runtime.store.read()["tasks"][row["id"]]["events"]) < 700


def test_an_oversized_conversation_continues_in_a_linked_new_one(tmp_path):
    runtime, model, _, _ = make_runtime(tmp_path, assistant("First."), assistant("Continued answer."))
    row = runtime.create({"message": "Original question about invoices."})
    advance(runtime)
    runtime.store.transaction(lambda data: data["tasks"][row["id"]]["messages"].extend(
        [{"role": "assistant", "content": "x" * 200_000, "context_epoch": 0} for _ in range(11)]))
    assert len(encoded(runtime.store.read()["tasks"][row["id"]])) > event_compaction.TASK_BYTES_HARD_LIMIT
    continued = send(runtime, row["id"], "Please continue with March.")
    assert continued["id"] != row["id"] and continued["continued_from"] == row["id"]
    assert continued["title"].endswith("（续）")
    first = continued["messages"][0]["content"]
    assert "Continued from an earlier conversation" in first and "Original question about invoices." in first
    assert first.rstrip().endswith("Please continue with March.")
    assert runtime.get(row["id"])["continued_to"] == continued["id"]
    advance(runtime)
    assert runtime.get(continued["id"])["result"] == "Continued answer."


def test_continued_titles_get_one_numbered_suffix():
    title = event_compaction.continued_title
    assert title("Invoices") == "Invoices（续）"
    assert title("Invoices（续）") == "Invoices（续 2）"
    assert title("Invoices（续 2）") == "Invoices（续 3）"
    # Titles stacked by an earlier build are folded into one counted suffix.
    assert title("Invoices（续）（续）（续）") == "Invoices（续 4）"
    assert title("") == "（续）"


def test_goal_history_stays_small_in_a_long_conversation(tmp_path):
    runtime, _, _, _ = make_runtime(tmp_path, *[assistant(f"ok {i}") for i in range(40)])
    runtime.update_settings({"max_context_chars": 400000})
    row = runtime.create({"message": "Start."})
    advance(runtime)
    # A task written by an older build kept full goal copies (every earlier message, every time).
    runtime.store.transaction(lambda data: data["tasks"][row["id"]].update(goal_history=[
        {**data["tasks"][row["id"]]["goal"], "constraints": [{"id": "constraint:old", "text": "x" * 5000}]}] * 3))
    sizes = []
    for turn in range(30):
        send(runtime, row["id"], f"Requirement {turn}: " + "detail " * 300)
        advance(runtime)
        sizes.append(len(encoded(runtime.store.read()["tasks"][row["id"]])))
    stored = runtime.store.read()["tasks"][row["id"]]
    history = stored["goal_history"]
    assert len(history) == 20
    assert all("constraints" not in item and "text" not in item for item in history)
    assert history[-1]["constraint_ids"][-1].startswith("constraint:") and len(history[-1]["constraint_ids"]) == 30  # start + 29 earlier messages
    assert "x" * 5000 not in str(history)
    # Growth per owner message stays about the same (was proportional to the number of earlier messages).
    early, late = sizes[5] - sizes[4], sizes[29] - sizes[28]
    assert late < early * 1.5, sizes


def test_full_goal_copies_from_an_older_build_are_shrunk_before_continuing(tmp_path):
    runtime, _, _, _ = make_runtime(tmp_path, assistant("First."), assistant("Same conversation."))
    row = runtime.create({"message": "Start."})
    advance(runtime)
    old_copy = {"revision": 1, "kind": "unclassified", "original_message_id": "m", "text": "t",
                "constraints": [{"id": "constraint:old", "text": "x" * 100_000}]}
    runtime.store.transaction(lambda data: data["tasks"][row["id"]].update(goal_history=[old_copy] * 25))
    assert len(encoded(runtime.store.read()["tasks"][row["id"]])) > event_compaction.TASK_BYTES_HARD_LIMIT
    after = send(runtime, row["id"], "Still there?")
    assert after["id"] == row["id"]  # Shrunk in place, not continued in a new conversation.
    advance(runtime)
    stored = runtime.store.read()["tasks"][row["id"]]
    assert len(stored["goal_history"]) <= 20 and "x" * 1000 not in str(stored["goal_history"])
    assert runtime.get(row["id"])["result"] == "Same conversation."
