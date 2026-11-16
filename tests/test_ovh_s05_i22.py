"""Overhaul S05-I22: file_review link checks only below workspace root and allow cloud placeholder reparse tags."""
from __future__ import annotations

import os
import stat
import subprocess
import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest
from test_agent_local_tools import execute
from test_agent_runtime import make_runtime

from rynmesh.agent_runtime import file_review_io
from rynmesh.agent_runtime.file_review import FileReview, safe_path
from rynmesh.agent_runtime.local_tools import LocalTools
from rynmesh.agent_runtime.operation_ledger import OperationLedger
from rynmesh.agent_runtime.store import AgentToolRejected


def _make_dir_link(link_path: Path, target_dir: Path) -> None:
    if os.name == "nt":
        subprocess.run(
            ["cmd", "/c", "mklink", "/J", str(link_path), str(target_dir)],
            check=True,
            capture_output=True,
        )
    else:
        link_path.symlink_to(target_dir, target_is_directory=True)


def _remove_dir_link(link_path: Path) -> None:
    if not link_path.exists() and not link_path.is_symlink():
        return
    if os.name == "nt":
        link_path.rmdir()
    else:
        link_path.unlink()


def _setup_tools_for_workspace(base: Path, workspace: Path):
    home = base / "node_home"
    home.mkdir(parents=True, exist_ok=True)
    runtime, _, _, _ = make_runtime(home / "agent")
    store = runtime.store
    configuration = {"workspace_root": str(workspace), "desktop_access": False}
    runtime.update_settings(configuration)
    task = runtime.create({"message": "S05-I22 file review"})

    def _rename(data):
        row = data["tasks"].pop(task["id"])
        row["id"] = "task"
        data["tasks"]["task"] = row

    store.transaction(_rename)
    ledger = OperationLedger(home)
    tools = LocalTools(SimpleNamespace(home=home), lambda: ledger)
    tools.store = store
    context = {"task_id": "task", "local_execution": configuration}
    return tools, context, store


def test_t1_workspace_behind_junction_or_symlink_allows_write_undo_and_parent_handle_scoping(tmp_path, monkeypatch):
    """T1: 工作区路径本身经过目录联接/符号链接时写入与撤销成功，且 Windows 父目录句柄仅对 root 以下加 OPEN_REPARSE_POINT（覆盖改动 1、3）。"""
    real_area = tmp_path / "real_backing"
    real_ws = real_area / "ws"
    (real_ws / "sub").mkdir(parents=True)

    linked_area = tmp_path / "linked_mount"
    direct_ws_link = tmp_path / "direct_ws_link"
    _make_dir_link(linked_area, real_area)
    _make_dir_link(direct_ws_link, real_ws)
    try:
        # 1. Direct FileReview.apply with un-resolved workspace behind ancestor junction + spy on CreateFileW flags
        ws_via_ancestor = linked_area / "ws"
        tools, context, _ = _setup_tools_for_workspace(tmp_path / "env1", ws_via_ancestor)
        try:
            opened_dir_flags: list[tuple[Path, int]] = []
            if os.name == "nt":
                import ctypes

                orig_windll = ctypes.WinDLL

                def _spying_windll(name, *args, **kwargs):
                    dll = orig_windll(name, *args, **kwargs)
                    if str(name).lower() == "kernel32":
                        real_create = dll.CreateFileW

                        def _wrapped_create(lp_filename, access, share, sec, creation, flags, template):
                            if flags & 0x02000000:
                                opened_dir_flags.append((Path(lp_filename), int(flags)))
                            return real_create(lp_filename, access, share, sec, creation, flags, template)

                        return SimpleNamespace(
                            CreateFileW=_wrapped_create,
                            CloseHandle=dll.CloseHandle,
                            SetFileInformationByHandle=dll.SetFileInformationByHandle,
                        )
                    return dll

                monkeypatch.setattr(ctypes, "WinDLL", _spying_windll)

            fr = FileReview(tools, "task", str(ws_via_ancestor))
            unres_target = ws_via_ancestor / "sub" / "direct.txt"
            fr.apply([(unres_target, None, b"hello via junction\n", None)], operation_id="op_direct_1")
            assert (real_ws / "sub" / "direct.txt").read_bytes() == b"hello via junction\n"

            # Manifest keeps the un-resolved path passed to apply()
            manifest = fr.load()
            assert manifest["records"][0]["steps"][0]["path"] == str(unres_target)
            assert not manifest["records"][0]["steps"][0].get("outside_workspace")

            if os.name == "nt":
                assert opened_dir_flags, "Expected Windows directory handles to be opened in replace_checked"
                flags_by_path = {os.path.normcase(str(p)): fl for p, fl in opened_dir_flags}
                sub_key = os.path.normcase(str(ws_via_ancestor / "sub"))
                ws_key = os.path.normcase(str(ws_via_ancestor))
                link_key = os.path.normcase(str(linked_area))
                # Below root -> FILE_FLAG_BACKUP_SEMANTICS | FILE_FLAG_OPEN_REPARSE_POINT (0x02200000)
                assert flags_by_path[sub_key] == 0x02200000
                # At root and above root -> normal directory open FILE_FLAG_BACKUP_SEMANTICS (0x02000000)
                assert flags_by_path[ws_key] == 0x02000000
                assert flags_by_path[link_key] == 0x02000000
        finally:
            tools.processes.close()

        # 2. End-to-end LocalTools (ryn_file_write + ryn_apply_patch + change_review + undo) when workspace itself IS a junction/symlink
        tools2, context2, _ = _setup_tools_for_workspace(tmp_path / "env2", direct_ws_link)
        try:
            w_res = execute(
                tools2,
                context2,
                "ryn_file_write",
                {"path": "sub/via_tool.txt", "content": "line 1\n"},
                "op_write_link_ws",
            )
            assert w_res["written"] is True
            assert (real_ws / "sub" / "via_tool.txt").read_text(encoding="utf-8") == "line 1\n"

            patch_text = (
                "*** Begin Patch\n"
                "*** Update File: sub/via_tool.txt\n"
                "@@\n"
                "-line 1\n"
                "+line 2\n"
                "*** End Patch\n"
            )
            p_res = execute(tools2, context2, "ryn_apply_patch", {"patch": patch_text}, "op_patch_link_ws")
            assert p_res["applied"] is True
            assert (real_ws / "sub" / "via_tool.txt").read_text(encoding="utf-8") == "line 2\n"

            rev = tools2.change_review("task", str(direct_ws_link))
            assert len(rev["files"]) == 1
            assert rev["files"][0]["path"] == "sub/via_tool.txt"
            assert not rev["files"][0].get("outside_workspace")

            fr2 = tools2.file_review("task", str(direct_ws_link))
            prev2 = fr2.preview()
            assert prev2["can_undo"] is True
            undo2 = fr2.undo(None, prev2["token"], "undo_" + uuid.uuid4().hex[:12])
            assert undo2["status"] == "completed"
            assert not (real_ws / "sub" / "via_tool.txt").exists()
        finally:
            tools2.processes.close()
    finally:
        _remove_dir_link(direct_ws_link)
        _remove_dir_link(linked_area)


def test_t2_subdirectory_junction_or_symlink_inside_workspace_is_rejected(tmp_path):
    """T2: 工作区内子目录是联接/符号链接时仍拒（覆盖改动 1、2）。"""
    workspace = tmp_path / "workspace"
    real_sub = workspace / "real_sub"
    real_sub.mkdir(parents=True)
    linked_sub = workspace / "linked_sub"
    _make_dir_link(linked_sub, real_sub)
    tools, context, _ = _setup_tools_for_workspace(tmp_path / "env_t2", workspace)
    try:
        with pytest.raises(AgentToolRejected) as exc_sp:
            safe_path(workspace, "linked_sub/inside.txt")
        assert str(exc_sp.value) == "agent_review_link_forbidden"

        with pytest.raises(AgentToolRejected) as exc_tool:
            execute(
                tools,
                context,
                "ryn_file_write",
                {"path": "linked_sub/inside.txt", "content": "blocked"},
                "op_t2_blocked",
            )
        assert str(exc_tool.value) == "agent_review_link_forbidden"
        assert not (real_sub / "inside.txt").exists()
    finally:
        tools.processes.close()
        _remove_dir_link(linked_sub)


def test_t3_cloud_reparse_tags_allowed_while_symlink_mount_appexeclink_and_unknown_rejected(tmp_path, monkeypatch):
    """T3: Windows 模拟云占位标签（0x9000001A 及 0x9000x01A 变体）放行，SYMLINK/MOUNT_POINT/APPEXECLINK/未知标签仍拒（覆盖改动 2）。"""
    workspace = tmp_path / "cloud_ws"
    cloud_dir = workspace / "onedrive_folder"
    cloud_dir.mkdir(parents=True)
    cloud_file = cloud_dir / "notes.txt"
    cloud_file.write_text("cloud content\n", encoding="utf-8")

    orig_lstat = Path.lstat
    active_tag = {"value": 0x9000001A}

    def _mock_lstat(self: Path):
        real_stat = orig_lstat(self)
        if os.path.normcase(str(self)) in {
            os.path.normcase(str(cloud_dir)),
            os.path.normcase(str(cloud_file)),
        }:
            return SimpleNamespace(
                st_mode=stat.S_IFDIR | 0o755 if self == cloud_dir else stat.S_IFREG | 0o644,
                st_file_attributes=0x400 | 0x20,
                st_reparse_tag=active_tag["value"],
                st_nlink=1,
                st_size=real_stat.st_size,
                st_dev=real_stat.st_dev,
                st_ino=real_stat.st_ino,
                st_mtime_ns=real_stat.st_mtime_ns,
            )
        return real_stat

    monkeypatch.setattr(Path, "lstat", _mock_lstat)

    # All IO_REPARSE_TAG_CLOUD* variants (0x9000001A and 0x9000x01A) must pass
    for cloud_tag in (0x9000001A, 0x9000101A, 0x9000601A, 0x9000A01A, 0x9000F01A):
        active_tag["value"] = cloud_tag
        checked = safe_path(workspace, "onedrive_folder/notes.txt")
        assert checked == cloud_file

    # SYMLINK (0xA000000C), MOUNT_POINT (0xA0000003), APPEXECLINK (0x8000001B), and unknown tags must be rejected
    for forbidden_tag in (0xA000000C, 0xA0000003, 0x8000001B, 0x80000099, 0x9000001B, 0):
        active_tag["value"] = forbidden_tag
        with pytest.raises(AgentToolRejected) as exc_tag:
            safe_path(workspace, "onedrive_folder/notes.txt")
        assert str(exc_tag.value) == "agent_review_link_forbidden"

    assert file_review_io is not None


def test_t4_link_inside_workspace_pointing_outside_workspace_is_rejected(tmp_path, monkeypatch):
    """T4: 工作区内链接指向工作区外仍拒，含 lstat 拦截与 resolved.is_relative_to(real_root) 兜底校验（覆盖改动 1、2）。"""
    workspace = tmp_path / "workspace_t4"
    workspace.mkdir()
    outside = tmp_path / "outside_secret"
    outside.mkdir()
    (outside / "keep.txt").write_text("untouched", encoding="utf-8")

    escape_link = workspace / "escape_out"
    _make_dir_link(escape_link, outside)
    tools, context, _ = _setup_tools_for_workspace(tmp_path / "env_t4", workspace)
    try:
        with pytest.raises(AgentToolRejected) as exc_1:
            safe_path(workspace, "escape_out/keep.txt")
        assert str(exc_1.value) == "agent_review_link_forbidden"

        fr = FileReview(tools, "task", str(workspace))
        with pytest.raises(AgentToolRejected) as exc_fr:
            fr.apply([(escape_link / "new_outside.txt", None, b"escape", None)], operation_id="op_t4_fr")
        assert str(exc_fr.value) == "agent_review_link_forbidden"

        with pytest.raises(AgentToolRejected) as exc_2:
            execute(
                tools,
                context,
                "ryn_file_write",
                {"path": "escape_out/new_outside.txt", "content": "escape"},
                "op_t4_escape",
            )
        assert str(exc_2.value) in {"agent_review_link_forbidden", "agent_local_path_outside_workspace"}
        assert not (outside / "new_outside.txt").exists()
        assert (outside / "keep.txt").read_text(encoding="utf-8") == "untouched"

        # Even if lstat reported a cloud tag, resolved.is_relative_to(real_root) still blocks escaping outside real_root
        orig_lstat = Path.lstat

        def _cloud_lstat(self: Path):
            st = orig_lstat(self)
            if os.path.normcase(str(self)) == os.path.normcase(str(escape_link)):
                return SimpleNamespace(
                    st_mode=stat.S_IFDIR | 0o755,
                    st_file_attributes=0x400,
                    st_reparse_tag=0x9000001A,
                    st_nlink=1,
                )
            return st

        monkeypatch.setattr(Path, "lstat", _cloud_lstat)
        with pytest.raises(AgentToolRejected) as exc_escape_resolve:
            safe_path(workspace, "escape_out/keep.txt")
        assert str(exc_escape_resolve.value) in {"agent_review_path_forbidden", "agent_review_link_forbidden"}
    finally:
        tools.processes.close()
        _remove_dir_link(escape_link)
