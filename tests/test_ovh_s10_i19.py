"""Tests for S10-I19: scheduled automation run settlement, notifications, overlap/missed-slot handling, and retention."""
from __future__ import annotations

from pathlib import Path
from typing import Any

from fastapi import FastAPI
from fastapi.testclient import TestClient

from rynmesh.agent_runtime import automation_worker, automations
from rynmesh.agent_runtime.routes import install_agent
from rynmesh.background_workers import BackgroundWorkerRegistry
from rynmesh.services import peer_box


class _StubModel:
    supports_destination_binding = True
    supports_output_token_limit = True
    supports_request_deadline = True
    supports_request_id = True
    supports_conversation_key = True

    def __init__(self) -> None:
        self.responses: list[Any] = []

    def complete(self, model_id, messages, specs, cancelled, on_event, **options):
        if self.responses:
            item = self.responses.pop(0)
            if isinstance(item, Exception):
                raise item
            return item
        return {
            "message": {
                "role": "assistant",
                "content": '已完成检查。\n::inbox-item{title="完成" summary="正常完成本次运行"}',
            },
            "finish_reason": "stop",
            "usage": {"input_tokens": 20, "output_tokens": 15},
        }


class _StubTools:
    def specs(self):
        return []

    def metadata(self, name: str):
        return {"effect": "read", "title": name}

    def execute(self, name: str, arguments: dict, **options):
        return {"ok": True}


class _StubCatalog:
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


class _StubNodeStore:
    peer_id = "peer-s10-i19"


def _make_env(tmp_path: Path):
    app = FastAPI()
    workers = BackgroundWorkerRegistry()
    key = peer_box.load_or_create_messaging_key(tmp_path / "messaging.x25519")
    model = _StubModel()
    runtime = install_agent(
        app,
        store=_StubNodeStore(),
        home=tmp_path,
        workers=workers,
        local_control=lambda _req: None,
        messaging_key=key,
        integrations=(model, _StubTools(), _StubCatalog()),
    )
    runtime.update_settings({"enabled": True, "model_id": "local/gpt-4o", "desktop_access": True})
    client = TestClient(app)
    worker: automation_worker.AutomationSchedulerWorker = app.state.agent_automation_worker
    return app, runtime, model, client, worker


def test_t1_failed_run_with_dont_notify_still_notifies(tmp_path: Path):
    _app, runtime, _model, client, worker = _make_env(tmp_path)
    created = client.post(
        "/api/local/agent/automations",
        json={
            "name": "失败不可隐藏测试",
            "prompt": "执行日报汇总",
            "repeat": "daily",
            "time": "09:00",
            "notification_policy": "failed_runs_only",
        },
    ).json()["automation"]
    auto_id = created["id"]

    run_res = client.post(f"/api/local/agent/automations/{auto_id}/run-now").json()
    task_id = run_res["task_id"]
    run_id = run_res["run"]["id"]

    def _force_failed_with_dont_notify(data):
        row = data["tasks"][task_id]
        row["messages"].append(
            {
                "role": "assistant",
                "content": (
                    "尝试隐藏错误\n"
                    "<heartbeat>\n"
                    f"<automation_id>{auto_id}</automation_id>\n"
                    "<decision>DONT_NOTIFY</decision>\n"
                    "<message>一切正常无需打扰</message>\n"
                    "</heartbeat>"
                ),
            }
        )
        row["status"] = "failed"
        row["error"] = {"code": "agent_tool_failed", "message": "数据库连接被拒绝"}

    runtime.store.transaction(_force_failed_with_dont_notify)
    assert worker.run_once().activity is True

    settled_task = runtime.get(task_id)
    assert settled_task["latest_turn_is_silent_heartbeat"] is False
    notif = settled_task.get("automation_notification")
    assert isinstance(notif, dict)
    assert notif["status"] == "failed"
    assert notif["decision"] == "NOTIFY"

    runs = {r["id"]: r for r in runtime.list_automations()["runs"]}
    assert runs[run_id]["status"] == "FAILED"


def test_t2_failed_heartbeat_without_new_reply_does_not_inherit_previous_turn(tmp_path: Path):
    _app, runtime, model, client, worker = _make_env(tmp_path)
    model.responses = [
        {
            "message": {"role": "assistant", "content": "已开启监控。"},
            "finish_reason": "stop",
            "usage": {"input_tokens": 10, "output_tokens": 8},
        }
    ]
    base_task = runtime.create({"message": "持续监控接口健康"})
    runtime.run_once()

    hb = client.post(
        "/api/local/agent/automations",
        json={
            "name": "接口健康心跳",
            "prompt": "检查健康状态",
            "repeat": "minutely",
            "interval_minutes": 5,
            "kind": "heartbeat",
            "target_task_id": base_task["id"],
        },
    ).json()["automation"]
    auto_id = hb["id"]
    mem_path = Path(hb["memory_path"])

    # First heartbeat succeeds with DONT_NOTIFY and an old summary
    model.responses = [
        {
            "message": {
                "role": "assistant",
                "content": (
                    '旧轮次一切正常\n::inbox-item{title="旧标题" summary="旧轮次静默摘要XYZ"}\n'
                    "<heartbeat>\n"
                    f"<automation_id>{auto_id}</automation_id>\n"
                    "<decision>DONT_NOTIFY</decision>\n"
                    "<message>旧轮次静默摘要XYZ</message>\n"
                    "</heartbeat>"
                ),
            },
            "finish_reason": "stop",
            "usage": {"input_tokens": 15, "output_tokens": 20},
        }
    ]
    runtime.store.transaction(lambda d: d["automations"][auto_id].update(next_run_at=automations.now_ms() - 1000))
    worker.run_once()
    runtime.run_once()

    mem_after_first = mem_path.read_text(encoding="utf-8")
    count_old_in_mem = mem_after_first.count("旧轮次静默摘要XYZ")

    # Second heartbeat wakes up the task, then fails BEFORE producing any new assistant reply
    runtime.store.transaction(lambda d: d["automations"][auto_id].update(next_run_at=automations.now_ms() - 1000))
    worker.run_once()

    def _fail_second_heartbeat_without_assistant_reply(data):
        t_row = data["tasks"][base_task["id"]]
        t_row["status"] = "failed"
        t_row["error"] = {"code": "agent_upstream_failed", "message": "上游模型 503 不可用"}
        return t_row["automation_run_id"]

    second_run_id = runtime.store.transaction(_fail_second_heartbeat_without_assistant_reply)
    assert worker.run_once().activity is True

    settled_task = runtime.get(base_task["id"])
    notif = settled_task.get("automation_notification")
    assert isinstance(notif, dict)
    assert notif["id"] == second_run_id
    assert notif["status"] == "failed"
    assert "上游模型 503 不可用" in notif["message"]
    assert "旧轮次静默摘要XYZ" not in notif["message"]

    listed = runtime.list_automations()
    auto_after = next(i for i in listed["items"] if i["id"] == auto_id)
    assert "上游模型 503 不可用" in auto_after["last_run_summary"]
    assert "旧轮次静默摘要XYZ" not in auto_after["last_run_summary"]

    run2 = next(r for r in listed["runs"] if r["id"] == second_run_id)
    assert run2["status"] == "FAILED"
    assert run2["decision"] == ""

    mem_after_second = mem_path.read_text(encoding="utf-8")
    assert mem_after_second.count("旧轮次静默摘要XYZ") == count_old_in_mem


def test_t3_deleted_task_run_becomes_lost_only_after_ten_minutes(tmp_path: Path):
    _app, runtime, _model, client, worker = _make_env(tmp_path)
    auto = client.post(
        "/api/local/agent/automations",
        json={"name": "丢失任务检测", "prompt": "执行检查", "repeat": "daily", "time": "10:00"},
    ).json()["automation"]
    auto_id = auto["id"]

    triggered = client.post(f"/api/local/agent/automations/{auto_id}/run-now").json()
    task_id = triggered["task_id"]
    run_id = triggered["run"]["id"]

    now_val = automations.now_ms()
    five_min_ago_iso = automations.ms_to_iso(now_val - 5 * 60 * 1000)

    def _delete_task_five_min_ago(data):
        removed = data["tasks"].pop(task_id, None)
        data.setdefault("deleted_tasks", {})[task_id] = removed or {"id": task_id}
        data["automation_runs"][run_id]["created_at"] = five_min_ago_iso

    runtime.store.transaction(_delete_task_five_min_ago)
    assert worker.run_once().activity is False
    run_at_5m = next(r for r in runtime.list_automations()["runs"] if r["id"] == run_id)
    assert run_at_5m["status"] == "IN_PROGRESS"

    eleven_min_ago_iso = automations.ms_to_iso(now_val - 11 * 60 * 1000)
    runtime.store.transaction(lambda d: d["automation_runs"][run_id].update(created_at=eleven_min_ago_iso))
    assert worker.run_once().activity is True

    listed = runtime.list_automations()
    run_at_11m = next(r for r in listed["runs"] if r["id"] == run_id)
    assert run_at_11m["status"] == "LOST"
    assert run_at_11m["error"] == "task_removed"
    assert run_at_11m["inbox_summary"] == "对应任务已被删除"
    auto_after = next(i for i in listed["items"] if i["id"] == auto_id)
    assert auto_after["last_run_status"] == "LOST"


def test_t4_never_policy_silences_failed_and_cancelled_runs(tmp_path: Path):
    _app, runtime, _model, client, worker = _make_env(tmp_path)
    auto = client.post(
        "/api/local/agent/automations",
        json={
            "name": "完全静默任务",
            "prompt": "后台静默巡检",
            "repeat": "daily",
            "time": "11:00",
            "notification_policy": "never",
        },
    ).json()["automation"]
    auto_id = auto["id"]

    triggered = client.post(f"/api/local/agent/automations/{auto_id}/run-now").json()
    task_id = triggered["task_id"]

    def _mark_failed(data):
        row = data["tasks"][task_id]
        row["status"] = "failed"
        row["error"] = {"code": "agent_failed", "message": "预期静默失败"}

    runtime.store.transaction(_mark_failed)
    assert worker.run_once().activity is True

    settled_task = runtime.get(task_id)
    assert settled_task.get("automation_notification") is None
    assert settled_task["latest_turn_is_silent_heartbeat"] is True
    runs = {r["id"]: r for r in runtime.list_automations()["runs"]}
    assert runs[triggered["run"]["id"]]["status"] == "FAILED"


def test_t5_stalled_waiting_input_run_becomes_needs_attention_and_settles_later(tmp_path: Path):
    _app, runtime, _model, client, worker = _make_env(tmp_path)
    auto = client.post(
        "/api/local/agent/automations",
        json={"name": "挂起超时检测", "prompt": "执行统计", "repeat": "daily", "time": "12:00"},
    ).json()["automation"]
    auto_id = auto["id"]

    triggered = client.post(f"/api/local/agent/automations/{auto_id}/run-now").json()
    task_id = triggered["task_id"]
    run_id = triggered["run"]["id"]

    now_val = automations.now_ms()
    ten_min_ago_iso = automations.ms_to_iso(now_val - 10 * 60 * 1000)

    def _stall_waiting_input(data):
        row = data["tasks"][task_id]
        row["status"] = "waiting_input"
        row["pending_input"] = {"kind": "question", "question": "请选择导出月份"}
        data["automation_runs"][run_id]["created_at"] = ten_min_ago_iso

    runtime.store.transaction(_stall_waiting_input)
    assert worker.run_once().activity is False
    assert next(r for r in runtime.list_automations()["runs"] if r["id"] == run_id)["status"] == "IN_PROGRESS"

    thirty_one_min_ago_iso = automations.ms_to_iso(now_val - 31 * 60 * 1000)
    runtime.store.transaction(lambda d: d["automation_runs"][run_id].update(created_at=thirty_one_min_ago_iso))
    assert worker.run_once().activity is True

    stalled_task = runtime.get(task_id)
    notif = stalled_task.get("automation_notification")
    assert isinstance(notif, dict)
    assert "定时任务需要处理" in notif["title"] or "定时任务需要处理" in notif["message"]
    run_attn = next(r for r in runtime.list_automations()["runs"] if r["id"] == run_id)
    assert run_attn["status"] == "NEEDS_ATTENTION"

    # When the task later finishes, terminal settlement overrides NEEDS_ATTENTION
    def _finish_task(data):
        row = data["tasks"][task_id]
        row["status"] = "completed"
        row["pending_input"] = None
        row["messages"].append({"role": "assistant", "content": "导出已完成。"})

    runtime.store.transaction(_finish_task)
    assert worker.run_once().activity is True
    run_done = next(r for r in runtime.list_automations()["runs"] if r["id"] == run_id)
    assert run_done["status"] == "ACCEPTED"


def test_t6_missed_slots_coalesce_to_single_run_with_missed_runs_count(tmp_path: Path):
    _app, runtime, _model, client, worker = _make_env(tmp_path)
    auto = client.post(
        "/api/local/agent/automations",
        json={
            "name": "跨时段合并补跑",
            "prompt": "每小时同步",
            "repeat": "hourly",
            "interval_hours": 1,
            "time": "00:00",
        },
    ).json()["automation"]
    auto_id = auto["id"]

    # Simulate shutdown spanning 3 hourly periods after the originally scheduled next_run_at
    past_due_ms = automations.now_ms() - (3 * 3_600_000 + 120_000)
    runtime.store.transaction(lambda d: d["automations"][auto_id].update(next_run_at=past_due_ms))

    assert worker.run_once().activity is True
    # Second tick immediately afterward must NOT trigger extra catch-up runs
    assert worker.run_once().activity is False

    listed = runtime.list_automations()
    item = next(i for i in listed["items"] if i["id"] == auto_id)
    assert item["run_count"] == 1
    assert item["missed_runs"] == 3

    runs_for_auto = [r for r in listed["runs"] if r["automation_id"] == auto_id]
    assert len(runs_for_auto) == 1
    assert runs_for_auto[0]["missed_runs"] == 3


def test_t7_cron_overlap_records_skipped_overlap_without_spawning_task(tmp_path: Path):
    _app, runtime, _model, client, worker = _make_env(tmp_path)
    auto = client.post(
        "/api/local/agent/automations",
        json={
            "name": "重叠保护任务",
            "prompt": "执行耗时同步",
            "repeat": "minutely",
            "interval_minutes": 5,
        },
    ).json()["automation"]
    auto_id = auto["id"]

    # Trigger Round 1 via scheduler and mark its task as running
    now_val = automations.now_ms()
    runtime.store.transaction(lambda d: d["automations"][auto_id].update(next_run_at=now_val - 1000))
    assert worker.run_once().activity is True

    first_task_id = next(i for i in runtime.list_automations()["items"] if i["id"] == auto_id)["last_task_id"]
    tasks_before = set(runtime.store.inspect(lambda d: list((d.get("tasks") or {}).keys())))
    assert first_task_id in tasks_before

    def _keep_running_and_make_due_again(data):
        data["tasks"][first_task_id]["status"] = "running"
        data["automations"][auto_id]["next_run_at"] = automations.now_ms() - 1000

    runtime.store.transaction(_keep_running_and_make_due_again)
    assert worker.run_once().activity is True

    tasks_after = set(runtime.store.inspect(lambda d: list((d.get("tasks") or {}).keys())))
    assert tasks_after == tasks_before

    listed = runtime.list_automations()
    auto_after = next(i for i in listed["items"] if i["id"] == auto_id)
    assert auto_after["next_run_at"] > automations.now_ms()
    assert auto_after["last_run_status"] == "SKIPPED_OVERLAP"

    runs_for_auto = [r for r in listed["runs"] if r["automation_id"] == auto_id]
    statuses = [r["status"] for r in runs_for_auto]
    assert "SKIPPED_OVERLAP" in statuses


def test_t8_once_automation_auto_archives_on_completion_preserving_runs(tmp_path: Path):
    _app, runtime, _model, client, _worker = _make_env(tmp_path)
    # 1) Default once automation: auto_archive defaults to True -> deleted on completion, run record kept
    once_default = client.post(
        "/api/local/agent/automations",
        json={"name": "一次性自动归档", "prompt": "提醒交周报", "repeat": "once", "time": "20:00"},
    ).json()["automation"]
    once_id = once_default["id"]
    assert once_default["auto_archive"] is True
    toml_path = Path(once_default["memory_path"]).parent / "automation.toml"
    assert toml_path.exists()

    fired = client.post(f"/api/local/agent/automations/{once_id}/run-now").json()
    run_id = fired["run"]["id"]
    runtime.run_once()

    listed = runtime.list_automations()
    assert all(i["id"] != once_id for i in listed["items"])
    assert not toml_path.exists()
    preserved_run = next((r for r in listed["runs"] if r["id"] == run_id), None)
    assert preserved_run is not None
    assert preserved_run["status"] == "ACCEPTED"

    # 2) Explicit auto_archive=False on once automation -> stays PAUSED after completion
    once_keep = client.post(
        "/api/local/agent/automations",
        json={
            "name": "一次性保留暂停",
            "prompt": "提醒复盘",
            "repeat": "once",
            "time": "21:00",
            "auto_archive": False,
        },
    ).json()["automation"]
    keep_id = once_keep["id"]
    assert once_keep["auto_archive"] is False
    client.post(f"/api/local/agent/automations/{keep_id}/run-now")
    runtime.run_once()

    listed_after = runtime.list_automations()
    kept_item = next((i for i in listed_after["items"] if i["id"] == keep_id), None)
    assert kept_item is not None
    assert kept_item["status"] == "PAUSED"


def test_t9_fifty_first_run_evicts_oldest_run_for_same_automation(tmp_path: Path):
    _app, runtime, _model, client, _worker = _make_env(tmp_path)
    auto_a = client.post(
        "/api/local/agent/automations",
        json={"name": "高频任务A", "prompt": "检查A", "repeat": "daily", "time": "08:00"},
    ).json()["automation"]["id"]
    auto_b = client.post(
        "/api/local/agent/automations",
        json={"name": "独立任务B", "prompt": "检查B", "repeat": "daily", "time": "08:30"},
    ).json()["automation"]["id"]

    b_run_id = client.post(f"/api/local/agent/automations/{auto_b}/run-now").json()["run"]["id"]
    runtime.run_once()

    a_run_ids: list[str] = []
    base_ms = 1_760_000_000_000
    for idx in range(51):
        def _trigger_and_settle(data, i=idx):
            outcome = automation_worker.trigger_automation_in_tx(
                runtime,
                data,
                auto_a,
                force=True,
                now_epoch_ms=base_ms + i * 60_000,
            )
            rid = outcome["run"]["id"]
            tid = outcome["task_id"]
            data["automation_runs"][rid]["created_at"] = automations.ms_to_iso(base_ms + i * 60_000)
            t_row = data["tasks"][tid]
            t_row["status"] = "completed"
            t_row["messages"].append({"role": "assistant", "content": f"完成第 {i} 次"})
            automations.settle_automation_turn_in_tx(runtime.store, data, t_row)
            return rid

        a_run_ids.append(runtime.store.transaction(_trigger_and_settle))

    remaining_runs = runtime.store.inspect(lambda d: dict(d.get("automation_runs") or {}))
    a_remaining = [rid for rid, r in remaining_runs.items() if r.get("automation_id") == auto_a]
    assert len(a_remaining) == 50
    assert a_run_ids[0] not in remaining_runs
    assert set(a_run_ids[1:]) == set(a_remaining)
    assert b_run_id in remaining_runs


def test_t10_deleting_automation_clears_all_its_run_records(tmp_path: Path):
    _app, runtime, _model, client, _worker = _make_env(tmp_path)
    auto_a = client.post(
        "/api/local/agent/automations",
        json={"name": "待删除自动化A", "prompt": "任务A", "repeat": "daily", "time": "09:00"},
    ).json()["automation"]["id"]
    auto_b = client.post(
        "/api/local/agent/automations",
        json={"name": "保留自动化B", "prompt": "任务B", "repeat": "daily", "time": "10:00"},
    ).json()["automation"]["id"]

    for _ in range(3):
        client.post(f"/api/local/agent/automations/{auto_a}/run-now")
        runtime.run_once()
    b_run_id = client.post(f"/api/local/agent/automations/{auto_b}/run-now").json()["run"]["id"]
    runtime.run_once()

    before_runs = runtime.store.inspect(lambda d: dict(d.get("automation_runs") or {}))
    assert sum(1 for r in before_runs.values() if r.get("automation_id") == auto_a) == 3

    del_res = client.delete(f"/api/local/agent/automations/{auto_a}")
    assert del_res.status_code == 200

    after_runs = runtime.store.inspect(lambda d: dict(d.get("automation_runs") or {}))
    assert sum(1 for r in after_runs.values() if r.get("automation_id") == auto_a) == 0
    assert b_run_id in after_runs
