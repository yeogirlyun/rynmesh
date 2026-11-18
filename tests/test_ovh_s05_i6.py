"""Overhaul S05-I6: automatic mode outside-workspace file tool access, ~ expansion, safe_path checks, undo, and rejection corrections."""
from __future__ import annotations

import os
import subprocess
import uuid
from pathlib import Path

import pytest
from test_agent_local_tools import execute
from test_agent_local_tools import local as local_fixture

from rynmesh.agent_runtime.file_review import safe_path
from rynmesh.agent_runtime.local_specs import SPECS
from rynmesh.agent_runtime.store import REJECTION_CORRECTIONS, AgentToolRejected


@pytest.fixture
def local(tmp_path):
    yield from local_fixture.__wrapped__(tmp_path)


def _set_modes(store, *, settings_mode: str, task_mode: str, task_id: str = "task") -> None:
    def _tx(data):
        data["settings"]["permission_mode"] = settings_mode
        if task_id in data["tasks"]:
            data["tasks"][task_id]["permission_mode"] = task_mode

    store.transaction(_tx)


def test_t1_automatic_mode_allows_outside_workspace_write_read_patch_and_reversible_file_review(local, tmp_path):
    """T1: automatic 档 apply_patch/file_write/file_read 写读工作区外绝对路径成功且 file_review 有记录可撤销（覆盖改动 1、2、3、4、6）。"""
    tools, context, workspace, store = local
    _set_modes(store, settings_mode="automatic", task_mode="automatic")

    # Verify Step 6 tool descriptions updated cleanly while keeping encoding label intact.
    for tool_name in ("ryn_file_list", "ryn_file_read", "ryn_file_write", "ryn_file_edit", "ryn_apply_patch", "ryn_file_manage"):
        desc = SPECS[tool_name][0]
        assert "工作区内；自动处理档可用绝对路径访问工作区外" in desc
        assert "inside workspace" not in desc
    for tool_name in ("ryn_file_read", "ryn_file_write", "ryn_file_edit"):
        assert "文本文件（UTF-8/UTF-16/GB18030）" in SPECS[tool_name][0]

    outside_dir = tmp_path / "outside_area"
    outside_dir.mkdir()
    out_write = outside_dir / "written.txt"
    out_patch = outside_dir / "patched.txt"

    # 1. ryn_file_write to outside-workspace absolute path
    w_res = execute(
        tools,
        context,
        "ryn_file_write",
        {"path": str(out_write), "content": "first line\n"},
        "op_out_write",
    )
    assert w_res["written"] is True
    assert Path(w_res["path"]) == out_write.resolve()
    assert out_write.read_text(encoding="utf-8") == "first line\n"

    # 2. ryn_file_read from outside-workspace absolute path
    r_res = tools.perform("ryn_file_read", {"path": str(out_write)}, context, lambda: False)
    assert r_res["text"] == "first line\n"
    assert r_res["sha256"] == w_res["sha256"]

    # 3. ryn_apply_patch adding a new outside file and updating existing outside file
    patch_body = (
        "*** Begin Patch\n"
        f"*** Add File: {out_patch}\n"
        "+created by patch\n"
        f"*** Update File: {out_write}\n"
        "@@\n"
        "-first line\n"
        "+updated line\n"
        "*** End Patch\n"
    )
    p_res = execute(
        tools,
        context,
        "ryn_apply_patch",
        {"patch": patch_body},
        "op_out_patch",
    )
    assert p_res["applied"] is True
    assert out_patch.read_text(encoding="utf-8") == "created by patch\n"
    assert out_write.read_text(encoding="utf-8") == "updated line\n"

    # 4. Inspect FileReview manifest (outside_workspace=True), review(), preview(), and undo()
    fr = tools.file_review("task", str(workspace))
    manifest = fr.load()
    assert os.path.normcase(manifest["root"]) == os.path.normcase(str(workspace))
    all_steps = [s for rec in manifest["records"] for s in rec["steps"]]
    assert len(all_steps) >= 3
    assert all(s.get("outside_workspace") is True for s in all_steps)

    rev = tools.change_review("task", str(workspace))
    rev_paths = {Path(f["path"]) for f in rev["files"]}
    assert out_write.resolve() in rev_paths
    assert out_patch.resolve() in rev_paths
    assert all(os.path.isabs(f["path"]) for f in rev["files"])
    assert all(f["can_undo"] is True for f in rev["files"])

    prev = fr.preview()
    assert prev["can_undo"] is True
    assert all(os.path.isabs(item["path"]) for item in prev["items"])

    undo_res = fr.undo(None, prev["token"], "undo_req_" + uuid.uuid4().hex[:12])
    assert undo_res["status"] == "completed"
    assert all(os.path.isabs(p) for p in undo_res["completed"])
    assert not out_write.exists()
    assert not out_patch.exists()


@pytest.mark.parametrize("mode", ["workspace", "confirm_changes", "read_only"])
def test_t2_non_automatic_modes_reject_outside_workspace_with_correction(local, tmp_path, mode):
    """T2: workspace/confirm_changes（及 read_only）档仍拒绝且带 correction（覆盖改动 1、2、5、7）。"""
    tools, context, _, store = local
    _set_modes(store, settings_mode=mode, task_mode=mode)

    assert "agent_local_path_outside_workspace" in REJECTION_CORRECTIONS
    assert "agent_review_path_forbidden" in REJECTION_CORRECTIONS

    outside_dir = tmp_path / f"outside_{mode}"
    outside_dir.mkdir()
    target = outside_dir / "blocked.txt"
    target.write_text("secret\n", encoding="utf-8")

    expected_phrase = "文件工具只能访问 workspace_root；需要时请主人切到自动处理或改工作区，不要用 shell 绕过"

    # Read tool rejection + correction
    with pytest.raises(AgentToolRejected) as read_exc:
        tools.perform("ryn_file_read", {"path": str(target)}, context, lambda: False)
    assert str(read_exc.value) == "agent_local_path_outside_workspace"
    assert read_exc.value.correction and expected_phrase in read_exc.value.correction

    # Write preview rejection + correction
    with pytest.raises(AgentToolRejected) as write_exc:
        tools.preview("ryn_file_write", {"path": str(outside_dir / "new.txt"), "content": "nope"}, context)
    assert str(write_exc.value) == "agent_local_path_outside_workspace"
    assert write_exc.value.correction and expected_phrase in write_exc.value.correction

    # Patch preview rejection + correction
    patch_body = f"*** Begin Patch\n*** Add File: {outside_dir / 'patch_new.txt'}\n+nope\n*** End Patch\n"
    with pytest.raises(AgentToolRejected) as patch_exc:
        tools.preview("ryn_apply_patch", {"patch": patch_body}, context)
    assert str(patch_exc.value) == "agent_local_path_outside_workspace"
    assert patch_exc.value.correction and expected_phrase in patch_exc.value.correction
    assert not (outside_dir / "new.txt").exists()
    assert not (outside_dir / "patch_new.txt").exists()


def test_t3_outside_paths_with_dot_git_ads_device_names_and_links_are_rejected(local, tmp_path):
    """T3: 外部路径含 .git、ADS、设备名、符号链接/目录连接点/硬链接仍拒，且 forbidden details 含被拒段名（覆盖改动 2、3、4、5）。"""
    tools, context, workspace, store = local
    _set_modes(store, settings_mode="automatic", task_mode="automatic")

    outside_dir = tmp_path / "outside_guards"
    outside_dir.mkdir()
    (outside_dir / ".git").mkdir()

    # 1. .git segment in outside path -> agent_review_path_forbidden with segment='.git' and correction
    git_target = outside_dir / ".git" / "config"
    with pytest.raises(AgentToolRejected) as git_exc:
        execute(tools, context, "ryn_file_write", {"path": str(git_target), "content": "bad"}, "op_git_out")
    assert str(git_exc.value) == "agent_review_path_forbidden"
    assert isinstance(git_exc.value.details, dict)
    assert git_exc.value.details.get("segment") == ".git"
    assert git_exc.value.correction

    # 2. ADS (colon stream) in outside path -> rejected
    ads_target = str(outside_dir / "normal.txt") + ":hidden_stream"
    with pytest.raises(AgentToolRejected) as ads_exc:
        execute(tools, context, "ryn_file_write", {"path": ads_target, "content": "bad"}, "op_ads_out")
    assert str(ads_exc.value) in {"agent_review_path_forbidden", "agent_local_path_invalid"}
    if str(ads_exc.value) == "agent_review_path_forbidden":
        assert ads_exc.value.details and ":" in ads_exc.value.details.get("segment", "")

    # 3. Reserved device names (on Windows: CON.txt / NUL; also check direct path() validation)
    if os.name == "nt":
        for dev_name in ("CON", "CON.txt", "NUL", "COM1.log", "LPT1"):
            dev_path = str(outside_dir / dev_name)
            with pytest.raises(AgentToolRejected) as dev_exc:
                tools.path(dev_path, context)
            assert str(dev_exc.value) == "agent_local_path_invalid"
            with pytest.raises(AgentToolRejected):
                execute(tools, context, "ryn_file_write", {"path": dev_path, "content": "bad"}, f"op_dev_{dev_name}")
    else:
        # Null byte / forbidden trailing space segment still rejected on POSIX:
        with pytest.raises(AgentToolRejected) as sp_exc:
            safe_path(workspace, str(outside_dir / "trailing "), outside_ok=True)
        assert str(sp_exc.value) == "agent_review_path_forbidden"
        assert sp_exc.value.details and sp_exc.value.details.get("segment") == "trailing "

    # 4. Symlink / junction in outside path parent chain -> agent_review_link_forbidden
    real_sub = outside_dir / "real_sub"
    real_sub.mkdir()
    real_file = real_sub / "keep.txt"
    real_file.write_text("original", encoding="utf-8")
    link_dir = outside_dir / "linked_sub"
    if os.name == "nt":
        subprocess.run(["cmd", "/c", "mklink", "/J", str(link_dir), str(real_sub)], check=True, capture_output=True)
    else:
        link_dir.symlink_to(real_sub, target_is_directory=True)
    try:
        with pytest.raises(AgentToolRejected) as link_exc:
            execute(
                tools,
                context,
                "ryn_file_write",
                {"path": str(link_dir / "escape.txt"), "content": "bad"},
                "op_link_out",
            )
        assert str(link_exc.value) == "agent_review_link_forbidden"
        assert not (real_sub / "escape.txt").exists()
    finally:
        link_dir.rmdir() if os.name == "nt" else link_dir.unlink()

    # 5. Hardlink on existing outside file -> agent_review_regular_file_required
    hard_alias = outside_dir / "hard_alias.txt"
    os.link(real_file, hard_alias)
    with pytest.raises(AgentToolRejected) as hard_exc:
        safe_path(workspace, str(hard_alias), outside_ok=True)
    assert str(hard_exc.value) == "agent_review_regular_file_required"


def test_t4_tilde_home_path_expands_for_read_write_and_patch(local, tmp_path, monkeypatch):
    """T4: ~/a.txt 展开为用户家目录绝对路径（覆盖改动 2、3、4）。"""
    tools, context, workspace, store = local
    fake_home = tmp_path / "fake_user_home"
    fake_home.mkdir()
    monkeypatch.setenv("HOME", str(fake_home))
    monkeypatch.setenv("USERPROFILE", str(fake_home))

    # In workspace mode, ~/a.txt expands to fake_home/a.txt (outside workspace) -> outside_workspace rejection,
    # NOT treated as <workspace>/~/a.txt!
    _set_modes(store, settings_mode="workspace", task_mode="workspace")
    with pytest.raises(AgentToolRejected) as ws_exc:
        tools.preview("ryn_file_write", {"path": "~/a.txt", "content": "blocked"}, context)
    assert str(ws_exc.value) == "agent_local_path_outside_workspace"
    assert not (workspace / "~").exists()
    assert not (fake_home / "a.txt").exists()

    # In automatic mode, ~/a.txt expands to fake_home/a.txt and writes/reads/patches cleanly!
    _set_modes(store, settings_mode="automatic", task_mode="automatic")
    w_res = execute(tools, context, "ryn_file_write", {"path": "~/a.txt", "content": "hello home\n"}, "op_tilde_write")
    assert Path(w_res["path"]) == (fake_home / "a.txt").resolve()
    assert (fake_home / "a.txt").read_text(encoding="utf-8") == "hello home\n"
    assert not (workspace / "~").exists()

    r_res = tools.perform("ryn_file_read", {"path": "~/a.txt"}, context, lambda: False)
    assert r_res["text"] == "hello home\n"

    patch_res = execute(
        tools,
        context,
        "ryn_apply_patch",
        {"patch": "*** Begin Patch\n*** Update File: ~/a.txt\n@@\n-hello home\n+patched home\n*** End Patch\n"},
        "op_tilde_patch",
    )
    assert patch_res["applied"] is True
    assert (fake_home / "a.txt").read_text(encoding="utf-8") == "patched home\n"


def test_t5_stricter_of_task_and_settings_permission_mode_governs_outside_access(local, tmp_path):
    """T5: 任务档位比设置严（或设置比任务严）时以严的为准（覆盖改动 1、2、4）。"""
    tools, context, _, store = local
    outside_dir = tmp_path / "outside_strictness"
    outside_dir.mkdir()
    target = outside_dir / "strict.txt"

    # Case 1: settings is automatic, but task is workspace -> effective is workspace (rejected)
    _set_modes(store, settings_mode="automatic", task_mode="workspace")
    assert tools.policy(context)["permission_mode"] == "workspace"
    with pytest.raises(AgentToolRejected) as exc_1:
        execute(tools, context, "ryn_file_write", {"path": str(target), "content": "no"}, "op_strict_1")
    assert str(exc_1.value) == "agent_local_path_outside_workspace"

    # Case 2: settings is automatic, task is confirm_changes -> effective is confirm_changes (rejected)
    _set_modes(store, settings_mode="automatic", task_mode="confirm_changes")
    assert tools.policy(context)["permission_mode"] == "confirm_changes"
    with pytest.raises(AgentToolRejected) as exc_2:
        tools.perform("ryn_file_read", {"path": str(target)}, context, lambda: False)
    assert str(exc_2.value) == "agent_local_path_outside_workspace"

    # Case 3: existing task retains its mode, but outside access still needs both modes.
    _set_modes(store, settings_mode="workspace", task_mode="automatic")
    policy = tools.policy(context)
    assert policy["permission_mode"] == "automatic" and policy["allow_outside_workspace"] is False
    with pytest.raises(AgentToolRejected) as exc_3:
        execute(tools, context, "ryn_file_write", {"path": str(target), "content": "no"}, "op_strict_3")
    assert str(exc_3.value) == "agent_local_path_outside_workspace"

    # Case 4: both settings and task are automatic -> effective is automatic (allowed)
    _set_modes(store, settings_mode="automatic", task_mode="automatic")
    assert tools.policy(context)["permission_mode"] == "automatic"
    ok_res = execute(tools, context, "ryn_file_write", {"path": str(target), "content": "yes\n"}, "op_strict_4")
    assert ok_res["written"] is True
    assert target.read_text(encoding="utf-8") == "yes\n"
