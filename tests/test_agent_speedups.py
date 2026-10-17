"""Step latency: batched local reads, continuous steps, context budget, command output."""
import json
import os
import sys
import threading
import time
import uuid

from test_agent_codex_loop import WorkstationTools
from test_agent_runtime import Catalog, advance, assistant, make_runtime

from rynmesh.agent_runtime import local_tools, state
from rynmesh.agent_runtime.local_processes import HEAD_CHARS, PAGE_CHARS, Processes
from rynmesh.agent_runtime.store import UNKNOWN_WINDOW_CONTEXT_CHARS


def calls(*items):
    return {"message": {"role": "assistant", "content": "", "tool_calls": [
        {"id": "call_" + uuid.uuid4().hex, "type": "function", "function": {"name": name, "arguments": json.dumps(args)}}
        for name, args in items]}}


class ReadTools(WorkstationTools):
    names = ("ryn_shell_execute", "ryn_file_write", "ryn_file_read", "ryn_file_list", "ryn_file_search")


def workstation(tmp_path, *responses):
    workspace = tmp_path / "work"
    workspace.mkdir()
    runtime, model, _, _ = make_runtime(tmp_path / "private", *responses)
    runtime.tools = ReadTools(runtime, tmp_path / "private")
    runtime.update_settings({"workspace_root": str(workspace)})
    return runtime, model, workspace


def test_reads_from_one_response_run_concurrently_in_one_step(tmp_path, monkeypatch):
    runtime, model, workspace = workstation(tmp_path,
        calls(("ryn_file_read", {"path": "a.txt"}), ("ryn_file_read", {"path": "b.txt"}),
              ("ryn_file_list", {"path": "."}), ("ryn_file_read", {"path": "missing.txt"})),
        assistant("Read both files."))
    for name in ("a", "b"):
        (workspace / f"{name}.txt").write_text(f"content {name}\n", encoding="utf-8")
    active, peak, original = [0], [0], local_tools.LocalTools.perform
    lock = threading.Lock()

    def slow(self, name, args, context, cancelled):
        with lock:
            active[0] += 1
            peak[0] = max(peak[0], active[0])
        time.sleep(0.3)
        try:
            return original(self, name, args, context, cancelled)
        finally:
            with lock:
                active[0] -= 1
    monkeypatch.setattr(local_tools.LocalTools, "perform", slow)
    task = runtime.create({"message": "Read a and b."})
    advance(runtime)  # model proposes four reads
    started = time.monotonic()
    advance(runtime)  # one step answers every read
    elapsed = time.monotonic() - started
    row = runtime.store.read()["tasks"][task["id"]]
    tools = [message for message in row["messages"] if message["role"] == "tool"]
    assert len(tools) == 4 and not row["queue"] and row["in_flight"] is None
    assert peak[0] >= 2 and elapsed < 1.15  # four sequential reads take >= 1.2 s
    assert "content a" in tools[0]["content"] and "content b" in tools[1]["content"]
    assert json.loads(tools[3]["content"])["error"].startswith("agent_")
    assert [r["status"] for r in row["receipts"]] == ["confirmed", "confirmed", "confirmed", "failed"]
    advance(runtime)
    assert runtime.get(task["id"])["status"] == "completed"
    assert len(model.calls) == 2


def test_a_write_in_the_same_response_keeps_ordinary_order(tmp_path):
    runtime, _, workspace = workstation(tmp_path,
        calls(("ryn_file_write", {"path": "c.txt", "content": "new\n"}), ("ryn_file_read", {"path": "c.txt"}),
              ("ryn_file_read", {"path": "c.txt", "start_line": 1})),
        assistant("Done."))
    task = runtime.create({"message": "Write then read c."})
    advance(runtime, 2)  # model, then the write alone
    row = runtime.store.read()["tasks"][task["id"]]
    assert [m["role"] for m in row["messages"]].count("tool") == 1 and len(row["queue"]) == 2
    advance(runtime)  # both following reads together
    row = runtime.store.read()["tasks"][task["id"]]
    assert [m["role"] for m in row["messages"]].count("tool") == 3 and not row["queue"]


def test_continuous_runner_finishes_a_task_in_one_worker_call(tmp_path):
    runtime, model, workspace = workstation(tmp_path,
        assistant(name="ryn_file_list", args={"path": "."}), assistant("Listed."))
    task = runtime.create({"message": "List the workspace."})
    result = runtime.run_continuously()
    assert result.activity and runtime.get(task["id"])["status"] == "completed" and len(model.calls) == 2
    assert not runtime.run_continuously().activity


def test_continuous_runner_stops_after_a_failed_step(tmp_path):
    runtime, model, _ = workstation(tmp_path, {"message": "not a message"}, assistant("Recovered."))
    task = runtime.create({"message": "Hello."})
    runtime.run_continuously()
    row = runtime.get(task["id"])
    assert len(model.calls) == 1 and row["status"] == "queued"  # The retry waits for the next worker interval.


class WindowCatalog(Catalog):
    def __init__(self, window):
        self.window = window

    def list_models(self):
        return [{**super().list_models()[0], **({"context_window": self.window} if self.window else {})}]


def test_context_budget_follows_a_declared_window(tmp_path):
    runtime, _, _, _ = make_runtime(tmp_path)
    task = runtime.create({"message": "Hi."})
    row = runtime.store.read()["tasks"][task["id"]]
    assert row["limits"]["max_context_chars"] == 600000
    assert runtime._context_limit(row) == UNKNOWN_WINDOW_CONTEXT_CHARS
    runtime.catalog = WindowCatalog(200000)
    assert runtime._context_limit(row) == (200000 - 512) * 3 - 1024
    runtime.catalog = WindowCatalog(8000)
    assert runtime._context_limit(row) == (8000 - 512) * 3 - 1024


def test_old_default_ceiling_is_migrated_once_and_explicit_values_stay():
    data = {"settings": {"max_context_chars": 120000}, "tasks": {}}
    assert state.migrate(data)["settings"]["max_context_chars"] == 600000 and data["context_budget_version"] == 1
    data["settings"]["max_context_chars"] = 120000
    assert state.migrate(data)["settings"]["max_context_chars"] == 120000


def test_finished_command_output_shows_head_and_tail():
    processes = Processes()
    script = "import sys; sys.stdout.write('H' * 3000 + 'M' * 9000 + 'FAILED summary at the end')"
    ident = processes.start([sys.executable, "-c", script], os.getcwd(), "t", 30, lambda: True, lambda: False)
    result = processes.wait(ident, "t", lambda: False)
    processes.close()
    assert result["output"].startswith("H" * HEAD_CHARS) and result["output"].endswith("FAILED summary at the end")
    assert "characters omitted" in result["output"] and result["next_offset"] == HEAD_CHARS
    assert len(result["output"]) < PAGE_CHARS + 200
    page = processes.read(ident, "t", HEAD_CHARS)
    assert page["output"] == ("H" * 1000 + "M" * 9000)[:PAGE_CHARS]


def test_window_id_reaches_conversation_aware_models(tmp_path):
    runtime, model, _, _ = make_runtime(tmp_path, assistant("Hi."))
    seen = []

    class Aware:
        supports_conversation_key = True

        def complete(self, model_id, messages, specs, cancelled, on_event, **options):
            seen.append((bool(specs), options.get("conversation")))
            return model.complete(model_id, messages, specs, cancelled, on_event)
    runtime.model = Aware()
    task = runtime.create({"message": "Hello."})
    advance(runtime)
    assert seen == [(True, task["id"])]


def test_system_prompt_changes_only_in_state_lines_after_a_write(tmp_path):
    from rynmesh.llm_package.codex_threads import state_lines
    runtime, model, _ = workstation(tmp_path,
        assistant(name="ryn_file_write", args={"path": "a.txt", "content": "x\n"}), assistant("Wrote a.txt."))
    runtime.create({"message": "Write a.txt."})
    advance(runtime, 3)
    before, after = model.calls[0][1][0]["content"], model.calls[1][1][0]["content"]
    assert before != after  # the write receipt is visible to the model
    assert state_lines(before, after) is not None  # ...as a state-line update, so a Codex thread continues


def test_continuous_runner_continues_after_single_no_side_effect_tool_error_and_backs_off_on_repeat(tmp_path):
    runtime, model, workspace = workstation(
        tmp_path,
        assistant(name="ryn_file_read", args={"path": "missing_1.txt"}),
        assistant(name="ryn_file_read", args={"path": "existing.txt"}),
        assistant("Recovered after missing file."),
    )
    (workspace / "existing.txt").write_text("ok\n", encoding="utf-8")
    task = runtime.create({"message": "Read the file."})
    result = runtime.run_continuously()
    done = runtime.get(task["id"])
    assert result.activity and done["status"] == "completed"
    assert done["result"] == "Recovered after missing file."
    assert len(model.calls) == 3

    # Consecutive identical (tool_name, error_code) errors must NOT continue cleanly without backoff.
    repeat_dir = tmp_path / "repeat"
    repeat_dir.mkdir()
    runtime2, model2, _ = workstation(
        repeat_dir,
        assistant(name="ryn_file_read", args={"path": "absent_a.txt"}),
        assistant(name="ryn_file_read", args={"path": "absent_b.txt"}),
        assistant("Stopped repeating."),
    )
    task2 = runtime2.create({"message": "Try reading absent files."})
    runtime2.run_continuously()
    row2 = runtime2.get(task2["id"])
    assert len(model2.calls) == 2 and row2["status"] == "queued"
    tool_msgs = [json.loads(m["content"]) for m in runtime2.store.read()["tasks"][task2["id"]]["messages"] if m["role"] == "tool"]
    assert len(tool_msgs) == 2
    assert "correction" in tool_msgs[1]

