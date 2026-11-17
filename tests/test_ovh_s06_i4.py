"""S06-I4: append-only loaded schemas and usage-aware eviction."""
import copy

import pytest
from test_agent_runtime import advance, assistant, fit_window, make_runtime
from test_agent_streamed_reads import Reads, raw
from test_agent_tool_menu import ManyTools

from rynmesh.agent_runtime import tool_menu
from rynmesh.agent_runtime.store import AgentToolRejected, encoded


def specs():
    return [{"type": "function", "function": {"name": name,
        "description": "fixture filler " * 100 + (" uniquequartz" if name == "tool_D" else ""),
        "parameters": {"type": "object", "properties": {}}}}
        for name in [f"tool_{letter}" for letter in "ABCDEFGH"]]


def state(loaded=()):
    return {"mode": "directory", "catalog_revision": tool_menu.catalog_revision(specs()),
        "context_epoch": 0, "goal_revision": 1, "lazy_controls": False,
        "loaded_names": list(loaded), "revision": 1}


def size(names):
    by_name = {s["function"]["name"]: s for s in specs()}
    return len(encoded([tool_menu.directory_spec(specs()), *(by_name[n] for n in names)]))


def discover(menu, args, *, count=3, usage=None):
    cap = size(["tool_A", "tool_B", "tool_D"][:count])
    return tool_menu.discover(specs(), [], lambda _: {"effect": "read"}, menu, args,
                             capacity_bytes=cap, usage=usage)


def test_t1_project_evicts_tail_and_keeps_initial_items():
    for initial, expected in [((), ["tool_A", "tool_B"]), (("tool_B",), ["tool_B"])]:
        cap = size(expected)
        exposed, result = tool_menu.project(specs(), [], state(["tool_A", "tool_B", "tool_C"]),
            max_context_bytes=32000, authority_bytes=32000-256-cap, epoch=0, goal_revision=1,
            initial_names=initial)
        assert result["loaded_names"] == expected
        assert [s["function"]["name"] for s in exposed] == [tool_menu.NAME, *expected]
        assert len(encoded(exposed)) <= cap


def test_t1_discovery_evicts_tail_not_oldest_and_preserves_schema_prefix():
    original = state(["tool_A", "tool_B", "tool_C"])
    before = copy.deepcopy(original)
    _, result = discover(original, {"load": ["tool_D"]})
    assert result["loaded_names"] == ["tool_A", "tool_B", "tool_D"]
    assert original == before
    by_name = {s["function"]["name"]: s for s in specs()}
    assert encoded([by_name[n] for n in before["loaded_names"][:2]]) == encoded([by_name[n] for n in result["loaded_names"][:2]])


@pytest.mark.parametrize("window", [17000, 32000, 47000])
def test_t2_owner_revision_keeps_loaded_tools_in_medium_windows(window):
    original = state(["tool_A", "tool_B"])
    exposed, result = tool_menu.project(specs(), [], original, max_context_bytes=window,
        authority_bytes=1000, epoch=0, goal_revision=2)
    assert result["loaded_names"][:2] == ["tool_A", "tool_B"]
    assert [s["function"]["name"] for s in exposed][:3] == [tool_menu.NAME, "tool_A", "tool_B"]


def test_t3_new_epoch_can_rebuild_and_small_loads_append():
    _, result = tool_menu.project(specs(), [], state(["tool_A", "tool_B"]),
        max_context_bytes=32000, authority_bytes=1000, epoch=1, goal_revision=1,
        initial_names=("tool_D",))
    assert result["loaded_names"] == ["tool_D"]
    _, loaded_a = discover(state(), {"load": ["tool_A"]})
    _, loaded_b = discover(loaded_a, {"load": ["tool_B"]})
    assert loaded_b["loaded_names"] == ["tool_A", "tool_B"]
    _, cleared = discover(loaded_b, {"load": []})
    assert cleared["loaded_names"] == []


@pytest.mark.parametrize("usage,expected", [
    ({"tool_A": 5}, ["tool_A", "tool_B", "tool_D"]),
    ({"tool_B": 5, "tool_C": 6}, ["tool_B", "tool_C", "tool_D"]),
    ({"tool_A": 9, "tool_B": 3, "tool_C": 7}, ["tool_A", "tool_C", "tool_D"]),
    ({"tool_A": 3, "tool_B": 3, "tool_C": 3}, ["tool_A", "tool_B", "tool_D"]),
])
def test_t4_unused_before_lru_with_tail_tie_breaks(usage, expected):
    _, result = discover(state(["tool_A", "tool_B", "tool_C"]), {"load": ["tool_D"]}, usage=usage)
    assert result["loaded_names"] == expected


def test_t5_query_appends_and_new_requests_are_never_silently_dropped():
    _, result = discover(state(["tool_A", "tool_B"]), {"query": "uniquequartz"})
    assert result["loaded_names"] == ["tool_A", "tool_B", "tool_D"]
    with pytest.raises(AgentToolRejected, match="agent_tool_schema_limit"):
        tool_menu.discover(specs(), [], lambda _: {"effect": "read"}, state(["tool_A"]),
            {"load": ["tool_D"]}, capacity_bytes=size([]))


def test_explicit_batch_and_existing_query_matches_are_protected():
    _, result = discover(state(["tool_A", "tool_B", "tool_C"]), {"load": ["tool_D", "tool_E"]})
    assert result["loaded_names"] == ["tool_A", "tool_D", "tool_E"]
    catalog = specs()
    catalog[1]["function"]["description"] += " uniquequartz"
    menu = state(["tool_A", "tool_B", "tool_C"])
    menu["catalog_revision"] = tool_menu.catalog_revision(catalog)
    by_name = {s["function"]["name"]: s for s in catalog}
    cap = len(encoded([tool_menu.directory_spec(catalog), *(by_name[n] for n in ["tool_B", "tool_C", "tool_D"])]))
    _, result = tool_menu.discover(catalog, [], lambda _: {"effect": "read"}, menu,
        {"query": "uniquequartz"}, capacity_bytes=cap, usage={"tool_A": 5, "tool_C": 6})
    assert result["loaded_names"] == ["tool_B", "tool_C", "tool_D"]


def test_usage_is_monotonic_across_step_resets_and_never_changes_menu_revision():
    row = {"steps": 8, "context_epoch": 0, "tool_menu": state(["tool_A"])}
    before = copy.deepcopy(row["tool_menu"])
    tool_menu.record_usage(row, ["tool_A", "tool_B"], epoch=0)
    first = row["tool_menu_usage"]["tool_A"]
    assert first == row["tool_menu_usage"]["tool_B"] >= 8
    row["steps"] = 1
    tool_menu.record_usage(row, ["tool_C"], epoch=0)
    assert row["tool_menu_usage"]["tool_C"] > first
    assert row["tool_menu"] == before
    row["context_epoch"] = 1
    usage = copy.deepcopy(row["tool_menu_usage"])
    tool_menu.record_usage(row, ["stale"], epoch=0)
    assert row["tool_menu_usage"] == usage


class TwoReads(Reads):
    def specs(self):
        result = []
        for name in ("ryn_file_read", "ryn_file_search"):
            item = copy.deepcopy(super().specs()[0])
            item["function"]["name"] = name
            result.append(item)
        return result


@pytest.mark.parametrize("mode", ["single", "batch", "stream"])
def test_real_execution_paths_record_usage_on_row_not_menu(tmp_path, mode):
    rt, model, tools, _ = make_runtime(tmp_path)
    if mode == "single":
        proposed = assistant(name="inspect_service", args={"service_id": "svc1"})["message"]["tool_calls"]
        expected = {"inspect_service"}
    else:
        rt.tools = TwoReads()
        proposed = [raw(0), raw(1, name="ryn_file_search")]
        expected = {"ryn_file_read", "ryn_file_search"}
    def complete(model_id, messages, tool_specs, cancelled, on_event):
        if mode == "stream":
            for index, call in enumerate(proposed):
                on_event({"type": "tool_call_complete", "index": index, "call": call})
        return {"message": {"role": "assistant", "content": "", "tool_calls": proposed}}
    model.complete = complete
    task_id = rt.create({"message": "Inspect fixture inputs"})["id"]
    advance(rt)
    before = copy.deepcopy(rt.store.read()["tasks"][task_id]["tool_menu"])
    if mode != "stream":
        advance(rt)
    saved = rt.store.read()["tasks"][task_id]
    assert expected <= saved["tool_menu_usage"].keys()
    assert saved["tool_menu"] == before and "usage" not in saved["tool_menu"]
    assert len(rt.tools.calls) == (1 if mode == "single" else 2)


def test_usage_is_forwarded_to_real_discovery_path(tmp_path, monkeypatch):
    rt, _, _, _ = make_runtime(tmp_path, assistant(name=tool_menu.NAME, args={"load": ["inspect_service"]}))
    rt.tools = ManyTools()
    rt.update_settings({"max_context_chars": 12000})
    task_id = rt.create({"message": "Discover fixture tools"})["id"]
    fit_window(rt, task_id, 12000, small_window=True)
    advance(rt)
    usage = {"read_fixture_5": 20}
    rt.store.transaction(lambda data: data["tasks"][task_id].update(tool_menu_usage=usage))
    real, seen = tool_menu.discover, []
    def spy(*args, **kwargs):
        seen.append(copy.deepcopy(kwargs.get("usage")))
        return real(*args, **kwargs)
    monkeypatch.setattr(tool_menu, "discover", spy)
    advance(rt)
    assert seen == [usage]


def test_new_epoch_resets_recorded_usage_in_real_projection(tmp_path):
    rt, _, _, _ = make_runtime(tmp_path,
        assistant(name="inspect_service", args={"service_id": "svc1"}), assistant("Done"))
    task_id = rt.create({"message": "Inspect fixture"})["id"]
    advance(rt, 2)
    assert rt.store.read()["tasks"][task_id]["tool_menu_usage"]
    rt.store.transaction(lambda data: data["tasks"][task_id].update(context_epoch=1))
    advance(rt)
    assert rt.store.read()["tasks"][task_id]["tool_menu_usage"] == {}


def test_t2_actual_owner_followup_preserves_loaded_schema(tmp_path):
    rt, model, _, _ = make_runtime(tmp_path,
        assistant(name=tool_menu.NAME, args={"load": ["read_fixture_0"]}),
        assistant(name="read_fixture_0", args={"service_id": "svc1"}))
    rt.tools = ManyTools()
    rt.update_settings({"max_context_chars": 32000})
    task_id = rt.create({"message": "Inspect fixture"})["id"]
    fit_window(rt, task_id, 32000)
    advance(rt, 2)
    old = rt.get(task_id)
    rt.add_message(task_id, {"message": "Keep the same tools and continue", "intent": "steer",
        "expected_control_revision": old["control_revision"], "expected_goal_revision": old["goal"]["revision"]})
    advance(rt)
    assert "read_fixture_0" in {s["function"]["name"] for s in model.calls[-1][2]}
