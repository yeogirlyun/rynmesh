"""Isolated Stage 0.3 disk-investigation benchmark and per-phase timing acceptance.

Measures and records:
1. Total wall-clock time (`total_elapsed_ms`)
2. Time to first useful result (`first_useful_result_ms`)
3. Empty poll count (`empty_poll_count_total`)
4. Overlapping background scan count (`overlapping_scan_count`)
5. Per-phase timing breakdown (`scheduler_wait_seconds`, `preparation_seconds`,
   `model_call_seconds`, `tool_execution_seconds`, `store_persistence_seconds`)
6. Final answer coverage (`answer_coverage`: top directories, top files, sizes, cleanup recommendations)
"""
from __future__ import annotations

import argparse
import copy
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
DISK_PROMPT = (
    "请排查当前工作区目录的磁盘空间占用情况，找出占用空间最大的前 3 个子目录和最大的前 3 个具体文件"
    "（包含相对路径与准确字节数），并简短说明哪些属于可清理的缓存或日志。"
)


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
    names = (
        "ryn_shell_execute",
        "ryn_file_read",
        "ryn_file_search",
        "ryn_file_list",
        "ryn_process_read",
        "ryn_process_stop",
    )

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


def populate_disk_fixture(workspace: Path) -> dict:
    """Create a deterministic multi-directory disk usage fixture."""
    files = {
        "build_cache/Symbols/bundle_debug.bin": 420_000,
        "build_cache/incremental/chunk_01.cache": 180_000,
        "logs/runtime/agent_trace_20261015.log": 310_000,
        "logs/runtime/CrashDump_old.dmp": 140_000,
        "datasets/snapshots/accounts_2026.parquet": 260_000,
        "src/core/engine.py": 12_000,
        "README.md": 2_048,
    }
    for rel, size in files.items():
        target = workspace / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        pattern = (f"[{rel}] " + ("A" * 64) + "\n").encode("ascii")
        repeats = (size // len(pattern)) + 1
        target.write_bytes((pattern * repeats)[:size])
    return {
        "expected_top_dirs": ["build_cache", "logs", "datasets"],
        "expected_top_files": [
            ("build_cache/Symbols/bundle_debug.bin", 420_000),
            ("logs/runtime/agent_trace_20261015.log", 310_000),
            ("datasets/snapshots/accounts_2026.parquet", 260_000),
        ],
    }


def run_live_disk_investigation(model_name: str, timeout_seconds: float) -> dict:
    adapter = CLIAgentAdapter("codex_cli")
    adapter._command_path()

    try:
        with tempfile.TemporaryDirectory(prefix="ryn-stage03-disk-") as temp:
            base = Path(temp)
            home, workspace = base / "private", base / "workspace"
            home.mkdir()
            workspace.mkdir()
            fixture_meta = populate_disk_fixture(workspace)

            store = AgentStore(home / "agent", peer_box.load_or_create_messaging_key(home / "messaging.x25519"))
            tools = WorkstationTools(store, home)
            started = time.monotonic()
            deadline = started + timeout_seconds
            model = LiveModel(adapter, model_name, deadline)
            runtime = AgentRuntime(store, model, tools, Catalog())
            runtime.update_settings(
                {
                    "enabled": True,
                    "model_id": MODEL_ID,
                    "permission_mode": "workspace",
                    "workspace_root": str(workspace),
                    "max_steps": 14,
                    "max_seconds": int(timeout_seconds),
                    "max_context_chars": 300000,
                    "max_output_chars": 20000,
                }
            )

            first_useful_result_ms = None
            overlapping_scan_count = 0
            empty_poll_count_total = 0

            created = runtime.create(
                {
                    "title": "Stage 0.3 live disk investigation",
                    "message": DISK_PROMPT,
                    "reasoning_effort": "low",
                    "response_language": "zh-CN",
                }
            )
            task_id = created["id"]

            try:
                while time.monotonic() < deadline:
                    runtime.run_once()
                    current = store.read()["tasks"][task_id]
                    if first_useful_result_ms is None:
                        for ev in current.get("events", []):
                            if ev.get("type") == "tool_result":
                                res = ev.get("result") or {}
                                if not res.get("error") and (
                                    res.get("output") or res.get("entries") or res.get("matches")
                                ):
                                    first_useful_result_ms = round((time.monotonic() - started) * 1000, 1)
                                    break
                    if current["status"] in {"completed", "failed", "cancelled", "interrupted"}:
                        break
            finally:
                tools.local.processes.close()

            total_elapsed_ms = round((time.monotonic() - started) * 1000, 1)
            final_row = store.read()["tasks"][task_id]
            assistant_timings = [
                ev.get("timing")
                for ev in final_row.get("events", [])
                if ev.get("type") == "assistant" and ev.get("timing")
            ]
            tool_events = [
                ev for ev in final_row.get("events", []) if ev.get("type") == "tool_result"
            ]
            for ev in tool_events:
                res = ev.get("result") or {}
                if ev.get("tool") == "ryn_process_read" and res.get("output") == "" and res.get("running"):
                    empty_poll_count_total += 1
                if ev.get("tool") == "ryn_shell_execute" and res.get("active_background_processes"):
                    overlapping_scan_count += 1

            phase_summary = {
                "scheduler_wait_seconds": round(
                    sum(float(t.get("scheduler_wait_seconds", 0.0)) for t in assistant_timings)
                    + sum(float((ev.get("timing") or {}).get("scheduler_wait_seconds", 0.0)) for ev in tool_events),
                    4,
                ),
                "preparation_seconds": round(
                    sum(
                        max(
                            0.0,
                            float(t.get("preparation_seconds", 0.0))
                            - float(
                                t.get(
                                    "preparation_persistence_seconds",
                                    max(0.0, float(t.get("store_persistence_seconds", 0.0)) - float(t.get("stream_persistence_seconds", 0.0))),
                                )
                            ),
                        )
                        for t in assistant_timings
                    )
                    + sum(float((ev.get("timing") or {}).get("preparation_seconds", 0.0)) for ev in tool_events),
                    4,
                ),
                "model_call_seconds": round(
                    sum(
                        max(0.0, float(t.get("model_call_seconds", 0.0)) - float(t.get("stream_persistence_seconds", 0.0)))
                        for t in assistant_timings
                    ),
                    4,
                ),
                "tool_execution_seconds": round(
                    sum(float((ev.get("timing") or {}).get("tool_execution_seconds", 0.0)) for ev in tool_events),
                    4,
                ),
                "store_persistence_seconds": round(
                    sum(float(t.get("store_persistence_seconds", 0.0)) for t in assistant_timings)
                    + sum(float((ev.get("timing") or {}).get("store_persistence_seconds", 0.0)) for ev in tool_events),
                    4,
                ),
            }

            answer_text = final_row.get("result") or ""
            dirs_covered = [d for d in fixture_meta["expected_top_dirs"] if d in answer_text]
            files_covered = [
                rel for rel, _ in fixture_meta["expected_top_files"]
                if Path(rel).name in answer_text
            ]
            sizes_covered = [
                size for _, size in fixture_meta["expected_top_files"]
                if str(size) in answer_text or f"{size:,}" in answer_text
            ]

            return {
                "recorded_at": datetime.now(timezone.utc).isoformat(),
                "model": model_name,
                "prompt": DISK_PROMPT,
                "status": final_row["status"],
                "error": final_row.get("error"),
                "steps": final_row["steps"],
                "total_elapsed_ms": total_elapsed_ms,
                "first_useful_result_ms": first_useful_result_ms,
                "empty_poll_count_total": empty_poll_count_total,
                "overlapping_scan_count": overlapping_scan_count,
                "phase_timing_summary": phase_summary,
                "assistant_timings": assistant_timings,
                "tool_calls_executed": [
                    {
                        "tool": ev.get("tool"),
                        "timing": ev.get("timing"),
                        "stop_reason": (ev.get("result") or {}).get("stop_reason"),
                        "empty_poll_count": (ev.get("result") or {}).get("empty_poll_count"),
                        "active_background_processes": (ev.get("result") or {}).get("active_background_processes"),
                    }
                    for ev in tool_events
                ],
                "model_calls": model.calls,
                "answer_coverage": {
                    "top_dirs_found": dirs_covered,
                    "top_dirs_coverage_ratio": f"{len(dirs_covered)}/{len(fixture_meta['expected_top_dirs'])}",
                    "top_files_found": files_covered,
                    "top_files_coverage_ratio": f"{len(files_covered)}/{len(fixture_meta['expected_top_files'])}",
                    "exact_byte_sizes_found": sizes_covered,
                    "cleanup_recommendation_present": any(
                        kw in answer_text for kw in ("build_cache", "logs", "缓存", "日志", "清理")
                    ),
                },
                "result_preview": answer_text[:1200],
            }
    finally:
        adapter.shutdown()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--model", default="gpt-6.1-sol")
    parser.add_argument("--timeout", type=float, default=180.0)
    parser.add_argument(
        "--output",
        type=Path,
        default=Path(__file__).with_name("live-stage03-disk-investigation.json"),
    )
    args = parser.parse_args()
    report = run_live_disk_investigation(args.model, args.timeout)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
