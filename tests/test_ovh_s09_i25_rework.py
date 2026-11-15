"""Rework tests for S09-I25: folded tombstone seq suppression in natural consolidation and lifecycle idempotency retention."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey

from rynmesh.agent_runtime import memory, memory_natural
from rynmesh.agent_runtime.store import AgentError, AgentStore

DEST_A = {
    "catalog_model_id": "local/svc-a/model/gemini-4-argon-medium",
    "provider_peer_id": "peer-local-owner",
    "source_id": "src-local-a",
    "destination_revision": "rev-a-1",
}


def _make_store_with_automatic_memory(tmp_path: Path) -> AgentStore:
    store = AgentStore(tmp_path / "agent", X25519PrivateKey.generate())

    def init_tx(data: dict[str, Any]) -> None:
        data["settings"].update(enabled=True)
        memory.ensure_memory_state(data)
        memory.update_memory_settings(
            data,
            {
                "capture_enabled": True,
                "use_enabled": True,
                "activation_policy": "automatic",
                "extraction_destination": dict(DEST_A),
                "default_exposure": {
                    "mode": "selected_models",
                    "allowed_destinations": [dict(DEST_A)],
                },
            },
        )

    store.transaction(init_tx)
    return store


def _seed_task_messages(
    store: AgentStore,
    task_id: str,
    user_messages: list[tuple[str, str]],
) -> None:
    def tx(data: dict[str, Any]) -> None:
        memory.ensure_memory_state(data)
        msgs = []
        for mid, text in user_messages:
            msg = {
                "message_id": mid,
                "role": "user",
                "author_role": "owner",
                "content": text,
                "context_epoch": 0,
            }
            memory_natural.stamp_single_owner_message_in_tx(data, msg)
            msgs.append(msg)
        data.setdefault("tasks", {})[task_id] = {
            "id": task_id,
            "status": "completed",
            "archived": False,
            "revision": 1,
            "conversation_revision": 1,
            "context_epoch": 0,
            "model_id": DEST_A["catalog_model_id"],
            "model_destination": dict(DEST_A),
            "messages": msgs,
            "events": [],
            "in_flight": None,
            "pending_control": None,
            "pending_user_messages": [],
            "pending_approval": None,
            "pending_input": None,
            "queue": [],
        }

    store.transaction(tx)


def test_s09_i25_folded_tombstone_blocks_pre_delete_natural_consolidation_and_keeps_max_seq(
    tmp_path: Path,
) -> None:
    """Rework 1a: After tombstones are folded into suppressed_slots (including multiple tombstones
    sharing the same 16-hex prefix keeping max deleted_at_source_seq), natural consolidation on
    a pre-delete message still returns tombstone_suppressed and does not relearn the deleted slot."""
    store = _make_store_with_automatic_memory(tmp_path)
    _seed_task_messages(
        store,
        "task_before",
        [
            ("m_very_old", "最早提过富士 X-T4。"),
            ("m_pre_delete", "我现在的主力机身是富士 X-T5。"),
        ],
    )

    def fold_and_claim(data: dict[str, Any]) -> dict[str, Any]:
        # Create and delete camera_body after m_pre_delete (deleted_at_source_seq >= 2)
        entry = memory.upsert_active_entry(
            data,
            {
                "scope": {"kind": "personal"},
                "category": "equipment",
                "fact_key": "personal.gear.camera_body",
                "text": "富士 X-T5",
                "exposure_policy": {"mode": "destinations", "allowed_destinations": [dict(DEST_A)]},
            },
            origin="manual",
        )["entry"]
        memory.delete_memory_entry(data, entry["id"], reason="owner_deleted")

        mem = data["memory"]
        orig_tomb = dict(mem["tombstones"][entry["id"]])
        high_seq = int(orig_tomb["deleted_at_source_seq"])
        assert high_seq >= 2

        # Simulate an older tombstone sharing the exact same 16-hex prefix with seq=1
        older_copy = dict(orig_tomb)
        older_copy["entry_id"] = "older_same_prefix"
        older_copy["deleted_at_source_seq"] = 1
        older_copy["deleted_at"] = float(orig_tomb["deleted_at"]) - 100.0
        mem["tombstones"]["older_same_prefix"] = older_copy

        # Also add a newer tombstone processed later with smaller seq=1 to ensure max() wins either order
        later_low = dict(orig_tomb)
        later_low["entry_id"] = "later_same_prefix_low_seq"
        later_low["deleted_at_source_seq"] = 1
        later_low["deleted_at"] = float(orig_tomb["deleted_at"]) + 100.0
        mem["tombstones"]["later_same_prefix_low_seq"] = later_low

        # Fold all tombstones into suppressed_slots
        memory._compact_tombstones(mem, fold_all=True)
        assert mem["tombstones"] == {}
        assert len(mem["suppressed_slots"]) >= 1

        memory.enqueue_extraction_job(
            data,
            task_id="task_before",
            message_ids=["m_pre_delete"],
            force_relearn=True,
        )
        claimed = memory.claim_extraction_job(data, worker_id="w_fold")
        assert claimed is not None
        return claimed

    claimed = store.transaction(fold_and_claim)

    obs = [
        {
            "observation_id": "obs_cam",
            "kind": "owner_fact",
            "scope": "personal",
            "subject": "owner",
            "stability": "durable",
            "ownership_mode": "owned_or_habitual",
            "category": "equipment",
            "fact_key": "personal.gear.camera_body",
            "text": "富士 X-T5",
            "evidence_span": "我现在的主力机身是富士 X-T5",
            "source_message_ids": ["m_pre_delete"],
        }
    ]
    ops = [
        {
            "action": "add",
            "observation_ids": ["obs_cam"],
            "target_entry_id": None,
            "expected_entry_revision": None,
            "scope": "personal",
            "kind": "owner_fact",
            "category": "equipment",
            "fact_key": "personal.gear.camera_body",
            "text": "富士 X-T5",
            "evidence_span": "我现在的主力机身是富士 X-T5",
            "source_message_ids": ["m_pre_delete"],
        }
    ]

    res = store.transaction(
        lambda data: memory_natural.commit_natural_consolidation_job(
            data,
            job_id=claimed["id"],
            worker_id="w_fold",
            lease_token=claimed["lease_token"],
            phase1_mode="observations",
            phase1_items=obs,
            phase2_operations=ops,
            permitted_snapshot=[],
        )
    )
    assert "tombstone_suppressed" in res["skipped_reasons"]
    assert res["activated_entry_ids"] == []
    snap = store.read()
    assert snap["memory"]["entries"] == {}
    assert len(snap["memory"]["suppressed_slots"]) == 1
    assert snap["memory"]["suppressed_slots"][0].get("deleted_at_source_seq", 0) >= 2


def test_s09_i25_folded_tombstone_allows_post_delete_new_message_consolidation(
    tmp_path: Path,
) -> None:
    """Rework 1b: Messages created AFTER deletion (owner_source_seq > deleted_at_source_seq)
    can still be learned normally even when the slot was folded into suppressed_slots."""
    store = _make_store_with_automatic_memory(tmp_path)
    _seed_task_messages(store, "task_old", [("m_old", "我以前的机身是富士 X-T5。")])

    def delete_and_fold(data: dict[str, Any]) -> None:
        entry = memory.upsert_active_entry(
            data,
            {
                "scope": {"kind": "personal"},
                "category": "equipment",
                "fact_key": "personal.gear.camera_body",
                "text": "富士 X-T5",
                "exposure_policy": {"mode": "destinations", "allowed_destinations": [dict(DEST_A)]},
            },
            origin="manual",
        )["entry"]
        memory.delete_memory_entry(data, entry["id"], reason="owner_deleted")
        memory._compact_tombstones(data["memory"], fold_all=True)
        assert data["memory"]["tombstones"] == {}
        assert len(data["memory"]["suppressed_slots"]) == 1

    store.transaction(delete_and_fold)

    # Now owner sends a NEW message after the deletion
    _seed_task_messages(store, "task_new", [("m_new", "我刚换了新机身索尼 A7CR。")])

    def enqueue_and_claim(data: dict[str, Any]) -> dict[str, Any]:
        memory.enqueue_extraction_job(
            data,
            task_id="task_new",
            message_ids=["m_new"],
            force_relearn=True,
        )
        claimed = memory.claim_extraction_job(data, worker_id="w_post")
        assert claimed is not None
        return claimed

    claimed = store.transaction(enqueue_and_claim)

    obs = [
        {
            "observation_id": "obs_new_cam",
            "kind": "owner_fact",
            "scope": "personal",
            "subject": "owner",
            "stability": "durable",
            "ownership_mode": "owned_or_habitual",
            "category": "equipment",
            "fact_key": "personal.gear.camera_body",
            "text": "索尼 A7CR",
            "evidence_span": "我刚换了新机身索尼 A7CR",
            "source_message_ids": ["m_new"],
        }
    ]
    ops = [
        {
            "action": "add",
            "observation_ids": ["obs_new_cam"],
            "target_entry_id": None,
            "expected_entry_revision": None,
            "scope": "personal",
            "kind": "owner_fact",
            "category": "equipment",
            "fact_key": "personal.gear.camera_body",
            "text": "索尼 A7CR",
            "evidence_span": "我刚换了新机身索尼 A7CR",
            "source_message_ids": ["m_new"],
        }
    ]

    res = store.transaction(
        lambda data: memory_natural.commit_natural_consolidation_job(
            data,
            job_id=claimed["id"],
            worker_id="w_post",
            lease_token=claimed["lease_token"],
            phase1_mode="observations",
            phase1_items=obs,
            phase2_operations=ops,
            permitted_snapshot=[],
        )
    )
    assert "tombstone_suppressed" not in res["skipped_reasons"]
    assert len(res["activated_entry_ids"]) == 1
    snap = store.read()
    learned = snap["memory"]["entries"][res["activated_entry_ids"][0]]
    assert learned["fact_key"] == "personal.gear.camera_body"
    assert learned["text"] == "索尼 A7CR"


def test_s09_i25_compact_for_lifecycle_retains_recent_control_requests_idempotency(
    tmp_path: Path,
) -> None:
    """Rework 2: compact_for_lifecycle prunes only older control_requests while retaining
    the most recent half (64), so retrying with the last pre-compaction client_request_id
    is still recognized as a duplicate request."""
    store = _make_store_with_automatic_memory(tmp_path)

    def populate_and_compact(data: dict[str, Any]) -> dict[str, Any]:
        # Fill 127 older control_requests first
        for idx in range(memory.MAX_CONTROL_REQUESTS - 1):
            memory.forget_by_slot_or_query(
                data,
                scope={"kind": "personal"},
                fact_keys=[f"personal.noop.{idx}"],
                client_request_id=f"req-{idx:03d}",
            )

        # Create a real entry and delete it with the final pre-compaction request id
        target = memory.upsert_active_entry(
            data,
            {
                "scope": {"kind": "personal"},
                "category": "preference",
                "fact_key": "personal.editor.theme",
                "text": "Solarized Dark",
            },
            origin="manual",
        )["entry"]
        first_res = memory.forget_by_slot_or_query(
            data,
            scope={"kind": "personal"},
            fact_keys=["personal.editor.theme"],
            client_request_id="req-last-before-compact",
        )
        assert first_res["deleted_ids"] == [target["id"]]
        assert len(data["memory"]["control_requests"]) == memory.MAX_CONTROL_REQUESTS

        # Run lifecycle compaction
        memory.compact_for_lifecycle(data["memory"])
        assert len(data["memory"]["control_requests"]) == memory.MAX_CONTROL_REQUESTS // 2
        assert "req-000" not in data["memory"]["control_requests"]
        assert "req-last-before-compact" in data["memory"]["control_requests"]
        return first_res

    first_res = store.transaction(populate_and_compact)

    # Re-add an active entry under the same fact_key, then retry the same pre-compaction request id:
    # idempotency protection MUST return the cached result without deleting the new entry!
    def retry_and_check_conflict(data: dict[str, Any]) -> None:
        fresh = memory.upsert_active_entry(
            data,
            {
                "scope": {"kind": "personal"},
                "category": "preference",
                "fact_key": "personal.editor.theme",
                "text": "Gruvbox Dark",
            },
            origin="manual",
        )["entry"]

        replay_res = memory.forget_by_slot_or_query(
            data,
            scope={"kind": "personal"},
            fact_keys=["personal.editor.theme"],
            client_request_id="req-last-before-compact",
        )
        assert replay_res == first_res
        # The newly added entry must NOT be deleted a second time
        assert fresh["id"] in data["memory"]["entries"]
        assert data["memory"]["entries"][fresh["id"]]["status"] == "active"

        # Reusing the same request id with a mutated payload must still raise agent_request_conflict
        with pytest.raises(AgentError, match="agent_request_conflict"):
            memory.forget_by_slot_or_query(
                data,
                scope={"kind": "personal"},
                fact_keys=["personal.editor.different_key"],
                client_request_id="req-last-before-compact",
            )

    store.transaction(retry_and_check_conflict)
