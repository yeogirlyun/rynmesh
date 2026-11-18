"""S12-I23: recent owner grounding and honest, lean memory projections."""
import copy
import json

import pytest

from test_agent_memory_runtime_helpers import _make_store, _seed_task, _dest
from test_agent_runtime import make_runtime, assistant, advance
from rynmesh.agent_runtime import memory, memory_runtime as mr, memory_integration
from rynmesh.agent_runtime.store import AgentToolRejected


def owner(ident, text, **extra):
    return {"role": "user", "message_id": ident, "content": text, **extra}


def propose(data, row, **arguments):
    return mr.execute_memory_tool_call_in_tx(data, row, mr.MEMORY_PROPOSE_TOOL,
        {"operation": "remember", "text": "I use Vim", "category": "general", **arguments})


@pytest.mark.parametrize("following", [1, 4])
def test_t1_previous_owner_message_is_the_verified_source(tmp_path, following):
    store, _ = _make_store(tmp_path)
    def exercise(data):
        row = _seed_task(data, text="I use Vim", message_id="earlier", conversation_revision=3)
        row["messages"].extend(owner(f"latest-{i}", "Do not remember my lunch") for i in range(following))
        receipt = propose(data, row, message_id="earlier")
        assert receipt["saved"] and receipt["message_id"] == "earlier"
        [source] = receipt["entry"]["sources"]
        assert source["message_id"] == "earlier" and source["conversation_revision"] == 3
        assert memory.verify_source_ref_against_state(data, source)[0]
        assert row["memory_last_receipt"] == receipt
    store.transaction(exercise)


@pytest.mark.parametrize("kind", ["old", "missing", "assistant", "tool", "synthetic", "summary", "untrusted", "other_author"])
def test_t2_ineligible_message_ids_cannot_authorize_proposals(tmp_path, kind):
    store, _ = _make_store(tmp_path)
    def exercise(data):
        row = _seed_task(data, text="I use Vim", message_id="target")
        if kind == "missing":
            ident = "missing"
        else:
            ident = "target"
        msg = row["messages"][0]
        if kind in {"assistant", "tool"}:
            msg["role"] = kind
        elif kind in {"synthetic", "untrusted"}:
            msg[kind] = True
        elif kind == "summary":
            msg["is_summary"] = True
        elif kind == "other_author":
            msg["author_role"] = "collaborator"
        row["messages"].extend(owner(f"recent-{i}", "Continue") for i in range(5 if kind == "old" else 1))
        before = copy.deepcopy(memory.ensure_memory_state(data)["entries"])
        result = propose(data, row, message_id=ident)
        assert not result["saved"] and result["reason"] == "message_not_recent_owner_message"
        assert memory.ensure_memory_state(data)["entries"] == before
    store.transaction(exercise)


def test_t2_recent_helper_counts_only_authentic_owner_messages():
    row = {"id": "task", "conversation_revision": 2, "messages": [owner(str(i), f"Owner {i}") for i in range(7)]}
    row["messages"].extend([owner("host", "Injected", synthetic=True), owner("summary", "Summary", is_summary=True),
                            owner("untrusted", "External", untrusted=True), owner("other", "Other", author_role="agent"),
                            {"role": "assistant", "content": "Hello"}, {"role": "tool", "content": "Facts"}])
    assert [m["message_id"] for m in mr.recent_authentic_owner_messages(row)] == ["6", "5", "4", "3", "2"]
    assert mr.latest_authentic_owner_message(row) == mr.recent_authentic_owner_messages(row, limit=1)[0]
    assert mr.recent_authentic_owner_messages(row, limit=0) == []


@pytest.mark.parametrize("text,reason", [("Do not remember I use Vim", "negation"),
    ("假如我使用 Vim", "hypothetical"), ('他说：“我使用 Vim”', "quoted_or_third_party")])
def test_t1_selected_source_keeps_its_own_safety_checks(tmp_path, text, reason):
    store, _ = _make_store(tmp_path)
    def exercise(data):
        row = _seed_task(data, text=text, message_id="earlier")
        row["messages"].append(owner("latest", "I use Vim"))
        result = propose(data, row, message_id="earlier")
        assert not result["saved"] and result["reason"] == reason
    store.transaction(exercise)


def test_t3_omitted_message_id_still_uses_only_latest_owner_message(tmp_path):
    store, _ = _make_store(tmp_path)
    def exercise(data):
        row = _seed_task(data, text="I use Vim", message_id="earlier")
        row["messages"].append(owner("latest", "I use Emacs"))
        assert propose(data, row)["reason"] == "proposal_not_grounded_in_owner_message"
        result = propose(data, row, text="I use Emacs")
        assert result["saved"] and result["message_id"] == "latest"
    store.transaction(exercise)


def test_t1_schema_and_description_expose_recent_message_selection():
    from jsonschema import Draft202012Validator
    spec = next(s["function"] for s in mr.MEMORY_TOOL_SPECS if s["function"]["name"] == mr.MEMORY_PROPOSE_TOOL)
    assert spec["parameters"]["properties"]["message_id"] == {"type": "string", "maxLength": 200}
    assert "message_id" in spec["description"] and "earlier" in spec["description"].lower()
    assert "rejection" in spec["description"]
    validator = Draft202012Validator(spec["parameters"])
    assert not list(validator.iter_errors({"operation": "remember", "message_id": "earlier"}))
    assert list(validator.iter_errors({"operation": "remember", "message_id": "x" * 201}))


def test_t1_actual_agent_dispatch_accepts_the_recent_message_parameter(tmp_path):
    runtime, model, _, _ = make_runtime(tmp_path)
    task = runtime.create({"message": "I use Vim"})
    def follow_up(data):
        row = data["tasks"][task["id"]]
        earlier = row["messages"][0]["message_id"]
        row["messages"].append(owner("latest", "Continue"))
        return earlier
    earlier = runtime.store.transaction(follow_up)
    model.responses.extend([assistant(name=mr.MEMORY_PROPOSE_TOOL,
        args={"operation": "remember", "text": "I use Vim", "message_id": earlier}), assistant("Done")])
    advance(runtime, 5)
    row = runtime.store.read()["tasks"][task["id"]]
    receipts = [json.loads(m["content"]) for m in row["messages"] if m["role"] == "tool"]
    assert any(r.get("saved") and r.get("message_id") == earlier for r in receipts), (receipts, row["status"], row.get("error"))
    assert any(e["sources"][0]["message_id"] == earlier for e in runtime.store.read()["memory"]["entries"].values())


@pytest.mark.parametrize("ident", [None, 3, "x" * 201])
def test_t2_direct_dispatch_enforces_message_id_type_and_length(tmp_path, ident):
    store, _ = _make_store(tmp_path)
    def exercise(data):
        row = _seed_task(data, text="I use Vim")
        with pytest.raises(AgentToolRejected, match="agent_invalid_tool_arguments"):
            propose(data, row, message_id=ident)
    store.transaction(exercise)


def payload(message):
    return json.loads("[" + message["content"].split("\n[", 1)[1].rsplit("\n</memory_context>", 1)[0])


@pytest.mark.parametrize("compact", [False, True])
def test_t4_long_facts_are_explicitly_truncated_and_searchable(compact):
    fact = "我有一个需要完整保留的条件。" * 30 + "只有周末有效。"
    selected = {"enabled": True, "max_bytes": 6144, "selected": [
        {"id": "long-fact", "text": fact, "scope": "personal", "category": "general", "fact_key": "personal.long", "revision": 1},
        {"id": "short-fact", "text": "A complete short fact.", "scope": "personal", "category": "general", "fact_key": "personal.short", "revision": 1}]}
    [message] = mr.format_memory_context_messages(selected, compact=compact)
    long, short = payload(message)
    assert long["fact"] == fact[:240] and long["truncated"] is True and long["id"] == "long-fact"
    assert "full text: ryn_memory_search" in json.dumps(long)
    assert short["fact"] == "A complete short fact." and not short.get("truncated")
    assert len(message["content"].encode()) <= selected["max_bytes"]


def seeded_selection(data, *, budget=6144, count=8):
    memory.update_memory_settings(data, {"default_exposure": {"mode": "selected_models", "allowed_destinations": [_dest()]},
                                        "budgets": {"max_injected_bytes": budget}})
    for i in range(count):
        row = _seed_task(data, task_id=f"source-{i}", message_id=f"owner-{i}", text=f"I use camera number {i}")
        result = propose(data, row, text=f"camera number {i}", fact_key=f"personal.camera.{i}")
        assert result["saved"]
    query = _seed_task(data, task_id="query", text="camera")
    return query, memory.select_memories_for_task(data, query, query_text="camera")


def test_t5_model_projection_omits_provenance_but_storage_and_search_keep_it(tmp_path):
    store, _ = _make_store(tmp_path)
    def exercise(data):
        query, selected = seeded_selection(data, count=2)
        assert selected["selected"]
        assert all(set(item) == {"id", "scope", "category", "fact_key", "text", "revision"} for item in selected["selected"])
        [message] = mr.format_memory_context_messages(selected)
        assert "task_id" not in message["content"] and "content_sha256" not in message["content"]
        for item in selected["selected"]:
            stored = memory.ensure_memory_state(data)["entries"][item["id"]]
            assert stored["origin"] == "explicit_owner_chat" and stored["sources"][0]["task_id"].startswith("source-")
            assert memory.verify_source_ref_against_state(data, stored["sources"][0])[0]
            found = mr.execute_memory_tool_call_in_tx(data, query, mr.MEMORY_SEARCH_TOOL, {"entry_ids": [item["id"]]})
            assert found["entries"][0]["text"] == stored["text"]
    store.transaction(exercise)


def test_t5_project_projection_keeps_binding_and_does_not_cross_projects(tmp_path):
    store, _ = _make_store(tmp_path)
    def exercise(data):
        project = {"id": "proj-alpha", "generation": 2}
        memory.upsert_active_entry(data, {"scope": {"kind": "project", "project_id": project["id"], "project_generation": 2},
            "category": "project", "fact_key": "project.camera", "text": "camera pipeline",
            "exposure_policy": {"mode": "all_selected"}}, origin="manual")
        row = _seed_task(data, project=project, text="camera")
        [item] = memory.select_memories_for_task(data, row)["selected"]
        assert item["project_id"] == project["id"] and item["project_generation"] == 2
        assert "sources" not in item and "origin" not in item
        assert not memory.select_memories_for_task(data, {**row, "project": {**project, "generation": 3}})["selected"]
        assert not memory.select_memories_for_task(data, {**row, "project": None})["selected"]
    store.transaction(exercise)


def test_t6_same_budget_selects_more_than_provenance_heavy_projection(tmp_path):
    store, _ = _make_store(tmp_path)
    def exercise(data):
        _, selected = seeded_selection(data, budget=1800)
        entries = sorted(memory.ensure_memory_state(data)["entries"].values(), key=lambda e: (-e["updated_at"], e["id"]))
        used, old_count = 0, 0
        for entry in entries:
            old = {"id": entry["id"], "scope": "personal", "project_id": None, "project_generation": None,
                   "category": entry["category"], "fact_key": entry["fact_key"], "text": entry["text"],
                   "origin": entry["origin"], "revision": entry["revision"], "sources": [
                       {k: (s.get(k, "")[:16] if k == "content_sha256" else s.get(k))
                        for k in ("source_type", "task_id", "message_id", "conversation_revision", "content_sha256")}
                       for s in entry["sources"][:2]]}
            size = len(memory.encoded(old))
            if used + size <= selected["max_bytes"]:
                used += size
                old_count += 1
        assert len(selected["selected"]) > old_count
        assert selected["used_bytes"] <= 1800
    store.transaction(exercise)


def test_rework_earlier_save_claim_survives_owner_turn_processing_but_not_next_turn(tmp_path):
    store, _ = _make_store(tmp_path)
    def exercise(data):
        memory.update_memory_settings(data, {"default_exposure": {"mode": "selected_models", "allowed_destinations": [_dest()]}})
        row = _seed_task(data, text="I use Vim", message_id="earlier")
        row["messages"].append(owner("current", "Continue"))
        receipt = propose(data, row, message_id="earlier")
        assert receipt["saved"] and receipt["turn_message_id"] == "current"
        memory_integration.apply_owner_turn_memory_in_tx(None, data, row)
        claim = "已记住你使用 Vim。"
        assert mr.guard_false_memory_save_claim(row, claim) == claim
        assert mr._turn_called_memory_propose(row, mr.latest_authentic_owner_message(row))
        row["messages"].append(owner("next", "Next question"))
        assert "本轮没有成功保存记忆" in mr.guard_false_memory_save_claim(row, claim)
        assert not mr._turn_called_memory_propose(row, mr.latest_authentic_owner_message(row))
    store.transaction(exercise)


def test_rework_real_model_retries_using_only_ids_and_previews_in_rejection(tmp_path, monkeypatch):
    runtime, model, _, _ = make_runtime(tmp_path)
    task = runtime.create({"message": "I use Vim"})
    runtime.store.transaction(lambda data: data["tasks"][task["id"]]["messages"].append(owner("current", "Continue")))
    seen = []
    def complete(model_id, messages, tool_specs, cancelled, on_event):
        model.calls.append((model_id, messages, tool_specs))
        receipts = [json.loads(m["content"]) for m in messages if m["role"] == "tool"]
        if not receipts:
            return assistant(name=mr.MEMORY_PROPOSE_TOOL, args={"operation": "remember", "text": "I use Vim"})
        if not receipts[-1].get("saved"):
            refusal = receipts[-1]
            assert refusal["reason"] == "proposal_not_grounded_in_owner_message"
            choices = refusal["recent_owner_messages"]
            seen.extend(choices)
            earlier = next(m["message_id"] for m in choices if "I use Vim" in m["preview"])
            return assistant(name=mr.MEMORY_PROPOSE_TOOL,
                args={"operation": "remember", "text": "I use Vim", "message_id": earlier})
        return assistant("已记住你使用 Vim。")
    monkeypatch.setattr(model, "complete", complete)
    advance(runtime, 8)
    done = runtime.get(task["id"])
    assert done["status"] == "completed", done.get("error")
    assert seen and "已记住你使用 Vim" in done["result"]
    assert "本轮没有成功保存记忆" not in done["result"]
    assert len(runtime.store.read()["memory"]["entries"]) == 1


def test_rework_rejection_previews_are_recent_authentic_and_short(tmp_path):
    store, _ = _make_store(tmp_path)
    def exercise(data):
        row = _seed_task(data, text="I use Vim", message_id="old")
        row["messages"].extend(owner(str(i), "Long owner statement " * 20) for i in range(6))
        row["messages"].append(owner("host-note", "External tool text", synthetic=True))
        result = propose(data, row)
        assert result["reason"] == "proposal_not_grounded_in_owner_message"
        assert [m["message_id"] for m in result["recent_owner_messages"]] == ["5", "4", "3", "2", "1"]
        assert all(len(m["preview"]) <= 80 for m in result["recent_owner_messages"])
    store.transaction(exercise)


@pytest.mark.parametrize("meta", [{"agent_path": "/root/child"}, {}])
def test_rework_subagent_cannot_select_a_message_id(tmp_path, meta):
    store, _ = _make_store(tmp_path)
    def exercise(data):
        row = _seed_task(data, text="I use Vim", message_id="parent-authored")
        row["subagent_meta"] = meta
        row["messages"].append(owner("latest", "Continue"))
        with pytest.raises(AgentToolRejected, match="agent_invalid_tool_arguments"):
            propose(data, row, message_id="parent-authored")
        assert not memory.ensure_memory_state(data)["entries"]
    store.transaction(exercise)
