"""The real bridge's empty length response shares the bounded checker repair."""
import pytest
from test_agent_runtime import advance, approve, assistant
from test_agent_runtime_verification import assessment, verified_runtime

from rynmesh.agent_runtime.runtime import AgentRuntime
from rynmesh.agent_runtime.store import AgentModelResponseError, AgentStore


def setup(tmp_path, *, failures=1, finish="length", max_steps=16):
    runtime, model, tools, key = verified_runtime(tmp_path,
        assistant(name="share_service", args={"service_id": "A"}), assistant("A shared"), assessment())
    runtime.update_settings({"max_steps": max_steps})
    original = model.complete
    remaining = [failures]
    def empty(model_id, messages, specs, cancelled, on_event):
        if not specs and remaining[0]:
            remaining[0] -= 1
            model.calls.append((model_id, messages, specs))
            raise AgentModelResponseError({"input_tokens": 21921, "output_tokens": 4096}, finish)
        return original(model_id, messages, specs, cancelled, on_event)
    model.complete = empty
    task = runtime.create({"message": "Share A and report the saved state"})
    advance(runtime, 2)
    approve(runtime, task["id"])
    advance(runtime, 4)
    return runtime, model, tools, key, task["id"]


def test_empty_length_checker_retry_survives_reopen_is_metered_and_never_replays_write(tmp_path):
    runtime, model, tools, key, task_id = setup(tmp_path)
    waiting = runtime.store.read()["tasks"][task_id]
    assert waiting["status"] == "queued" and waiting["verification"]["format_retries"] == 1
    rejected = next(e for e in waiting["events"] if e["type"] == "model_response_rejected")
    assert rejected["stage"] == "verification_model" and rejected["finish_reason"] == "length"
    assert rejected["usage"] == {"input_tokens": 21921, "output_tokens": 4096}
    entries = [e for e in waiting["budget"]["records"] if e["stage"] == "verification_model"]
    assert len(entries) == 1 and entries[0]["charged_tokens"] == 26017 and entries[0]["status"] == "settled"
    reopened = AgentRuntime(AgentStore(tmp_path, key), model, tools, runtime.catalog)
    reopened.reconcile()
    advance(reopened)
    done = reopened.get(task_id)
    assert done["outcome"]["status"] == "verified" and done["steps"] == 4
    assert len(model.calls) == 4 and len(tools.calls) == 1
    assert model.calls[-1][2] == [] and "previous response was invalid" in model.calls[-1][1][0]["content"]
    assert len([e for e in done["events"] if e["type"] == "verification_format_retry_scheduled"]) == 1


def test_second_empty_checker_failure_stops_without_a_second_repair_or_write(tmp_path):
    runtime, model, tools, _, task_id = setup(tmp_path, failures=2)
    advance(runtime, 3)
    done = runtime.get(task_id)
    assert done["status"] == "completed" and done["outcome"]["status"] == "unverified"
    assert len(model.calls) == 4 and len(tools.calls) == 1
    assert len([e for e in done["events"] if e["type"] == "verification_format_retry_scheduled"]) == 1
    assert len([e for e in done["events"] if e["type"] == "model_response_rejected"]) == 2


@pytest.mark.parametrize("finish", ["content_filter", "cancelled", "unknown", "stop"])
def test_non_length_bridge_rejection_does_not_enter_format_retry(tmp_path, finish):
    runtime, model, tools, _, task_id = setup(tmp_path, finish=finish)
    advance(runtime, 2)
    done = runtime.get(task_id)
    assert done["outcome"]["status"] == "unverified"
    assert len(model.calls) == 3 and len(tools.calls) == 1
    assert not any(e["type"] == "verification_format_retry_scheduled" for e in done["events"])


@pytest.mark.parametrize("action", ["pause", "cancel", "disable"])
def test_owner_control_stops_queued_empty_checker_retry(tmp_path, action):
    runtime, model, tools, _, task_id = setup(tmp_path)
    if action == "disable":
        runtime.update_settings({"enabled": False})
    else:
        runtime.action(task_id, {"action": action})
    advance(runtime, 2)
    assert len(model.calls) == 3 and len(tools.calls) == 1
    assert runtime.get(task_id)["outcome"]["status"] != "verified"


def test_exhausted_step_budget_prevents_empty_checker_retry(tmp_path):
    runtime, model, tools, _, task_id = setup(tmp_path, max_steps=3)
    advance(runtime, 2)
    done = runtime.get(task_id)
    assert done["status"] == "completed" and done["outcome"]["status"] == "unverified"
    assert len(model.calls) == 3 and len(tools.calls) == 1
    assert not any(e["type"] == "verification_format_retry_scheduled" for e in done["events"])
