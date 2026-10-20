"""Independent Stage 2 helper acceptance against real Runtime-created owner tasks.

Root runs this file after the writer's marker. Nothing is patched in product code.
The model boundary raises if called; these tests concern existing helper contracts,
not routes/UI/worker integration. All state is encrypted in pytest's tmp_path.
Set RYN_MEMORY_REPO to change the isolated checkout under review.
"""
from __future__ import annotations

import copy
import importlib
import json
import os
from pathlib import Path
import socket
import sys

import pytest
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey

sys.dont_write_bytecode = True
REPO = Path(os.environ.get("RYN_MEMORY_REPO", str(Path(__file__).resolve().parents[2]))).resolve()
sys.path.insert(0, str(REPO))

memory = importlib.import_module("rynmesh.agent_runtime.memory")
host = importlib.import_module("rynmesh.agent_runtime.memory_runtime")
runtime_module = importlib.import_module("rynmesh.agent_runtime.runtime")
store_module = importlib.import_module("rynmesh.agent_runtime.store")
models_module = importlib.import_module("rynmesh.agent_runtime.models")
for module in (memory, host, runtime_module, store_module, models_module):
    assert Path(module.__file__).resolve().is_relative_to(REPO), "Mixed original/delivery imports are forbidden"
AgentRuntime, AgentStore = runtime_module.AgentRuntime, store_module.AgentStore
AgentError, AgentToolRejected = store_module.AgentError, store_module.AgentToolRejected


class NoModelCalls:
    supports_destination_binding = True

    def complete(self, *_args, **_kwargs):
        raise AssertionError("Helper-only acceptance unexpectedly called a model")


class NoBusinessTools:
    def specs(self):
        return []

    def metadata(self, _name):
        raise AssertionError("Helper-only acceptance unexpectedly inspected a business tool")


class Catalog:
    def list_models(self):
        return [{"id": "local:test", "name": "Offline synthetic model", "source": "local_api",
                 "source_id": "memory-helper-fixture", "destination_revision": "rev1",
                 "available": True, "agent_supported": True}]


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def forbidden(*_args, **_kwargs):
        raise AssertionError("Network is forbidden in helper acceptance")
    monkeypatch.setattr(socket.socket, "connect", forbidden)
    monkeypatch.setattr(socket, "create_connection", forbidden)


@pytest.fixture
def node(tmp_path):
    key = X25519PrivateKey.generate()
    runtime = AgentRuntime(AgentStore(tmp_path / "node", key), NoModelCalls(), NoBusinessTools(), Catalog())
    runtime.update_settings({"enabled": True, "model_id": "local:test"})
    return runtime, key


def owner_task(runtime, text, *, project_id=None):
    value = {"message": text}
    if project_id is not None:
        value["project_id"] = project_id
    task = runtime.create(value)
    row = runtime.store.read()["tasks"][task["id"]]
    assert row["messages"][0]["role"] == "user"
    assert row["messages"][0].get("message_id"), "Original Runtime must author a real stable owner-message identity"
    return task["id"]


def explicit(runtime, task_id):
    return runtime.store.transaction(lambda data: host.execute_explicit_owner_intent_in_tx(data, data["tasks"][task_id]))


def propose(runtime, task_id, arguments):
    return runtime.store.transaction(lambda data: host.execute_memory_tool_call_in_tx(
        data, data["tasks"][task_id], host.MEMORY_PROPOSE_TOOL, arguments))


def active(runtime):
    return runtime.store.inspect(memory.list_memories_snapshot)["entries"]


def admin_entry(runtime, *, key, text, scope=None, exposure=None):
    payload = {"fact_key": key, "text": text, "category": "equipment", "scope": scope or {"kind": "personal"}}
    if exposure is not None:
        payload["exposure_policy"] = exposure
    return runtime.store.transaction(lambda data: memory.upsert_active_entry(data, payload))["entry"]


def project(runtime, tmp_path, name):
    root = tmp_path / name
    root.mkdir()
    return runtime.load_project({"path": str(root)})


def test_real_runtime_owner_photo_save_is_idempotent_encrypted_and_restart_safe(node):
    runtime, key = node
    task_id = owner_task(runtime, "请记住我喜欢摄影，我的机身是富士 X-T5，镜头是 XF16-55mm F2.8")
    row_before = runtime.store.read()["tasks"][task_id]
    receipt = explicit(runtime, task_id)
    assert receipt["applied"] is True and not receipt.get("errors")
    facts = active(runtime)
    text = json.dumps(facts, ensure_ascii=False)
    for expected in ("喜欢摄影", "X-T5", "XF16-55mm"):
        assert expected in text
    assert all(e["origin"] == "explicit_owner_chat" for e in facts)
    assert all(s["author_role"] == "owner" for e in facts for s in e["sources"])
    assert runtime.store.read()["tasks"][task_id]["messages"] == row_before["messages"]
    ids = {e["id"] for e in facts}
    assert explicit(runtime, task_id) == receipt
    assert {e["id"] for e in active(runtime)} == ids
    restored = AgentStore(runtime.store.root, key)
    assert {e["id"] for e in restored.inspect(memory.list_memories_snapshot)["entries"]} == ids
    for file in runtime.store.root.rglob("*.json"):
        disk = file.read_text(encoding="utf-8")
        assert "X-T5" not in disk and "XF16-55mm" not in disk


def test_owner_update_changes_only_camera_and_forget_changes_only_lens(node, monkeypatch):
    runtime, _ = node
    # This case verifies unrelated-source survival for an authorized recipient.
    # Give the fixture catalog its real synthetic node identity, then explicitly
    # authorize that exact verified destination; an empty allow-list now denies.
    catalog_rows = runtime.catalog.list_models()
    for catalog_row in catalog_rows:
        catalog_row["peer_id"] = runtime.consumer_node_id
    monkeypatch.setattr(runtime.catalog, "list_models", lambda: copy.deepcopy(catalog_rows))
    destination = runtime._current_destination("local:test", require_bound=True)
    runtime.store.transaction(lambda data: memory.update_memory_settings(data, {
        "default_exposure": {"mode": "selected_models", "allowed_destinations": [destination]}}))
    first = owner_task(runtime, "请记住我喜欢摄影，我的机身是富士 X-T5，镜头是 XF16-55mm F2.8")
    assert explicit(runtime, first)["applied"] is True
    second = owner_task(runtime, "请把我的机身改成富士 X-H2")
    assert explicit(runtime, second)["applied"] is True
    facts = active(runtime)
    body = [e for e in facts if e["fact_key"].endswith("gear.camera_body")]
    assert len(body) == 1 and "X-H2" in body[0]["text"] and "X-T5" not in body[0]["text"]
    assert any("XF16-55mm" in e["text"] for e in facts)
    selection = runtime.store.inspect(lambda data: memory.select_memories_for_task(
        data, data["tasks"][second], query_text="摄影 器材 机身 镜头"))
    assert "XF16-55mm" in json.dumps(selection["selected"], ensure_ascii=False), "Camera correction must not fence unrelated lens from the same old owner message"
    third = owner_task(runtime, "请忘记我的镜头")
    assert explicit(runtime, third)["applied"] is True
    facts = active(runtime)
    assert not any("XF16-55mm" in e["text"] for e in facts)
    assert any("X-H2" in e["text"] for e in facts)
    assert any("喜欢摄影" in e["text"] for e in facts)


@pytest.mark.parametrize("scope_arg", [None, "personal"])
def test_model_cannot_promote_host_project_fact_to_personal(node, tmp_path, scope_arg):
    runtime, _ = node
    p = project(runtime, tmp_path, "project-a")
    task_id = owner_task(runtime, "请记住本项目使用 PostgreSQL", project_id=p["id"])
    args = {"operation": "remember", "text": "本项目使用 PostgreSQL", "fact_key": "project.database"}
    if scope_arg is not None:
        args["scope_kind"] = scope_arg
    try:
        result = propose(runtime, task_id, args)
    except AgentToolRejected:
        result = {"saved": False}
    facts = active(runtime)
    assert not any(e["scope"]["kind"] == "personal" and "PostgreSQL" in e["text"] for e in facts)
    if result.get("saved"):
        assert result["entry"]["scope"]["kind"] == "project"
        assert result["entry"]["scope"]["project_id"] == p["id"]


def test_model_forget_arguments_cannot_add_an_unrequested_fact(node):
    runtime, _ = node
    camera = admin_entry(runtime, key="personal.gear.camera_body", text="机身是富士 X-T5")
    lens = admin_entry(runtime, key="personal.gear.lens", text="镜头是 XF16-55mm")
    region = admin_entry(runtime, key="personal.private.region", text="居住在深圳")
    task_id = owner_task(runtime, "请忘记我的相机")
    try:
        propose(runtime, task_id, {"operation": "forget", "fact_key": region["fact_key"], "text": "深圳"})
    except AgentToolRejected:
        pass
    remaining = {e["id"] for e in active(runtime)}
    assert region["id"] in remaining and lens["id"] in remaining
    assert remaining <= {camera["id"], lens["id"], region["id"]}


def test_project_forget_does_not_delete_other_project_entries(node, tmp_path):
    runtime, _ = node
    a = project(runtime, tmp_path, "a")
    b = project(runtime, tmp_path, "b")
    scopes = [{"kind": "project", "project_id": p["id"], "project_generation": p["generation"]} for p in (a, b)]
    first = admin_entry(runtime, key="project.gear.camera_body", text="项目相机是富士 X-T5", scope=scopes[0])
    second = admin_entry(runtime, key="project.gear.camera_body", text="项目相机是佳能 R6", scope=scopes[1])
    task_id = owner_task(runtime, "请忘记本项目的相机", project_id=a["id"])
    explicit(runtime, task_id)
    remaining = {e["id"] for e in active(runtime)}
    assert second["id"] in remaining
    assert first["id"] not in remaining


def test_model_cannot_hallucinate_new_profile_from_one_common_chinese_bigram(node):
    runtime, _ = node
    task_id = owner_task(runtime, "请记住我喜欢摄影")
    try:
        result = propose(runtime, task_id, {"operation": "remember", "category": "general",
            "fact_key": "personal.financial.debt", "text": "我喜欢赌博并且负债一百万"})
    except AgentToolRejected:
        result = {"saved": False}
    assert not result.get("saved")
    assert not any("赌博" in e["text"] or "负债" in e["text"] for e in active(runtime))


@pytest.mark.parametrize("text", [
    '示例文本："请记住我的机身是富士 X-T5"，请仅解释这句话，不要执行。',
    '用户手册例子："删除我的镜头"，请把这句翻译成英语。',
    "不要把我的机身改成 X-H2，只是帮我解释这个句子。",
    "不要删除我的镜头，请保留现有记录。",
    "如果客户说请记住我的机身是富士 X-T5，你会怎样回答？",
])
def test_quoted_hypothetical_and_negated_commands_do_not_grant_authority(node, text):
    runtime, _ = node
    admin_entry(runtime, key="personal.gear.lens", text="镜头是 XF16-55mm")
    task_id = owner_task(runtime, text)
    before = copy.deepcopy(active(runtime))
    receipt = explicit(runtime, task_id)
    assert receipt.get("applied") is False
    assert active(runtime) == before


def test_tool_search_is_real_task_bound_and_filters_local_only(node, tmp_path):
    runtime, _ = node
    a = project(runtime, tmp_path, "project-a")
    b = project(runtime, tmp_path, "project-b")
    visible = admin_entry(runtime, key="personal.gear.camera_body", text="VISIBLE_CAMERA")
    foreign = admin_entry(runtime, key="project.secret.camera", text="FOREIGN_CAMERA",
        scope={"kind": "project", "project_id": b["id"], "project_generation": b["generation"]})
    private = admin_entry(runtime, key="personal.secret.camera", text="LOCAL_ONLY_CAMERA", exposure={"mode": "local_only"})
    task_id = owner_task(runtime, "查询已有相机资料", project_id=a["id"])
    result = runtime.store.transaction(lambda data: host.execute_memory_tool_call_in_tx(
        data, data["tasks"][task_id], host.MEMORY_SEARCH_TOOL, {"query": "CAMERA", "limit": 12}))
    ids = {e["id"] for e in result["entries"]}
    assert visible["id"] in ids
    assert foreign["id"] not in ids and private["id"] not in ids


def test_late_limit_failure_does_not_commit_partial_mutation(node):
    runtime, _ = node
    runtime.store.transaction(lambda data: memory.update_memory_settings(data, {"budgets": {"max_entries": 8}}))
    for i in range(8):
        admin_entry(runtime, key="personal.seed." + str(i), text="Synthetic seed " + str(i))
    task_id = owner_task(runtime, "请记住我的机身是富士 X-T5，镜头是 XF16-55mm F2.8")
    before = copy.deepcopy(runtime.store.read()["memory"])
    try:
        receipt = explicit(runtime, task_id)
    except AgentError as exc:
        assert str(exc) == "agent_memory_entry_limit", "The real transaction must reach the intended capacity failure, not an unrelated interface failure"
    else:
        assert receipt.get("applied") is False
        assert "agent_memory_entry_limit" in json.dumps(receipt, ensure_ascii=False)
    assert runtime.store.read()["memory"] == before, "An error receipt must not persist the already-mutated over-limit memory dictionary"


def test_selector_result_formats_as_low_priority_assistant_data(node):
    runtime, _ = node
    admin_entry(runtime, key="personal.gear.camera_body", text="相机是富士 X-T5")
    task_id = owner_task(runtime, "已有相机是什么")
    selection = runtime.store.inspect(lambda data: memory.select_memories_for_task(data, data["tasks"][task_id], query_text="相机"))
    assert selection["selected"]
    messages = host.format_memory_context_messages(selection)
    assert len(messages) == 1 and messages[0]["role"] == "assistant"
    assert "X-T5" in messages[0]["content"]
    assert "lower priority" in messages[0]["content"]


def test_extractor_reads_real_job_manifest_and_rejects_non_strict_json(node):
    runtime, _ = node
    task_id = owner_task(runtime, "我的机身是富士 X-T5")
    def enqueue(data):
        row = data["tasks"][task_id]
        destination = row["model_destination"]
        memory.update_memory_settings(data, {"capture_enabled": True, "extraction_destination": destination})
        return memory.enqueue_extraction_job(data, task_id=task_id)["job"]
    job = runtime.store.transaction(enqueue)
    row = runtime.store.read()["tasks"][task_id]
    prompt = host.build_extraction_messages(job)
    owner_lines = json.loads(prompt[-1]["content"])["owner_messages"]
    assert owner_lines and "X-T5" in json.dumps(owner_lines, ensure_ascii=False)
    msg_id = row["messages"][0]["message_id"]
    valid = json.dumps({"candidates": [{"category": "equipment", "fact_key": "personal.gear.camera_body",
        "text": "机身是富士 X-T5", "keywords": [], "source_message_ids": [msg_id]}]}, ensure_ascii=False)
    candidates = host.parse_extraction_response(valid, job, row)
    assert len(candidates) == 1 and candidates[0]["source_message_ids"] == [msg_id]
    before = copy.deepcopy(runtime.store.read())
    empty = runtime.store.transaction(lambda data: host.parse_extraction_response(
        '{"candidates": []}', data["memory"]["jobs"][job["id"]], data["tasks"][task_id]))
    assert empty == [], "Valid empty candidates must be a successful parser no-op"
    assert runtime.store.read() == before
    assert not before["memory"]["candidates"]
    for invalid in ("not JSON", "```json\n" + valid + "\n```", valid + " trailing prose"):
        with pytest.raises(ValueError, match="^invalid_extraction_json$"):
            runtime.store.transaction(lambda data: host.parse_extraction_response(
                invalid, data["memory"]["jobs"][job["id"]], data["tasks"][task_id]))
        assert runtime.store.read() == before, "Invalid extraction must not mutate the real store or create candidates"


def test_failed_or_unconfirmed_current_memory_save_cannot_keep_a_positive_claim(node):
    runtime, _ = node
    task_id = owner_task(runtime, "请记住我的机身是富士 X-T5")
    row = runtime.store.read()["tasks"][task_id]
    latest = host.latest_authentic_owner_message(row)
    assert latest is not None
    affirmative = "已保存到记忆：你的机身是富士 X-T5。"
    failed = copy.deepcopy(row)
    failed["memory_last_receipt"] = {
        "applied": False, "intent": "remember", "task_id": task_id,
        "message_id": latest["message_id"], "conversation_revision": latest["conversation_revision"],
        "errors": ["agent_memory_entry_limit"],
    }
    guarded = host.guard_false_memory_save_claim(failed, affirmative)
    assert "已保存到记忆" not in guarded, "An error prefix followed by the original positive claim is still contradictory"
    assert affirmative not in guarded
    unconfirmed = copy.deepcopy(row)
    unconfirmed.pop("memory_last_receipt", None)
    unconfirmed["memory_explicit_receipts"] = {}
    guarded = host.guard_false_memory_save_claim(unconfirmed, "已经记住你的设备是富士 X-T5。")
    assert "已经记住你的设备" not in guarded, "A current owner request without a confirmed current receipt cannot be presented as persisted"


def test_old_memory_receipts_cannot_back_new_claims_or_rewrite_file_save_text(node):
    runtime, _ = node
    task_id = owner_task(runtime, "请记住我的机身是富士 X-T5")
    assert explicit(runtime, task_id)["applied"] is True
    first_row = runtime.store.read()["tasks"][task_id]
    old_success = copy.deepcopy(first_row["memory_last_receipt"])
    old_owner = host.latest_authentic_owner_message(first_row)
    assert old_owner is not None
    confirmed = "已保存到记忆：你的机身是富士 X-T5。"
    assert host.guard_false_memory_save_claim(first_row, confirmed) == confirmed

    # Append through the original owner API; no model dispatch or synthetic user
    # insertion. Then supply controlled stale receipt metadata to this pure guard.
    runtime.action(task_id, {"action": "pause"})
    paused = runtime.get(task_id)
    runtime.add_message(task_id, {"message": "请记住我的机身是富士 X-H2", "expected_revision": paused["revision"]})
    current = runtime.store.read()["tasks"][task_id]
    new_owner = host.latest_authentic_owner_message(current)
    assert new_owner is not None and new_owner["message_id"] != old_owner["message_id"]
    current["memory_last_receipt"] = old_success
    current["memory_explicit_receipts"] = {
        f"{old_owner['message_id']}:r{old_owner['conversation_revision']}": old_success,
    }
    guarded = host.guard_false_memory_save_claim(current, "已保存到记忆：你的设备已改为富士 X-H2。")
    assert "已保存到记忆" not in guarded, "An old success cannot attest a new owner-message save"

    runtime.action(task_id, {"action": "pause"})
    paused = runtime.get(task_id)
    runtime.add_message(task_id, {"message": "把本轮内容保存成 report.txt", "expected_revision": paused["revision"]})
    file_row = runtime.store.read()["tasks"][task_id]
    old_failure = {"saved": False, "status": "error", "operation": "remember", "task_id": task_id,
                   "message_id": old_owner["message_id"], "conversation_revision": old_owner["conversation_revision"],
                   "reason": "agent_memory_entry_limit"}
    file_row["memory_last_receipt"] = old_failure
    file_row["memory_explicit_receipts"] = {
        f"{old_owner['message_id']}:r{old_owner['conversation_revision']}": old_failure,
    }
    for file_claim in ("已保存文件 report.txt。", "文件已保存到 report.txt。"):
        assert host.guard_false_memory_save_claim(file_row, file_claim) == file_claim, "A stale memory failure must not rewrite a non-memory file receipt"


def test_real_native_catalog_destination_allows_an_exact_empty_source_id(node):
    runtime, _ = node
    native_row = {"id": "local/codex-cli", "source": "local_service", "adapter": "codex_cli",
                  "runtime": "external", "peer_id": "native-provider-fixture", "source_id": "",
                  "destination_revision": "native-binding-rev1"}
    destination = models_module.ModelCatalog.destination(native_row)
    assert destination == {"catalog_model_id": "local/codex-cli", "provider_peer_id": "native-provider-fixture",
                           "source_id": "", "destination_revision": "native-binding-rev1"}
    assert memory.destination_matches(destination, destination), "Empty native source_id is an exact identity value, not an absent provider or revision"
    policy = {"mode": "destinations", "allowed_destinations": [destination]}
    entry = admin_entry(runtime, key="personal.gear.camera_body", text="NATIVE_BOUND_CAMERA", exposure=policy)
    result = runtime.store.inspect(lambda data: memory.search_memories(
        data, query="NATIVE_BOUND_CAMERA", destination=destination, enforce_destination=True))
    assert entry["id"] in {item["id"] for item in result["entries"]}
    changed_source = {**destination, "source_id": "different-source"}
    assert not memory.destination_matches(destination, changed_source), "Empty source_id must not become a wildcard"


def test_real_native_catalog_destination_without_revision_is_rejected(node):
    runtime, _ = node
    native_row = {"id": "local/codex-cli", "source": "local_service", "adapter": "codex_cli",
                  "runtime": "external", "peer_id": "native-provider-fixture", "source_id": "",
                  "destination_revision": "native-binding-rev1"}
    bound = models_module.ModelCatalog.destination(native_row)
    incomplete_row = {key: value for key, value in native_row.items() if key != "destination_revision"}
    incomplete = models_module.ModelCatalog.destination(incomplete_row)
    assert incomplete["provider_peer_id"] and incomplete["source_id"] == ""
    assert incomplete["destination_revision"] == ""
    assert not memory.destination_matches(incomplete, incomplete), "A missing revision cannot authorize itself even when provider and model match"
    entry = admin_entry(runtime, key="personal.gear.camera_body", text="REVISION_BOUND_CAMERA",
                        exposure={"mode": "destinations", "allowed_destinations": [bound]})
    result = runtime.store.inspect(lambda data: memory.search_memories(
        data, query="REVISION_BOUND_CAMERA", destination=incomplete, enforce_destination=True))
    assert entry["id"] not in {item["id"] for item in result["entries"]}
