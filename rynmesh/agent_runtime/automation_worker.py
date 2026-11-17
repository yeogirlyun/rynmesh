"""Background scheduler worker for Ryn Agent Scheduled Tasks (Automations)."""
from __future__ import annotations

import copy
import secrets
import threading
import time
import uuid
from typing import Any

from ..background_workers import WorkerRunResult
from . import automations, budget, control, lifecycle
from .data_policy import merge_state
from .memory import invalidate_task_policy_in_tx
from .prompt import PROMPT_VERSION, select_base_instructions
from .protocol import event, now
from .state import initialize, owner_update
from .store import DEFAULT_SETTINGS, AgentError
from .web_search import restrict_web_policy

AUTOMATION_WORKER_NAME = "ryn-agent-automations"
MAX_CLAIMS_PER_TICK = 3


def _resolve_automation_model(runtime: Any, data: dict[str, Any]) -> tuple[str, dict[str, Any] | None]:
    settings = {**DEFAULT_SETTINGS, **(data.get("settings") or {})}
    model_id = str(settings.get("model_id") or "").strip()
    selected = None
    if model_id:
        try:
            selected = runtime._model_metadata(model_id)
        except Exception:
            selected = None

    if not model_id or selected is None:
        # Check most recent task in data["tasks"]
        tasks = list((data.get("tasks") or {}).values())
        for t in reversed(tasks):
            if isinstance(t, dict) and t.get("model_id"):
                cand_id = str(t["model_id"])
                try:
                    cand_meta = runtime._model_metadata(cand_id)
                except Exception:
                    cand_meta = None
                if cand_meta is not None:
                    model_id = cand_id
                    selected = cand_meta
                    break
                elif not model_id:
                    model_id = cand_id

    if not model_id:
        try:
            for m in runtime.models():
                if isinstance(m, dict) and m.get("available") and m.get("agent_supported"):
                    model_id = str(m.get("id") or "")
                    selected = m
                    break
        except Exception:
            pass

    if not model_id:
        model_id = "default"

    destination = None
    if selected is not None:
        try:
            peer_dest = selected.get("source") == "peer"
            destination = (
                runtime._bound_destination(selected)
                if peer_dest
                else runtime._destination(selected)
            )
        except Exception:
            destination = None
    return model_id, destination


def _find_active_cron_task_id(
    data: dict[str, Any],
    automation_id: str,
    item: dict[str, Any],
) -> str | None:
    tasks = data.get("tasks") or {}
    last_tid = str(item.get("last_task_id") or "").strip()
    if last_tid and isinstance(tasks.get(last_tid), dict):
        t_row = tasks[last_tid]
        if t_row.get("status") in {"queued", "running"} or bool(t_row.get("in_flight")):
            return last_tid
    for run in (data.get("automation_runs") or {}).values():
        if not isinstance(run, dict) or str(run.get("automation_id") or "") != automation_id:
            continue
        if run.get("status") != "IN_PROGRESS":
            continue
        cand_tid = str(run.get("task_id") or "").strip()
        if cand_tid and isinstance(tasks.get(cand_tid), dict):
            t_row = tasks[cand_tid]
            if t_row.get("status") in {"queued", "running"} or bool(t_row.get("in_flight")):
                return cand_tid
    return None


_SKIP_REASON_SUMMARIES = {
    "owner_paused": "目标对话已由主人暂停，跳过本次心跳",
    "interrupted": "目标对话处于中断状态，跳过本次心跳",
    "outcome_unknown": "目标对话存在未确认结果的写操作，跳过本次心跳",
    "fork_pending": "分叉对话正在等待主人指令，跳过本次心跳",
}


def _advance_schedule(
    item: dict[str, Any],
    current_ms: int,
    *,
    store: Any = None,
    run_count: int | None = None,
    data: dict[str, Any] | None = None,
) -> int | None:
    salt = automations.ensure_jitter_salt(store, data) if store is not None else ""
    eff_run_count = int(run_count if run_count is not None else (item.get("run_count") or 0))
    next_ms = automations.compute_next_run_at(
        str(item.get("rrule") or ""),
        after_ms=current_ms,
        salt=salt,
        automation_id=str(item.get("id") or ""),
        run_count=eff_run_count,
    )
    if next_ms is None:
        item["status"] = "PAUSED"
        item["next_run_at"] = None
        item["next_run_at_iso"] = ""
    elif str(item.get("status") or "ACTIVE") == "ACTIVE":
        item["next_run_at"] = next_ms
        item["next_run_at_iso"] = automations.ms_to_iso(next_ms)
    return next_ms


def _resolve_heartbeat_target(
    store: Any,
    data: dict[str, Any],
    initial_task_id: str,
) -> tuple[str, dict[str, Any] | None]:
    tasks = data.get("tasks") or {}
    archives = data.get("archives") or {}
    deleted_tasks = data.get("deleted_tasks") or {}
    curr_id = initial_task_id
    visited: set[str] = set()
    while curr_id and curr_id not in visited and len(visited) < 64:
        visited.add(curr_id)
        try:
            curr_row = lifecycle.find(store, data, curr_id)
        except AgentError:
            return curr_id, None
        next_id = str(curr_row.get("continued_to") or "").strip()
        if next_id:
            curr_id = next_id
            continue
        if curr_id in archives or curr_id in deleted_tasks or curr_id not in tasks:
            return curr_id, None
        row = tasks.get(curr_id)
        return (curr_id, row) if isinstance(row, dict) else (curr_id, None)
    return curr_id, None


def trigger_automation_in_tx(
    runtime: Any,
    data: dict[str, Any],
    automation_id: str,
    *,
    force: bool = False,
    now_epoch_ms: int | None = None,
    missed_runs: int | None = None,
) -> dict[str, Any]:
    automations.ensure_automation_state(data)
    target_id = automations._validated_automation_id(automation_id)
    item = data["automations"].get(target_id)
    if not isinstance(item, dict):
        raise AgentError("agent_automation_not_found")

    current_ms = int(now_epoch_ms if now_epoch_ms is not None else automations.now_ms())
    if missed_runs is not None:
        effective_missed = max(0, int(missed_runs))
    elif not force and isinstance(item.get("next_run_at"), (int, float)):
        effective_missed = automations.compute_missed_runs(
            str(item.get("rrule") or ""),
            item.get("next_run_at"),
            current_ms,
        )
    else:
        effective_missed = 0
    run_id = f"arun-{current_ms}-{secrets.token_hex(3)}"
    kind = str(item.get("kind") or "cron").strip().lower()
    prompt_text = str(item.get("prompt") or item.get("name") or "Execute scheduled automation.").strip()
    title_text = f"⏰ {str(item.get('name') or '定时任务').strip()}"[:150]

    settings = {**DEFAULT_SETTINGS, **(data.get("settings") or {})}
    target_task_id = str(item.get("target_task_id") or "").strip()
    tasks = data.get("tasks") or {}
    archives = data.get("archives") or {}
    deleted_tasks = data.get("deleted_tasks") or {}
    now_str = now()

    target_row: dict[str, Any] | None = None
    target_updated = False
    if kind == "heartbeat" and target_task_id:
        resolved_tid, resolved_row = _resolve_heartbeat_target(runtime.store, data, target_task_id)
        if resolved_tid != target_task_id:
            target_task_id = resolved_tid
            item["target_task_id"] = target_task_id
            target_updated = True
        target_row = resolved_row
        if target_row is None:
            if item.get("dedicated_chat"):
                target_task_id = ""
                item["target_task_id"] = ""
            elif not force:
                item["status"] = "PAUSED"
                item["next_run_at"] = None
                item["next_run_at_iso"] = ""
                item["updated_at"] = now_str
                automations.try_write_automation_files(runtime.store, item)
                return {
                    "triggered": False,
                    "deferred": False,
                    "paused": True,
                    "automation": automations.public_automation(runtime.store, item),
                }
    elif kind == "heartbeat":
        last_tid = str(item.get("last_task_id") or "").strip()
        if (
            last_tid
            and isinstance(tasks.get(last_tid), dict)
            and last_tid not in archives
            and last_tid not in deleted_tasks
        ):
            target_task_id = last_tid
            item["target_task_id"] = last_tid
            item["dedicated_chat"] = True
            target_updated = True
            target_row = tasks[last_tid]

    if kind == "heartbeat" and target_task_id and isinstance(target_row, dict):
        is_ambiguous = control.ambiguous(target_row)
        if force and is_ambiguous:
            raise AgentError("agent_operation_unresolved")
        reconciling = control.reconciliation_active(target_row)
        is_busy = (
            reconciling
            or target_row.get("status") in {"queued", "running", "stopping", "waiting_approval", "waiting_input"}
            or bool(target_row.get("pending_approval"))
            or bool(target_row.get("pending_input"))
            or (bool(target_row.get("pending_control")) and not is_ambiguous)
            or bool(target_row.get("pending_user_messages"))
            or (bool(target_row.get("in_flight")) and target_row.get("status") != "interrupted" and not is_ambiguous)
        )
        if is_busy and not force:
            defer_ms = current_ms + 60_000
            item["next_run_at"] = defer_ms
            item["next_run_at_iso"] = automations.ms_to_iso(defer_ms)
            if target_updated:
                item["updated_at"] = now_str
                automations.try_write_automation_files(runtime.store, item)
            return {
                "triggered": False,
                "deferred": True,
                "automation": automations.public_automation(runtime.store, item),
            }

        skip_reason: str | None = None
        if not force:
            if is_ambiguous:
                skip_reason = "outcome_unknown"
            elif bool((target_row.get("fork") or {}).get("awaiting_instruction")):
                skip_reason = "fork_pending"
            elif target_row.get("status") == "paused":
                skip_reason = "owner_paused"
            elif target_row.get("status") == "interrupted":
                skip_reason = "interrupted"

        if skip_reason is not None:
            _advance_schedule(item, current_ms, store=runtime.store, data=data)
            item["last_run_at"] = current_ms
            item["last_run_at_iso"] = automations.ms_to_iso(current_ms)
            item["last_run_status"] = "SKIPPED"
            item["missed_runs"] = effective_missed
            item["updated_at"] = now_str
            automations.try_write_automation_files(runtime.store, item)

            skip_record = {
                "id": run_id,
                "automation_id": target_id,
                "automation_name": str(item.get("name") or ""),
                "kind": "heartbeat",
                "task_id": target_task_id,
                "status": "SKIPPED",
                "skip_reason": skip_reason,
                "decision": "",
                "inbox_title": str(item.get("name") or ""),
                "inbox_summary": _SKIP_REASON_SUMMARIES.get(skip_reason, skip_reason),
                "missed_runs": effective_missed,
                "created_at": now_str,
                "completed_at": now_str,
                "updated_at": now_str,
            }
            data["automation_runs"][run_id] = skip_record
            automations.prune_automation_runs_in_tx(data, target_id)
            return {
                "triggered": False,
                "skipped": True,
                "reason": skip_reason,
                "deferred": False,
                "run": skip_record,
                "task_id": None,
                "automation": automations.public_automation(runtime.store, item),
            }

        target_row["data_policy_state"] = merge_state(target_row.get("data_policy_state"), item.get("data_policy_state"))
        invalidate_task_policy_in_tx(data, target_row)
        auto_name = str(item.get("name") or "定时提醒").strip()
        hb_msg = {
            "role": "user",
            "content": automations.format_heartbeat_user_message(
                automation_id=target_id,
                instructions=prompt_text,
                current_time_iso=now_str,
            ),
            "message_id": f"msg-hb-{run_id}",
            "ui": {
                "kind": "heartbeat",
                "text": f"⏰ {auto_name}：{prompt_text}",
                "automation_id": target_id,
                "automation_name": auto_name,
            },
        }
        budget.new_round(target_row)
        target_row.setdefault("messages", []).append(hb_msg)
        owner_update(target_row, hb_msg)
        target_row["status"] = "queued"
        target_row["steps"] = 0
        target_row["elapsed_seconds"] = 0.0
        target_row["result"] = ""
        target_row["error"] = None
        target_row["pending_approval"] = None
        target_row["pending_input"] = None
        target_row["source"] = "automation"
        target_row["automation_kind"] = "heartbeat"
        target_row["automation_id"] = target_id
        target_row["automation_run_id"] = run_id
        target_row["latest_turn_is_silent_heartbeat"] = False
        target_row["updated_at"] = now_str
        goal_rev = int((target_row.get("goal") or {}).get("revision", 1))
        event(
            target_row,
            "message_added",
            goal_revision=goal_rev,
            source="automation",
            automation_id=target_id,
            run_id=run_id,
            automation_kind="heartbeat",
        )
        spawned_task_id = target_task_id
        spawned_row = target_row
    else:
        active_prev_tid = _find_active_cron_task_id(data, target_id, item) if not force else None
        if active_prev_tid:
            _advance_schedule(item, current_ms, store=runtime.store, data=data)
            item["last_run_at"] = current_ms
            item["last_run_at_iso"] = automations.ms_to_iso(current_ms)
            item["last_run_status"] = "SKIPPED_OVERLAP"
            item["missed_runs"] = effective_missed
            item["updated_at"] = now_str
            automations.try_write_automation_files(runtime.store, item)

            skip_record = {
                "id": run_id,
                "automation_id": target_id,
                "automation_name": str(item.get("name") or ""),
                "kind": kind if kind == "heartbeat" else "cron",
                "task_id": active_prev_tid,
                "status": "SKIPPED_OVERLAP",
                "decision": "",
                "inbox_title": str(item.get("name") or ""),
                "inbox_summary": "上一轮运行尚未结束，跳过本次时段",
                "missed_runs": effective_missed,
                "created_at": now_str,
                "completed_at": now_str,
                "updated_at": now_str,
            }
            data["automation_runs"][run_id] = skip_record
            automations.prune_automation_runs_in_tx(data, target_id)
            return {
                "triggered": False,
                "deferred": False,
                "skipped_overlap": True,
                "run": skip_record,
                "task_id": None,
                "automation": automations.public_automation(runtime.store, item),
            }

        model_id, destination = _resolve_automation_model(runtime, data)
        workspace_root = (
            str(item.get("workspace_root") or "").strip()
            or str(settings.get("workspace_root") or "").strip()
            or runtime.store.default_workspace()
        )
        desktop_access = bool(item.get("desktop_access", True))
        spawned_task_id = "agent_" + uuid.uuid4().hex
        spawned_kind = "heartbeat" if kind == "heartbeat" else "cron"
        auto_name = str(item.get("name") or "定时提醒").strip()
        initial_message: dict[str, Any] = (
            {
                "role": "user",
                "content": automations.format_heartbeat_user_message(
                    automation_id=target_id,
                    instructions=prompt_text,
                    current_time_iso=now_str,
                ),
                "message_id": f"msg-hb-{run_id}",
                "ui": {
                    "kind": "heartbeat",
                    "text": f"⏰ {auto_name}：{prompt_text}",
                    "automation_id": target_id,
                    "automation_name": auto_name,
                },
            }
            if spawned_kind == "heartbeat"
            else {"role": "user", "content": prompt_text}
        )
        spawned_row = {
            "id": spawned_task_id,
            "client_request_id": None,
            "request_fingerprint": "",
            "title": title_text,
            "model_id": model_id,
            "status": "queued",
            "source": "automation",
            "automation_kind": spawned_kind,
            "automation_id": target_id,
            "automation_run_id": run_id,
            "latest_turn_is_silent_heartbeat": False,
            "revision": 0,
            "created_at": now_str,
            "updated_at": now_str,
            "messages": [initial_message],
            "events": [],
            "pending_approval": None,
            "steps": 0,
            "result": "",
            "error": None,
            "limits": {key: settings[key] for key in DEFAULT_SETTINGS if key.startswith("max_")},
            "permission_mode": item.get("permission_mode", "automatic") if spawned_kind == "cron" else settings["permission_mode"],
            "allowed_write_tools": list(settings.get("allowed_write_tools") or []),
            "queue": [],
            "in_flight": None,
            "approved": None,
            "elapsed_seconds": 0.0,
            "base_instructions": select_base_instructions(),
            "context_budget_provenance": data.get("settings_context_budget_provenance", "default_auto"),
            "data_policy_state": merge_state(None, item.get("data_policy_state")),
        }
        initialize(spawned_row)
        spawned_row["local_execution"] = {
            "workspace_root": workspace_root,
            "desktop_access": desktop_access,
            **restrict_web_policy({}, settings),
        }
        spawned_row["skills"] = []
        spawned_row["verify_text_responses"] = False
        spawned_row["response_language"] = "zh-CN"
        budget.initialize(spawned_row)
        spawned_row["limits_source"] = {key: (data.get("settings_limit_sources") or {}).get(key, "default")
                                        for key in budget.SEGMENT_LIMITS}
        if destination is not None:
            spawned_row["model_destination"] = copy.deepcopy(destination)
        spawned_row["prompt_version"] = PROMPT_VERSION
        event(spawned_row, "created", source="automation", automation_id=target_id, run_id=run_id, automation_kind=spawned_kind)
        data.setdefault("tasks", {})[spawned_task_id] = spawned_row
        if spawned_kind == "heartbeat":
            target_task_id = spawned_task_id
            item["target_task_id"] = spawned_task_id
            item["dedicated_chat"] = True

    automations.inherit_task_policy_in_tx(data, spawned_row)
    run_count = int(item.get("run_count") or 0) + 1
    item["run_count"] = run_count
    item["missed_runs"] = effective_missed
    item["last_run_at"] = current_ms
    item["last_run_at_iso"] = automations.ms_to_iso(current_ms)
    item["last_run_status"] = "running"
    item["last_task_id"] = spawned_task_id
    item["updated_at"] = now_str

    _advance_schedule(item, current_ms, store=runtime.store, run_count=run_count, data=data)
    automations.try_write_automation_files(runtime.store, item)

    run_record = {
        "id": run_id,
        "automation_id": target_id,
        "automation_name": str(item.get("name") or ""),
        "kind": kind if kind == "heartbeat" else "cron",
        "task_id": spawned_task_id,
        "status": "IN_PROGRESS",
        "decision": "",
        "inbox_title": str(item.get("name") or ""),
        "inbox_summary": "",
        "missed_runs": effective_missed,
        "created_at": now_str,
        "updated_at": now_str,
    }
    data["automation_runs"][run_id] = run_record
    automations.prune_automation_runs_in_tx(data, target_id)

    return {
        "triggered": True,
        "deferred": False,
        "run": run_record,
        "task_id": spawned_task_id,
        "automation": automations.public_automation(runtime.store, item),
    }


class AutomationSchedulerWorker:
    """Evaluates due automations on disk/store and dispatches scheduled tasks."""

    def __init__(self, runtime: Any, *, worker_id: str = AUTOMATION_WORKER_NAME) -> None:
        self.runtime = runtime
        self.worker_id = worker_id
        self._lock = threading.Lock()

    def _needs_tick(self, data: dict[str, Any]) -> bool:
        if not data.get("settings", {}).get("enabled", True):
            return False
        records = data.get("automations", {})
        current_ms = automations.now_ms()
        for auto_id, item in records.items():
            if auto_id != auto_id.lower():
                continue  # Conflicting legacy ids stay listed for repair, never double-dispatched.
            if not isinstance(item, dict) or str(item.get("status") or "ACTIVE").upper() != "ACTIVE":
                continue
            due = item.get("next_run_at")
            if due is None or (isinstance(due, (int, float)) and int(due) <= current_ms):
                return True
        tasks = data.get("tasks") or {}
        for run in (data.get("automation_runs") or {}).values():
            if not isinstance(run, dict):
                continue
            r_status = run.get("status")
            if r_status not in {"IN_PROGRESS", "NEEDS_ATTENTION"}:
                continue
            task = tasks.get(str(run.get("task_id") or ""))
            if isinstance(task, dict):
                if str(task.get("automation_run_id") or "") != str(run.get("id") or ""):
                    return True
                t_status = task.get("status")
                if t_status in automations.TERMINAL_TASK_STATUSES:
                    return True
                if r_status == "IN_PROGRESS" and t_status in automations.ATTENTION_TASK_STATUSES:
                    created_ms = automations.parse_iso_ms(run.get("created_at"))
                    if created_ms is not None and current_ms - created_ms >= automations.ATTENTION_TIMEOUT_MS:
                        return True
            elif r_status == "IN_PROGRESS":
                created_ms = automations.parse_iso_ms(run.get("created_at"))
                if created_ms is not None and current_ms - created_ms >= automations.LOST_TASK_TIMEOUT_MS:
                    return True
        return automations.sync_automation_files(self.runtime.store, records, check_only=True, data=data)

    def run_once(self) -> WorkerRunResult:
        if not self._lock.acquire(blocking=False):
            return WorkerRunResult(activity=False)
        try:
            spawned_task_ids: list[str] = []

            def tick(data: dict[str, Any]) -> bool:
                if not data.get("settings", {}).get("enabled", True):
                    return False
                automations.ensure_automation_state(data)
                changed = automations.sync_automations_from_disk_in_tx(self.runtime.store, data)

                current_ms = automations.now_ms()
                # Settle terminal, stalled (NEEDS_ATTENTION), and orphaned (LOST) runs
                tasks = data.get("tasks") or {}
                for run_rec in list(data["automation_runs"].values()):
                    if not isinstance(run_rec, dict):
                        continue
                    r_status = run_rec.get("status")
                    if r_status not in {"IN_PROGRESS", "NEEDS_ATTENTION"}:
                        continue
                    tid = str(run_rec.get("task_id") or "")
                    t_row = tasks.get(tid)
                    if isinstance(t_row, dict):
                        if str(t_row.get("automation_run_id") or "") != str(run_rec.get("id") or ""):
                            now_s = now()
                            run_rec["status"] = "CANCELLED"
                            run_rec["completed_at"] = now_s
                            run_rec["updated_at"] = now_s
                            changed = True
                            continue
                        t_status = t_row.get("status")
                        if t_status in automations.TERMINAL_TASK_STATUSES:
                            automations.settle_automation_turn_in_tx(self.runtime.store, data, t_row)
                            changed = True
                        elif r_status == "IN_PROGRESS" and t_status in automations.ATTENTION_TASK_STATUSES:
                            created_ms = automations.parse_iso_ms(run_rec.get("created_at"))
                            if created_ms is not None and current_ms - created_ms >= automations.ATTENTION_TIMEOUT_MS:
                                automations.settle_automation_turn_in_tx(self.runtime.store, data, t_row)
                                changed = True
                    elif r_status == "IN_PROGRESS":
                        created_ms = automations.parse_iso_ms(run_rec.get("created_at"))
                        if created_ms is not None and current_ms - created_ms >= automations.LOST_TASK_TIMEOUT_MS:
                            automations.settle_lost_automation_run_in_tx(self.runtime.store, data, run_rec)
                            changed = True

                due_items: list[tuple[str, int]] = []
                for auto_id, item in data["automations"].items():
                    if auto_id != auto_id.lower():
                        continue
                    if not isinstance(item, dict):
                        continue
                    if str(item.get("status") or "ACTIVE").upper() != "ACTIVE":
                        continue
                    next_ms = item.get("next_run_at")
                    if next_ms is None:
                        salt = automations.ensure_jitter_salt(self.runtime.store, data)
                        next_ms = automations.compute_next_run_at(
                            str(item.get("rrule") or ""),
                            after_ms=current_ms,
                            salt=salt,
                            automation_id=auto_id,
                            run_count=int(item.get("run_count") or 0),
                        )
                        item["next_run_at"] = next_ms
                        item["next_run_at_iso"] = automations.ms_to_iso(next_ms)
                        changed = True
                        continue
                    if isinstance(next_ms, (int, float)) and int(next_ms) <= current_ms:
                        missed = automations.compute_missed_runs(
                            str(item.get("rrule") or ""),
                            int(next_ms),
                            current_ms,
                        )
                        due_items.append((auto_id, missed))

                for auto_id, missed in due_items[:MAX_CLAIMS_PER_TICK]:
                    outcome = trigger_automation_in_tx(
                        self.runtime,
                        data,
                        auto_id,
                        force=False,
                        now_epoch_ms=current_ms,
                        missed_runs=missed,
                    )
                    changed = True
                    if outcome.get("triggered") and outcome.get("task_id"):
                        spawned_task_ids.append(str(outcome["task_id"]))

                return changed

            # Recheck inside the transaction before dispatch. Idle two-second
            # polls only inspect the committed schedule and its editable files.
            if not self.runtime.store.inspect(self._needs_tick):
                return WorkerRunResult(activity=False)
            activity = bool(self.runtime.store.transaction(tick))
            for tid in spawned_task_ids:
                self.runtime._queued_since[tid] = time.monotonic()
            return WorkerRunResult(activity=activity)
        finally:
            self._lock.release()
