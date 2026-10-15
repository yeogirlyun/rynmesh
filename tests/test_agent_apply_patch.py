"""Codex-style patches and always-available core tools."""
import copy

import pytest
from test_agent_local_tools import execute
from test_agent_local_tools import local as local  # noqa: F401  (fixture)

from rynmesh.agent_runtime import local_patch, tool_menu
from rynmesh.agent_runtime.execution_policy import decide
from rynmesh.agent_runtime.local_specs import SPECS
from rynmesh.agent_runtime.store import AgentToolRejected, encoded


def patch(*lines):
    return "\n".join(["*** Begin Patch", *lines, "*** End Patch"])


def test_hunks_keep_crlf_final_newline_and_follow_anchor():
    text = "def a():\r\n    return 1\r\n\r\ndef b():\r\n    return 1\r\n"
    [operation] = local_patch.parse(patch("*** Update File: x.py", "@@ def b():", "-    return 1", "+    return 2"))
    assert local_patch.apply_hunks(text, operation["hunks"]) == "def a():\r\n    return 1\r\n\r\ndef b():\r\n    return 2\r\n"


def test_trailing_whitespace_fallback_eof_and_pure_addition():
    [operation] = local_patch.parse(patch("*** Update File: x", "@@", " one", "-two", "+TWO", "@@", "+four", "*** End of File"))
    assert local_patch.apply_hunks("one  \ntwo\nthree", operation["hunks"]) == "one  \nTWO\nthree\nfour"
    with pytest.raises(AgentToolRejected, match="agent_patch_context_not_found"):
        local_patch.apply_hunks("alpha\n", local_patch.parse(patch("*** Update File: x", "-beta", "+gamma"))[0]["hunks"])


@pytest.mark.parametrize("text,code", [("no envelope", "invalid_envelope"),
    (patch("*** Add File: a", "missing plus"), "invalid_add_line"),
    (patch("*** Update File: a"), "empty_update"), (patch("*** Frobnicate: a"), "invalid_operation")])
def test_malformed_patches_are_rejected(text, code):
    with pytest.raises(AgentToolRejected, match="agent_patch_" + code):
        local_patch.parse(text)


def test_one_patch_adds_updates_moves_and_deletes(local):  # noqa: F811
    tools, context, workspace, _ = local
    (workspace / "app.py").write_bytes(b"import os\r\n\r\nVALUE = 1\r\n")
    (workspace / "old.txt").write_text("keep\n", encoding="utf-8")
    (workspace / "gone.txt").write_text("bye\n", encoding="utf-8")
    result = execute(tools, context, "ryn_apply_patch", {"patch": patch(
        "*** Add File: pkg/new.py", "+print('hi')",
        "*** Update File: app.py", " import os", " ", "-VALUE = 1", "+VALUE = 2",
        "*** Update File: old.txt", "*** Move to: moved/old.txt",
        "*** Delete File: gone.txt")})
    assert result["applied"] and [item["action"] for item in result["files"]] == ["added", "updated", "moved", "deleted"]
    assert (workspace / "pkg/new.py").read_text(encoding="utf-8") == "print('hi')\n"
    assert (workspace / "app.py").read_bytes() == b"import os\r\n\r\nVALUE = 2\r\n"
    assert (workspace / "moved/old.txt").read_text(encoding="utf-8") == "keep\n" and not (workspace / "old.txt").exists()
    assert not (workspace / "gone.txt").exists()
    read = tools.perform("ryn_file_read", {"path": "app.py"}, context, lambda: False)
    assert read["sha256"] == result["files"][1]["sha256"]
    review = tools.change_review("task", str(workspace))
    assert {item["path"]: item["status"] for item in review["files"]}["app.py"] == "modified"


def test_a_hunk_that_does_not_match_writes_nothing(local):  # noqa: F811
    tools, context, workspace, _ = local
    (workspace / "a.txt").write_text("a\n", encoding="utf-8")
    with pytest.raises(AgentToolRejected, match="agent_patch_context_not_found"):
        tools.preview("ryn_apply_patch", {"patch": patch("*** Add File: b.txt", "+b",
                                                         "*** Update File: a.txt", "-missing", "+x")}, context)
    assert not (workspace / "b.txt").exists() and (workspace / "a.txt").read_text(encoding="utf-8") == "a\n"


@pytest.mark.parametrize("lines,code", [(["*** Add File: ../escape.txt", "+x"], "agent_local_path_outside_workspace"),
    (["*** Add File: a.txt", "+x"], "agent_patch_file_exists"),
    (["*** Update File: a.txt", "-a", "+b", "*** Delete File: a.txt"], "agent_patch_duplicate_path")])
def test_unsafe_or_ambiguous_targets_are_refused(local, lines, code):  # noqa: F811
    tools, context, workspace, _ = local
    (workspace / "a.txt").write_text("a\n", encoding="utf-8")
    with pytest.raises(AgentToolRejected, match=code):
        tools.preview("ryn_apply_patch", {"patch": patch(*lines)}, context)


def test_preview_stays_bounded_and_detects_a_changed_file(local):  # noqa: F811
    tools, context, workspace, _ = local
    big = patch("*** Add File: big.txt", *("+" + "x" * 90 for _ in range(800)))
    preview = tools.preview("ryn_apply_patch", {"patch": big}, context)
    assert len(encoded(preview)) < 16000 and preview["operation"]["patch_bytes"] > 70000
    (workspace / "a.txt").write_text("a\n", encoding="utf-8")
    args = {"patch": patch("*** Update File: a.txt", "-a", "+b")}
    stale = tools.preview("ryn_apply_patch", args, context)
    (workspace / "a.txt").write_text("a\nchanged elsewhere\n", encoding="utf-8")
    with pytest.raises(AgentToolRejected, match="agent_approval_preview_changed"):
        tools.execute("ryn_apply_patch", args, operation_id="op", context=context, expected_preview=stale, cancelled=lambda: False)


def test_workspace_mode_runs_patches_without_a_dialog():
    call = {"name": "ryn_apply_patch", "arguments": {"patch": "x"}}
    row = {"permission_mode": "workspace"}
    assert decide(call, {"effect": "write"}, row, {"permission_mode": "workspace"}).action == "execute"
    assert decide(call, {"effect": "write"}, {"permission_mode": "confirm_changes"}, {"permission_mode": "workspace"}).action == "ask"


def specs(names):
    return [{"type": "function", "function": {"name": name, "description": SPECS[name][0], "parameters": SPECS[name][3]}}
            for name in names]


def test_core_tools_stay_exposed_and_loads_accumulate():
    business = specs(SPECS)
    exposed, state = tool_menu.project(business, [], {}, max_context_bytes=120000, authority_bytes=4000,
                                       epoch=0, goal_revision=1, pinned_names=tool_menu.CORE_TOOLS)
    names = [spec["function"]["name"] for spec in exposed]
    assert state["mode"] == "directory" and set(tool_menu.CORE_TOOLS) <= set(names)
    assert state["loaded_names"] == [] and state["pinned_names"] == list(tool_menu.CORE_TOOLS)
    capacity = tool_menu.capacity(120000, 4000)
    result, state = tool_menu.discover(business, [], lambda _: {"effect": "read"}, state,
                                       {"load": ["ryn_browser_open", "ryn_file_read"]}, capacity_bytes=capacity)
    assert state["loaded_names"] == ["ryn_browser_open"] and result["always_available"] == list(tool_menu.CORE_TOOLS)
    _, state = tool_menu.discover(business, [], lambda _: {"effect": "read"}, state, {"load": ["ryn_browser_read"]}, capacity_bytes=capacity)
    assert state["loaded_names"] == ["ryn_browser_open", "ryn_browser_read"]
    _, state = tool_menu.discover(business, [], lambda _: {"effect": "read"}, state, {"unload": ["ryn_browser_open"]}, capacity_bytes=capacity)
    assert state["loaded_names"] == ["ryn_browser_read"]
    exposed, state = tool_menu.project(business, [], state, max_context_bytes=120000, authority_bytes=4000,
                                       epoch=0, goal_revision=1, pinned_names=tool_menu.CORE_TOOLS)
    assert {"ryn_browser_read", *tool_menu.CORE_TOOLS} <= {spec["function"]["name"] for spec in exposed}


def test_loading_past_capacity_releases_the_oldest_load_first(monkeypatch):
    monkeypatch.setattr(tool_menu, "ADDITIVE_CAPACITY", 0)
    business = specs(SPECS)
    _, state = tool_menu.project(business, [], {}, max_context_bytes=120000, authority_bytes=4000,
                                 epoch=0, goal_revision=1, pinned_names=tool_menu.CORE_TOOLS)
    base = len(encoded([tool_menu.directory_spec(business), *specs(tool_menu.CORE_TOOLS)]))
    tight = base + len(encoded(specs(["ryn_browser_read"]))) + len(encoded(specs(["ryn_browser_action"]))) + 8
    _, state = tool_menu.discover(business, [], lambda _: {"effect": "read"}, state, {"load": ["ryn_browser_read"]}, capacity_bytes=tight)
    room = base + len(encoded(specs(["ryn_browser_action"]))) + len(encoded(specs(["ryn_render_inspect"]))) + 8
    _, state = tool_menu.discover(business, [], lambda _: {"effect": "read"}, copy.deepcopy(state),
                                  {"load": ["ryn_browser_action", "ryn_render_inspect"]}, capacity_bytes=room)
    assert state["loaded_names"] == ["ryn_browser_action", "ryn_render_inspect"]
    with pytest.raises(AgentToolRejected, match="agent_tool_schema_limit"):
        tool_menu.discover(business, [], lambda _: {"effect": "read"}, state,
                           {"load": ["ryn_browser_action", "ryn_render_inspect", "ryn_computer_action"]}, capacity_bytes=tight)


def test_small_windows_replace_loads_instead_of_accumulating():
    business = specs(SPECS)
    _, state = tool_menu.project(business, [], {}, max_context_bytes=12000, authority_bytes=1000, epoch=0, goal_revision=1,
                                 pinned_names=tool_menu.CORE_TOOLS)
    assert not state.get("pinned_names")  # no room for pins in a 12 KB window
    capacity = tool_menu.capacity(12000, 1000)
    _, state = tool_menu.discover(business, [], lambda _: {"effect": "read"}, state, {"load": ["ryn_file_read"]}, capacity_bytes=capacity)
    _, state = tool_menu.discover(business, [], lambda _: {"effect": "read"}, state, {"load": ["ryn_file_list"]}, capacity_bytes=capacity)
    assert state["loaded_names"] == ["ryn_file_list"]
