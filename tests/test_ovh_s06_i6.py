"""S06-I6: state-dependent internal catalog and versioned skill-file pinning."""
import json

import pytest
from test_agent_extensions_runtime import ext_env
from test_agent_runtime import advance, assistant, fit_window, make_runtime
from test_agent_tool_menu import ManyTools
from test_ovh_s09_i4 import _write_skill

from rynmesh.agent_runtime import subagents, tool_menu
from rynmesh.agent_runtime.store import AgentToolRejected


def chain(runtime, depth):
    root = runtime.create({"message": "Coordinate this task"})["id"]
    def spawn(data):
        row = data["tasks"][root]
        for i in range(depth):
            child = subagents.spawn_agent_in_tx(data, row, {
                "task_name": f"level_{i}", "prompt": "Inspect this task", "role": "worker", "fork_turns": "none"})
            row["status"] = "paused"
            row = data["tasks"][child["agent_id"]]
        return row["id"]
    tid = runtime.store.transaction(spawn)
    return runtime.store.read()["tasks"][tid]


@pytest.mark.parametrize("depth,spawn_visible", [(2, False), (1, True)])
def test_t1_t2_depth_controls_the_real_menu(tmp_path, depth, spawn_visible):
    runtime, _, _, _ = make_runtime(tmp_path)
    row = chain(runtime, depth)
    (specs, _), _ = runtime._menu(row, max_context_bytes=200000)
    names = {s["function"]["name"] for s in specs}
    assert ("ryn_spawn_agent" in names) is spawn_visible
    assert {"ryn_wait_agent", "ryn_send_message", "ryn_list_agents"} <= names
    assert subagents.subagent_depth(row) == depth
    assert subagents.at_max_depth(row) is (depth == subagents.MAX_SUBAGENT_DEPTH)


def test_t3_small_window_discovery_uses_the_same_filtered_catalog(tmp_path):
    runtime, model, _, _ = make_runtime(tmp_path,
        assistant(name=tool_menu.NAME, args={"load": ["inspect_service"]}, call_id="discover"),
        assistant(name="inspect_service", args={"service_id": "svc1"}, call_id="inspect"), assistant("Done."))
    runtime.tools = ManyTools()
    row = chain(runtime, 2)
    fit_window(runtime, row["id"], 12000, small_window=True)
    advance(runtime, 5)
    done = runtime.store.read()["tasks"][row["id"]]
    assert not any(e.get("result", {}).get("error") == "agent_tool_definition_changed" for e in done["events"])
    result = next(json.loads(m["content"]) for m in done["messages"] if m.get("tool_call_id") == "discover" and m["role"] == "tool")
    assert done["tool_menu"]["lazy_controls"] is True
    assert "inspect_service" in result["loaded_names"]
    assert "ryn_spawn_agent" not in {s["function"]["name"] for s in runtime._internal_specs(done)}
    assert any(call[0] == "inspect_service" for call in runtime.tools.calls)


def test_depth_helpers_keep_queued_spawn_refusal(tmp_path):
    runtime, _, _, _ = make_runtime(tmp_path)
    row = chain(runtime, 2)
    with pytest.raises(AgentToolRejected, match="agent_subagent_depth_limit_reached"):
        runtime.store.transaction(lambda d: subagents.spawn_agent_in_tx(d, d["tasks"][row["id"]], {
            "task_name": "too_deep", "prompt": "Inspect", "role": "worker"}))
    assert subagents.subagent_depth({}) == 0
    assert subagents.subagent_depth({"subagent_meta": {"agent_path": "/root/one/"}}) == 1


def test_preexisting_queued_spawn_keeps_the_depth_refusal(tmp_path):
    runtime, _, _, _ = make_runtime(tmp_path, assistant(name="ryn_spawn_agent", args={
        "task_name": "queued", "prompt": "Inspect", "role": "explorer", "fork_turns": "none"}))
    row = chain(runtime, 1)
    advance(runtime)
    queued = runtime.store.read()["tasks"][row["id"]]
    assert queued["queue"][0]["name"] == "ryn_spawn_agent"
    runtime.store.transaction(lambda d: d["tasks"][row["id"]]["subagent_meta"].update(agent_path="/root/one/two"))
    advance(runtime)
    done = runtime.store.read()["tasks"][row["id"]]
    errors = [e.get("result", {}).get("error") for e in done["events"]]
    assert "agent_subagent_depth_limit_reached" in errors
    assert "agent_tool_definition_changed" not in errors


@pytest.fixture
def skill_env(ext_env, tmp_path, monkeypatch):
    for key in ("CODEX_HOME", "RYN_HOME", "USERPROFILE", "HOME"):
        monkeypatch.setenv(key, str(tmp_path / "isolated-home"))
    runtime, _, tools, workspace, _ = ext_env
    tid = runtime.create({"message": "Use tools without selecting a skill"})["id"]
    return runtime, tools.extensions.store, tid


def pins(runtime, tid):
    row = runtime.store.read()["tasks"][tid]
    (specs, state), _ = runtime._menu(row, max_context_bytes=200000)
    pinned = set(state.get("pinned_names", []))
    assert pinned <= {s["function"]["name"] for s in specs}
    return pinned


def install_skill(store, tmp_path):
    root = tmp_path / "skill-root"
    package = root / "example"
    _write_skill(package, "example", "Test skill", "Use refs.txt")
    (package / "refs.txt").write_text("Real skill package file", encoding="utf-8")
    store.sync_discovered_skills(roots=[(root, "user")])


def test_t4_enabled_unselected_package_files_are_pinned(skill_env, tmp_path):
    runtime, store, tid = skill_env
    install_skill(store, tmp_path)
    assert runtime.store.read()["tasks"][tid]["skills"] == []
    assert "ryn_read_skill_file" in pins(runtime, tid)


def test_t5_no_package_files_does_not_pin_the_reader(skill_env):
    runtime, store, tid = skill_env
    assert store.any_enabled_skill_has_files() is False
    assert "ryn_read_skill_file" not in pins(runtime, tid)


def test_skill_cache_reuses_catalog_and_invalidates_on_revision(skill_env, tmp_path, monkeypatch):
    runtime, store, tid = skill_env
    assert store.any_enabled_skill_has_files() is False
    install_skill(store, tmp_path)
    assert store.any_enabled_skill_has_files() is True
    def no_scan(*args):
        raise AssertionError("unchanged skill catalog must not be rescanned")
    def no_disk(*args, **kwargs):
        raise AssertionError("unchanged skill catalog must not be reread from disk")
    with monkeypatch.context() as patch:
        patch.setattr(store, "_is_skill_effectively_enabled", no_scan)
        patch.setattr(store, "_read_record", no_disk)
        assert store.any_enabled_skill_has_files() is True
        assert store.any_enabled_skill_has_files() is True
    revision = next(s["revision"] for s in store.overview()["skills"] if s["id"] == "example")
    disabled = store.set_skill_enabled("example", False, expected_revision=revision)
    assert store.any_enabled_skill_has_files() is False
    assert "ryn_read_skill_file" not in pins(runtime, tid)
    store.set_skill_enabled("example", True, expected_revision=disabled["revision"])
    assert store.any_enabled_skill_has_files() is True
