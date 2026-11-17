"""Overhaul S10-I24: Heartbeat busy/skip classification, continued_to chain routing, and binding settlement/instructions to automation_run_id."""
from __future__ import annotations

import datetime as dt
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from rynmesh.agent_runtime import automation_worker, automations
from rynmesh.agent_runtime.routes import install_agent
from rynmesh.agent_runtime.store import AgentError
from rynmesh.background_workers import BackgroundWorkerRegistry
from rynmesh.services import peer_box


class _CapturingModel:
    supports_destination_binding = True
    supports_output_token_limit = True
    supports_request_deadline = True
    supports_request_id = True
    supports_conversation_key = True

    def __init__(self) -> None:
        self.responses: list[dict[str, Any]] = []
        self.captured_messages: list[list[dict[str, Any]]] = []

    def complete(self, model_id, messages, specs, cancelled, on_event, **options):
        self.captured_messages.append(list(messages))
        if self.responses:
            return self.responses.pop(0)
        return {
            "message": {"role": "assistant", "content": "默认回复。"},
            "finish_reason": "stop",
            "usage": {"input_tokens": 20, "output_tokens": 12},
        }


class _FakeTools:
    def specs(self):
        return []

    def metadata(self, name: str):
        return {"effect": "read", "title": name}

    def execute(self, name: str, arguments: dict, **options):
        return {"ok": True}


class _FakeCatalog:
    def list_models(self):
        return [
            {
                "id": "local/gpt-4o",
                "name": "GPT-4o",
                "source": "local_api",
                "available": True,
                "agent_supported": True,
                "destination_binding_supported": True,
                "destination_revision": "rev-1",
                "context_window_tokens": 128000,
            }
        ]


class _FakeNodeStore:
    peer_id = "peer-s10-i24"


def _make_env(tmp_path: Path):
    app = FastAPI()
    workers = BackgroundWorkerRegistry()
    key = peer_box.load_or_create_messaging_key(tmp_path / "messaging.x25519")
    model = _CapturingModel()
    tools = _FakeTools()
    catalog = _FakeCatalog()
    runtime = install_agent(
        app,
        store=_FakeNodeStore(),
        home=tmp_path,
        workers=workers,
        local_control=lambda _req: None,
        messaging_key=key,
        integrations=(model, tools, catalog),
    )
    runtime.update_settings({"enabled": True, "model_id": "local/gpt-4o", "desktop_access": True})
    client = TestClient(app)
    worker: automation_worker.AutomationSchedulerWorker = app.state.agent_automation_worker
    return app, runtime, model, client, worker


def _create_finished_task(runtime, model: _CapturingModel, prompt: str = "初始对话") -> dict[str, Any]:
    model.responses.append(
        {
            "message": {"role": "assistant", "content": "收到，准备就绪。"},
            "finish_reason": "stop",
            "usage": {"input_tokens": 10, "output_tokens": 10},
        }
    )
    created = runtime.create({"message": prompt})
    runtime.run_once()
    done = runtime.get(created["id"])
    assert done["status"] == "completed"
    return done


def _create_heartbeat(
    client: TestClient,
    target_task_id: str,
    *,
    name: str = "巡检心跳",
    prompt: str = "检查状态",
    notification_policy: str = "all_runs",
) -> dict[str, Any]:
    res = client.post(
        "/api/local/agent/automations",
        json={
            "name": name,
            "prompt": prompt,
            "repeat": "hourly",
            "interval_hours": 1,
            "time": "00:00",
            "kind": "heartbeat",
            "target_task_id": target_task_id,
            "notification_policy": notification_policy,
        },
    )
    assert res.status_code == 200, res.text
    return res.json()["automation"]


@pytest.mark.parametrize(
    ("hour", "minute", "second", "microsecond"),
    [
        (19, 0, 10, 0),
        (19, 20, 0, 0),
        (19, 53, 36, 271_000),
        (19, 59, 50, 0),
    ],
    ids=["top_of_hour_plus_10s", "mid_hour_20m", "late_hour_53m36s", "ten_sec_before_hour"],
)
def test_t1_paused_task_skips_heartbeat_with_owner_paused_and_advances_schedule(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    hour: int,
    minute: int,
    second: int,
    microsecond: int,
):
    """T1: 任务 paused → 心跳不改状态、run=SKIPPED(owner_paused)、下一次时间正常推进（覆盖改动 1）。"""
    _app, runtime, model, client, worker = _make_env(tmp_path)
    task = _create_finished_task(runtime, model)
    tid = task["id"]

    local_tz = automations._local_tz()
    fixed_dt = dt.datetime(2026, 11, 16, hour, minute, second, microsecond, tzinfo=local_tz)
    fixed_now_ms = int(fixed_dt.timestamp() * 1000)
    monkeypatch.setattr(automations, "now_ms", lambda: fixed_now_ms)

    hb = _create_heartbeat(client, tid)
    auto_id = hb["id"]

    # Put task into owner-paused state:
    runtime.store.transaction(lambda d: d["tasks"][tid].update(status="paused"))
    msg_count_before = len(runtime.get(tid)["messages"])

    due_ms = fixed_now_ms - 5_000
    runtime.store.transaction(lambda d: d["automations"][auto_id].update(next_run_at=due_ms))

    assert worker.run_once().activity is True

    after_task = runtime.get(tid)
    assert after_task["status"] == "paused"
    assert len(after_task["messages"]) == msg_count_before

    listed = runtime.list_automations()
    auto_item = next(i for i in listed["items"] if i["id"] == auto_id)
    assert auto_item["status"] == "ACTIVE"

    expected_next = automations.compute_next_run_at(
        auto_item["rrule"],
        after_ms=fixed_now_ms,
        salt=automations.ensure_jitter_salt(runtime.store),
        automation_id=auto_id,
        run_count=0,
    )
    assert auto_item["next_run_at"] == expected_next
    assert auto_item["next_run_at"] > fixed_now_ms
    assert auto_item["next_run_at"] != fixed_now_ms + 60_000
    if minute == 0 and second == 10:
        assert auto_item["next_run_at"] >= fixed_now_ms + 59 * 60 * 1000

    # Skipped slot must not immediately re-trigger on the next scheduler tick:
    assert worker.run_once().activity is False

    runs = [r for r in listed["runs"] if r["automation_id"] == auto_id]
    assert len(runs) == 1
    assert runs[0]["status"] == "SKIPPED"
    assert runs[0]["skip_reason"] == "owner_paused"
    assert "SKIPPED" in automations.AUTOMATION_RUN_STATUSES


def test_t2_interrupted_outcome_unknown_and_fork_pending_each_skip_without_clearing_error(tmp_path: Path):
    """T2: interrupted、error=="agent_write_outcome_unknown"、fork 待指令各自跳过且 error 不被清（覆盖改动 1）。"""
    _app, runtime, model, client, _worker = _make_env(tmp_path)

    cases = [
        (
            "interrupted_case",
            {"status": "interrupted", "error": "agent_node_restarted"},
            "interrupted",
            "agent_node_restarted",
        ),
        (
            "outcome_unknown_case",
            {
                "status": "interrupted",
                "error": "agent_write_outcome_unknown",
                "in_flight": {"name": "mcp__x__mut", "effect": "write", "dispatched": True, "operation_id": "op_u"},
            },
            "outcome_unknown",
            "agent_write_outcome_unknown",
        ),
        (
            "fork_pending_case",
            {"status": "paused", "error": None, "fork": {"awaiting_instruction": True, "source_task_id": "agent_src"}},
            "fork_pending",
            None,
        ),
    ]

    for label, patch_fields, expected_reason, expected_error in cases:
        task = _create_finished_task(runtime, model, prompt=f"Case {label}")
        tid = task["id"]
        hb = _create_heartbeat(client, tid, name=f"HB-{label}")
        auto_id = hb["id"]

        runtime.store.transaction(lambda d, t=tid, pf=patch_fields: d["tasks"][t].update(**pf))
        now_val = automations.now_ms()
        outcome = runtime.store.transaction(
            lambda d, aid=auto_id, ts=now_val: automation_worker.trigger_automation_in_tx(
                runtime, d, aid, force=False, now_epoch_ms=ts
            )
        )
        assert outcome["triggered"] is False
        assert outcome["skipped"] is True
        assert outcome["reason"] == expected_reason

        after = runtime.store.read()["tasks"][tid]
        assert after["status"] == patch_fields["status"]
        assert after["error"] == expected_error

        run_rec = outcome["run"]
        assert run_rec["status"] == "SKIPPED"
        assert run_rec["skip_reason"] == expected_reason


def test_t3_force_true_rejects_outcome_unknown_with_agent_operation_unresolved_without_clearing_marker(tmp_path: Path):
    """T3: force=True 对 outcome_unknown 返回错误且不抹标记（覆盖改动 1）。"""
    _app, runtime, model, client, _worker = _make_env(tmp_path)
    task = _create_finished_task(runtime, model)
    tid = task["id"]
    hb = _create_heartbeat(client, tid)
    auto_id = hb["id"]

    runtime.store.transaction(
        lambda d: d["tasks"][tid].update(
            status="interrupted",
            error="agent_write_outcome_unknown",
        )
    )

    with pytest.raises(AgentError, match="agent_operation_unresolved"):
        runtime.run_automation_now(auto_id)

    http_res = client.post(f"/api/local/agent/automations/{auto_id}/run-now")
    assert http_res.status_code == 409
    assert http_res.json()["detail"] == "agent_operation_unresolved"

    # Marker must remain intact:
    stored = runtime.store.read()["tasks"][tid]
    assert stored["status"] == "interrupted"
    assert stored["error"] == "agent_write_outcome_unknown"


def test_t4_pending_control_defers_heartbeat_by_60_seconds_without_overwriting_task(tmp_path: Path):
    """T4: pending_control 非空 → 推迟 60 秒而非覆盖（覆盖改动 1）。"""
    _app, runtime, model, client, _worker = _make_env(tmp_path)
    task = _create_finished_task(runtime, model)
    tid = task["id"]
    hb = _create_heartbeat(client, tid)
    auto_id = hb["id"]

    runtime.store.transaction(
        lambda d: d["tasks"][tid].update(
            status="paused",
            pending_control={"kind": "edit_message", "message_index": 0, "message": {"role": "user", "content": "edited"}},
        )
    )

    now_val = automations.now_ms()
    outcome = runtime.store.transaction(
        lambda d: automation_worker.trigger_automation_in_tx(
            runtime, d, auto_id, force=False, now_epoch_ms=now_val
        )
    )
    assert outcome["triggered"] is False
    assert outcome["deferred"] is True

    auto_after = runtime.store.read()["automations"][auto_id]
    assert auto_after["next_run_at"] == now_val + 60_000

    task_after = runtime.store.read()["tasks"][tid]
    assert task_after["status"] == "paused"
    assert task_after["pending_control"]["kind"] == "edit_message"


def test_t5_continued_task_chain_retargets_heartbeat_to_chain_tail_and_pauses_if_tail_deleted(tmp_path: Path):
    """T5: 任务已续接 → 心跳投到续接后的任务；续接链末端已删除则暂停（覆盖改动 2）。"""
    _app, runtime, model, client, worker = _make_env(tmp_path)
    task1 = _create_finished_task(runtime, model, prompt="Task 1")
    task2 = _create_finished_task(runtime, model, prompt="Task 2")
    task3 = _create_finished_task(runtime, model, prompt="Task 3")

    hb = _create_heartbeat(client, task1["id"], prompt="检查续接链末端状态")
    auto_id = hb["id"]

    # Link task1 -> task2 -> task3:
    def _link_chain(d):
        d["tasks"][task1["id"]]["continued_to"] = task2["id"]
        d["tasks"][task2["id"]]["continued_from"] = task1["id"]
        d["tasks"][task2["id"]]["continued_to"] = task3["id"]
        d["tasks"][task3["id"]]["continued_from"] = task2["id"]
        d["automations"][auto_id]["next_run_at"] = automations.now_ms() - 1000

    runtime.store.transaction(_link_chain)
    model.responses.append(
        {
            "message": {
                "role": "assistant",
                "content": (
                    "已在续接任务完成检查。\n"
                    "<heartbeat>\n"
                    f"<automation_id>{auto_id}</automation_id>\n"
                    "<decision>NOTIFY</decision>\n"
                    "<message>续接任务心跳完成</message>\n"
                    "</heartbeat>"
                ),
            },
            "finish_reason": "stop",
            "usage": {"input_tokens": 20, "output_tokens": 20},
        }
    )

    assert worker.run_once().activity is True
    listed = runtime.list_automations()
    auto_item = next(i for i in listed["items"] if i["id"] == auto_id)
    assert auto_item["target_task_id"] == task3["id"]
    assert auto_item["last_task_id"] == task3["id"]

    runtime.run_once()
    assert runtime.get(task3["id"])["status"] == "completed"
    assert "续接任务" in runtime.get(task3["id"])["result"]

    # Now point task3 -> missing deleted task4 and verify heartbeat auto-pauses:
    def _link_deleted_tail(d):
        d["tasks"][task3["id"]]["continued_to"] = "agent_deleted_tail"
        d.setdefault("deleted_tasks", {})["agent_deleted_tail"] = {"id": "agent_deleted_tail"}
        d["automations"][auto_id]["next_run_at"] = automations.now_ms() - 1000

    runtime.store.transaction(_link_deleted_tail)
    assert worker.run_once().activity is True
    paused_item = next(i for i in runtime.list_automations()["items"] if i["id"] == auto_id)
    assert paused_item["status"] == "PAUSED"


def test_t6_owner_followup_clears_automation_turn_effects_without_hiding_reply_or_mutating_summary(tmp_path: Path):
    """T6: 心跳完成后主人再发一条普通消息并完成 → last_run_summary/memory.md/通知都不变、failed_runs_only 下该回复不被隐藏、developer 说明不再含心跳段（覆盖改动 3）。"""
    _app, runtime, model, client, worker = _make_env(tmp_path)
    base_task = _create_finished_task(runtime, model, prompt="帮我盯住构建")
    tid = base_task["id"]
    hb = _create_heartbeat(
        client,
        tid,
        name="构建静默监控",
        prompt="检查构建失败没有",
        notification_policy="failed_runs_only",
    )
    auto_id = hb["id"]
    mem_path = Path(hb["memory_path"])

    model.captured_messages.clear()
    model.responses.append(
        {
            "message": {
                "role": "assistant",
                "content": (
                    '第一次心跳检查完毕。\n::inbox-item{title="心跳1" summary="第一次心跳摘要AAA"}\n'
                    "<heartbeat>\n"
                    f"<automation_id>{auto_id}</automation_id>\n"
                    "<decision>NOTIFY</decision>\n"
                    "<message>第一次心跳摘要AAA</message>\n"
                    "</heartbeat>"
                ),
            },
            "finish_reason": "stop",
            "usage": {"input_tokens": 25, "output_tokens": 25},
        }
    )
    runtime.store.transaction(lambda d: d["automations"][auto_id].update(next_run_at=automations.now_ms() - 1000))
    assert worker.run_once().activity is True
    runtime.run_once()

    state_after_hb1 = runtime.list_automations()
    auto_after_hb1 = next(i for i in state_after_hb1["items"] if i["id"] == auto_id)
    assert auto_after_hb1["last_run_summary"] == "第一次心跳摘要AAA"
    mem_after_hb1 = mem_path.read_text(encoding="utf-8")
    assert "第一次心跳摘要AAA" not in mem_after_hb1
    hb1_run_id = runtime.store.read()["tasks"][tid]["automation_run_id"]
    assert f"(completed) run={hb1_run_id} decision=NOTIFY" in mem_after_hb1
    task_after_hb1 = runtime.get(tid)
    notif_after_hb1 = task_after_hb1.get("automation_notification")

    model.captured_messages.clear()
    model.responses.append(
        {
            "message": {"role": "assistant", "content": "这是给主人的普通回复BBB，不能被隐藏或写进自动化摘要。"},
            "finish_reason": "stop",
            "usage": {"input_tokens": 30, "output_tokens": 20},
        }
    )
    runtime.add_message(
        tid,
        {
            "message": "顺便问一下现在的分支名是什么？",
            "expected_control_revision": task_after_hb1["control_revision"],
        },
    )
    runtime.run_once()

    task_after_owner = runtime.get(tid)
    assert task_after_owner["status"] == "completed"
    assert task_after_owner["source"] == "automation"
    assert task_after_owner["automation_id"] == auto_id
    assert task_after_owner["latest_turn_is_silent_heartbeat"] is False
    last_assistant_msg = [m for m in task_after_owner["messages"] if m.get("role") == "assistant"][-1]
    assert (last_assistant_msg.get("ui") or {}).get("hidden") is not True
    assert (last_assistant_msg.get("ui") or {}).get("kind") != "silent_heartbeat"

    owner_dev_texts = "\n".join(
        str(m.get("content") or "") for m in model.captured_messages[-1] if m.get("role") in {"developer", "system"}
    )
    assert "# Heartbeat wakeups" not in owner_dev_texts
    assert "not interactively watching" not in owner_dev_texts

    state_after_owner = runtime.list_automations()
    auto_after_owner = next(i for i in state_after_owner["items"] if i["id"] == auto_id)
    assert auto_after_owner["last_run_summary"] == "第一次心跳摘要AAA"
    assert mem_path.read_text(encoding="utf-8") == mem_after_hb1
    assert task_after_owner.get("automation_notification") == notif_after_hb1


def test_t7_subsequent_heartbeat_after_owner_message_restores_instructions_and_settles_normally(tmp_path: Path):
    """T7: 主人发言后下一次心跳照常结算并恢复说明，且同一次 run 不重复结算（覆盖改动 3）。"""
    _app, runtime, model, client, worker = _make_env(tmp_path)
    base_task = _create_finished_task(runtime, model, prompt="监控任务")
    tid = base_task["id"]
    hb = _create_heartbeat(client, tid, name="恢复监控", prompt="继续巡检")
    auto_id = hb["id"]
    mem_path = Path(hb["memory_path"])

    # Heartbeat #1:
    model.responses.append(
        {
            "message": {
                "role": "assistant",
                "content": (
                    '巡检1。\n::inbox-item{title="巡检1" summary="巡检摘要1"}\n'
                    f"<heartbeat><automation_id>{auto_id}</automation_id><decision>DONT_NOTIFY</decision><message>巡检摘要1</message></heartbeat>"
                ),
            },
            "finish_reason": "stop",
            "usage": {"input_tokens": 20, "output_tokens": 20},
        }
    )
    runtime.store.transaction(lambda d: d["automations"][auto_id].update(next_run_at=automations.now_ms() - 1000))
    worker.run_once()
    runtime.run_once()

    # Owner speaks:
    t1_state = runtime.get(tid)
    model.captured_messages.clear()
    model.responses.append(
        {
            "message": {"role": "assistant", "content": "收到主人补充说明。"},
            "finish_reason": "stop",
            "usage": {"input_tokens": 20, "output_tokens": 15},
        }
    )
    runtime.add_message(tid, {"message": "主人发言测试", "expected_control_revision": t1_state["control_revision"]})
    runtime.run_once()
    owner_dev = "\n".join(
        str(m.get("content") or "") for m in model.captured_messages[-1] if m.get("role") in {"developer", "system"}
    )
    assert "# Heartbeat wakeups" not in owner_dev

    # Heartbeat #2 after owner message:
    model.captured_messages.clear()
    model.responses.append(
        {
            "message": {
                "role": "assistant",
                "content": (
                    '巡检2完成。\n::inbox-item{title="巡检2" summary="第二次心跳摘要CCC"}\n'
                    "<heartbeat>\n"
                    f"<automation_id>{auto_id}</automation_id>\n"
                    "<decision>NOTIFY</decision>\n"
                    "<message>第二次心跳摘要CCC</message>\n"
                    "</heartbeat>"
                ),
            },
            "finish_reason": "stop",
            "usage": {"input_tokens": 30, "output_tokens": 25},
        }
    )
    runtime.store.transaction(lambda d: d["automations"][auto_id].update(next_run_at=automations.now_ms() - 1000))
    assert worker.run_once().activity is True
    runtime.run_once()

    hb2_dev_texts = "\n".join(
        str(m.get("content") or "") for m in model.captured_messages[-1] if m.get("role") in {"developer", "system"}
    )
    assert "# Heartbeat wakeups" in hb2_dev_texts
    assert "not interactively watching" in hb2_dev_texts

    state_after_hb2 = runtime.list_automations()
    auto_after_hb2 = next(i for i in state_after_hb2["items"] if i["id"] == auto_id)
    assert auto_after_hb2["last_run_summary"] == "第二次心跳摘要CCC"
    mem_text = mem_path.read_text(encoding="utf-8")
    assert "第二次心跳摘要CCC" not in mem_text
    hb2_run_id = runtime.store.read()["tasks"][tid]["automation_run_id"]
    hb2_note = f"(completed) run={hb2_run_id} decision=NOTIFY"
    assert mem_text.count(hb2_note) == 1

    # Calling settle_automation_turn_in_tx again on the already-settled run must be a no-op:
    runtime.store.transaction(
        lambda d: automations.settle_automation_turn_in_tx(runtime.store, d, d["tasks"][tid])
    )
    assert mem_path.read_text(encoding="utf-8") == mem_text
