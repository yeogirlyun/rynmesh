"""Encoding detection, round-trip preservation, and search reporting for local file tools (S05-I27)."""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest
from test_agent_runtime import make_runtime

from rynmesh.agent_runtime import local_specs, local_tools
from rynmesh.agent_runtime.local_tools import LocalTools
from rynmesh.agent_runtime.operation_ledger import OperationLedger
from rynmesh.agent_runtime.store import AgentToolRejected


@pytest.fixture
def local_env(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    home = tmp_path / "node"
    home.mkdir()
    runtime, _, _, _ = make_runtime(home / "agent")
    state = runtime.store
    configuration = {"workspace_root": str(workspace), "desktop_access": False}
    runtime.update_settings(configuration)
    task = runtime.create({"message": "Encoding test task"})

    def rename(data):
        row = data["tasks"].pop(task["id"])
        row["id"] = "task"
        data["tasks"]["task"] = row

    state.transaction(rename)
    ledger = OperationLedger(home)
    tools = LocalTools(SimpleNamespace(home=home), lambda: ledger)
    tools.store = state
    context = {"task_id": "task", "local_execution": configuration}
    yield tools, context, workspace
    tools.processes.close()


def _execute(tools, context, name, args, op_id):
    preview = tools.preview(name, args, context)
    return tools.execute(
        name,
        args,
        operation_id=op_id,
        context=context,
        expected_preview=preview,
        cancelled=lambda: False,
    )


def test_gbk_and_utf16_file_read_returns_encoding(local_env):
    tools, context, workspace = local_env
    gbk_text = "姓名,部门,金额\r\n张三,财务,1200\r\n李四,研发,2500\r\n"
    gbk_raw = gbk_text.encode("gbk")
    gbk_file = workspace / "finance.csv"
    gbk_file.write_bytes(gbk_raw)

    decoded, enc = local_tools.decode_text(gbk_raw)
    assert enc == "gb18030"
    assert decoded == gbk_text

    res_default = tools.perform("ryn_file_read", {"path": "finance.csv"}, context, lambda: False)
    assert res_default["encoding"] == "gb18030"
    assert res_default["text"] == gbk_text

    res_slice = tools.perform("ryn_file_read", {"path": "finance.csv", "start_line": 2, "max_lines": 1}, context, lambda: False)
    assert res_slice["encoding"] == "gb18030"
    assert "张三,财务,1200" in res_slice["text"]

    py_gbk = "def 计算():\r\n    返回 = '中文结果'\r\n    return 返回\r\n"
    (workspace / "mod.py").write_bytes(py_gbk.encode("gbk"))
    res_indent = tools.perform(
        "ryn_file_read",
        {"path": "mod.py", "mode": "indentation", "anchor_line": 2},
        context,
        lambda: False,
    )
    assert res_indent["encoding"] == "gb18030"
    assert "中文结果" in res_indent["text"]

    u16le_raw = b"\xff\xfe" + "配置项=已启用\r\n".encode("utf-16-le")
    (workspace / "u16le.ini").write_bytes(u16le_raw)
    res_u16le = tools.perform("ryn_file_read", {"path": "u16le.ini"}, context, lambda: False)
    assert res_u16le["encoding"] == "utf-16-le"
    assert res_u16le["text"] == "配置项=已启用\r\n"

    u16be_raw = b"\xfe\xff" + "配置项=只读\r\n".encode("utf-16-be")
    (workspace / "u16be.ini").write_bytes(u16be_raw)
    res_u16be = tools.perform("ryn_file_read", {"path": "u16be.ini"}, context, lambda: False)
    assert res_u16be["encoding"] == "utf-16-be"
    assert res_u16be["text"] == "配置项=只读\r\n"

    raw_3, text_3, enc_3 = tools.text(gbk_file)
    assert raw_3 == gbk_raw and text_3 == gbk_text and enc_3 == "gb18030"
    raw_2, text_2 = tools.text_pair(gbk_file)
    assert raw_2 == gbk_raw and text_2 == gbk_text
    assert tools.text(gbk_file, with_encoding=False) == (gbk_raw, gbk_text)


def test_apply_patch_edit_and_write_preserve_gbk_and_utf16_encoding(local_env):
    tools, context, workspace = local_env
    original_lines = ["标题,金额", "甲,100", "乙,200", "丙,300"]
    gbk_path = workspace / "report.csv"
    gbk_path.write_bytes(("\r\n".join(original_lines) + "\r\n").encode("gbk"))

    tools.perform("ryn_file_read", {"path": "report.csv"}, context, lambda: False)
    patch = (
        "*** Begin Patch\n"
        "*** Update File: report.csv\n"
        "@@\n"
        " 甲,100\n"
        "-乙,200\n"
        "+乙,250\n"
        " 丙,300\n"
        "*** End Patch\n"
    )
    _execute(tools, context, "ryn_apply_patch", {"patch": patch}, "op-gbk-patch")

    expected_after_patch = "标题,金额\r\n甲,100\r\n乙,250\r\n丙,300\r\n".encode("gbk")
    assert gbk_path.read_bytes() == expected_after_patch

    _execute(
        tools,
        context,
        "ryn_file_edit",
        {"path": "report.csv", "old_text": "丙,300", "new_text": "丙,360"},
        "op-gbk-edit",
    )
    assert gbk_path.read_bytes() == "标题,金额\r\n甲,100\r\n乙,250\r\n丙,360\r\n".encode("gbk")

    _execute(
        tools,
        context,
        "ryn_file_write",
        {"path": "report.csv", "content": "标题,金额\r\n丁,900\r\n"},
        "op-gbk-write",
    )
    assert gbk_path.read_bytes() == "标题,金额\r\n丁,900\r\n".encode("gbk")

    _execute(
        tools,
        context,
        "ryn_file_write",
        {"path": "brand_new.txt", "content": "新建文件默认UTF8\n"},
        "op-new-utf8",
    )
    assert (workspace / "brand_new.txt").read_bytes() == "新建文件默认UTF8\n".encode("utf-8")

    u16_path = workspace / "win.reg.txt"
    u16_path.write_bytes(b"\xff\xfe" + "第一项=旧值\r\n第二项=保留\r\n".encode("utf-16-le"))
    tools.perform("ryn_file_read", {"path": "win.reg.txt"}, context, lambda: False)
    u16_patch = (
        "*** Begin Patch\n"
        "*** Update File: win.reg.txt\n"
        "@@\n"
        "-第一项=旧值\n"
        "+第一项=新值\n"
        " 第二项=保留\n"
        "*** End Patch\n"
    )
    _execute(tools, context, "ryn_apply_patch", {"patch": u16_patch}, "op-u16-patch")
    assert u16_path.read_bytes() == b"\xff\xfe" + "第一项=新值\r\n第二项=保留\r\n".encode("utf-16-le")


def test_writing_emoji_to_gbk_file_rejected_with_char_and_line(local_env):
    tools, context, workspace = local_env
    initial_bytes = "第一行：正常中文\n第二行：待修改\n第三行：结尾\n".encode("gbk")
    gbk_path = workspace / "notes.txt"
    gbk_path.write_bytes(initial_bytes)
    tools.perform("ryn_file_read", {"path": "notes.txt"}, context, lambda: False)

    patch = (
        "*** Begin Patch\n"
        "*** Update File: notes.txt\n"
        "@@\n"
        " 第一行：正常中文\n"
        "-第二行：待修改\n"
        "+第二行：火箭🚀发射\n"
        " 第三行：结尾\n"
        "*** End Patch\n"
    )
    with pytest.raises(AgentToolRejected) as exc_patch:
        _execute(tools, context, "ryn_apply_patch", {"patch": patch}, "op-emoji-patch")
    assert str(exc_patch.value) == "agent_local_encoding_unrepresentable"
    assert "🚀" in (exc_patch.value.correction or "") or "U+1F680" in (exc_patch.value.correction or "")
    assert "2" in (exc_patch.value.correction or "")
    assert exc_patch.value.details.get("line") == 2
    assert gbk_path.read_bytes() == initial_bytes

    with pytest.raises(AgentToolRejected) as exc_edit:
        _execute(
            tools,
            context,
            "ryn_file_edit",
            {"path": "notes.txt", "old_text": "待修改", "new_text": "待修改😀"},
            "op-emoji-edit",
        )
    assert str(exc_edit.value) == "agent_local_encoding_unrepresentable"
    assert "😀" in (exc_edit.value.correction or "") or "U+1F600" in (exc_edit.value.correction or "")
    assert exc_edit.value.details.get("line") == 2
    assert gbk_path.read_bytes() == initial_bytes

    with pytest.raises(AgentToolRejected) as exc_write:
        _execute(
            tools,
            context,
            "ryn_file_write",
            {"path": "notes.txt", "content": "第一行\n第二行\n第三行🚀\n"},
            "op-emoji-write",
        )
    assert str(exc_write.value) == "agent_local_encoding_unrepresentable"
    assert exc_write.value.details.get("line") == 3
    assert gbk_path.read_bytes() == initial_bytes


def test_file_search_matches_gbk_and_utf16_files(local_env, monkeypatch):
    monkeypatch.setattr(local_tools, "resolve_rg_executable", lambda: None)
    tools, context, workspace = local_env
    (workspace / "app_gbk.log").write_bytes("2026-11-15 警告\r\n2026-11-15 错误：数据库连接超时\r\n".encode("gbk"))
    (workspace / "cfg_u16.ini").write_bytes(b"\xff\xfe" + "说明=数据库连接池配置\r\n".encode("utf-16-le"))

    result = tools.perform("ryn_file_search", {"path": ".", "query": "数据库连接"}, context, lambda: False)
    matched_by_name = {Path(m["path"]).name: m["line"] for m in result["matches"]}
    assert matched_by_name == {"app_gbk.log": 2, "cfg_u16.ini": 1}
    assert result["non_text_files"] == 0
    assert result["too_large_files"] == 0


def test_file_search_reports_non_text_files_and_too_large_files(local_env, monkeypatch):
    monkeypatch.setattr(local_tools, "resolve_rg_executable", lambda: None)
    tools, context, workspace = local_env
    (workspace / "valid.txt").write_text("target needle line\n", encoding="utf-8")

    # 12 pure binary files with non-SEARCH_BINARY_SUFFIXES extensions (.dat/.raw)
    for idx in range(10):
        payload = b"\x00\x89\x50\x4e\x47\x0d\x0a\x1a\x0a" + bytes([idx, 0xFF, 0xFE, 0x00, 0xAC, 0xD8])
        (workspace / f"blob_{idx:02d}.dat").write_bytes(payload)
    # High-byte sequence that decodes to GB18030 Private Use Area (binary masquerading as multibyte)
    pua_binary = b"\xaa\xa1\xaa\xa2\xfe\xa1\xfe\xa2" * 16
    (workspace / "blob_10.raw").write_bytes(pua_binary)
    # Control-byte binary without high bytes
    ctrl_binary = b"\x01\x02\x03\x04\x05\x06\x07\x0e\x0f\x10" * 8
    (workspace / "blob_11.raw").write_bytes(ctrl_binary)

    with pytest.raises(UnicodeError):
        local_tools.decode_text(pua_binary)
    with pytest.raises(UnicodeError):
        local_tools.decode_text(ctrl_binary)

    # > 1 MiB file
    (workspace / "oversized.log").write_bytes(b"needle\n" + b"a" * (local_tools.MAX_FILE + 128))

    result = tools.perform("ryn_file_search", {"path": ".", "query": "needle"}, context, lambda: False)
    assert [Path(m["path"]).name for m in result["matches"]] == ["valid.txt"]
    assert result["non_text_files"] == 12
    assert len(result["non_text_file_paths"]) == 10
    assert all(Path(p).suffix in {".dat", ".raw"} for p in result["non_text_file_paths"])
    assert result["too_large_files"] == 1


def test_utf8_and_utf8_bom_behavior_and_specs_unchanged(local_env):
    tools, context, workspace = local_env
    utf8_raw = "hello 世界 🚀\nline two\n".encode("utf-8")
    utf8_path = workspace / "plain.txt"
    utf8_path.write_bytes(utf8_raw)

    r_plain = tools.perform("ryn_file_read", {"path": "plain.txt"}, context, lambda: False)
    assert r_plain["encoding"] == "utf-8"
    assert r_plain["text"] == "hello 世界 🚀\nline two\n"

    _execute(
        tools,
        context,
        "ryn_file_edit",
        {"path": "plain.txt", "old_text": "line two", "new_text": "line 2 ✅"},
        "op-utf8-edit",
    )
    assert utf8_path.read_bytes() == "hello 世界 🚀\nline 2 ✅\n".encode("utf-8")

    bom_raw = b"\xef\xbb\xbf" + "首行BOM\r\n次行保持\r\n".encode("utf-8")
    bom_path = workspace / "with_bom.txt"
    bom_path.write_bytes(bom_raw)

    r_bom = tools.perform("ryn_file_read", {"path": "with_bom.txt"}, context, lambda: False)
    assert r_bom["encoding"] == "utf-8-sig"
    assert r_bom["text"] == "首行BOM\r\n次行保持\r\n"

    patch_bom = (
        "*** Begin Patch\n"
        "*** Update File: with_bom.txt\n"
        "@@\n"
        " 首行BOM\n"
        "-次行保持\n"
        "+次行已改\n"
        "*** End Patch\n"
    )
    _execute(tools, context, "ryn_apply_patch", {"patch": patch_bom}, "op-bom-patch")
    assert bom_path.read_bytes() == b"\xef\xbb\xbf" + "首行BOM\r\n次行已改\r\n".encode("utf-8")

    expected_label = "文本文件（UTF-8/UTF-16/GB18030）"
    for tool_name in ("ryn_file_read", "ryn_file_write", "ryn_file_edit"):
        assert expected_label in local_specs.SPECS[tool_name][0]
