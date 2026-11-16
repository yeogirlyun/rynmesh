"""Overhaul S04-I21: allow outside-workspace shell cwd in automatic permission mode."""
from __future__ import annotations

import os
from pathlib import Path

import pytest
from test_agent_local_tools import execute
from test_agent_local_tools import local as local_fixture

from rynmesh.agent_runtime.local_specs import SPECS
from rynmesh.agent_runtime.store import AgentToolRejected


@pytest.fixture
def local(tmp_path):
    yield from local_fixture.__wrapped__(tmp_path)


def _set_modes(store, *, settings_mode: str, task_mode: str | None, task_id: str = "task") -> None:
    def _tx(data):
        data["settings"]["permission_mode"] = settings_mode
        if task_id in data["tasks"]:
            data["tasks"][task_id]["permission_mode"] = task_mode

    store.transaction(_tx)


def _pwd_and_touch_cmd(marker_name: str, extra_touch: Path | None = None) -> tuple[str, str]:
    if os.name == "nt":
        parts = [
            f"Set-Content -LiteralPath '{marker_name}' -Value 'ok' -NoNewline",
            "(Get-Location).Path",
        ]
        if extra_touch is not None:
            parts.insert(1, f"Set-Content -LiteralPath '{extra_touch}' -Value 'ws' -NoNewline")
        return "powershell", "; ".join(parts)
    parts = [
        f"printf ok > '{marker_name}'",
        "pwd",
    ]
    if extra_touch is not None:
        parts.insert(1, f"printf ws > '{extra_touch}'")
    return "sh", " && ".join(parts)


def test_t1_automatic_mode_allows_outside_workspace_shell_cwd_and_snapshots_workspace_root(local, tmp_path):
    """T1: automatic 下 cwd=临时目录（工作区外）可执行，且 workspace_files 仍只看工作区根、工具说明已更新（覆盖改动 1、2、3）。"""
    tools, context, workspace, store = local

    # Requirement 3: updated ryn_shell_execute spec description
    spec_desc = SPECS["ryn_shell_execute"][0]
    assert "cwd must be inside workspace, except in automatic mode" in spec_desc
    assert "cwd must be inside workspace." not in spec_desc

    outside_dir = tmp_path / "outside_shell_cwd"
    outside_dir.mkdir()

    # Both row and settings must be automatic: missing/None or workspace on row must NOT allow outside cwd
    _set_modes(store, settings_mode="automatic", task_mode=None)
    with pytest.raises(AgentToolRejected, match="agent_local_path_outside_workspace"):
        tools.perform(
            "ryn_shell_execute",
            {"command": "pwd", "cwd": str(outside_dir)},
            context,
            lambda: False,
        )

    _set_modes(store, settings_mode="automatic", task_mode="workspace")
    with pytest.raises(AgentToolRejected, match="agent_local_path_outside_workspace"):
        tools.preview("ryn_shell_execute", {"command": "pwd", "cwd": str(outside_dir)}, context)

    _set_modes(store, settings_mode="workspace", task_mode="automatic")
    with pytest.raises(AgentToolRejected, match="agent_local_path_outside_workspace"):
        tools.preview("ryn_shell_execute", {"command": "pwd", "cwd": str(outside_dir)}, context)

    # Now set both row and settings to automatic
    _set_modes(store, settings_mode="automatic", task_mode="automatic")

    # Non-existent outside directory in automatic mode is rejected cleanly in both preview and perform
    missing_outside = tmp_path / "missing_outside_cwd"
    with pytest.raises(AgentToolRejected, match="agent_local_cwd_not_directory"):
        tools.preview("ryn_shell_execute", {"command": "pwd", "cwd": str(missing_outside)}, context)
    with pytest.raises(AgentToolRejected, match="agent_local_cwd_not_directory"):
        tools.perform("ryn_shell_execute", {"command": "pwd", "cwd": str(missing_outside)}, context, lambda: False)

    # Requirement 1 & 2: execute in existing outside directory; outside file changes do NOT pollute workspace_files
    shell_name, cmd = _pwd_and_touch_cmd("outside_created.txt")
    res = execute(
        tools,
        context,
        "ryn_shell_execute",
        {"shell": shell_name, "command": cmd, "cwd": str(outside_dir)},
        "op_s04_i21_out_1",
    )
    assert res["exit_code"] == 0
    assert (outside_dir / "outside_created.txt").read_text(encoding="utf-8") == "ok"
    printed_lines = [line.strip() for line in res["output"].splitlines() if line.strip()]
    assert Path(printed_lines[-1]).resolve() == outside_dir.resolve()
    assert res.get("workspace_files") == []

    # Requirement 2: workspace root is still snapshotted when workspace files change during an outside-cwd command
    ws_marker = workspace / "touched_from_outside_cwd.txt"
    shell_name2, cmd2 = _pwd_and_touch_cmd("outside_created_2.txt", extra_touch=ws_marker)
    res2 = tools.perform(
        "ryn_shell_execute",
        {"shell": shell_name2, "command": cmd2, "cwd": str(outside_dir)},
        context,
        lambda: False,
        operation_id="op_s04_i21_out_2",
    )
    assert res2["exit_code"] == 0
    assert ws_marker.read_text(encoding="utf-8") == "ws"
    ws_paths = {Path(item["path"] if isinstance(item, dict) else item).resolve() for item in res2.get("workspace_files", [])}
    assert ws_marker.resolve() in ws_paths
    assert (outside_dir / "outside_created_2.txt").resolve() not in ws_paths


@pytest.mark.parametrize("mode", ["workspace", "confirm_changes", "read_only"])
def test_t2_workspace_and_other_modes_reject_outside_workspace_shell_cwd(local, tmp_path, mode):
    """T2: workspace 等非 automatic 档同样参数被拒（覆盖改动 1）。"""
    tools, context, _, store = local
    _set_modes(store, settings_mode=mode, task_mode=mode)

    outside_dir = tmp_path / f"outside_blocked_{mode}"
    outside_dir.mkdir()
    shell_name, cmd = _pwd_and_touch_cmd("blocked.txt")

    with pytest.raises(AgentToolRejected) as prev_exc:
        tools.preview("ryn_shell_execute", {"shell": shell_name, "command": cmd, "cwd": str(outside_dir)}, context)
    assert str(prev_exc.value) == "agent_local_path_outside_workspace"

    with pytest.raises(AgentToolRejected) as perf_exc:
        tools.perform(
            "ryn_shell_execute",
            {"shell": shell_name, "command": cmd, "cwd": str(outside_dir)},
            context,
            lambda: False,
        )
    assert str(perf_exc.value) == "agent_local_path_outside_workspace"
    assert not (outside_dir / "blocked.txt").exists()


def test_t3_relative_cwd_still_resolves_against_workspace_root(local, tmp_path):
    """T3: 相对 cwd 仍按工作区解析，且相对路径 .. 越界即使在 automatic 下仍被拒（覆盖改动 1）。"""
    tools, context, workspace, store = local
    subdir = workspace / "sub" / "inner"
    subdir.mkdir(parents=True)
    outside_sibling = tmp_path / "outside_sibling"
    outside_sibling.mkdir()

    for idx, mode in enumerate(("workspace", "automatic")):
        _set_modes(store, settings_mode=mode, task_mode=mode)
        marker = f"rel_{mode}.txt"
        shell_name, cmd = _pwd_and_touch_cmd(marker)
        res = execute(
            tools,
            context,
            "ryn_shell_execute",
            {"shell": shell_name, "command": cmd, "cwd": "sub/inner"},
            f"op_s04_i21_rel_{idx}",
        )
        assert res["exit_code"] == 0
        assert (subdir / marker).read_text(encoding="utf-8") == "ok"
        printed_lines = [line.strip() for line in res["output"].splitlines() if line.strip()]
        assert Path(printed_lines[-1]).resolve() == subdir.resolve()

    # Relative ../ escape must still be rejected even in automatic mode
    _set_modes(store, settings_mode="automatic", task_mode="automatic")
    with pytest.raises(AgentToolRejected, match="agent_local_path_outside_workspace"):
        tools.preview("ryn_shell_execute", {"command": "pwd", "cwd": "../outside_sibling"}, context)
    with pytest.raises(AgentToolRejected, match="agent_local_path_outside_workspace"):
        tools.perform("ryn_shell_execute", {"command": "pwd", "cwd": "../outside_sibling"}, context, lambda: False)
