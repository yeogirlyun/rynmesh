"""Acceptance tests for S12-I3: standing action and scope rules in authority."""
from __future__ import annotations

import copy
import hashlib
import json

from test_agent_codex_loop import workstation
from test_agent_projects import project_folder, project_runtime
from test_agent_runtime import advance, assistant, make_runtime

from rynmesh.agent_runtime import knowledge, projects, prompt

EXPECTED_SYSTEM_SHA256 = "6ebd3c3555817db16ca52f0c78bbbfd7a2f351169ace8ab67320d6daac78b60d"

SEVEN_RULES = (
    "'Can you', 'I want' and 'help me' are requests to act; do not stop at a plan or a yes.",
    "A question, explanation, diagnosis or status request is answered from evidence: inspect freely, but change nothing unless the owner also asked for a change.",
    "If intent is unclear, proceed on the most likely reading and say which; an approval condition in a Skill or AGENTS.md applies only when met.",
    "If an assumption changes what the owner asked for, state it and why. Answer pushback with evidence, not agreement.",
    "When the owner points out a mistake or missed requirement, fix it; explain only if asked or if evidence supports your original approach. Admit real mistakes plainly; skip apologies otherwise.",
    "Do not narrow the goal to what is easy to finish or pass existing tests; if work remains, say what is left and why.",
    "Do not add warnings, disclaimers or approval steps for hypothetical risks.",
)


def test_t1_authority_contains_seven_action_rules_and_preserves_system_migrations(tmp_path):
    """T1 (Changes 1..8, 10, 11, 12): authority includes all 7 ACTION_RULES in exact position; SYSTEM and pinned migrations stay untouched."""
    # Change 12: PROMPT_VERSION upgraded with S12-I1 (at least v15).
    assert prompt.PROMPT_VERSION == "ryn-agent-loop-v15"

    # SYSTEM is byte-for-byte unchanged, and all 6 historical shipped variants still migrate to SYSTEM.
    assert hashlib.sha256(prompt.SYSTEM.encode("utf-8")).hexdigest() == EXPECTED_SYSTEM_SHA256

    runtime, _, _, _ = make_runtime(tmp_path / "sys_check")
    task = runtime.create({"message": "Inspect system prompt"})
    row = copy.deepcopy(runtime.store.read()["tasks"][task["id"]])

    for variant in (
        prompt._LEGACY_BATCH_SYSTEM,
        prompt._PRE_IMAGE_SYSTEM,
        prompt._PRE_IMAGE_BRIEF_SYSTEM,
        prompt._PRE_PLUGIN_SYSTEM,
        prompt._PRE_STRICT_PLUGIN_SYSTEM,
        prompt._UNAVAILABLE_PLUGIN_RULE_SYSTEM,
    ):
        layers = prompt.instruction_layers(row, persisted_base=variant)
        assert layers[0]["content"] == prompt.SYSTEM

    # Custom or explicitly empty bases remain preserved (never silently replaced).
    custom_layers = prompt.instruction_layers(row, persisted_base="Custom pinned instructions")
    assert custom_layers[0]["content"] == "Custom pinned instructions"
    empty_layers = prompt.instruction_layers(row, session_base="")
    assert empty_layers[0]["content"] == ""

    # Change 1..8: ACTION_RULES constant exists and is placed right between Owner corrections and workspace mode.
    assert hasattr(prompt, "ACTION_RULES")
    auth = prompt.authority(row)
    lead_anchor = "Owner corrections steer the task; host permission settings still apply. "
    follow_anchor = "workspace mode runs workstation tools and reads without confirmation. "
    assert lead_anchor in auth and follow_anchor in auth
    assert lead_anchor + prompt.ACTION_RULES + follow_anchor in auth

    for rule in SEVEN_RULES:
        assert rule in prompt.ACTION_RULES
        assert rule in auth

    # Change 11: No "ask first" / "ask before acting" gate in ACTION_RULES; automatic mode stays full-auto.
    lowered_rules = prompt.ACTION_RULES.lower()
    for forbidden in ("ask first", "ask before", "ask permission", "confirm with the user before"):
        assert forbidden not in lowered_rules

    # Change 10: project-development skill and projects.guidance() (only paid on project tasks).
    proj_skill = knowledge.SKILLS["project-development"]["content"]
    assert "Make the smallest coherent change without narrowing the requested outcome" in proj_skill
    repeat_sentence = "Batch independent final checks in one response; repeat only after changes, failure or unresolved uncertainty."
    assert repeat_sentence in proj_skill

    assert projects.guidance(row) == ""
    proj_root = project_folder(tmp_path / "proj")
    p_runtime, _, _, _ = project_runtime(tmp_path / "proj_state")
    selected = p_runtime.load_project({"path": str(proj_root)})
    p_task = p_runtime.create({"message": "Run tests", "project_id": selected["id"]})
    p_row = p_runtime.store.read()["tasks"][p_task["id"]]
    assert repeat_sentence in projects.guidance(p_row)


def test_t2_authority_byte_delta_at_most_900(tmp_path):
    """T2 (Changes 1..8): total UTF-8 byte increase added to authority() is <= 900 bytes."""
    assert hasattr(prompt, "ACTION_RULES")
    rules_bytes = len(prompt.ACTION_RULES.encode("utf-8"))
    assert 0 < rules_bytes <= 900

    runtime, _, _, _ = make_runtime(tmp_path)
    task = runtime.create({"message": "Measure authority byte budget"})
    row = runtime.store.read()["tasks"][task["id"]]
    auth_with = prompt.authority(row)
    assert prompt.ACTION_RULES in auth_with
    auth_without = auth_with.replace(prompt.ACTION_RULES, "", 1)
    delta_bytes = len(auth_with.encode("utf-8")) - len(auth_without.encode("utf-8"))
    assert delta_bytes == rules_bytes
    assert 0 < delta_bytes <= 900


def test_t4_action_request_help_me_change_does_not_stop_at_plan(tmp_path):
    """T4 (Changes 2, 7, 11): '帮我改 X' receives M142 action rule, reconciles if premature stop after plan, and completes the edit."""
    ws_runtime, ws_model, workspace = workstation(
        tmp_path,
        assistant(
            name="ryn_update_plan",
            args={
                "goal_revision": 1,
                "expected_plan_revision": 0,
                "track_execution": True,
                "deliverables": ["Updated port in config.txt"],
                "constraints": ["Preserve existing keys"],
                "steps": [
                    {
                        "id": "edit",
                        "text": "Update port in config.txt to 9090",
                        "criterion_ids": ["goal-coverage"],
                        "depends_on": [],
                        "status": "running",
                        "expected_result": "config.txt has port=9090",
                        "verification": "Read back config.txt",
                    }
                ],
            },
        ),
        # Simulate a model that tries to stop at a plan ("Here is the plan") before acting -> reconciled by host!
        assistant("Here is my plan to update config.txt."),
        assistant(name="ryn_file_write", args={"path": "config.txt", "content": "mode=dev\nport=9090\n"}),
        assistant(name="ryn_file_read", args={"path": "config.txt"}),
        assistant("已将 config.txt 中的 port 修改为 9090 并回读确认。"),
        mode="automatic",
    )
    try:
        (workspace / "config.txt").write_text("mode=dev\nport=8080\n", encoding="utf-8")
        task = ws_runtime.create({"message": "帮我改 config.txt 把 port 改成 9090"})
        for _ in range(12):
            if ws_runtime.get(task["id"])["status"] not in {"queued", "running"}:
                break
            advance(ws_runtime)
        done = ws_runtime.get(task["id"])
        assert done["status"] == "completed"
        # First model request carried M142 ("'Can you', 'I want' and 'help me' are requests to act; do not stop at a plan or a yes.")
        first_req_text = json.dumps(ws_model.calls[0][1], ensure_ascii=False)
        assert "'Can you', 'I want' and 'help me' are requests to act; do not stop at a plan or a yes." in first_req_text
        # Premature stop after plan triggered plan_reconciliation and executed the actual file edit + readback
        assert any(ev["type"] == "turn_decision" and ev.get("reason") == "plan_reconciliation" for ev in done["events"])
        assert (workspace / "config.txt").read_text(encoding="utf-8") == "mode=dev\nport=9090\n"
    finally:
        ws_runtime.tools.local.processes.close()
