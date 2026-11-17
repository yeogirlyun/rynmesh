"""S09-I27: explicit intent boundaries, grounded paraphrases and safe forget selection."""
import copy
import json

import pytest

from test_agent_memory_runtime_helpers import _make_store, _seed_task, _dest
from test_agent_runtime import make_runtime, advance, assistant
from rynmesh.agent_runtime import memory, memory_runtime as mr, memory_integration as mi


@pytest.mark.parametrize("text", ["好的，以后请记住我用 Windows", "另外帮我记一下我用 Windows", "嗯，那今后请记住我用 Windows",
                                "Please also remember I use Windows", "From now on, please remember I use Windows"])
def test_t1_polite_connecting_prefixes_recognize_remember(text):
    result = mr.analyze_owner_memory_intent(text)
    assert result["is_explicit"] and result["intent"] == "remember"
    assert any("Windows" in op["text"] for op in result["operations"])


def test_t2_paraphrase_black_coffee_grounded_but_invented_values_rejected(tmp_path):
    assert mr.verify_proposal_grounding_in_owner_message("用户偏好黑咖啡", "我喜欢喝黑咖啡")
    for text in ("用户偏好黑咖啡，邮箱是 fake@example.com", "用户偏好黑咖啡，端口是 7", "用户偏好黑咖啡，路径是 C:/invented"):
        assert not mr.verify_proposal_grounding_in_owner_message(text, "我喜欢喝黑咖啡")
    store, _ = _make_store(tmp_path)
    def propose(data):
        row = _seed_task(data, text="我喜欢喝黑咖啡")
        return mr.execute_memory_tool_call_in_tx(data, row, mr.MEMORY_PROPOSE_TOOL,
                                                {"operation": "remember", "text": "用户偏好黑咖啡", "category": "preference"})
    assert store.transaction(propose)["saved"] is True


@pytest.mark.parametrize("text,source,expected", [
    ("甲乙丙丁戊己", "甲乙丙丁戊庚", True),  # Four of five bigrams.
    ("甲乙丙丁戊己", "甲乙丙辛戊己", False),
    ("邮箱是 a@b.c", "邮箱是 a@b.c", True),
    ("邮箱是 a@b.c", "邮箱是 a，域名 b.c", False),
    ("偏好黑咖啡 7", "我喜欢喝黑咖啡 8", False),
    ("偏好黑咖啡 C:/private", "我喜欢喝黑咖啡 C:/public", False),
    ("Windows", "WindowsServer", False),
    ("黑咖啡", '他说：“我喜欢喝黑咖啡”', False),
])
def test_t2_bigram_threshold_and_value_tokens(text, source, expected):
    assert mr.verify_proposal_grounding_in_owner_message(text, source) is expected


def seed_cameras(data, count=3, pending=False):
    for i in range(count):
        row = _seed_task(data, task_id=f"seed-{i}", message_id=f"seed-msg-{i}", text=f"记住我的相机型号{i}是富士")
        assert mr.execute_explicit_owner_intent_in_tx(data, row)["applied"]
    state = memory.ensure_memory_state(data)
    if pending:
        for i, (eid, entry) in enumerate(list(state["entries"].items())):
            state["entries"].pop(eid)
            cid = f"candidate-{i}"
            state["candidates"][cid] = {**entry, "id": cid, "status": "pending", "reset_epoch": state["reset_epoch"]}
    return state


@pytest.mark.parametrize("pending", [False, True])
def test_t3_ambiguous_forget_does_not_mutate_then_exact_key_selects_one(tmp_path, pending):
    store, _ = _make_store(tmp_path)
    def exercise(data):
        state = seed_cameras(data, pending=pending)
        before = copy.deepcopy(state)
        row = _seed_task(data, task_id="forget", text="忘记我的相机")
        receipt = mi.apply_owner_turn_memory_in_tx(None, data, row)
        assert receipt["applied"] is False and receipt["reason"] == "forget_ambiguous"
        assert receipt["needs_confirmation"] is True and len(receipt["candidates"]) == 3
        assert all(set(c) == {"id", "fact_key", "text"} and len(c["text"]) <= 80 for c in receipt["candidates"])
        assert row["memory_last_receipt"]["needs_confirmation"]
        assert state["entries"] == before["entries"] and state["candidates"] == before["candidates"]
        assert state["tombstones"] == before["tombstones"]
        target = receipt["candidates"][0]
        chosen = mr.execute_memory_tool_call_in_tx(data, row, mr.MEMORY_PROPOSE_TOOL,
                                                  {"operation": "forget", "fact_key": target["fact_key"]})
        assert chosen["applied"] is True
        assert not chosen.get("needs_confirmation")
        if pending:
            assert state["candidates"][target["id"]]["status"] == "rejected"
            assert sum(c["status"] == "pending" for c in state["candidates"].values()) == 2
        else:
            assert target["id"] not in state["entries"] and len(state["entries"]) == 2
    store.transaction(exercise)


@pytest.mark.parametrize("query,count,applied", [("忘记我", 3, False), ("忘记我的相机", 1, True)])
def test_t3_single_character_does_not_delete_and_unique_match_still_deletes(tmp_path, query, count, applied):
    store, _ = _make_store(tmp_path)
    def exercise(data):
        state = seed_cameras(data, count=count)
        row = _seed_task(data, task_id="forget", text=query)
        receipt = mr.execute_explicit_owner_intent_in_tx(data, row)
        assert receipt["applied"] is applied
        assert len(state["entries"]) == (0 if applied else count)
    store.transaction(exercise)


@pytest.mark.parametrize("text", ["帮我把端口改成 8080", "删除 build 目录", "清除缓存", "纠正端口是 8080"])
def test_t4_coding_commands_are_not_memory_and_emit_no_host_result(tmp_path, text):
    assert mr.analyze_owner_memory_intent(text)["is_explicit"] is False
    runtime, model, _, _ = make_runtime(tmp_path, assistant("Done"))
    task = runtime.create({"message": text})
    advance(runtime)
    assert not any("host_memory_operation_result" in str(m.get("content", "")) for m in model.calls[0][1])
    assert not runtime.store.read().get("memory", {}).get("entries")


@pytest.mark.parametrize("text", ["删除我的相机偏好", "忘记我的相机", "清除关于我的相机偏好", "把我的邮箱改成 a@b.c"])
def test_t5_personal_memory_forget_and_update_remain_explicit(text):
    result = mr.analyze_owner_memory_intent(text)
    assert result["is_explicit"] and result["intent"] in {"forget", "update"}


@pytest.mark.parametrize("text", ["他说这个不行，另外请记住我用 Windows", "日志里说连接失败。请记住我用 Windows",
                                "他说这个不行。\n另外请记住我用 Windows", "They said no. Also remember I use Windows"])
def test_t6_unrelated_reported_speech_does_not_veto_owner_command(tmp_path, text):
    result = mr.analyze_owner_memory_intent(text)
    assert result["is_explicit"] and result["intent"] == "remember"
    runtime, model, _, _ = make_runtime(tmp_path, assistant("Done"))
    task = runtime.create({"message": text})
    advance(runtime)
    assert runtime.store.read()["tasks"][task["id"]]["memory_last_receipt"]["applied"]


def test_t7_remember_a_do_not_save_password_policy_is_not_negated():
    result = mr.analyze_owner_memory_intent("记住，不要保存密码")
    assert result["is_explicit"] and result["intent"] == "remember"


@pytest.mark.parametrize("applied", [False, True])
def test_t8_t9_host_note_reports_actual_result_without_suppressing_other_work(applied):
    data = {}
    row = _seed_task(data, text="记住我喜欢黑咖啡；然后检查服务")
    row["memory_last_receipt"] = {"intent": "remember", "applied": applied, "message_id": "msg-owner-0",
                                  "conversation_revision": 0, "entries": [{"id": "mem-one"}] if applied else [],
                                  "reason": None if applied else "not_applied"}
    note = mi.current_host_memory_operation_note(None, row)
    assert "do not call business tools" not in note.lower()
    assert ("其它工作照常完成" if applied else "记忆操作未执行") in note


@pytest.mark.parametrize("text", ["好的，不要记住我的相机是富士", "假如我说：\n请记住我用 Windows",
                                "他说：\n请记住我用 Windows", "“请记住我用 Windows”", "怎么记住我用 Windows？",
                                "He said:\nPlease remember I use Windows", "If I said:\nPlease remember I use Windows"])
def test_exclusions_still_apply_to_command_and_reporting_lead_in(text):
    assert mr.analyze_owner_memory_intent(text)["is_explicit"] is False


def test_t9_memory_result_does_not_block_real_business_tool_dispatch(tmp_path):
    runtime, model, tools, _ = make_runtime(tmp_path, assistant(name="inspect_service", args={"service_id": "svc1"}), assistant("Done"))
    runtime.create({"message": "请记住我喜欢黑咖啡；然后检查服务 svc1"})
    advance(runtime, 4)
    assert tools.calls[0][0] == "inspect_service"
    note = next(m["content"] for m in model.calls[0][1] if "host_memory_operation_result" in str(m.get("content", "")))
    assert "其它工作照常完成" in note
    assert len(runtime.store.read().get("memory", {}).get("entries", {})) == 1


def test_unique_pending_rejection_has_truthful_applied_host_note(tmp_path):
    store, _ = _make_store(tmp_path)
    def exercise(data):
        seed_cameras(data, count=1, pending=True)
        row = _seed_task(data, task_id="forget", text="忘记我的相机")
        result = mi.apply_owner_turn_memory_in_tx(None, data, row)
        assert result["applied"]
        note = mi.current_host_memory_operation_note(None, row)
        assert json.loads(note.split("\n", 1)[0])["host_memory_operation_result"]["applied"] is True
    store.transaction(exercise)


def test_ambiguous_model_forget_does_not_disclose_unapproved_candidate_content(tmp_path):
    store, _ = _make_store(tmp_path)
    def exercise(data):
        state = seed_cameras(data)
        for entry in state["entries"].values():
            entry["exposure_policy"] = {"mode": "selected_models", "allowed_destinations": [_dest(model_id="other") ]}
        row = _seed_task(data, task_id="forget", text="忘记我的相机")
        result = mr.execute_memory_tool_call_in_tx(data, row, mr.MEMORY_PROPOSE_TOOL, {"operation": "forget"})
        assert result["needs_confirmation"] and result["candidates"] == []
        assert len(row["memory_last_receipt"]["candidates"]) == 3  # Owner-facing encrypted receipt retains the choices.
    store.transaction(exercise)


def test_sentence_boundaries_inside_quoted_owner_values_are_preserved():
    result = mr.analyze_owner_memory_intent('Remember my alias is "J. Smith". Also inspect service.')
    assert result["is_explicit"] and 'J. Smith' in result["operations"][0]["text"]


def test_exact_owner_fact_key_resolves_forget_without_broadening(tmp_path):
    store, _ = _make_store(tmp_path)
    def exercise(data):
        state = seed_cameras(data)
        key = next(iter(state["entries"].values()))["fact_key"]
        row = _seed_task(data, task_id="forget", text="forget " + key)
        result = mr.execute_explicit_owner_intent_in_tx(data, row)
        assert result["applied"] and len(result["deleted_ids"]) == 1
        assert len(state["entries"]) == 2
    store.transaction(exercise)


@pytest.mark.parametrize("text", ["He said:\nPlease remember I use Windows", "If I said:\nPlease remember I use Windows"])
def test_reporting_introductions_cannot_be_saved_through_model_propose(tmp_path, text):
    store, _ = _make_store(tmp_path)
    def exercise(data):
        row = _seed_task(data, text=text)
        result = mr.execute_memory_tool_call_in_tx(data, row, mr.MEMORY_PROPOSE_TOOL,
                                                  {"operation": "remember", "text": "Windows"})
        assert result["saved"] is False
    store.transaction(exercise)


def test_ambiguous_turn_never_autodeletes_remaining_match_on_later_steps(tmp_path):
    store, _ = _make_store(tmp_path)
    def exercise(data):
        state = seed_cameras(data)
        row = _seed_task(data, task_id="forget", text="忘记我的相机")
        initial = mr.execute_explicit_owner_intent_in_tx(data, row)
        assert initial["needs_confirmation"]
        for target in initial["candidates"][:2]:
            result = mr.execute_memory_tool_call_in_tx(data, row, mr.MEMORY_PROPOSE_TOOL,
                                                       {"operation": "forget", "fact_key": target["fact_key"]})
            assert result["applied"]
        assert len(state["entries"]) == 1
        mr.execute_explicit_owner_intent_in_tx(data, row)  # The real loop rechecks the same owner turn every step.
        assert len(state["entries"]) == 1
    store.transaction(exercise)


def test_exact_slot_namespace_fact_key_can_resolve_ambiguous_forget(tmp_path):
    store, _ = _make_store(tmp_path)
    def exercise(data):
        state = seed_cameras(data)
        for entry in state["entries"].values():
            entry["fact_key"] = entry["fact_key"].replace(".profile.", ".slot.")
        row = _seed_task(data, task_id="forget", text="忘记我的相机")
        initial = mr.execute_explicit_owner_intent_in_tx(data, row)
        assert initial["needs_confirmation"]
        result = mr.execute_memory_tool_call_in_tx(data, row, mr.MEMORY_PROPOSE_TOOL,
                                                  {"operation": "forget", "fact_key": initial["candidates"][0]["fact_key"]})
        assert result["applied"] and len(state["entries"]) == 2
    store.transaction(exercise)
