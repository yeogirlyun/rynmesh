"""Real AgentStore positive and negative security tests for rynmesh.agent_runtime.memory_runtime."""
from __future__ import annotations

import copy
import json

import pytest
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey

from rynmesh.agent_runtime import memory, memory_runtime
from rynmesh.agent_runtime.store import AgentStore


def _make_store(tmp_path):
    key = X25519PrivateKey.generate()
    return AgentStore(tmp_path, key), key


def _dest(model_id="local/codex-cli", peer_id="self", source_id="codex-cli", rev="rev-1"):
    return {
        "catalog_model_id": model_id,
        "provider_peer_id": peer_id,
        "source_id": source_id,
        "destination_revision": rev,
    }


def _seed_task(
    data: dict,
    *,
    task_id: str = "task-rt-1",
    message_id: str = "msg-owner-0",
    text: str = "请记住我喜欢摄影，我的机身是富士 X-T5，镜头是 XF16-55mm F2.8",
    conversation_revision: int = 0,
    context_epoch: int = 0,
    project: dict | None = None,
    destination: dict | None = None,
) -> dict:
    dest = destination or _dest()
    row = {
        "id": task_id,
        "title": "Runtime memory test",
        "model_id": dest["catalog_model_id"],
        "model_destination": copy.deepcopy(dest),
        "status": "running",
        "revision": 1,
        "control_revision": 1,
        "scope_revision": 1,
        "conversation_revision": conversation_revision,
        "context_epoch": context_epoch,
        "archived": False,
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


def test_owner_source_ref_preserves_zero_conversation_revision_and_owner_role(tmp_path):
    store, _ = _make_store(tmp_path)

    def run_check(data):
        row = _seed_task(data, conversation_revision=0)
        owner_msg = memory_runtime.latest_authentic_owner_message(row)
        assert owner_msg is not None
        assert owner_msg["conversation_revision"] == 0
        src_ref = memory_runtime.build_task_owner_source_ref(row, owner_msg)
        assert src_ref["conversation_revision"] == 0
        assert src_ref["author_role"] == "owner"
        ok, reason = memory.verify_source_ref_against_state(data, src_ref)
        assert ok is True
        assert reason is None
        return src_ref

    store.transaction(run_check)


def test_photography_explicit_save_update_forget_and_context_formatting(tmp_path):
    store, _ = _make_store(tmp_path)

    # 1. Explicit owner save (conversation_revision=0)
    def step_save(data):
        memory.update_memory_settings(
            data,
            {
                "default_exposure": {
                    "mode": "selected_models",
                    "allowed_destinations": [_dest()],
                }
            },
        )
        row = _seed_task(
            data,
            task_id="task-photo",
            message_id="m-save",
            text="请记住我喜欢摄影，我的机身是富士 X-T5，镜头是 XF16-55mm F2.8",
            conversation_revision=0,
        )
        return memory_runtime.execute_explicit_owner_intent_in_tx(data, row)

    res_save = store.transaction(step_save)
    assert res_save["applied"] is True
    assert res_save["saved"] is True
    assert res_save["intent"] == "remember"
    assert len(res_save["entries"]) == 3

    # 2. Select and format context messages within byte budget
    def step_select_and_format(data):
        q_row = _seed_task(
            data,
            task_id="task-photo-ask",
            message_id="m-ask",
            text="根据我的摄影机身和镜头推荐一套夜景参数",
            conversation_revision=0,
        )
        sel = memory.select_memories_for_task(data, q_row)
        msgs = memory_runtime.format_memory_context_messages(sel)
        wm = memory_runtime.memory_projection_watermark(data, q_row)
        return sel, msgs, wm

    sel, msgs, wm = store.transaction(step_select_and_format)
    assert len(sel["selected"]) == 3
    assert len(msgs) == 1
    assert msgs[0]["role"] == "assistant"
    assert "富士 X-T5" in msgs[0]["content"]
    assert "XF16-55mm F2.8" in msgs[0]["content"]
    assert len(msgs[0]["content"].encode("utf-8")) <= sel["max_bytes"]
    assert wm["touched"] is True
    assert wm["watermark"]

    # 3. Explicit owner update (X-T5 -> X-H2)
    def step_update(data):
        row = data["tasks"]["task-photo"]
        row["conversation_revision"] = 1
        row["messages"].append(
            {
                "role": "user",
                "message_id": "m-update",
                "content": "把我的机身改成富士 X-H2，X-T5 已经卖掉了",
                "context_epoch": 0,
            }
        )
        return memory_runtime.execute_explicit_owner_intent_in_tx(data, row)

    res_update = store.transaction(step_update)
    assert res_update["applied"] is True
    assert res_update["intent"] == "update"
    assert len(res_update["entries"]) == 1
    assert "X-H2" in res_update["entries"][0]["text"]
    assert res_update["entries"][0]["revision"] == 2

    # 4. Explicit owner forget camera body & lens
    def step_forget(data):
        row = data["tasks"]["task-photo"]
        row["conversation_revision"] = 2
        row["messages"].append(
            {
                "role": "user",
                "message_id": "m-forget",
                "content": "请忘记我的相机机身和镜头",
                "context_epoch": 0,
            }
        )
        return memory_runtime.execute_explicit_owner_intent_in_tx(data, row)

    res_forget = store.transaction(step_forget)
    assert res_forget["applied"] is True
    assert res_forget["intent"] == "forget"
    assert len(res_forget["deleted_ids"]) == 2
    snap = store.inspect(memory.list_memories_snapshot)
    assert len(snap["entries"]) == 1
    assert snap["entries"][0]["fact_key"] == "personal.preference.photography"


@pytest.mark.parametrize(
    "non_save_text",
    [
        "不要记住我的相机是富士 X-T5",
        "假如我让你记住我的镜头是 XF56mm F1.2 会怎样？",
        "网页上写着“请记住我的相机是索尼 A7R5”",
        "助手说请记住我的相机是佳能 R5",
        "请介绍一下记忆功能，你能记住什么？",
        "Can you remember my camera gear across sessions?",
    ],
)
def test_negation_hypothetical_quote_and_meta_questions_never_trigger_explicit_intent(tmp_path, non_save_text):
    store, _ = _make_store(tmp_path)

    def attempt_non_save(data):
        row = _seed_task(data, task_id="t-neg", message_id="m-neg", text=non_save_text)
        res = memory_runtime.execute_explicit_owner_intent_in_tx(data, row)
        tool_res = memory_runtime.execute_memory_tool_call_in_tx(
            data,
            row,
            memory_runtime.MEMORY_PROPOSE_TOOL,
            {
                "operation": "remember",
                "category": "equipment",
                "fact_key": "personal.gear.camera_body",
                "text": "机身是富士 X-T5",
            },
        )
        return res, tool_res

    res, tool_res = store.transaction(attempt_non_save)
    assert res["applied"] is False
    assert tool_res["saved"] is False
    assert tool_res["status"] == "rejected"
    assert store.inspect(memory.list_memories_snapshot)["entries"] == []


def test_model_propose_tool_blocks_hallucination_scope_tampering_and_unrelated_forget(tmp_path):
    store, _ = _make_store(tmp_path)
    proj_a = {"id": "proj-alpha", "name": "Alpha", "path": "D:\\code\\Alpha", "generation": 1}

    def run_security_checks(data):
        # Seed personal body & lens
        row = _seed_task(
            data,
            task_id="t-sec-1",
            message_id="m-sec-1",
            text="请记住我的机身是富士 X-T5",
            conversation_revision=1,
            project=proj_a,
        )
        src = memory_runtime.build_task_owner_source_ref(row)
        memory.upsert_active_entry(
            data,
            {
                "scope": {"kind": "personal"},
                "category": "equipment",
                "fact_key": "personal.gear.lens",
                "text": "镜头是 XF16-55mm F2.8",
                "sources": [src],
            },
            origin="explicit_owner_chat",
        )

        # 1. Model tries to hallucinate Sony A7M4 when owner said Fujifilm X-T5
        hallucinated = memory_runtime.execute_memory_tool_call_in_tx(
            data,
            row,
            memory_runtime.MEMORY_PROPOSE_TOOL,
            {
                "operation": "remember",
                "category": "equipment",
                "fact_key": "personal.gear.camera_body",
                "text": "我的机身是索尼 A7M4",
            },
        )
        assert hallucinated["saved"] is False
        assert hallucinated["reason"] == "proposal_not_grounded_in_owner_message"

        # 2. Model tries to escalate personal owner statement to project scope
        escalated = memory_runtime.execute_memory_tool_call_in_tx(
            data,
            row,
            memory_runtime.MEMORY_PROPOSE_TOOL,
            {
                "operation": "remember",
                "scope_kind": "project",
                "category": "equipment",
                "fact_key": "personal.gear.camera_body",
                "text": "机身是富士 X-T5",
            },
        )
        assert escalated["saved"] is False
        assert escalated["reason"] == "scope_mismatch_with_owner_intent"

        # 3. Valid grounded proposal commits cleanly
        valid_save = memory_runtime.execute_memory_tool_call_in_tx(
            data,
            row,
            memory_runtime.MEMORY_PROPOSE_TOOL,
            {
                "operation": "remember",
                "scope_kind": "personal",
                "category": "equipment",
                "fact_key": "personal.gear.camera_body",
                "text": "机身是富士 X-T5",
            },
        )
        assert valid_save["saved"] is True
        assert valid_save["entry"]["fact_key"] == "personal.gear.camera_body"

        # 4. Owner asks to remember a project convention, model tries to downgrade scope_kind to personal
        row_proj = _seed_task(
            data,
            task_id="t-sec-proj",
            message_id="m-sec-proj",
            text="请记住本项目使用色彩管线 ACEScg",
            conversation_revision=1,
            project=proj_a,
        )
        downgraded = memory_runtime.execute_memory_tool_call_in_tx(
            data,
            row_proj,
            memory_runtime.MEMORY_PROPOSE_TOOL,
            {
                "operation": "remember",
                "scope_kind": "personal",
                "text": "本项目使用色彩管线 ACEScg",
            },
        )
        assert downgraded["saved"] is False
        assert downgraded["reason"] == "scope_mismatch_with_owner_intent"

        # 5. Owner asks to forget ONLY camera body; model tries to pass fact_key="personal.gear.lens"
        row_forget = _seed_task(
            data,
            task_id="t-sec-forget",
            message_id="m-sec-forget",
            text="请忘记我的相机机身",
            conversation_revision=1,
        )
        wrong_forget = memory_runtime.execute_memory_tool_call_in_tx(
            data,
            row_forget,
            memory_runtime.MEMORY_PROPOSE_TOOL,
            {
                "operation": "forget",
                "fact_key": "personal.gear.lens",
                "text": "镜头",
            },
        )
        assert wrong_forget["saved"] is False
        assert wrong_forget["reason"] == "forget_target_not_grounded_in_owner_message"

        # Lens must still be active
        snap = memory.list_memories_snapshot(data)
        active_keys = {e["fact_key"] for e in snap["entries"]}
        assert "personal.gear.lens" in active_keys
        assert "personal.gear.camera_body" in active_keys

    store.transaction(run_security_checks)


def test_explicit_multi_clause_failure_atomicity_and_false_save_claim_guard(tmp_path):
    store, _ = _make_store(tmp_path)

    def setup_near_limit(data):
        memory.update_memory_settings(data, {"budgets": {"max_entries": 8}})
        for idx in range(7):
            memory.upsert_active_entry(
                data,
                {
                    "text": f"已有条目 {idx}",
                    "category": "general",
                    "fact_key": f"personal.general.item_{idx}",
                },
                origin="manual",
            )
        # Task with 2 clauses: 1st clause would reach 8, 2nd clause overflows max_entries=8
        row = _seed_task(
            data,
            task_id="t-atomic",
            message_id="m-atomic",
            text="请记住我的机身是富士 X-T5，我的镜头是 XF16-55mm F2.8",
            conversation_revision=0,
        )
        receipt = memory_runtime.execute_explicit_owner_intent_in_tx(data, row)
        guarded = memory_runtime.guard_false_memory_save_claim(row, "好的，我已经为您记住了机身和镜头。")
        return receipt, guarded

    receipt, guarded = store.transaction(setup_near_limit)
    assert receipt["applied"] is False
    assert receipt["saved"] is False
    assert receipt["entries"] == []
    assert "agent_memory_entry_limit" in receipt["errors"][0]
    assert guarded.startswith("记忆未保存")

    # Verify first clause was rolled back atomically: still exactly 7 entries on disk
    snap = store.inspect(memory.list_memories_snapshot)
    assert len(snap["entries"]) == 7


def test_model_search_tool_enforces_use_enabled_project_generation_and_destination(tmp_path):
    store, _ = _make_store(tmp_path)
    dest_a = _dest(model_id="local/model-a", peer_id="self", source_id="src-a", rev="rev-a")
    dest_b = _dest(model_id="local/model-a", peer_id="peer-other", source_id="src-b", rev="rev-b")

    def run_search_checks(data):
        memory.upsert_active_entry(
            data,
            {
                "scope": {"kind": "project", "project_id": "proj-1", "project_generation": 1},
                "category": "project",
                "fact_key": "project.fact.gen1",
                "text": "Gen 1 摄影图像管线",
                "exposure_policy": {"mode": "destinations", "allowed_destinations": [dest_a]},
            },
            origin="manual",
        )
        # Same project ID but generation 2 cannot read generation 1 memory via model tool
        row_gen2 = _seed_task(
            data,
            task_id="t-gen2",
            message_id="m-gen2",
            text="查询项目管线",
            project={"id": "proj-1", "generation": 2},
            destination=dest_a,
        )
        res_gen2 = memory_runtime.execute_memory_tool_call_in_tx(
            data,
            row_gen2,
            memory_runtime.MEMORY_SEARCH_TOOL,
            {"query": "摄影"},
        )
        assert res_gen2["entries"] == []

        # Generation 1 with matching destination sees the entry
        row_gen1_ok = _seed_task(
            data,
            task_id="t-gen1-ok",
            message_id="m-gen1-ok",
            text="查询项目管线",
            project={"id": "proj-1", "generation": 1},
            destination=dest_a,
        )
        res_gen1_ok = memory_runtime.execute_memory_tool_call_in_tx(
            data,
            row_gen1_ok,
            memory_runtime.MEMORY_SEARCH_TOOL,
            {"query": "摄影"},
        )
        assert len(res_gen1_ok["entries"]) == 1

        # Same catalog_model_id on different peer is rejected
        row_gen1_wrong_peer = _seed_task(
            data,
            task_id="t-gen1-peer",
            message_id="m-gen1-peer",
            text="查询项目管线",
            project={"id": "proj-1", "generation": 1},
            destination=dest_b,
        )
        res_wrong_peer = memory_runtime.execute_memory_tool_call_in_tx(
            data,
            row_gen1_wrong_peer,
            memory_runtime.MEMORY_SEARCH_TOOL,
            {"query": "摄影"},
        )
        assert res_wrong_peer["entries"] == []

        # Turning use_enabled=False blocks model search tool immediately
        memory.update_memory_settings(data, {"use_enabled": False})
        res_disabled = memory_runtime.execute_memory_tool_call_in_tx(
            data,
            row_gen1_ok,
            memory_runtime.MEMORY_SEARCH_TOOL,
            {"query": "摄影"},
        )
        assert res_disabled["entries"] == []

    store.transaction(run_search_checks)


def test_background_extraction_manifest_and_strict_json_parsing(tmp_path):
    store, _ = _make_store(tmp_path)
    dest = _dest()

    def create_and_claim_job(data):
        memory.update_memory_settings(data, {"capture_enabled": True, "extraction_destination": dest})
        _seed_task(
            data,
            task_id="t-ext-1",
            message_id="m-ext-1",
            text="我平时喜欢风光摄影，我的机身是富士 X-T5",
            conversation_revision=0,
            destination=dest,
        )
        memory.enqueue_extraction_job(data, task_id="t-ext-1")
        leased = memory.claim_extraction_job(data, worker_id="ext-worker")
        return leased

    leased_job = store.transaction(create_and_claim_job)
    prompts = memory_runtime.build_extraction_messages(leased_job)
    assert len(prompts) == 2
    assert "富士 X-T5" in prompts[1]["content"]

    # Non-JSON or malformed output must raise ValueError("invalid_extraction_json") and NEVER fallback-extract
    with pytest.raises(ValueError, match="invalid_extraction_json"):
        memory_runtime.parse_extraction_response("Sure! Here is what I found: 机身是富士 X-T5", leased_job)

    # Empty candidates list is a valid no-op
    assert memory_runtime.parse_extraction_response('{"candidates": []}', leased_job) == []

    # Grounded candidate with valid source_message_ids is accepted; ungrounded candidate is filtered out
    raw_json = json.dumps(
        {
            "candidates": [
                {
                    "category": "equipment",
                    "fact_key": "personal.gear.camera_body",
                    "text": "机身是富士 X-T5",
                    "source_message_ids": ["m-ext-1"],
                },
                {
                    "category": "equipment",
                    "fact_key": "personal.gear.lens",
                    "text": "镜头是索尼 GM 24-70mm",
                    "source_message_ids": ["m-ext-1"],
                },
            ]
        },
        ensure_ascii=False,
    )
    parsed = memory_runtime.parse_extraction_response(raw_json, leased_job)
    assert len(parsed) == 1
    assert parsed[0]["text"] == "机身是富士 X-T5"
    assert parsed[0]["source_message_ids"] == ["m-ext-1"]


def test_owner_wrapper_stripping_negation_word_boundaries_and_forget_retention_isolation(tmp_path):
    store, key = _make_store(tmp_path)
    dest = _dest()

    save_sentence = "请记住这三条我的个人信息：我喜欢摄影；我的机身是富士 X-T5；我的镜头是 XF16-55mm F2.8。请分别保存为三条记忆。"
    correct_sentence = "把我的机身改成富士 X-H2，X-T5 已经卖掉了。请更新已有机身记忆，保留镜头信息。"
    forget_sentence = "忘记我的相机和镜头信息，保留摄影爱好。请实际删除这些器材记忆，并如实说明结果。"

    def run_wrapper_and_retention_checks(data):
        row = _seed_task(
            data,
            task_id="t-wrap-1",
            message_id="m-wrap-save",
            text=save_sentence,
            conversation_revision=0,
            destination=dest,
        )
        # 1. SAVE with '这三条我的个人信息：' and '请分别保存为三条记忆。' must parse into exact 3 slots
        r_save = memory_runtime.execute_explicit_owner_intent_in_tx(data, row)
        assert r_save["applied"] is True
        assert r_save["intent"] == "remember"
        snap1 = memory.list_memories_snapshot(data)["entries"]
        assert {e["fact_key"] for e in snap1} == {
            "personal.preference.photography",
            "personal.gear.camera_body",
            "personal.gear.lens",
        }
        by_key1 = {e["fact_key"]: e for e in snap1}
        assert by_key1["personal.preference.photography"]["text"] == "喜欢摄影"
        assert "X-T5" in by_key1["personal.gear.camera_body"]["text"]
        assert "XF16-55mm F2.8" in by_key1["personal.gear.lens"]["text"]
        for wrapper in ("这三条我的个人信息", "保存为三条", "请分别", "为三条记忆"):
            assert all(wrapper not in e["text"] for e in snap1)
        # Original owner message and source hash must remain untouched
        assert row["messages"][0]["content"] == save_sentence

        # 2. CORRECT updates only camera_body and preserves lens + photography
        row["conversation_revision"] = 1
        row["messages"].append(
            {
                "role": "user",
                "author_role": "owner",
                "message_id": "m-wrap-correct",
                "content": correct_sentence,
                "context_epoch": 0,
            }
        )
        r_corr = memory_runtime.execute_explicit_owner_intent_in_tx(data, row)
        assert r_corr["applied"] is True
        assert r_corr["intent"] == "update"
        snap2 = memory.list_memories_snapshot(data)["entries"]
        by_key2 = {e["fact_key"]: e for e in snap2}
        assert set(by_key2.keys()) == {
            "personal.preference.photography",
            "personal.gear.camera_body",
            "personal.gear.lens",
        }
        assert "X-H2" in by_key2["personal.gear.camera_body"]["text"]
        assert "X-T5" not in by_key2["personal.gear.camera_body"]["text"]
        assert "XF16-55mm F2.8" in by_key2["personal.gear.lens"]["text"]
        assert by_key2["personal.preference.photography"]["text"] == "喜欢摄影"

        # 3. Partial forget with '保留镜头和摄影爱好' / '不要删除镜头' must not include lens or hobby in forget targets
        partial_forget_text = "忘记我的机身，保留镜头和摄影爱好"
        partial_intent = memory_runtime.analyze_owner_memory_intent(partial_forget_text, row)
        assert partial_intent["is_explicit"] is True
        assert partial_intent["intent"] == "forget"
        assert [op["fact_key"] for op in partial_intent["operations"]] == ["personal.gear.camera_body"]

        dont_delete_lens_intent = memory_runtime.analyze_owner_memory_intent("忘记我的机身，不要删除镜头", row)
        assert dont_delete_lens_intent["is_explicit"] is True
        assert [op["fact_key"] for op in dont_delete_lens_intent["operations"]] == ["personal.gear.camera_body"]

        # Contradictory forget ('忘记我的镜头，但保留镜头') fails closed
        contradictory = memory_runtime.analyze_owner_memory_intent("忘记我的镜头，但保留镜头", row)
        assert contradictory["is_explicit"] is False
        assert contradictory["operations"] == []

        # Model tool call trying to delete preserved '镜头' when owner said '忘记我的机身，保留镜头和摄影爱好' is rejected
        row_partial = _seed_task(
            data,
            task_id="t-wrap-partial",
            message_id="m-wrap-partial",
            text=partial_forget_text,
            conversation_revision=0,
            destination=dest,
        )
        bad_tool_forget = memory_runtime.execute_memory_tool_call_in_tx(
            data,
            row_partial,
            memory_runtime.MEMORY_PROPOSE_TOOL,
            {"operation": "forget", "text": "镜头"},
        )
        assert bad_tool_forget["applied"] is False
        assert bad_tool_forget["reason"] == "forget_target_not_grounded_in_owner_message"
        assert "personal.gear.lens" in {e["fact_key"] for e in memory.list_memories_snapshot(data)["entries"]}

        # 4. Exact FORGET deletes camera_body and lens while preserving photography hobby
        row["conversation_revision"] = 2
        row["messages"].append(
            {
                "role": "user",
                "author_role": "owner",
                "message_id": "m-wrap-forget",
                "content": forget_sentence,
                "context_epoch": 0,
            }
        )
        r_forget = memory_runtime.execute_explicit_owner_intent_in_tx(data, row)
        assert r_forget["applied"] is True
        assert r_forget["intent"] == "forget"
        snap3 = memory.list_memories_snapshot(data)["entries"]
        assert [e["fact_key"] for e in snap3] == ["personal.preference.photography"]
        assert snap3[0]["text"] == "喜欢摄影"

        # 5. True negative save/remember commands remain non-mutating
        for neg_sentence in (
            "请别保存我的机身信息。",
            "不要记住我的机身是富士 X-T5。",
            "请记住我的机身是富士 X-T5，但请别保存到记忆。",
        ):
            neg_intent = memory_runtime.analyze_owner_memory_intent(neg_sentence, row)
            assert neg_intent["is_explicit"] is False
            assert neg_intent["operations"] == []

    store.transaction(run_wrapper_and_retention_checks)
    reopened = AgentStore(tmp_path, key)
    final_entries = reopened.inspect(memory.list_memories_snapshot)["entries"]
    assert [e["fact_key"] for e in final_entries] == ["personal.preference.photography"]
    assert final_entries[0]["text"] == "喜欢摄影"

