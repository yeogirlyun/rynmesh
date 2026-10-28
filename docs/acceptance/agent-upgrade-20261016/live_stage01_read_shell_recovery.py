"""Isolated Stage 0.1 acceptance using real Codex inference and real LocalTools.

Verifies that when a host-confirmed read-only shell command (ryn_shell_execute)
is interrupted after process start (e.g. an OS wait error on the first call),
the task does NOT enter agent_write_outcome_unknown; instead it records a
deterministic read-only failure receipt (no_mutation=True, not_executed=False)
and the real model continues using read tools to answer the fixture balance.
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

from rynmesh.agent_runtime.local_specs import SPECS  # noqa: E402
from rynmesh.agent_runtime.local_tools import LocalTools  # noqa: E402
from rynmesh.agent_runtime.operation_ledger import OperationLedger  # noqa: E402
from rynmesh.agent_runtime.runtime import AgentRuntime  # noqa: E402
from rynmesh.agent_runtime.store import AgentError, AgentStore, cancellation_reason  # noqa: E402
from rynmesh.llm_package import codex_threads  # noqa: E402
from rynmesh.llm_package.cli_adapter import CLIAgentAdapter  # noqa: E402
from rynmesh.services import peer_box  # noqa: E402

MODEL_ID = "local:isolated-codex"


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


def run(model_name):
    started = time.monotonic()
    adapter = CLIAgentAdapter("codex_cli")
    adapter._command_path()
    report = {
        "model": model_name,
        "reasoning_effort": "low",
        "fixture_only": True,
        "injected_fault": "first_read_shell_wait_oserror_after_process_start",
        "measured_at": datetime.now(timezone.utc).isoformat(),
    }
    try:
        with tempfile.TemporaryDirectory(prefix="ryn-stage01-read-shell-") as temporary:
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
            cmd = "Get-Content balance.py" if os.name == "nt" else "cat balance.py"
            task = runtime.create(
                {
                    "message": (
                        "这是隔离验收，工作区只有测试夹具，不是真实财务数据。"
                        f"先调用一次 ryn_shell_execute，命令严格为 {cmd}。"
                        "如果该只读命令执行失败或返回错误回执，请改用原生 ryn_file_read 读取 balance.py，"
                        "然后回答夹具中的示例余额和币种。不得改文件，不要要求人工确认。"
                    ),
                    "reasoning_effort": "low",
                }
            )
            for _ in range(48):
                runtime.run_once()
                row = store.read()["tasks"][task["id"]]
                if row["status"] in {"completed", "failed", "interrupted", "cancelled", "waiting_approval", "waiting_input"}:
                    break
                if time.monotonic() >= started + 180:
                    break
            row = store.read()["tasks"][task["id"]]
            events = row["events"]
            raw_results = [
                event.get("result")
                for event in events
                if event["type"] == "tool_result" and isinstance(event.get("result"), dict)
            ]
            tool_results = [
                {k: ("<isolated_workspace>/balance.py" if k == "path" else v) for k, v in r.items()}
                for r in raw_results
            ]
            errors = [r.get("error") for r in tool_results if r.get("error")]
            tool_names = [event["tool"] for event in events if event["type"] == "tool_started"]
            recovered = (
                "agent_read_interrupted" in errors
                and row.get("error") != "agent_write_outcome_unknown"
                and any(name in {"ryn_file_read", "ryn_file_search"} for name in tool_names)
            )
            forbidden = {"approval_requested", "verification_started", "change_review_requested", "interrupted"}
            passed = (
                row["status"] == "completed"
                and recovered
                and "123.45" in row["result"]
                and "CNY" in row["result"]
                and not any(event["type"] in forbidden for event in events)
            )
            report.update(
                task_state={
                    "status": row["status"],
                    "error": row.get("error"),
                    "steps": row["steps"],
                    "elapsed_seconds": round(row["elapsed_seconds"], 3),
                    "in_flight": bool(row.get("in_flight")),
                },
                tool_names=tool_names,
                tool_errors=errors,
                tool_results=tool_results,
                model_calls=model.calls,
                result_has_fixture_amount="123.45" in row["result"],
                recovered=recovered,
                passed=passed,
                approval_events=sum(event["type"] == "approval_requested" for event in events),
            )
            tools.local.processes.close()
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
    parser.add_argument("--out", default=str(Path(__file__).with_name("live-stage01-read-shell-recovery.json")))
    args = parser.parse_args()
    if not args.live:
        parser.error("--live is required: real inference uses the logged-in Codex account")
    report = run(args.model)
    Path(args.out).write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))
    raise SystemExit(0 if report.get("passed") else 1)


if __name__ == "__main__":
    main()
