"""Stage 4 independent experiment: Codex native dynamicTools protocol vs JSON proposal wrapper.

Compares under identical model, reasoning effort, workspace fixture, and permissions:
1. Arm A (`json_proposal_wrapper`): Current production `codex_tools.chat` with `outputSchema`
   and `codex_threads` pooled thread continuation.
2. Arm B (`native_dynamic_tools`): Codex `app-server` native `dynamicTools` (`item/tool/call`)
   via `codex_session._stateless_chat(..., output_schema=None)`, preserving strict provider
   execution isolation (`sandbox="read-only"`, `approvalPolicy="never"`, no provider-side execution).
"""
from __future__ import annotations

import argparse
import copy
import json
import sys
import tempfile
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
from rynmesh.agent_runtime.store import AgentStore  # noqa: E402
from rynmesh.llm_package import codex_session, codex_threads  # noqa: E402
from rynmesh.llm_package.cli_adapter import CLIAgentAdapter  # noqa: E402
from rynmesh.services import peer_box  # noqa: E402

MODEL_ID = "local:isolated-codex"
TASK_PROMPT = (
    "请查找工作区中 `service_config.py` 里的 `DEFAULT_TIMEOUT_SECONDS`，"
    "将它的值从 15 改为 30，完成后简短告诉我修改结果。"
)


class Catalog:
    def list_models(self):
        return [
            {
                "id": MODEL_ID,
                "name": "Isolated Codex experiment",
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


class ExperimentTools:
    names = (
        "ryn_file_read",
        "ryn_file_search",
        "ryn_apply_patch",
        "ryn_file_edit",
        "ryn_file_list",
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


class ProtocolExperimentModel:
    supports_reasoning_effort = True
    supports_conversation_key = True
    supports_request_deadline = True
    supports_output_token_limit = True

    def __init__(self, adapter, model_name, deadline, *, mode: str):
        self.adapter = adapter
        self.model_name = model_name
        self.deadline = deadline
        self.mode = mode  # "json_proposal_wrapper" | "native_dynamic_tools"
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
        request_id = f"exp_{self.mode}_" + uuid.uuid4().hex
        started = time.monotonic()
        limit = min(self.deadline, deadline or self.deadline)
        remaining = max(5.0, limit - started)
        if on_request_policy:
            on_request_policy(
                {
                    "provider_timeout_seconds": 180,
                    "effective_timeout_seconds": remaining,
                    "measurement": "pre_admission_upper_bound",
                    "enforcement": "adapter_timeout_and_cancel_watch",
                }
            )
        body = {
            "messages": messages,
            "tools": tool_specs,
            "max_tokens": max_output_tokens,
            "reasoning_effort": reasoning_effort or "low",
            "stream": False,
        }
        record = {"step": len(self.calls) + 1, "mode": self.mode}
        try:
            if self.mode == "json_proposal_wrapper":
                result = self.adapter.chat(
                    body,
                    task_id=request_id,
                    timeout_s=remaining,
                    model=self.model_name,
                    conversation_scope=codex_threads.scope("local", codex_threads.window_key(conversation)),
                )
            else:
                result = codex_session._stateless_chat(
                    self.adapter,
                    body,
                    task_id=request_id,
                    timeout_s=remaining,
                    model=self.model_name,
                    output_schema=None,
                )
            msg = result["message"]
            if msg.get("content") is None:
                msg = {**msg, "content": ""}
            record.update(
                tool_names=[c["function"]["name"] for c in msg.get("tool_calls", [])],
                input_tokens=result.get("input_tokens"),
                output_tokens=result.get("output_tokens"),
                provider_ms=result.get("duration_ms"),
                finish_reason=result.get("finish_reason"),
            )
            return {
                "message": msg,
                "finish_reason": result["finish_reason"],
                "usage": {"input_tokens": result.get("input_tokens"), "output_tokens": result.get("output_tokens")},
            }
        except Exception as exc:
            record["error_type"] = type(exc).__name__
            record["error_text"] = str(exc)[:300]
            raise
        finally:
            record["wall_ms"] = round((time.monotonic() - started) * 1000)
            self.calls.append(record)


def run_arm(mode: str, model_name: str, timeout_seconds: float) -> dict:
    adapter = CLIAgentAdapter("codex_cli")
    with tempfile.TemporaryDirectory(prefix=f"ryn-stage4-{mode}-") as tmp:
        base = Path(tmp)
        workspace = base / "workspace"
        workspace.mkdir(parents=True, exist_ok=True)
        target = workspace / "service_config.py"
        target.write_text(
            "# Service configuration\n"
            "DEFAULT_RETRIES = 3\n"
            "DEFAULT_TIMEOUT_SECONDS = 15\n",
            encoding="utf-8",
        )
        store_dir = base / "store"
        key = peer_box.load_or_create_messaging_key(base / "messaging.x25519")
        store = AgentStore(store_dir, key)
        tools = ExperimentTools(store, store_dir)
        started = time.monotonic()
        deadline = started + timeout_seconds
        model = ProtocolExperimentModel(adapter, model_name, deadline, mode=mode)
        runtime = AgentRuntime(store, model, tools, Catalog())
        runtime.update_settings(
            {
                "enabled": True,
                "model_id": MODEL_ID,
                "workspace_root": str(workspace),
                "permission_mode": "workspace",
                "max_steps": 12,
                "max_seconds": int(timeout_seconds),
            }
        )
        task = runtime.create({"message": TASK_PROMPT, "response_language": "zh-CN"})
        task_id = task["id"]
        while time.monotonic() < deadline:
            runtime.run_continuously()
            current = runtime.get(task_id)
            if current["status"] in {"completed", "failed", "cancelled", "interrupted"}:
                break
            time.sleep(0.05)

        total_elapsed_ms = round((time.monotonic() - started) * 1000)
        final_task = runtime.get(task_id)
        pool = getattr(adapter, "_codex_threads", None)
        pool_stats = copy.deepcopy(pool.stats) if pool is not None else {"started": 0, "resumed": 0, "fallback": 0, "rebuilt": 0}
        codex_threads.close_pool(adapter)
        updated_content = target.read_text(encoding="utf-8")
        tool_events = [e for e in final_task.get("events", []) if e.get("type") == "tool_result"]
        tool_errors = [
            e for e in tool_events if isinstance(e.get("result"), dict) and e["result"].get("error")
        ]
        return {
            "mode": mode,
            "status": final_task["status"],
            "error": final_task.get("error"),
            "steps": final_task["steps"],
            "total_elapsed_ms": total_elapsed_ms,
            "file_updated_correctly": "DEFAULT_TIMEOUT_SECONDS = 30" in updated_content and "DEFAULT_RETRIES = 3" in updated_content,
            "tool_sequence": [e.get("tool") for e in tool_events],
            "tool_error_count": len(tool_errors),
            "thread_pool_stats": pool_stats,
            "model_calls": model.calls,
            "final_answer": final_task.get("result", ""),
        }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--model", default="gpt-6.1-sol")
    parser.add_argument("--timeout", type=float, default=150.0)
    parser.add_argument(
        "--out",
        default=str(Path(__file__).resolve().parent / "live-stage4-protocol-experiment.json"),
    )
    args = parser.parse_args()

    adapter = CLIAgentAdapter("codex_cli")
    cmd_path = adapter._command_path()
    codex_session._check_protocol(cmd_path, Path(cmd_path).stat().st_mtime_ns)

    arm_a = run_arm("json_proposal_wrapper", args.model, args.timeout)
    arm_b = run_arm("native_dynamic_tools", args.model, args.timeout)

    report = {
        "recorded_at": datetime.now(timezone.utc).isoformat(),
        "model": args.model,
        "codex_executable": Path(cmd_path).name,
        "protocol_schema_verified": True,
        "task_prompt": TASK_PROMPT,
        "arms": {
            "json_proposal_wrapper": arm_a,
            "native_dynamic_tools": arm_b,
        },
    }
    Path(args.out).write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
