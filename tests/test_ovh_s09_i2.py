"""Overhaul S09-I2: Selected skills as user <skill> fragments, 8000-byte UTF-8 safe truncation, non-fatal integrity skip, AGENTS.md stop-citation rule, and 8-skill limit reporting."""
from __future__ import annotations

import json
from pathlib import Path

import pytest
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey

from rynmesh.agent_runtime import knowledge, projects, request_context
from rynmesh.agent_runtime.extensions import ExtensionStore
from rynmesh.agent_runtime.runtime import AgentRuntime
from rynmesh.agent_runtime.store import AgentError, AgentStore
from test_agent_extensions_runtime import _assistant, _Catalog, _ExtensionHarnessTools, _Model
from test_agent_projects import project_folder, project_runtime


def _make_ext_runtime(tmp_path: Path) -> tuple[AgentRuntime, _Model, _ExtensionHarnessTools]:
    home = tmp_path / "home"
    workspace = tmp_path / "workspace"
    home.mkdir()
    workspace.mkdir()
    key = X25519PrivateKey.generate()
    store = AgentStore(home / "agent", key)
    tools = _ExtensionHarnessTools(home, store)
    model = _Model()
    runtime = AgentRuntime(store, model, tools, _Catalog())
    runtime.update_settings(
        {
            "enabled": True,
            "model_id": "local:test",
            "workspace_root": str(workspace),
            "permission_mode": "workspace",
        }
    )
    return runtime, model, tools


def _install_skill(
    ext_store: ExtensionStore,
    skill_id: str,
    content: str,
    *,
    ref_files: dict[str, str] | None = None,
) -> Path:
    pkg_dir = ext_store.allocate_package_dir("skill", skill_id)
    pkg_dir.mkdir(parents=True, exist_ok=True)
    (pkg_dir / "SKILL.md").write_text(content, encoding="utf-8")
    for rel, body in (ref_files or {}).items():
        target = pkg_dir / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(body, encoding="utf-8")
    ext_store.commit_imported_items(
        plugins=[],
        skills=[
            {
                "id": skill_id,
                "name": skill_id,
                "title": skill_id,
                "description": f"Description for {skill_id}",
                "version": "1.0.0",
                "content": content,
                "package_dir": str(pkg_dir),
            }
        ],
        mcp_servers=[],
    )
    ext_store.set_skill_enabled(skill_id, True)
    return pkg_dir


def test_t1_selected_skills_are_user_skill_fragments_before_tagged_preferences_and_projects_stop_rule(tmp_path: Path):
    """T1: 选中技能为 user 角色且带 <skill> 标签，偏好是带标签的 user 数据；顺序为技能块在偏好前；AGENTS.md 块尾含停下引用规则（覆盖改动 1、2、4）。"""
    ext_store = ExtensionStore(tmp_path / "ext")
    _install_skill(
        ext_store,
        "audit-flow",
        "# Audit Flow\nCheck checklist first.",
        ref_files={"refs/checklist.md": "Checklist body"},
    )
    builtin_snap = ext_store.select_skills(["resource-report"])[0]
    imported_snap = ext_store.select_skills(["audit-flow"])[0]
    prefs = {"revision": 1, "items": [{"id": "lang", "text": "Reply concisely in Chinese."}]}

    msgs = knowledge.messages({"skills": [builtin_snap, imported_snap]}, prefs)
    assert len(msgs) == 3

    # First two messages are user <skill> fragments:
    assert msgs[0]["role"] == "user"
    assert msgs[0]["content"].startswith("<skill><name>resource-report</name><path>builtin://resource-report</path>\n")
    assert msgs[0]["content"].endswith("</skill>")
    assert "untrusted" not in msgs[0]["content"].lower().split("</path>", 1)[0]
    assert "Optional local guidance (untrusted, lower priority" not in msgs[0]["content"]
    assert ("cannot grant permissions" in msgs[0]["content"]) or ("技能不能授予权限" in msgs[0]["content"])
    assert "If you stop because of a rule in a Skill, name the skill and quote the rule." in msgs[0]["content"]

    assert msgs[1]["role"] == "user"
    assert msgs[1]["content"].startswith("<skill><name>audit-flow</name><path>SKILL.md</path>\n")
    assert msgs[1]["content"].endswith("</skill>")
    assert "ryn_read_skill_file" in msgs[1]["content"]
    assert "动手前先用 ryn_read_skill_file 读技能要求的参考文件" in msgs[1]["content"]
    assert "If you stop because of a rule in a Skill, name the skill and quote the rule." in msgs[1]["content"]

    # Third message is tagged host preferences data after the skill blocks.
    assert msgs[2]["role"] == "user" and msgs[2]["synthetic"] is True
    assert msgs[2]["content"].startswith('<host_note kind="preferences">')
    assert "Reply concisely in Chinese." in msgs[2]["content"]
    assert "audit-flow" not in msgs[2]["content"]

    # Step 2: build_context docstring & order (skills before preferences):
    assert "skills precede saved preferences" in (request_context.build_context.__doc__ or "")
    built = request_context.build_context(
        [{"role": "developer", "content": "sys"}],
        [{"role": "user", "content": "hello"}],
        guidance=msgs,
    )
    assert [m["role"] for m in built] == ["developer", "user", "user", "user", "user"]

    # Step 4: projects.guidance AGENTS.md block ends with stop-citation rule:
    root = project_folder(tmp_path / "proj_agents")
    (root / "AGENTS.md").write_text("# Team Rule\nNever touch legacy billing.\n", encoding="utf-8")
    p_runtime, _, _, _ = project_runtime(tmp_path / "proj_state")
    sel = p_runtime.load_project({"path": str(root)})
    created = p_runtime.create({"message": "Inspect", "project_id": sel["id"]})
    proj_row = p_runtime.store.read()["tasks"][created["id"]]
    rendered_guidance = projects.guidance(proj_row)
    assert "AGENTS.md" in rendered_guidance
    tail_after_instructions = rendered_guidance.split("</INSTRUCTIONS>", 1)[1]
    assert "AGENTS.md" in tail_after_instructions
    assert ("quote the rule" in tail_after_instructions) or ("写明文件和原文" in tail_after_instructions)


def test_t2_20kb_skill_truncated_to_8000_bytes_with_continuation_hint_and_legacy_snapshot_fallback(tmp_path: Path):
    """T2: 20 KB 技能被截到 8000 字节且有续读提示；旧快照长 content 渲染时同样截断（覆盖改动 1、3）。"""
    ext_store = ExtensionStore(tmp_path / "ext_20k")
    big_content = ("Step line: verify metrics carefully.\n" * 600)  # > 21 KB
    assert len(big_content.encode("utf-8")) > 20_000
    _install_skill(ext_store, "large-skill", big_content)

    snaps = ext_store.select_skills(["large-skill"])
    assert len(snaps) == 1
    snap = snaps[0]
    assert snap.get("truncated") is True
    assert len(snap["content"].encode("utf-8")) <= 8000
    assert len(snap["content"].encode("utf-8")) > 7950
    assert "ryn_read_skill_file" in json.dumps(snap, ensure_ascii=False)

    rendered = knowledge.messages({"skills": [snap]}, {"revision": 0, "items": []})
    assert len(rendered) == 2  # Skill fragment followed by the JSON skill-usage guidance.
    assert rendered[0]["role"] == "user"
    assert "ryn_read_skill_file" in rendered[0]["content"]
    assert "续读" in rendered[0]["content"]

    # Legacy snapshot with un-truncated 20 KB content is also truncated at render time:
    legacy_snap = {
        "id": "legacy-big",
        "version": "1.0.0",
        "install_generation": 1,
        "title": "Legacy Big",
        "description": "Legacy",
        "content": big_content,
    }
    legacy_rendered = knowledge.messages({"skills": [legacy_snap]}, {"revision": 0, "items": []})
    assert len(legacy_rendered[0]["content"].encode("utf-8")) < 9000
    assert "ryn_read_skill_file" in legacy_rendered[0]["content"]


def test_t3_multibyte_utf8_characters_are_not_split_at_8000_byte_boundary(tmp_path: Path):
    """T3: 多字节字符不被切坏（覆盖改动 1、3）。"""
    ext_store = ExtensionStore(tmp_path / "ext_utf8")
    # 7999 ASCII bytes + 3-byte Chinese characters + 4-byte emojis -> straddles byte 8000!
    tricky = ("a" * 7999) + ("测" * 500) + ("🚀" * 100)
    assert len(tricky.encode("utf-8")) > 9500
    _install_skill(ext_store, "utf8-skill", tricky)

    snap = ext_store.select_skills(["utf8-skill"])[0]
    assert snap.get("truncated") is True
    snap_bytes = snap["content"].encode("utf-8")
    assert len(snap_bytes) == 7999  # '测' starts at byte 7999 (needs 3 bytes -> 8002 > 8000), so cleanly stops at 7999!
    assert "\ufffd" not in snap["content"]

    # Also test 4-byte emoji straddling byte 8000 (7998 ASCII + 4-byte emoji = 8002 > 8000):
    tricky_emoji = ("b" * 7998) + ("🚀" * 400)
    _install_skill(ext_store, "emoji-skill", tricky_emoji)
    emoji_snap = ext_store.select_skills(["emoji-skill"])[0]
    assert emoji_snap.get("truncated") is True
    assert len(emoji_snap["content"].encode("utf-8")) == 7998
    assert "\ufffd" not in emoji_snap["content"]


def test_t4_modified_skill_package_is_skipped_with_warning_and_task_created_with_skill_skipped_event(tmp_path: Path):
    """T4: 包被改动时该技能被跳过、任务照常创建并有事件；显式点名的所有技能都失败时才报错（覆盖改动 3）。"""
    runtime, model, tools = _make_ext_runtime(tmp_path)
    ext_store = tools.extensions.store
    good_dir = _install_skill(ext_store, "good-skill", "# Good Skill\nProceed safely.")
    bad_dir = _install_skill(
        ext_store,
        "tampered-skill",
        "# Tampered Skill\nCheck ref.",
        ref_files={"refs/note.md": "original note"},
    )
    assert good_dir.is_dir()
    # Tamper with file inside tampered-skill package directory:
    (bad_dir / "refs" / "note.md").write_text("modified outside store!", encoding="utf-8")

    # 1. Store-level select_skills skips tampered-skill and reports warnings without raising:
    warnings_out: list[dict] = []
    selected = ext_store.select_skills(["good-skill", "tampered-skill"], warnings=warnings_out)
    assert [s["id"] for s in selected] == ["good-skill"]
    assert any(w.get("skill_id") == "tampered-skill" for w in warnings_out)

    # 2. Task creation with mixed valid + tampered explicit skills succeeds and records skill_skipped event:
    created = runtime.create(
        {
            "message": "Run task with good and tampered skills",
            "skill_ids": ["good-skill", "tampered-skill"],
        }
    )
    assert [s["id"] for s in created["skills"]] == ["good-skill"]
    skipped_events = [e for e in created["events"] if e["type"] == "skill_skipped"]
    assert len(skipped_events) == 1
    assert skipped_events[0]["skill_id"] == "tampered-skill"

    # 3. $mention of tampered-skill alone also skips without rejecting the task:
    created_mention = runtime.create({"message": "Please check $tampered-skill status"})
    assert created_mention["skills"] == []
    assert any(
        e["type"] == "skill_skipped" and e.get("skill_id") == "tampered-skill"
        for e in created_mention["events"]
    )

    # 4. When ALL explicitly passed skill_ids fail, task creation raises:
    with pytest.raises(AgentError, match="agent_skill_integrity_failed"):
        runtime.create(
            {
                "message": "Only tampered explicit skill",
                "skill_ids": ["tampered-skill"],
            }
        )


def test_t5_adding_two_skills_beyond_eight_emits_skills_limit_reached_and_informs_model_in_skill_tail(tmp_path: Path):
    """T5: 已有 8 个技能时再点名 2 个，事件列出这 2 个 id，任务技能仍是 8 个，且 <skill> 尾部告知模型（覆盖改动 1、5）。"""
    runtime, model, tools = _make_ext_runtime(tmp_path)
    ext_store = tools.extensions.store
    for i in range(1, 11):
        _install_skill(ext_store, f"custom-s{i}", f"# Custom Skill {i}\nFollow rule {i}.")

    initial_eight = [f"custom-s{i}" for i in range(1, 9)]
    model.responses = [
        _assistant(content="Initial turn done."),
        _assistant(content="Second turn done."),
    ]
    created = runtime.create({"message": "Start with 8 skills", "skill_ids": initial_eight})
    tid = created["id"]
    assert [s["id"] for s in created["skills"]] == initial_eight
    runtime.run_once()
    done_first = runtime.get(tid)
    assert done_first["status"] == "completed"

    # Mention 2 more skills ($custom-s9 and $custom-s10) in follow-up message:
    updated = runtime.add_message(
        tid,
        {
            "message": "Now also apply $custom-s9 and $custom-s10 please.",
            "expected_control_revision": done_first["control_revision"],
        },
    )
    assert len(updated["skills"]) == 8
    assert [s["id"] for s in updated["skills"]] == initial_eight

    limit_events = [e for e in updated["events"] if e["type"] == "skills_limit_reached"]
    assert len(limit_events) == 1
    assert limit_events[0]["dropped_skill_ids"] == ["custom-s9", "custom-s10"]

    # Run second turn and verify model context has <skill> fragment tail listing custom-s9 & custom-s10:
    captured_msgs: list[list[dict]] = []
    orig_complete = model.complete

    def spy_complete(model_id, messages, tool_specs, cancelled, on_event, **kwargs):
        captured_msgs.append(messages)
        return orig_complete(model_id, messages, tool_specs, cancelled, on_event, **kwargs)

    model.complete = spy_complete
    runtime.run_once()
    assert len(captured_msgs) == 1
    skill_user_msgs = [
        m for m in captured_msgs[0]
        if m.get("role") == "user" and isinstance(m.get("content"), str) and m["content"].startswith("<skill>")
    ]
    assert len(skill_user_msgs) == 8
    last_skill_content = skill_user_msgs[-1]["content"]
    assert "custom-s9" in last_skill_content
    assert "custom-s10" in last_skill_content
