"""Background scheduler worker for Ryn Agent Scheduled Tasks (Automations)."""
from __future__ import annotations

import copy
import secrets
import threading
import time
import uuid
from typing import Any

from ..background_workers import WorkerRunResult
from . import automations, budget
from .prompt import PROMPT_VERSION, select_base_instructions
from .protocol import event, now
from .state import initialize
from .store import DEFAULT_SETTINGS, AgentError

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


def trigger_automation_in_tx(
    runtime: Any,
    data: dict[str, Any],
    automation_id: str,
    *,
    force: bool = False,
    now_epoch_ms: int | None = None,
) -> dict[str, Any]:
    automations.ensure_automation_state(data)
    target_id = str(automation_id or "").strip()
    item = data["automations"].get(target_id)
    if not isinstance(item, dict):
        raise AgentError("agent_automation_not_found")

    current_ms = int(now_epoch_ms if now_epoch_ms is not None else automations.now_ms())
    run_id = f"arun-{current_ms}-{secrets.token_hex(3)}"
    kind = str(item.get("kind") or "cron").strip().lower()
    prompt_text = str(item.get("prompt") or item.get("name") or "Execute scheduled automation.").strip()
    title_text = f"⏰ {str(item.get('name') or '定时任务').strip()}"[:150]

    settings = {**DEFAULT_SETTINGS, **(data.get("settings") or {})}
    target_task_id = str(item.get("target_task_id") or "").strip()
    tasks = data.get("tasks") or {}

    if kind == "heartbeat" and target_task_id and isinstance(tasks.get(target_task_id), dict):
        target_row = tasks[target_task_id]
        is_busy = (
            target_row.get("status") in {"queued", "running", "waiting_approval", "waiting_input"}
            or bool(target_row.get("in_flight"))
            or bool(target_row.get("pending_approval"))
            or bool(target_row.get("pending_input"))
        )
        if is_busy and not force:
            defer_ms = current_ms + 60_000
            item["next_run_at"] = defer_ms
            item["next_run_at_iso"] = automations.ms_to_iso(defer_ms)
            return {
                "triggered": False,
                "deferred": True,
                "automation": automations.public_automation(runtime.store, item),
            }

        hb_msg = {
            "role": "user",
            "content": f'<heartbeat automation_id="{target_id}">\n{prompt_text}\n</heartbeat>',
        }
        target_row.setdefault("messages", []).append(hb_msg)
        target_row["status"] = "queued"
        target_row["source"] = "automation"
        target_row["automation_id"] = target_id
        target_row["automation_run_id"] = run_id
        target_row["updated_at"] = now()
        target_row["revision"] = int(target_row.get("revision", 0)) + 1
        if isinstance(target_row.get("goal"), dict):
            target_row["goal"]["text"] = prompt_text
            target_row["goal"]["revision"] = int(target_row["goal"].get("revision", 0)) + 1
        event(target_row, "automation_triggered", automation_id=target_id, run_id=run_id)
        spawned_task_id = target_task_id
        spawned_row = target_row
    else:
        model_id, destination = _resolve_automation_model(runtime, data)
        workspace_root = (
            str(item.get("workspace_root") or "").strip()
            or str(settings.get("workspace_root") or "").strip()
            or runtime.store.default_workspace()
        )
        desktop_access = bool(item.get("desktop_access", True))
        spawned_task_id = "agent_" + uuid.uuid4().hex
        spawned_row = {
            "id": spawned_task_id,
            "client_request_id": None,
            "request_fingerprint": "",
            "title": title_text,
            "model_id": model_id,
            "status": "queued",
            "source": "automation",
            "automation_id": target_id,
            "automation_run_id": run_id,
            "revision": 0,
            "created_at": now(),
            "updated_at": now(),
            "messages": [{"role": "user", "content": prompt_text}],
            "events": [],
            "pending_approval": None,
            "steps": 0,
            "result": "",
            "error": None,
            "limits": {key: settings[key] for key in DEFAULT_SETTINGS if key.startswith("max_")},
            "permission_mode": "automatic",
            "allowed_write_tools": list(settings.get("allowed_write_tools") or []),
            "queue": [],
            "in_flight": None,
            "approved": None,
            "elapsed_seconds": 0.0,
            "base_instructions": select_base_instructions(),
            "context_budget_provenance": data.get("settings_context_budget_provenance", "default_auto"),
        }
        initialize(spawned_row)
        spawned_row["local_execution"] = {
            "workspace_root": workspace_root,
            "desktop_access": desktop_access,
        }
        spawned_row["skills"] = []
        spawned_row["verify_text_responses"] = False
        spawned_row["response_language"] = "zh-CN"
        budget.initialize(spawned_row)
        if destination is not None:
            spawned_row["model_destination"] = copy.deepcopy(destination)
        spawned_row["prompt_version"] = PROMPT_VERSION
        event(spawned_row, "created", source="automation", automation_id=target_id, run_id=run_id)
        data.setdefault("tasks", {})[spawned_task_id] = spawned_row

    run_count = int(item.get("run_count") or 0) + 1
    item["run_count"] = run_count
    item["last_run_at"] = current_ms
    item["last_run_at_iso"] = automations.ms_to_iso(current_ms)
    item["last_run_status"] = "running"
    item["last_task_id"] = spawned_task_id
    item["updated_at"] = now()

    salt = automations.ensure_jitter_salt(runtime.store)
    next_ms = automations.compute_next_run_at(
        str(item.get("rrule") or ""),
        after_ms=current_ms,
        salt=salt,
        automation_id=target_id,
        run_count=run_count,
    )
    if next_ms is None:
        item["status"] = "PAUSED"
        item["next_run_at"] = None
        item["next_run_at_iso"] = ""
        automations.write_automation_files(runtime.store, item)
    elif str(item.get("status") or "ACTIVE") == "ACTIVE":
        item["next_run_at"] = next_ms
        item["next_run_at_iso"] = automations.ms_to_iso(next_ms)

    run_record = {
        "id": run_id,
        "automation_id": target_id,
        "automation_name": str(item.get("name") or ""),
        "task_id": spawned_task_id,
        "status": "IN_PROGRESS",
        "inbox_title": str(item.get("name") or ""),
        "inbox_summary": "",
        "created_at": now(),
        "updated_at": now(),
    }
    data["automation_runs"][run_id] = run_record

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

                # Settle any runs whose tasks have already reached a terminal state
                tasks = data.get("tasks") or {}
                for run_rec in list(data["automation_runs"].values()):
                    if not isinstance(run_rec, dict) or run_rec.get("status") != "IN_PROGRESS":
                        continue
                    tid = str(run_rec.get("task_id") or "")
                    t_row = tasks.get(tid)
                    if isinstance(t_row, dict) and t_row.get("status") in {"completed", "failed", "cancelled"}:
                        automations.settle_automation_turn_in_tx(self.runtime.store, data, t_row)
                        changed = True

                current_ms = automations.now_ms()
                due_ids: list[str] = []
                for auto_id, item in data["automations"].items():
                    if not isinstance(item, dict):
                        continue
                    if str(item.get("status") or "ACTIVE").upper() != "ACTIVE":
                        continue
                    next_ms = item.get("next_run_at")
                    if next_ms is None:
                        salt = automations.ensure_jitter_salt(self.runtime.store)
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
                        due_ids.append(auto_id)

                for auto_id in due_ids[:MAX_CLAIMS_PER_TICK]:
                    outcome = trigger_automation_in_tx(
                        self.runtime,
                        data,
                        auto_id,
                        force=False,
                        now_epoch_ms=current_ms,
                    )
                    changed = True
                    if outcome.get("triggered") and outcome.get("task_id"):
                        spawned_task_ids.append(str(outcome["task_id"]))

                return changed

            activity = bool(self.runtime.store.transaction(tick))
            for tid in spawned_task_ids:
                self.runtime._queued_since[tid] = time.monotonic()
            return WorkerRunResult(activity=activity)
        finally:
            self._lock.release()
