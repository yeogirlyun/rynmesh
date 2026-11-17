"""S05-I28: real rg search, Python fallback, bounds and cancellation."""
import subprocess
import sys
import time
from pathlib import Path

import pytest

from test_agent_local_tools import local
from rynmesh.agent_runtime import local_tools
from rynmesh.agent_runtime.store import AgentToolRejected


@pytest.fixture
def rg(monkeypatch):
    executable = local_tools.resolve_rg_executable()
    assert executable, "S05-I28 acceptance requires the bundled or installed ripgrep"
    monkeypatch.setattr(local_tools, "resolve_rg_executable", lambda: executable)
    return executable


def search(local, **args):
    tools, context, _, _ = local
    return tools.perform("ryn_file_search", {"path": ".", "query": "needle", **args}, context, lambda: False)


@pytest.mark.parametrize("git_repo", [False, True])
def test_t1_rg_obeys_nested_wildcards_and_negations(local, rg, git_repo):
    workspace = local[2]
    if git_repo:
        (workspace / ".git").mkdir()
    src = workspace / "src"
    src.mkdir()
    (src / ".gitignore").write_text("*.log\n!keep.log\ncache/\n", encoding="utf-8")
    (src / "cache").mkdir()
    for name in ("ignored.log", "keep.log", "ok.txt", "cache/data.txt"):
        (src / name).write_text("needle\n", encoding="utf-8")
    result = search(local)
    assert result["engine"] == "rg"
    assert {Path(m["path"]).name for m in result["matches"]} == {"keep.log", "ok.txt"}
    assert result["incomplete"] is False


def test_t2_5000_files_complete_without_python_file_limit(local, rg):
    workspace = local[2]
    for index in range(5000):
        (workspace / f"f{index:04d}.txt").write_text("needle\n" if index == 4999 else "other\n", encoding="utf-8")
    result = search(local)
    assert result["engine"] == "rg" and result["incomplete"] is False
    assert result["total_matches"] == 1 and result["scanned_files"] == 5000
    assert Path(result["matches"][0]["path"]).name == "f4999.txt"


def test_t3_python_fallback_keeps_pruning_filters_and_counts(local, monkeypatch):
    monkeypatch.setattr(local_tools, "resolve_rg_executable", lambda: None)
    workspace = local[2]
    (workspace / ".gitignore").write_text("build/\n", encoding="utf-8")
    (workspace / "build").mkdir()
    (workspace / "build" / "ignored.txt").write_text("needle\n", encoding="utf-8")
    (workspace / "a.txt").write_text("NEEDLE\nneedle\n", encoding="utf-8")
    (workspace / "b.py").write_text("needle\n", encoding="utf-8")
    result = search(local, case_sensitive=False, file_pattern="*.txt")
    assert result["engine"] == "python" and result["total_matches"] == 2
    assert result["scanned_files"] == 1 and result["pruned_directories"] == ["build"]
    assert [m["line"] for m in result["matches"]] == [1, 2]


def test_t4_python_valid_rg_unsupported_regex_falls_back(local, rg):
    (local[2] / "a.txt").write_text("needle\n", encoding="utf-8")
    result = search(local, query="(?=needle)needle", regex=True)
    assert result["engine"] == "python" and result["total_matches"] == 1
    assert result["matches"][0]["text"] == "needle"


def test_t4_invalid_python_regex_rejected_before_rg(local, rg, monkeypatch):
    def no_launch(*args, **kwargs):
        pytest.fail("invalid regex must not start rg")
    monkeypatch.setattr(local_tools.local_processes.subprocess, "Popen", no_launch)
    with pytest.raises(AgentToolRejected, match="agent_invalid_search_regex"):
        search(local, query="[", regex=True)


def test_t4_rg_newline_regex_error_also_falls_back(local, rg):
    (local[2] / "a.txt").write_text("needle\n", encoding="utf-8")
    result = search(local, query=r"needle\n", regex=True)
    assert result["engine"] == "python" and result["total_matches"] == 0


@pytest.mark.parametrize("stop", ["cancel", "timeout", "permission"])
def test_t5_idle_rg_stops_and_is_reaped(local, rg, monkeypatch, stop):
    popen = subprocess.Popen
    children = []
    def sleeping_rg(*args, **kwargs):
        child = popen([sys.executable, "-c", "import time; time.sleep(60)"], **kwargs)
        children.append(child)
        return child
    monkeypatch.setattr(local_tools.local_processes.subprocess, "Popen", sleeping_rg)
    if stop == "timeout":
        monkeypatch.setattr(local_tools, "MAX_SEARCH_SECONDS", 0.1)
    tools, context, _, _ = local
    original_policy = tools.policy
    def revoked(*args, **kwargs):
        if children:
            raise AgentToolRejected("agent_disabled")
        return original_policy(*args, **kwargs)
    if stop == "permission":
        monkeypatch.setattr(tools, "policy", revoked)
    started = time.monotonic()
    try:
        if stop == "timeout":
            result = search(local)
            assert result["engine"] == "rg" and result["incomplete"]
            assert result["stopped_reason"] == "time_limit"
        else:
            with pytest.raises(AgentToolRejected, match="agent_cancelled" if stop == "cancel" else "agent_disabled"):
                tools.perform("ryn_file_search", {"path": ".", "query": "needle"}, context,
                              lambda: bool(children) if stop == "cancel" else False)
        assert time.monotonic() - started < 5
        assert len(children) == 1 and children[0].poll() is not None
    finally:
        for child in children:
            if child.poll() is None:
                child.kill()
                child.wait()


@pytest.mark.parametrize("engine", ["rg", "python"])
def test_rg_pages_50_and_ranks_50_files(local, rg, monkeypatch, engine):
    if engine == "python":
        monkeypatch.setattr(local_tools, "resolve_rg_executable", lambda: None)
    for index in range(60):
        (local[2] / f"a{index:02d}.txt").write_text("needle\n", encoding="utf-8")
    first = search(local)
    second = search(local, offset=first["next_offset"])
    assert first["engine"] == engine and len(first["matches"]) == 50 and first["next_offset"] == 50
    assert len(first["files_matched"]) == 50 and first["files_matched_total"] == 60
    assert len(second["matches"]) == 10 and second["next_offset"] is None


def test_rg_literal_case_glob_and_leading_dash(local, rg):
    (local[2] / "a.txt").write_text("-NEEDLE.*\n-needleX\n", encoding="utf-8")
    (local[2] / "b.py").write_text("-needle.*\n", encoding="utf-8")
    result = search(local, query="-needle.*", case_sensitive=False, file_pattern="*.txt")
    assert result["engine"] == "rg" and result["total_matches"] == 1
    assert result["matches"][0]["text"] == "-NEEDLE.*"


def test_rg_large_file_binary_and_non_utf8_coverage(local, rg):
    workspace = local[2]
    (workspace / "large.txt").write_bytes(b"x\n" * (600 * 1024) + b"needle\n")
    (workspace / "oversized.txt").write_bytes(b"x" * (8 * 1024 * 1024 + 1) + b"needle\n")
    (workspace / "binary.bin").write_bytes(b"\0needle\n")
    (workspace / "gbk.txt").write_bytes("中文 needle\n".encode("gbk"))
    result = search(local)
    assert result["engine"] == "rg"
    assert {Path(m["path"]).name for m in result["matches"]} == {"large.txt", "gbk.txt"}
    assert "UTF-8" in result["coverage"] and "GBK" in result["coverage"]


def test_rg_global_match_limit_5000(local, rg):
    for index in range(30):
        (local[2] / f"a{index:02d}.txt").write_text("needle\n" * 180, encoding="utf-8")
    result = search(local)
    assert result["engine"] == "rg" and result["total_matches"] == 5000
    assert result["incomplete"] and result["stopped_reason"] == "match_limit"


def test_rg_per_file_match_cap_is_disclosed(local, rg):
    (local[2] / "a.txt").write_text("needle\n" * 210, encoding="utf-8")
    result = search(local)
    assert result["engine"] == "rg" and result["total_matches"] == 200
    assert result["incomplete"] and result["stopped_reason"] == "match_limit"


def test_rg_single_file_search_returns_absolute_path(local, rg):
    (local[2] / "a.txt").write_text("needle\n", encoding="utf-8")
    result = search(local, path="a.txt")
    assert result["engine"] == "rg"
    assert result["matches"] == [{"path": str(local[2] / "a.txt"), "line": 1, "text": "needle"}]


def test_rg_searches_dotfiles_like_the_python_scan(local, rg):
    (local[2] / ".github").mkdir()
    (local[2] / ".github" / "ci.yml").write_text("needle\n", encoding="utf-8")
    result = search(local)
    assert result["engine"] == "rg"
    assert [m["path"] for m in result["matches"]] == [str(local[2] / ".github" / "ci.yml")]
