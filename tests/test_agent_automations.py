from __future__ import annotations

import datetime as dt
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from rynmesh.agent_runtime import automation_worker, automations
from rynmesh.agent_runtime.routes import install_agent
from rynmesh.agent_runtime.store import AgentError
from rynmesh.background_workers import BackgroundWorkerRegistry
from rynmesh.services import peer_box


class _FakeModel:
    supports_destination_binding = True
    supports_output_token_limit = True
    supports_request_deadline = True
    supports_request_id = True
    supports_conversation_key = True

    def __init__(self) -> None:
        self.responses: list[dict[str, Any]] = []

    def complete(self, model_id, messages, specs, cancelled, on_event, **options):
        if self.responses:
            return self.responses.pop(0)
        return {
            "message": {
                "role": "assistant",
                "content": '已完成微信消息发送。\n::inbox-item{title="微信已发送" summary="已向张三发送：早点休息"}',
            },
            "finish_reason": "stop",
            "usage": {"input_tokens": 25, "output_tokens": 18},
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
    peer_id = "peer-local-test"


def _build_test_env(tmp_path: Path):
    app = FastAPI()
    workers = BackgroundWorkerRegistry()
    key = peer_box.load_or_create_messaging_key(tmp_path / "messaging.x25519")
    model = _FakeModel()
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
    return app, runtime, model, client


def test_rrule_schedule_labels_and_next_run_calculation():
    rrule_daily = automations.build_rrule(repeat="daily", time_str="22:00")
    assert rrule_daily == "FREQ=DAILY;BYHOUR=22;BYMINUTE=0;BYSECOND=0"
    labels = automations.format_schedule_labels(rrule_daily)
    assert labels["repeat"] == "daily"
    assert labels["time"] == "22:00"
    assert labels["schedule_label_zh"] == "每天 22:00"
    assert labels["schedule_label_en"] == "Daily at 22:00"

    tz = dt.timezone(dt.timedelta(hours=8))
    anchor_dt = dt.datetime(2026, 10, 20, 21, 0, 0, tzinfo=tz)
    anchor_ms = int(anchor_dt.timestamp() * 1000)
    next_ms = automations.compute_next_run_at(
        rrule_daily,
        after_ms=anchor_ms,
        tz=tz,
        salt="deadbeef",
        automation_id="auto-wechat",
    )
    expected_dt = dt.datetime(2026, 10, 20, 22, 0, 0, tzinfo=tz)
    # Exact clock-time reminders have 0ms jitter
    assert next_ms == int(expected_dt.timestamp() * 1000)

    # Hourly schedules apply deterministic jitter from salt
    rrule_hourly = automations.build_rrule(repeat="hourly", interval_hours=2, time_str="00:15")
    next_hourly_1 = automations.compute_next_run_at(
        rrule_hourly,
        after_ms=anchor_ms,
        tz=tz,
        salt="abcd1234",
        automation_id="auto-hourly",
    )
    next_hourly_2 = automations.compute_next_run_at(
        rrule_hourly,
        after_ms=anchor_ms,
        tz=tz,
        salt="abcd1234",
        automation_id="auto-hourly",
    )
    assert next_hourly_1 == next_hourly_2
    slot_dt = dt.datetime(2026, 10, 20, 21, 15, 0, tzinfo=tz)
    assert int(slot_dt.timestamp() * 1000) <= next_hourly_1 < int(slot_dt.timestamp() * 1000) + 45_000


def test_toml_disk_sync_and_api_crud(tmp_path: Path):
    _app, runtime, _model, client = _build_test_env(tmp_path)

    res = client.post(
        "/api/local/agent/automations",
        json={
            "name": "给张三发微信消息",
            "prompt": "打开微信客户端，搜索张三，发送：早点休息",
            "repeat": "daily",
            "time": "22:00",
            "desktop_access": True,
        },
    )
    assert res.status_code == 200
    created = res.json()["automation"]
    auto_id = created["id"]
    assert created["name"] == "给张三发微信消息"
    assert created["schedule_label_zh"] == "每天 22:00"
    assert created["status"] == "ACTIVE"

    toml_file = Path(created["memory_path"]).parent / "automation.toml"
    mem_file = Path(created["memory_path"])
    assert toml_file.exists()
    assert mem_file.exists()

    # Edit TOML on disk and verify list_automations syncs the change
    toml_text = toml_file.read_text(encoding="utf-8").replace(
        'name = "给张三发微信消息"',
        'name = "给李四发晚安微信"',
    )
    toml_file.write_text(toml_text, encoding="utf-8")

    listed = client.get("/api/local/agent/automations").json()
    assert len(listed["items"]) == 1
    assert listed["items"][0]["name"] == "给李四发晚安微信"

    # Pause via PATCH
    patched = client.patch(
        f"/api/local/agent/automations/{auto_id}",
        json={"status": "PAUSED"},
    ).json()["automation"]
    assert patched["status"] == "PAUSED"
    assert patched["next_run_at"] is None

    # Delete via DELETE
    del_res = client.delete(f"/api/local/agent/automations/{auto_id}")
    assert del_res.status_code == 200
    assert not toml_file.exists()


def test_api_rejects_automation_ids_that_are_not_safe_directory_names(tmp_path: Path):
    _app, runtime, _model, client = _build_test_env(tmp_path)
    for automation_id in (".", "..", "../outside", "..\\outside", "auto/child", "C:outside", "auto.", "CON"):
        response = client.post(
            "/api/local/agent/automations",
            json={"id": automation_id, "name": "Unsafe ID", "prompt": "Check status"},
        )
        assert response.status_code == 400
        assert response.json()["detail"] == "agent_invalid_automation_id"
    assert runtime.list_automations()["items"] == []
    assert not (runtime.store.root / "automation.toml").exists()
    assert not (runtime.store.root / "memory.md").exists()


def test_deleting_legacy_invalid_automation_cannot_remove_agent_root(tmp_path: Path):
    store = SimpleNamespace(root=tmp_path / "agent")
    store.root.mkdir()
    sentinel = store.root / "sessions.json"
    sentinel.write_text("preserve agent state", encoding="utf-8")
    data = {"automations": {"..": {"id": ".."}}}

    with pytest.raises(AgentError, match="agent_invalid_automation_id"):
        automations.delete_automation_in_tx(store, data, "..")

    assert sentinel.read_text(encoding="utf-8") == "preserve agent state"
    assert ".." in data["automations"]


@pytest.mark.parametrize("linked_root", [False, True])
def test_automation_paths_reject_directory_links_outside_agent_root(tmp_path: Path, linked_root: bool):
    store = SimpleNamespace(root=tmp_path / "agent")
    store.root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    sentinel = outside / "memory.md"
    sentinel.write_text("preserve external files", encoding="utf-8")
    root = store.root / "automations"
    if linked_root:
        link = root
    else:
        root.mkdir()
        link = root / "auto-linked"
    try:
        link.symlink_to(outside, target_is_directory=True)
    except OSError:
        pytest.skip("Directory symlinks are unavailable on this host")

    with pytest.raises(AgentError, match="agent_invalid_automation_path"):
        automations.delete_automation_files(store, "auto-linked")

    assert sentinel.read_text(encoding="utf-8") == "preserve external files"


def test_chat_tool_creates_automation_and_scheduler_executes(tmp_path: Path):
    app, runtime, model, client = _build_test_env(tmp_path)

    # 1. Simulate user asking in chat to schedule a 22:00 WeChat task
    model.responses = [
        {
            "message": {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {
                        "id": "call_auto_1",
                        "type": "function",
                        "function": {
                            "name": "ryn_automation_update",
                            "arguments": {
                                "action": "create",
                                "name": "给张三发微信消息",
                                "prompt": "打开桌面微信客户端，搜索联系人「张三」，发送消息：「早点休息」",
                                "repeat": "daily",
                                "time": "22:00",
                                "desktop_access": True,
                            },
                        },
                    }
                ],
            },
            "finish_reason": "tool_calls",
            "usage": {"input_tokens": 30, "output_tokens": 25},
        },
        {
            "message": {
                "role": "assistant",
                "content": "好的，已经为你设置好每天 22:00 给张三发微信消息的定时任务。",
            },
            "finish_reason": "stop",
            "usage": {"input_tokens": 40, "output_tokens": 20},
        },
    ]

    task_data = runtime.create({"message": "每天晚上十点，打开微信给张三发消息：早点休息"})
    # Step 1: model emits ryn_automation_update tool call
    runtime.run_once()
    # Step 2: internal tool executes
    runtime.run_once()
    # Step 3: model finishes turn
    runtime.run_once()

    finished_task = runtime.get(task_data["id"])
    assert finished_task["status"] == "completed"
    tool_events = [
        ev for ev in finished_task["events"]
        if ev.get("type") == "tool_result" and ev.get("tool") == "ryn_automation_update"
    ]
    assert len(tool_events) == 1
    auto_result = tool_events[0]["result"]
    assert auto_result["ok"] is True
    assert auto_result["name"] == "给张三发微信消息"
    assert auto_result["schedule_label_zh"] == "每天 22:00"
    auto_id = auto_result["id"]

    # 2. Force the automation to be due now and run AutomationSchedulerWorker
    def _make_due(data):
        data["automations"][auto_id]["next_run_at"] = automations.now_ms() - 1000

    runtime.store.transaction(_make_due)
    worker: automation_worker.AutomationSchedulerWorker = app.state.agent_automation_worker
    tick_res = worker.run_once()
    assert tick_res.activity is True

    state_after_claim = runtime.list_automations()
    auto_item = state_after_claim["items"][0]
    spawned_task_id = auto_item["last_task_id"]
    assert spawned_task_id
    assert auto_item["run_count"] == 1

    # 3. Execute the spawned automation task and verify ::inbox-item settlement + memory.md note
    runtime.run_once()
    spawned_task = runtime.get(spawned_task_id)
    assert spawned_task["status"] == "completed"
    assert spawned_task["source"] == "automation"
    assert spawned_task["automation_id"] == auto_id

    final_state = runtime.list_automations()
    assert final_state["items"][0]["last_run_status"] == "completed"
    assert "已向张三发送：早点休息" in final_state["items"][0]["last_run_summary"]
    assert final_state["runs"][0]["status"] == "ACCEPTED"
    assert final_state["runs"][0]["inbox_title"] == "微信已发送"

    mem_content = Path(final_state["items"][0]["memory_path"]).read_text(encoding="utf-8")
    assert "已向张三发送：早点休息" in mem_content


def test_heartbeat_deferral_and_once_auto_pause(tmp_path: Path):
    app, runtime, _model, client = _build_test_env(tmp_path)

    # Create a single-shot (once) automation and trigger via run-now
    once_res = client.post(
        "/api/local/agent/automations",
        json={
            "name": "单次提醒",
            "prompt": "提醒我开会",
            "repeat": "once",
            "time": "22:00",
        },
    ).json()["automation"]
    assert once_res["repeat"] == "once"
    assert once_res["status"] == "ACTIVE"

    run_now_res = client.post(f"/api/local/agent/automations/{once_res['id']}/run-now").json()
    assert run_now_res["triggered"] is True
    # Single-shot automation automatically transitions to PAUSED after firing once
    assert run_now_res["automation"]["status"] == "PAUSED"
    assert run_now_res["automation"]["next_run_at"] is None

    # Create a heartbeat automation targeting a busy task
    busy_task = runtime.create({"message": "长任务"})
    hb_res = client.post(
        "/api/local/agent/automations",
        json={
            "name": "会话心跳检查",
            "prompt": "检查进度",
            "repeat": "hourly",
            "interval_hours": 1,
            "kind": "heartbeat",
            "target_task_id": busy_task["id"],
        },
    ).json()["automation"]

    due_ts = automations.now_ms() - 1000
    runtime.store.transaction(lambda d: d["automations"][hb_res["id"]].update(next_run_at=due_ts))
    worker: automation_worker.AutomationSchedulerWorker = app.state.agent_automation_worker
    worker.run_once()

    # Because busy_task is still queued, heartbeat defers next_run_at by ~60s instead of spawning
    deferred_item = next(i for i in runtime.list_automations()["items"] if i["id"] == hb_res["id"])
    assert deferred_item["run_count"] == 0
    assert deferred_item["next_run_at"] >= due_ts + 55_000


def test_codex_heartbeat_notify_vs_dont_notify_and_auto_pause_archived(tmp_path: Path):
    app, runtime, model, client = _build_test_env(tmp_path)

    # 1. Start a conversation and finish the initial turn
    model.responses = [
        {
            "message": {"role": "assistant", "content": "好的，我会每隔5分钟帮你检查构建状态。"},
            "finish_reason": "stop",
            "usage": {"input_tokens": 20, "output_tokens": 15},
        }
    ]
    base_task = runtime.create({"message": "帮我监控构建状态，好了提醒我"})
    runtime.run_once()
    assert runtime.get(base_task["id"])["status"] == "completed"

    # 2. Create a minutely heartbeat automation attached to base_task["id"]
    hb_auto = client.post(
        "/api/local/agent/automations",
        json={
            "name": "构建状态监控",
            "prompt": "检查构建是否完成，如果没完成请使用 DONT_NOTIFY 保持静默，完成了则 NOTIFY 提醒我。",
            "repeat": "minutely",
            "interval_minutes": 5,
            "kind": "heartbeat",
            "target_task_id": base_task["id"],
        },
    ).json()["automation"]
    auto_id = hb_auto["id"]
    assert hb_auto["kind"] == "heartbeat"
    assert hb_auto["repeat"] == "minutely"
    assert hb_auto["interval_minutes"] == 5
    assert "FREQ=MINUTELY;INTERVAL=5" in hb_auto["rrule"]

    # 3. First heartbeat run returns DONT_NOTIFY -> should be silent, strip <heartbeat>, and mark ui.kind == "silent_heartbeat"
    model.responses = [
        {
            "message": {
                "role": "assistant",
                "content": (
                    "构建仍在进行中（45%）。\n"
                    "<heartbeat>\n"
                    f"<automation_id>{auto_id}</automation_id>\n"
                    "<decision>DONT_NOTIFY</decision>\n"
                    "<message>Still building</message>\n"
                    "</heartbeat>"
                ),
            },
            "finish_reason": "stop",
            "usage": {"input_tokens": 30, "output_tokens": 25},
        }
    ]
    runtime.store.transaction(lambda d: d["automations"][auto_id].update(next_run_at=automations.now_ms() - 1000))
    worker: automation_worker.AutomationSchedulerWorker = app.state.agent_automation_worker
    worker.run_once()
    runtime.run_once()

    after_silent = runtime.get(base_task["id"])
    assert after_silent["status"] == "completed"
    assert after_silent["latest_turn_is_silent_heartbeat"] is True
    assert "<heartbeat>" not in after_silent["result"]
    assert after_silent["messages"][-1]["ui"]["kind"] == "silent_heartbeat"
    assert after_silent["messages"][-2]["ui"]["kind"] == "silent_heartbeat"
    runs_after_silent = runtime.list_automations()["runs"]
    assert runs_after_silent[0]["decision"] == "DONT_NOTIFY"
    assert runs_after_silent[0]["status"] == "ARCHIVED"

    # 4. Second heartbeat run returns NOTIFY -> strips <heartbeat>, sets latest_turn_is_silent_heartbeat=False, populates automation_notification
    model.responses = [
        {
            "message": {
                "role": "assistant",
                "content": (
                    "🎉 构建已经全部通过！\n"
                    "<heartbeat>\n"
                    f"<automation_id>{auto_id}</automation_id>\n"
                    "<decision>NOTIFY</decision>\n"
                    "<message>构建已成功完成，所有测试通过</message>\n"
                    "</heartbeat>"
                ),
            },
            "finish_reason": "stop",
            "usage": {"input_tokens": 35, "output_tokens": 25},
        }
    ]
    runtime.store.transaction(lambda d: d["automations"][auto_id].update(next_run_at=automations.now_ms() - 1000))
    worker.run_once()
    runtime.run_once()

    after_notify = runtime.get(base_task["id"])
    assert after_notify["status"] == "completed"
    assert after_notify["latest_turn_is_silent_heartbeat"] is False
    assert "<heartbeat>" not in after_notify["result"]
    assert "构建已经全部通过" in after_notify["result"]
    notif = after_notify.get("automation_notification")
    assert isinstance(notif, dict)
    assert notif["decision"] == "NOTIFY"
    assert notif["task_id"] == base_task["id"]
    assert "构建已成功完成" in notif["message"]

    # 5. If the target conversation is archived or deleted, next scheduled tick automatically pauses the heartbeat automation
    def _archive_target(d):
        d.setdefault("archives", {})[base_task["id"]] = d["tasks"].pop(base_task["id"])
        d["automations"][auto_id]["next_run_at"] = automations.now_ms() - 1000

    runtime.store.transaction(_archive_target)
    worker.run_once()
    paused_auto = next(i for i in runtime.list_automations()["items"] if i["id"] == auto_id)
    assert paused_auto["status"] == "PAUSED"
    assert paused_auto["next_run_at"] is None
