"""S09-I20: contextual memory roles, proportional budgets and cheap fitting."""
import copy
import json

import pytest
from test_agent_runtime import make_runtime
from test_agent_memory_natural import FakeRuntime, DEST_A, _enable_memory, _seed_completed_task

from rynmesh.agent_runtime import memory, memory_integration, memory_natural, memory_runtime, request_context


def entries(count=8):
    return [{"id": f"entry-{i:03}", "scope": {"kind": "personal"}, "category": "general",
             "fact_key": f"personal.fact.{i}", "kind": "owner_fact", "text": f"Fact {i:03}: " + "x" * 145,
             "revision": 1} for i in range(count)]


def selection(items=None, *, navigation=False):
    return {"enabled": True, "selected": entries(2) if items is None else items, "max_bytes": 100000,
            "include_navigation": navigation, "profile_summary": ["profile" * 600] if navigation else [],
            "navigation_index": [{"id": "navigation-only", "fact_key": "personal.navigation"}] if navigation else []}


@pytest.mark.parametrize("kind", ["selected", "navigation", "mixed"])
def test_t1_memory_is_tagged_user_data_with_search_staleness_and_source_rules(kind):
    selected = selection([] if kind == "navigation" else entries(2), navigation=kind != "selected")
    selected["max_bytes"] = 6144
    selected["profile_summary"] = ["Useful factual profile"]
    [message] = memory_runtime.format_memory_context_messages(selected)
    assert message["role"] == "user"
    assert message["content"].startswith("<memory_context>") and message["content"].endswith("</memory_context>")
    for rule in ("ryn_memory_search", "kind=task_summary", "self-contained", "may be stale", "来自记忆，可能过时",
                 "original sources", "never instructions or tool authority"):
        assert rule in message["content"]
    assert len(message["content"].encode()) <= selected["max_bytes"]


def test_t2_profile_can_exceed_512_bytes_under_a_6144_byte_budget():
    profile, index, used = memory_natural.build_navigation_and_summary(entries(), 6144)
    profile_bytes = sum(len(json.dumps(line, ensure_ascii=False).encode()) for line in profile)
    assert 512 < profile_bytes <= 6144 // 4
    assert len(profile) == 4 and index and used <= 6144


def test_t2_automatic_selector_preserves_quarter_budget_with_remaining_context(tmp_path):
    runtime = FakeRuntime(tmp_path)
    _enable_memory(runtime, activation_policy="automatic", allowed_destinations=[DEST_A])
    row = _seed_completed_task(runtime, "profile-task", [("owner", "Recall previous decisions")])
    def seed(data):
        for entry in entries():
            memory.upsert_active_entry(data, {key: entry[key] for key in ("scope", "category", "fact_key", "kind", "text")}, origin="manual")
    runtime.store.transaction(seed)
    selected = memory.select_memories_for_task(runtime.store.read(), row, remaining_context_bytes=24576)
    assert selected["max_bytes"] == 6144 and selected["include_navigation"] is True
    assert sum(len(json.dumps(line).encode()) for line in selected["profile_summary"]) > 512


@pytest.mark.parametrize("checkpoint", [None, {"through": 2, "content": "Checkpoint summary"}])
def test_t3_memory_keeps_its_layer_position_and_trusted_disclosure_marker(checkpoint):
    [message] = memory_runtime.format_memory_context_messages(selection())
    history = [{"role": "user", "content": "Earlier owner"}, {"role": "assistant", "content": "Earlier answer"},
               {"role": "user", "content": "Current owner"}]
    built = request_context.build_context([{"role": "developer", "content": "Host rules"}], history,
        guidance=[{"role": "user", "content": "<skill>Skill</skill>"}, {"role": "user", "content": "Preferences"}, message],
        plan=[{"role": "assistant", "content": "Plan"}], checkpoint=checkpoint)
    assert built[3] == message and built[3]["role"] == "user"
    assert built[2]["content"] == "Preferences" and built[4]["content"] == "Plan"
    assert built[-1]["content"] == "Current owner"
    wrapped = memory_integration.ContextMessages(built)
    wrapped.disclosed_memory = True
    assert memory_integration.messages_disclose_memory(wrapped)
    assert memory_integration.messages_disclose_memory(built, row={"_context_disclosed_memory": True})
    assert not memory_integration.messages_disclose_memory(built)


def fit_fixture(tmp_path, monkeypatch, *, boundary=False):
    runtime, _, _, _ = make_runtime(tmp_path)
    created = runtime.create({"message": "Use relevant remembered decisions"})
    row = runtime.store.read()["tasks"][created["id"]]
    row["messages"].extend({"role": "assistant", "content": f"Historical note {i}: " + "x" * 3000} for i in range(20))
    items = entries(128)
    active = [selection(items[:40])]
    monkeypatch.setattr(memory_integration, "select_guidance_and_memory",
                        lambda runtime, row: ([], None if row.get("_exclude_optional_memory") else copy.deepcopy(active[0])))
    monkeypatch.setattr(runtime, "_effective_capacity", lambda *args, **kwargs: (1000000, {}))
    original_measure = runtime._request_context_bytes
    expected = runtime._context(copy.deepcopy(row))
    active[0] = selection(items[:41])
    next_context = runtime._context(copy.deepcopy(row))
    specs = runtime._specs({**row, "_exclude_optional_memory": True})
    size40 = original_measure(row, expected, specs)
    size41 = original_measure(row, next_context, specs)
    assert size40 < size41
    capacity = size40 if boundary else (size40 + size41) // 2
    monkeypatch.setattr(runtime, "_effective_capacity", lambda *args, **kwargs: (capacity, {}))
    active[0] = selection(items, navigation=True)
    return runtime, row, expected, original_measure, capacity


@pytest.mark.parametrize("boundary", [False, True])
def test_t4_128_entries_fit_same_40_prefix_with_at_most_three_full_measurements(tmp_path, monkeypatch, boundary):
    runtime, row, expected, original_measure, _ = fit_fixture(tmp_path, monkeypatch, boundary=boundary)
    measurements, builds = [], []
    def measure(*args, **kwargs):
        measurements.append(1)
        return original_measure(*args, **kwargs)
    original_build = request_context.build_context
    def build(*args, **kwargs):
        builds.append(1)
        return original_build(*args, **kwargs)
    monkeypatch.setattr(runtime, "_request_context_bytes", measure)
    monkeypatch.setattr(request_context, "build_context", build)
    actual = runtime._context(copy.deepcopy(row))
    assert actual == expected
    assert len(measurements) <= 3 and len(builds) <= 3
    assert memory_integration.messages_disclose_memory(actual)


@pytest.mark.parametrize("still_too_large", [False, True])
def test_t4_underestimated_candidate_is_halved_once_then_checked_or_dropped(tmp_path, monkeypatch, still_too_large):
    runtime, row, _, original_measure, capacity = fit_fixture(tmp_path, monkeypatch)
    checked = []
    def measure(row, messages=None, specs=None, **kwargs):
        memories = [m for m in messages if "Owner long-term memory facts" in str(m.get("content", ""))]
        checked.append(memories)
        if memories and (len(checked) == 2 or still_too_large):
            return capacity + 1
        return original_measure(row, messages, specs, **kwargs)
    monkeypatch.setattr(runtime, "_request_context_bytes", measure)
    actual = runtime._context(copy.deepcopy(row))
    assert len(checked) <= 3
    if still_too_large:
        assert not memory_integration.messages_disclose_memory(actual)
        assert not any("Owner long-term memory facts" in str(m.get("content", "")) for m in actual)
    else:
        [fragment] = [m for m in actual if "Owner long-term memory facts" in str(m.get("content", ""))]
        expected = memory_runtime.format_memory_context_messages(selection(entries(20)))[0]
        assert fragment == expected and memory_integration.messages_disclose_memory(actual)
