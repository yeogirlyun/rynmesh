"""S05-I24 attachment excerpts through encrypted history, requests and checkpoints."""
import base64
import copy
import json

from test_agent_runtime import advance, assistant, make_runtime

from rynmesh.agent_runtime.input_attachments import attach_to_message, parse_attachments, provider_content
from rynmesh.agent_runtime.prompt import provider_messages


SECRET = "attachment-detail-should-not-be-permanent-说清文件细节"


def upload(text=SECRET, name="evidence.txt"):
    return {"name": name, "media_type": "text/plain", "data_base64": base64.b64encode(text.encode()).decode()}


def stored(runtime, tid):
    return runtime.store.read()["tasks"][tid]


def request_text(messages):
    return json.dumps(messages, ensure_ascii=False)


def attached_runtime(tmp_path, *responses):
    runtime, model, _, _ = make_runtime(tmp_path, *responses)
    task = runtime.create({"message": "Read this evidence", "attachments": [upload()]})
    return runtime, model, task["id"]


def test_t1_new_excerpt_reaches_real_model_request_without_local_metadata(tmp_path):
    runtime, model, tid = attached_runtime(tmp_path, assistant("Read the evidence"))
    advance(runtime)
    request = model.calls[0][1]
    assert SECRET in request_text(request)
    owner = next(m for m in request if m["role"] == "user" and "Read this evidence" in request_text([m]))
    assert "Bounded content excerpt:" in request_text([owner])
    assert "attachment_excerpts" not in owner and "attachments" not in owner
    assert stored(runtime, tid)["messages"][0].get("attachment_excerpts")


def test_t2_next_turn_omits_old_excerpt_and_keeps_exact_read_path(tmp_path):
    runtime, model, tid = attached_runtime(tmp_path, assistant("Read"), assistant("Continued"))
    advance(runtime)
    first = stored(runtime, tid)["messages"][0]
    runtime.add_message(tid, {"message": "Now continue", "expected_revision": runtime.get(tid)["revision"]})
    advance(runtime)
    request = model.calls[-1][1]
    assert SECRET not in request_text(request)
    old = next(m for m in request if m["role"] == "user" and "Read this evidence" in request_text([m]))
    assert "附件 1 摘录已省略" in request_text([old])
    assert "ryn_file_read" in request_text([old])
    assert first["attachments"][0]["path"] in str(old["content"])
    assert stored(runtime, tid)["messages"][0] == first


def test_t3_owner_content_contains_only_manifest_and_original_ui_text(tmp_path):
    runtime, _, tid = attached_runtime(tmp_path)
    message = stored(runtime, tid)["messages"][0]
    assert SECRET not in message["content"]
    assert "Bounded content excerpt:" not in message["content"]
    assert message["content"].startswith("Read this evidence") and message["ui"]["text"] == "Read this evidence"
    assert "evidence.txt (text/plain," in message["content"] and "local path:" in message["content"]
    assert SECRET in request_text(message["attachment_excerpts"])


def test_t4_legacy_embedded_excerpt_is_preserved_byte_for_byte():
    content = "Owner text\nUser-attached files:\nBounded content excerpt:\n" + SECRET + "\nEnd of attachment excerpt."
    legacy = {"role": "user", "content": content, "attachments": [{"name": "old.txt", "path": "/old.txt"}]}
    original = copy.deepcopy(legacy)
    result = provider_messages([legacy, {"role": "assistant", "content": "old reply"}, {"role": "user", "content": "next"}])
    assert result[0]["content"] == content and legacy == original
    assert provider_content(legacy) == content


def test_t2_checkpointed_latest_owner_does_not_rehydrate_excerpt(tmp_path):
    runtime, _, tid = attached_runtime(tmp_path)
    row = stored(runtime, tid)
    row["context_checkpoint"] = {"through": len(row["messages"]), "content": "Earlier evidence summarized"}
    context = runtime._context(row)
    assert SECRET not in request_text(context)
    assert "附件 1 摘录已省略" in request_text(context)
    row["messages"].append({"role": "user", "content": "fresh", "message_id": "owner-fresh",
                            "ui": {"kind": "message", "text": "fresh"}})
    attach_to_message(runtime.store, row, row["messages"][-1], parse_attachments([upload("new-detail", "new.txt")]))
    context = runtime._context(row)
    assert SECRET not in request_text(context) and "new-detail" in request_text(context)


def test_t2_old_excerpt_does_not_raise_authority_floor(tmp_path):
    runtime, _, tid = attached_runtime(tmp_path)
    row = stored(runtime, tid)
    row["messages"].append({"role": "user", "content": "next", "ui": {"kind": "message", "text": "next"}})
    without = copy.deepcopy(row)
    # Keep an empty excerpt entry, so only the archived body size changes.
    for entry in without["messages"][0].get("attachment_excerpts", []):
        entry["text"] = ""
    assert runtime._menu(row)[1] == runtime._menu(without)[1]
    assert SECRET not in request_text(runtime._context(row))


def test_t1_current_turn_keeps_excerpt_through_tool_responses_and_notification(tmp_path):
    runtime, _, tid = attached_runtime(tmp_path)
    row = stored(runtime, tid)
    row["messages"].extend([
        {"role": "assistant", "content": "Inspecting"},
        {"role": "user", "content": "Worker finished", "ui": {"kind": "subagent_notification"}},
    ])
    assert SECRET in request_text(runtime._context(row))


def test_t3_multiple_excerpts_keep_number_path_and_read_hints(tmp_path):
    runtime, _, _, _ = make_runtime(tmp_path)
    row = {"id": "mixed", "local_execution": {"workspace_root": str(tmp_path)}}
    message = {"role": "user", "content": "Read both"}
    attach_to_message(runtime.store, row, message, parse_attachments([upload("first-detail"), upload("second-detail", "second.txt")]))
    assert "first-detail" not in message["content"] and "second-detail" not in message["content"]
    current = provider_messages([message])[0]
    assert "first-detail" in str(current["content"]) and "second-detail" in str(current["content"])
    projected = provider_messages([message, {"role": "user", "content": "next"}])[0]
    assert "附件 1 摘录已省略" in str(projected["content"]) and "附件 2 摘录已省略" in str(projected["content"])
    for file in message["attachments"]:
        assert file["path"] in str(projected["content"])


def test_t1_t2_mixed_image_projection_preserves_parts_and_checkpoint_image_omission(tmp_path):
    from test_input_attachments import _png_upload
    from rynmesh.agent_runtime import request_context
    runtime, _, _, _ = make_runtime(tmp_path)
    row = {"id": "mixed", "local_execution": {"workspace_root": str(tmp_path)}}
    message = {"role": "user", "content": "Inspect document and image"}
    attach_to_message(runtime.store, row, message, parse_attachments([upload(), _png_upload("image.png")]))
    image = message["attachments"][1]["image_url"]
    current = provider_messages([message])[0]["content"]
    assert SECRET in current[0]["text"]
    assert current[1] == {"type": "text", "text": "[Image #1]"}
    assert current[2]["image_url"]["url"] == image
    later = provider_messages([message, {"role": "user", "content": "next"}])[0]["content"]
    assert SECRET not in str(later) and "附件 1 摘录已省略" in str(later)
    assert later[2]["image_url"]["url"] == image
    compacted = request_context.build_context([], [message, {"role": "user", "content": "next"}],
                                             checkpoint={"through": 1, "content": "Summary"})
    parts = provider_messages(compacted)[0]["content"]
    assert all(p["type"] == "text" for p in parts)
    assert SECRET not in str(parts) and image not in str(parts)
    assert request_context.OMITTED_IMAGE_PLACEHOLDER in str(parts)
