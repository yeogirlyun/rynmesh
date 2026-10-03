"""Node-owned dispatch and result archiving using existing consumer orders.

The intent and visible messages share one encrypted history transaction. A
dispatch claim is durable before calling the order service. If a process dies
in that boundary, reconciliation only checks the original ID, never resubmits.
"""
from __future__ import annotations

import copy
import hashlib
import re
import time
from datetime import datetime, timezone
from typing import Callable

from fastapi import HTTPException

from ..background_workers import WorkerRunResult
from ..file_transactions import file_transaction
from .store import ConversationError, ConversationStore, _json, clean_conversation

TERMINAL = {"succeeded", "failed", "cancelled", "timed_out", "interrupted"}


def run_records(data: dict) -> dict:
    section = data.setdefault("runs", {"version": 1, "records": {}})
    if not isinstance(section, dict) or section.get("version") != 1:
        raise ConversationError("ask_history_version_unsupported")
    if not isinstance(section.get("records"), dict):
        raise ConversationError("ask_history_unreadable")
    return section["records"]


def public_run(row: dict) -> dict:
    return {key: row[key] for key in ("task_id", "conversation_id", "state", "cancel_requested", "error_code", "cancel_error_code", "cancel_delivered") if key in row}


class AskRunService:
    def __init__(self, history: ConversationStore, context: Callable, commands: Callable):
        self.history, self.context, self.commands = history, context, commands
        self._last_checked: dict[str, float] = {}

    def begin(self, value: dict) -> dict:
        task_id = value.get("task_id")
        if not isinstance(task_id, str) or not re.fullmatch(r"task_[a-f0-9]{32}", task_id):
            raise ConversationError("ask_invalid_request")
        intent = {key: value.get(key) for key in ("conversation_id", "expected_revision", "question", "prompt_sha256")}
        if value.get("ai_permission") is not None:
            intent["ai_permission"] = value["ai_permission"]
        fingerprint = hashlib.sha256(_json(intent)).hexdigest()
        with file_transaction(self.history.lock):
            envelope, data = self.history._read()
            runs = run_records(data)
            if task_id in runs:
                if runs[task_id]["fingerprint"] != fingerprint:
                    raise ConversationError("ask_run_identity_conflict")
                return public_run(runs[task_id])
            conversation_id = intent["conversation_id"]
            if not isinstance(conversation_id, str):
                raise ConversationError("ask_invalid_request")
            row = data["conversations"].get(conversation_id)
            if row is None or conversation_id in data['tombstones']:
                raise ConversationError("ask_conversation_not_found")
            if type(intent["expected_revision"]) is not int or row["revision"] != intent["expected_revision"]:
                raise ConversationError("ask_revision_conflict")
            if any(run["conversation_id"] == conversation_id and run["state"] not in TERMINAL for run in runs.values()):
                raise ConversationError("ask_run_busy")
            if len(runs) >= 10000 or sum(run["state"] not in TERMINAL for run in runs.values()) >= 64:
                raise ConversationError("ask_history_limit")
            preview = self.context().preview(row, intent["question"])
            if preview["prompt_sha256"] != intent["prompt_sha256"]:
                raise ConversationError("ask_preview_changed")
            if preview.get("ai_permission") != intent.get("ai_permission"):
                raise ConversationError("ask_preview_changed")
            now = datetime.now(timezone.utc).isoformat()
            sources = {"contextIds": [source["library_id"] for source in preview["sources"]],
                       "contextBytes": [source["included_bytes"] for source in preview["sources"]],
                       "promptSha256": preview["prompt_sha256"]}
            question = {"id": "question_" + task_id, "taskId": task_id, "role": "user", "content": intent["question"], "status": "complete", "createdAt": now}
            answer = {"id": "answer_" + task_id, "taskId": task_id, "role": "assistant", "content": "Queued on this node. You can leave this page and return later.", "status": "queued", "createdAt": now, **sources}
            updated = {**row, "messages": [*row["messages"], question, answer], "draft": "", "updatedAt": now}
            if not row["messages"]:
                updated["title"] = intent["question"].strip().replace("\n", " ").replace("\r", " ")[:80] or row["title"]
                updated["title"] = "".join(char for char in updated["title"] if ord(char) >= 32)
            clean_conversation(updated)
            data["conversations"][conversation_id] = {**updated, "revision": row["revision"] + 1}
            run = {"task_id": task_id, "conversation_id": conversation_id, "fingerprint": fingerprint,
                   "state": "queued", "cancel_requested": False, "last_checked": 0,
                   "body": {"task_id": task_id, "idempotency_key": task_id, "provider_peer_id": preview["provider_peer_id"],
                            "service_id": preview["service_id"], "network_id": row["networkId"], "transport": "auto", "response_mode": "stream-v1",
                            "prompt": preview["prompt"], "prompt_format": preview.get("prompt_format", "text"),
                            **({"ai_permission": intent["ai_permission"]} if "ai_permission" in intent else {}),
                            "max_tokens": preview["max_output_tokens"]}}
            runs[task_id] = run
            self.history._write(envelope, data)
            return public_run(run)

    def get(self, task_id: str) -> dict:
        with file_transaction(self.history.lock):
            _, data = self.history._read()
            row = run_records(data).get(task_id)
            if row is None:
                raise ConversationError("ask_run_not_found")
            return public_run(row)

    def cancel(self, task_id: str) -> dict:
        with file_transaction(self.history.lock):
            envelope, data = self.history._read()
            row = run_records(data).get(task_id)
            if row is None:
                raise ConversationError("ask_run_not_found")
            if row["state"] not in TERMINAL:
                row["cancel_requested"] = True
                self._message(data, row, "cancel_requested", "Cancellation requested. The provider may still be computing; the node is checking the original task.")
                self.history._write(envelope, data)
            return public_run(row)

    def _message(self, data: dict, run: dict, status: str, content: str, result: dict | None = None) -> bool:
        conversation = data["conversations"].get(run["conversation_id"])
        if conversation is None:
            return False  # Deleted history never comes back through a late result.
        message = next((row for row in conversation["messages"] if row.get("id") == "answer_" + run["task_id"]), None)
        if message is None:
            return False
        update = {"status": status, "content": content}
        if result:
            update.update({target: result[source] for source, target in (("input_tokens", "inputTokens"), ("output_tokens", "outputTokens"), ("amount", "cost")) if result.get(source) is not None})
        if all(message.get(key) == value for key, value in update.items()):
            return False
        message.update(update)
        conversation["updatedAt"] = datetime.now(timezone.utc).isoformat()
        conversation["revision"] += 1
        clean_conversation(conversation)
        return True

    def _mark(self, task_id: str, **fields) -> None:
        with file_transaction(self.history.lock):
            envelope, data = self.history._read()
            run = run_records(data).get(task_id)
            if run is None or run["state"] in TERMINAL:
                return  # A terminal row is already archived; never reopen it.
            # A None clears the field rather than storing a null: a stale code
            # from an earlier attempt must not outlive the attempt that succeeded.
            run.update({key: value for key, value in fields.items() if value is not None})
            for key, value in fields.items():
                if value is None:
                    run.pop(key, None)
            self.history._write(envelope, data)

    def _finish(self, task_id: str, result: dict) -> None:
        with file_transaction(self.history.lock):
            envelope, data = self.history._read()
            run = run_records(data)[task_id]
            if run["state"] in TERMINAL:
                return
            state = str(result.get("state") or "failed")
            content = result.get("output") if state == "succeeded" else None
            if state == "succeeded" and not isinstance(content, str):
                state = "interrupted"
                content = "The original task succeeded, but its retained answer is unavailable. It has not been submitted again."
            if not content:
                content = {
                    "cancelled": "Cancellation was recorded. This does not confirm that the provider stopped computation immediately.",
                    "timed_out": "The original task timed out. No new request was submitted.",
                    "interrupted": "The node could not confirm dispatch of the original task after interruption. It has not been submitted again.",
                }.get(state, "The original request failed. No new request was submitted.")
                content = {
                    "runtime_busy": "The provider is busy. Wait or choose another service.",
                    "model_not_ready": "The model is not ready. Check the provider's model setup or choose another service.",
                    "model_not_found": "The selected model is unavailable. Check the provider's model configuration or choose another service.",
                    "runtime_unavailable": "The model runtime is unavailable. Check the provider's runtime or choose another service.",
                    "runtime_connection_failed": "The provider could not reach its model runtime. Check its local AI settings or choose another service.",
                    "service_unhealthy": "The AI service is not ready. Check the provider's model setup or choose another service.",
                    "provider_unavailable": "The provider is unavailable. Check its connection or choose another service.",
                    "direct_transport_failed": "The direct connection failed. Check the provider's connection, then check the original task before retrying.",
                    "p2p_transport_failed": "The peer connection failed. Check the connection, then check the original task before retrying.",
                    "encrypted_relay_failed": "The relay connection failed. Check the connection, then check the original task before retrying.",
                    "ai_permission_denied": "This friend has not granted access to this AI service, or has revoked it. Ask them to review your permission; no other provider was used.",
                    "capacity_exhausted": "The provider is busy. Wait or choose another service.",
                    "p2p_capacity_exhausted": "No connection session is available. Wait for the active session to close.",
                    "insufficient_balance": "There are not enough credits for this request.",
                    "consumer_restarted_before_completion": "The node restarted before the task completed. Its original order was recovered as failed; it has not been submitted again.",
                    "provider_restarted_before_completion": "The provider restarted before completing the original task. It was recorded as failed and has not been submitted again; this does not confirm that computation stopped immediately.",
                }.get(str(result.get("error_code")), content)
            run["state"] = state
            # No untrusted provider error detail or frozen prompt in receipts.
            code = result.get("error_code")
            if isinstance(code, str) and re.fullmatch(r"[a-z][a-z0-9_]{0,95}", code):
                run["error_code"] = code
            run.pop("body", None)
            self._message(data, run, "complete" if state == "succeeded" else "cancelled" if state == "cancelled" else "interrupted" if state == "interrupted" else "failed", content, result)
            self.history._write(envelope, data)

    def run_once(self) -> WorkerRunResult:
        commands = self.commands()
        if commands is None:
            return WorkerRunResult()
        with file_transaction(self.history.lock):
            envelope, data = self.history._read()
            pending = [row for row in run_records(data).values() if row["state"] not in TERMINAL]
            if not pending:
                return WorkerRunResult()
            run = min(pending, key=lambda row: self._last_checked.get(row["task_id"], 0.0))
            self._last_checked[run["task_id"]] = time.time()
            dispatch = run["state"] == "queued"
            changed = False
            if dispatch:
                run["state"] = "dispatching"
                changed = True
            if run["conversation_id"] not in data["conversations"] and not run["cancel_requested"]:
                run["cancel_requested"] = True
                changed = True
            run = copy.deepcopy(run)
            if changed:
                self.history._write(envelope, data)
        task_id = run["task_id"]
        if dispatch and run["cancel_requested"]:
            self._finish(task_id, {"state": "cancelled"})
            return WorkerRunResult(activity=True)
        cancel_error = None
        try:
            if dispatch:
                commands.submit(run["body"])
            if run["cancel_requested"] and not run.get("cancel_delivered"):
                try:
                    commands.cancel(task_id)
                    # A delivered cancel retires the code left by a failed one.
                    self._mark(task_id, cancel_delivered=True, cancel_error_code=None)
                except HTTPException as exc:
                    # A rejected cancel (e.g. balance release conflict) must
                    # never stop the status check from reaching a real result.
                    cancel_error = exc
            result = commands.status(task_id)
        except HTTPException as exc:
            if exc.status_code == 404:
                self._finish(task_id, {"state": "interrupted"})
            # Other failures remain check-only; even an ambiguous submit error
            # must not create another order or a fresh task identity.
            return WorkerRunResult(activity=True)
        except Exception:
            # Registry errors must not contain a question, response or provider
            # error body. A later iteration reconciles the durable task ID.
            raise ConversationError("ask_run_check_unavailable") from None
        if cancel_error is not None:
            detail = cancel_error.detail
            code = detail if isinstance(detail, str) and re.fullmatch(r"[a-z][a-z0-9_]{0,95}", detail) else "cancel_rejected"
            self._mark(task_id, cancel_error_code=code)
        if result.get("state") in TERMINAL and not result.get("result_pending"):
            self._finish(task_id, result)
            commands.acknowledge(task_id)  # Only after encrypted history commits.
        else:
            with file_transaction(self.history.lock):
                envelope, data = self.history._read()
                current = run_records(data)[task_id]
                if current["state"] not in TERMINAL:
                    row_changed = current["state"] != "running"
                    current["state"] = "running"
                    message_changed = self._message(data, current, "cancel_requested" if current["cancel_requested"] else "running", "Cancellation requested; checking the original task." if current["cancel_requested"] else "The node is waiting for the original task. You can leave this page.")
                    if row_changed or message_changed:
                        self.history._write(envelope, data)
        return WorkerRunResult(activity=True)
