"""Stage 1 transactional tests for rynmesh.agent_runtime.memory with real encrypted AgentStore."""
from __future__ import annotations

import copy
import json

import pytest
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey

from rynmesh.agent_runtime import knowledge, memory
from rynmesh.agent_runtime.store import AgentError, AgentStore


def _make_store(tmp_path):
    key = X25519PrivateKey.generate()
    return AgentStore(tmp_path, key), key


def _seed_task(
    data: dict,
    *,
    task_id: str = "task-photo-1",
    message_id: str = "msg-owner-1",
    text: str = "请记住我喜欢摄影，我的机身是富士 X-T5，镜头是 XF16-55mm F2.8",
    conversation_revision: int = 1,
    context_epoch: int = 0,
    project: dict | None = None,
    destination: dict | None = None,
    archived: bool = False,
) -> dict:
    dest = destination or {
        "catalog_model_id": "local/codex-cli",
        "provider_peer_id": "self",
        "source_id": "codex-cli",
        "destination_revision": "rev-1",
    }
    row = {
        "id": task_id,
        "title": "Photography setup",
        "model_id": dest["catalog_model_id"],
        "model_destination": copy.deepcopy(dest),
        "status": "completed",
        "revision": 1,
        "control_revision": 1,
        "scope_revision": 1,
        "conversation_revision": conversation_revision,
        "context_epoch": context_epoch,
        "archived": archived,
        "project": copy.deepcopy(project) if project else None,
        "goal": {"revision": 1, "text": text, "original_message_id": message_id, "constraints": []},
        "messages": [
            {
                "role": "user",
                "message_id": message_id,
                "content": text,
                "context_epoch": context_epoch,
            }
        ],
        "events": [],
    }
    data.setdefault("tasks", {})[task_id] = row
    return row


def test_encrypted_persistence_reload_and_explicit_owner_lifecycle(tmp_path):
    store, key = _make_store(tmp_path)
    secret_camera = "富士 X-T5"
    secret_lens = "XF16-55mm F2.8"
    replacement_camera = "富士 X-H2"

    def create_initial(data):
        row = _seed_task(data)
        src = memory.build_source_ref(
            source_type="task_message",
            task_id=row["id"],
            message_id="msg-owner-1",
            conversation_revision=row["conversation_revision"],
            context_epoch=row["context_epoch"],
            content=row["messages"][0]["content"],
        )
        r1 = memory.upsert_active_entry(
            data,
            {
                "scope": {"kind": "personal"},
                "category": "photography",
                "fact_key": "personal.hobby.photography",
                "text": "喜欢摄影",
                "sources": [src],
            },
            origin="explicit_owner_chat",
            client_request_id="req-save-hobby",
        )
        r2 = memory.upsert_active_entry(
            data,
            {
                "scope": {"kind": "personal"},
                "category": "equipment",
                "fact_key": "personal.gear.camera_body",
                "text": f"相机机身是 {secret_camera}",
                "sources": [src],
            },
            origin="explicit_owner_chat",
            client_request_id="req-save-body",
        )
        r3 = memory.upsert_active_entry(
            data,
            {
                "scope": {"kind": "personal"},
                "category": "equipment",
                "fact_key": "personal.gear.lens",
                "text": f"常用镜头是 {secret_lens}",
                "sources": [src],
            },
            origin="explicit_owner_chat",
            client_request_id="req-save-lens",
        )
        return r1, r2, r3

    r1, r2, r3 = store.transaction(create_initial)
    assert r1["entry"]["revision"] == 1
    assert r2["entry"]["revision"] == 1
    assert r3["entry"]["revision"] == 1
    assert r3["generation"] == 3

    # Verify encrypted on disk: no plaintext in sessions.json or task files
    raw_sessions = (tmp_path / "sessions.json").read_text(encoding="utf-8")
    assert secret_camera not in raw_sessions
    assert secret_lens not in raw_sessions
    assert "personal.gear.camera_body" not in raw_sessions

    # Reload via fresh AgentStore instance with same key
    reloaded_store = AgentStore(tmp_path, key)
    snap = reloaded_store.inspect(memory.list_memories_snapshot)
    assert len(snap["entries"]) == 3
    assert snap["generation"] == 3

    # Update camera body from X-T5 to X-H2 with revision check
    body_entry_id = r2["entry"]["id"]

    def update_camera(data):
        row = data["tasks"]["task-photo-1"]
        row["conversation_revision"] = 2
        row["messages"].append(
            {
                "role": "user",
                "message_id": "msg-owner-2",
                "content": "把我的机身改成 X-H2，X-T5 已经卖掉了",
                "context_epoch": 0,
            }
        )
        src2 = memory.build_source_ref(
            source_type="task_message",
            task_id=row["id"],
            message_id="msg-owner-2",
            conversation_revision=2,
            context_epoch=0,
            content=row["messages"][-1]["content"],
        )
        return memory.upsert_active_entry(
            data,
            {
                "id": body_entry_id,
                "scope": {"kind": "personal"},
                "category": "equipment",
                "fact_key": "personal.gear.camera_body",
                "text": f"相机机身是 {replacement_camera}",
                "sources": [src2],
            },
            origin="explicit_owner_chat",
            expected_entry_revision=1,
            client_request_id="req-update-body",
        )

    updated = reloaded_store.transaction(update_camera)
    assert updated["entry"]["id"] == body_entry_id
    assert updated["entry"]["revision"] == 2
    assert replacement_camera in updated["entry"]["text"]
    assert secret_camera not in updated["entry"]["text"]

    # Forget camera and lens facts and verify opaque tombstones contain no plaintext
    def forget_gear(data):
        return memory.forget_by_slot_or_query(
            data,
            scope={"kind": "personal"},
            categories=["equipment"],
            client_request_id="req-forget-gear",
        )

    forgotten = reloaded_store.transaction(forget_gear)
    assert forgotten["deleted_count"] == 2
    after_forget = reloaded_store.read()
    assert len(after_forget["memory"]["entries"]) == 1
    tombstones_json = json.dumps(after_forget["memory"]["tombstones"], ensure_ascii=False)
    assert secret_camera not in tombstones_json
    assert replacement_camera not in tombstones_json
    assert secret_lens not in tombstones_json


def test_candidates_not_injected_until_accepted_and_never_overwrite_active(tmp_path):
    store, _ = _make_store(tmp_path)

    def setup_and_extract(data):
        dest = {
            "catalog_model_id": "local/codex-cli",
            "provider_peer_id": "self",
            "source_id": "codex-cli",
            "destination_revision": "rev-1",
        }
        memory.update_memory_settings(
            data,
            {"capture_enabled": True, "use_enabled": True, "extraction_destination": dest},
        )
        _seed_task(
            data,
            task_id="task-chat-1",
            message_id="msg-1",
            text="我平时用富士 X-T5 和 XF16-55mm F2.8 拍人像",
            destination=dest,
        )
        enq = memory.enqueue_extraction_job(data, task_id="task-chat-1")
        assert enq["queued"] is True
        leased = memory.claim_extraction_job(data, worker_id="worker-1")
        assert leased is not None and leased["id"] == enq["job"]["id"]
        assert leased.get("lease_token")
        committed = memory.commit_extraction_job(
            data,
            job_id=leased["id"],
            worker_id="worker-1",
            lease_token=leased["lease_token"],
            proposals=[
                {
                    "category": "equipment",
                    "fact_key": "personal.gear.camera_body",
                    "text": "机身是富士 X-T5",
                    "source_message_ids": ["msg-1"],
                }
            ],
            tokens_used=120,
        )
        return committed

    committed = store.transaction(setup_and_extract)
    assert committed["state"] == "committed"
    assert len(committed["committed_candidates"]) == 1
    cand_id = committed["committed_candidates"][0]["id"]

    # Pending candidate must NOT be returned by select_memories_for_task
    def check_unaccepted(data):
        query_task = _seed_task(
            data,
            task_id="task-query-1",
            message_id="msg-q1",
            text="按我现有设备推荐一个人像拍摄设置",
        )
        return memory.select_memories_for_task(data, query_task)

    selection_before = store.transaction(check_unaccepted)
    assert selection_before["selected"] == []

    # Accept candidate -> becomes active -> now selected for relevant task
    def accept(data):
        return memory.decide_candidate(
            data,
            cand_id,
            decision="accept",
            expected_candidate_revision=1,
            client_request_id="req-accept-1",
        )

    accepted = store.transaction(accept)
    assert accepted["candidate"]["status"] == "accepted"
    assert accepted["entry"]["status"] == "active"
    assert accepted["entry"]["text"] == "机身是富士 X-T5"

    def check_accepted(data):
        query_task = data["tasks"]["task-query-1"]
        return memory.select_memories_for_task(data, query_task)

    selection_after = store.inspect(check_accepted)
    assert len(selection_after["selected"]) == 1
    assert selection_after["selected"][0]["text"] == "机身是富士 X-T5"
    assert selection_after["selected"][0]["sources"][0]["task_id"] == "task-chat-1"
    assert selection_after["selected"][0]["sources"][0]["message_id"] == "msg-1"

    # Unrelated SQL task should NOT pull in photography gear
    def check_unrelated_sql(data):
        sql_task = _seed_task(
            data,
            task_id="task-sql-1",
            message_id="msg-sql-1",
            text="Write a PostgreSQL migration to add a composite index on orders(customer_id, created_at).",
        )
        return memory.select_memories_for_task(data, sql_task)

    sql_selection = store.transaction(check_unrelated_sql)
    assert sql_selection["selected"] == []


def test_project_and_destination_isolation_and_manual_preferences_independence(tmp_path):
    store, _ = _make_store(tmp_path)
    dest_a = {
        "catalog_model_id": "local/model-a",
        "provider_peer_id": "self",
        "source_id": "src-a",
        "destination_revision": "rev-a1",
    }
    dest_b = {
        "catalog_model_id": "peer/model-b",
        "provider_peer_id": "peer-node-2",
        "source_id": "src-b",
        "destination_revision": "rev-b1",
    }
    proj_a = {"id": "proj-alpha", "name": "Alpha", "path": "D:\\code\\Alpha", "generation": 1}
    proj_b = {"id": "proj-beta", "name": "Beta", "path": "D:\\code\\Beta", "generation": 1}

    def seed_memories_and_prefs(data):
        knowledge.update_preferences(
            data,
            {"expected_revision": 0, "items": [{"id": "pref-1", "text": "Use concise bullet points"}]},
        )
        memory.set_manual_preference_exposure(
            data,
            "pref-1",
            expected_preferences_revision=1,
            exposure_policy={"mode": "destinations", "allowed_destinations": [dest_a]},
        )
        # Project Alpha memory
        memory.upsert_active_entry(
            data,
            {
                "scope": {"kind": "project", "project_id": "proj-alpha", "project_generation": 1},
                "category": "project",
                "fact_key": "project.alpha.CameraPipeline",
                "text": "Alpha 项目使用 RAW 图像色彩处理管线",
                "exposure_policy": {"mode": "all_selected"},
            },
            origin="manual",
        )
        # Personal memory restricted to dest_a only
        memory.upsert_active_entry(
            data,
            {
                "scope": {"kind": "personal"},
                "category": "equipment",
                "fact_key": "personal.gear.camera_body",
                "text": "我的个人机身是富士 X-T5",
                "exposure_policy": {"mode": "destinations", "allowed_destinations": [dest_a]},
            },
            origin="manual",
        )

    store.transaction(seed_memories_and_prefs)

    # Verify manual preferences API format is untouched
    prefs_snap = store.inspect(knowledge.preferences)
    assert prefs_snap == {"revision": 1, "items": [{"id": "pref-1", "text": "Use concise bullet points"}]}

    def query_projections(data):
        task_alpha_dest_a = _seed_task(
            data,
            task_id="t-alpha-a",
            message_id="m1",
            text="检查项目的图像管线和我的摄影器材机身",
            project=proj_a,
            destination=dest_a,
        )
        task_beta_dest_a = _seed_task(
            data,
            task_id="t-beta-a",
            message_id="m2",
            text="检查项目的图像管线和我的摄影器材机身",
            project=proj_b,
            destination=dest_a,
        )
        task_alpha_dest_b = _seed_task(
            data,
            task_id="t-alpha-b",
            message_id="m3",
            text="检查项目的图像管线和我的摄影器材机身",
            project=proj_a,
            destination=dest_b,
        )
        sel_alpha_a = memory.select_memories_for_task(data, task_alpha_dest_a)
        sel_beta_a = memory.select_memories_for_task(data, task_beta_dest_a)
        sel_alpha_b = memory.select_memories_for_task(data, task_alpha_dest_b)
        pref_alpha_a = memory.filter_manual_preferences_for_task(data, task_alpha_dest_a, knowledge.preferences(data))
        pref_alpha_b = memory.filter_manual_preferences_for_task(data, task_alpha_dest_b, knowledge.preferences(data))
        return sel_alpha_a, sel_beta_a, sel_alpha_b, pref_alpha_a, pref_alpha_b

    sel_alpha_a, sel_beta_a, sel_alpha_b, pref_alpha_a, pref_alpha_b = store.transaction(query_projections)

    # Project Alpha + dest_a sees both Project Alpha fact and dest_a-permitted personal fact
    keys_alpha_a = {item["fact_key"] for item in sel_alpha_a["selected"]}
    assert keys_alpha_a == {"project.alpha.camerapipeline", "personal.gear.camera_body"}
    assert len(pref_alpha_a["items"]) == 1

    # Project Beta + dest_a NEVER sees Project Alpha fact, only personal fact
    keys_beta_a = {item["fact_key"] for item in sel_beta_a["selected"]}
    assert keys_beta_a == {"personal.gear.camera_body"}

    # Project Alpha + dest_b NEVER sees dest_a-restricted personal fact or dest_a-restricted manual preference
    keys_alpha_b = {item["fact_key"] for item in sel_alpha_b["selected"]}
    assert keys_alpha_b == {"project.alpha.camerapipeline"}
    assert pref_alpha_b["items"] == []


def test_late_extraction_job_cannot_resurrect_deleted_updated_or_reset_facts(tmp_path):
    store, _ = _make_store(tmp_path)
    dest = {
        "catalog_model_id": "local/codex-cli",
        "provider_peer_id": "self",
        "source_id": "codex-cli",
        "destination_revision": "rev-1",
    }

    # Case 1: Owner updates A -> B while job leased on old message proposing A is in flight
    def start_job_before_owner_correction(data):
        memory.update_memory_settings(data, {"capture_enabled": True, "extraction_destination": dest})
        row = _seed_task(
            data,
            task_id="task-race-1",
            message_id="msg-old",
            text="我的机身是富士 X-T5",
            conversation_revision=1,
            destination=dest,
        )
        src_old = memory.build_source_ref(
            source_type="task_message",
            task_id="task-race-1",
            message_id="msg-old",
            conversation_revision=1,
            context_epoch=0,
            content=row["messages"][0]["content"],
        )
        memory.upsert_active_entry(
            data,
            {
                "scope": {"kind": "personal"},
                "category": "equipment",
                "fact_key": "personal.gear.camera_body",
                "text": "机身是富士 X-T5",
                "sources": [src_old],
            },
            origin="explicit_owner_chat",
        )
        enq = memory.enqueue_extraction_job(data, task_id="task-race-1")
        leased = memory.claim_extraction_job(data, worker_id="slow-worker")
        return enq["job"]["id"], leased["lease_token"]

    job1_id, lease1_token = store.transaction(start_job_before_owner_correction)

    # Owner corrects camera body to X-H2 while slow-worker is running
    def owner_corrects_to_xh2(data):
        row = data["tasks"]["task-race-1"]
        row["messages"].append(
            {
                "role": "user",
                "message_id": "msg-new",
                "content": "把我的机身改成富士 X-H2",
                "context_epoch": 0,
            }
        )
        src_new = memory.build_source_ref(
            source_type="task_message",
            task_id="task-race-1",
            message_id="msg-new",
            conversation_revision=1,
            context_epoch=0,
            content="把我的机身改成富士 X-H2",
        )
        return memory.upsert_active_entry(
            data,
            {
                "scope": {"kind": "personal"},
                "category": "equipment",
                "fact_key": "personal.gear.camera_body",
                "text": "机身是富士 X-H2",
                "sources": [src_new],
            },
            origin="explicit_owner_chat",
        )

    store.transaction(owner_corrects_to_xh2)

    # Slow worker finishes late and tries to commit stale X-T5 proposal with expected_entry_revision=1
    def commit_late_job1(data):
        return memory.commit_extraction_job(
            data,
            job_id=job1_id,
            worker_id="slow-worker",
            lease_token=lease1_token,
            proposals=[
                {
                    "category": "equipment",
                    "fact_key": "personal.gear.camera_body",
                    "text": "机身是富士 X-T5",
                    "expected_entry_revision": 1,
                    "source_message_ids": ["msg-old"],
                }
            ],
        )

    res1 = store.transaction(commit_late_job1)
    assert res1["state"] == "stale"
    assert res1["committed_candidates"] == []
    snap1 = store.inspect(memory.list_memories_snapshot)
    assert len(snap1["entries"]) == 1
    assert snap1["entries"][0]["text"] == "机身是富士 X-H2"
    assert snap1["candidates"] == []

    # Case 2: Owner deletes the fact (tombstone written) while a second job is leased
    def start_job_before_delete(data):
        row = _seed_task(
            data,
            task_id="task-race-2",
            message_id="msg-lens",
            text="我的镜头是 XF16-55mm F2.8",
            conversation_revision=1,
            destination=dest,
        )
        src_lens = memory.build_source_ref(
            source_type="task_message",
            task_id="task-race-2",
            message_id="msg-lens",
            conversation_revision=1,
            context_epoch=0,
            content=row["messages"][0]["content"],
        )
        created = memory.upsert_active_entry(
            data,
            {
                "scope": {"kind": "personal"},
                "category": "equipment",
                "fact_key": "personal.gear.lens",
                "text": "镜头是 XF16-55mm F2.8",
                "sources": [src_lens],
            },
            origin="explicit_owner_chat",
        )
        enq = memory.enqueue_extraction_job(data, task_id="task-race-2")
        leased2 = memory.claim_extraction_job(data, worker_id="slow-worker-2", job_id=enq["job"]["id"])
        memory.delete_memory_entry(data, created["entry"]["id"], expected_entry_revision=1)
        return enq["job"]["id"], leased2["lease_token"]

    job2_id, lease2_token = store.transaction(start_job_before_delete)

    def commit_late_job2(data):
        return memory.commit_extraction_job(
            data,
            job_id=job2_id,
            worker_id="slow-worker-2",
            lease_token=lease2_token,
            proposals=[
                {
                    "category": "equipment",
                    "fact_key": "personal.gear.lens",
                    "text": "镜头是 XF16-55mm F2.8",
                    "source_message_ids": ["msg-lens"],
                }
            ],
        )

    res2 = store.transaction(commit_late_job2)
    assert res2["state"] == "stale"
    assert res2["committed_candidates"] == []

    # Case 3: Owner resets memory (reset_epoch advances) while a third job is leased
    def start_job_before_reset(data):
        _seed_task(
            data,
            task_id="task-race-3",
            message_id="msg-3",
            text="我喜欢拍风光",
            conversation_revision=1,
            destination=dest,
        )
        enq = memory.enqueue_extraction_job(data, task_id="task-race-3")
        leased3 = memory.claim_extraction_job(data, worker_id="slow-worker-3", job_id=enq["job"]["id"])
        memory.reset_memory(data, client_request_id="req-reset-all")
        return enq["job"]["id"], leased3["lease_token"]

    job3_id, lease3_token = store.transaction(start_job_before_reset)

    def commit_late_job3(data):
        return memory.commit_extraction_job(
            data,
            job_id=job3_id,
            worker_id="slow-worker-3",
            lease_token=lease3_token,
            proposals=[
                {
                    "category": "photography",
                    "fact_key": "personal.hobby.landscape",
                    "text": "喜欢拍风光",
                    "source_message_ids": ["msg-3"],
                }
            ],
        )

    res3 = store.transaction(commit_late_job3)
    assert res3["state"] == "stale"
    assert res3["job"]["error_code"] == "reset_epoch_mismatch"
    assert res3["committed_candidates"] == []

    # Pre-reset task cannot be re-enqueued for background extraction unless force_relearn=True
    def try_requeue_old_task(data):
        return memory.enqueue_extraction_job(data, task_id="task-race-3", force_relearn=False)

    requeued = store.transaction(try_requeue_old_task)
    assert requeued["queued"] is False
    assert requeued["reason"] == "no_eligible_owner_sources"


def test_branch_rewrite_and_task_deletion_invalidate_derived_memory_and_sources(tmp_path):
    store, _ = _make_store(tmp_path)
    dest = {
        "catalog_model_id": "local/codex-cli",
        "provider_peer_id": "self",
        "source_id": "codex-cli",
        "destination_revision": "rev-1",
    }

    def setup_branch_memory(data):
        memory.update_memory_settings(data, {"capture_enabled": True, "extraction_destination": dest})
        row = _seed_task(
            data,
            task_id="task-branch-1",
            message_id="msg-b1",
            text="请记住我的备用机是理光 GR3",
            conversation_revision=1,
            destination=dest,
        )
        src = memory.build_source_ref(
            source_type="task_message",
            task_id="task-branch-1",
            message_id="msg-b1",
            conversation_revision=1,
            context_epoch=0,
            content=row["messages"][0]["content"],
        )
        memory.upsert_active_entry(
            data,
            {
                "scope": {"kind": "personal"},
                "category": "equipment",
                "fact_key": "personal.gear.backup_camera",
                "text": "备用机是理光 GR3",
                "sources": [src],
            },
            origin="explicit_owner_chat",
        )
        # Simulate user editing msg-b1 to a different branch
        old_messages = copy.deepcopy(row["messages"])
        row["conversation_revision"] = 2
        row["messages"] = [
            {
                "role": "user",
                "message_id": "msg-b1-edited",
                "content": "帮我查一下今天的天气",
                "context_epoch": 1,
            }
        ]
        return memory.record_branch_rewrite_fence(
            data,
            task_id="task-branch-1",
            previous_conversation_revision=1,
            new_conversation_revision=2,
            retired_messages=old_messages,
        )

    rewrite_result = store.transaction(setup_branch_memory)
    assert rewrite_result["invalidated_entries"] == 1
    snap = store.inspect(memory.list_memories_snapshot)
    assert snap["entries"] == []

    # Also test task deletion invalidating derived entries while keeping manual entries intact
    def setup_and_delete_task(data):
        row = _seed_task(
            data,
            task_id="task-del-1",
            message_id="msg-d1",
            text="请记住我的三脚架是峰设",
            conversation_revision=1,
            destination=dest,
        )
        src = memory.build_source_ref(
            source_type="task_message",
            task_id="task-del-1",
            message_id="msg-d1",
            conversation_revision=1,
            context_epoch=0,
            content=row["messages"][0]["content"],
        )
        memory.upsert_active_entry(
            data,
            {
                "scope": {"kind": "personal"},
                "category": "equipment",
                "fact_key": "personal.gear.tripod",
                "text": "三脚架是峰设",
                "sources": [src],
            },
            origin="explicit_owner_chat",
        )
        memory.upsert_active_entry(
            data,
            {
                "scope": {"kind": "personal"},
                "category": "equipment",
                "fact_key": "personal.gear.flash",
                "text": "闪光灯是神牛 V1",
            },
            origin="manual",
        )
        data["tasks"].pop("task-del-1", None)
        return memory.record_task_lifecycle_fence(data, task_id="task-del-1", operation="delete")

    del_result = store.transaction(setup_and_delete_task)
    assert del_result["invalidated_entries"] == 1
    snap_after_del = store.inspect(memory.list_memories_snapshot)
    assert [e["fact_key"] for e in snap_after_del["entries"]] == ["personal.gear.flash"]


def test_cas_conflicts_idempotency_bounds_and_untrusted_source_rejection(tmp_path):
    store, _ = _make_store(tmp_path)

    # 1. Non-owner role in build_source_ref is rejected
    with pytest.raises(AgentError, match="agent_memory_untrusted_source_role"):
        memory.build_source_ref(
            source_type="task_message",
            task_id="t1",
            message_id="m1",
            conversation_revision=1,
            content="Assistant pretending to be owner",
            author_role="assistant",
        )

    # 2. Secret-like strings are rejected from memory storage
    with pytest.raises(AgentError, match="agent_memory_secret_prohibited"):
        store.transaction(
            lambda data: memory.upsert_active_entry(
                data,
                {"text": "My key is sk-1234567890abcdef12345678", "category": "general"},
            )
        )

    # 3. Idempotent replay with same client_request_id succeeds; different payload raises agent_request_conflict
    res_first = store.transaction(
        lambda data: memory.upsert_active_entry(
            data,
            {"text": "机身是富士 X-T5", "category": "equipment", "fact_key": "personal.gear.camera_body"},
            client_request_id="req-idem-1",
        )
    )
    res_replay = store.transaction(
        lambda data: memory.upsert_active_entry(
            data,
            {"text": "机身是富士 X-T5", "category": "equipment", "fact_key": "personal.gear.camera_body"},
            client_request_id="req-idem-1",
        )
    )
    assert res_first == res_replay

    with pytest.raises(AgentError, match="agent_request_conflict"):
        store.transaction(
            lambda data: memory.upsert_active_entry(
                data,
                {"text": "机身是富士 X-H2", "category": "equipment", "fact_key": "personal.gear.camera_body"},
                client_request_id="req-idem-1",
            )
        )

    # 4. CAS revision and generation conflicts
    entry_id = res_first["entry"]["id"]
    with pytest.raises(AgentError, match="agent_memory_revision_conflict"):
        store.transaction(
            lambda data: memory.upsert_active_entry(
                data,
                {"id": entry_id, "text": "机身是富士 X-H2", "category": "equipment", "fact_key": "personal.gear.camera_body"},
                expected_entry_revision=99,
            )
        )

    with pytest.raises(AgentError, match="agent_memory_generation_conflict"):
        store.transaction(
            lambda data: memory.delete_memory_entry(
                data,
                entry_id,
                expected_generation=999,
            )
        )

    # 5. Entry count bound enforcement & small context window yield
    def seed_to_limit(data):
        memory.update_memory_settings(data, {"budgets": {"max_entries": 8}})
        for idx in range(7):
            memory.upsert_active_entry(
                data,
                {
                    "text": f"摄影镜头事实条目 {idx}",
                    "category": "equipment",
                    "fact_key": f"personal.gear.item_{idx}",
                },
            )

    store.transaction(seed_to_limit)
    before_overflow = store.inspect(memory.list_memories_snapshot)
    assert len(before_overflow["entries"]) == 8

    with pytest.raises(AgentError, match="agent_memory_entry_limit"):
        store.transaction(
            lambda data: memory.upsert_active_entry(
                data,
                {
                    "text": "超出上限的第9条事实",
                    "category": "equipment",
                    "fact_key": "personal.gear.item_overflow",
                },
            )
        )

    after_overflow = store.inspect(memory.list_memories_snapshot)
    assert len(after_overflow["entries"]) == 8
    assert after_overflow["generation"] == before_overflow["generation"]

    def test_small_window(data):
        row = _seed_task(data, task_id="t-tight", message_id="m-tight", text="推荐摄影镜头和机身配置")
        tight = memory.select_memories_for_task(data, row, remaining_context_bytes=1500)
        normal = memory.select_memories_for_task(data, row, remaining_context_bytes=48000)
        return tight, normal

    tight, normal = store.transaction(test_small_window)
    assert tight["selected"] == []
    assert len(normal["selected"]) > 0
    assert normal["used_bytes"] <= normal["max_bytes"]


def test_read_apis_do_not_mutate_cached_store_root(tmp_path):
    store, _ = _make_store(tmp_path)

    def check_pure_reads(data):
        before_keys = set(data.keys())
        snap = memory.list_memories_snapshot(data)
        settings = memory.get_memory_settings_snapshot(data)
        searched = memory.search_memories(data, query="摄影")
        prefs = memory.filter_manual_preferences_for_task(
            data,
            {"id": "t-read", "messages": []},
            {"revision": 1, "items": [{"id": "p1", "text": "中文"}]},
        )
        selected = memory.select_memories_for_task(
            data,
            {"id": "t-read", "messages": [{"role": "user", "content": "摄影"}]},
            remaining_context_bytes=32000,
        )
        after_keys = set(data.keys())
        assert "memory" not in data
        assert before_keys == after_keys
        return snap, settings, searched, prefs, selected

    snap, settings, searched, prefs, selected = store.inspect(check_pure_reads)
    assert snap["entries"] == []
    assert settings["settings"]["capture_enabled"] is False
    assert searched["entries"] == []
    assert len(prefs["items"]) == 1
    assert selected["selected"] == []
    assert "memory" not in store.read()


def test_tombstone_limit_preserves_protection_and_watermark_compaction_blocks_old_sources_after_restart(tmp_path):
    store, key = _make_store(tmp_path)
    dest = {
        "catalog_model_id": "local/codex-cli",
        "provider_peer_id": "self",
        "source_id": "codex-cli",
        "destination_revision": "rev-1",
    }

    def setup_tombstone_and_watermark_bounds(data):
        memory.update_memory_settings(
            data,
            {
                "capture_enabled": True,
                "extraction_destination": dest,
                "budgets": {
                    "max_tombstones": 4,
                    "max_fenced_sources": 8,
                },
            },
        )
        row = _seed_task(
            data,
            task_id="task-bound-1",
            message_id="msg-1",
            text="我的旧镜头是 XF18-55mm",
            conversation_revision=1,
            destination=dest,
        )
        src_bound = memory.build_source_ref(
            source_type="task_message",
            task_id="task-bound-1",
            message_id="msg-1",
            conversation_revision=1,
            context_epoch=0,
            content=row["messages"][0]["content"],
        )
        enq = memory.enqueue_extraction_job(data, task_id="task-bound-1")
        leased = memory.claim_extraction_job(data, worker_id="w-bound", job_id=enq["job"]["id"])

        # Create and delete 4 facts to fill max_tombstones=4
        created_ids = []
        for idx in range(4):
            fkey = "personal.gear.old_lens" if idx == 0 else f"personal.gear.temp_{idx}"
            res = memory.upsert_active_entry(
                data,
                {
                    "scope": {"kind": "personal"},
                    "category": "equipment",
                    "fact_key": fkey,
                    "text": f"设备条目 {idx}",
                    "sources": [src_bound] if idx == 0 else [],
                },
                origin="explicit_owner_chat" if idx == 0 else "manual",
            )
            created_ids.append(res["entry"]["id"])
        for eid in created_ids:
            memory.delete_memory_entry(data, eid)

        # Create a 5th entry; deleting it folds the oldest half (2 tombstones) into suppressed_slots instead of failing
        fifth = memory.upsert_active_entry(
            data,
            {
                "scope": {"kind": "personal"},
                "category": "equipment",
                "fact_key": "personal.gear.fifth",
                "text": "第五个设备",
            },
            origin="manual",
        )
        memory.delete_memory_entry(data, fifth["entry"]["id"])

        # Overflow fenced_messages and retired_branches (>8) to trigger watermark compaction
        for rev in range(1, 12):
            memory.record_branch_rewrite_fence(
                data,
                task_id="task-bound-1",
                previous_conversation_revision=rev,
                new_conversation_revision=rev + 1,
                retired_messages=[
                    {
                        "role": "user",
                        "message_id": f"msg-{rev}",
                        "content": f"旧分支消息 {rev}",
                    }
                ],
            )
        row["conversation_revision"] = 12
        row["messages"] = [
            {
                "role": "user",
                "message_id": "msg-12",
                "content": "当前分支最新消息",
                "context_epoch": 0,
            }
        ]
        return enq["job"]["id"], leased["lease_token"]

    job_id, lease_token = store.transaction(setup_tombstone_and_watermark_bounds)

    # Reload AgentStore from disk to verify persistence of tombstones and watermarks across restart
    reloaded = AgentStore(tmp_path, key)

    def verify_after_restart(data):
        mem = memory.read_memory_state(data)
        assert len(mem["tombstones"]) == 3
        assert len(mem["suppressed_slots"]) == 2
        assert len(mem["source_fences"]["fenced_messages"]) <= 8
        assert len(mem["source_fences"]["retired_branches"]) <= 8
        assert mem["source_fences"]["fenced_task_watermarks"].get("task-bound-1", 0) >= 3
        assert mem["source_fences"].get("fenced_relearn_watermarks", {}).get("task-bound-1", 0) >= 1

        # Late extraction job must still be rejected after restart and watermark compaction
        late_res = memory.commit_extraction_job(
            data,
            job_id=job_id,
            worker_id="w-bound",
            lease_token=lease_token,
            proposals=[
                {
                    "category": "equipment",
                    "fact_key": "personal.gear.old_lens",
                    "text": "我的旧镜头是 XF18-55mm",
                    "source_message_ids": ["msg-1"],
                }
            ],
        )
        assert late_res["state"] == "stale"
        assert late_res["committed_candidates"] == []

        # Old compacted revision (rev 1) must still fail source verification via watermark
        old_ref = memory.build_source_ref(
            source_type="task_message",
            task_id="task-bound-1",
            message_id="msg-1",
            conversation_revision=1,
            context_epoch=0,
            content="我的旧镜头是 XF18-55mm",
        )
        valid, reason = memory.verify_source_ref_against_state(data, old_ref, for_background_job=False)
        assert valid is False
        assert reason in {
            "source_task_watermark_fenced",
            "source_message_fenced",
            "source_branch_retired",
            "source_slot_relearn_watermark_fenced",
        }
        return late_res

    reloaded.transaction(verify_after_restart)


def test_attempt_lease_token_prevents_expired_attempt_pollution_and_schema_corruption_fails_closed(tmp_path):
    store, _ = _make_store(tmp_path)
    dest = {
        "catalog_model_id": "local/codex-cli",
        "provider_peer_id": "self",
        "source_id": "codex-cli",
        "destination_revision": "rev-1",
    }

    def claim_twice_same_worker(data):
        memory.update_memory_settings(data, {"capture_enabled": True, "extraction_destination": dest})
        _seed_task(
            data,
            task_id="task-lease-1",
            message_id="task-lease-1:owner:0",
            text="我的机身是富士 X-T5",
            conversation_revision=1,
            destination=dest,
        )
        enq = memory.enqueue_extraction_job(data, task_id="task-lease-1")
        job_id = enq["job"]["id"]
        attempt1 = memory.claim_extraction_job(data, worker_id="same-worker", job_id=job_id, lease_seconds=30)
        # Simulate lease expiry so same worker re-claims a 2nd attempt
        data["memory"]["jobs"][job_id]["lease_until"] = 1.0
        attempt2 = memory.claim_extraction_job(data, worker_id="same-worker", job_id=job_id, lease_seconds=30)
        assert attempt1["lease_token"] != attempt2["lease_token"]
        return job_id, attempt1["lease_token"], attempt2["lease_token"]

    job_id, token1, token2 = store.transaction(claim_twice_same_worker)

    # Late attempt 1 with old lease_token must be rejected even though worker_id is identical
    with pytest.raises(AgentError, match="agent_memory_job_lease_lost"):
        store.transaction(
            lambda data: memory.commit_extraction_job(
                data,
                job_id=job_id,
                worker_id="same-worker",
                lease_token=token1,
                proposals=[
                    {
                        "category": "equipment",
                        "fact_key": "personal.gear.camera_body",
                        "text": "机身是富士 X-T5",
                        "source_message_ids": ["task-lease-1:owner:0"],
                    }
                ],
            )
        )

    # Attempt 2 with current lease_token succeeds cleanly
    committed2 = store.transaction(
        lambda data: memory.commit_extraction_job(
            data,
            job_id=job_id,
            worker_id="same-worker",
            lease_token=token2,
            proposals=[
                {
                    "category": "equipment",
                    "fact_key": "personal.gear.camera_body",
                    "text": "机身是富士 X-T5",
                    "source_message_ids": ["task-lease-1:owner:0"],
                }
            ],
        )
    )
    assert committed2["state"] == "committed"
    assert len(committed2["committed_candidates"]) == 1

    # Unsupported schema_version or corrupted security containers must fail closed
    with pytest.raises(AgentError, match="agent_memory_unsupported_schema"):
        memory.read_memory_state({"memory": {"schema_version": 999}})

    with pytest.raises(AgentError, match="agent_memory_corrupted_state"):
        memory.ensure_memory_state({"memory": {"schema_version": 1, "tombstones": "corrupted-not-a-dict"}})


def test_s09_i25_300_consecutive_deletions_fold_tombstones_preserve_suppression_restore_and_reset(tmp_path):
    store, key = _make_store(tmp_path)
    dest = {
        "catalog_model_id": "local/codex-cli",
        "provider_peer_id": "self",
        "source_id": "codex-cli",
        "destination_revision": "rev-1",
    }

    def run_300_deletions_and_checks(data):
        # Legacy state without suppressed_slots must load cleanly
        legacy_mem = memory.default_memory_state()
        legacy_mem.pop("suppressed_slots", None)
        data["memory"] = legacy_mem
        loaded_legacy = memory.read_memory_state(data)
        assert loaded_legacy.get("suppressed_slots") == []
        ensured_legacy = memory.ensure_memory_state(data)
        assert ensured_legacy.get("suppressed_slots") == []

        memory.update_memory_settings(
            data,
            {"capture_enabled": True, "use_enabled": True, "extraction_destination": dest},
        )
        row = _seed_task(
            data,
            task_id="task-300",
            message_id="msg-300-base",
            text="整理器材清单",
            conversation_revision=1,
            destination=dest,
        )

        # Consecutively create and delete 300 entries (exceeding MAX_TOMBSTONES=256)
        for idx in range(300):
            mid = f"msg-300-{idx:03d}"
            mtext = f"请记住测试器材槽位编号 {idx:03d}"
            row["messages"].append({"role": "user", "message_id": mid, "content": mtext, "context_epoch": 0})
            msg_src = memory.build_source_ref(
                source_type="task_message",
                task_id=row["id"],
                message_id=mid,
                conversation_revision=1,
                context_epoch=0,
                content=mtext,
            )
            upserted = memory.upsert_active_entry(
                data,
                {
                    "scope": {"kind": "personal"},
                    "category": "equipment",
                    "fact_key": f"personal.gear.slot_{idx:03d}",
                    "text": f"测试器材槽位编号 {idx:03d}",
                    "sources": [msg_src],
                },
                origin="explicit_owner_chat",
            )
            if idx % 25 == 24:
                memory.forget_by_slot_or_query(
                    data,
                    scope={"kind": "personal"},
                    fact_keys=[f"personal.gear.slot_{idx:03d}"],
                )
            else:
                memory.delete_memory_entry(data, upserted["entry"]["id"])

        mem = data["memory"]
        assert len(mem["tombstones"]) <= memory.MAX_TOMBSTONES
        assert len(mem.get("suppressed_slots") or []) > 0
        # Verify folded items only keep compact scope + 16-hex prefixes + deleted_at (no source_fences)
        sample_folded = mem["suppressed_slots"][0]
        assert set(sample_folded.keys()) <= {"scope", "fact_key_hmac", "aliases_hmac", "deleted_at"}
        assert "source_fences" not in sample_folded
        assert set(sample_folded["scope"].keys()) == {"kind", "project_id"}
        assert len(sample_folded["fact_key_hmac"]) == 16
        assert all(len(a) == 16 for a in sample_folded["aliases_hmac"])

        # Settings snapshot includes tombstone_count and suppressed_slot_count
        settings_snap = memory.get_memory_settings_snapshot(data)
        assert settings_snap["tombstone_count"] == len(mem["tombstones"])
        assert settings_snap["suppressed_slot_count"] == len(mem["suppressed_slots"])

        # Oldest slot (idx=0) must have been folded out of tombstones into suppressed_slots
        tomb_fks = {t["fact_key_hmac"] for t in mem["tombstones"].values()}
        slot0_mac = memory._slot_hmac(mem, {"kind": "personal", "owner_id": "local_owner", "project_id": None, "project_generation": None}, "personal.gear.slot_000", mutate=False)
        assert slot0_mac not in tomb_fks
        assert memory.is_slot_tombstoned(
            mem,
            {"kind": "personal", "owner_id": "local_owner", "project_id": None, "project_generation": None},
            "personal.gear.slot_000",
            "测试器材槽位编号 000",
        ) is True

        # Background extraction on a brand-new task proposing the folded slot_000 must still be blocked
        _seed_task(
            data,
            task_id="task-relearn-attempt",
            message_id="msg-relearn-1",
            text="我又提到了测试器材槽位编号 000",
            conversation_revision=1,
            destination=dest,
        )
        enq = memory.enqueue_extraction_job(data, task_id="task-relearn-attempt")
        leased = memory.claim_extraction_job(data, worker_id="w-300", job_id=enq["job"]["id"])
        committed = memory.commit_extraction_job(
            data,
            job_id=leased["id"],
            worker_id="w-300",
            lease_token=leased["lease_token"],
            proposals=[
                {
                    "category": "equipment",
                    "fact_key": "personal.gear.slot_000",
                    "text": "测试器材槽位编号 000",
                    "source_message_ids": ["msg-relearn-1"],
                }
            ],
        )
        assert committed["committed_candidates"] == []
        assert "tombstone_suppressed" in committed["skipped_reasons"]
        assert committed["job"]["error_code"] == "tombstone_suppressed"

        # Explicit owner restore clears the folded item from suppressed_slots
        suppressed_before_restore = len(mem["suppressed_slots"])
        restored = memory.upsert_active_entry(
            data,
            {
                "scope": {"kind": "personal"},
                "category": "equipment",
                "fact_key": "personal.gear.slot_000",
                "text": "测试器材槽位编号 000",
            },
            origin="manual",
        )
        assert restored["entry"]["fact_key"] == "personal.gear.slot_000"
        assert len(mem["suppressed_slots"]) < suppressed_before_restore
        assert memory.is_slot_tombstoned(
            mem,
            {"kind": "personal", "owner_id": "local_owner", "project_id": None, "project_generation": None},
            "personal.gear.slot_000",
            "测试器材槽位编号 000",
        ) is False

        # Total memory bytes within MAX_MEMORY_BYTES
        from rynmesh.agent_runtime.store import encoded

        assert len(encoded(mem)) <= memory.MAX_MEMORY_BYTES

        # Reset clears both tombstones and suppressed_slots
        memory.reset_memory(data)
        assert data["memory"]["tombstones"] == {}
        assert data["memory"]["suppressed_slots"] == []

    store.transaction(run_300_deletions_and_checks)
    reloaded = AgentStore(tmp_path, key)
    snap = reloaded.inspect(memory.get_memory_settings_snapshot)
    assert snap["tombstone_count"] == 0
    assert snap["suppressed_slot_count"] == 0


def test_s09_i25_suppressed_slots_cap_at_4096_evicts_oldest_by_deleted_at(tmp_path):
    store, _ = _make_store(tmp_path)

    def run_cap_check(data):
        memory.update_memory_settings(data, {"budgets": {"max_tombstones": 4}})
        mem = memory.ensure_memory_state(data)
        mem["settings"]["budgets"]["max_memory_bytes"] = 1024 * 1024
        mem["suppressed_slots"] = [
            {
                "scope": {"kind": "personal", "project_id": None},
                "fact_key_hmac": f"{idx:016x}",
                "aliases_hmac": [f"{idx:016x}"],
                "deleted_at": float(1000 + idx),
            }
            for idx in range(4095)
        ]
        for idx in range(5):
            e = memory.upsert_active_entry(
                data,
                {
                    "scope": {"kind": "personal"},
                    "category": "general",
                    "fact_key": f"personal.general.cap_{idx}",
                    "text": f"容量测试 {idx}",
                },
                origin="manual",
            )
            memory.delete_memory_entry(data, e["entry"]["id"])

        assert len(mem["suppressed_slots"]) == 4096
        remaining_prefixes = {item["fact_key_hmac"] for item in mem["suppressed_slots"]}
        # Oldest deleted_at (idx=0 -> 1000.0) must be evicted when 2 folded items push 4095 -> 4097 -> 4096
        assert f"{0:016x}" not in remaining_prefixes
        assert f"{1:016x}" in remaining_prefixes

    store.transaction(run_cap_check)


def test_s09_i25_lifecycle_hooks_never_block_task_delete_archive_or_branch_rewrite_when_ledger_full(tmp_path):
    from rynmesh.agent_runtime import conversation, lifecycle, memory_integration, state

    store, _ = _make_store(tmp_path)
    dest = {
        "catalog_model_id": "local/codex-cli",
        "provider_peer_id": "self",
        "source_id": "codex-cli",
        "destination_revision": "rev-1",
    }

    def emit(row, kind, **extra):
        seq = int(row.get("event_cursor", 0)) + 1
        row["event_cursor"] = seq
        row.setdefault("events", []).append({"seq": seq, "kind": kind, **extra})

    def seed_stuffed_state(data):
        memory.update_memory_settings(
            data,
            {
                "capture_enabled": True,
                "use_enabled": True,
                "activation_policy": "owner_review",
                "extraction_destination": dest,
                "budgets": {
                    "max_tombstones": 4,
                    "max_fenced_sources": 8,
                    "max_memory_bytes": 16384,
                },
            },
        )
        # Seed 3 tasks: one to delete, one to archive, one to branch-rewrite (edit message)
        del_row = _seed_task(
            data,
            task_id="task-full-del",
            message_id="msg-del-1",
            text="请记住我的镜头滤镜是 B+W UV",
            conversation_revision=1,
            destination=dest,
        )
        state.initialize(del_row)
        del_row["control_revision"] = 1
        del_row["in_flight"] = False

        arch_row = copy.deepcopy(del_row)
        arch_row["id"] = "task-full-arch"
        arch_row["messages"][0]["message_id"] = "msg-arch-1"
        arch_row["messages"][0]["content"] = "归档对话消息"
        data["tasks"]["task-full-arch"] = arch_row

        edit_row = copy.deepcopy(del_row)
        edit_row["id"] = "task-full-edit"
        edit_row["messages"][0]["message_id"] = "msg-edit-old"
        edit_row["messages"][0]["content"] = "请记住我的闪光灯品牌是保荣摄影灯"
        edit_row["goal"]["text"] = edit_row["messages"][0]["content"]
        edit_row["goal"]["original_message_id"] = "msg-edit-old"
        data["tasks"]["task-full-edit"] = edit_row

        del_src = memory.build_source_ref(
            source_type="task_message",
            task_id="task-full-del",
            message_id="msg-del-1",
            conversation_revision=1,
            context_epoch=0,
            content=del_row["messages"][0]["content"],
        )
        edit_src = memory.build_source_ref(
            source_type="task_message",
            task_id="task-full-edit",
            message_id="msg-edit-old",
            conversation_revision=1,
            context_epoch=0,
            content=edit_row["messages"][0]["content"],
        )
        memory.upsert_active_entry(
            data,
            {
                "scope": {"kind": "personal"},
                "category": "photography",
                "fact_key": "personal.photo.filter",
                "text": "镜头滤镜是 B+W UV",
                "sources": [del_src],
                "exposure_policy": {"mode": "all_selected", "allowed_destinations": []},
            },
            origin="explicit_owner_chat",
        )
        memory.upsert_active_entry(
            data,
            {
                "scope": {"kind": "personal"},
                "category": "photography",
                "fact_key": "personal.photo.strobe",
                "text": "摄影闪光灯品牌是保荣摄影灯",
                "sources": [edit_src],
                "exposure_policy": {"mode": "all_selected", "allowed_destinations": []},
            },
            origin="explicit_owner_chat",
        )
        # Add a pending candidate derived from msg-edit-old
        mem = data["memory"]
        mem["candidates"]["cand-edit-old"] = {
            "id": "cand-edit-old",
            "scope": {"kind": "personal", "owner_id": "local_owner", "project_id": None, "project_generation": None},
            "category": "photography",
            "fact_key": "personal.photo.strobe_cand",
            "text": "候选闪光灯保荣",
            "kind": "owner_fact",
            "status": "pending",
            "revision": 1,
            "origin": "background_extraction",
            "job_id": None,
            "reset_epoch": 0,
            "sources": [edit_src],
            "target_entry_id": None,
            "expected_entry_revision": None,
            "exposure_policy": {"mode": "all_selected", "allowed_destinations": [], "revision": 1},
            "created_at": 1.0,
            "updated_at": 1.0,
        }

        # Stuff tombstones, watermarks (> max_fenced*2 = 16), and bytes (> 16384) so even after compaction bounds fail
        for idx in range(6):
            mem["tombstones"][f"tomb-full-{idx}"] = {
                "entry_id": f"tomb-full-{idx}",
                "scope": {"kind": "personal", "owner_id": "local_owner", "project_id": None, "project_generation": None},
                "fact_key_hmac": f"{idx + 100:064x}",
                "aliases_hmac": [f"{idx + 100:064x}"],
                "deleted_revision": 1,
                "deletion_generation": 1,
                "deleted_at_source_seq": 0,
                "reset_epoch": 0,
                "source_fences": [],
                "suppression_mode": "block_background_relearn_until_explicit_owner_restore",
                "owner_action_id": f"act-{idx}",
                "deleted_at": float(10 + idx),
                "reason": "owner_deleted",
            }
        for idx in range(25):
            mem["source_fences"]["fenced_task_watermarks"][f"old-fenced-task-{idx}"] = 99
        for idx in range(20):
            mem["entries"][f"stuffed-active-{idx}"] = {
                "id": f"stuffed-active-{idx}",
                "scope": {"kind": "personal", "owner_id": "local_owner", "project_id": None, "project_generation": None},
                "category": "general",
                "fact_key": f"personal.stuffed.k_{idx}",
                "text": ("填充记忆字节内容_" * 60) + str(idx),
                "kind": "owner_fact",
                "status": "active",
                "revision": 1,
                "origin": "manual",
                "sources": [],
                "supersedes": [],
                "superseded_by": None,
                "exposure_policy": {"mode": "local_only", "allowed_destinations": [], "revision": 1},
                "created_at": 1.0,
                "updated_at": 1.0,
                "last_used_at": None,
                "reviewed_by_owner_at": 1.0,
            }
        return del_src

    del_src = store.transaction(seed_stuffed_state)

    # 1. Task delete succeeds despite stuffed ledger; degraded marker recorded; deleted task source fails verification
    del_res = lifecycle.change(
        store,
        "task-full-del",
        {"expected_control_revision": 1, "client_request_id": "00000000-0000-4000-8000-000000000001"},
        "delete",
        emit,
        lambda: "2026-11-15T12:00:00Z",
        on_mutate=memory_integration.on_task_lifecycle_in_tx,
    )
    assert del_res["deleted"] is True

    after_del = store.read()
    assert "task-full-del" not in after_del["tasks"]
    assert after_del.get("memory_ledger_degraded", {}).get("task_id") == "task-full-del"
    assert after_del["memory_ledger_degraded"]["operation"] == "delete"
    assert after_del["memory_ledger_degraded"]["code"]
    valid_del, reason_del = memory.verify_source_ref_against_state(after_del, del_src, for_background_job=False)
    assert valid_del is False
    assert reason_del == "source_task_missing"

    # 2. Task archive succeeds despite stuffed ledger
    arch_res = lifecycle.change(
        store,
        "task-full-arch",
        {"expected_control_revision": 1, "client_request_id": "00000000-0000-4000-8000-000000000002"},
        "archive",
        emit,
        lambda: "2026-11-15T12:00:01Z",
        on_mutate=memory_integration.on_task_lifecycle_in_tx,
    )
    assert arch_res["archived"] is True
    after_arch = store.read()
    assert after_arch["memory_ledger_degraded"]["task_id"] == "task-full-arch"
    assert after_arch["memory_ledger_degraded"]["operation"] == "archive"

    # 3. Message edit / branch rewrite succeeds via minimal fallback and stops retired-message entries from selection
    def perform_branch_rewrite(data):
        row = data["tasks"]["task-full-edit"]
        replacement = {
            "role": "user",
            "message_id": "msg-edit-new",
            "content": "推荐摄影闪光灯配置",
            "ui": {"kind": "message", "text": "推荐摄影闪光灯配置"},
        }
        conversation.rewrite(
            row,
            {"kind": "edit_message", "message_index": 0, "message": replacement},
            emit,
            data=data,
        )
        selected = memory.select_memories_for_task(
            data,
            row,
            query_text="摄影 闪光灯 保荣摄影灯",
            remaining_context_bytes=48000,
        )
        return selected

    selected_after_edit = store.transaction(perform_branch_rewrite)
    after_edit = store.read()
    assert after_edit["memory_ledger_degraded"]["task_id"] == "task-full-edit"
    assert after_edit["memory_ledger_degraded"]["operation"] == "branch_rewrite"
    assert after_edit["memory"]["candidates"]["cand-edit-old"]["status"] == "stale"
    selected_keys = [item["fact_key"] for item in selected_after_edit["selected"]]
    assert "personal.photo.strobe" not in selected_keys
    settings_snap = memory.get_memory_settings_snapshot(after_edit)
    assert settings_snap["memory_ledger_degraded"]["operation"] == "branch_rewrite"



