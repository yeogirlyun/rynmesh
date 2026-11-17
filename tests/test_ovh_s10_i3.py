"""S10-I3 model goal tools: owner consent, usage, transitions and real dispatch."""
import importlib
import json

import pytest
from test_agent_runtime import advance, assistant, make_runtime

from rynmesh.agent_runtime import budget, subagents
from rynmesh.agent_runtime.loop import ALL_INTERNAL_SPECS
from rynmesh.agent_runtime.output_policy import encoded
from rynmesh.agent_runtime.store import AgentToolRejected
from rynmesh.agent_runtime.runtime import AgentRuntime

NAMES = {"ryn_get_goal", "ryn_create_goal", "ryn_update_goal"}


def goals():
    return importlib.import_module("rynmesh.agent_runtime.goals")


def row(tmp_path):
    rt, _, _, _ = make_runtime(tmp_path)
    task = rt.create({"message": "Explicit persistent goal request"})
    result = rt.store.read()["tasks"][task["id"]]
    result["allow_model_goal_creation"] = True
    return result


def charge(item, tokens, seconds):
    reserved = budget.reserve(item, "model", "test", input_bound=tokens, seconds_bound=seconds)
    budget.settle(item, reserved["id"], usage={"input_tokens": tokens, "output_tokens": 0}, elapsed_seconds=seconds)


def create_goal(item, **kwargs):
    return goals().tool_create(item, {"objective": "  Finish all work  ", **kwargs})


def test_t1_get_remaining_usage_and_clamp_at_zero(tmp_path):
    item = row(tmp_path)
    charge(item, 100, 2)
    created = create_goal(item, token_budget=50)
    assert created["goal"]["objective"] == "Finish all work"
    assert created["goal"]["origin"] == "model"
    charge(item, 20, 3)
    result = goals().tool_get(item, {})
    assert result["goal"]["tokens_used"] == 20
    assert result["goal"]["seconds_used"] == 3
    assert result["goal"]["token_budget"] == 50
    assert result["remainingTokens"] == result["goal"]["remainingTokens"] == 30
    charge(item, 40, 1)
    assert goals().tool_get(item, {})["remainingTokens"] == 0


def test_t2_duplicate_unfinished_goal_is_rejected(tmp_path):
    item = row(tmp_path)
    first = create_goal(item)
    with pytest.raises(AgentToolRejected, match="agent_goal_exists"):
        create_goal(item)
    assert item["thread_goal"]["id"] == first["goal"]["id"]


@pytest.mark.parametrize("count", [0, 1, 2])
def test_t3_blocked_requires_three_consecutive_goal_turns(tmp_path, count):
    item = row(tmp_path)
    created = create_goal(item)
    item["thread_goal"]["blocked_turns"] = count
    with pytest.raises(AgentToolRejected, match="agent_goal_blocked_too_early") as caught:
        goals().tool_update(item, {"status": "blocked", "expected_goal_id": created["goal"]["id"]})
    assert "3" in caught.value.correction
    assert item["thread_goal"]["status"] == "active"
    item["thread_goal"]["blocked_turns"] = 3
    assert goals().tool_update(item, {"status": "blocked", "expected_goal_id": created["goal"]["id"]})["goal"]["status"] == "blocked"


def test_t4_complete_reports_final_usage_and_stays_terminal(tmp_path):
    rt, model, _, _ = make_runtime(tmp_path)
    task_id = rt.create({"message": "Complete the explicit goal"})["id"]
    def seed(data):
        item = data["tasks"][task_id]
        item["allow_model_goal_creation"] = True
        return create_goal(item, token_budget=1000000)["goal"]["id"]
    goal_id = rt.store.transaction(seed)
    model.responses = [assistant(name="ryn_update_goal", args={"status": "complete", "expected_goal_id": goal_id}), assistant("Done.")]
    for _ in range(8):
        advance(rt)
        if rt.get(task_id)["status"] == "completed":
            break
    item = rt.store.read()["tasks"][task_id]
    assert item["thread_goal"]["status"] == "complete"
    assert item["status"] == "completed"
    results = [json.loads(m["content"]) for m in item["messages"] if m["role"] == "tool"]
    assert results[0]["report_usage"] == "Report the final token usage to the owner."
    assert results[0]["goal"]["tokens_used"] > 0
    assert results[0]["remainingTokens"] >= 0
    calls = len(model.calls)
    advance(rt, 3)
    assert len(model.calls) == calls


@pytest.mark.parametrize("tool", ["tool_get", "tool_create", "tool_update"])
def test_t5_subagent_goal_tools_are_forbidden_even_with_inherited_data(tmp_path, tool):
    item = row(tmp_path)
    create_goal(item)
    item["parent_task_id"] = "parent"
    assert not (NAMES & {s["function"]["name"] for s in AgentRuntime._internal_specs(item)})
    with pytest.raises(AgentToolRejected, match="agent_goal_subagent_forbidden"):
        getattr(goals(), tool)(item, {"objective": "x", "status": "complete", "expected_goal_id": item["thread_goal"]["id"]})


def test_t6_specs_fit_pin_floor_and_describe_strict_creation_and_completion():
    specs = {s["function"]["name"]: s["function"] for s in ALL_INTERNAL_SPECS}
    assert NAMES <= specs.keys()
    assert len(encoded(ALL_INTERNAL_SPECS)) <= 13840
    assert "explicit" in specs["ryn_create_goal"]["description"].lower()
    assert "ordinary" in specs["ryn_create_goal"]["description"].lower()
    assert "token_budget" in specs["ryn_create_goal"]["description"]
    update = specs["ryn_update_goal"]["description"].lower()
    assert all(word in update for word in ("audit", "3", "owner", "budget"))
    assert set(specs["ryn_update_goal"]["parameters"]["properties"]["status"]["enum"]) == {"complete", "blocked", "paused"}


@pytest.mark.parametrize("arguments,code", [({"objective": "   "}, "agent_goal_invalid_objective"),
    ({"objective": "x" * 4001}, "agent_goal_invalid_objective"),
    ({"token_budget": 0}, "agent_goal_invalid_budget"), ({"token_budget": -1}, "agent_goal_invalid_budget"),
    ({"token_budget": True}, "agent_goal_invalid_budget"),
    ({"token_budget": budget.RANGES["max_total_tokens"][1] + 1}, "agent_goal_invalid_budget")])
def test_creation_validates_objective_and_budget(tmp_path, arguments, code):
    item = row(tmp_path)
    with pytest.raises(AgentToolRejected, match=code):
        create_goal(item, **arguments)
    assert not item.get("thread_goal")


@pytest.mark.parametrize("tool", ["tool_get", "tool_update"])
def test_missing_goal_returns_model_correction(tmp_path, tool):
    with pytest.raises(AgentToolRejected, match="agent_goal_not_found") as caught:
        getattr(goals(), tool)(row(tmp_path), {"status": "complete", "expected_goal_id": "missing"})
    assert "no goal exists" in caught.value.correction.lower()


@pytest.mark.parametrize("message,accepted", [("Pause the goal", True), ("请暂停目标", True),
    ("Do not pause the goal", False), ("不要暂停目标", False), ("Keep working", False)])
def test_pause_requires_latest_explicit_owner_request(tmp_path, message, accepted):
    item = row(tmp_path)
    created = create_goal(item)
    item["messages"] += [{"role": "user", "message_id": "owner_latest", "content": message},
                         {"role": "assistant", "content": "Pause the goal"},
                         {"role": "user", "source": "goal", "content": "Pause the goal"}]
    args = {"status": "paused", "expected_goal_id": created["goal"]["id"]}
    if accepted:
        assert goals().tool_update(item, args)["goal"]["status"] == "paused"
    else:
        with pytest.raises(AgentToolRejected, match="agent_goal_pause_not_requested"):
            goals().tool_update(item, args)


def test_update_checks_goal_identity_and_status_before_mutating(tmp_path):
    item = row(tmp_path)
    created = create_goal(item)
    with pytest.raises(AgentToolRejected, match="agent_goal_id_conflict"):
        goals().tool_update(item, {"status": "complete", "expected_goal_id": "stale"})
    with pytest.raises(AgentToolRejected, match="agent_goal_invalid_status"):
        goals().tool_update(item, {"status": "active", "expected_goal_id": created["goal"]["id"]})
    assert item["thread_goal"]["status"] == "active"


def test_owner_consent_reaches_real_menu_and_model_dispatch(tmp_path):
    rt, model, _, _ = make_runtime(tmp_path)
    ordinary = rt.create({"message": "Ordinary work"})["id"]
    baseline = rt.store.read()["tasks"][ordinary]
    assert not (NAMES & {s["function"]["name"] for s in rt._internal_specs(baseline)})
    task_id = rt.create({"message": "Explicitly create a persistent goal", "allow_model_goal_creation": True})["id"]
    baseline = rt.store.read()["tasks"][task_id]
    assert NAMES <= {s["function"]["name"] for s in rt._internal_specs(baseline)}
    rt.action(ordinary, {"action": "pause", "expected_revision": rt.get(ordinary)["revision"]})
    model.responses = [assistant(name="ryn_create_goal", args={"objective": "  Explicit goal  "}),
                       assistant(name="ryn_create_goal", args={"objective": "Cannot overwrite"}),
                       assistant(name="ryn_get_goal"), assistant("Done.")]
    for _ in range(12):
        advance(rt)
        if rt.get(task_id)["status"] == "completed":
            break
    item = rt.store.read()["tasks"][task_id]
    assert item["thread_goal"]["objective"] == "Explicit goal"
    replies = [json.loads(m["content"]) for m in item["messages"] if m["role"] == "tool"]
    assert replies[1]["error"] == "agent_goal_exists" and replies[1]["not_executed"]
    assert replies[1]["correction"]
    assert replies[2]["goal"]["objective"] == "Explicit goal"


def test_create_requires_owner_permission_and_children_do_not_inherit_goal(tmp_path):
    item = row(tmp_path)
    item.pop("allow_model_goal_creation")
    with pytest.raises(AgentToolRejected, match="agent_goal_creation_not_allowed"):
        create_goal(item)
    rt, _, _, _ = make_runtime(tmp_path / "spawn")
    parent_id = rt.create({"message": "Parent"})["id"]
    def spawn(data):
        parent = data["tasks"][parent_id]
        parent["allow_model_goal_creation"] = True
        create_goal(parent)
        result = subagents.spawn_agent_in_tx(data, parent, {"prompt": "Inspect only", "agent_type": "explorer", "fork_turns": "all"})
        return data["tasks"][result["agent_id"]]
    child = rt.store.transaction(spawn)
    assert child.get("thread_goal") is None
    assert not child.get("allow_model_goal_creation")


def test_owner_consent_is_accepted_by_real_http_task_creation(tmp_path):
    from test_agent_automations import _build_test_env
    _, rt, _, client = _build_test_env(tmp_path)
    with client:
        created = client.post("/api/local/agent/tasks", json={"message": "Explicit persistent goal", "allow_model_goal_creation": True})
        assert created.status_code == 200
        item = rt.store.read()["tasks"][created.json()["id"]]
        assert item["allow_model_goal_creation"] is True
        assert NAMES <= {s["function"]["name"] for s in rt._internal_specs(item)}
        invalid = client.post("/api/local/agent/tasks", json={"message": "Invalid consent", "allow_model_goal_creation": "yes"})
        assert invalid.status_code == 400
        assert invalid.json()["detail"] == "agent_invalid_request"


def test_completed_goal_does_not_authorize_another_goal_without_owner_permission(tmp_path):
    item = row(tmp_path)
    original = create_goal(item)["goal"]["id"]
    goals().tool_update(item, {"status": "complete", "expected_goal_id": original})
    item.pop("allow_model_goal_creation")
    with pytest.raises(AgentToolRejected, match="agent_goal_creation_not_allowed"):
        create_goal(item)
    item["allow_model_goal_creation"] = True
    next_goal = create_goal(item)["goal"]
    assert next_goal["id"] != original
    assert next_goal["status"] == "active" and next_goal["tokens_used"] == 0


@pytest.mark.parametrize("name", sorted(NAMES))
def test_internal_dispatch_refuses_subagent_before_menu_or_mutation(tmp_path, name):
    import time
    rt, _, _, _ = make_runtime(tmp_path)
    task_id = rt.create({"message": "Parent"})["id"]
    selected = rt.store.read()["tasks"][task_id]
    selected["parent_task_id"] = "parent"
    selected["queue"] = [{"name": name, "arguments": {}}]
    with pytest.raises(AgentToolRejected, match="agent_goal_subagent_forbidden"):
        rt._internal_tool(selected, time.monotonic())
    assert not rt.store.read()["tasks"][task_id].get("thread_goal")
