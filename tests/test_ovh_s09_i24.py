"""Tests for S09-I24: default explicit-entry visibility to current model, visible_to_model receipts, and memory_not_injected events."""
from __future__ import annotations

import copy
from pathlib import Path
from typing import Any

from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey

from rynmesh.agent_runtime import memory, memory_integration, memory_runtime
from rynmesh.agent_runtime.runtime import AgentRuntime
from rynmesh.agent_runtime.store import AgentStore

DEST_MODEL_A = {
    "catalog_model_id": "local:model-a",
    "provider_peer_id": "local_node",
    "source_id": "src-alpha",
    "destination_revision": "rev-a1",
}

DEST_MODEL_B = {
    "catalog_model_id": "local:model-b",
    "provider_peer_id": "local_node",
    "source_id": "src-beta",
    "destination_revision": "rev-b1",
}


class _BoundCatalog:
    def list_models(self) -> list[dict[str, Any]]:
        return [
            {
                "id": DEST_MODEL_A["catalog_model_id"],
                "name": "Model A",
                "source": "local_api",
                "peer_id": DEST_MODEL_A["provider_peer_id"],
                "source_id": DEST_MODEL_A["source_id"],
                "destination_revision": DEST_MODEL_A["destination_revision"],
                "destination_binding_supported": True,
                "available": True,
                "agent_supported": True,
            },
            {
                "id": DEST_MODEL_B["catalog_model_id"],
                "name": "Model B",
                "source": "local_api",
                "peer_id": DEST_MODEL_B["provider_peer_id"],
                "source_id": DEST_MODEL_B["source_id"],
                "destination_revision": DEST_MODEL_B["destination_revision"],
                "destination_binding_supported": True,
                "available": True,
                "agent_supported": True,
            },
        ]


class _BoundModel:
    supports_destination_binding = True

    def __init__(self, *responses: dict[str, Any]) -> None:
        self.responses = list(responses)
        self.calls: list[tuple[str, list[dict[str, Any]], list[dict[str, Any]], dict[str, Any]]] = []

    def complete(
        self,
        model_id: str,
        messages: list[dict[str, Any]],
        tool_specs: list[dict[str, Any]],
        cancelled: Any,
        on_event: Any,
        **options: Any,
    ) -> dict[str, Any]:
        self.calls.append((model_id, messages, tool_specs, options))
        if self.responses:
            return self.responses.pop(0)
        return {"message": {"role": "assistant", "content": "OK"}}


class _NoopTools:
    def specs(self) -> list[dict[str, Any]]:
        return []

    def metadata(self, name: str) -> dict[str, Any]:
        raise ValueError("unavailable")

    def execute(self, name: str, arguments: dict[str, Any], *, operation_id: str, cancelled: Any) -> dict[str, Any]:
        raise ValueError("unavailable")


def _seed_task_row(
    data: dict[str, Any],
    *,
    task_id: str,
    message_id: str,
    text: str,
    destination: dict[str, str] | None,
    model_id: str | None = None,
) -> dict[str, Any]:
    row = {
        "id": task_id,
        "title": "S09-I24 memory test",
        "model_id": model_id if model_id is not None else (destination["catalog_model_id"] if destination else ""),
        "model_destination": copy.deepcopy(destination) if destination is not None else None,
        "status": "running",
        "revision": 1,
        "control_revision": 1,
        "scope_revision": 1,
        "conversation_revision": 0,
        "context_epoch": 0,
        "event_cursor": 0,
        "archived": False,
        "project": None,
        "goal": {"revision": 1, "text": text, "original_message_id": message_id, "constraints": []},
        "messages": [
            {
                "role": "user",
                "message_id": message_id,
                "content": text,
                "context_epoch": 0,
            }
        ],
        "events": [],
    }
    data.setdefault("tasks", {})[task_id] = row
    return row


def test_t1_default_settings_explicit_remember_visible_to_current_model_and_searchable(
    tmp_path: Path,
) -> None:
    """T1 (covers 1, 2): Under default memory settings, '记住我喜欢黑咖啡' binds exposure to the current task's verified destination, sets visible_to_model=True, and a subsequent task on the same model injects and searches it while another model cannot."""
    store = AgentStore(tmp_path, X25519PrivateKey.generate())

    def save_fact(data: dict[str, Any]) -> dict[str, Any]:
        row = _seed_task_row(
            data,
            task_id="task-coffee-save",
            message_id="msg-coffee-1",
            text="记住我喜欢黑咖啡",
            destination=DEST_MODEL_A,
        )
        return memory_runtime.execute_explicit_owner_intent_in_tx(data, row)

    receipt = store.transaction(save_fact)
    assert receipt["applied"] is True
    assert receipt["saved"] is True
    assert receipt["visible_to_model"] is True
    assert len(receipt["entries"]) == 1
    entry = receipt["entries"][0]
    assert entry["exposure_policy"]["mode"] == "destinations"
    assert entry["exposure_policy"]["allowed_destinations"] == [DEST_MODEL_A]

    # Next task on the same verified model destination can inject AND search it via ryn_memory_search
    def verify_next_task_visibility(data: dict[str, Any]) -> tuple[dict[str, Any], list[dict[str, Any]], dict[str, Any], dict[str, Any]]:
        next_same = _seed_task_row(
            data,
            task_id="task-coffee-next",
            message_id="msg-coffee-2",
            text="根据我的喜好推荐一款饮品，我喜欢什么咖啡？",
            destination=DEST_MODEL_A,
        )
        sel = memory.select_memories_for_task(data, next_same)
        ctx_msgs = memory_runtime.format_memory_context_messages(sel)
        search_same = memory_runtime.execute_memory_tool_call_in_tx(
            data,
            next_same,
            memory_runtime.MEMORY_SEARCH_TOOL,
            {"query": "黑咖啡"},
        )
        next_other = _seed_task_row(
            data,
            task_id="task-coffee-other-model",
            message_id="msg-coffee-3",
            text="我喜欢什么咖啡？",
            destination=DEST_MODEL_B,
        )
        search_other = memory_runtime.execute_memory_tool_call_in_tx(
            data,
            next_other,
            memory_runtime.MEMORY_SEARCH_TOOL,
            {"query": "黑咖啡"},
        )
        return sel, ctx_msgs, search_same, search_other

    sel, ctx_msgs, search_same, search_other = store.transaction(verify_next_task_visibility)
    assert len(sel["selected"]) == 1
    assert "黑咖啡" in sel["selected"][0]["text"]
    assert len(ctx_msgs) == 1
    assert "黑咖啡" in ctx_msgs[0]["content"]
    assert len(search_same["entries"]) == 1
    assert "黑咖啡" in search_same["entries"][0]["text"]
    # Does NOT leak to other unselected models
    assert search_other["entries"] == []

    # Also check ryn_memory_propose path uses row destination when default_exposure is empty
    def propose_on_model_a(data: dict[str, Any]) -> dict[str, Any]:
        row_prop = _seed_task_row(
            data,
            task_id="task-propose-tea",
            message_id="msg-tea-1",
            text="顺便记一下我下午常喝乌龙茶",
            destination=DEST_MODEL_A,
        )
        return memory_runtime.execute_memory_tool_call_in_tx(
            data,
            row_prop,
            memory_runtime.MEMORY_PROPOSE_TOOL,
            {
                "operation": "remember",
                "scope_kind": "personal",
                "category": "preference",
                "fact_key": "personal.preference.tea",
                "text": "下午常喝乌龙茶",
            },
        )

    prop_receipt = store.transaction(propose_on_model_a)
    assert prop_receipt["saved"] is True
    assert prop_receipt["visible_to_model"] is True
    assert prop_receipt["entry"]["exposure_policy"]["mode"] == "destinations"
    assert prop_receipt["entry"]["exposure_policy"]["allowed_destinations"] == [DEST_MODEL_A]


def test_t2_task_without_destination_falls_back_to_local_only_and_visible_to_model_false(
    tmp_path: Path,
) -> None:
    """T2 backend (covers 1, 2): When the task has no verified model_destination, explicit remember saves as local_only with saved=True and visible_to_model=False."""
    store = AgentStore(tmp_path, X25519PrivateKey.generate())

    def save_without_dest(data: dict[str, Any]) -> dict[str, Any]:
        row = _seed_task_row(
            data,
            task_id="task-no-dest",
            message_id="msg-no-dest",
            text="记住我喜欢黑咖啡",
            destination=None,
            model_id="legacy-unbound-model",
        )
        return memory_runtime.execute_explicit_owner_intent_in_tx(data, row)

    receipt = store.transaction(save_without_dest)
    assert receipt["applied"] is True
    assert receipt["saved"] is True
    assert receipt["visible_to_model"] is False
    assert len(receipt["entries"]) == 1
    assert receipt["entries"][0]["exposure_policy"] == {
        "mode": "local_only",
        "allowed_destinations": [],
        "revision": 1,
    }


def test_t5_unauthorized_or_unbound_model_sets_skip_reason_and_emits_memory_not_injected_once_per_epoch(
    tmp_path: Path,
) -> None:
    """T5 (covers 6): After switching to an unauthorized model (or unbound model), select_guidance_and_memory populates _memory_skip_reason / excluded_by_exposure, and loop emits memory_not_injected once per (task, context_epoch, reason)."""
    key = X25519PrivateKey.generate()
    model = _BoundModel(
        {"message": {"role": "assistant", "content": "First step reply"}},
        {"message": {"role": "assistant", "content": "Second step reply"}},
    )
    runtime = AgentRuntime(
        AgentStore(tmp_path, key),
        model,
        _NoopTools(),
        _BoundCatalog(),
        consumer_node_id="local_node",
    )
    runtime.update_settings({"enabled": True, "model_id": DEST_MODEL_A["catalog_model_id"]})

    # Seed an active memory authorized ONLY for DEST_MODEL_A
    def seed_memory_for_a(data: dict[str, Any]) -> None:
        memory.upsert_active_entry(
            data,
            {
                "scope": {"kind": "personal"},
                "category": "preference",
                "fact_key": "personal.preference.coffee",
                "text": "喜欢喝黑咖啡",
                "exposure_policy": {"mode": "destinations", "allowed_destinations": [DEST_MODEL_A]},
            },
            origin="manual",
        )

    runtime.store.transaction(seed_memory_for_a)

    # Create a task running on DEST_MODEL_B (valid bound destination, but NOT authorized for the memory)
    runtime.update_settings({"model_id": DEST_MODEL_B["catalog_model_id"]})
    created = runtime.create({"message": "推荐一款咖啡"})
    tid = created["id"]

    live_row = copy.deepcopy(runtime.store.read()["tasks"][tid])
    _guidance, selection = memory_integration.select_guidance_and_memory(runtime, live_row)
    assert selection is not None
    assert selection["selected"] == []
    assert selection["excluded_by_exposure"] == 1
    assert live_row["_memory_skip_reason"] == "excluded_by_exposure"

    # First context build / run_once writes memory_not_injected once
    runtime._context(live_row)
    # Second context build in the same context_epoch=0 must NOT duplicate the event!
    runtime._context(live_row)
    runtime.run_once()

    stored_after_epoch0 = runtime.store.read()["tasks"][tid]
    not_injected_e0 = [
        ev for ev in stored_after_epoch0["events"] if ev.get("type") == "memory_not_injected"
    ]
    assert len(not_injected_e0) == 1
    assert not_injected_e0[0]["context_epoch"] == 0
    assert not_injected_e0[0]["reason"] == "excluded_by_exposure"
    assert not_injected_e0[0]["excluded"] == 1

    # Rotating to context_epoch=1 allows recording once for the new epoch; also verify destination_binding_unsupported reason
    def bump_epoch(data: dict[str, Any]) -> None:
        data["tasks"][tid]["context_epoch"] = 1

    runtime.store.transaction(bump_epoch)
    model.supports_destination_binding = False
    row_epoch1 = copy.deepcopy(runtime.store.read()["tasks"][tid])
    _g2, sel2 = memory_integration.select_guidance_and_memory(runtime, row_epoch1)
    assert sel2 is None
    assert row_epoch1["_memory_skip_reason"] == "destination_binding_unsupported"

    runtime._context(row_epoch1)
    runtime._context(row_epoch1)
    stored_after_epoch1 = runtime.store.read()["tasks"][tid]
    not_injected_e1 = [
        ev
        for ev in stored_after_epoch1["events"]
        if ev.get("type") == "memory_not_injected" and ev.get("context_epoch") == 1
    ]
    assert len(not_injected_e1) == 1
    assert not_injected_e1[0]["reason"] == "destination_binding_unsupported"
