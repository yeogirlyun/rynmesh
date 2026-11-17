"""S06-I24: unavailable CodeMode and count-bounded real request menus."""
import copy
from types import SimpleNamespace

import pytest
from test_agent_integrations import node as node
from test_agent_runtime import advance, assistant, make_runtime

from rynmesh.agent_runtime import code_mode, tool_menu
from rynmesh.agent_runtime.bridge import build_agent_integrations
from rynmesh.agent_runtime.store import AgentToolRejected
from rynmesh.llm_package.chat import validate_chat


def small_specs(count, prefix="t"):
    return [{"type": "function", "function": {"name": f"{prefix}{n}", "parameters": {}}} for n in range(count)]


@pytest.fixture
def node_probe(monkeypatch):
    clock, calls, current = [1000.0], [], [None]
    monkeypatch.setattr(code_mode, "time", SimpleNamespace(monotonic=lambda: clock[0]))
    monkeypatch.setattr(code_mode, "_NODE_AVAILABILITY_CACHE", (0, None), raising=False)
    def resolve():
        calls.append(clock[0])
        if current[0] is None:
            raise AgentToolRejected("agent_code_exec_node_unavailable")
        return current[0]
    monkeypatch.setattr(code_mode, "_resolve_node_binary", resolve)
    return clock, calls, current


def project(specs, state=None, **kwargs):
    return tool_menu.project(specs, [], state, max_context_bytes=6000000, authority_bytes=0,
                             epoch=0, goal_revision=1, **kwargs)


def test_T1_real_registry_and_capability_catalog_hide_unavailable_code_mode(node, tmp_path, node_probe):
    app, store = node
    _, tools, _ = build_agent_integrations(app, store, store.home)
    runtime, _, _, _ = make_runtime(tmp_path / "agent")
    runtime.tools = tools
    try:
        specs = tools.specs()
        assert code_mode.NAME not in [s["function"]["name"] for s in specs]
        assert tools.spec(code_mode.NAME) is None and tools.metadata(code_mode.NAME) is None
        exposed, state = project(specs, initial_names=[code_mode.NAME], pinned_names=[code_mode.NAME])
        assert code_mode.NAME not in [s["function"]["name"] for s in exposed]
        assert code_mode.NAME not in state.get("pinned_names", []) + state["loaded_names"]
        entry = next(item for item in runtime.tool_catalog() if item["name"] == code_mode.NAME)
        assert entry["available"] is False and entry["reason"] == "node_missing"
        assert len(node_probe[1]) == 1
        node_probe[2][0] = "/node/restored"
        node_probe[0][0] += 60
        assert code_mode.NAME in [s["function"]["name"] for s in tools.specs()]
        assert tools.spec(code_mode.NAME) is not None and tools.metadata(code_mode.NAME)["effect"] == "read"
        restored = [item for item in runtime.tool_catalog() if item["name"] == code_mode.NAME]
        assert len(restored) == 1 and restored[0].get("available", True)
        assert len(node_probe[1]) == 2
    finally:
        tools.local.processes.close()
        tools.close()


def test_T2_cached_missing_and_present_results_expire_after_sixty_seconds(node_probe):
    clock, calls, current = node_probe
    assert code_mode.node_available() is None
    current[0] = "/node/restored"
    clock[0] += 59
    assert code_mode.node_available() is None and len(calls) == 1
    clock[0] += 1
    assert code_mode.node_available() == current[0] and len(calls) == 2
    current[0] = None
    clock[0] += 59
    assert code_mode.node_available() == "/node/restored" and len(calls) == 2
    clock[0] += 1
    assert code_mode.node_available() is None and len(calls) == 3


def test_T2_unrelated_probe_rejection_is_not_disguised_as_missing_node(node_probe, monkeypatch):
    def broken():
        raise AgentToolRejected("agent_scope_invalid")
    monkeypatch.setattr(code_mode, "_resolve_node_binary", broken)
    with pytest.raises(AgentToolRejected, match="agent_scope_invalid"):
        code_mode.node_available()


def test_T3_stale_loaded_name_is_removed_and_revision_changes_without_throwing():
    specs = small_specs(200)
    _, prior = project(specs)
    prior["loaded_names"] = ["t1", code_mode.NAME]
    exposed, state = project(specs, prior)
    assert state["loaded_names"] == ["t1"]
    assert state["revision"] > prior["revision"]
    assert code_mode.NAME not in [s["function"]["name"] for s in exposed]
    assert prior["loaded_names"] == ["t1", code_mode.NAME]


def test_T3_real_model_turn_records_removed_tool_and_excludes_it(node, tmp_path, monkeypatch):
    app, store = node
    _, tools, _ = build_agent_integrations(app, store, store.home)
    runtime, model, _, _ = make_runtime(tmp_path / "agent", assistant("Ready to continue."))
    runtime.tools = tools
    current = ["/node/initial"]
    monkeypatch.setattr(code_mode, "node_available", lambda: current[0])
    task = runtime.create({"message": "Continue working"})
    old_row = runtime.store.read()["tasks"][task["id"]]
    (old_specs, old_state), _ = runtime._menu(old_row)
    assert code_mode.NAME in [s["function"]["name"] for s in old_specs]
    def old_menu(data):
        row = data["tasks"][task["id"]]
        row["tool_menu"] = copy.deepcopy(old_state)
        row["tool_menu"]["loaded_names"] = [code_mode.NAME]
    runtime.store.transaction(old_menu)
    current[0] = None
    try:
        advance(runtime)
        row = runtime.store.read()["tasks"][task["id"]]
        assert code_mode.NAME not in [s["function"]["name"] for s in model.calls[0][2]]
        assert row["tool_menu_notice"]["removed"] == [code_mode.NAME]
        assert code_mode.NAME in str(model.calls[0][1]) and "removed" in str(model.calls[0][1])
        assert any(e["type"] == "tool_menu_changed" for e in row["events"])
    finally:
        tools.local.processes.close()
        tools.close()


@pytest.mark.parametrize("mode", ["full", "loaded", "pinned", "internal"])
def test_T4_request_tool_count_is_bounded_and_oldest_loads_are_released(mode):
    specs = small_specs(121 if mode == "full" else 200)
    names = [s["function"]["name"] for s in specs]
    if mode == "internal":
        exposed, state = tool_menu.project([], specs, None, max_context_bytes=6000000,
                                           authority_bytes=0, epoch=0, goal_revision=1)
    elif mode == "pinned":
        exposed, state = project(specs, pinned_names=names, initial_names=names)
    elif mode == "loaded":
        _, prior = project(specs)
        prior["loaded_names"] = copy.deepcopy(names)
        exposed, state = project(specs, prior)
        assert state["loaded_names"] == names[-119:]
    else:
        exposed, state = project(specs)
    assert state["mode"] == "directory"
    assert len(exposed) <= 120
    assert len({s["function"]["name"] for s in exposed}) == len(exposed)
    validate_chat({"messages": [{"role": "user", "content": "Hello"}], "tools": exposed})
