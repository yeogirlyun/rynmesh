"""Stage 3.3 live natural-language task acceptance (unhinted file search, patch & verification).

Runs natural-language tasks without hinting tool names or recovery routes in the prompt,
recording:
- completion status (`status`)
- time to first useful result (`first_useful_result_ms`)
- total wall-clock time (`total_elapsed_ms`)
- manual intervention count (`manual_interventions`)
- autonomous tool sequence (`tool_sequence`), tool rejections/retries (`tool_error_count`),
  and file verification outcome.
"""
from __future__ import annotations

import argparse
import copy
import json
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
FILE_MOD_PROMPT = (
    "请在这个项目里查找 EnterpriseInvoice 的税率计算逻辑，将它的默认税率从 0.05 改为 0.08，"
    "不要改动 PersonalInvoice 的税率，修改后确认文件内容并简短说明改了哪个文件。"
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
        "ryn_apply_patch",
        "ryn_file_edit",
        "ryn_file_write",
        "ryn_file_list",
        "ryn_process_read",
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


def populate_project_fixture(workspace: Path) -> Path:
    (workspace / "src" / "billing").mkdir(parents=True, exist_ok=True)
    (workspace / "docs").mkdir(parents=True, exist_ok=True)
    (workspace / "docs" / "overview.md").write_text(
        "# Billing Service\n\nHandles personal and enterprise invoice calculations.\n",
        encoding="utf-8",
    )
    calc_path = workspace / "src" / "billing" / "calculator.py"
    calc_path.write_text(
        "class PersonalInvoice:\n"
        "    def compute_total(self, subtotal: float) -> float:\n"
        "        tax_rate = 0.05\n"
        "        return round(subtotal * (1.0 + tax_rate), 2)\n\n\n"
        "class EnterpriseInvoice:\n"
        "    def compute_total(self, subtotal: float) -> float:\n"
        "        tax_rate = 0.05\n"
        "        return round(subtotal * (1.0 + tax_rate), 2)\n",
        encoding="utf-8",
    )
    return calc_path


def run_live_file_modification(model_name: str, timeout_seconds: float) -> dict:
    adapter = CLIAgentAdapter("codex_cli")
    health = adapter.health()
    if not health.get("ok"):
        raise RuntimeError(f"codex_cli unavailable: {health}")

    with tempfile.TemporaryDirectory(prefix="ryn-stage33-filemod-") as tmp:
        base = Path(tmp)
        workspace = base / "workspace"
        workspace.mkdir(parents=True, exist_ok=True)
        calc_path = populate_project_fixture(workspace)
        store_dir = base / "store"
        key = peer_box.load_or_create_messaging_key(base / "messaging.x25519")
        store = AgentStore(store_dir, key)
        tools = WorkstationTools(store, store_dir)
        started = time.monotonic()
        deadline = started + timeout_seconds
        model = LiveModel(adapter, model_name, deadline)
        runtime = AgentRuntime(store, model, tools, Catalog())
        runtime.update_settings(
            {
                "enabled": True,
                "model_id": MODEL_ID,
                "workspace_root": str(workspace),
                "permission_mode": "workspace",
                "max_steps": 16,
                "max_seconds": int(timeout_seconds),
            }
        )
        task = runtime.create({"message": FILE_MOD_PROMPT, "response_language": "zh-CN"})
        task_id = task["id"]

        first_useful_result_ms = None
        while time.monotonic() < deadline:
            runtime.run_continuously()
            current = runtime.get(task_id)
            for ev in current.get("events", []):
                if ev.get("type") == "tool_result" and first_useful_result_ms is None:
                    res = ev.get("result") or {}
                    if not res.get("error"):
                        first_useful_result_ms = round((time.monotonic() - started) * 1000)
            if current["status"] in {"completed", "failed", "cancelled", "interrupted"}:
                break
            time.sleep(0.05)

        total_elapsed_ms = round((time.monotonic() - started) * 1000)
        final_task = runtime.get(task_id)
        updated_text = calc_path.read_text(encoding="utf-8")
        codex_threads.close_pool(adapter)

        tool_events = [e for e in final_task.get("events", []) if e.get("type") == "tool_result"]
        tool_sequence = [e.get("tool") for e in tool_events]
        tool_errors = [
            {"tool": e.get("tool"), "error": (e.get("result") or {}).get("error")}
            for e in tool_events
            if isinstance(e.get("result"), dict) and e["result"].get("error")
        ]

        personal_intact = "class PersonalInvoice:\n    def compute_total(self, subtotal: float) -> float:\n        tax_rate = 0.05" in updated_text
        enterprise_updated = "class EnterpriseInvoice:\n    def compute_total(self, subtotal: float) -> float:\n        tax_rate = 0.08" in updated_text

        return {
            "recorded_at": datetime.now(timezone.utc).isoformat(),
            "scenario": "unhinted_natural_language_file_search_and_modification",
            "model": model_name,
            "prompt": FILE_MOD_PROMPT,
            "prompt_hints_tools_or_recovery": False,
            "status": final_task["status"],
            "error": final_task.get("error"),
            "steps": final_task["steps"],
            "total_elapsed_ms": total_elapsed_ms,
            "first_useful_result_ms": first_useful_result_ms,
            "manual_interventions": 0,
            "tool_sequence": tool_sequence,
            "tool_error_count": len(tool_errors),
            "tool_errors": tool_errors,
            "verification": {
                "personal_invoice_tax_rate_preserved_0_05": personal_intact,
                "enterprise_invoice_tax_rate_updated_0_08": enterprise_updated,
                "mentions_calculator_py": "calculator.py" in (final_task.get("result") or ""),
            },
            "final_answer": final_task.get("result", ""),
            "model_calls": model.calls,
        }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--model", default="gpt-6.1-sol")
    parser.add_argument("--timeout", type=float, default=180.0)
    parser.add_argument(
        "--out",
        default=str(Path(__file__).resolve().parent / "live-stage33-file-modification.json"),
    )
    args = parser.parse_args()
    report = run_live_file_modification(args.model, args.timeout)
    Path(args.out).write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
