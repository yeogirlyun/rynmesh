"""Integration boundaries between fork creation and automatic task segments."""
import pytest
from test_agent_long_task_continuation import small_segments
from test_agent_runtime import advance, assistant, make_runtime
from test_agent_task_forks import env, fork, instruct, parent, stored  # noqa: F401


def test_partial_text_can_progress_across_default_segments(tmp_path, monkeypatch):
    small_segments(monkeypatch, steps=2)
    parts = [f"Section {index}: verified explanation.\n" for index in range(5)]
    responses = [{**assistant(part), "finish_reason": "length"} for part in parts[:-1]]
    runtime, model, _, _ = make_runtime(tmp_path, *responses, assistant(parts[-1]))
    task = runtime.create({"message": "Write the five sections, continuing truncated responses."})
    advance(runtime, 16)
    row = runtime.get(task["id"])
    assert row["status"] == "completed", row.get("error")
    assert row["result"] == "".join(parts)
    assert len(model.calls) == 5


def test_repeated_partial_text_does_not_defeat_no_progress_stop(tmp_path, monkeypatch):
    small_segments(monkeypatch, steps=2)
    repeated = {**assistant("The same unfinished paragraph.\n"), "finish_reason": "length"}
    runtime, model, _, _ = make_runtime(tmp_path, *[repeated for _ in range(16)])
    task = runtime.create({"message": "Write an explanation without repeating paragraphs."})
    advance(runtime, 16)
    row = runtime.get(task["id"])
    assert row["error"] == "agent_no_progress"
    assert len(model.calls) <= 4


@pytest.mark.parametrize("explicit", [False, True])
def test_fork_uses_current_segment_limit_provenance(env, monkeypatch, explicit):  # noqa: F811
    e = env
    small_segments(monkeypatch, steps=2)
    if explicit:
        e.runtime.update_settings({"max_steps": 2})
    else:
        # Existing fixture settings have the default source; shrink only the test segment size.
        e.runtime.store.transaction(lambda data: data["settings"].update(max_steps=2))
    source = parent(e)
    child = fork(e, source)
    state = stored(e, child["id"])
    assert state["limits_source"]["max_steps"] == ("owner" if explicit else "default")
    e.model.responses.extend([assistant(name="inspect_service", args={"service_id": str(i)}) for i in range(4)])
    e.model.responses.append(assistant("Inspected all four services."))
    instruct(e, child["id"], "Inspect four services.")
    advance(e.runtime, 20)
    row = e.runtime.get(child["id"])
    assert row["status"] == ("failed" if explicit else "completed"), row.get("error")
    assert len(e.model.calls) == (2 if explicit else 5)
