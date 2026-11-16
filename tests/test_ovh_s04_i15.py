"""Tests for S04-I15: incremental OutputDecoder fallback for non-UTF-8 output, sticky encoding, PYTHONUTF8 env, and output_encoding payload."""

from __future__ import annotations

import json
import sys

from rynmesh.agent_runtime import local_processes
from rynmesh.agent_runtime.local_processes import Processes, child_environment


def test_t1_cp936_bytes_decode_to_chinese_and_report_output_encoding(tmp_path, monkeypatch):
    OutputDecoder = getattr(local_processes, "OutputDecoder", None)
    assert OutputDecoder is not None, "OutputDecoder is not defined in local_processes"

    monkeypatch.setattr(local_processes.os, "name", "nt")
    monkeypatch.setattr(local_processes.locale, "getpreferredencoding", lambda *_: "cp936")

    raw_cp936 = "正在 Ping 本地主机：数据包已接收".encode("cp936")
    decoder = OutputDecoder()
    # Split mid-character on the first 2-byte GBK character (`\xd5\xfd`) to verify split-chunk safety.
    part1 = decoder.decode(raw_cp936[:1], final=False)
    part2 = decoder.decode(raw_cp936[1:], final=True)
    assert part1 + part2 == "正在 Ping 本地主机：数据包已接收"
    assert decoder.encoding == "cp936"

    # Non-Windows stays UTF-8 with replacement rather than switching to a regional code page.
    monkeypatch.setattr(local_processes.os, "name", "posix")
    posix_decoder = OutputDecoder()
    replaced = posix_decoder.decode(raw_cp936, final=True)
    assert "\ufffd" in replaced
    assert posix_decoder.encoding == "utf-8"

    # Real child process on host writing raw cp936 bytes to stdout buffer:
    monkeypatch.undo()
    monkeypatch.setattr(local_processes.os, "name", "nt")
    monkeypatch.setattr(local_processes.locale, "getpreferredencoding", lambda *_: "cp936")
    processes = Processes()
    try:
        script = "import sys; sys.stdout.buffer.write('中文系统输出测试：连接成功\\n'.encode('cp936')); sys.stdout.buffer.flush()"
        ident = processes.start([sys.executable, "-c", script], tmp_path, "task_i15_t1", 15, lambda: True, lambda: False)
        result = processes.wait(ident, "task_i15_t1", lambda: False)
    finally:
        processes.close()

    assert "中文系统输出测试：连接成功" in result["output"]
    assert "\ufffd" not in result["output"]
    assert result["output_encoding"] == "cp936"


def test_t2_utf8_output_stays_utf8_without_switching(tmp_path, monkeypatch):
    OutputDecoder = getattr(local_processes, "OutputDecoder", None)
    assert OutputDecoder is not None, "OutputDecoder is not defined in local_processes"

    monkeypatch.setattr(local_processes.os, "name", "nt")
    monkeypatch.setattr(local_processes.locale, "getpreferredencoding", lambda *_: "cp936")

    utf8_bytes = "纯正 UTF-8 中文输出与符号 🚀 完成".encode("utf-8")
    decoder = OutputDecoder()
    # Feed 2 bytes at a time so multi-byte UTF-8 sequences are split across boundaries.
    pieces = [decoder.decode(utf8_bytes[i : i + 2], final=False) for i in range(0, len(utf8_bytes), 2)]
    pieces.append(decoder.decode(b"", final=True))
    assert "".join(pieces) == "纯正 UTF-8 中文输出与符号 🚀 完成"
    assert decoder.encoding == "utf-8"

    monkeypatch.undo()
    processes = Processes()
    try:
        script = "import sys; sys.stdout.buffer.write('标准UTF8输出不切换\\n'.encode('utf-8')); sys.stdout.buffer.flush()"
        ident = processes.start([sys.executable, "-c", script], tmp_path, "task_i15_t2", 15, lambda: True, lambda: False)
        result = processes.wait(ident, "task_i15_t2", lambda: False)
    finally:
        processes.close()

    assert "标准UTF8输出不切换" in result["output"]
    assert result["output_encoding"] == "utf-8"


def test_t3_mixed_utf8_prefix_then_gbk_suffix_keeps_prefix_and_decodes_suffix(tmp_path, monkeypatch):
    OutputDecoder = getattr(local_processes, "OutputDecoder", None)
    assert OutputDecoder is not None, "OutputDecoder is not defined in local_processes"

    monkeypatch.setattr(local_processes.os, "name", "nt")
    monkeypatch.setattr(local_processes.locale, "getpreferredencoding", lambda *_: "cp936")

    # Case A: separate chunks (UTF-8 chunk -> GBK chunk -> sticky subsequent GBK chunk)
    dec_multi = OutputDecoder()
    out1 = dec_multi.decode("前半段UTF-8中文\n".encode("utf-8"), final=False)
    assert out1 == "前半段UTF-8中文\n"
    assert dec_multi.encoding == "utf-8"
    out2 = dec_multi.decode("后半段GBK中文\n".encode("cp936"), final=False)
    out3 = dec_multi.decode("后续粘性GBK行\n".encode("cp936"), final=True)
    assert out1 + out2 + out3 == "前半段UTF-8中文\n后半段GBK中文\n后续粘性GBK行\n"
    assert dec_multi.encoding == "cp936"

    # Case B: single combined chunk containing valid UTF-8 prefix immediately followed by GBK bytes
    dec_single = OutputDecoder()
    combined = "前半UTF8不变|".encode("utf-8") + "后半GBK正确".encode("cp936")
    assert dec_single.decode(combined, final=True) == "前半UTF8不变|后半GBK正确"
    assert dec_single.encoding == "cp936"

    # Case C: real spawned process emitting UTF-8 then cp936
    monkeypatch.undo()
    monkeypatch.setattr(local_processes.os, "name", "nt")
    monkeypatch.setattr(local_processes.locale, "getpreferredencoding", lambda *_: "cp936")
    processes = Processes()
    try:
        script = (
            "import sys; "
            "sys.stdout.buffer.write('前半UTF8行\\n'.encode('utf-8')); sys.stdout.buffer.flush(); "
            "sys.stdout.buffer.write('后半GBK行\\n'.encode('cp936')); sys.stdout.buffer.flush()"
        )
        ident = processes.start([sys.executable, "-c", script], tmp_path, "task_i15_t3", 15, lambda: True, lambda: False)
        result = processes.wait(ident, "task_i15_t3", lambda: False)
    finally:
        processes.close()

    assert "前半UTF8行" in result["output"]
    assert "后半GBK行" in result["output"]
    assert result["output_encoding"] == "cp936"


def test_t4_python_utf8_variables_injected_into_child_environment(tmp_path, monkeypatch):
    monkeypatch.setenv("PYTHONUTF8", "0")
    monkeypatch.setenv("PYTHONIOENCODING", "gbk")

    env = child_environment(task_id="task_i15_t4")
    norm = {k.upper(): v for k, v in env.items()}
    assert norm.get("PYTHONUTF8") == "1"
    assert norm.get("PYTHONIOENCODING") == "utf-8"

    processes = Processes()
    try:
        script = (
            "import json, os; "
            "print(json.dumps({'utf8': os.environ.get('PYTHONUTF8'), 'io': os.environ.get('PYTHONIOENCODING'), 'msg': '子进程UTF8中文'}, ensure_ascii=False))"
        )
        ident = processes.start([sys.executable, "-c", script], tmp_path, "task_i15_t4", 15, lambda: True, lambda: False)
        result = processes.wait(ident, "task_i15_t4", lambda: False)
    finally:
        processes.close()

    parsed = json.loads(result["output"].strip())
    assert parsed == {"utf8": "1", "io": "utf-8", "msg": "子进程UTF8中文"}
    assert result["output_encoding"] == "utf-8"


def test_trailing_incomplete_utf8_at_eof_emits_replacement_without_switching(monkeypatch):
    monkeypatch.setattr(local_processes.os, "name", "nt")
    monkeypatch.setattr(local_processes.locale, "getpreferredencoding", lambda *_: "cp1252")

    decoder = local_processes.OutputDecoder()
    part1 = decoder.decode("🚀世界".encode("utf-8") + b"\xe4\xbd", final=False)
    part2 = decoder.decode(b"", final=True)
    assert part1 + part2 == "🚀世界\ufffd"
    assert decoder.encoding == "utf-8"

    single = local_processes.OutputDecoder()
    assert single.decode("🚀世界".encode("utf-8") + b"\xe4\xbd", final=True) == "🚀世界\ufffd"
    assert single.encoding == "utf-8"
