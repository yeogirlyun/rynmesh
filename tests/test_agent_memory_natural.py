"""Tests for Ryn Agent Memory Natural v2: two-stage extraction/consolidation, progressive recall, CAS, and causal ordering."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey

from rynmesh.agent_runtime import (
    memory,
    memory_integration,
    memory_natural,
    memory_runtime,
    memory_worker,
)
from rynmesh.agent_runtime.store import AgentError, AgentStore

DEST_A = {
    "catalog_model_id": "local/svc-a/model/gemini-4-argon-medium",
    "provider_peer_id": "peer-local-owner",
    "source_id": "src-local-a",
    "destination_revision": "rev-a-1",
}

DEST_B = {
    "catalog_model_id": "local/svc-b/model/claude-4-sonnet",
    "provider_peer_id": "peer-local-owner",
    "source_id": "src-local-b",
    "destination_revision": "rev-b-1",
}


class FakeModelAdapter:
    supports_destination_binding = True
    supports_output_token_limit = True
    supports_request_deadline = True

    def __init__(self, responses: list[Any]) -> None:
        self.responses = list(responses)
        self.calls: list[dict[str, Any]] = []

    def complete(
        self,
        model_id: str,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        cancelled: Any,
        on_event: Any,
        **options: Any,
    ) -> dict[str, Any]:
        self.calls.append(
            {
                "model_id": model_id,
                "messages": messages,
                "tools": tools,
                "options": options,
            }
        )
        if not self.responses:
            return {"message": {"role": "assistant", "content": json.dumps({"observations": []})}, "usage": {"total_tokens": 32}}
        resp = self.responses.pop(0)
        if isinstance(resp, Exception):
            raise resp
        if "message" not in resp and "content" in resp:
            return {
                "message": {"role": "assistant", "content": resp["content"]},
                "usage": resp.get("usage", {"total_tokens": 48}),
            }
        return resp


class FakeRuntime:
    def __init__(self, tmp_path: Path, responses: list[Any] | None = None) -> None:
        self.store = AgentStore(tmp_path / "agent", X25519PrivateKey.generate())
        self.store.transaction(lambda data: data["settings"].update(enabled=True))
        self.model = FakeModelAdapter(responses or [])
        self._models = {
            DEST_A["catalog_model_id"]: {
                "id": DEST_A["catalog_model_id"],
                "available": True,
                "agent_supported": True,
                "peer_id": DEST_A["provider_peer_id"],
                "source_id": DEST_A["source_id"],
                "destination_revision": DEST_A["destination_revision"],
            },
            DEST_B["catalog_model_id"]: {
                "id": DEST_B["catalog_model_id"],
                "available": True,
                "agent_supported": True,
                "peer_id": DEST_B["provider_peer_id"],
                "source_id": DEST_B["source_id"],
                "destination_revision": DEST_B["destination_revision"],
            },
        }

    def _model_metadata(self, model_id: str) -> dict[str, Any] | None:
        return self._models.get(model_id)

    def _bound_destination(self, model: dict[str, Any]) -> dict[str, str]:
        return {
            "catalog_model_id": str(model["id"]),
            "provider_peer_id": str(model["peer_id"]),
            "source_id": str(model["source_id"]),
            "destination_revision": str(model["destination_revision"]),
        }

    def _current_destination(self, model_id: str, *, require_bound: bool = False) -> dict[str, str]:
        del require_bound
        model = self._model_metadata(model_id)
        if not model:
            raise AgentError("agent_model_unavailable")
        return self._bound_destination(model)


def _seed_completed_task(
    runtime: FakeRuntime,
    task_id: str,
    user_messages: list[tuple[str, str]],
    *,
    conversation_revision: int = 1,
    destination: dict[str, str] | None = None,
    project: dict[str, Any] | None = None,
) -> dict[str, Any]:
    dest = dict(destination or DEST_A)

    def tx(data: dict[str, Any]) -> dict[str, Any]:
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
        row: dict[str, Any] = {
            "id": task_id,
            "status": "completed",
            "archived": False,
            "revision": 1,
            "conversation_revision": conversation_revision,
            "context_epoch": 0,
            "model_id": dest["catalog_model_id"],
            "model_destination": dest,
            "messages": msgs,
            "events": [],
            "in_flight": None,
            "pending_control": None,
            "pending_user_messages": [],
            "pending_approval": None,
            "pending_input": None,
            "queue": [],
        }
        if project is not None:
            row["project"] = dict(project)
            data.setdefault("projects", {})[project["id"]] = {
                "id": project["id"],
                "path": project.get("path", "D:/code/demo"),
                "generation": project.get("generation", 1),
                "enabled": True,
            }
        data.setdefault("tasks", {})[task_id] = row
        return row

    return runtime.store.transaction(tx)


def _enable_memory(
    runtime: FakeRuntime,
    *,
    activation_policy: str = "automatic",
    allowed_destinations: list[dict[str, str]] | None = None,
    default_mode: str = "selected_models",
) -> dict[str, Any]:
    def tx(data: dict[str, Any]) -> dict[str, Any]:
        mem = memory.ensure_memory_state(data)
        return memory.update_memory_settings(
            data,
            {
                "capture_enabled": True,
                "use_enabled": True,
                "activation_policy": activation_policy,
                "extraction_destination": dict(DEST_A),
                "default_exposure": {
                    "mode": default_mode,
                    "allowed_destinations": list(allowed_destinations if allowed_destinations is not None else [DEST_A]),
                },
            },
            expected_generation=mem["generation"],
        )

    return runtime.store.transaction(tx)


def test_natural_two_stage_consolidation_update_and_retraction(tmp_path: Path) -> None:
    """Natural statements activate via 2-stage consolidation, update with CAS, and retract without dropping sibling slots."""
    stage1_turn1 = {
        "observations": [
            {
                "observation_id": "obs_body",
                "kind": "owner_fact",
                "scope": "personal",
                "subject": "owner",
                "stability": "durable",
                "ownership_mode": "owned_or_habitual",
                "category": "equipment",
                "fact_key": "personal.gear.camera_body",
                "text": "Owner uses a Fujifilm X-T5 body.",
                "evidence_span": "平时出门拍照我一直用富士 X-T5 机身",
                "source_message_ids": ["m1"],
            },
            {
                "observation_id": "obs_lens",
                "kind": "owner_fact",
                "scope": "personal",
                "subject": "owner",
                "stability": "durable",
                "ownership_mode": "owned_or_habitual",
                "category": "equipment",
                "fact_key": "personal.gear.lens",
                "text": "Owner pairs the camera with an XF 35mm F1.4 lens.",
                "evidence_span": "配的是 XF 35mm F1.4 镜头",
                "source_message_ids": ["m1"],
            },
        ]
    }
    stage2_turn1 = {
        "operations": [
            {
                "action": "add",
                "observation_ids": ["obs_body"],
                "target_entry_id": None,
                "expected_entry_revision": None,
                "scope": "personal",
                "kind": "owner_fact",
                "category": "equipment",
                "fact_key": "personal.gear.camera_body",
                "text": "Owner uses a Fujifilm X-T5 body.",
                "evidence_span": "平时出门拍照我一直用富士 X-T5 机身",
                "source_message_ids": ["m1"],
            },
            {
                "action": "add",
                "observation_ids": ["obs_lens"],
                "target_entry_id": None,
                "expected_entry_revision": None,
                "scope": "personal",
                "kind": "owner_fact",
                "category": "equipment",
                "fact_key": "personal.gear.lens",
                "text": "Owner pairs the camera with an XF 35mm F1.4 lens.",
                "evidence_span": "配的是 XF 35mm F1.4 镜头",
                "source_message_ids": ["m1"],
            },
        ]
    }

    runtime = FakeRuntime(
        tmp_path,
        responses=[
            {"content": json.dumps(stage1_turn1, ensure_ascii=False)},
            {"content": json.dumps(stage2_turn1, ensure_ascii=False)},
        ],
    )
    _enable_memory(runtime, activation_policy="automatic", allowed_destinations=[DEST_A])
    _seed_completed_task(
        runtime,
        "task_1",
        [("m1", "平时出门拍照我一直用富士 X-T5 机身，配的是 XF 35mm F1.4 镜头。")],
    )

    worker = memory_worker.MemoryExtractionWorker(runtime)
    run_res1 = worker.run_once()
    assert run_res1.activity is True
    assert len(runtime.model.calls) == 2
    assert runtime.model.calls[0]["tools"] == []
    assert runtime.model.calls[1]["tools"] == []

    snap1 = runtime.store.read()
    entries1 = {
        e["fact_key"]: e
        for e in snap1["memory"]["entries"].values()
        if e.get("status") == "active"
    }
    assert set(entries1.keys()) == {"personal.gear.camera_body", "personal.gear.lens"}
    body_id = entries1["personal.gear.camera_body"]["id"]
    lens_id = entries1["personal.gear.lens"]["id"]
    assert entries1["personal.gear.camera_body"]["origin"] == "automatic_consolidation"
    assert snap1["memory"]["daily_usage"]["extraction_calls"] == 2

    # Turn 2: Owner retracts camera_body ("把 X-T5 机身卖掉了") while keeping the lens from m1 intact
    stage1_turn2 = {
        "observations": [
            {
                "observation_id": "obs_sold_body",
                "kind": "owner_fact",
                "scope": "personal",
                "subject": "owner",
                "stability": "durable",
                "ownership_mode": "replaced_or_sold",
                "category": "equipment",
                "fact_key": "personal.gear.camera_body",
                "text": "Owner sold the Fujifilm X-T5 body.",
                "evidence_span": "我上礼拜把富士 X-T5 机身卖掉了",
                "source_message_ids": ["m2"],
            }
        ]
    }
    stage2_turn2 = {
        "operations": [
            {
                "action": "retract",
                "observation_ids": ["obs_sold_body"],
                "target_entry_id": body_id,
                "expected_entry_revision": entries1["personal.gear.camera_body"]["revision"],
                "scope": "personal",
                "kind": "owner_fact",
                "category": "equipment",
                "fact_key": "personal.gear.camera_body",
                "text": "Owner sold the Fujifilm X-T5 body.",
                "evidence_span": "我上礼拜把富士 X-T5 机身卖掉了",
                "source_message_ids": ["m2"],
            }
        ]
    }
    runtime.model.responses.extend([
        {"content": json.dumps(stage1_turn2, ensure_ascii=False)},
        {"content": json.dumps(stage2_turn2, ensure_ascii=False)},
    ])
    _seed_completed_task(
        runtime,
        "task_2",
        [("m2", "我上礼拜把富士 X-T5 机身卖掉了。")],
    )

    run_res2 = worker.run_once()
    assert run_res2.activity is True
    snap2 = runtime.store.read()
    active2 = {
        e["fact_key"]: e
        for e in snap2["memory"]["entries"].values()
        if e.get("status") == "active"
    }
    # Camera body was retracted, but sibling lens entry from m1 MUST remain active and recallable
    assert set(active2.keys()) == {"personal.gear.lens"}
    assert active2["personal.gear.lens"]["id"] == lens_id
    assert body_id in snap2["memory"]["tombstones"]
    assert snap2["memory"]["tombstones"][body_id]["reason"] == "natural_retraction"


def test_progressive_recall_compact_profile_and_guarded_details(tmp_path: Path) -> None:
    """Authorized compact facts help new chats; source details remain available on demand."""
    runtime = FakeRuntime(tmp_path)
    _enable_memory(runtime, activation_policy="automatic", allowed_destinations=[DEST_A])
    row = _seed_completed_task(
        runtime,
        "task_recall",
        [("m_seed", "我的主力相机是理光 GR IIIx，平时喜欢街拍。")],
    )

    def seed_entry(data: dict[str, Any]) -> str:
        src = memory_runtime.build_task_owner_source_ref(row)
        res = memory.upsert_active_entry(
            data,
            {
                "scope": {"kind": "personal"},
                "category": "equipment",
                "fact_key": "personal.gear.compact_camera",
                "text": "主力随身相机是理光 GR IIIx（40mm 等效焦段）",
                "kind": "owner_fact",
                "sources": [src],
                "exposure_policy": {"mode": "destinations", "allowed_destinations": [DEST_A]},
            },
            origin="automatic_consolidation",
        )
        return res["entry"]["id"]

    entry_id = runtime.store.transaction(seed_entry)
    snap = runtime.store.read()

    # Compact profile values are legitimate; full entry/source arrays are not eagerly injected.
    sel = memory.select_memories_for_task(
        snap,
        row,
        query_text="周末出去轻装扫街带什么合适？",
    )
    assert sel["selected"] == []
    assert sel["include_navigation"] is True
    assert len(sel["navigation_index"]) == 1
    nav_item = sel["navigation_index"][0]
    assert nav_item["id"] == entry_id
    assert nav_item["category"] == "equipment"
    assert nav_item["fact_key"] == "personal.gear.compact_camera"
    assert "text" not in nav_item
    assert "cue" not in nav_item
    assert "summary" not in nav_item
    assert "GR IIIx" not in json.dumps(sel["navigation_index"], ensure_ascii=False)
    assert "GR IIIx" in json.dumps(sel["profile_summary"], ensure_ascii=False)
    assert len(json.dumps({key: sel[key] for key in ("profile_summary", "navigation_index")},
                          ensure_ascii=False).encode("utf-8")) <= sel["max_bytes"]

    ctx_msgs = memory_runtime.format_memory_context_messages(sel)
    assert len(ctx_msgs) == 1
    assert "navigation_index" in ctx_msgs[0]["content"]
    assert "GR IIIx" in ctx_msgs[0]["content"]
    assert "我的主力相机是理光 GR IIIx，平时喜欢街拍。" not in ctx_msgs[0]["content"]

    # Model-directed progressive recall via ryn_memory_search retrieves the full entry
    found = memory.search_memories(
        snap,
        for_model_tool=True,
        task_row=row,
        topic="equipment/personal.gear.compact_camera",
        entry_ids=[entry_id],
    )
    assert len(found["entries"]) == 1
    assert found["entries"][0]["id"] == entry_id
    assert "GR IIIx" in found["entries"][0]["text"]

    # Unauthorized destination DEST_B receives neither navigation_index nor search results
    row_unauth = {**row, "model_id": DEST_B["catalog_model_id"], "model_destination": DEST_B}
    sel_unauth = memory.select_memories_for_task(snap, row_unauth, query_text="扫街带什么？")
    assert sel_unauth["selected"] == []
    assert sel_unauth["navigation_index"] == []
    found_unauth = memory.search_memories(snap, for_model_tool=True, task_row=row_unauth, query="相机")
    assert found_unauth["entries"] == []


def test_cas_revision_and_owner_acceptance_order_barrier(tmp_path: Path) -> None:
    """Older background jobs cannot overwrite newer manual/explicit owner edits, and stale CAS revisions are skipped."""
    runtime = FakeRuntime(tmp_path)
    _enable_memory(runtime, activation_policy="automatic", allowed_destinations=[DEST_A])
    # 1. Task message arrives first (owner_source_seq = 1)
    _seed_completed_task(
        runtime,
        "task_old",
        [("m_old", "我平时用富士 X-T5 机身。")],
    )

    # 2. Enqueue and claim the background job for m_old (source_seq = 1)
    def enqueue_and_claim(data: dict[str, Any]) -> dict[str, Any]:
        memory.enqueue_extraction_job(data, task_id="task_old", message_ids=["m_old"])
        claimed = memory.claim_extraction_job(data, worker_id="w1")
        assert claimed is not None
        return claimed

    claimed_job = runtime.store.transaction(enqueue_and_claim)

    # 3. Before the background job commits, the owner manually updates the same slot (owner_acceptance_seq = 2 > 1)
    def manual_owner_update(data: dict[str, Any]) -> dict[str, Any]:
        res = memory.upsert_active_entry(
            data,
            {
                "scope": {"kind": "personal"},
                "category": "equipment",
                "fact_key": "personal.gear.camera_body",
                "text": "Owner manually set camera body to Nikon Z8.",
                "kind": "owner_fact",
                "exposure_policy": {"mode": "destinations", "allowed_destinations": [DEST_A]},
            },
            origin="manual",
        )
        return res["entry"]

    manual_entry = runtime.store.transaction(manual_owner_update)
    assert manual_entry["owner_acceptance_seq"] > 1

    # 4. Now the older background job (source_seq = 1) attempts to update manual_entry (even with matching revision 1)
    obs_old = [
        {
            "observation_id": "obs_1",
            "kind": "owner_fact",
            "scope": "personal",
            "subject": "owner",
            "stability": "durable",
            "ownership_mode": "owned_or_habitual",
            "category": "equipment",
            "fact_key": "personal.gear.camera_body",
            "text": "Owner uses Fujifilm X-T5.",
            "evidence_span": "我平时用富士 X-T5 机身",
            "source_message_ids": ["m_old"],
        }
    ]
    ops_old = [
        {
            "action": "update",
            "observation_ids": ["obs_1"],
            "target_entry_id": manual_entry["id"],
            "expected_entry_revision": manual_entry["revision"],
            "scope": "personal",
            "kind": "owner_fact",
            "category": "equipment",
            "fact_key": "personal.gear.camera_body",
            "text": "Owner uses Fujifilm X-T5.",
            "evidence_span": "我平时用富士 X-T5 机身",
            "source_message_ids": ["m_old"],
        }
    ]

    def try_commit_stale_order(data: dict[str, Any]) -> dict[str, Any]:
        snap_permitted = memory_natural.select_existing_memories_for_consolidation(data, claimed_job, DEST_A)
        return memory_natural.commit_natural_consolidation_job(
            data,
            job_id=claimed_job["id"],
            worker_id="w1",
            lease_token=claimed_job["lease_token"],
            phase1_mode="observations",
            phase1_items=obs_old,
            phase2_operations=ops_old,
            permitted_snapshot=snap_permitted,
        )

    commit_res = runtime.store.transaction(try_commit_stale_order)
    assert commit_res["state"] == "stale"
    assert "stale_source_order" in commit_res["skipped_reasons"]

    # Active entry must remain Nikon Z8
    snap_after = runtime.store.read()
    assert snap_after["memory"]["entries"][manual_entry["id"]]["text"] == "Owner manually set camera body to Nikon Z8."


def test_scoped_tombstone_isolation_and_default_exposure_fail_closed(tmp_path: Path) -> None:
    """Unrelated tombstones cannot authorize restoring a deleted slot, and empty default_exposure fails closed to local_only."""
    runtime = FakeRuntime(tmp_path)
    # Configure default_exposure with restricted/empty allowed_destinations
    _enable_memory(runtime, activation_policy="automatic", allowed_destinations=[], default_mode="restricted")

    _seed_completed_task(
        runtime,
        "task_tomb",
        [
            ("m_early", "我的机身是富士 X-T5。"),
            ("m_other", "我的三脚架是峰设旅行脚架。"),
        ],
    )

    def setup_tombstones_and_job(data: dict[str, Any]) -> dict[str, Any]:
        # Create and delete camera_body after m_early (so deleted_at_source_seq > m_early.owner_source_seq)
        e_cam = memory.upsert_active_entry(
            data,
            {
                "scope": {"kind": "personal"},
                "category": "equipment",
                "fact_key": "personal.gear.camera_body",
                "text": "富士 X-T5",
                "exposure_policy": {"mode": "destinations", "allowed_destinations": [DEST_A]},
            },
            origin="manual",
        )["entry"]
        memory.delete_memory_entry(data, e_cam["id"], reason="owner_deleted")

        # Also create and delete an UNRELATED slot with a very old deleted_at_source_seq = 0
        e_other = memory.upsert_active_entry(
            data,
            {
                "scope": {"kind": "personal"},
                "category": "equipment",
                "fact_key": "personal.gear.bag",
                "text": "Peak Design Everyday Sling",
                "exposure_policy": {"mode": "destinations", "allowed_destinations": [DEST_A]},
            },
            origin="manual",
        )["entry"]
        memory.delete_memory_entry(data, e_other["id"], reason="owner_deleted")
        data["memory"]["tombstones"][e_other["id"]]["deleted_at_source_seq"] = 0

        memory.enqueue_extraction_job(data, task_id="task_tomb", message_ids=["m_early", "m_other"], force_relearn=True)
        claimed = memory.claim_extraction_job(data, worker_id="w1")
        assert claimed is not None
        return claimed

    claimed = runtime.store.transaction(setup_tombstones_and_job)

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
            "evidence_span": "我的机身是富士 X-T5",
            "source_message_ids": ["m_early"],
        },
        {
            "observation_id": "obs_tripod",
            "kind": "owner_fact",
            "scope": "personal",
            "subject": "owner",
            "stability": "durable",
            "ownership_mode": "owned_or_habitual",
            "category": "equipment",
            "fact_key": "personal.gear.tripod",
            "text": "峰设旅行脚架",
            "evidence_span": "我的三脚架是峰设旅行脚架",
            "source_message_ids": ["m_other"],
        },
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
            "evidence_span": "我的机身是富士 X-T5",
            "source_message_ids": ["m_early"],
        },
        {
            "action": "add",
            "observation_ids": ["obs_tripod"],
            "target_entry_id": None,
            "expected_entry_revision": None,
            "scope": "personal",
            "kind": "owner_fact",
            "category": "equipment",
            "fact_key": "personal.gear.tripod",
            "text": "峰设旅行脚架",
            "evidence_span": "我的三脚架是峰设旅行脚架",
            "source_message_ids": ["m_other"],
        },
    ]

    def run_commit(data: dict[str, Any]) -> dict[str, Any]:
        return memory_natural.commit_natural_consolidation_job(
            data,
            job_id=claimed["id"],
            worker_id="w1",
            lease_token=claimed["lease_token"],
            phase1_mode="observations",
            phase1_items=obs,
            phase2_operations=ops,
            permitted_snapshot=[],
        )

    res = runtime.store.transaction(run_commit)
    # camera_body must be suppressed by its own tombstone (despite unrelated bag tombstone having seq=0)
    assert "tombstone_suppressed" in res["skipped_reasons"]
    # tripod is added, and because default_exposure is restricted/empty, its exposure_policy MUST fail closed to local_only
    assert len(res["activated_entry_ids"]) == 1
    tripod_id = res["activated_entry_ids"][0]
    snap = runtime.store.read()
    tripod_entry = snap["memory"]["entries"][tripod_id]
    assert tripod_entry["fact_key"] == "personal.gear.tripod"
    assert tripod_entry["exposure_policy"]["mode"] == "local_only"
    assert tripod_entry["exposure_policy"]["allowed_destinations"] == []


def test_strict_observation_authority_and_evidence_message_coupling(tmp_path: Path) -> None:
    """Rejects missing authority fields, project->personal promotion, borrowed items, and extra ungrounded source_message_ids."""
    runtime = FakeRuntime(tmp_path)
    _enable_memory(runtime, activation_policy="automatic", allowed_destinations=[DEST_A])
    _seed_completed_task(
        runtime,
        "task_strict",
        [
            ("m1", "我平时习惯用 Capture One 修图。"),
            ("m2", "今天天气真不错，这周末借朋友的徕卡 M11 玩两天。\n> 引用：我一直用索尼 A7R5"),
        ],
        project={"id": "proj-1", "path": "D:/code/demo", "generation": 1},
    )

    def get_job(data: dict[str, Any]) -> dict[str, Any]:
        memory.enqueue_extraction_job(data, task_id="task_strict", message_ids=["m1", "m2"], force_relearn=True)
        claimed = memory.claim_extraction_job(data, worker_id="w1")
        assert claimed is not None
        return claimed

    job = runtime.store.transaction(get_job)

    # 1. Extra ungrounded message ID (m2 does not contain m1's evidence_span) -> rejected
    ok_extra_msg, reason_extra, _ = memory_natural.verify_natural_observation_grounding(
        {
            "observation_id": "obs_extra_msg",
            "kind": "owner_preference",
            "scope": "personal",
            "subject": "owner",
            "stability": "durable",
            "ownership_mode": "owned_or_habitual",
            "category": "workflow",
            "fact_key": "personal.workflow.raw_editor",
            "text": "Uses Capture One for RAW editing.",
            "evidence_span": "我平时习惯用 Capture One 修图",
            "source_message_ids": ["m1", "m2"],
        },
        job,
    )
    assert ok_extra_msg is False
    assert reason_extra == "evidence_span_not_in_source"

    # 2. Project subject attempting to promote to personal scope -> rejected
    ok_proj_promo, reason_promo, _ = memory_natural.verify_natural_observation_grounding(
        {
            "observation_id": "obs_promo",
            "kind": "project_fact",
            "scope": "personal",
            "subject": "project",
            "stability": "durable",
            "ownership_mode": "owned_or_habitual",
            "category": "project",
            "fact_key": "project.editor",
            "text": "Project uses Capture One",
            "evidence_span": "我平时习惯用 Capture One 修图",
            "source_message_ids": ["m1"],
        },
        job,
    )
    assert ok_proj_promo is False
    assert reason_promo == "project_subject_cannot_promote_to_personal"

    # 3. Borrowed friend's item -> rejected
    ok_borrow, reason_borrow, _ = memory_natural.verify_natural_observation_grounding(
        {
            "observation_id": "obs_borrow",
            "kind": "owner_fact",
            "scope": "personal",
            "subject": "owner",
            "stability": "durable",
            "ownership_mode": "owned_or_habitual",
            "category": "equipment",
            "fact_key": "personal.gear.camera_body",
            "text": "Owner uses Leica M11",
            "evidence_span": "借朋友的徕卡 M11 玩两天",
            "source_message_ids": ["m2"],
        },
        job,
    )
    assert ok_borrow is False
    assert "temporary_or_borrowed" in reason_borrow

    # 4. Quoted line in m2 -> rejected
    ok_quote, reason_quote, _ = memory_natural.verify_natural_observation_grounding(
        {
            "observation_id": "obs_quote",
            "kind": "owner_fact",
            "scope": "personal",
            "subject": "owner",
            "stability": "durable",
            "ownership_mode": "owned_or_habitual",
            "category": "equipment",
            "fact_key": "personal.gear.camera_body",
            "text": "Owner uses Sony A7R5",
            "evidence_span": "我一直用索尼 A7R5",
            "source_message_ids": ["m2"],
        },
        job,
    )
    assert ok_quote is False
    assert reason_quote == "evidence_span_not_in_source"


def test_pre_consent_automatic_frontier_and_stage2_failure_never_auto_activates(tmp_path: Path) -> None:
    """Pre-consent messages are skipped in automatic mode, and Stage-2 failure never auto-activates Stage-1 observations."""
    runtime = FakeRuntime(tmp_path)
    # Task created BEFORE enabling automatic capture
    _seed_completed_task(
        runtime,
        "task_pre_consent",
        [("m_pre", "我平时习惯用 Lightroom Classic 管理照片。")],
    )
    _enable_memory(runtime, activation_policy="automatic", allowed_destinations=[DEST_A])

    worker = memory_worker.MemoryExtractionWorker(runtime)
    res_pre = worker.run_once()
    assert res_pre.activity is False
    assert len(runtime.model.calls) == 0

    # New task AFTER consent, where Stage 1 succeeds but Stage 2 returns invalid JSON
    stage1_ok = {
        "observations": [
            {
                "observation_id": "obs_1",
                "kind": "owner_preference",
                "scope": "personal",
                "subject": "owner",
                "stability": "durable",
                "ownership_mode": "owned_or_habitual",
                "category": "workflow",
                "fact_key": "personal.workflow.catalog_app",
                "text": "Uses Photo Mechanic for culling.",
                "evidence_span": "我选片一直用 Photo Mechanic",
                "source_message_ids": ["m_post"],
            }
        ]
    }
    runtime.model.responses.extend([
        {"content": json.dumps(stage1_ok, ensure_ascii=False)},
        {"content": "NOT VALID STAGE 2 JSON"},
    ])
    _seed_completed_task(
        runtime,
        "task_post_consent",
        [("m_post", "我选片一直用 Photo Mechanic。")],
    )

    res_post = worker.run_once()
    assert res_post.activity is True
    assert len(runtime.model.calls) == 2
    snap = runtime.store.read()
    assert snap["memory"]["entries"] == {}
    jobs = list(snap["memory"]["jobs"].values())
    assert len(jobs) == 1
    assert jobs[0]["state"] == "failed"
    assert jobs[0]["error_code"] == "invalid_consolidation_json"
