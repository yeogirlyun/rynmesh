"""S09-I10: builtin review skill, real selection and project guidance."""
import json

import pytest
from test_agent_extensions_runtime import ext_env  # noqa: F401
from test_agent_projects import project_folder, project_runtime
from test_agent_runtime import assistant

from rynmesh.agent_runtime import knowledge, projects
from rynmesh.agent_runtime.extensions.store import ExtensionStore

REVIEW = ("Review: list problems the change introduced, most serious first, each with [P0-P3], "
          "file:line and the triggering input; then open questions. Say so if none.")


@pytest.fixture(autouse=True)
def isolated_discovery(monkeypatch):
    monkeypatch.setattr("rynmesh.agent_runtime.extensions.codex_skills.default_local_skill_roots", lambda *args, **kwargs: [])


def test_t1_catalog_has_version_one_review_metadata_without_eager_body():
    item = next(item for item in knowledge.catalog() if item["id"] == "code-review")
    assert item["version"] == "1" and "review" in item["description"].lower()
    assert "content" not in item


def test_t2_selected_fragment_contains_evidence_and_reporting_rules():
    skill = knowledge.select(["code-review"])[0]
    assert skill["content"].startswith("审查别人的改动；项目或 owner 给的具体准则优先")
    message = knowledge.messages({"skills": [skill]}, {"revision": 0, "items": []})[0]
    assert message["role"] == "user" and message["content"].startswith("<skill><name>code-review</name><path>builtin://code-review</path>")
    for rule in ("merge-base", "[P0-P3]", "file:line", "triggering input", "introduced", "actionable", "author would fix",
                 "affected code", "cause", "3 lines", "open questions", "no findings"):
        assert rule in message["content"]
    assert "cannot grant permissions" in message["content"]


def test_t2_real_dollar_selection_dispatches_body_but_catalog_alone_does_not(ext_env):
    runtime, model, _, _, _ = ext_env
    first = runtime.create({"message": "Inspect available skills"})
    runtime.run_once()
    assert runtime.get(first["id"])["skills"] == []
    assert "code-review" in json.dumps(model.calls[-1][1])
    assert not any("审查别人的改动" in str(item.get("content", "")) for item in model.calls[-1][1])
    selected = runtime.create({"message": "$code-review Review this patch"})
    assert [item["id"] for item in runtime.get(selected["id"])["skills"]] == ["code-review"]
    runtime.run_once()
    fragment = next(item for item in model.calls[-1][1] if str(item.get("content", "")).startswith("<skill><name>code-review</name>"))
    assert "[P0-P3]" in fragment["content"] and "merge-base" in fragment["content"]


@pytest.mark.parametrize("selected_project", [True, False])
def test_t3_real_project_context_has_review_guidance_only_for_project_tasks(tmp_path, selected_project):
    runtime, model, _, _ = project_runtime(tmp_path / "state")
    model.responses.append(assistant("Review complete."))
    original_complete = model.complete
    model.complete = lambda *args, **kwargs: original_complete(*args)
    options = {"message": "Review the proposed changes"}
    if selected_project:
        root = project_folder(tmp_path)
        project = runtime.load_project({"path": str(root)})
        options["project_id"] = project["id"]
    row = runtime.create(options)
    stored = runtime.store.read()["tasks"][row["id"]]
    guidance = projects.guidance(stored)
    assert (REVIEW in guidance) is selected_project
    if selected_project:
        assert guidance.index("</environment_context>") < guidance.index(REVIEW)
    runtime.run_once()
    assert any(REVIEW in str(item.get("content", "")) for item in model.calls[0][1]) is selected_project


def test_t3_plain_workspace_guidance_does_not_gain_a_project_review_rule(tmp_path):
    row = {"local_execution": {"workspace_root": str(tmp_path)},
           "workspace_instructions": {"root": str(tmp_path), "files": []}}
    assert REVIEW not in projects.guidance(row)


def test_t1_default_and_legacy_extension_state_enable_new_review_skill(ext_env):
    _, _, tools, workspace, _ = ext_env
    store = tools.extensions.store
    defaults = store._default_state()
    assert defaults["builtin_skills_enabled"]["code-review"] is True
    assert defaults["builtin_skills_revisions"]["code-review"] == 1
    legacy = store._read()
    legacy["builtin_skills_enabled"].pop("code-review", None)
    legacy["builtin_skills_revisions"].pop("code-review", None)
    legacy["builtin_skills_enabled"]["project-development"] = False
    legacy["builtin_skills_revisions"]["project-development"] = 7
    store._write(legacy)
    reopened = ExtensionStore(store.root, store.key)
    state = reopened._read()
    assert state["builtin_skills_enabled"]["code-review"] is True
    assert state["builtin_skills_revisions"]["code-review"] == 1
    assert state["builtin_skills_enabled"]["project-development"] is False
    assert state["builtin_skills_revisions"]["project-development"] == 7
    enabled = {item["id"] for item in reopened.enabled_skills_catalog(workspace_root=workspace)}
    assert "code-review" in enabled and "project-development" not in enabled
    state["builtin_skills_enabled"]["code-review"] = False
    reopened._write(state)
    assert "code-review" not in {item["id"] for item in reopened.enabled_skills_catalog(workspace_root=workspace)}
