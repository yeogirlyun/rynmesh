"""Exact owner wording must parse into facts, not persist its instruction wrapper.

Pure parser assertion prevents an integration cleanup/rekey from hiding a
foundation defect. The durable path uses genuine Runtime owner messages plus
the real explicit-owner helper in an encrypted AgentStore. No model is called;
no memory entry or source proof is constructed directly by this test.
"""
from __future__ import annotations

import hashlib
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

SAVE = "请记住这三条我的个人信息：我喜欢摄影；我的机身是富士 X-T5；我的镜头是 XF16-55mm F2.8。请分别保存为三条记忆。"
CORRECT = "把我的机身改成富士 X-H2，X-T5 已经卖掉了。请更新已有机身记忆，保留镜头信息。"
FORGET = "忘记我的相机和镜头信息，保留摄影爱好。请实际删除这些器材记忆，并如实说明结果。"
KEYS = {"personal.preference.photography", "personal.gear.camera_body", "personal.gear.lens"}
WRAPPERS = ("这三条我的个人信息", "保存为三条", "请分别", "为三条记忆")


class NoModelCalls:
    supports_destination_binding = True

    def complete(self, *_args, **_kwargs):
        raise AssertionError("Exact owner-wrapper contract uses zero model requests")


class NoTools:
    def specs(self):
        return []

    def metadata(self, _name):
        raise AssertionError("No business tool is available in the owner-wrapper fixture")


class Catalog:
    def list_models(self):
        return [{"id": "local:wrapper-fixture", "name": "Offline exact owner wrapper",
                 "source": "local_api", "source_id": "synthetic-wrapper-source", "destination_revision": "rev1",
                 "available": True, "agent_supported": True, "context_window": 32768}]


@pytest.fixture(autouse=True)
def prohibit_network(monkeypatch):
    def forbidden(*_args, **_kwargs):
        raise AssertionError("Networking is forbidden in the zero-model wrapper contract")
    monkeypatch.setattr(socket.socket, "connect", forbidden)
    monkeypatch.setattr(socket, "create_connection", forbidden)


@pytest.fixture
def node(tmp_path):
    key = X25519PrivateKey.generate()
    store = store_module.AgentStore(tmp_path / "synthetic-node" / "agent", key)
    runtime = runtime_module.AgentRuntime(store, NoModelCalls(), NoTools(), Catalog())
    runtime.update_settings({"enabled": True, "model_id": "local:wrapper-fixture"})
    task = runtime.create({"message": SAVE})
    return runtime, key, task["id"]


def explicit(runtime, task_id):
    return runtime.store.transaction(lambda data: host.execute_explicit_owner_intent_in_tx(data, data["tasks"][task_id]))


def facts(runtime):
    return runtime.store.inspect(memory.list_memories_snapshot)["entries"]


def append_owner(runtime, task_id, text):
    current = runtime.get(task_id)
    return runtime.add_message(task_id, {"message": text, "intent": "information",
        "expected_control_revision": current["control_revision"],
        "expected_goal_revision": current["goal"]["revision"]})


@pytest.mark.parametrize("sentence,intent,expected_keys", [
    (SAVE, "remember", KEYS),
    (CORRECT, "update", {"personal.gear.camera_body"}),
    (FORGET, "forget", {"personal.gear.camera_body", "personal.gear.lens"}),
])
def test_exact_three_owner_sentences_parse_only_their_requested_slots(node, sentence, intent, expected_keys):
    runtime, _key, task_id = node
    row = runtime.store.read()["tasks"][task_id]
    analysis = host.analyze_owner_memory_intent(sentence, row)
    assert analysis["is_explicit"] is True and analysis["intent"] == intent, analysis
    operations = analysis["operations"]
    assert len(operations) == len(expected_keys), ("Headings and tail instructions are not extra facts or delete targets", operations)
    assert {operation["fact_key"] for operation in operations} == expected_keys, operations
    assert all(operation["operation"] == intent for operation in operations)
    assert not any(wrapper in operation.get("text", "") for operation in operations for wrapper in WRAPPERS), operations
    if intent == "update":
        assert "X-H2" in operations[0]["text"] and "X-T5" not in operations[0]["text"]
    if intent == "forget":
        assert not any("爱好" in operation.get("query", "") for operation in operations)


def assert_original_owner_sentence_source(runtime, task_id, sentence):
    row = runtime.store.read()["tasks"][task_id]
    latest = host.latest_authentic_owner_message(row)
    assert latest["content"] == sentence, "Parser must not rewrite the actual owner text to a normalized fact"
    source = host.build_task_owner_source_ref(row)
    assert source["message_id"] == latest["message_id"]
    assert source["task_id"] == task_id and source["conversation_revision"] == 0
    assert source["content_sha256"] == hashlib.sha256(sentence.encode("utf-8")).hexdigest()
    return source


def test_real_store_exact_save_then_full_correction_and_gear_forget_preserves_hobby(node):
    runtime, key, task_id = node
    save_source = assert_original_owner_sentence_source(runtime, task_id, SAVE)
    receipt = explicit(runtime, task_id)
    assert receipt.get("applied") is True and not receipt.get("errors"), receipt
    saved = facts(runtime)
    assert len(saved) == 3 and {entry["fact_key"] for entry in saved} == KEYS, saved
    assert not any(wrapper in entry["text"] for entry in saved for wrapper in WRAPPERS), saved
    mapping = {entry["fact_key"]: entry for entry in saved}
    assert mapping["personal.preference.photography"]["text"] == "喜欢摄影"
    assert "X-T5" in mapping["personal.gear.camera_body"]["text"]
    assert "XF16-55mm F2.8" in mapping["personal.gear.lens"]["text"]
    original = runtime.store.read()["tasks"][task_id]["messages"][0]
    expected_hash = hashlib.sha256(SAVE.encode("utf-8")).hexdigest()
    assert original["role"] == "user" and original["content"] == SAVE
    for entry in saved:
        assert entry["origin"] == "explicit_owner_chat"
        assert all(source["message_id"] == original["message_id"]
                   and source["task_id"] == task_id and source["conversation_revision"] == 0
                   and source["content_sha256"] == expected_hash for source in entry["sources"])
    assert_original_owner_sentence_source(runtime, task_id, SAVE)
    restored = store_module.AgentStore(runtime.store.root, key)
    assert len(restored.inspect(memory.list_memories_snapshot)["entries"]) == 3

    append_owner(runtime, task_id, CORRECT)
    correct_source = assert_original_owner_sentence_source(runtime, task_id, CORRECT)
    corrected_receipt = explicit(runtime, task_id)
    assert corrected_receipt.get("applied") is True and not corrected_receipt.get("errors"), corrected_receipt
    corrected = facts(runtime)
    assert len(corrected) == 3 and {entry["fact_key"] for entry in corrected} == KEYS, corrected
    mapping = {entry["fact_key"]: entry for entry in corrected}
    assert "X-H2" in mapping["personal.gear.camera_body"]["text"]
    assert "X-T5" not in mapping["personal.gear.camera_body"]["text"]
    assert "XF16-55mm F2.8" in mapping["personal.gear.lens"]["text"]
    assert mapping["personal.preference.photography"]["text"] == "喜欢摄影"
    assert all(source["message_id"] == correct_source["message_id"]
               and source["content_sha256"] == correct_source["content_sha256"]
               for source in mapping["personal.gear.camera_body"]["sources"])
    for fact_key in ("personal.preference.photography", "personal.gear.lens"):
        assert all(source["message_id"] == save_source["message_id"]
                   and source["content_sha256"] == save_source["content_sha256"]
                   for source in mapping[fact_key]["sources"])
    assert_original_owner_sentence_source(runtime, task_id, CORRECT)

    append_owner(runtime, task_id, FORGET)
    forget_source = assert_original_owner_sentence_source(runtime, task_id, FORGET)
    forgotten_receipt = explicit(runtime, task_id)
    assert forgotten_receipt.get("applied") is True and not forgotten_receipt.get("errors"), forgotten_receipt
    assert forgotten_receipt["message_id"] == forget_source["message_id"]
    assert_original_owner_sentence_source(runtime, task_id, FORGET)
    final = restored.inspect(memory.list_memories_snapshot)["entries"]
    assert len(final) == 1
    assert {entry["fact_key"] for entry in final} == {"personal.preference.photography"}, final
    assert any(entry["text"] == "喜欢摄影" for entry in final)
    assert not any("XF16-55" in entry["text"] or "X-T5" in entry["text"] or "X-H2" in entry["text"] for entry in final)
    assert all(source["message_id"] == save_source["message_id"]
               and source["content_sha256"] == save_source["content_sha256"]
               for source in final[0]["sources"])
    for path in runtime.store.root.rglob("*.json"):
        content = path.read_bytes()
        assert b"X-T5" not in content and b"X-H2" not in content and b"XF16-55" not in content


@pytest.mark.parametrize("sentence", [
    "请别保存我的机身信息。",
    "不要记住我的机身是富士 X-T5。",
    "请记住我的机身是富士 X-T5，但请别保存到记忆。",
])
def test_true_do_not_save_or_remember_remains_non_mutating(node, sentence):
    runtime, _key, task_id = node
    before = facts(runtime)
    append_owner(runtime, task_id, sentence)
    row = runtime.store.read()["tasks"][task_id]
    source = assert_original_owner_sentence_source(runtime, task_id, sentence)
    analysis = host.analyze_owner_memory_intent(sentence, row)
    assert analysis["is_explicit"] is False and analysis["operations"] == [], analysis
    receipt = explicit(runtime, task_id)
    assert receipt.get("applied") is False, receipt
    assert facts(runtime) == before, "A genuine negative instruction must not mutate any existing fact"
    assert assert_original_owner_sentence_source(runtime, task_id, sentence) == source
