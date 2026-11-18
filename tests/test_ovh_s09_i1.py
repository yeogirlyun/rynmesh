"""S09-I1: per-turn skill discovery, explicit selection and durable activation."""
import copy

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from test_agent_extensions_runtime import ext_env, _assistant
from rynmesh.agent_runtime.extensions.routes import install_extension_routes
from rynmesh.agent_runtime.extensions.adapter import READ_SKILL_FILE_TOOL
from rynmesh.agent_runtime.extensions.store import ExtensionStore


@pytest.fixture(autouse=True)
def isolated_discovery(monkeypatch):
    from rynmesh.agent_runtime.extensions import codex_skills
    monkeypatch.setattr(codex_skills, "default_local_skill_roots", lambda *a, **kw: [])


def stored(runtime, created):
    return runtime.store.inspect(lambda data: copy.deepcopy(data["tasks"][created["id"]]))


def seed(env, count=1):
    store = env[2].extensions.store
    state = store._read()
    for i in range(count):
        sid = f"s09-demo-{i}"
        state["skills"][sid] = dict(id=sid, name=sid, title=sid, version="1", enabled=True,
            description=f"CATALOG_DESCRIPTION_{i}", content=f"SNAPSHOT_CONTENT_{i}", scope="user",
            policy={"allow_implicit_invocation": i != 1}, files=[])
    store._write(state)
    return store


def test_T1_catalog_metadata_real_developer_context_and_explicit_policy(ext_env):
    runtime, model, tools, workspace, _ = ext_env
    store = seed(ext_env, 2)
    catalog = {s["id"]: s for s in store.enabled_skills_catalog(workspace_root=workspace)}
    assert catalog["s09-demo-0"]["scope"] == "user"
    assert catalog["s09-demo-0"]["plugin_id"] is None
    assert catalog["s09-demo-1"]["policy"]["allow_implicit_invocation"] is False
    assert all("scope" in s and "policy" in s and "plugin_id" in s for s in catalog.values())
    created = runtime.create({"message": "Inspect without naming any skill"})
    runtime.run_once()
    text = str(model.calls[-1][1])
    assert "CATALOG_DESCRIPTION_0" in text
    assert "CATALOG_DESCRIPTION_1" not in text
    selected = runtime._resolve_skills([], "$s09-demo-1", workspace_root=str(workspace))
    assert [s["id"] for s in selected] == ["s09-demo-1"]
    assert runtime.get(created["id"])["skills"] == []


def test_T2_stable_scope_order_and_description_limit(ext_env, monkeypatch):
    adapter = ext_env[2].extensions
    rows = [dict(id=f"{scope}-demo", scope=scope, description="字" * 1500, policy={})
            for scope in ("plugin", "user", "repo", "system")]
    monkeypatch.setattr(adapter.store, "enabled_skills_catalog", lambda **kw: copy.deepcopy(rows))
    monkeypatch.setattr(adapter, "plugin_catalog", lambda **kw: [])
    note = adapter.skills_context_note({}, budget_bytes=40000, selected_skill_ids=[])
    assert note.encode() == adapter.skills_context_note({}, budget_bytes=40000, selected_skill_ids=[]).encode()
    assert [line.split(":")[0] for line in note.splitlines() if line.startswith("- ")] == [
        "- system-demo", "- repo-demo", "- user-demo", "- plugin-demo"]
    assert "字" * 1025 not in note
    assert "技能匹配任务时先用 ryn_read_skill_file" in note


def test_T3_shrink_descriptions_before_omitting_and_count(ext_env, monkeypatch):
    adapter = ext_env[2].extensions
    rows = [dict(id=f"skill-{i:02}", scope="user", description="文" * 1024, policy={}) for i in range(20)]
    monkeypatch.setattr(adapter.store, "enabled_skills_catalog", lambda **kw: rows)
    full_ids = adapter.skills_context_note({}, budget_bytes=600, selected_skill_ids=[])
    assert len(full_ids.encode()) <= 600
    assert all(f"skill-{i:02}" in full_ids for i in range(20))
    small = adapter.skills_context_note({}, budget_bytes=290, selected_skill_ids=[])
    assert len(small.encode()) <= 290
    lines = small.splitlines()
    count = sum(line.startswith("- skill-") for line in lines)
    assert f"- {20-count} more skills omitted" in small
    assert all("文" not in line for line in lines if line.startswith("- "))


def test_T4_plugin_index_prioritizes_ids_and_catalog_names(ext_env, monkeypatch):
    adapter = ext_env[2].extensions
    row = dict(name="Demo plugin", description="LONG_DESCRIPTION" * 100, skill_ids=["plugin-skill"],
               skills=[dict(id="plugin-skill", description="UNIQUE_SKILL_DESCRIPTION")], unavailable=[])
    monkeypatch.setattr(adapter, "plugin_overview_for_model", lambda context: [copy.deepcopy(row)])
    note = adapter.plugin_context_note({}, selected_skill_ids=[])
    assert "Demo plugin: Skills: plugin-skill" in note
    assert "UNIQUE_SKILL_DESCRIPTION" not in note
    monkeypatch.setattr(adapter.store, "enabled_skills_catalog", lambda **kw: [
        dict(id="plugin-skill", scope="plugin", plugin_id="demo", description="skill description", policy={})])
    monkeypatch.setattr(adapter, "plugin_catalog", lambda **kw: [dict(id="demo", name="Demo plugin")])
    assert "(plugin: Demo plugin)" in adapter.skills_context_note({}, budget_bytes=8000, selected_skill_ids=[])


@pytest.mark.parametrize("initial_count", [0, 8])
def test_T5_real_read_activates_snapshot_and_caps_eight(ext_env, initial_count):
    runtime, model, tools, workspace, _ = ext_env
    seed(ext_env, 10)
    model.responses = [_assistant(name=READ_SKILL_FILE_TOOL, args={"skill_id": "s09-demo-9", "path": "SKILL.md"}, call_id="read-skill"), _assistant("done")]
    created = runtime.create({"message": "Read workflow", "skill_ids": [f"s09-demo-{i}" for i in range(initial_count)]})
    runtime.run_once()
    names = [s["function"]["name"] for s in model.calls[0][2]]
    assert READ_SKILL_FILE_TOOL in names
    for _ in range(5):
        runtime.run_once()
        if runtime.get(created["id"])["status"] == "completed":
            break
    done = runtime.get(created["id"])
    assert any(e["type"] == "tool_result" and e.get("tool") == READ_SKILL_FILE_TOOL and not e.get("result", {}).get("error") for e in done["events"]), done["events"]
    assert len(done["skills"]) == (8 if initial_count else 1)
    if not initial_count:
        assert done["skills"][0]["id"] == "s09-demo-9"
        assert stored(runtime, created)["skills"][0]["content"] == "SNAPSHOT_CONTENT_9"
    else:
        assert any(e["type"] == "skills_limit_reached" for e in done["events"])


def test_settings_routes_persist_and_disable_only_catalog(ext_env):
    runtime, model, tools, workspace, _ = ext_env
    store = seed(ext_env)
    app = FastAPI()
    calls = []
    install_extension_routes(app, runtime=runtime, extensions=tools.extensions, local_control=lambda req: calls.append(req.method))
    client = TestClient(app)
    path = "/api/local/agent/extensions/skills/settings"
    assert client.get(path).json()["skills_catalog_in_context"] is True
    response = client.patch(path, json={"skills_catalog_in_context": False})
    assert response.status_code == 200
    assert response.json()["skills_catalog_in_context"] is False
    assert store.overview()["skills_catalog_in_context"] is False
    assert ExtensionStore(store.root, store.key).skills_settings()["skills_catalog_in_context"] is False
    assert client.patch(path, json={"skills_catalog_in_context": "false"}).status_code == 400
    created = runtime.create({"message": "$s09-demo-0"})
    runtime.run_once()
    assert "CATALOG_DESCRIPTION_0" not in runtime._skills_note(stored(runtime, created))
    assert runtime.get(created["id"])["skills"][0]["id"] == "s09-demo-0"
    assert calls == ["GET", "PATCH", "PATCH"]


def test_context_catalog_budget_uses_capacity_and_fallback(ext_env, monkeypatch):
    runtime, _, tools, workspace, _ = ext_env
    created = runtime.create({"message": "inspect"})
    created = stored(runtime, created)
    budgets = []
    monkeypatch.setattr(tools.extensions, "skills_context_note", lambda context, **kw: budgets.append(kw["budget_bytes"]) or "skills")
    monkeypatch.setattr(runtime, "_effective_capacity", lambda row: (3000000, None))
    monkeypatch.setattr(runtime, "_context_policy", lambda row: {"context_window_known": True})
    assert runtime._skills_note(created) == "skills"
    assert budgets[-1] == 40000
    monkeypatch.setattr(runtime, "_effective_capacity", lambda row: (100000, None))
    runtime._skills_note(created)
    assert budgets[-1] == 2000
    monkeypatch.setattr(runtime, "_context_policy", lambda row: {})
    runtime._skills_note(created)
    assert budgets[-1] == 8000


def test_T5_builtin_only_catalog_still_exposes_reader(ext_env):
    runtime, model, _, _, _ = ext_env
    created = runtime.create({"message": "Inspect"})
    runtime.run_once()
    assert not runtime.get(created["id"])["skills"]
    assert READ_SKILL_FILE_TOOL in [spec["function"]["name"] for spec in model.calls[0][2]]


def test_workspace_catalog_and_read_activation_are_scoped(ext_env, tmp_path):
    runtime, _, tools, workspace, _ = ext_env
    store = seed(ext_env)
    state = store._read()
    state["skills"]["s09-demo-0"].update(scope="repo", workspace_roots=[str(workspace)])
    store._write(state)
    own = tools.extensions.skills_context_note({"local_execution": {"workspace_root": str(workspace)}}, budget_bytes=8000, selected_skill_ids=[])
    other = tools.extensions.skills_context_note({"local_execution": {"workspace_root": str(tmp_path / 'other')}}, budget_bytes=8000, selected_skill_ids=[])
    assert "s09-demo-0" in own
    assert "s09-demo-0" not in other



def test_T5_failed_read_does_not_activate(ext_env):
    runtime, model, tools, workspace, _ = ext_env
    seed(ext_env)
    model.responses = [_assistant(name=READ_SKILL_FILE_TOOL,
        args={"skill_id": "s09-demo-0", "path": "references/missing.md"}, call_id="bad-read"), _assistant("done")]
    created = runtime.create({"message": "Read workflow reference"})
    for _ in range(5):
        runtime.run_once()
    done = runtime.get(created["id"])
    assert not done["skills"]
    assert any(e["type"] == "tool_result" and e.get("result", {}).get("error") for e in done["events"])


def test_T5_queued_followup_preserves_skill_ids_until_dispatch(ext_env):
    runtime, _, _, _, _ = ext_env
    seed(ext_env)
    created = runtime.create({"message": "Original request"})
    queued = runtime.queue_message(created["id"], {"message": "Use workflow next", "skill_ids": ["s09-demo-0"]})
    item = queued["queued_messages"][0]
    assert item["skill_ids"] == ["s09-demo-0"]
    edited = runtime.update_queued(created["id"], item["id"], {"message": "Use workflow after this turn"})
    assert edited["queued_messages"][0]["skill_ids"] == ["s09-demo-0"]
    done = runtime.update_queued(created["id"], item["id"], {"send_now": True})
    assert done["skills"][0]["id"] == "s09-demo-0"
