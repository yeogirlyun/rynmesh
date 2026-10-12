"""Bounded tool discovery preserves actual dispatch and approval boundaries."""
import copy
import importlib.util
import json
from pathlib import Path

import pytest
from test_agent_runtime import Tools, advance, assistant, make_runtime

from rynmesh.agent_runtime import tool_menu
from rynmesh.agent_runtime.runtime import AgentRuntime
from rynmesh.agent_runtime.store import AgentStore, AgentToolRejected, encoded


class ManyTools(Tools):
    def specs(self):
        original = super().specs()
        extra = []
        for index in range(28):
            spec = copy.deepcopy(original[0])
            spec["function"]["name"] = f"read_fixture_{index}"
            spec["function"]["description"] = "Read the named synthetic business record; permission is checked at dispatch."
            extra.append(spec)
        return original + extra

    def metadata(self, name):
        if name.startswith("read_fixture_"):
            return {"effect": "read", "title": name}
        return super().metadata(name)

    def _validate(self, name, arguments):
        assert name in {item["function"]["name"] for item in self.specs()}
        assert arguments == {"service_id": "svc1"}


def test_twelve_k_runtime_discovers_then_dispatches_and_restores_menu(tmp_path):
    runtime, model, _, key = make_runtime(tmp_path,
        assistant(name=tool_menu.NAME, args={"load": ["inspect_service"]}),
        assistant(name="inspect_service", args={"service_id": "svc1"}), assistant("Read confirmed."))
    tools = runtime.tools = ManyTools()
    runtime.update_settings({"max_context_chars": 12000})
    task = runtime.create({"message": "Inspect the one service."})
    advance(runtime, 2)
    saved = runtime.store.read()["tasks"][task["id"]]
    assert saved["tool_menu"]["loaded_names"] == ["inspect_service"]
    assert tools.calls == []
    assert {s["function"]["name"] for s in model.calls[0][2]} == {
        tool_menu.NAME, "ryn_update_plan", "ryn_request_input", "ryn_read_result"}
    restored = AgentRuntime(AgentStore(tmp_path, key), model, tools, runtime.catalog)
    advance(restored, 3)
    assert restored.get(task["id"])["status"] == "completed"
    assert [call[0] for call in tools.calls] == ["inspect_service"]
    assert "inspect_service" in {s["function"]["name"] for s in model.calls[1][2]}
    assert all(len(encoded(call[1])) + len(encoded(call[2])) <= 12000 for call in model.calls)
    events = restored.get(task["id"])["events"]
    assert any(e["type"] == "tool_result" and e.get("tool") == tool_menu.NAME for e in events)
    # A later correction must use the same small-window menu floor, rather
    # than incorrectly charging every business schema again.
    corrected = restored.add_message(task["id"], {"message": "Check svc1 again.", "intent": "steer",
        "expected_control_revision": restored.get(task["id"])["control_revision"],
        "expected_goal_revision": restored.get(task["id"])["goal"]["revision"]})
    assert corrected["goal"]["revision"] == 2


def test_unexposed_tool_cannot_skip_discovery(tmp_path):
    runtime, _, _, _ = make_runtime(tmp_path, assistant(name="inspect_service", args={"service_id": "svc1"}))
    runtime.tools = ManyTools()
    runtime.update_settings({"max_context_chars": 12000})
    task = runtime.create({"message": "Inspect the service."})
    advance(runtime)
    assert runtime.get(task["id"])["error"] == "agent_invalid_tool_call"
    assert not runtime.tools.calls


def test_loading_write_tool_still_requires_exact_approval(tmp_path):
    runtime, _, _, _ = make_runtime(tmp_path,
        assistant(name=tool_menu.NAME, args={"load": ["share_service"]}),
        assistant(name="share_service", args={"service_id": "svc1"}))
    runtime.tools = ManyTools()
    runtime.update_settings({"max_context_chars": 12000})
    task = runtime.create({"message": "Share service svc1."})
    advance(runtime, 4)
    row = runtime.get(task["id"])
    assert row["status"] == "waiting_approval"
    assert row["pending_approval"]["arguments"] == {"service_id": "svc1"}
    assert not runtime.tools.shared and not runtime.tools.calls


@pytest.mark.parametrize("oversized", [False, True])
def test_closed_latest_exchange_can_be_compacted_when_no_shorter_prefix_fits(tmp_path, oversized):
    responses = ([assistant("x" * 4000)] if oversized else []) + [assistant("Observed fixture evidence; no writes performed."), assistant("Read complete.")]
    runtime, model, _, key = make_runtime(tmp_path, *responses)
    runtime.tools = ManyTools()
    runtime.update_settings({"max_context_chars": 12000})
    task = runtime.create({"message": "Inspect only svc1. Never share anything."})
    def seed(data):
        data["tasks"][task["id"]]["messages"].extend([
            {**assistant(name="inspect_service", args={"service_id": "svc1"}, call_id="observed")["message"],
             "reasoning_content": "PRIVATE_PROVIDER_CONTINUATION" * 500},
            {"role": "tool", "tool_call_id": "observed", "content": json.dumps({"text": "e" * 8800})}])
    runtime.store.transaction(seed)
    original = runtime.store.read()["tasks"][task["id"]]["messages"]
    advance(runtime)
    if oversized:
        rejected = runtime.store.read()["tasks"][task["id"]]
        assert rejected["status"] == "queued" and rejected["model_retry_count"] == 1
        assert not rejected.get("context_checkpoint")
        assert not runtime.tools.calls
        runtime = AgentRuntime(AgentStore(tmp_path, key), model, runtime.tools, runtime.catalog)
        advance(runtime)
    current = runtime.store.read()["tasks"][task["id"]]
    assert current["context_checkpoint"]["through"] == len(original)
    assert current["messages"][:len(original)] == original
    assert "PRIVATE_PROVIDER_CONTINUATION" in original[1]["reasoning_content"]
    assert all("PRIVATE_PROVIDER_CONTINUATION" not in json.dumps(call[1]) for call in model.calls)
    assert len(model.calls) == (3 if oversized else 2) and not model.calls[0][2]
    assert any(message["content"] == original[0]["content"] for message in model.calls[-1][1])
    assert all(len(encoded(call[1])) + len(encoded(call[2])) <= 12000 for call in model.calls)


def test_every_catalog_entry_is_reachable_and_binding_change_clears_loads():
    tools = ManyTools()
    specs, state = tool_menu.project(tools.specs(), [], {}, max_context_bytes=12000,
        authority_bytes=1000, epoch=0, goal_revision=1)
    assert [s["function"]["name"] for s in specs] == [tool_menu.NAME]
    seen, cursor = [], 0
    while cursor is not None:
        result, state = tool_menu.discover(tools.specs(), [], tools.metadata, state, {"cursor": cursor}, capacity_bytes=8952)
        assert "available_tool_names" not in result
        assert specs[0]["function"]["parameters"]["properties"]["load"]["items"]["enum"] == sorted(s["function"]["name"] for s in tools.specs())
        assert len(encoded(result)) <= 8952 // 12
        seen.extend(row["name"] for row in result["tools"])
        cursor = result["next_cursor"]
    assert sorted(seen) == sorted(s["function"]["name"] for s in tools.specs())
    _, state = tool_menu.discover(tools.specs(), [], tools.metadata, state, {"load": ["share_service"]}, capacity_bytes=8952)
    for change in ({"epoch": 1, "goal_revision": 1}, {"epoch": 0, "goal_revision": 2}):
        _, reset = tool_menu.project(tools.specs(), [], state, max_context_bytes=12000, authority_bytes=1000, **change)
        assert not reset["loaded_names"] and reset["revision"] > state["revision"]


def test_loading_confirms_exact_selection_without_repeating_catalog_pages():
    tools = ManyTools()
    _, state = tool_menu.project(tools.specs(), [], {}, max_context_bytes=12000,
        authority_bytes=1000, epoch=0, goal_revision=1)
    receipt, loaded = tool_menu.discover(tools.specs(), [], tools.metadata, state,
        {"load": ["inspect_service"]}, capacity_bytes=8952)
    assert receipt["loaded_names"] == ["inspect_service"]
    assert receipt["tools"] == [] and receipt["next_cursor"] is None
    assert "available_tool_names" not in receipt and receipt["permission_granted"] is False
    assert state["loaded_names"] == []
    assert len(encoded(receipt)) < 400
    browsed, unchanged = tool_menu.discover(tools.specs(), [], tools.metadata, loaded,
        {"load": ["inspect_service"], "cursor": 0}, capacity_bytes=8952)
    assert 0 < len(browsed["tools"]) <= 8 and browsed["next_cursor"] == len(browsed["tools"])
    assert "available_tool_names" not in browsed and unchanged == loaded
    cleared, cleared_state = tool_menu.discover(tools.specs(), [], tools.metadata, loaded,
        {"load": []}, capacity_bytes=8952)
    assert cleared["loaded_names"] == [] and cleared_state["revision"] > loaded["revision"]


def test_reference_uses_same_menu_and_rejects_same_batch_hidden_dispatch():
    path = Path(__file__).parents[1] / "docs/acceptance/ryn-agent-delivery/reference/tool_menu_host.py"
    spec = importlib.util.spec_from_file_location("menu_host", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    backend = ManyTools()
    menu = module.ReferenceMenuTools(backend, max_context_bytes=12000)
    messages = [{"role": "user", "content": "Read svc1."}]
    projected = menu.project(messages, menu.specs())
    assert [s["function"]["name"] for s in projected] == [tool_menu.NAME]
    menu.execute(tool_menu.NAME, {"load": ["inspect_service"]}, cancelled=lambda: False)
    with pytest.raises(AgentToolRejected, match="not_exposed"):
        menu._validate("inspect_service", {"service_id": "svc1"})
    next_specs = menu.project(messages, menu.specs())
    assert {s["function"]["name"] for s in next_specs} == {tool_menu.NAME, "inspect_service"}
    result = menu.execute("inspect_service", {"service_id": "svc1"}, operation_id="read1", cancelled=lambda: False)
    assert result["service_id"] == "svc1" and len(backend.calls) == 1
    assert menu.project(messages, []) == []
    with pytest.raises(ValueError, match="schema_changed"):
        menu.project(messages, next_specs)
    large_history = [*messages, {"role": "assistant", "content": "e" * 11200}]
    before = copy.deepcopy(large_history)
    reduced = menu.project(large_history, menu.specs())
    assert large_history == before
    assert [spec["function"]["name"] for spec in reduced] == [tool_menu.NAME]
    assert menu.state["loaded_names"] == []
    with pytest.raises(AgentToolRejected, match="not_exposed"):
        menu._validate("inspect_service", {"service_id": "svc1"})


def test_eight_directory_pages_share_a_bounded_exchange_without_repeated_index():
    tools = ManyTools()
    business = tools.specs()
    for spec in business:
        spec["function"]["description"] = "Detailed multilingual resource description \u8d44\u6e90 " * 100
    specs, state = tool_menu.project(business, [], {}, max_context_bytes=12000,
        authority_bytes=3000, epoch=0, goal_revision=1)
    directory = specs[0]
    assert len(directory["function"]["parameters"]["properties"]["load"]["items"]["enum"]) == len(business)
    messages, cursor, names = [], 0, []
    for index in range(8):
        receipt, _ = tool_menu.discover(business, [], tools.metadata, state, {"cursor": cursor}, capacity_bytes=8744)
        assert len(encoded(receipt)) <= 8744 // 12
        assert "available_tool_names" not in receipt
        assert all(len(row["description"]) <= 80 for row in receipt["tools"])
        names.extend(row["name"] for row in receipt["tools"])
        messages.append({"role": "tool", "tool_call_id": str(index), "content": encoded(receipt).decode()})
        cursor = receipt["next_cursor"]
        if cursor is None:
            break
    assert len(names) == len(set(names))
    assert len(encoded(messages)) + len(encoded(specs)) + 3000 < 12000


def test_large_name_index_falls_back_to_complete_bounded_pagination():
    tools = ManyTools()
    business = tools.specs()
    for spec in business:
        spec["function"]["name"] += "_" + "long_name" * 8
    specs, state = tool_menu.project(business, [], {}, max_context_bytes=12000,
        authority_bytes=1000, epoch=0, goal_revision=1)
    assert "enum" not in specs[0]["function"]["parameters"]["properties"]["load"]["items"]
    names, cursor = [], 0
    while cursor is not None:
        receipt, _ = tool_menu.discover(business, [], lambda _: {"effect": "read"}, state, {"cursor": cursor}, capacity_bytes=10744)
        names.extend(r["name"] for r in receipt["tools"])
        cursor = receipt["next_cursor"]
    assert names == sorted(s["function"]["name"] for s in business)
    receipt, loaded = tool_menu.discover(business, [], lambda _: {"effect": "read"}, state, {"load": [names[-1]]}, capacity_bytes=10744)
    assert receipt["loaded_names"] == loaded["loaded_names"] == [names[-1]]


@pytest.mark.parametrize("goal_revision,criterion,code", [(99, "goal-coverage", "agent_goal_revision_conflict"),
    (1, "invented-criterion", "agent_invalid_control_proposal")])
def test_rejected_plan_returns_current_server_facts_without_accepting_proposal(tmp_path, goal_revision, criterion, code):
    args = {"goal_revision": goal_revision, "steps": [{"id": "read", "text": "Read resource", "criterion_ids": [criterion], "depends_on": []}]}
    runtime, _, tools, _ = make_runtime(tmp_path, assistant(name="ryn_update_plan", args=args))
    task = runtime.create({"message": "Read the permitted resource; never share."})
    criteria = copy.deepcopy(task["criteria"])
    advance(runtime, 2)
    row = runtime.store.read()["tasks"][task["id"]]
    result = json.loads(row["messages"][-1]["content"])
    assert result["error"] == code and result["no_mutation"]
    assert result["expected_goal_revision"] == task["goal"]["revision"]
    assert result["allowed_criterion_ids"] == [c["id"] for c in criteria]
    assert row["criteria"] == criteria and row["plan"]["steps"] == [] and not tools.calls
