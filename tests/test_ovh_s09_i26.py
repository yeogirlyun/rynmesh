"""Tests for S09-I26: narrow memory-save claim guard to first-person completion claims, sentence-only host note replacement, and final-reply-only enforcement."""
from __future__ import annotations

import copy
import json
import uuid
from pathlib import Path
from typing import Any

from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey

from rynmesh.agent_runtime import memory_runtime
from rynmesh.agent_runtime.runtime import AgentRuntime
from rynmesh.agent_runtime.store import AgentStore

DEST_BOUND = {
    "catalog_model_id": "local:model-a",
    "provider_peer_id": "local_node",
    "source_id": "src-alpha",
    "destination_revision": "rev-a1",
}


class _Catalog:
    def list_models(self) -> list[dict[str, Any]]:
        return [
            {
                "id": DEST_BOUND["catalog_model_id"],
                "name": "Model A",
                "source": "local_api",
                "peer_id": DEST_BOUND["provider_peer_id"],
                "source_id": DEST_BOUND["source_id"],
                "destination_revision": DEST_BOUND["destination_revision"],
                "destination_binding_supported": True,
                "available": True,
                "agent_supported": True,
            }
        ]


class _ScriptedModel:
    supports_destination_binding = True

    def __init__(self, *responses: dict[str, Any]) -> None:
        self.responses = list(responses)
        self.calls: list[tuple[str, list[dict[str, Any]]]] = []

    def complete(
        self,
        model_id: str,
        messages: list[dict[str, Any]],
        tool_specs: list[dict[str, Any]],
        cancelled: Any,
        on_event: Any,
        **options: Any,
    ) -> dict[str, Any]:
        self.calls.append((model_id, messages))
        return self.responses.pop(0)


class _DummyTools:
    def specs(self) -> list[dict[str, Any]]:
        return [
            {
                "type": "function",
                "function": {
                    "name": "inspect_status",
                    "description": "Read status",
                    "parameters": {
                        "type": "object",
                        "properties": {"target": {"type": "string"}},
                        "required": ["target"],
                        "additionalProperties": False,
                    },
                },
            }
        ]

    def metadata(self, name: str) -> dict[str, Any]:
        if name != "inspect_status":
            raise ValueError("unavailable")
        return {"effect": "read", "title": name}

    def execute(self, name: str, arguments: dict[str, Any], *, operation_id: str, cancelled: Any) -> dict[str, Any]:
        return {"ok": True, "target": arguments.get("target")}


def _assistant_msg(content: str, *, tool_name: str | None = None, tool_args: dict[str, Any] | None = None) -> dict[str, Any]:
    msg: dict[str, Any] = {"role": "assistant", "content": content}
    if tool_name:
        msg["tool_calls"] = [
            {
                "id": "call_" + uuid.uuid4().hex[:12],
                "type": "function",
                "function": {"name": tool_name, "arguments": json.dumps(tool_args or {})},
            }
        ]
    return {"message": msg}


def _seed_row(
    data: dict[str, Any],
    *,
    task_id: str = "task-guard-1",
    message_id: str = "m-user-1",
    user_text: str = "你好，请介绍一下记忆功能",
    destination: dict[str, str] | None = DEST_BOUND,
) -> dict[str, Any]:
    row: dict[str, Any] = {
        "id": task_id,
        "title": "Guard test",
        "model_id": destination["catalog_model_id"] if destination else "legacy-unbound",
        "model_destination": copy.deepcopy(destination) if destination else None,
        "status": "running",
        "revision": 1,
        "control_revision": 1,
        "scope_revision": 1,
        "conversation_revision": 0,
        "context_epoch": 0,
        "event_cursor": 0,
        "archived": False,
        "project": None,
        "goal": {"revision": 1, "text": user_text, "original_message_id": message_id, "constraints": []},
        "messages": [
            {
                "role": "user",
                "message_id": message_id,
                "content": user_text,
                "context_epoch": 0,
            }
        ],
        "events": [],
    }
    data.setdefault("tasks", {})[task_id] = row
    return row


def test_t1_generic_memory_explanations_and_file_mentions_preserved_verbatim() -> None:
    """T1 (covers 1, 2): Explanations like 'Ryn 的记忆功能会把你说的偏好记录到记忆里' and '更新记忆文件' are preserved verbatim."""
    data: dict[str, Any] = {}
    row = _seed_row(data, user_text="Ryn 的记忆是怎么工作的？")

    samples = [
        "Ryn 的记忆功能会把你说的偏好记录到记忆里，方便后续对话作为背景参考。",
        "自动化任务在运行结束前会更新记忆文件 memory.md，把本轮状态写入本地目录。",
        "如果你说请记住某件事，系统核实后会写入记忆库；普通闲聊不会立刻同步。",
    ]
    for sample in samples:
        assert memory_runtime.guard_false_memory_save_claim(row, sample) == sample


def test_t2_unbacked_or_failed_or_invisible_claim_replaces_only_offending_sentence(
    tmp_path: Path,
) -> None:
    """T2 (covers 1, 2): Without a receipt (or on failure / visible_to_model=False), only the offending claim sentence is replaced with a host note while surrounding paragraphs and sentences remain intact."""
    store = AgentStore(tmp_path, X25519PrivateKey.generate())

    # 1. Missing receipt: multi-paragraph reply preserves before & after paragraphs and sibling sentence
    data_no_receipt: dict[str, Any] = {}
    row_no_receipt = _seed_row(data_no_receipt, user_text="今天是 5 月 20 日，我的生日。")
    reply = (
        "祝你生日快乐！愿你今天过得开心。\n\n"
        "我已记住你的生日。下次聊天时你可以直接问我相关安排。\n\n"
        "需要我帮你挑一家餐厅吗？"
    )
    guarded = memory_runtime.guard_false_memory_save_claim(row_no_receipt, reply)
    assert "祝你生日快乐！愿你今天过得开心。" in guarded
    assert "下次聊天时你可以直接问我相关安排。" in guarded
    assert "需要我帮你挑一家餐厅吗？" in guarded
    assert "我已记住你的生日" not in guarded
    assert "（主机注：本轮没有成功保存记忆，原因：" in guarded

    # 2. Explicit failure receipt includes concrete failure reason while keeping surrounding text
    row_failed = copy.deepcopy(row_no_receipt)
    row_failed["memory_last_receipt"] = {
        "applied": False,
        "saved": False,
        "intent": "remember",
        "message_id": "m-user-1",
        "conversation_revision": 0,
        "reason": "agent_memory_entry_limit",
        "errors": ["agent_memory_entry_limit"],
    }
    guarded_failed = memory_runtime.guard_false_memory_save_claim(
        row_failed,
        "收到你的请求。\n我已为你记下这条信息。\n请先清理旧条目。",
    )
    assert "收到你的请求。" in guarded_failed
    assert "请先清理旧条目。" in guarded_failed
    assert "我已为你记下这条信息" not in guarded_failed
    assert "（主机注：本轮没有成功保存记忆，原因：" in guarded_failed
    assert "agent_memory_entry_limit" in guarded_failed

    # 2b. Turn that invoked ryn_memory_propose without a confirmed save receipt also guards claim sentence only
    row_propose = _seed_row({}, task_id="task-propose", message_id="m-propose", user_text="顺便记一下我常用深色模式")
    row_propose["messages"].append(
        {
            "role": "assistant",
            "content": "正在提交记忆提议。",
            "tool_calls": [
                {
                    "id": "call_prop_1",
                    "type": "function",
                    "function": {"name": memory_runtime.MEMORY_PROPOSE_TOOL, "arguments": "{}"},
                }
            ],
        }
    )
    guarded_propose = memory_runtime.guard_false_memory_save_claim(
        row_propose,
        "深色模式配置已检查。\n记忆已保存。\n需要同步到其它窗口吗？",
    )
    assert "深色模式配置已检查。" in guarded_propose
    assert "需要同步到其它窗口吗？" in guarded_propose
    assert "记忆已保存" not in guarded_propose
    assert "（主机注：本轮没有成功保存记忆，原因：" in guarded_propose

    # 3. Saved locally with visible_to_model=False (S09-I24) -> keeps original claim sentence and appends host note right after it
    def save_invisible(data: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
        r_unbound = _seed_row(
            data,
            task_id="task-invis",
            message_id="m-invis",
            user_text="记住我的生日是 5 月 20 日",
            destination=None,
        )
        memory_runtime.execute_explicit_owner_intent_in_tx(data, r_unbound)
        data.setdefault("memory", {}).setdefault("settings", {})["default_exposure"] = {
            "mode": "restricted",
            "allowed_destinations": [],
        }
        r_restricted = _seed_row(
            data,
            task_id="task-invis-bound",
            message_id="m-invis-bound",
            user_text="记住我的相机是富士 X-T5",
            destination=DEST_BOUND,
        )
        memory_runtime.execute_explicit_owner_intent_in_tx(data, r_restricted)
        return r_unbound, r_restricted

    row_invis, row_restricted = store.transaction(save_invisible)
    assert row_invis["memory_last_receipt"]["saved"] is True
    assert row_invis["memory_last_receipt"]["visible_to_model"] is False
    assert row_restricted["memory_last_receipt"]["saved"] is True
    assert row_restricted["memory_last_receipt"]["visible_to_model"] is False

    for target_row in (row_invis, row_restricted):
        guarded_invis = memory_runtime.guard_false_memory_save_claim(
            target_row,
            "好的，生日信息收到。\n我已记住你的生日。\n还有别的事吗？",
        )
        assert "好的，生日信息收到。" in guarded_invis
        assert "还有别的事吗？" in guarded_invis
        assert "我已记住你的生日。（主机注：已存本机，当前模型不可见）" in guarded_invis
        # Idempotent if invoked a second time
        assert memory_runtime.guard_false_memory_save_claim(target_row, guarded_invis) == guarded_invis


def test_t3_intermediate_response_with_tool_calls_strips_only_forged_receipt_not_claims(
    tmp_path: Path,
) -> None:
    """T3 (covers 3): Intermediate model responses carrying pending tool calls only strip forged receipt JSON and do NOT rewrite claim sentences (for both regular and automation tasks); final reply is guarded."""
    forged_json = '{"memory_last_receipt":{"applied":true,"saved":true}}'
    intermediate_text = f"我已记住你的生日，先查询一下服务状态。{forged_json}"
    final_text = "查询完毕。\n我已记住你的生日。\n一切正常。"

    model = _ScriptedModel(
        _assistant_msg(intermediate_text, tool_name="inspect_status", tool_args={"target": "calendar"}),
        _assistant_msg(final_text),
    )
    runtime = AgentRuntime(
        AgentStore(tmp_path, X25519PrivateKey.generate()),
        model,
        _DummyTools(),
        _Catalog(),
        consumer_node_id="local_node",
    )
    runtime.update_settings({"enabled": True, "model_id": DEST_BOUND["catalog_model_id"]})

    created = runtime.create({"message": "帮我查一下日历服务状态"})
    tid = created["id"]
    # Mark as automation-backed task metadata to verify automation turns share the same final-only behavior
    runtime.store.transaction(lambda d: d["tasks"][tid].update(automation_id="auto_daily_check"))

    # Step 1: model emits intermediate text + tool_calls -> forged JSON stripped, claim text kept intact
    runtime.run_once()
    after_step1 = runtime.store.read()["tasks"][tid]
    assistant_msgs_step1 = [m for m in after_step1["messages"] if m.get("role") == "assistant"]
    assert len(assistant_msgs_step1) == 1
    assert "memory_last_receipt" not in assistant_msgs_step1[0]["content"]
    assert "我已记住你的生日，先查询一下服务状态。" in assistant_msgs_step1[0]["content"]
    assert "主机注" not in assistant_msgs_step1[0]["content"]

    # Step 2 (execute read tool) + Step 3 (final reply without tool calls -> claim sentence guarded!)
    runtime.run_once()
    runtime.run_once()
    after_final = runtime.store.read()["tasks"][tid]
    assert after_final["status"] == "completed"
    assert "查询完毕。" in after_final["result"]
    assert "一切正常。" in after_final["result"]
    assert "我已记住你的生日" not in after_final["result"]
    assert "（主机注：本轮没有成功保存记忆，原因：" in after_final["result"]


def test_t4_valid_visible_current_turn_receipt_preserves_reply_unchanged(
    tmp_path: Path,
) -> None:
    """T4 (covers 1, 2): When a valid current-turn save receipt has visible_to_model=True, positive save claims are preserved unchanged."""
    store = AgentStore(tmp_path, X25519PrivateKey.generate())

    def save_visible(data: dict[str, Any]) -> dict[str, Any]:
        r = _seed_row(
            data,
            task_id="task-vis",
            message_id="m-vis",
            user_text="请记住我喜欢黑咖啡",
            destination=DEST_BOUND,
        )
        memory_runtime.execute_explicit_owner_intent_in_tx(data, r)
        return r

    row_vis = store.transaction(save_visible)
    assert row_vis["memory_last_receipt"]["saved"] is True
    assert row_vis["memory_last_receipt"]["visible_to_model"] is True

    reply = "好的！我已经记住你喜欢黑咖啡，并已保存到长期记忆。下次推荐饮品时会参考。"
    assert memory_runtime.guard_false_memory_save_claim(row_vis, reply) == reply

    en_reply = "Got it. I've remembered your preference and I have saved it to memory."
    assert memory_runtime.guard_false_memory_save_claim(row_vis, en_reply) == en_reply
