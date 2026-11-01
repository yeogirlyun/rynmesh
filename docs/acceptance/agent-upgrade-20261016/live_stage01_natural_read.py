"""Isolated Stage 0.1 natural-language acceptance (no tool or recovery hints in prompt).

Separately records:
1. Unhinted natural-language balance query with post-spawn read-shell fault hook armed
   (records actual autonomous tool selection, whether fault hook fired, and latency).
2. Model continuation after synthetically injected restart state: the model first proposes a tool
   on the unhinted prompt, the test harness rewrites that first tool call to an in-flight read-only
   `ryn_shell_execute` and simulates host restart + `reconcile()`, then verifies model continuation
   from the `agent_node_restarted` receipt without prompt hints.
"""
from __future__ import annotations

import argparse
import copy
import errno
import json
import os
import sys
import tempfile
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))

from rynmesh.agent_runtime import verification  # noqa: E402
from rynmesh.agent_runtime.execution_policy import classify_operation  # noqa: E402
from rynmesh.agent_runtime.local_specs import SPECS  # noqa: E402
from rynmesh.agent_runtime.local_tools import LocalTools  # noqa: E402
from rynmesh.agent_runtime.operation_ledger import OperationLedger  # noqa: E402
from rynmesh.agent_runtime.protocol import digest, model_retry_authority, now  # noqa: E402
from rynmesh.agent_runtime.runtime import AgentRuntime  # noqa: E402
from rynmesh.agent_runtime.store import AgentError, AgentStore, cancellation_reason  # noqa: E402
from rynmesh.llm_package import codex_threads  # noqa: E402
from rynmesh.llm_package.cli_adapter import CLIAgentAdapter  # noqa: E402
from rynmesh.services import peer_box  # noqa: E402

MODEL_ID = "local:isolated-codex"
NATURAL_PROMPT = "请帮我查一下当前工作区里记录的示例账户余额和币种是多少，直接回答具体数值和币种。"


class Catalog:
    def list_models(self):
        return [
            {
                "id": MODEL_ID,
                "name": "Isolated Codex acceptance",
                "source": "local_api",
                "available": True,
                "agent_supported": True,
                "adapter": "codex_cli",
                "supported_reasoning_efforts": ["low"],
                "default_reasoning_effort": "low",
                "context_window": 128000,
                "max_output_tokens": 2048,
                "streaming": True,
            }
        ]


class WorkstationTools:
    names = ("ryn_shell_execute", "ryn_file_read", "ryn_file_search", "ryn_file_list", "ryn_process_read")

    def __init__(self, store, home):
        ledger = OperationLedger(home)
        self.local = LocalTools(SimpleNamespace(home=home), lambda: ledger)
        self.local.store = store

    def specs(self):
        return [
            {
                "type": "function",
                "function": {
                    "name": name,
                    "description": SPECS[name][0],
                    "parameters": copy.deepcopy(SPECS[name][3]),
                },
            }
            for name in self.names
        ]

    def metadata(self, name):
        effect = SPECS[name][1]
        return {"effect": effect, "scope_context_supported": True, "preview_precondition": effect == "write"}

    def preview(self, name, args, *, context):
        return self.local.preview(name, args, context)

    def execute(self, name, args, *, operation_id, cancelled, context, expected_preview=None):
        return self.local.execute(
            name,
            args,
            operation_id=operation_id,
            cancelled=cancelled,
            context=context,
            expected_preview=expected_preview,
        )

    def recover_operation(self, task_id, operation_id=None, *, error="agent_read_interrupted", operation_classification=None):
        return self.local.recover_operation(
            task_id,
            operation_id,
            error=error,
            operation_classification=operation_classification,
        )


class LiveModel:
    supports_reasoning_effort = True
    supports_conversation_key = True
    supports_request_deadline = True
    supports_output_token_limit = True

    def __init__(self, adapter, model, deadline):
        self.adapter, self.model, self.deadline = adapter, model, deadline
        self.calls = []

    def complete(
        self,
        model_id,
        messages,
        tool_specs,
        cancelled,
        on_event,
        *,
        reasoning_effort=None,
        conversation=None,
        max_output_tokens=2048,
        deadline=None,
        on_request_policy=None,
    ):
        request_id = "acceptance_" + uuid.uuid4().hex
        started = time.monotonic()
        stop = threading.Event()
        limit = min(self.deadline, deadline or self.deadline)
        if cancelled():
            raise AgentError(cancellation_reason(cancelled))
        remaining = limit - started
        if remaining <= 0:
            raise AgentError("agent_inference_timeout")
        if on_request_policy:
            on_request_policy(
                {
                    "provider_timeout_seconds": 180,
                    "effective_timeout_seconds": remaining,
                    "measurement": "pre_admission_upper_bound",
                    "enforcement": "adapter_timeout_and_cancel_watch",
                }
            )
        cancelled_by_watcher = []

        def watch():
            while not stop.wait(0.05):
                if cancelled() or time.monotonic() >= limit:
                    cancelled_by_watcher.append(True)
                    self.adapter.cancel(request_id)
                    return

        def emit(chunk):
            if on_event:
                for choice in chunk.get("choices", []):
                    text = (choice.get("delta") or {}).get("content")
                    if isinstance(text, str) and text:
                        on_event({"type": "text_delta", "text": text})

        watcher = threading.Thread(target=watch, daemon=True, name="acceptance-model-deadline")
        watcher.start()
        record = {"step": len(self.calls) + 1}
        try:
            result = self.adapter.chat(
                {
                    "messages": messages,
                    "tools": tool_specs,
                    "max_tokens": max_output_tokens,
                    "reasoning_effort": reasoning_effort or "low",
                    "stream": True,
                },
                task_id=request_id,
                timeout_s=remaining,
                model=self.model,
                on_event=emit,
                conversation_scope=codex_threads.scope("local", codex_threads.window_key(conversation)),
            )
            if cancelled_by_watcher:
                raise AgentError("agent_inference_timeout")
            record.update(
                tool_names=[call["function"]["name"] for call in result["message"].get("tool_calls", [])],
                input_tokens=result.get("input_tokens"),
                output_tokens=result.get("output_tokens"),
                provider_ms=result.get("duration_ms"),
                finish_reason=result.get("finish_reason"),
            )
            return {
                "message": result["message"],
                "finish_reason": result["finish_reason"],
                "usage": {"input_tokens": result.get("input_tokens"), "output_tokens": result.get("output_tokens")},
            }
        except Exception as exc:
            record["error_type"] = type(exc).__name__
            raise
        finally:
            stop.set()
            watcher.join(1)
            record.update(
                wall_ms=round((time.monotonic() - started) * 1000),
                deadline_cancelled=bool(cancelled_by_watcher),
            )
            self.calls.append(record)


def _run_unhinted_natural_query(adapter, model_name):
    started = time.monotonic()
    with tempfile.TemporaryDirectory(prefix="ryn-stage01-natural-") as temporary:
        base = Path(temporary)
        home, workspace = base / "private", base / "workspace"
        home.mkdir()
        workspace.mkdir()
        (workspace / "balance.py").write_text(
            "# Acceptance fixture, no network or real financial records.\n"
            "# balance_query sample balance: 123.45 CNY\n"
            "def balance_query():\n    return {'balance': 123.45, 'currency': 'CNY'}\n",
            encoding="utf-8",
        )
        store = AgentStore(home / "agent", peer_box.load_or_create_messaging_key(home / "messaging.x25519"))
        tools = WorkstationTools(store, home)
        orig_wait = tools.local.processes.wait
        injected = {"count": 0}

        def flaky_wait(*args, **kwargs):
            if injected["count"] == 0:
                injected["count"] += 1
                raise OSError(errno.EIO, "simulated post-spawn read pipe failure")
            return orig_wait(*args, **kwargs)

        tools.local.processes.wait = flaky_wait
        model = LiveModel(adapter, model_name, started + 180)
        runtime = AgentRuntime(store, model, tools, Catalog())
        runtime.update_settings(
            {
                "enabled": True,
                "model_id": MODEL_ID,
                "workspace_root": str(workspace),
                "permission_mode": "read_only",
                "max_steps": 12,
                "max_seconds": 180,
                "max_context_chars": 300000,
            }
        )
        task = runtime.create({"message": NATURAL_PROMPT, "reasoning_effort": "low"})
        try:
            for _ in range(48):
                runtime.run_once()
                row = store.read()["tasks"][task["id"]]
                if row["status"] in {"completed", "failed", "interrupted", "cancelled", "waiting_approval", "waiting_input"}:
                    break
                if time.monotonic() >= started + 180:
                    break
            row = store.read()["tasks"][task["id"]]
            events = row["events"]
            tool_names = [event["tool"] for event in events if event["type"] == "tool_started"]
            errors = [
                event.get("result", {}).get("error")
                for event in events
                if event["type"] == "tool_result" and isinstance(event.get("result"), dict) and event.get("result", {}).get("error")
            ]
            passed = (
                row["status"] == "completed"
                and row.get("error") != "agent_write_outcome_unknown"
                and "123.45" in row["result"]
                and "CNY" in row["result"]
            )
            return {
                "scenario": "unhinted_natural_query_with_shell_fault_hook_armed",
                "prompt": NATURAL_PROMPT,
                "prompt_contains_tool_or_recovery_hints": False,
                "shell_fault_hook_triggered": injected["count"] > 0,
                "task_state": {
                    "status": row["status"],
                    "error": row.get("error"),
                    "steps": row["steps"],
                    "elapsed_seconds": round(row["elapsed_seconds"], 3),
                    "in_flight": bool(row.get("in_flight")),
                },
                "tool_names": tool_names,
                "tool_errors": errors,
                "model_calls": model.calls,
                "result_has_fixture_amount": "123.45" in row["result"] and "CNY" in row["result"],
                "passed": passed,
                "approval_events": sum(event["type"] == "approval_requested" for event in events),
                "wall_ms": round((time.monotonic() - started) * 1000),
            }
        finally:
            tools.local.processes.close()


def _run_injected_restart_state_model_continuation(adapter, model_name):
    """Natural-language prompt; harness rewrites step-1 tool call into an in-flight read shell and simulates restart; model continues with no prompt hints."""
    started = time.monotonic()
    with tempfile.TemporaryDirectory(prefix="ryn-stage01-natural-restart-") as temporary:
        base = Path(temporary)
        home, workspace = base / "private", base / "workspace"
        home.mkdir()
        workspace.mkdir()
        (workspace / "balance.py").write_text(
            "# Acceptance fixture, no network or real financial records.\n"
            "# balance_query sample balance: 123.45 CNY\n"
            "def balance_query():\n    return {'balance': 123.45, 'currency': 'CNY'}\n",
            encoding="utf-8",
        )
        store = AgentStore(home / "agent", peer_box.load_or_create_messaging_key(home / "messaging.x25519"))
        tools = WorkstationTools(store, home)
        model = LiveModel(adapter, model_name, started + 180)
        runtime = AgentRuntime(store, model, tools, Catalog())
        runtime.update_settings(
            {
                "enabled": True,
                "model_id": MODEL_ID,
                "workspace_root": str(workspace),
                "permission_mode": "read_only",
                "max_steps": 12,
                "max_seconds": 180,
                "max_context_chars": 300000,
            }
        )
        task = runtime.create({"message": NATURAL_PROMPT, "reasoning_effort": "low"})
        # Step 1: Run one real model step on the unhinted natural prompt
        runtime.run_once()
        queued_row = store.read()["tasks"][task["id"]]
        initial_model_proposed_tools = [item.get("name") for item in queued_row.get("queue", []) if item.get("name")]
        # Harness synthetically rewrites the step-1 tool call to an in-flight read-only shell to test restart continuation
        cmd = "Get-ChildItem" if os.name == "nt" else "ls"
        op_id = "op_unhinted_restart_read"
        call_id = "call_unhinted_restart_read"
        args = {"command": cmd}
        classification = classify_operation(
            "ryn_shell_execute",
            args,
            tools.metadata("ryn_shell_execute"),
            operation_id=op_id,
            task_id=task["id"],
            workspace_root=str(workspace),
            goal_revision=queued_row["goal"]["revision"],
            scope_revision=queued_row["scope_revision"],
            context_epoch=queued_row.get("context_epoch", 0),
            control_revision=queued_row["control_revision"],
        )
        binding = {
            "operation_id": op_id,
            "tool": "ryn_shell_execute",
            "task_id": task["id"],
            "arguments_sha256": digest(args),
            "local_execution": queued_row["local_execution"],
        }
        ledger = tools.local.ledger_factory()
        ledger.begin(binding, {"title": "Execute"}, receipt_reserve=32768)
        ledger.update(op_id, "dispatched", operation_classification=classification, host_pid=os.getpid())

        def inject_in_flight_restart_state(data):
            row = data["tasks"][task["id"]]
            call_item = {
                "call_id": call_id,
                "operation_id": op_id,
                "name": "ryn_shell_execute",
                "arguments": args,
                "effect": "write",
                "context_epoch": row.get("context_epoch", 0),
            }
            # Harness rewrites the assistant's first tool call with the in-flight read shell so transcript pairs match
            for msg in reversed(row["messages"]):
                if msg.get("role") == "assistant" and msg.get("tool_calls"):
                    msg["tool_calls"] = [
                        {
                            "id": call_id,
                            "type": "function",
                            "function": {"name": "ryn_shell_execute", "arguments": json.dumps(args)},
                        }
                    ]
                    break
            row["status"] = "running"
            row["queue"] = [call_item]
            row["in_flight"] = {
                **call_item,
                "kind": "tool",
                "dispatched": True,
                "authorization": "read",
                "operation_classification": classification,
                "goal_revision": row["goal"]["revision"],
                "control_revision": row["control_revision"],
                "scope_revision": row["scope_revision"],
                "context_epoch": row.get("context_epoch", 0),
                "authority": model_retry_authority(data["settings"]),
                "preview": None,
            }
            verification.record(row, call_item, "intent", now(), flight=row["in_flight"])

        store.transaction(inject_in_flight_restart_state)
        try:
            restarted_tools = WorkstationTools(store, home)
            restarted = AgentRuntime(store, model, restarted_tools, Catalog())
            restarted.reconcile()
            settled_ledger = restarted_tools.local.ledger_factory().get(op_id)
            for _ in range(48):
                restarted.run_once()
                row = store.read()["tasks"][task["id"]]
                if row["status"] in {"completed", "failed", "interrupted", "cancelled", "waiting_approval", "waiting_input"}:
                    break
                if time.monotonic() >= started + 180:
                    break
            row = store.read()["tasks"][task["id"]]
            events = row["events"]
            tool_names = [event["tool"] for event in events if event["type"] == "tool_started"]
            errors = [
                event.get("result", {}).get("error")
                for event in events
                if event["type"] == "tool_result" and isinstance(event.get("result"), dict) and event.get("result", {}).get("error")
            ]
            passed = (
                row["status"] == "completed"
                and "agent_node_restarted" in errors
                and settled_ledger.get("status") == "failed"
                and settled_ledger.get("cleanup_confirmed") is True
                and "receipt_reserve" not in settled_ledger
                and "123.45" in row["result"]
                and "CNY" in row["result"]
            )
            return {
                "scenario": "injected_restart_state_model_continuation",
                "prompt": NATURAL_PROMPT,
                "prompt_contains_tool_or_recovery_hints": False,
                "initial_model_proposed_tools": initial_model_proposed_tools,
                "initial_tool_call_rewritten_by_harness": True,
                "injected_in_flight_tool": "ryn_shell_execute",
                "task_state": {
                    "status": row["status"],
                    "error": row.get("error"),
                    "steps": row["steps"],
                    "elapsed_seconds": round(row["elapsed_seconds"], 3),
                    "in_flight": bool(row.get("in_flight")),
                },
                "ledger_after_reconcile": {
                    "status": settled_ledger.get("status"),
                    "no_mutation": settled_ledger.get("no_mutation"),
                    "cleanup_confirmed": settled_ledger.get("cleanup_confirmed"),
                    "has_receipt_reserve": "receipt_reserve" in settled_ledger,
                },
                "tool_names_after_restart": tool_names,
                "tool_errors": errors,
                "model_calls": model.calls,
                "result_has_fixture_amount": "123.45" in row["result"] and "CNY" in row["result"],
                "passed": passed,
                "approval_events": sum(event["type"] == "approval_requested" for event in events),
                "wall_ms": round((time.monotonic() - started) * 1000),
            }
        finally:
            tools.local.processes.close()
            restarted_tools.local.processes.close()


def run(model_name):
    started = time.monotonic()
    adapter = CLIAgentAdapter("codex_cli")
    adapter._command_path()
    report = {
        "model": model_name,
        "reasoning_effort": "low",
        "fixture_only": True,
        "measured_at": datetime.now(timezone.utc).isoformat(),
    }
    try:
        unhinted_query = _run_unhinted_natural_query(adapter, model_name)
        injected_restart = _run_injected_restart_state_model_continuation(adapter, model_name)
        report.update(
            unhinted_natural_query=unhinted_query,
            injected_restart_state_model_continuation=injected_restart,
            passed=bool(unhinted_query.get("passed") and injected_restart.get("passed")),
        )
    except Exception as exc:
        report.update(passed=False, acceptance_error_type=type(exc).__name__)
    finally:
        adapter.shutdown()
    report["wall_ms"] = round((time.monotonic() - started) * 1000)
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--live", action="store_true")
    parser.add_argument("--model", default="gpt-6.1-sol")
    parser.add_argument("--out", default=str(Path(__file__).with_name("live-stage01-natural-read.json")))
    args = parser.parse_args()
    if not args.live:
        parser.error("--live is required: real inference uses the logged-in Codex account")
    report = run(args.model)
    Path(args.out).write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))
    raise SystemExit(0 if report.get("passed") else 1)


if __name__ == "__main__":
    main()
