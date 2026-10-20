"""Two additional independent encrypted-Store regressions; root runs them.

These use authentic owner messages initialized by the existing runtime, public
memory mutation helpers, and actual persisted Store reloads. No algorithm or
serialized fence-key parser is copied into the tests.
"""
from __future__ import annotations
import copy
import sys
from pathlib import Path

sys.dont_write_bytecode = True
sys.path.insert(0, str(Path(__file__).resolve().parent))
import test_stage1_memory_boundaries as boundary
import pytest
from rynmesh.agent_runtime import memory
from rynmesh.agent_runtime.store import AgentError, AgentStore

sandbox = boundary.sandbox


def _source_from_persisted_row(row, message):
    return memory.build_source_ref(
        source_type="task_message", task_id=row["id"],
        message_id=message["message_id"],
        conversation_revision=row.get("conversation_revision", 0),
        context_epoch=message.get("context_epoch", row.get("context_epoch", 0)),
        source_revision=row["revision"], content=message["content"],
        author_role="owner", project_id=(row.get("project") or {}).get("id"),
    )


def _assert_old_source_not_requeued(store, task_id, message_id):
    try:
        queued = store.transaction(lambda data: memory.enqueue_extraction_job(
            data, task_id=task_id, message_ids=[message_id], force_relearn=False))
    except AgentError:
        queued = {"queued": False}
    assert queued["queued"] is False, "A source captured before reset was re-enqueued without relearn authorization"


def test_legacy_colon_message_id_fence_survives_real_store_compaction(sandbox):
    runtime, key = sandbox
    store = runtime.store
    boundary.enable_capture(store)
    # Eight is the public settings API's documented minimum; nine genuine
    # deletions force real compaction without generating a large benchmark.
    store.transaction(lambda data: memory.update_memory_settings(
        data, {"budgets": {"max_fenced_sources": 8}}))
    protected_source = None
    for index in range(9):
        text = f"My camera is LEGACY_SOURCE_CAMERA_{index}"
        created = runtime.create({"message": text})
        def legacy_owner_fixture(data):
            row = data["tasks"][created["id"]]
            row["status"] = "completed"
            row["conversation_revision"] = 7
            row["model_destination"] = copy.deepcopy(boundary.DEST_A)
            # Legacy real owner IDs contain multiple colons. We do not compute
            # or duplicate the product's combined fence storage key.
            row["messages"][0]["message_id"] = row["id"] + ":owner:0"
            return row
        row = store.transaction(legacy_owner_fixture)
        message = row["messages"][0]
        src = _source_from_persisted_row(row, message)
        before = store.inspect(lambda data: memory.verify_source_ref_against_state(
            data, src, for_background_job=True))
        assert before[0] is True, ("Positive-control source invalid before deletion", before)
        made = store.transaction(lambda data: memory.upsert_active_entry(
            data, {"category": "photography", "fact_key": f"audit.legacy.camera.{index}",
                   "text": text, "sources": [src],
                   "exposure_policy": {"mode": "destinations", "allowed_destinations": [boundary.DEST_A]}},
            origin="explicit_owner_chat"))
        store.transaction(lambda data: memory.delete_memory_entry(data, made["entry"]["id"]))
        denied = store.inspect(lambda data: memory.verify_source_ref_against_state(
            data, src, for_background_job=True))
        assert denied[0] is False, "The delete operation did not fence its owner source"
        if index == 0:
            protected_source = src
    assert protected_source["conversation_revision"] == 7
    assert protected_source["message_id"].endswith(":owner:0")
    stored_fences = store.inspect(lambda data: copy.deepcopy(data["memory"]["source_fences"]))
    assert len(stored_fences["fenced_messages"]) <= 8, "Fixture did not cross the configured compaction bound"
    protected_task_id = protected_source["task_id"]
    protected_watermark = max(
        stored_fences.get("fenced_task_watermarks", {}).get(protected_task_id, -1),
        stored_fences.get("fenced_relearn_watermarks", {}).get(protected_task_id, -1),
    )
    assert protected_watermark >= 7, "No persistent compacted fence protects the actual legacy source task through revision 7"
    restored = AgentStore(store.root, key)
    after_reload = restored.inspect(lambda data: memory.verify_source_ref_against_state(
        data, protected_source, for_background_job=True))
    assert after_reload[0] is False, (
        "Compaction/reload lost protection for a colon-containing legacy owner message at conversation revision 7",
        after_reload, stored_fences.get("fenced_task_watermarks"),
    )
    _assert_old_source_not_requeued(restored, protected_source["task_id"], protected_source["message_id"])


def test_project_reset_allows_new_owner_message_in_same_unedited_conversation(sandbox):
    runtime, key = sandbox
    store = runtime.store
    boundary.enable_capture(store)
    old_row, old_message = boundary.owner_source(
        runtime, text="My old camera is PRE_RESET_CAMERA_SENTINEL", project=boundary.PROJECT)
    task_id = old_row["id"]
    old_conv = old_row.get("conversation_revision", 0)
    store.transaction(lambda data: memory.reset_memory(data, scope=boundary.SCOPE))
    restored = AgentStore(store.root, key)
    _assert_old_source_not_requeued(restored, task_id, old_message["message_id"])

    # Exercise the real append-user-message operation, not an artificial branch
    # rewrite or force_relearn switch. Normal follow-ups do not change the
    # conversation revision, so a conversation-wide watermark is too coarse.
    current = runtime.get(task_id)
    runtime.add_message(task_id, {
        "message": "My new lens is POST_RESET_LENS_SENTINEL",
        "intent": "information",
        "expected_control_revision": current["control_revision"],
        "expected_goal_revision": current["goal"]["revision"],
    })
    appended = restored.inspect(lambda data: copy.deepcopy(data["tasks"][task_id]))
    new_message = next(message for message in reversed(appended["messages"])
                       if message.get("role") == "user")
    assert new_message["message_id"] != old_message["message_id"]
    assert appended.get("conversation_revision", 0) == old_conv, "Fixture unexpectedly performed a branch rewrite"
    assert appended["revision"] > old_row["revision"], "Owner append did not advance the task revision"
    _assert_old_source_not_requeued(restored, task_id, old_message["message_id"])
    new_source = _source_from_persisted_row(appended, new_message)
    eligible = restored.inspect(lambda data: memory.verify_source_ref_against_state(
        data, new_source, for_background_job=True))
    assert eligible[0] is True, ("Scoped reset blocked a newly authored owner message in the same conversation", eligible)

    queued = restored.transaction(lambda data: memory.enqueue_extraction_job(
        data, task_id=task_id, message_ids=[new_message["message_id"]], force_relearn=False))
    assert queued["queued"] is True, "Post-reset owner message could not be queued"
    lease = restored.transaction(lambda data: memory.claim_extraction_job(
        data, worker_id="post-reset-source-worker", job_id=queued["job"]["id"]))
    assert lease is not None
    assert {source["message_id"] for source in lease["source_manifest"]} == {new_message["message_id"]}
    committed = boundary.commit_claim(restored, lease, [{
        "category": "photography", "fact_key": "project.gear.new_lens",
        "text": "POST_RESET_LENS_SENTINEL",
        "source_message_ids": [new_message["message_id"]],
    }])
    assert committed["state"] == "committed", committed
    assert len(committed["committed_candidates"]) == 1
    candidate = committed["committed_candidates"][0]
    assert {source["message_id"] for source in candidate["sources"]} == {new_message["message_id"]}
    restored.transaction(lambda data: memory.decide_candidate(
        data, candidate["id"], decision="accept", expected_candidate_revision=candidate["revision"]))
    final = restored.inspect(memory.list_memories_snapshot)
    assert len(final["entries"]) == 1
    assert "POST_RESET_LENS_SENTINEL" in str(final["entries"])
    assert "PRE_RESET_CAMERA_SENTINEL" not in str(final["entries"])
    _assert_old_source_not_requeued(restored, task_id, old_message["message_id"])


@pytest.mark.parametrize("changed_field", ["provider_peer_id", "source_id", "destination_revision", "unknown_destination"])
def test_same_model_id_different_actual_destination_cannot_select_private_active_fact(sandbox, changed_field):
    """Exercise the real Store selection projection, not destination_matches()."""
    runtime, _ = sandbox
    store = runtime.store
    made = store.transaction(lambda data: memory.upsert_active_entry(data, {
        "category": "photography", "fact_key": "audit.destination.camera",
        "text": "ACTIVE_DESTINATION_PRIVATE_CAMERA_SENTINEL",
        "exposure_policy": {"mode": "destinations", "allowed_destinations": [boundary.DEST_A]},
    }, origin="manual"))
    allowed_row, _ = boundary.owner_source(runtime, text="Recommend camera settings", destination=boundary.DEST_A)
    denied_destination = copy.deepcopy(boundary.DEST_A)
    if changed_field != "unknown_destination":
        denied_destination[changed_field] = "different-actual-" + changed_field
    denied_row, _ = boundary.owner_source(runtime, text="Recommend camera settings", destination=denied_destination)
    if changed_field == "unknown_destination":
        # Keep the catalog model ID, but remove actual provider/source binding.
        # A fallback to just the model name must not satisfy an exact policy.
        store.transaction(lambda data: data["tasks"][denied_row["id"]].update(model_destination=None))
    positive = store.inspect(lambda data: memory.select_memories_for_task(
        data, data["tasks"][allowed_row["id"]], query_text="camera"))
    rejected = store.inspect(lambda data: memory.select_memories_for_task(
        data, data["tasks"][denied_row["id"]], query_text="camera"))
    assert made["entry"]["id"] in {entry["id"] for entry in positive["selected"]}, "Positive-control exact destination lost its fact"
    assert made["entry"]["id"] not in {entry["id"] for entry in rejected["selected"]}, (
        "Same catalog model ID bypassed actual provider/source/config binding", changed_field, rejected)
    assert "ACTIVE_DESTINATION_PRIVATE_CAMERA_SENTINEL" not in str(rejected)
