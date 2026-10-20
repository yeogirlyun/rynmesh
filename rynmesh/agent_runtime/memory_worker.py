"""Bounded background worker for opt-in agent memory natural consolidation and candidate extraction."""
from __future__ import annotations

import copy
import threading
import time
from typing import Any

from ..background_workers import WorkerRunResult
from . import memory, memory_integration, memory_natural, memory_runtime, projects
from .store import AgentError, encoded

MEMORY_WORKER_NAME = "ryn-agent-memory"
DEFAULT_WORKER_LEASE_SECONDS = 240.0
DEFAULT_EXTRACTION_MAX_OUTPUT_TOKENS = 512
DEFAULT_EXTRACTION_TIMEOUT_SECONDS = 180.0


class MemoryExtractionWorker:
    """Bounded background worker that extracts and consolidates memory from authentic owner messages."""

    def __init__(
        self,
        runtime: Any,
        *,
        worker_id: str = MEMORY_WORKER_NAME,
        lease_seconds: float = DEFAULT_WORKER_LEASE_SECONDS,
        max_output_tokens: int = DEFAULT_EXTRACTION_MAX_OUTPUT_TOKENS,
        request_timeout_seconds: float = DEFAULT_EXTRACTION_TIMEOUT_SECONDS,
    ) -> None:
        self.runtime = runtime
        self.worker_id = worker_id
        self.lease_seconds = lease_seconds
        self.max_output_tokens = max_output_tokens
        self.request_timeout_seconds = request_timeout_seconds
        self._lock = threading.Lock()

    @staticmethod
    def _is_task_eligible_for_capture(data: dict[str, Any], tid: str | None, row: Any) -> bool:
        if not isinstance(tid, str) or not tid or not isinstance(row, dict):
            return False
        if row.get("status") != "completed" or row.get("archived"):
            return False
        if (
            row.get("in_flight")
            or row.get("pending_control")
            or row.get("pending_user_messages")
            or row.get("pending_approval")
            or row.get("pending_input")
            or row.get("queue")
        ):
            return False
        mem = data.get("memory")
        if isinstance(mem, dict):
            fences = mem.get("source_fences") or {}
            if tid in (fences.get("deleted_tasks") or {}) or tid in (fences.get("archived_tasks") or {}):
                return False
        return True

    def _peek_state(self, *, task_id: str | None = None) -> dict[str, Any]:
        def inspect_fn(data: dict[str, Any]) -> dict[str, Any]:
            if not data.get("settings", {}).get("enabled", True):
                return {"enabled": False, "capture_enabled": False, "has_active_jobs": False, "task_eligible": False}
            mem = data.get("memory")
            if not isinstance(mem, dict):
                return {"enabled": True, "capture_enabled": False, "has_active_jobs": False, "task_eligible": False}
            settings = mem.get("settings") or {}
            capture_enabled = bool(settings.get("capture_enabled", False))
            jobs = mem.get("jobs") or {}
            has_active_jobs = any(
                isinstance(j, dict) and j.get("state") in {"queued", "leased", "extracted"}
                for j in jobs.values()
            )
            task_eligible = True
            if task_id is not None:
                t_row = (data.get("tasks") or {}).get(task_id)
                task_eligible = self._is_task_eligible_for_capture(data, task_id, t_row)
            return {
                "enabled": True,
                "capture_enabled": capture_enabled,
                "extraction_destination": copy.deepcopy(settings.get("extraction_destination")),
                "has_active_jobs": has_active_jobs,
                "task_eligible": task_eligible,
            }

        if hasattr(self.runtime.store, "inspect"):
            return self.runtime.store.inspect(inspect_fn)
        return inspect_fn(self.runtime.store.read())

    def _retire_active_jobs(self, error_code: str) -> bool:
        def mutate(data: dict[str, Any]) -> bool:
            mem = data.get("memory")
            if not isinstance(mem, dict):
                return False
            changed = False
            now_ts = time.time()
            for job in (mem.get("jobs") or {}).values():
                if isinstance(job, dict) and job.get("state") in {"queued", "leased", "extracted"}:
                    job["state"] = "stale"
                    job["error_code"] = error_code
                    job["updated_at"] = now_ts
                    changed = True
            if changed:
                mem["generation"] = int(mem.get("generation", 0)) + 1
            return changed

        return self.runtime.store.transaction(mutate)

    def _attempt_reason(self, data: dict[str, Any], job: dict[str, Any], *, expected_state: str | None = None) -> str | None:
        """Pure live authority check; a matching tuple alone cannot renew consent."""
        mem = data.get("memory") or {}
        settings = mem.get("settings") or {}
        if not data.get("settings", {}).get("enabled", True) or not settings.get("capture_enabled", False):
            return "capture_disabled"
        live = (mem.get("jobs") or {}).get(job.get("id"))
        if not isinstance(live, dict):
            return "extraction_job_missing"
        if live.get("lease_owner") != self.worker_id or live.get("lease_token") != job.get("lease_token"):
            return "extraction_lease_lost"
        if float(live.get("lease_until") or 0.0) <= time.time():
            return "extraction_lease_expired"
        allowed = {expected_state} if expected_state else {"leased", "extracted"}
        if live.get("state") not in allowed:
            return "extraction_job_not_active"
        if any(live.get(key) != job.get(key) for key in ("task_id", "project_id", "project_generation", "activation_policy", "dispatch_consent_generation")):
            return "extraction_job_binding_changed"
        if int(mem.get("consent_generation") or 0) != job.get("dispatch_consent_generation"):
            return "extraction_consent_changed"
        if str(settings.get("activation_policy") or "owner_review") != job.get("activation_policy", "owner_review"):
            return "extraction_policy_changed"
        if live.get("reset_epoch") != job.get("reset_epoch") or job.get("reset_epoch") != mem.get("reset_epoch"):
            return "extraction_reset_epoch_changed"
        bound = memory.normalize_destination(job.get("destination_binding"))
        if not bound or memory.normalize_destination(settings.get("extraction_destination")) != bound or memory.normalize_destination(live.get("destination_binding")) != bound:
            return "extraction_destination_drifted"
        if live.get("source_manifest") != job.get("source_manifest"):
            return "extraction_source_manifest_changed"
        row = (data.get("tasks") or {}).get(job.get("task_id"))
        if not self._is_task_eligible_for_capture(data, job.get("task_id"), row):
            return "source_task_not_completed"
        project = row.get("project") if isinstance(row.get("project"), dict) else {}
        if project.get("id") != job.get("project_id") or (job.get("project_id") and project.get("generation") != job.get("project_generation")):
            return "extraction_project_generation_changed"
        if job.get("project_id"):
            try:
                projects.validate_binding(data, project)
            except AgentError:
                return "extraction_project_binding_invalid"
        for ref in job.get("source_manifest") or []:
            ok, reason = memory.verify_source_ref_against_state(data, ref, for_background_job=True)
            if not ok:
                return reason or "extraction_source_invalidated"
        return None

    def _live_attempt_reason(self, job: dict[str, Any], *, expected_state: str | None = None,
                             catalog_attempts: int = 1, deadline: float | None = None) -> str | None:
        for attempt in range(catalog_attempts):
            reason = self._stored_attempt_reason(job, expected_state=expected_state)
            if reason:
                return reason
            if deadline is not None and time.monotonic() >= deadline:
                return "extraction_timeout"
            try:
                memory_integration.verify_exact_catalog_destination(self.runtime, job.get("destination_binding"), require_available=True)
            except memory_integration.MemoryDestinationUnavailable:
                if attempt + 1 < catalog_attempts:
                    continue
                return "extraction_destination_unavailable"
            except AgentError:
                return "extraction_destination_drifted"
            return self._stored_attempt_reason(job, expected_state=expected_state)
        return "extraction_destination_drifted"

    def _stored_attempt_reason(self, job: dict[str, Any], *, expected_state: str | None = None) -> str | None:
        view = lambda data: self._attempt_reason(data, job, expected_state=expected_state)
        return self.runtime.store.inspect(view) if hasattr(self.runtime.store, "inspect") else view(self.runtime.store.read())

    @staticmethod
    def _response_tokens(response: Any, estimated_input: int) -> int:
        usage = response.get("usage") if isinstance(response, dict) else None
        total = usage.get("total_tokens") if isinstance(usage, dict) else None
        if type(total) is int and total >= 0:
            return total
        message = response.get("message") if isinstance(response, dict) else None
        return estimated_input + (max(1, len(encoded(message)) // 4) if message is not None else 0)

    @staticmethod
    def _reserve_stage(job: dict[str, Any], stage: int, day: str, tokens: int) -> None:
        attempts = job.setdefault("dispatch_usage", {})
        token = job["lease_token"]
        for old in list(attempts):
            if old != token and all(row.get("settled") for row in attempts[old].values()):
                attempts.pop(old)
        if token not in attempts and len(attempts) >= 8:
            raise AgentError("agent_memory_dispatch_accounting_limit")
        stages = attempts.setdefault(token, {})
        if str(stage) in stages:
            raise AgentError("agent_memory_dispatch_stage_exists")
        stages[str(stage)] = {"day": day, "reserved_tokens": tokens, "dispatched": False, "settled": False}

    @staticmethod
    def _settle_stage(data: dict[str, Any], job: dict[str, Any], stage: int, actual_tokens: int) -> bool:
        mem = memory.ensure_memory_state(data)
        live = (mem.get("jobs") or {}).get(job.get("id"))
        attempts = live.get("dispatch_usage") if isinstance(live, dict) else None
        stages = attempts.get(job.get("lease_token")) if isinstance(attempts, dict) else None
        row = stages.get(str(stage)) if isinstance(stages, dict) else None
        if not isinstance(row, dict) or row.get("settled"):
            return False
        actual = max(0, actual_tokens) if row.get("dispatched") else 0
        usage = mem["daily_usage"]
        if usage.get("day") == row.get("day"):
            usage["extraction_tokens"] = max(0, int(usage.get("extraction_tokens", 0)) - int(row["reserved_tokens"]) + actual)
            if not row.get("dispatched"):
                usage["extraction_calls"] = max(0, int(usage.get("extraction_calls", 0)) - 1)
        row.update(settled=True, actual_tokens=actual)
        return True

    def _mark_dispatch(self, job: dict[str, Any], stage: int, deadline: float) -> str | None:
        expected_state = "leased" if stage == 1 else "extracted"
        reason = self._live_attempt_reason(job, expected_state=expected_state, catalog_attempts=2, deadline=deadline)
        if reason:
            return reason
        def mutate(data: dict[str, Any]) -> str | None:
            reason = self._attempt_reason(data, job, expected_state=expected_state)
            if reason or time.monotonic() >= deadline:
                return reason or "extraction_timeout"
            live = data["memory"]["jobs"][job["id"]]
            row = ((live.get("dispatch_usage") or {}).get(job.get("lease_token")) or {}).get(str(stage))
            if not isinstance(row, dict) or row.get("settled") or row.get("dispatched"):
                return "extraction_dispatch_already_finalized"
            row["dispatched"] = True
            return None
        return self.runtime.store.transaction(mutate)

    def _collect_and_claim(self, verified_dest: dict[str, Any]) -> tuple[str, Any] | None:
        def mutate(data: dict[str, Any]) -> tuple[str, Any] | None:
            if not data.get("settings", {}).get("enabled", True):
                return None
            mem = memory.ensure_memory_state(data)
            settings = mem.get("settings") or {}
            if not settings.get("capture_enabled", False):
                return None
            configured_dest = memory.normalize_destination(settings.get("extraction_destination"))
            if not configured_dest or configured_dest != verified_dest:
                return None

            is_auto_policy = str(settings.get("activation_policy") or "owner_review") == "automatic"
            auto_frontier_seq = int(mem.get("automatic_capture_frontier_seq") or 0)

            # 1. Collector: enqueue bounded jobs only for completed, nonarchived/nondeleted eligible tasks
            deleted_tasks = (mem.get("source_fences") or {}).get("deleted_tasks") or {}
            archived_tasks = (mem.get("source_fences") or {}).get("archived_tasks") or {}
            relearn_tasks = (mem.get("source_fences") or {}).get("relearn_tasks") or {}
            for tid, row in sorted((data.get("tasks") or {}).items()):
                if not self._is_task_eligible_for_capture(data, tid, row):
                    continue
                if isinstance(row.get("project"), dict):
                    try:
                        projects.validate_binding(data, row["project"])
                    except AgentError:
                        continue
                memory_natural.stamp_task_owner_messages_in_tx(data, row)
                force_relearn_task = tid in relearn_tasks
                eligible_ids: list[str] = []
                for msg in row.get("messages") or []:
                    if not isinstance(msg, dict) or msg.get("role") != "user":
                        continue
                    mid = msg.get("message_id")
                    if not isinstance(mid, str) or not mid.strip():
                        continue
                    ui_kind = (msg.get("ui") or {}).get("kind") if isinstance(msg.get("ui"), dict) else None
                    if ui_kind == "source_credential":
                        continue
                    text = (
                        msg.get("ui", {}).get("text")
                        if isinstance(msg.get("ui"), dict) and "text" in msg["ui"]
                        else msg.get("content", "")
                    )
                    if not isinstance(text, str) or not text.strip():
                        continue
                    if is_auto_policy and not force_relearn_task:
                        msg_seq = int(msg.get("owner_source_seq") or 0)
                        if 0 < msg_seq <= auto_frontier_seq:
                            continue
                    intent_info = memory_runtime.analyze_owner_memory_intent(text, row)
                    if intent_info.get("is_explicit"):
                        continue
                    eligible_ids.append(mid)
                if eligible_ids:
                    try:
                        memory.enqueue_extraction_job(
                            data,
                            task_id=tid,
                            message_ids=eligible_ids,
                            force_relearn=force_relearn_task,
                        )
                    except AgentError:
                        continue

            # 2. Claim next queued or expired-lease job whose source task is completed or permanently unavailable
            now_ts = time.time()
            candidate_job_ids: list[str] = []
            for jid, job in sorted(
                (mem.get("jobs") or {}).items(),
                key=lambda item: (float((item[1] or {}).get("created_at") or 0.0), item[0]),
            ):
                if not isinstance(job, dict):
                    continue
                state = job.get("state")
                if state == "queued" or (state == "leased" and float(job.get("lease_until") or 0.0) <= now_ts):
                    t_id = job.get("task_id")
                    t_row = (data.get("tasks") or {}).get(t_id)
                    if (
                        not isinstance(t_row, dict)
                        or t_row.get("archived")
                        or t_id in deleted_tasks
                        or t_id in archived_tasks
                    ):
                        candidate_job_ids.append(jid)
                        continue
                    if not self._is_task_eligible_for_capture(data, t_id, t_row):
                        continue
                    candidate_job_ids.append(jid)

            if not candidate_job_ids:
                return None

            target_jid = candidate_job_ids[0]
            claimed = memory.claim_extraction_job(
                data,
                worker_id=self.worker_id,
                job_id=target_jid,
                lease_seconds=self.lease_seconds,
            )
            if claimed is None:
                live_job = (mem.get("jobs") or {}).get(target_jid)
                if isinstance(live_job, dict) and live_job.get("state") == "stale":
                    mem["generation"] += 1
                    return ("stale", copy.deepcopy(live_job))
                return None

            jid = claimed["id"]
            live_job = mem["jobs"][jid]
            now_ts = time.time()

            # 3. Verify destination binding and task/project state at dispatch
            job_dest = memory.normalize_destination(claimed.get("destination_binding"))
            if job_dest != verified_dest:
                live_job.update(state="stale", error_code="extraction_destination_drifted", updated_at=now_ts)
                mem["generation"] += 1
                return ("stale", copy.deepcopy(live_job))

            task_row = (data.get("tasks") or {}).get(claimed.get("task_id"))
            if not isinstance(task_row, dict) or task_row.get("archived"):
                live_job.update(state="stale", error_code="source_task_unavailable", updated_at=now_ts)
                mem["generation"] += 1
                return ("stale", copy.deepcopy(live_job))

            if not self._is_task_eligible_for_capture(data, claimed.get("task_id"), task_row):
                live_job.update(
                    state="queued",
                    lease_owner=None,
                    lease_token=None,
                    lease_until=0.0,
                    updated_at=now_ts,
                )
                return None

            if claimed.get("project_id"):
                live_proj = task_row.get("project") if isinstance(task_row.get("project"), dict) else {}
                try:
                    projects.validate_binding(data, live_proj)
                except AgentError:
                    live_job.update(state="stale", error_code="project_binding_invalid", updated_at=now_ts)
                    mem["generation"] += 1
                    return ("stale", copy.deepcopy(live_job))
                if (
                    live_proj.get("id") != claimed.get("project_id")
                    or live_proj.get("generation") != claimed.get("project_generation")
                ):
                    live_job.update(state="stale", error_code="project_generation_changed", updated_at=now_ts)
                    mem["generation"] += 1
                    return ("stale", copy.deepcopy(live_job))

            live_job["dispatch_consent_generation"] = int(mem.get("consent_generation") or 0)
            claimed = copy.deepcopy(live_job)
            if str(claimed.get("activation_policy") or "owner_review") == "automatic":
                known = memory_natural.select_existing_memories_for_consolidation(data, claimed, verified_dest)
                slots = [
                    {key: copy.deepcopy(entry.get(key)) for key in ("scope", "project_id", "category", "fact_key", "kind")}
                    for entry in known if isinstance(entry, dict)
                ]
                prompt_messages = memory_natural.build_phase1_source_analysis_messages({**claimed, "known_memory_slots": slots})
            else:
                prompt_messages = memory_runtime.build_extraction_messages(claimed)
            if not prompt_messages:
                live_job.update(state="no_op", error_code="empty_manifest", updated_at=now_ts)
                mem["generation"] += 1
                return ("no_op", copy.deepcopy(live_job))

            # 4. Atomic daily budget reservation before model dispatch
            est_input_tokens = max(1, len(encoded(prompt_messages)) // 4)
            reserved_tokens = est_input_tokens + self.max_output_tokens
            budgets = settings.get("budgets") or {}
            max_calls = int(budgets.get("max_daily_extraction_calls", 32))
            max_tokens = int(budgets.get("max_daily_extraction_tokens", 24000))
            day = time.strftime("%Y-%m-%d", time.gmtime(now_ts))
            usage = mem["daily_usage"]
            if str(usage.get("day") or "") > day:
                live_job.update(state="failed", error_code="extraction_clock_regressed", updated_at=now_ts)
                mem["generation"] += 1
                return ("stale", copy.deepcopy(live_job))
            if usage.get("day") != day:
                usage["day"] = day
                usage["extraction_calls"] = 0
                usage["extraction_tokens"] = 0

            if (
                int(usage.get("extraction_calls", 0)) + 1 > max_calls
                or int(usage.get("extraction_tokens", 0)) + reserved_tokens > max_tokens
            ):
                live_job.update(state="failed", error_code="daily_extraction_budget_exhausted", updated_at=now_ts)
                mem["generation"] += 1
                return ("budget_exhausted", copy.deepcopy(live_job))

            try:
                self._reserve_stage(live_job, 1, day, reserved_tokens)
            except AgentError as exc:
                live_job.update(state="failed", error_code=str(exc), updated_at=now_ts)
                mem["generation"] += 1
                return ("stale", copy.deepcopy(live_job))
            usage["extraction_calls"] = int(usage.get("extraction_calls", 0)) + 1
            usage["extraction_tokens"] = int(usage.get("extraction_tokens", 0)) + reserved_tokens
            live_job["reserved_tokens"] = reserved_tokens
            live_job["reserved_day"] = day
            return (
                "dispatch",
                {
                    "job": copy.deepcopy(live_job),
                    "prompt_messages": prompt_messages,
                    "task_row": copy.deepcopy(task_row),
                    "reserved_tokens": reserved_tokens,
                    "reserved_day": day,
                },
            )

        return self.runtime.store.transaction(mutate)

    def _record_dispatch_failure(
        self,
        job: dict[str, Any],
        *,
        error_code: str,
        actual_tokens: int,
        reserved_tokens: int,
        reserved_day: str,
        state: str = "failed",
        stage: int = 1,
    ) -> None:
        def mutate(data: dict[str, Any]) -> None:
            mem = memory.ensure_memory_state(data)
            usage = mem["daily_usage"]
            self._settle_stage(data, job, stage, actual_tokens)
            live_job = (mem.get("jobs") or {}).get(job["id"])
            if not isinstance(live_job, dict):
                return
            if live_job.get("lease_owner") != self.worker_id or live_job.get("lease_token") != job.get("lease_token"):
                return
            if live_job.get("state") in {"committed", "no_op", "stale", "failed"}:
                return
            live_job["state"] = state
            live_job["error_code"] = error_code
            live_job["updated_at"] = time.time()
            mem["generation"] += 1

        self.runtime.store.transaction(mutate)

    def _prepare_stage2_dispatch(
        self,
        job: dict[str, Any],
        *,
        phase1_items: list[dict[str, Any]],
        verified_dest: dict[str, Any],
        stage1_actual_tokens: int,
        stage1_reserved_tokens: int,
        reserved_day: str,
        deadline: float | None = None,
    ) -> tuple[str, Any]:
        catalog_reason = self._live_attempt_reason(
            job, expected_state="leased", catalog_attempts=2, deadline=deadline)
        current_dest = verified_dest if catalog_reason is None else None

        def mutate(data: dict[str, Any]) -> tuple[str, Any]:
            mem = memory.ensure_memory_state(data)
            usage = mem["daily_usage"]
            settings = mem.get("settings") or {}
            configured_dest = memory.normalize_destination(settings.get("extraction_destination"))
            now_ts = time.time()

            self._settle_stage(data, job, 1, stage1_actual_tokens)
            live_job = (mem.get("jobs") or {}).get(job["id"])
            if not isinstance(live_job, dict):
                return ("stale", None)
            reason = catalog_reason or self._attempt_reason(data, job, expected_state="leased")
            if deadline is not None and time.monotonic() >= deadline:
                reason = reason or "extraction_timeout"
            if reason:
                if live_job.get("lease_owner") == self.worker_id and live_job.get("lease_token") == job.get("lease_token") and live_job.get("state") in {"leased", "extracted"}:
                    live_job["state"] = "stale"
                    live_job["error_code"] = reason
                    live_job["updated_at"] = now_ts
                    mem["generation"] += 1
                return ("stale", None)

            if (
                not data.get("settings", {}).get("enabled", True)
                or not settings.get("capture_enabled", False)
                or current_dest is None
                or configured_dest != verified_dest
                or current_dest != verified_dest
                or str(settings.get("activation_policy") or "owner_review") != "automatic"
            ):
                live_job["state"] = "stale"
                live_job["error_code"] = "capture_disabled_or_destination_drifted"
                live_job["updated_at"] = now_ts
                mem["generation"] += 1
                return ("stale", None)

            live_row = (data.get("tasks") or {}).get(job.get("task_id"))
            if not self._is_task_eligible_for_capture(data, job.get("task_id"), live_row):
                live_job["state"] = "stale"
                live_job["error_code"] = "source_task_not_completed"
                live_job["updated_at"] = now_ts
                mem["generation"] += 1
                return ("stale", None)

            if live_job.get("reset_epoch") != mem["reset_epoch"]:
                live_job["state"] = "stale"
                live_job["error_code"] = "reset_epoch_mismatch"
                live_job["updated_at"] = now_ts
                mem["generation"] += 1
                return ("stale", None)

            for src in live_job.get("source_manifest") or []:
                ok, reason = memory.verify_source_ref_against_state(data, src, for_background_job=True)
                if not ok:
                    live_job["state"] = "stale"
                    live_job["error_code"] = reason or "source_invalidated"
                    live_job["updated_at"] = now_ts
                    mem["generation"] += 1
                    return ("stale", None)

            permitted_snapshot = memory_natural.select_existing_memories_for_consolidation(
                data,
                live_job,
                verified_dest,
            )
            stage2_messages = memory_natural.build_phase2_consolidation_messages(
                live_job,
                phase1_items,
                permitted_snapshot,
            )
            est_input2 = max(1, len(encoded(stage2_messages)) // 4)
            reserved_tokens2 = est_input2 + self.max_output_tokens

            budgets = settings.get("budgets") or {}
            max_calls = int(budgets.get("max_daily_extraction_calls", 32))
            max_tokens = int(budgets.get("max_daily_extraction_tokens", 24000))
            day = time.strftime("%Y-%m-%d", time.gmtime(now_ts))
            if str(usage.get("day") or "") > day:
                live_job.update(state="failed", error_code="extraction_clock_regressed", updated_at=now_ts)
                return ("stale", None)
            if usage.get("day") != day:
                usage.update(day=day, extraction_calls=0, extraction_tokens=0)
            if (
                int(usage.get("extraction_calls", 0)) + 1 > max_calls
                or int(usage.get("extraction_tokens", 0)) + reserved_tokens2 > max_tokens
            ):
                live_job.update(state="failed", error_code="daily_extraction_budget_exhausted", updated_at=now_ts)
                mem["generation"] += 1
                return ("budget_exhausted", None)

            try:
                self._reserve_stage(live_job, 2, day, reserved_tokens2)
            except AgentError as exc:
                live_job.update(state="failed", error_code=str(exc), updated_at=now_ts)
                mem["generation"] += 1
                return ("stale", None)
            usage["extraction_calls"] = int(usage.get("extraction_calls", 0)) + 1
            usage["extraction_tokens"] = int(usage.get("extraction_tokens", 0)) + reserved_tokens2
            live_job["state"] = "extracted"
            live_job["updated_at"] = now_ts
            return (
                "dispatch",
                {
                    "stage2_messages": stage2_messages,
                    "permitted_snapshot": permitted_snapshot,
                    "stage2_reserved_tokens": reserved_tokens2,
                    "stage2_reserved_day": day,
                },
            )

        return self.runtime.store.transaction(mutate)

    def _commit_dispatched_job(
        self,
        job: dict[str, Any],
        *,
        proposals: list[dict[str, Any]],
        verified_dest: dict[str, Any],
        actual_tokens: int,
        reserved_tokens: int,
        reserved_day: str,
        deadline: float | None = None,
    ) -> None:
        try:
            current_dest = memory_integration.verify_exact_catalog_destination(
                self.runtime,
                verified_dest,
                require_available=True,
            )
        except AgentError:
            current_dest = None

        def mutate(data: dict[str, Any]) -> None:
            mem = memory.ensure_memory_state(data)
            usage = mem["daily_usage"]
            settings = mem.get("settings") or {}
            configured_dest = memory.normalize_destination(settings.get("extraction_destination"))
            attempt_reason = self._attempt_reason(data, job)
            if deadline is not None and time.monotonic() >= deadline:
                attempt_reason = attempt_reason or "extraction_timeout"
            if (
                attempt_reason
                or not data.get("settings", {}).get("enabled", True)
                or not settings.get("capture_enabled", False)
                or current_dest is None
                or configured_dest != verified_dest
                or current_dest != verified_dest
            ):
                self._settle_stage(data, job, 1, actual_tokens)
                live_job = (mem.get("jobs") or {}).get(job["id"])
                if (
                    isinstance(live_job, dict)
                    and live_job.get("lease_owner") == self.worker_id
                    and live_job.get("lease_token") == job.get("lease_token")
                    and live_job.get("state") in {"leased", "extracted"}
                ):
                    live_job["state"] = "stale"
                    live_job["error_code"] = attempt_reason or "capture_disabled_or_destination_drifted"
                    live_job["updated_at"] = time.time()
                    mem["generation"] += 1
                return

            live_row = (data.get("tasks") or {}).get(job.get("task_id"))
            if not self._is_task_eligible_for_capture(data, job.get("task_id"), live_row):
                self._settle_stage(data, job, 1, actual_tokens)
                live_job = (mem.get("jobs") or {}).get(job["id"])
                if (
                    isinstance(live_job, dict)
                    and live_job.get("lease_owner") == self.worker_id
                    and live_job.get("lease_token") == job.get("lease_token")
                    and live_job.get("state") in {"leased", "extracted"}
                ):
                    live_job["state"] = "stale"
                    live_job["error_code"] = "source_task_not_completed"
                    live_job["updated_at"] = time.time()
                    mem["generation"] += 1
                return

            # Release pre-reservation right before commit_extraction_job (which charges +1 call and +tokens_used)
            if usage.get("day") == reserved_day:
                usage["extraction_calls"] = max(0, int(usage.get("extraction_calls", 0)) - 1)
                usage["extraction_tokens"] = max(0, int(usage.get("extraction_tokens", 0)) - reserved_tokens)

            try:
                res = memory.commit_extraction_job(
                    data,
                    job_id=job["id"],
                    worker_id=self.worker_id,
                    proposals=proposals,
                    lease_token=job.get("lease_token"),
                    tokens_used=max(0, actual_tokens),
                )
            except AgentError as exc:
                # The inner atomic helper replaces memory on error; save the cost
                # through the current root, and return normally so Store persists it.
                mem = memory.ensure_memory_state(data)
                usage = mem["daily_usage"]
                if usage.get("day") == reserved_day:
                    usage["extraction_calls"] = int(usage.get("extraction_calls", 0)) + 1
                    usage["extraction_tokens"] = int(usage.get("extraction_tokens", 0)) + max(0, actual_tokens)
                live_job = (mem.get("jobs") or {}).get(job["id"])
                if isinstance(live_job, dict) and live_job.get("lease_token") == job.get("lease_token") and live_job.get("state") in {"leased", "extracted"}:
                    live_job.update(state="failed", error_code=str(exc), updated_at=time.time())
                return
            finally:
                current = memory.ensure_memory_state(data)
                current_job = (current.get("jobs") or {}).get(job["id"])
                stages = ((current_job.get("dispatch_usage") or {}).get(job.get("lease_token")) or {}) if isinstance(current_job, dict) else {}
                if isinstance(stages.get("1"), dict):
                    stages["1"].update(settled=True, actual_tokens=max(0, actual_tokens))

            if res.get("state") in {"stale", "failed"}:
                if usage.get("day") == reserved_day:
                    usage["extraction_calls"] = int(usage.get("extraction_calls", 0)) + 1
                    usage["extraction_tokens"] = int(usage.get("extraction_tokens", 0)) + max(0, actual_tokens)
            elif res.get("state") == "no_op":
                live_job = (mem.get("jobs") or {}).get(job["id"])
                if isinstance(live_job, dict) and not live_job.get("error_code"):
                    live_job["error_code"] = "no_eligible_candidates"

        self.runtime.store.transaction(mutate)

    def _commit_natural_dispatched_job(
        self,
        job: dict[str, Any],
        *,
        phase1_mode: str,
        phase1_items: list[dict[str, Any]],
        phase2_operations: list[dict[str, Any]],
        permitted_snapshot: list[dict[str, Any]] | None,
        verified_dest: dict[str, Any],
        actual_tokens: int,
        reserved_tokens: int,
        reserved_day: str,
        calls_used: int = 1,
        reserved_calls: int = 1,
        deadline: float | None = None,
    ) -> None:
        # A cold CLI inventory lookup can time out after inference succeeds.
        # Recheck once within the original deadline, with live local authority
        # before each lookup. Both checks still require the exact current tuple;
        # no cached destination or model result is promoted to authority.
        catalog_reason = self._live_attempt_reason(job, catalog_attempts=2, deadline=deadline)

        def mutate(data: dict[str, Any]) -> None:
            mem = memory.ensure_memory_state(data)
            reason = catalog_reason or self._attempt_reason(data, job)
            if deadline is not None and time.monotonic() >= deadline:
                reason = reason or "extraction_timeout"
            live_job = (mem.get("jobs") or {}).get(job["id"])
            if reason:
                if isinstance(live_job, dict) and live_job.get("lease_owner") == self.worker_id and live_job.get("lease_token") == job.get("lease_token") and live_job.get("state") in {"leased", "extracted"}:
                    live_job.update(state="stale", error_code=reason, updated_at=time.time())
                    mem["generation"] += 1
                return
            try:
                res = memory_natural.commit_natural_consolidation_job(
                    data,
                    job_id=job["id"],
                    worker_id=self.worker_id,
                    lease_token=job.get("lease_token"),
                    phase1_mode=phase1_mode,
                    phase1_items=phase1_items,
                    phase2_operations=phase2_operations,
                    permitted_snapshot=permitted_snapshot,
                    tokens_used=max(0, actual_tokens),
                    calls_used=max(1, int(calls_used)),
                    usage_accounted=True,
                )
            except AgentError as exc:
                # Stage usage was already settled, including on the helper's
                # rollback path. Reacquire the root replaced by _atomic_mutation.
                mem = memory.ensure_memory_state(data)
                live_job = (mem.get("jobs") or {}).get(job["id"])
                if isinstance(live_job, dict) and live_job.get("lease_owner") == self.worker_id and live_job.get("lease_token") == job.get("lease_token") and live_job.get("state") in {"leased", "extracted"}:
                    live_job.update(state="failed", error_code=str(exc), updated_at=time.time())
                    mem["generation"] += 1
                return
            if res.get("state") == "no_op":
                live_job = (data["memory"].get("jobs") or {}).get(job["id"])
                if isinstance(live_job, dict) and not live_job.get("error_code"):
                    live_job["error_code"] = "no_eligible_candidates"

        self.runtime.store.transaction(mutate)

    def run_once(self) -> WorkerRunResult:
        if not self._lock.acquire(blocking=False):
            return WorkerRunResult(activity=False)
        try:
            peek = self._peek_state()
            if not peek.get("enabled"):
                return WorkerRunResult(activity=False)
            if not peek.get("capture_enabled"):
                if peek.get("has_active_jobs"):
                    self._retire_active_jobs("capture_disabled")
                    return WorkerRunResult(activity=True)
                return WorkerRunResult(activity=False)

            raw_dest = peek.get("extraction_destination")
            try:
                verified_dest = memory_integration.verify_exact_catalog_destination(
                    self.runtime,
                    raw_dest,
                    require_available=True,
                )
            except AgentError:
                if peek.get("has_active_jobs"):
                    self._retire_active_jobs("extraction_destination_drifted")
                    return WorkerRunResult(activity=True)
                return WorkerRunResult(activity=False)

            claimed_outcome = self._collect_and_claim(verified_dest)
            if claimed_outcome is None:
                return WorkerRunResult(activity=False)

            status, payload = claimed_outcome
            if status != "dispatch":
                return WorkerRunResult(activity=True)

            job = payload["job"]
            prompt_messages = payload["prompt_messages"]
            task_row = payload["task_row"]
            reserved_tokens = int(payload["reserved_tokens"])
            reserved_day = str(payload["reserved_day"])
            started = time.monotonic()
            deadline = min(started + self.request_timeout_seconds, started + max(0.0, float(job.get("lease_until") or 0.0) - time.time()))
            stage = 1

            def cancelled() -> bool:
                if getattr(cancelled, "reason", None):
                    return True
                if time.monotonic() - started >= self.request_timeout_seconds:
                    cancelled.reason = "extraction_timeout"
                    return True
                if time.monotonic() >= deadline:
                    cancelled.reason = "extraction_lease_expired"
                    return True
                # Provider discovery may spawn a CLI and block. Cancellation
                # must remain a cheap local read; exact destination authority is
                # checked before each disclosure and again before commit.
                reason = self._stored_attempt_reason(job, expected_state="leased" if stage == 1 else "extracted")
                if reason:
                    cancelled.reason = reason
                    return True
                return False

            options: dict[str, Any] = {"expected_destination": copy.deepcopy(verified_dest)}
            if getattr(self.runtime.model, "supports_output_token_limit", False) is True:
                options["max_output_tokens"] = self.max_output_tokens
            if getattr(self.runtime.model, "supports_request_deadline", False) is True:
                options["deadline"] = deadline

            est_input = max(1, len(encoded(prompt_messages)) // 4)
            reason = self._mark_dispatch(job, 1, deadline)
            if reason:
                self._record_dispatch_failure(job, error_code=reason, actual_tokens=0, reserved_tokens=reserved_tokens, reserved_day=reserved_day, state="stale")
                return WorkerRunResult(activity=True)
            try:
                response = self.runtime.model.complete(
                    verified_dest["catalog_model_id"],
                    prompt_messages,
                    [],
                    cancelled,
                    lambda _ev: None,
                    **options,
                )
            except Exception as exc:
                err_code = getattr(cancelled, "reason", None) or str(exc) or "extraction_model_failed"
                if not getattr(cancelled, "reason", None) and not err_code.startswith(("agent_", "extraction_", "capture_")):
                    err_code = "extraction_model_failed"
                self._record_dispatch_failure(
                    job,
                    error_code=err_code,
                    actual_tokens=reserved_tokens,
                    reserved_tokens=reserved_tokens,
                    reserved_day=reserved_day,
                    state="stale" if err_code == "capture_disabled" else "failed",
                )
                return WorkerRunResult(activity=True)

            actual_tokens = self._response_tokens(response, est_input)
            if job.get("activation_policy") == "automatic":
                self.runtime.store.transaction(lambda data: self._settle_stage(data, job, 1, actual_tokens))
            if cancelled():
                reason = getattr(cancelled, "reason", None) or "extraction_cancelled"
                self._record_dispatch_failure(
                    job,
                    error_code=reason,
                    actual_tokens=actual_tokens,
                    reserved_tokens=reserved_tokens,
                    reserved_day=reserved_day,
                    state="stale" if reason == "capture_disabled" else "failed",
                )
                return WorkerRunResult(activity=True)

            raw_msg = response.get("message") if isinstance(response, dict) else None
            if not isinstance(raw_msg, dict) or raw_msg.get("tool_calls"):
                self._record_dispatch_failure(
                    job,
                    error_code="invalid_extraction_response",
                    actual_tokens=actual_tokens,
                    reserved_tokens=reserved_tokens,
                    reserved_day=reserved_day,
                )
                return WorkerRunResult(activity=True)

            raw_content = raw_msg.get("content")
            if not isinstance(raw_content, str):
                self._record_dispatch_failure(
                    job,
                    error_code="invalid_extraction_json",
                    actual_tokens=actual_tokens,
                    reserved_tokens=reserved_tokens,
                    reserved_day=reserved_day,
                )
                return WorkerRunResult(activity=True)

            job_with_scope = {
                **job,
                "task_scope": (
                    {
                        "kind": "project",
                        "project_id": job["project_id"],
                        "project_generation": job.get("project_generation"),
                    }
                    if job.get("project_id")
                    else {"kind": "personal", "project_id": None}
                ),
            }
            try:
                phase1_mode, phase1_items = memory_natural.parse_phase1_source_analysis_response(
                    raw_content,
                    job_with_scope,
                    task_row,
                )
            except (ValueError, AgentError) as exc:
                self._record_dispatch_failure(
                    job,
                    error_code=str(exc) or "invalid_extraction_json",
                    actual_tokens=actual_tokens,
                    reserved_tokens=reserved_tokens,
                    reserved_day=reserved_day,
                )
                return WorkerRunResult(activity=True)

            activation_policy = str(job.get("activation_policy") or "owner_review")
            if activation_policy != "automatic" or phase1_mode == "legacy_candidates":
                if phase1_mode == "legacy_candidates":
                    proposals = phase1_items
                else:
                    proposals = [
                        {
                            "kind": item.get("kind"),
                            "scope": {
                                "kind": item.get("scope", "personal"),
                                "project_id": item.get("project_id"),
                                "project_generation": job.get("project_generation") if item.get("scope") == "project" else None,
                            },
                            "category": item.get("category"),
                            "fact_key": item.get("fact_key"),
                            "text": item.get("text"),
                            "source_message_ids": item.get("source_message_ids"),
                        }
                        for item in phase1_items
                        if isinstance(item, dict) and item.get("ownership_mode") == "owned_or_habitual"
                    ]
                self._commit_dispatched_job(
                    job,
                    proposals=proposals,
                    verified_dest=verified_dest,
                    actual_tokens=actual_tokens,
                    reserved_tokens=reserved_tokens,
                    reserved_day=reserved_day,
                    deadline=deadline,
                )
                return WorkerRunResult(activity=True)

            # Automatic policy: if Stage 1 produced no durable observations, finish without Stage-2 call
            if not phase1_items:
                self._commit_natural_dispatched_job(
                    job,
                    phase1_mode=phase1_mode,
                    phase1_items=[],
                    phase2_operations=[],
                    permitted_snapshot=[],
                    verified_dest=verified_dest,
                    actual_tokens=actual_tokens,
                    reserved_tokens=reserved_tokens,
                    reserved_day=reserved_day,
                    calls_used=1,
                    reserved_calls=1,
                    deadline=deadline,
                )
                return WorkerRunResult(activity=True)

            # Stage 2: destination-authorized consolidation call under the same lease and deadline
            stage2_status, stage2_payload = self._prepare_stage2_dispatch(
                job,
                phase1_items=phase1_items,
                verified_dest=verified_dest,
                stage1_actual_tokens=actual_tokens,
                stage1_reserved_tokens=reserved_tokens,
                reserved_day=reserved_day,
                deadline=deadline,
            )
            if stage2_status != "dispatch" or stage2_payload is None:
                return WorkerRunResult(activity=True)

            stage2_messages = stage2_payload["stage2_messages"]
            permitted_snapshot = stage2_payload["permitted_snapshot"]
            stage2_reserved_tokens = int(stage2_payload["stage2_reserved_tokens"])
            stage2_reserved_day = str(stage2_payload["stage2_reserved_day"])
            stage = 2
            total_reserved_tokens = actual_tokens + stage2_reserved_tokens
            est_input2 = max(1, len(encoded(stage2_messages)) // 4)

            if getattr(self.runtime.model, "supports_request_deadline", False) is True:
                options["deadline"] = deadline

            reason = self._mark_dispatch(job, 2, deadline)
            if reason:
                self._record_dispatch_failure(job, error_code=reason, actual_tokens=0, reserved_tokens=stage2_reserved_tokens, reserved_day=stage2_reserved_day, state="stale", stage=2)
                return WorkerRunResult(activity=True)
            try:
                response2 = self.runtime.model.complete(
                    verified_dest["catalog_model_id"],
                    stage2_messages,
                    [],
                    cancelled,
                    lambda _ev: None,
                    **options,
                )
            except Exception as exc:
                err_code = getattr(cancelled, "reason", None) or str(exc) or "consolidation_model_failed"
                if not getattr(cancelled, "reason", None) and not err_code.startswith(("agent_", "extraction_", "consolidation_", "capture_")):
                    err_code = "consolidation_model_failed"
                self._record_dispatch_failure(
                    job,
                    error_code=err_code,
                    actual_tokens=stage2_reserved_tokens,
                    reserved_tokens=stage2_reserved_tokens,
                    reserved_day=stage2_reserved_day,
                    stage=2,
                    state="stale" if err_code == "capture_disabled" else "failed",
                )
                return WorkerRunResult(activity=True)

            actual_tokens2 = self._response_tokens(response2, est_input2)
            self.runtime.store.transaction(lambda data: self._settle_stage(data, job, 2, actual_tokens2))
            if cancelled():
                reason = getattr(cancelled, "reason", None) or "extraction_cancelled"
                self._record_dispatch_failure(
                    job,
                    error_code=reason,
                    actual_tokens=actual_tokens2,
                    reserved_tokens=stage2_reserved_tokens,
                    reserved_day=stage2_reserved_day,
                    stage=2,
                    state="stale" if reason == "capture_disabled" else "failed",
                )
                return WorkerRunResult(activity=True)

            raw_msg2 = response2.get("message") if isinstance(response2, dict) else None
            if not isinstance(raw_msg2, dict) or raw_msg2.get("tool_calls"):
                self._record_dispatch_failure(
                    job,
                    error_code="invalid_consolidation_response",
                    actual_tokens=actual_tokens2,
                    reserved_tokens=stage2_reserved_tokens,
                    reserved_day=stage2_reserved_day,
                    stage=2,
                )
                return WorkerRunResult(activity=True)

            raw_content2 = raw_msg2.get("content")
            if not isinstance(raw_content2, str):
                self._record_dispatch_failure(
                    job,
                    error_code="invalid_consolidation_json",
                    actual_tokens=actual_tokens2,
                    reserved_tokens=stage2_reserved_tokens,
                    reserved_day=stage2_reserved_day,
                    stage=2,
                )
                return WorkerRunResult(activity=True)

            try:
                phase2_operations = memory_natural.parse_phase2_consolidation_response(
                    raw_content2,
                    job_with_scope,
                    phase1_items,
                    existing_memories=permitted_snapshot,
                )
            except (ValueError, AgentError) as exc:
                self._record_dispatch_failure(
                    job,
                    error_code=str(exc) or "invalid_consolidation_json",
                    actual_tokens=actual_tokens2,
                    reserved_tokens=stage2_reserved_tokens,
                    reserved_day=stage2_reserved_day,
                    stage=2,
                )
                return WorkerRunResult(activity=True)

            self._commit_natural_dispatched_job(
                job,
                phase1_mode=phase1_mode,
                phase1_items=phase1_items,
                phase2_operations=phase2_operations,
                permitted_snapshot=permitted_snapshot,
                verified_dest=verified_dest,
                actual_tokens=actual_tokens + actual_tokens2,
                reserved_tokens=total_reserved_tokens,
                reserved_day=reserved_day,
                calls_used=2,
                reserved_calls=2,
                deadline=deadline,
            )
            return WorkerRunResult(activity=True)
        finally:
            self._lock.release()
