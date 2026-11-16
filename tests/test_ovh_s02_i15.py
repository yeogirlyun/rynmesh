"""S02-I15 regression tests: branch rewrites archive retired suffixes out of task rows."""
from __future__ import annotations

import copy
import hashlib
import json
import logging

from test_agent_runtime import Catalog, advance, assistant, make_runtime

from rynmesh.agent_runtime import lifecycle
from rynmesh.agent_runtime.store import AgentError, encoded

EXPECTED_BRANCH_KEYS = {
    "revision",
    "message_index",
    "event_cursor",
    "retired_count",
    "retired_sha256",
    "archive_ref",
}


def _send(runtime, task_id: str, message: str, **extra):
    row = runtime.get(task_id)
    return runtime.add_message(
        task_id,
        {"message": message, "expected_control_revision": row["control_revision"], **extra},
    )


def _run_ten_edits_and_measure(root_dir, suffix_body: str) -> tuple[int, dict, object]:
    runtime, model, _, _ = make_runtime(root_dir)
    turn_counter = 0

    def inference(model_id, messages, specs, cancelled, on_event):
        nonlocal turn_counter
        turn_counter += 1
        return assistant(f"Assistant reply #{turn_counter}.")

    model.complete = inference
    runtime.catalog.list_models = lambda: [
        {
            **Catalog().list_models()[0],
            "context_window": 262144,
            "context_window_known": True,
            "max_output_tokens": 4096,
        }
    ]

    created = runtime.create({"message": "Keep first question."})
    task_id = created["id"]
    advance(runtime)

    _send(runtime, task_id, "DISCARDED_SUFFIX_00_" + suffix_body)
    advance(runtime)

    for idx in range(1, 11):
        next_payload = (
            f"DISCARDED_SUFFIX_{idx:02d}_" + suffix_body
            if idx < 10
            else "Final concise replacement question."
        )
        _send(runtime, task_id, next_payload, edit_message_index=2)
        if idx < 10:
            advance(runtime)

    saved_row = runtime.store.read()["tasks"][task_id]
    return len(encoded(saved_row)), saved_row, runtime


def test_repeated_edits_keep_task_row_bounded_and_cap_branch_metadata_at_twenty(tmp_path):
    """T1: 10 consecutive edits do not grow task row with discarded suffix size; >20 caps metadata at 20; memory fence stays intact."""
    small_size, small_row, _ = _run_ten_edits_and_measure(tmp_path / "small", "s" * 16)
    large_size, large_row, runtime = _run_ten_edits_and_measure(tmp_path / "large", "L" * 25000)
    task_id = large_row["id"]

    # Ten ~50 KB discarded suffixes (~500 KB total) must NOT grow the task row compared to 16-byte suffixes.
    assert len(small_row["conversation_branches"]) == 10
    assert len(large_row["conversation_branches"]) == 10
    assert abs(large_size - small_size) < 256
    assert len(encoded(large_row["conversation_branches"])) < 3000
    assert "DISCARDED_SUFFIX_" not in encoded(large_row).decode("utf-8")

    advance(runtime)
    retired_owner_ids: list[str] = []

    for idx in range(11, 23):
        before_row = runtime.store.read()["tasks"][task_id]
        retired_user_msg = before_row["messages"][2]
        retired_owner_ids.append(retired_user_msg["message_id"])
        expected_sha = hashlib.sha256(encoded(before_row["messages"][2:])).hexdigest()

        next_payload = (
            f"DISCARDED_SUFFIX_{idx:02d}_" + ("Y" * 10000)
            if idx < 22
            else "Final twenty-second replacement question."
        )
        _send(runtime, task_id, next_payload, edit_message_index=2)
        advance(runtime)

        saved_row = runtime.store.read()["tasks"][task_id]
        raw_text = encoded(saved_row).decode("utf-8")
        assert f"DISCARDED_SUFFIX_{idx - 1:02d}_" not in raw_text

        latest_meta = saved_row["conversation_branches"][-1]
        assert set(latest_meta.keys()) == EXPECTED_BRANCH_KEYS
        assert "messages" not in latest_meta
        assert "goal" not in latest_meta
        assert latest_meta["revision"] == idx - 1
        assert latest_meta["message_index"] == 2
        assert latest_meta["retired_count"] == 2
        assert latest_meta["retired_sha256"] == expected_sha
        assert isinstance(latest_meta["archive_ref"], str) and latest_meta["archive_ref"]

    final_state = runtime.store.read()
    final_row = final_state["tasks"][task_id]
    assert len(final_row["conversation_branches"]) == 20
    assert len(encoded(final_row["conversation_branches"])) < 6000
    assert [item["revision"] for item in final_row["conversation_branches"]] == list(range(2, 22))
    assert all(set(item.keys()) == EXPECTED_BRANCH_KEYS for item in final_row["conversation_branches"])

    # Change 2: memory.record_branch_rewrite_fence still recorded retired owner message IDs from in-memory retired_messages.
    fences = final_state["memory"]["source_fences"]["retired_branches"]
    fenced_ids = {
        msg_id
        for fence in fences
        if fence.get("task_id") == task_id
        for msg_id in fence.get("retired_message_ids", [])
    }
    assert set(retired_owner_ids) <= fenced_ids

    # Archived chunks remain decrypted-readable via archive_store.
    archived_root = lifecycle.archive_store(runtime.store, task_id).read()
    assert len(archived_root["conversation_branches"]) == 22
    last_ref = final_row["conversation_branches"][-1]["archive_ref"]
    restored_last = lifecycle.read_archived_conversation_branch(runtime.store, task_id, last_ref)
    assert restored_last is not None
    assert restored_last["messages"][0]["message_id"] == retired_owner_ids[-1]


def test_legacy_inline_conversation_branches_migrate_to_encrypted_archive_and_read_back(tmp_path):
    """T2: Legacy inline messages/goal in conversation_branches migrate to archive_store on rewrite and leave metadata only."""
    runtime, model, _, _ = make_runtime(
        tmp_path,
        assistant("Initial answer."),
        assistant("Second answer to replace."),
        assistant("Rewritten answer."),
    )
    created = runtime.create({"message": "Original root prompt."})
    task_id = created["id"]
    advance(runtime)
    _send(runtime, task_id, "Current second prompt.")
    advance(runtime)

    legacy_messages = [
        {"role": "user", "message_id": f"{task_id}:legacy:2", "content": "LEGACY_INLINE_QUESTION_" + ("L" * 2048)},
        {"role": "assistant", "content": "LEGACY_INLINE_ANSWER"},
    ]
    legacy_goal = {
        "revision": 2,
        "kind": "unclassified",
        "original_message_id": f"{task_id}:owner:0",
        "text": "Original root prompt.",
        "constraints": [{"id": "c_legacy", "text": "LEGACY_INLINE_CONSTRAINT", "source": "user", "required": True}],
    }
    expected_legacy_sha = hashlib.sha256(encoded(legacy_messages)).hexdigest()

    def seed_legacy_branch(data: dict) -> None:
        task_row = data["tasks"][task_id]
        task_row["conversation_revision"] = 1
        task_row["conversation_branches"] = [
            {
                "revision": 0,
                "message_index": 2,
                "event_cursor": 3,
                "messages": copy.deepcopy(legacy_messages),
                "goal": copy.deepcopy(legacy_goal),
            }
        ]

    runtime.store.transaction(seed_legacy_branch)
    before_edit_row = runtime.store.read()["tasks"][task_id]
    expected_new_retired = copy.deepcopy(before_edit_row["messages"][2:])
    expected_new_goal = copy.deepcopy(before_edit_row["goal"])
    expected_new_sha = hashlib.sha256(encoded(expected_new_retired)).hexdigest()

    _send(runtime, task_id, "Fresh replacement second prompt.", edit_message_index=2)
    advance(runtime)

    saved_row = runtime.store.read()["tasks"][task_id]
    saved_dump = json.dumps(saved_row, ensure_ascii=False)
    assert "LEGACY_INLINE_QUESTION_" not in saved_dump
    assert "LEGACY_INLINE_ANSWER" not in saved_dump
    assert "LEGACY_INLINE_CONSTRAINT" not in saved_dump
    assert "Current second prompt." not in saved_dump

    branches = saved_row["conversation_branches"]
    assert len(branches) == 2
    migrated_meta, current_meta = branches[0], branches[1]
    assert set(migrated_meta.keys()) == EXPECTED_BRANCH_KEYS
    assert set(current_meta.keys()) == EXPECTED_BRANCH_KEYS
    assert migrated_meta["revision"] == 0
    assert migrated_meta["retired_count"] == 2
    assert migrated_meta["retired_sha256"] == expected_legacy_sha
    assert current_meta["revision"] == 1
    assert current_meta["retired_count"] == len(expected_new_retired)
    assert current_meta["retired_sha256"] == expected_new_sha

    migrated_archived = lifecycle.read_archived_conversation_branch(
        runtime.store, task_id, migrated_meta["archive_ref"]
    )
    assert migrated_archived is not None
    assert migrated_archived["messages"] == legacy_messages
    assert migrated_archived["goal"] == legacy_goal

    current_archived = lifecycle.read_archived_conversation_branch(
        runtime.store, task_id, current_meta["archive_ref"]
    )
    assert current_archived is not None
    assert current_archived["messages"] == expected_new_retired
    assert current_archived["goal"] == expected_new_goal


def test_archive_write_failure_falls_back_to_metadata_only_and_logs_diagnostic(tmp_path, monkeypatch, caplog):
    """Risk check: if archive_store write fails, rewrite still succeeds with metadata-only and emits body-free diagnostic."""
    runtime, _, _, _ = make_runtime(
        tmp_path,
        assistant("First reply."),
        assistant("Second reply."),
        assistant("Replacement reply."),
    )
    created = runtime.create({"message": "First question."})
    task_id = created["id"]
    advance(runtime)
    secret_discarded = "SENSITIVE_DISCARDED_TEXT_DO_NOT_LOG"
    _send(runtime, task_id, secret_discarded)
    advance(runtime)

    def fail_archive(store, tid, branches_by_ref):
        raise AgentError("agent_storage_unavailable")

    monkeypatch.setattr(lifecycle, "archive_conversation_branches", fail_archive)

    with caplog.at_level(logging.WARNING, logger="rynmesh.agent_runtime.diagnostic_log"):
        _send(runtime, task_id, "Replacement question after archive outage.", edit_message_index=2)
    advance(runtime)

    saved_row = runtime.store.read()["tasks"][task_id]
    assert saved_row["result"] == "Replacement reply."
    assert secret_discarded not in json.dumps(saved_row, ensure_ascii=False)
    meta = saved_row["conversation_branches"][-1]
    assert set(meta.keys()) == EXPECTED_BRANCH_KEYS
    assert meta["archive_ref"] is None
    assert meta["retired_count"] == 2

    matching_logs = [rec.getMessage() for rec in caplog.records if "conversation_branch_archive" in rec.getMessage()]
    assert matching_logs
    assert all(secret_discarded not in msg for msg in matching_logs)
