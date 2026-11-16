"""Tests for S09-memfix: distinguish never-configured default_exposure from owner-configured empty allowed_destinations."""
from __future__ import annotations

from pathlib import Path
from typing import Any

from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey

from rynmesh.agent_runtime import memory, memory_runtime
from rynmesh.agent_runtime.store import AgentStore

DEST_CURRENT = {
    "catalog_model_id": "local:model-main",
    "provider_peer_id": "node-self",
    "source_id": "src-main",
    "destination_revision": "rev-main-1",
}


def _seed_task_row(data: dict[str, Any], *, task_id: str, message_id: str, text: str) -> dict[str, Any]:
    row = {
        "id": task_id,
        "title": "S09-memfix test",
        "model_id": DEST_CURRENT["catalog_model_id"],
        "model_destination": dict(DEST_CURRENT),
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


def test_t1_never_configured_default_exposure_grants_current_model_while_explicit_empty_stays_local_only(
    tmp_path: Path,
) -> None:
    """When owner never configured default_exposure (or legacy state lacks the marker), new facts bind to current task model; once owner explicitly configures empty allowed_destinations, new facts stay local_only."""
    store = AgentStore(tmp_path, X25519PrivateKey.generate())

    # 1. Fresh install (never configured default_exposure) -> binds to current model
    def write_when_unconfigured(data: dict[str, Any]) -> dict[str, Any]:
        row = _seed_task_row(data, task_id="t-unconf", message_id="m-unconf", text="记住我喜欢喝绿茶")
        return memory_runtime.execute_explicit_owner_intent_in_tx(data, row)

    r_unconf = store.transaction(write_when_unconfigured)
    assert r_unconf["applied"] is True
    assert r_unconf["visible_to_model"] is True
    assert r_unconf["entries"][0]["exposure_policy"]["mode"] == "destinations"
    assert r_unconf["entries"][0]["exposure_policy"]["allowed_destinations"] == [DEST_CURRENT]

    # 2. Legacy state on disk without default_exposure_configured field -> treated as never configured
    def write_on_legacy_state(data: dict[str, Any]) -> dict[str, Any]:
        data["memory"].pop("default_exposure_configured", None)
        row = _seed_task_row(data, task_id="t-legacy", message_id="m-legacy", text="记住我住在杭州")
        return memory_runtime.execute_explicit_owner_intent_in_tx(data, row)

    r_legacy = store.transaction(write_on_legacy_state)
    assert r_legacy["applied"] is True
    assert r_legacy["visible_to_model"] is True
    assert r_legacy["entries"][0]["exposure_policy"]["mode"] == "destinations"
    assert r_legacy["entries"][0]["exposure_policy"]["allowed_destinations"] == [DEST_CURRENT]

    # 3. Owner explicitly sets default_exposure={'mode': 'selected_models', 'allowed_destinations': []} -> local_only
    def write_after_explicit_empty_exposure(data: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
        memory.update_memory_settings(
            data,
            {"default_exposure": {"mode": "selected_models", "allowed_destinations": []}},
        )
        row_exp = _seed_task_row(data, task_id="t-exp-empty", message_id="m-exp-empty", text="记住我的镜头是 35mm")
        r_chat = memory_runtime.execute_explicit_owner_intent_in_tx(data, row_exp)

        row_prop = _seed_task_row(data, task_id="t-prop-empty", message_id="m-prop-empty", text="我常用机械键盘")
        r_tool = memory_runtime.execute_memory_tool_call_in_tx(
            data,
            row_prop,
            memory_runtime.MEMORY_PROPOSE_TOOL,
            {
                "operation": "remember",
                "scope_kind": "personal",
                "category": "equipment",
                "fact_key": "personal.gear.keyboard",
                "text": "常用机械键盘",
            },
        )
        return r_chat, r_tool

    r_chat, r_tool = store.transaction(write_after_explicit_empty_exposure)
    assert r_chat["applied"] is True
    assert r_chat["visible_to_model"] is False
    assert r_chat["entries"][0]["exposure_policy"]["mode"] == "local_only"
    assert r_chat["entries"][0]["exposure_policy"]["allowed_destinations"] == []

    assert r_tool["saved"] is True
    assert r_tool["visible_to_model"] is False
    assert r_tool["entry"]["exposure_policy"]["mode"] == "local_only"
    assert r_tool["entry"]["exposure_policy"]["allowed_destinations"] == []
