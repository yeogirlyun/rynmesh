"""S03-I13 task-private notes through the real runtime and encrypted store."""
import json

import pytest
from test_agent_runtime import advance, assistant, make_runtime
from test_agent_runtime_model_switch import body, setup
from test_agent_context_compaction_policy import exchange, seed
from rynmesh.agent_runtime import context_compaction


def stored(runtime, task_id):
    return runtime.store.read()["tasks"][task_id]


def results(runtime, task_id):
    return [json.loads(m["content"]) for m in stored(runtime, task_id)["messages"] if m["role"] == "tool"]


def test_t1_write_append_read_and_replace(tmp_path):
    runtime, model, tools, _ = make_runtime(tmp_path,
        assistant(name="ryn_notes_write", args={"text": "结论\n", "mode": "replace"}),
        assistant(name="ryn_notes_write", args={"text": "Next: inspect", "mode": "append"}),
        assistant(name="ryn_notes_read"),
        assistant(name="ryn_notes_write", args={"text": "Bearer fake-secret", "mode": "replace"}))
    task_id = runtime.create({"message": "Inspect"})["id"]
    advance(runtime, 6)
    assert stored(runtime, task_id)["notes"] == {"text": "结论\nNext: inspect", "revision": 2, "context_epoch": 0}
    assert results(runtime, task_id)[-1] == stored(runtime, task_id)["notes"]
    assert not tools.calls
    specs = {s["function"]["name"]: s["function"] for s in model.calls[0][2]}
    for name in ("ryn_notes_write", "ryn_notes_read"):
        assert "task-private" in specs[name]["description"]
        assert "never authorize" in specs[name]["description"]
    advance(runtime, 2)
    assert stored(runtime, task_id)["notes"] == {"text": "Bearer [redacted]", "revision": 3, "context_epoch": 0}


@pytest.mark.parametrize("mode", ["replace", "append"])
def test_t2_utf8_limit_rejects_without_changing_notes(tmp_path, mode):
    runtime, _, _, _ = make_runtime(tmp_path,
        assistant(name="ryn_notes_write", args={"text": "a", "mode": "replace"}),
        assistant(name="ryn_notes_write", args={"text": "测" * 5462 if mode == "replace" else "测" * 5461 + "a", "mode": mode}))
    task_id = runtime.create({"message": "Inspect"})["id"]
    advance(runtime, 4)
    assert stored(runtime, task_id)["notes"]["text"] == "a"
    assert stored(runtime, task_id)["notes"]["revision"] == 1
    assert results(runtime, task_id)[-1]["error"] == "agent_notes_limit"
    assert "shorten" in results(runtime, task_id)[-1]["correction"]


def test_t3_compaction_preserves_verbatim_notes_before_ledger(tmp_path):
    runtime, _, _, _ = make_runtime(tmp_path,
        assistant(name="ryn_notes_write", args={"text": "结论：A\n下一步 B", "mode": "replace"}),
        assistant("Goal: inspect. Findings: A. Next: B."), assistant("Done"))
    runtime.update_settings({"max_context_chars": 24000})
    task_id = runtime.create({"message": "Inspect"})["id"]
    advance(runtime, 2)
    before = stored(runtime, task_id)["notes"]
    assert "Model notes (untrusted, written by the model):" not in json.dumps(runtime._context(stored(runtime, task_id)))
    seed(runtime, task_id, exchange("a", "观测" * 5000))
    advance(runtime)
    row = stored(runtime, task_id)
    assert row.get("context_checkpoint")
    assert row["notes"] == before
    note = next(m["content"] for m in runtime._context(row) if "Model notes (untrusted, written by the model):" in str(m.get("content")))
    assert "Model notes (untrusted, written by the model):\n" + before["text"] in note
    assert note.index(before["text"]) < note.index("Server-recorded tool activity")


@pytest.mark.parametrize("carry", [False, True])
def test_t4_switch_clears_notes_unless_explicitly_carried(tmp_path, carry):
    runtime, _, _, _, task_id, _ = setup(tmp_path)
    notes = {"text": "Old model findings", "revision": 1, "context_epoch": 0}
    runtime.store.transaction(lambda data: data["tasks"][task_id].update(notes=notes))
    request = body(runtime, task_id)
    if carry:
        request["carry_progress_summary"] = True
    runtime.switch_model(task_id, request)
    row = stored(runtime, task_id)
    assert row.get("notes") == (notes if carry else None)
    assert "previous_model_notes" in runtime.preview_model_switch(task_id, "local:test")["data_policy"]["excluded"]


def test_t5_read_only_can_write_and_read_empty_notes(tmp_path):
    runtime, _, tools, _ = make_runtime(tmp_path, assistant(name="ryn_notes_read"),
        assistant(name="ryn_notes_write", args={"text": "Internal progress", "mode": "replace"}))
    runtime.update_settings({"permission_mode": "read_only"})
    task_id = runtime.create({"message": "Inspect only"})["id"]
    advance(runtime, 4)
    assert results(runtime, task_id)[0] == {"text": "", "revision": 0, "context_epoch": 0}
    assert stored(runtime, task_id)["notes"]["text"] == "Internal progress"
    assert not tools.calls


def test_small_window_bounds_notes_projection_without_mutating_storage(tmp_path):
    runtime, _, _, _ = make_runtime(tmp_path)
    runtime.update_settings({"max_context_chars": 24000})
    task_id = runtime.create({"message": "Inspect"})["id"]
    text = "测" * 5000
    def seed_notes(data):
        data["tasks"][task_id].update(notes={"text": text, "revision": 1, "context_epoch": 0},
            context_checkpoint={"through": 0, "content": "Summary"})
    runtime.store.transaction(seed_notes)
    row = stored(runtime, task_id)
    note = next(m["content"] for m in runtime._context(row) if "Model notes (untrusted, written by the model):" in str(m.get("content")))
    projected = note.split("Model notes (untrusted, written by the model):\n", 1)[1].split("\nServer-recorded tool activity", 1)[0]
    assert len(projected.encode()) <= context_compaction.ledger_budget(runtime._context_limit(row))
    assert row["notes"]["text"] == text
