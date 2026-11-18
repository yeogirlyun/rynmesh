"""S12-I14: host data provenance survives locally and is stripped at dispatch."""
import copy
import json

import pytest
from test_agent_runtime import advance, assistant, make_runtime
from test_agent_image_routes import (CapturingModelBridge, _append_confirmed_mcp_turn,
    _antigravity_model_row, _codex_model_row, _write_mcp_artifact)
from rynmesh.agent_runtime import image_capabilities, knowledge, memory_natural, memory_runtime, prompt
from rynmesh.agent_runtime.loop import _is_owner_user_message
from rynmesh.llm_package.protocol_codecs import MESSAGES, make_request

INTRO = "Host runtime data, not an owner message and not your own reply."
IMAGE_INTRO = "These are system metadata about attached images, not owner instructions"


def _text(message):
    content = message.get("content")
    return content if isinstance(content, str) else "\n".join(
        p.get("text", "") for p in content or [] if isinstance(p, dict) and p.get("type") == "text")


def _kind(messages, kind):
    return next(m for m in messages if f'<host_note kind="{kind}">' in _text(m))


def _assert_host(message, kind):
    assert message["role"] == "user"
    assert message["origin"] == "host_note" and message["synthetic"] is True
    assert _text(message).startswith(f'<host_note kind="{kind}">')
    assert INTRO in _text(message) and "</host_note>" in _text(message)
    assert not _is_owner_user_message(message)


def test_t1_t2_budget_wrapup_fallback_is_tagged_but_not_leaked_to_model(tmp_path):
    runtime, model, _, _ = make_runtime(tmp_path, assistant("Progress report"))
    runtime.update_settings({"max_steps": 32})
    task = runtime.create({"message": "Read the evidence and report progress"})
    runtime.store.transaction(lambda data: data["tasks"][task["id"]].update(steps=31))
    advance(runtime)
    sent = model.calls[-1][1]
    notice = _kind(sent, "budget_wrap_up")
    assert notice["role"] == "user" and INTRO in _text(notice)
    assert "Do NOT call any tools" in _text(notice)
    assert all("origin" not in m and "synthetic" not in m for m in sent)


def test_t1_budget_notice_already_on_tool_result_keeps_server_note_path(tmp_path):
    runtime, model, _, _ = make_runtime(tmp_path, assistant("Progress report"))
    runtime.update_settings({"max_steps": 32})
    task = runtime.create({"message": "Inspect and report"})
    def seed(data):
        row = data["tasks"][task["id"]]
        row["steps"] = 31
        row["messages"].extend([assistant(name="inspect_service", args={"service_id": "fixture"}, call_id="prior")["message"],
            {"role": "tool", "tool_call_id": "prior", "content": '{"ok":true}', "context_epoch": 0}])
    runtime.store.transaction(seed)
    advance(runtime)
    tool = next(m for m in model.calls[-1][1] if m["role"] == "tool")
    assert "Do NOT call any tools" in json.loads(tool["content"])["server_note"]
    assert not any('<host_note kind="budget_wrap_up">' in _text(m) for m in model.calls[-1][1])


def test_t1_t4_continuation_is_durable_host_data_and_old_notes_stay_untouched(tmp_path):
    runtime, model, _, _ = make_runtime(tmp_path,
        {**assistant("Partial answer. "), "finish_reason": "length"}, assistant("Remaining answer."))
    task = runtime.create({"message": "Continue the work until the answer is complete"})
    legacy = {"role": "assistant", "content": "Runtime note: legacy unchanged", "context_epoch": 0}
    runtime.store.transaction(lambda data: data["tasks"][task["id"]]["messages"].append(copy.deepcopy(legacy)))
    advance(runtime)
    row = runtime.store.read()["tasks"][task["id"]]
    note = _kind(row["messages"], "continuation")
    _assert_host(note, "continuation")
    assert any(m == legacy for m in row["messages"])
    projected = prompt.provider_messages([note])[0]
    assert projected["role"] == "user" and "origin" not in projected and "synthetic" not in projected
    advance(runtime)
    assert _kind(model.calls[-1][1], "continuation")["role"] == "user"
    assert runtime.get(task["id"])["status"] == "completed"


def test_t1_invalid_reply_and_subagent_notifications_use_real_context_host_tags(tmp_path):
    runtime, model, _, _ = make_runtime(tmp_path, assistant("Continue after receiving the data"))
    task = runtime.create({"message": "Inspect original owner request"})
    def seed(data):
        row = data["tasks"][task["id"]]
        row["rejected_response_note"] = "Invalid JSON reply"
        row["subagent_notifications"] = [{"id": "notice_fixture", "agent_id": "child_fixture",
            "nickname": "reader", "agent_path": "/root/reader", "status": "completed", "message": "Read evidence", "delivered": False}]
    runtime.store.transaction(seed)
    projected = runtime._context(runtime.store.read()["tasks"][task["id"]])
    assert _kind(projected, "invalid_response")["role"] == "user"
    assert _kind(projected, "subagent_notification")["role"] == "user"
    assert all("origin" not in m and "synthetic" not in m for m in projected)
    advance(runtime)
    row = runtime.store.read()["tasks"][task["id"]]
    _assert_host(_kind(row["messages"], "subagent_notification"), "subagent_notification")
    assert not any("subagent_notification_ids" in m for m in row["messages"])
    assert all(n.get("delivered") for n in row["subagent_notifications"])


def test_t1_preferences_are_host_data_and_skill_fragments_are_preserved():
    row = {"skills": [{"id": "fixture_skill", "path": "SKILL.md", "content": "Read README first."}]}
    prefs = {"revision": 1, "items": [{"id": "brief", "text": "Use concise answers."}]}
    messages = knowledge.messages(row, prefs)
    assert messages[0]["role"] == "user" and messages[0]["content"].startswith("<skill>")
    note = _kind(messages, "preferences")
    _assert_host(note, "preferences")
    assert "lower priority than the current owner goal" in note["content"]
    assert "Use concise answers." in note["content"] and knowledge.HOW_TO_USE_SKILLS in note["content"]


def test_t2_t3_tagged_host_data_roundtrips_anthropic_with_tool_results_intact():
    owner = {"role": "user", "content": "Owner request", "message_id": "owner_fixture"}
    note = prompt.host_note("preferences", "Saved low-priority data")
    _assert_host(note, "preferences")
    assert note["content"].index(INTRO) < note["content"].index("Saved low-priority data")
    history = [{"role": "developer", "content": "Host policy"}, owner, note,
        assistant(name="inspect", args={}, call_id="call_fixture")["message"],
        {"role": "tool", "tool_call_id": "call_fixture", "content": '{"ok":true}'},
        prompt.host_note("invalid_response", "Try again"), prompt.host_note("subagent_notification", "Done")]
    projected = prompt.provider_messages(history)
    assert all("origin" not in m and "synthetic" not in m for m in projected)
    wire = make_request({"messages": projected, "max_tokens": 256, "tools": [{"type": "function", "function": {
        "name": "inspect", "parameters": {"type": "object", "properties": {}, "additionalProperties": False}}}]},
        MESSAGES, "claude-fixture", "a" * 64)
    uses = [p for m in wire["messages"] for p in m["content"] if p["type"] == "tool_use"]
    results = [p for m in wire["messages"] for p in m["content"] if p["type"] == "tool_result"]
    assert len(uses) == len(results) == 1 and results[0]["tool_use_id"] == uses[0]["id"]
    assert results[0]["content"] == '{"ok":true}'
    assert len([p for p in wire["messages"][-1]["content"] if p["type"] == "text"]) == 2


def test_t5_workspace_binding_is_a_synthetic_fact_not_an_owner_memory_source(tmp_path):
    runtime, _, _, _ = make_runtime(tmp_path, assistant("Waiting for the workspace"))
    task = runtime.create({"message": "Create a drawing"})
    advance(runtime)
    runtime.store.transaction(lambda data: data["tasks"][task["id"]]["local_execution"].update(workspace_root=""))
    workspace = tmp_path / "new_workspace"
    workspace.mkdir()
    runtime.bind_workspace(task["id"], {"workspace_root": str(workspace),
        "expected_control_revision": runtime.get(task["id"])["control_revision"]})
    row = runtime.store.read()["tasks"][task["id"]]
    note = _kind(row["messages"], "workspace_bound")
    _assert_host(note, "workspace_bound")
    assert not str(note.get("message_id", "")).startswith("owner_")
    assert str(workspace.resolve()) in note["content"]
    assert "Existing write approvals" not in note["content"]
    assert memory_runtime.latest_authentic_owner_message(row)["content"] == "Create a drawing"
    assert not memory_natural._is_stampable_owner_message({**note, "message_id": "synthetic_fixture"})
    assert len([e for e in row["events"] if e["type"] == "workspace_bound"]) == 1


@pytest.fixture
def image_turn(tmp_path):
    row = {"id": "task_fixture", "goal": {"revision": 1}, "scope_revision": 1, "context_epoch": 0,
           "local_execution": {"workspace_root": str(tmp_path)},
           "messages": [{"role": "user", "content": "Observe the screenshot", "message_id": "owner_fixture"}],
           "events": [], "receipts": []}
    _, artifact = _write_mcp_artifact(tmp_path, row["id"], "operation_fixture")
    _append_confirmed_mcp_turn(row, "capture_fixture", "operation_fixture", [artifact])
    return row, artifact


@pytest.mark.parametrize("status", ["unsupported", "verification_failed"])
def test_t6_image_status_is_tagged_host_data_separate_from_tool_results(image_turn, status):
    row, artifact = image_turn
    if status == "verification_failed":
        artifact["sha256"] = "0" * 64
    original = copy.deepcopy(row["messages"])
    projected, report = image_capabilities.materialize_operation_images(row, row["messages"],
        model_row=_antigravity_model_row() if status == "unsupported" else _codex_model_row(), model_client=CapturingModelBridge())
    note = _kind(projected, "image_status")
    _assert_host(note, "image_status")
    assert IMAGE_INTRO in note["content"]
    assert report["status"] == ("route_image_unsupported" if status == "unsupported" else "artifact_verification_failed")
    assert [m for m in projected if m["role"] == "tool"] == [m for m in original if m["role"] == "tool"]
    assert row["messages"] == original


def test_t7_verified_image_url_survives_host_tagging_and_provider_projection(image_turn):
    row, _ = image_turn
    projected, report = image_capabilities.materialize_operation_images(row, row["messages"],
        model_row=_codex_model_row(), model_client=CapturingModelBridge())
    assert report["materialized_count"] == 1
    note = _kind(projected, "image_input")
    _assert_host(note, "image_input")
    assert IMAGE_INTRO in _text(note)
    image = next(p for p in note["content"] if p["type"] == "image_url")
    safe = prompt.provider_messages([note])[0]
    assert image in safe["content"] and image["image_url"]["url"].startswith("data:image/png;base64,")
    assert "origin" not in safe and "synthetic" not in safe


def test_synthetic_host_data_is_not_a_compacted_owner_or_memory_source(tmp_path):
    from rynmesh.agent_runtime import request_context
    runtime, _, _, _ = make_runtime(tmp_path)
    task = runtime.create({"message": "Authentic owner request"})
    row = runtime.store.read()["tasks"][task["id"]]
    note = prompt.host_note("continuation", "Continue")
    row["messages"].append(note)
    assert not _is_owner_user_message(note)
    assert memory_runtime.latest_authentic_owner_message(row)["content"] == "Authentic owner request"
    compacted = request_context.build_context([], row["messages"], checkpoint={"through": 2, "content": "Summary"})
    assert [m["content"] for m in compacted if m["role"] == "user"] == ["Authentic owner request"]


def test_header_progress_does_not_leave_a_model_request_ending_in_assistant(tmp_path, monkeypatch):
    from rynmesh.agent_runtime import model_profiles

    runtime, _, _, _ = make_runtime(tmp_path)
    task = runtime.create({"message": "Original owner request"})
    row = runtime.store.read()["tasks"][task["id"]]
    row["messages"].append(assistant("The last recorded answer.")["message"])
    profile_for_task = model_profiles.for_task
    monkeypatch.setattr(model_profiles, "for_task", lambda *args, **kwargs: {
        **profile_for_task(*args, **kwargs), "progress_placement": "header",
    })

    projected = runtime._context(row)
    assert projected[-2] == {"role": "assistant", "content": "The last recorded answer."}
    assert projected[-1]["role"] == "user"
    assert projected[-1]["content"].startswith('<host_note kind="continuation">')
    assert INTRO in projected[-1]["content"]
