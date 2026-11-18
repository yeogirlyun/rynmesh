"""S10-I12: scheduled turns fail interactive operations and keep working."""
import copy
import json

import pytest

from test_agent_automations import _build_test_env
from test_agent_runtime import Tools, assistant
from rynmesh.agent_runtime import automations
from rynmesh.agent_runtime.execution_policy import decide


class ScheduledTools(Tools):
    explicit = False
    desktop = False

    def metadata(self, name):
        meta = super().metadata(name)
        if name == "share_service":
            meta.update(requires_explicit_approval=self.explicit, computer_access=self.desktop)
        return meta


def env(tmp_path, *, global_mode="automatic", explicit=False, desktop=False):
    app, runtime, model, client = _build_test_env(tmp_path)
    runtime.update_settings({"permission_mode": global_mode, "max_steps": 100})
    tools = ScheduledTools()
    tools.explicit, tools.desktop = explicit, desktop
    runtime.tools = tools
    seen = []
    original = model.complete
    def complete(*args, **kwargs):
        seen.append(copy.deepcopy(args[1]))
        return original(*args, **kwargs)
    model.complete = complete
    return runtime, model, tools, client, app.state.agent_automation_worker, seen


def trigger(runtime, client, *, kind="cron", mode=None, target=None, policy="failed_runs_only"):
    payload = {"name": "Scheduled test", "prompt": "Do what is permitted and report blockers",
               "kind": kind, "repeat": "daily", "time": "09:00", "notification_policy": policy}
    if target:
        payload["target_task_id"] = target
    response = client.post("/api/local/agent/automations", json=payload)
    assert response.status_code == 200, response.text
    item = response.json()["automation"]
    if mode is not None:
        # S10-I15's persisted per-automation field. Exercise the real worker
        # consuming it; do not replace the worker or mutate its spawned row.
        runtime.store.transaction(lambda data: data["automations"][item["id"]].update(permission_mode=mode))
    fired = client.post(f"/api/local/agent/automations/{item['id']}/run-now")
    assert fired.status_code == 200, fired.text
    result = fired.json()
    assert result["triggered"] is True
    return result["task_id"], result["run"]["id"], item["id"]


def finish(runtime, task_id, count=12):
    for _ in range(count):
        if runtime.get(task_id)["status"] not in {"queued", "running"}:
            break
        runtime.run_once()
    return runtime.get(task_id)


def tool_result(runtime, task_id, tool="share_service"):
    events = runtime.get(task_id)["events"]
    return next(e["result"] for e in reversed(events) if e["type"] == "tool_result" and e.get("tool") == tool)


def silent_reply(auto_id):
    return assistant("Finished what I could; the remaining operation needs approval.\n"
                     f"<heartbeat><automation_id>{auto_id}</automation_id><decision>DONT_NOTIFY</decision>"
                     "<message>No changes to report</message></heartbeat>")


def test_t1_explicit_approval_is_paired_failure_then_model_completes(tmp_path):
    runtime, model, tools, client, worker, seen = env(tmp_path, explicit=True)
    task_id, run_id, auto_id = trigger(runtime, client, mode="confirm_changes")
    model.responses = [assistant(name="share_service", args={"service_id": "example"}), silent_reply(auto_id)]
    done = finish(runtime, task_id)
    assert done["status"] == "completed"
    assert tools.calls == [] and tools.shared is False
    result = tool_result(runtime, task_id)
    assert result["error"] == "agent_unattended_approval_blocked"
    assert result["not_executed"] is True and result["no_mutation"] is True
    assert "No one is watching" in result["correction"]
    assert any(m.get("role") == "tool" and "agent_unattended_approval_blocked" in m.get("content", "") for m in seen[-1])
    assert not done["pending_approval"]


def test_t2_scheduled_input_is_rejected_and_questions_go_to_final_reply(tmp_path):
    runtime, model, tools, client, worker, seen = env(tmp_path)
    task_id, run_id, auto_id = trigger(runtime, client)
    model.responses = [assistant(name="ryn_request_input", args={"goal_revision": 1, "kind": "information",
        "prompt": "Which month?", "fields": [{"id": "month", "label": "Month", "type": "text", "required": True}]}),
        assistant("Question for the owner: which month should be checked?")]
    done = finish(runtime, task_id)
    assert done["status"] == "completed" and not done["pending_input"]
    result = tool_result(runtime, task_id, "ryn_request_input")
    assert result["error"] == "agent_unattended_input_blocked"
    assert result["not_executed"] and result["no_mutation"]
    assert "final response" in result["correction"]


@pytest.mark.parametrize("before_completion", [False, True])
def test_t3_owner_followup_restores_normal_interactive_approval(tmp_path, before_completion):
    runtime, model, tools, client, worker, seen = env(tmp_path, explicit=True)
    task_id, run_id, auto_id = trigger(runtime, client, mode="confirm_changes")
    model.responses = [assistant(name="share_service", args={"service_id": "first"}), silent_reply(auto_id)]
    if before_completion:
        runtime.run_once()
        runtime.run_once()
        assert runtime.get(task_id)["status"] == "queued"
        assert runtime.store.read()["tasks"][task_id]["unattended_blocked"]
    else:
        assert finish(runtime, task_id)["status"] == "completed"
    current = runtime.get(task_id)
    runtime.add_message(task_id, {"message": "Now share the second service", "expected_revision": current["revision"],
                                 **({"intent": "steer", "expected_goal_revision": current["goal"]["revision"]} if before_completion else {})})
    model.responses = [assistant(name="share_service", args={"service_id": "second"})]
    done = finish(runtime, task_id)
    assert done["status"] == "waiting_approval"
    assert done["pending_approval"]["arguments"] == {"service_id": "second"}
    raw = runtime.store.read()["tasks"][task_id]
    assert raw["automation_run_id"] is None
    assert not raw.get("unattended_blocked")


def test_t4_global_confirmation_does_not_block_automatic_cron_or_explicit_metadata(tmp_path):
    runtime, model, tools, client, worker, seen = env(tmp_path, global_mode="confirm_changes")  # explicit-approval tools stay blocked in cron (card T1)
    task_id, run_id, auto_id = trigger(runtime, client)
    model.responses = [assistant(name="share_service", args={"service_id": "allowed"}), assistant("Shared")]
    assert finish(runtime, task_id)["status"] == "completed"
    assert len(tools.calls) == 1 and tools.shared is True
    assert runtime.get(task_id)["permission_mode"] == "automatic"


def test_t5_global_read_only_still_blocks_cron_writes(tmp_path):
    runtime, model, tools, client, worker, seen = env(tmp_path, global_mode="read_only")
    task_id, run_id, auto_id = trigger(runtime, client)
    model.responses = [assistant(name="share_service", args={"service_id": "blocked"}), assistant("Read-only blocked this write")]
    assert finish(runtime, task_id)["status"] == "completed"
    assert tools.calls == [] and tools.shared is False
    assert tool_result(runtime, task_id)["error"] == "agent_permission_denied"


def test_t6_cron_uses_persisted_confirmation_and_notifies(tmp_path):
    runtime, model, tools, client, worker, seen = env(tmp_path)
    task_id, run_id, auto_id = trigger(runtime, client, mode="confirm_changes")
    assert runtime.get(task_id)["permission_mode"] == "confirm_changes"
    model.responses = [assistant(name="share_service", args={"service_id": "needs-approval"}), silent_reply(auto_id)]
    done = finish(runtime, task_id)
    assert done["status"] == "completed" and tools.calls == []
    worker.run_once()
    notice = runtime.get(task_id)["automation_notification"]
    assert notice["title"] == "定时任务需要处理" and "share_service" in notice["message"]
    run = runtime.store.read()["automation_runs"][run_id]
    assert run["blocked_operations"] == [{"tool": "share_service", "reason": "confirm_changes"}]


@pytest.mark.parametrize("policy", ["all_runs", "failed_runs_only", "never"])
def test_t7_workspace_heartbeat_blocks_records_and_overrides_silent_model(tmp_path, policy):
    runtime, model, tools, client, worker, seen = env(tmp_path, global_mode="workspace")
    model.responses = [assistant("Ready for heartbeat")]
    target = runtime.create({"message": "Monitor this task"})
    assert finish(runtime, target["id"])["status"] == "completed"
    task_id, run_id, auto_id = trigger(runtime, client, kind="heartbeat", target=target["id"], policy=policy)
    raw = runtime.store.read()["tasks"][task_id]
    assert raw["permission_mode"] == "workspace"
    instructions = automations.automation_developer_instructions(runtime.store, raw)
    assert "directly without asking for confirmation" not in instructions
    assert "requires approval" in instructions
    model.responses = [assistant(name="share_service", args={"service_id": "heartbeat"}), silent_reply(auto_id)]
    done = finish(runtime, task_id)
    assert done["status"] == "completed" and tools.calls == []
    worker.run_once()
    data = runtime.store.read()
    assert data["automation_runs"][run_id]["blocked_operations"] == [{"tool": "share_service", "reason": "external_change"}]
    assert data["automation_runs"][run_id]["decision"] == ("" if policy == "never" else "NOTIFY")
    assert data["automation_runs"][run_id]["inbox_title"] == "定时任务需要处理"
    assert not data["tasks"][task_id].get("unattended_blocked")
    notice = runtime.get(task_id).get("automation_notification")
    if policy == "never":
        assert notice is None
        assert runtime.get(task_id)["latest_turn_is_silent_heartbeat"] is True
    else:
        assert notice["decision"] == "NOTIFY" and notice["title"] == "定时任务需要处理"
        assert "share_service" in notice["message"]
        assert runtime.get(task_id)["latest_turn_is_silent_heartbeat"] is False


@pytest.mark.parametrize("scheduled,kind,global_mode,mode,expected", [
    (True, "cron", "confirm_changes", "automatic", "execute"),
    (True, "cron", "read_only", "automatic", "deny"),
    (True, "cron", "automatic", "confirm_changes", "ask"),
    (True, "heartbeat", "automatic", "workspace", "ask"),
    (False, "cron", "confirm_changes", "automatic", "execute"),
    (False, "cron", "automatic", "confirm_changes", "ask"),
])
def test_t8_policy_keeps_task_owned_permissions_and_cron_read_only_guard(scheduled, kind, global_mode, mode, expected):
    row = {"permission_mode": mode, "automation_kind": kind, "approved": None,
           "automation_run_id": "scheduled-run" if scheduled else None}
    decision = decide({"name": "share_service", "arguments": {"service_id": "example"}}, {"effect": "write"},
                      row, {"permission_mode": global_mode})
    assert decision.action == expected


def test_recorded_blocked_operations_are_bounded_and_persist_before_completion(tmp_path):
    runtime, model, tools, client, worker, seen = env(tmp_path)
    task_id, run_id, auto_id = trigger(runtime, client, mode="confirm_changes")
    prior = [{"tool": "earlier", "reason": "confirm_changes"}] * 19
    runtime.store.transaction(lambda data: data["tasks"][task_id].update(unattended_blocked=prior))
    model.responses = [assistant(name="share_service", args={"service_id": "twenty"}),
                       assistant(name="share_service", args={"service_id": "twenty-one"}), assistant("Blocked writes reported")]
    for _ in range(4):
        runtime.run_once()
    raw = runtime.store.read()["tasks"][task_id]
    assert raw["status"] == "queued"
    assert len(raw["unattended_blocked"]) == 20
    assert raw["unattended_blocked"][-1] == {"tool": "share_service", "reason": "confirm_changes"}
    assert tools.calls == []


def test_desktop_without_grant_is_reported_without_waiting(tmp_path):
    runtime, model, tools, client, worker, seen = env(tmp_path, desktop=True)
    task_id, run_id, auto_id = trigger(runtime, client, mode="confirm_changes")
    runtime.update_settings({"desktop_access": False})
    model.responses = [assistant(name="share_service", args={"service_id": "desktop"}), assistant("Desktop approval is needed")]
    done = finish(runtime, task_id)
    assert done["status"] == "completed" and tools.calls == []
    assert tool_result(runtime, task_id)["error"] == "agent_unattended_approval_blocked"
    worker.run_once()
    assert runtime.store.read()["automation_runs"][run_id]["blocked_operations"] == [{"tool": "share_service", "reason": "desktop_access"}]
