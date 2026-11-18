"""S05-I31: automatic Craft replacement through real review and receipt paths."""
from pathlib import Path

import pytest

from test_craft_agent import craft, node, manager, _act, _context, _args, PREFIX
from rynmesh.agent_runtime import file_review, workspace_artifacts
from rynmesh.agent_runtime.store import AgentToolRejected
from rynmesh.node_apps.craft_agent import ACTION, TOOLS


def prepare(craft, mode="automatic", before=PREFIX + b"old"):
    runtime, _ = craft.runtime(mode=mode, allowed=[ACTION])
    task = runtime.create({"message": "Update existing document"})
    target = craft.workspace / "existing.docx"
    target.write_bytes(before)
    context = _context(task)
    _act(craft, context, "new_document", operation="new")
    _act(craft, context, "type_text", {"text": "new"}, operation="type")
    return runtime, task, context, target


def test_t1_automatic_overwrite_is_staged_same_directory_and_undoable(craft):
    before = PREFIX + b"old"
    runtime, task, context, target = prepare(craft, before=before)
    result = _act(craft, context, "save_document", {"path": target.name}, operation="replace")
    assert result["ok"] and result["overwritten"] is True
    assert result["previous_sha256"] == file_review.digest(before)
    assert target.read_bytes() == PREFIX + b"new"
    saved = next(args["path"] for _, tool, args in craft.backend.calls if tool == "save_document")
    assert Path(saved).parent == target.parent and Path(saved) != target and not Path(saved).exists()
    assert result["files"][0]["path"] == str(target) and result["files"][0]["overwritten"]
    review = craft.tools.local.file_review(task["id"], craft.workspace)
    plan = review.preview()
    assert plan["can_undo"]
    review.undo(None, plan["token"], "undo-craft")
    assert target.read_bytes() == before


@pytest.mark.parametrize("mode", ["workspace", "confirm_changes"])
def test_t2_other_modes_still_require_new_output(craft, mode):
    _, _, context, target = prepare(craft, mode)
    with pytest.raises(AgentToolRejected, match="new_output_path"):
        craft.tools.preview(ACTION, _args("save_document", {"path": target.name}), context=context)
    assert target.read_bytes() == PREFIX + b"old"
    assert not any(tool == "save_document" for _, tool, _ in craft.backend.calls)


def test_t3_change_during_engine_save_refuses_replacement(craft, monkeypatch):
    _, task, context, target = prepare(craft)
    process = craft.tools.craft.sessions[(task["id"], "wordcraft")]["process"]
    original = process.tools_call
    def external_change(name, args, **kwargs):
        result = original(name, args, **kwargs)
        if name == "save_document":
            target.write_bytes(PREFIX + b"external")
        return result
    monkeypatch.setattr(process, "tools_call", external_change)
    result = _act(craft, context, "save_document", {"path": target.name}, operation="conflict")
    assert result["ok"] is False and result["error"] == "agent_local_file_changed_read_again"
    assert target.read_bytes() == PREFIX + b"external" and result["files"] == []
    assert not craft.tools.local.file_review(task["id"], craft.workspace).load()["records"]


def test_t3_stale_preview_rejected_before_engine_save(craft):
    _, _, context, target = prepare(craft)
    args = _args("save_document", {"path": target.name})
    preview = craft.tools.preview(ACTION, args, context=context)
    target.write_bytes(PREFIX + b"external")
    with pytest.raises(AgentToolRejected, match="preview_changed"):
        _act(craft, context, "save_document", {"path": target.name}, operation="stale", preview=preview)
    assert not any(tool == "save_document" for _, tool, _ in craft.backend.calls)


def test_large_office_file_has_preview_warning_and_degrades_without_snapshot(craft):
    _, task, context, target = prepare(craft)
    craft.tools.craft.sessions[(task["id"], "wordcraft")]["process"].text = "x" * file_review.MAX_FILE
    preview = craft.tools.preview(ACTION, _args("save_document", {"path": target.name}), context=context)
    assert preview["replace_targets"][0]["snapshot_available"] is True
    assert "undo" in preview["overwrite_notice"].lower()
    result = _act(craft, context, "save_document", {"path": target.name}, operation="large", preview=preview)
    assert result["ok"] and result["overwritten"]
    steps = craft.tools.local.file_review(task["id"], craft.workspace).load()["records"][0]["steps"]
    assert steps[0]["snapshot"] is False and steps[0]["state"] == "applied"


def test_existing_file_over_review_single_file_limit_requires_new_path(craft):
    _, _, context, target = prepare(craft, before=PREFIX + b"x" * file_review.MAX_FILE)
    with pytest.raises(AgentToolRejected, match="agent_craft_new_output_path_required"):
        craft.tools.preview(ACTION, _args("save_document", {"path": target.name}), context=context)


def test_overwritten_craft_receipt_is_an_update_not_task_created(craft):
    runtime, task, context, target = prepare(craft)
    result = _act(craft, context, "save_document", {"path": target.name}, operation="artifact")
    row = {"id": task["id"], "local_execution": task["local_execution"], "result": target.name,
           "receipts": [{"operation_id": "artifact", "status": "confirmed", "tool": ACTION, "result": result}]}
    item = workspace_artifacts.listing(row)["files"][0]
    assert item["created_by_task"] is False and item["source"] == "write"


@pytest.mark.parametrize("path", ["missing/output.docx", "directory.docx"])
def test_missing_parent_and_existing_directory_remain_rejected(craft, path):
    _, _, context, _ = prepare(craft)
    (craft.workspace / "directory.docx").mkdir()
    with pytest.raises(AgentToolRejected):
        craft.tools.preview(ACTION, _args("save_document", {"path": path}), context=context)


def test_automatic_discovery_explains_overwrite_policy(craft):
    _, _, context, _ = prepare(craft)
    tools = craft.tools.execute(TOOLS, {"app": "wordcraft"}, context=context,
                                operation_id="discovery", cancelled=lambda: False)
    assert tools["saves_require_new_path"] is False and tools["overwrites_require_backup"] is True


def test_timeout_publication_keeps_overwrite_receipt_and_backup(craft):
    _, task, context, target = prepare(craft)
    craft.backend.fail_next = "save_document"
    result = _act(craft, context, "save_document", {"path": target.name}, operation="timeout")
    assert result["ok"] and result["reconciled_after_timeout"] and result["overwritten"]
    assert result["files"][0]["previous_sha256"] == file_review.digest(PREFIX + b"old")
    assert craft.tools.local.file_review(task["id"], craft.workspace).preview()["can_undo"]
