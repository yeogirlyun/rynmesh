"""Exercise the actual dispatcher preflight without credentials or remote writes."""
import json
import subprocess
import textwrap
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def preflight(tmp_path, monkeypatch, *, tags=(), draft=None, error="release not found", ancestor=True):
    workflow = (ROOT / ".github/workflows/publish-release.yml").read_text(encoding="utf-8")
    step = workflow.split("        id: source\n", 1)[1].split("      - name:", 1)[0]
    code = textwrap.dedent(step.split("        run: |\n", 1)[1])
    monkeypatch.chdir(tmp_path)
    config = tmp_path / "webapp/src-tauri/tauri.conf.json"
    config.parent.mkdir(parents=True)
    config.write_text(json.dumps({"version": "0.9.0"}))
    output = tmp_path / "output"
    monkeypatch.setenv("VERSION", "0.9.0")
    monkeypatch.setenv("GITHUB_OUTPUT", str(output))
    calls = []

    def run(args, **kwargs):
        calls.append(args)
        if args[:3] == ["git", "tag", "--list"]:
            return subprocess.CompletedProcess(args, 0, "\n".join(tags), "")
        if args[:3] == ["gh", "release", "view"]:
            return subprocess.CompletedProcess(args, 0 if draft is not None else 1,
                                               json.dumps({"isDraft": draft}), error)
        if args[:3] == ["git", "merge-base", "--is-ancestor"] and not ancestor:
            raise subprocess.CalledProcessError(1, args)
        return subprocess.CompletedProcess(args, 0, "", "")

    monkeypatch.setattr(subprocess, "run", run)
    return code, calls, output


def test_retry_checks_out_original_tag_and_validates_versions(tmp_path, monkeypatch):
    code, calls, output = preflight(tmp_path, monkeypatch, tags=["v0.9.0"], draft=True)
    with pytest.raises(SystemExit) as stopped:
        exec(code, {})
    assert stopped.value.code == 0
    assert ["git", "checkout", "--detach", "v0.9.0"] in calls
    assert ["python", "scripts/set_release_version.py", "0.9.0", "--check"] in calls
    assert output.read_text() == "resume=true\n"


@pytest.mark.parametrize("draft,error,ancestor,expected", [
    (False, "", True, AssertionError),
    (None, "HTTP 403", True, RuntimeError),
    (True, "", False, subprocess.CalledProcessError),
])
def test_retry_fails_closed_before_checkout(tmp_path, monkeypatch, draft, error, ancestor, expected):
    code, calls, output = preflight(tmp_path, monkeypatch, tags=["v0.9.0"], draft=draft,
                                    error=error, ancestor=ancestor)
    with pytest.raises(expected):
        exec(code, {})
    assert not output.exists()
    assert not any(args[:2] == ["git", "checkout"] for args in calls)


def test_new_version_uses_normal_prepare_path(tmp_path, monkeypatch):
    code, calls, output = preflight(tmp_path, monkeypatch, tags=["v0.8.0"])
    exec(code, {})
    assert not output.exists()
    assert not any(args[:2] == ["git", "checkout"] for args in calls)
