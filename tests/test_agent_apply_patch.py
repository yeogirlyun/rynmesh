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


def test_duplicate_anchor_with_unique_context_succeeds_and_duplicate_full_context_is_rejected(local):  # noqa: F811
    tools, context, workspace, _ = local
    content = (
        "class Alpha:\n"
        "    def run(self):\n"
        "        return 1\n\n"
        "class Beta:\n"
        "    def run(self):\n"
        "        return 2\n"
    )
    target = workspace / "service.py"
    target.write_text(content, encoding="utf-8")
    result = execute(tools, context, "ryn_apply_patch", {"patch": patch(
        "*** Update File: service.py",
        "@@ def run(self):",
        "-        return 2",
        "+        return 20",
    )})
    assert result["applied"]
    assert "return 1\n" in target.read_text(encoding="utf-8")
    assert "return 20\n" in target.read_text(encoding="utf-8")

    ambiguous_content = (
        "class Alpha:\n"
        "    def run(self):\n"
        "        return 1\n\n"
        "class Beta:\n"
        "    def run(self):\n"
        "        return 1\n"
    )
    ambiguous_target = workspace / "service_ambiguous.py"
    ambiguous_target.write_text(ambiguous_content, encoding="utf-8")
    with pytest.raises(AgentToolRejected, match="agent_patch_context_not_found") as exc_info:
        tools.preview("ryn_apply_patch", {"patch": patch(
            "*** Update File: service_ambiguous.py",
            "@@ def run(self):",
            "-        return 1",
            "+        return 10",
        )}, context)
    assert exc_info.value.details["match_count"] == 2
    assert exc_info.value.details["hunk_index"] == 0
    assert ambiguous_target.read_text(encoding="utf-8") == ambiguous_content


def test_out_of_order_hunks_succeed_and_overlapping_hunks_are_rejected_without_writes(local):  # noqa: F811
    tools, context, workspace, _ = local
    original = "line1\nline2\nline3\nline4\nline5\nline6\n"
    target = workspace / "ordered.txt"
    target.write_text(original, encoding="utf-8")
    # Hunk 0 edits near the bottom; Hunk 1 edits near the top.
    result = execute(tools, context, "ryn_apply_patch", {"patch": patch(
        "*** Update File: ordered.txt",
        "@@",
        " line5",
        "-line6",
        "+LINE6",
        "@@",
        " line1",
        "-line2",
        "+LINE2",
    )})
    assert result["applied"]
    assert target.read_text(encoding="utf-8") == "line1\nLINE2\nline3\nline4\nline5\nLINE6\n"

    # Overlapping hunks on the original snapshot must be refused with zero disk writes.
    overlap_target = workspace / "overlap.txt"
    overlap_target.write_text(original, encoding="utf-8")
    with pytest.raises(AgentToolRejected, match="agent_patch_context_not_found") as exc_info:
        execute(tools, context, "ryn_apply_patch", {"patch": patch(
            "*** Update File: overlap.txt",
            "@@",
            " line2",
            "-line3",
            "+LINE3_A",
            " line4",
            "@@",
            " line3",
            "-line4",
            "+LINE4_B",
        )})
    assert exc_info.value.details["reason"] == "overlapping_hunks"
    assert overlap_target.read_text(encoding="utf-8") == original


def test_patch_preserves_added_indentation_verbatim_and_reports_bounded_diagnostics(local):  # noqa: F811
    tools, context, workspace, _ = local
    target = workspace / "indent.py"
    target.write_text("def calc():\n    x = 1\n    return x\n", encoding="utf-8")
    execute(tools, context, "ryn_apply_patch", {"patch": patch(
        "*** Update File: indent.py",
        "@@ def calc():",
        "     x = 1",
        "+      # intentionally 6-space indented comment",
        "     return x",
    )})
    assert "      # intentionally 6-space indented comment\n" in target.read_text(encoding="utf-8")

    with pytest.raises(AgentToolRejected, match="agent_patch_context_not_found") as exc_info:
        tools.preview("ryn_apply_patch", {"patch": patch(
            "*** Update File: indent.py",
            "@@ def calc():",
            "-  x = 1",
            "+  x = 2",
        )}, context)
    assert exc_info.value.correction
    assert exc_info.value.details["path"] == "indent.py"
    assert exc_info.value.details["match_count"] == 0
    assert "    x = 1" in exc_info.value.details["snippet"]
    assert len(exc_info.value.details["snippet"]) <= 1600


def test_observed_file_version_tracks_reads_detects_external_changes_and_survives_restart(tmp_path):
    from types import SimpleNamespace

    from test_agent_runtime import advance, assistant, make_runtime

    from rynmesh.agent_runtime.local_tools import LocalTools
    from rynmesh.agent_runtime.operation_ledger import OperationLedger
    from rynmesh.agent_runtime.runtime import AgentRuntime
    from rynmesh.agent_runtime.store import AgentStore

    workspace = tmp_path / "ws"
    workspace.mkdir()
    (workspace / "notes.txt").write_text("alpha\nbeta\n", encoding="utf-8")

    class FullLocalTools:
        names = ("ryn_file_read", "ryn_file_edit", "ryn_apply_patch", "ryn_file_write")

        def __init__(self, store, home):
            ledger = OperationLedger(home)
            self.local = LocalTools(SimpleNamespace(home=home), lambda: ledger)
            self.local.store = store

        def specs(self):
            return specs(self.names)

        def metadata(self, name):
            effect = SPECS[name][1]
            return {"effect": effect, "scope_context_supported": True, "preview_precondition": effect == "write"}

        def preview(self, name, args, *, context):
            return self.local.preview(name, args, context)

        def execute(self, name, args, *, operation_id, cancelled, context, expected_preview=None):
            return self.local.execute(name, args, operation_id=operation_id, cancelled=cancelled,
                                      context=context, expected_preview=expected_preview)

    # 1. Read file, then edit without expected_sha256 -> succeeds using observed_files.
    # 2. External modification occurs before next edit -> rejected with agent_local_file_changed_read_again,
    #    then model re-reads and applies patch.
    def external_modify_then_edit(call_index, messages, _specs):
        if call_index == 2:
            (workspace / "notes.txt").write_text("alpha\nEXTERNAL\n", encoding="utf-8")
            return assistant(name="ryn_file_edit", args={"path": "notes.txt", "old_text": "GAMMA\n", "new_text": "DELTA\n"})
        if call_index == 3:
            last_tool = json.loads(messages[-1]["content"])
            assert last_tool["error"] == "agent_local_file_changed_read_again"
            assert "correction" in last_tool
            return assistant(name="ryn_file_read", args={"path": "notes.txt"})
        if call_index == 4:
            return assistant(name="ryn_apply_patch", args={"patch": patch(
                "*** Update File: notes.txt",
                " alpha",
                "-EXTERNAL",
                "+FINAL",
            )})
        return assistant("Updated notes.txt.")

    import json
    runtime, model, _, key = make_runtime(
        tmp_path / "store",
        assistant(name="ryn_file_read", args={"path": "notes.txt"}),
        assistant(name="ryn_file_edit", args={"path": "notes.txt", "old_text": "beta\n", "new_text": "GAMMA\n"}),
    )
    runtime.tools = FullLocalTools(runtime.store, tmp_path / "store")
    runtime.update_settings({"workspace_root": str(workspace), "permission_mode": "workspace"})
    task = runtime.create({"message": "Update notes.txt"})
    advance(runtime, 4)
    assert (workspace / "notes.txt").read_text(encoding="utf-8") == "alpha\nGAMMA\n"

    # Restart runtime and verify observed_files persisted in task state.
    restored_store = AgentStore(tmp_path / "store", key)
    calls = {"idx": 2}
    def dynamic_complete(_model_id, messages, tool_specs, _cancelled, _on_event=None, **_kwargs):
        idx = calls["idx"]
        calls["idx"] += 1
        return external_modify_then_edit(idx, messages, tool_specs)
    model.complete = dynamic_complete
    restored = AgentRuntime(restored_store, model, FullLocalTools(restored_store, tmp_path / "store"), runtime.catalog)
    restored.reconcile()
    advance(restored, 8)
    done = restored.get(task["id"])
    assert done["status"] == "completed", done.get("error")
    assert (workspace / "notes.txt").read_text(encoding="utf-8") == "alpha\nFINAL\n"

    # Changing workspace_root clears observed_files on active tasks.
    other_ws = tmp_path / "ws2"
    other_ws.mkdir()
    task2 = restored.create({"message": "Second task"})
    restored.store.transaction(lambda data: data["tasks"][task2["id"]]["observed_files"].update({"dummy": "123"}))
    restored.update_settings({"workspace_root": str(other_ws)})
    assert restored.store.read()["tasks"][task2["id"]]["observed_files"] == {}


def test_large_apply_patch_exceeds_max_output_chars_and_executes_end_to_end(tmp_path):
    from types import SimpleNamespace

    from test_agent_runtime import advance, assistant, make_runtime

    from rynmesh.agent_runtime.local_tools import LocalTools
    from rynmesh.agent_runtime.operation_ledger import OperationLedger

    workspace = tmp_path / "ws"
    workspace.mkdir()
    big_patch = patch("*** Add File: large_module.py", *("+" + f"LINE_{i:04d} = '" + ("x" * 70) + "'" for i in range(420)))
    assert len(big_patch.encode("utf-8")) > 32000

    class PatchTools:
        def __init__(self, store, home):
            ledger = OperationLedger(home)
            self.local = LocalTools(SimpleNamespace(home=home), lambda: ledger)
            self.local.store = store

        def specs(self):
            return specs(("ryn_apply_patch", "ryn_file_read"))

        def metadata(self, name):
            effect = SPECS[name][1]
            return {"effect": effect, "scope_context_supported": True, "preview_precondition": effect == "write"}

        def preview(self, name, args, *, context):
            return self.local.preview(name, args, context)

        def execute(self, name, args, *, operation_id, cancelled, context, expected_preview=None):
            return self.local.execute(name, args, operation_id=operation_id, cancelled=cancelled,
                                      context=context, expected_preview=expected_preview)

    runtime, _, _, _ = make_runtime(
        tmp_path / "store",
        assistant(name="ryn_apply_patch", args={"patch": big_patch}),
        assistant("Created large_module.py."),
    )
    runtime.tools = PatchTools(runtime.store, tmp_path / "store")
    runtime.update_settings({"workspace_root": str(workspace), "permission_mode": "workspace", "max_output_chars": 10000})
    task = runtime.create({"message": "Write large_module.py"})
    advance(runtime, 4)
    done = runtime.get(task["id"])
    assert done["status"] == "completed", done.get("error")
    assert (workspace / "large_module.py").exists()
    assert (workspace / "large_module.py").stat().st_size > 30000


def test_file_search_regex_case_sensitive_file_pattern_and_invalid_regex_diagnostics(local):  # noqa: F811
    from pathlib import Path
    tools, context, workspace, _ = local
    (workspace / "alpha.py").write_text("def ComputeBalance():\n    return 42\n", encoding="utf-8")
    (workspace / "notes.md").write_text("computebalance in markdown\n", encoding="utf-8")

    # Case-insensitive regex filtered to *.py
    res = tools.perform("ryn_file_search", {
        "path": ".",
        "query": r"def\s+computebalance",
        "regex": True,
        "case_sensitive": False,
        "file_pattern": "*.py",
    }, context, lambda: False)
    assert len(res["matches"]) == 1
    assert Path(res["matches"][0]["path"]).name == "alpha.py"
    assert res["incomplete"] is False

    # Case-sensitive literal query should not match lowercase
    res_case = tools.perform("ryn_file_search", {
        "path": ".",
        "query": "computebalance",
        "case_sensitive": True,
        "file_pattern": "*.py",
    }, context, lambda: False)
    assert res_case["matches"] == []

    # Invalid regex reports agent_invalid_search_regex with correction and details
    with pytest.raises(AgentToolRejected, match="agent_invalid_search_regex") as exc_info:
        tools.perform("ryn_file_search", {
            "path": ".",
            "query": "[unclosed",
            "regex": True,
        }, context, lambda: False)
    assert exc_info.value.correction
    assert exc_info.value.details["query"] == "[unclosed"


def test_resolve_rg_executable_prefers_bundled_binary_and_falls_back_to_path(tmp_path, monkeypatch):
    import os

    from rynmesh.agent_runtime import local_processes

    fake_bin_dir = tmp_path / "bundled_bin"
    fake_bin_dir.mkdir()
    rg_name = "rg.exe" if os.name == "nt" else "rg"
    fake_rg = fake_bin_dir / rg_name
    fake_rg.write_bytes(b"MZ" if os.name == "nt" else b"#!/bin/sh\n")
    if os.name != "nt":
        fake_rg.chmod(0o755)

    monkeypatch.setattr(local_processes, "_rg_candidate_dirs", lambda: [fake_bin_dir])
    assert local_processes.resolve_rg_executable() == str(fake_rg)
    cmd = local_processes.literal_read_command({"command": "rg -n foo bar.txt"})
    assert cmd[0] == str(fake_rg) and "--no-config" in cmd
    env = local_processes.child_environment()
    assert env["PATH"].split(os.pathsep)[0] == str(fake_bin_dir)

    # Fallback to shutil.which when no bundled binary exists
    monkeypatch.setattr(local_processes, "_rg_candidate_dirs", lambda: [tmp_path / "empty_bin"])
    monkeypatch.setattr(local_processes.shutil, "which", lambda name: "/usr/bin/rg" if name in {"rg", "rg.exe"} else None)
    assert local_processes.resolve_rg_executable() == "/usr/bin/rg"


def test_core_tools_remain_pinned_with_internal_specs_in_moderate_additive_windows():
    from rynmesh.agent_runtime.prompt import INTERNAL_SPECS
    business = specs(SPECS)
    pinned_request = tool_menu.PLUGIN_DISCOVERY_TOOLS + ("ryn_project_inspect",) + tool_menu.CORE_TOOLS
    exposed, state = tool_menu.project(
        business,
        INTERNAL_SPECS,
        {},
        max_context_bytes=60000,
        authority_bytes=4500,
        epoch=0,
        goal_revision=1,
        pinned_names=pinned_request,
    )
    exposed_names = {spec["function"]["name"] for spec in exposed}
    assert set(tool_menu.CORE_TOOLS) <= set(state["pinned_names"])
    assert set(tool_menu.CORE_TOOLS) <= exposed_names


def test_codex_thread_reuses_session_across_tool_execution_permission_updates_tool_loading_and_followup():
    from rynmesh.agent_runtime.prompt import PROMPT_VERSION, SYSTEM, authority
    from rynmesh.llm_package import codex_threads

    class DummyRpc:
        def alive(self):
            return True

        def settle(self):
            return True

        def cpu_seconds(self, busy):
            return busy

        def close(self):
            pass

    def make_row(goal_rev=1, receipts=()):
        return {
            "id": "agent_task_1",
            "prompt_version": PROMPT_VERSION,
            "response_language": "zh-CN",
            "goal": {"revision": goal_rev, "original_message_id": "m1", "constraints": [{"message_id": "m1"}]},
            "criteria": [],
            "plan": {"revision": 0, "steps": []},
            "model_id": "local/codex",
            "scope_revision": 0,
            "local_execution": {"workspace_root": "/tmp/ws", "desktop_access": False},
            "permission_mode": "workspace",
            "allowed_write_tools": [],
            "receipts": list(receipts),
        }

    pool = codex_threads.Pool()
    key = ("codex", "gpt-5.4", "medium", "local_owner", codex_threads.window_key("agent_task_1"))
    funcs_v1 = {spec["function"]["name"]: spec for spec in specs(tool_menu.CORE_TOOLS)}

    row1 = make_row(goal_rev=1)
    sys1 = SYSTEM + authority(row1)
    msgs1 = [{"role": "system", "content": sys1}, {"role": "user", "content": "Inspect app.py"}]
    conv1 = codex_threads.Conversation(msgs1, funcs_v1, scope={"requester": "local_owner", "window": key[-1]})
    session = pool.session(DummyRpc(), key)
    conv1.session = session
    assistant_call = {"role": "assistant", "content": "", "tool_calls": [
        {"id": "call_1", "type": "function", "function": {"name": "ryn_shell_execute", "arguments": '{"command":"rg foo app.py"}'}}
    ]}
    conv1.finish(assistant_call)

    # Turn 2: Tool executed, operation_permissions updated in Server control state, plus dynamic tool loaded.
    row2 = make_row(goal_rev=1, receipts=[{
        "tool": "ryn_shell_execute", "effect": "write", "status": "confirmed", "authorization": "read",
        "operation_id": "op1", "arguments": {"command": "rg foo app.py"}, "arguments_sha256": "abc",
        "goal_revision": 1, "scope_revision": 0, "context_epoch": 0,
    }])
    sys2 = SYSTEM + authority(row2)
    msgs2 = [
        {"role": "system", "content": sys2},
        {"role": "user", "content": "Inspect app.py"},
        assistant_call,
        {"role": "tool", "tool_call_id": "call_1", "content": '{"exit_code":0,"output":"x = 1"}'},
    ]
    funcs_v2 = {**funcs_v1, "ryn_browser_open": specs(["ryn_browser_open"])[0]}
    conv2 = codex_threads.Conversation(msgs2, funcs_v2, scope={"requester": "local_owner", "window": key[-1]})
    leased = pool.lease(key, conv2)
    assert leased is session
    assert conv2.update is not None
    assert "system_state_updates" in conv2.update
    assert "functions_update" in conv2.update
    assert [f["function"]["name"] for f in conv2.update["functions_update"]["added_or_replaced"]] == ["ryn_browser_open"]
    conv2.session = leased
    final_msg = {"role": "assistant", "content": "app.py sets x = 1."}
    conv2.finish(final_msg)

    # Turn 3: Same-window owner follow-up message increments goal_revision and reuses the session.
    row3 = make_row(goal_rev=2, receipts=row2["receipts"])
    _ = SYSTEM + authority(row3)
    msgs3 = [*msgs2, final_msg, {"role": "user", "content": "Now change x to 2."}]
    conv3 = codex_threads.Conversation(msgs3, funcs_v2, scope={"requester": "local_owner", "window": key[-1]})
    leased3 = pool.lease(key, conv3)
    assert leased3 is session
    assert conv3.update["messages"] == [{"role": "user", "content": "Now change x to 2."}]
