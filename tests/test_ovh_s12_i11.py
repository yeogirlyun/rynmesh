"""S12-I11 stable cwd/shell context for project and ordinary tasks."""
import shutil
from types import SimpleNamespace

import pytest
from test_agent_runtime import make_runtime, assistant, advance
from rynmesh.agent_runtime import projects


def test_t1_ordinary_workspace_gets_environment_without_project_or_instructions():
    row = {"local_execution": {"workspace_root": "C:/work/ordinary"}}
    text = projects.guidance(row)
    assert "<environment_context>" in text and "<cwd>C:/work/ordinary</cwd>" in text
    assert "<shell>" in text and "<project>" not in text and "<INSTRUCTIONS>" not in text


def test_t1_empty_cwd_is_omitted_but_default_shell_is_present():
    text = projects.guidance({"local_execution": {"workspace_root": ""}})
    assert "<environment_context>" in text and "<shell>" in text and "<cwd>" not in text


@pytest.mark.parametrize("platform,pwsh,expected", [("nt", True, "pwsh"), ("nt", False, "powershell"), ("posix", True, "sh")])
def test_t2_default_shell_uses_quick_executable_selection_without_starting_processes(platform, pwsh, expected, monkeypatch):
    monkeypatch.setattr(projects, "os", SimpleNamespace(name=platform))
    checked = []
    def which(name):
        checked.append(name)
        return "C:/PowerShell/pwsh.exe" if name == "pwsh" and pwsh else None
    monkeypatch.setattr(shutil, "which", which)
    def no_process(*args, **kwargs):
        pytest.fail("guidance started an interpreter/version probe")
    monkeypatch.setattr(projects.subprocess, "run", no_process)
    monkeypatch.setattr(projects.subprocess, "Popen", no_process)
    text = projects.guidance({"local_execution": {"workspace_root": "/workspace"}})
    assert f"<shell>{expected}</shell>" in text
    assert checked == (["pwsh"] if platform == "nt" else [])


def test_t1_project_metadata_and_instructions_keep_existing_layers_with_shell():
    row = {"project": {"name": "Project", "path": "/project", "is_git": False,
        "entry_files": ["AGENTS.md"], "instructions": {"text": "Use project conventions.", "bytes": 24}},
        "local_execution": {"workspace_root": "/project"}}
    text = projects.guidance(row)
    assert "<cwd>/project</cwd>" in text and "<shell>" in text and "<project>" in text
    assert text.index("</environment_context>") < text.index("<INSTRUCTIONS>")
    assert "Use project conventions." in text


def test_second_step_does_not_put_live_subagents_into_the_environment_prefix():
    row = {"local_execution": {"workspace_root": "/workspace"}, "subagents": ["one"]}
    before = projects.guidance(row)
    assert "<environment_context>" in before
    row["subagents"] = ["two", "three"]
    row["notifications"] = ["agent finished"]
    assert projects.guidance(row) == before
    assert "subagent" not in before and "agent finished" not in before


def test_t1_real_nonproject_model_request_receives_workspace_and_shell(tmp_path):
    runtime, model, _, _ = make_runtime(tmp_path, assistant("Environment observed."))
    created = runtime.create({"message": "Report the current working directory and default shell."})
    advance(runtime)
    text = "\n".join(m.get("content", "") or "" for m in model.calls[0][1])
    root = created["local_execution"]["workspace_root"]
    assert "<environment_context>" in text and f"<cwd>{root}</cwd>" in text and "<shell>" in text
