"""Focused outcome wording on actual encrypted Runtime rows; no real models.

Missing/current-turn-unknown receipt is not proof of failure. Failed and success
fixtures use the genuine explicit-owner transaction, never forged receipts.
"""
from __future__ import annotations

import importlib
import os
import socket
import sys
from pathlib import Path

import pytest
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey

sys.dont_write_bytecode = True
REPO = Path(os.environ.get("RYN_MEMORY_REPO", str(Path(__file__).resolve().parents[2]))).resolve()
sys.path.insert(0, str(REPO))
host = importlib.import_module("rynmesh.agent_runtime.memory_runtime")
memory = importlib.import_module("rynmesh.agent_runtime.memory")
runtime_module = importlib.import_module("rynmesh.agent_runtime.runtime")
store_module = importlib.import_module("rynmesh.agent_runtime.store")
for module in (host, memory, runtime_module, store_module):
    assert Path(module.__file__).resolve().is_relative_to(REPO), "Mixed checkout imports"

CLAIM = "已记住你的相机信息，并保存到长期记忆。"
UNCERTAIN = ("未确认", "尚未确认", "无法确认")
FAILED = ("记忆未保存", "保存失败", "未能保存")


class NoModelRequests:
    def complete(self, *_args, **_kwargs):
        raise AssertionError("Guard acceptance must not send any model request")


class NoTools:
    def specs(self):
        return []

    def metadata(self, _name):
        raise AssertionError("Guard acceptance must not inspect business tools")


class Catalog:
    def list_models(self):
        return [{"id": "local:guard-fixture", "name": "Offline guard fixture", "source": "local_api",
                 "available": True, "agent_supported": True, "context_window": 32768}]


@pytest.fixture(autouse=True)
def forbid_network(monkeypatch):
    def forbidden(*_args, **_kwargs):
        raise AssertionError("External networking is forbidden in guard acceptance")
    monkeypatch.setattr(socket.socket, "connect", forbidden)
    monkeypatch.setattr(socket, "create_connection", forbidden)


@pytest.fixture
def runtime(tmp_path):
    store = store_module.AgentStore(tmp_path / "synthetic-node" / "agent", X25519PrivateKey.generate())
    instance = runtime_module.AgentRuntime(store, NoModelRequests(), NoTools(), Catalog())
    instance.update_settings({"enabled": True, "model_id": "local:guard-fixture"})
    return instance


def actual_receipt(runtime, task_id):
    return runtime.store.transaction(lambda data: host.execute_explicit_owner_intent_in_tx(data, data["tasks"][task_id]))


@pytest.mark.parametrize("prior_success", [False, True])
def test_no_current_receipt_reports_unknown_not_a_proven_save_failure(runtime, prior_success):
    authentic_prior_receipt = None
    if prior_success:
        task = runtime.create({"message": "请记住我的机身是富士 X-T5。"})
        receipt = actual_receipt(runtime, task["id"])
        assert receipt["applied"] is True and not receipt["errors"]
        authentic_prior_receipt = dict(receipt)
        current = runtime.get(task["id"])
        runtime.add_message(task["id"], {"message": "请告诉我你当前有没有相机资料。", "intent": "information",
            "expected_control_revision": current["control_revision"],
            "expected_goal_revision": current["goal"]["revision"]})
    else:
        task = runtime.create({"message": "请告诉我你当前有没有相机资料。"})
    row = runtime.store.read()["tasks"][task["id"]]
    # Integration now emits a genuine current-turn no-op receipt for ordinary
    # messages. This guard case deliberately models legacy/missing receipts on
    # the detached read copy, without rewriting canonical state or inventing a
    # successful receipt. The prior-success variant uses the real earlier one.
    if authentic_prior_receipt is None:
        row.pop("memory_last_receipt", None)
    else:
        row["memory_last_receipt"] = authentic_prior_receipt
    latest = host.latest_authentic_owner_message(row)
    last_receipt = row.get("memory_last_receipt") or {}
    assert not (last_receipt.get("message_id") == latest["message_id"] and
                last_receipt.get("conversation_revision") == latest["conversation_revision"]), (
                    "Fixture unexpectedly has a current-turn receipt; unknown status was not established")
    guarded = host.guard_false_memory_save_claim(row, CLAIM)
    assert any(phrase in guarded for phrase in UNCERTAIN), (
        "Missing current receipt only proves the outcome is unconfirmed, not that saving failed", guarded)
    assert not any(phrase in guarded for phrase in FAILED), (
        "Guard declared a known save failure without any current failure receipt", guarded)
    assert guarded != CLAIM


def test_genuine_failed_transaction_can_report_save_failure(runtime):
    runtime.store.transaction(lambda data: memory.update_memory_settings(data, {"budgets": {"max_entries": 4}}))
    for index in range(4):
        runtime.store.transaction(lambda data, i=index: memory.upsert_active_entry(data,
            {"fact_key": f"guard.seed.{i}", "text": f"Synthetic guard seed {i}", "scope": {"kind": "personal"}},
            origin="manual"))
    task = runtime.create({"message": "请记住我的机身是富士 X-T5。"})
    receipt = actual_receipt(runtime, task["id"])
    assert receipt["applied"] is False and "agent_memory_entry_limit" in receipt["errors"], receipt
    row = runtime.store.read()["tasks"][task["id"]]
    guarded = host.guard_false_memory_save_claim(row, CLAIM)
    assert any(phrase in guarded for phrase in FAILED), ("A genuine transaction failure should be reported accurately", guarded)
    assert guarded != CLAIM


def test_genuine_current_success_preserves_original_assistant_sentence(runtime):
    task = runtime.create({"message": "请记住我的机身是富士 X-T5。"})
    receipt = actual_receipt(runtime, task["id"])
    assert receipt["applied"] is True and not receipt["errors"], receipt
    row = runtime.store.read()["tasks"][task["id"]]
    assert host.guard_false_memory_save_claim(row, CLAIM) == CLAIM
