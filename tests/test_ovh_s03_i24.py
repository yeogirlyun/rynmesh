"""S03-I24 adaptive reservation, useful output room and CJK first-request prior."""
import pytest
from test_agent_runtime import advance, assistant, make_runtime
from test_agent_context_compaction_policy import WindowCatalog, exchange, seed, pressure, compaction_events, is_compaction_call

from rynmesh.agent_runtime import budget, context_compaction, output_policy
from rynmesh.agent_runtime.store import AgentError


def test_T1_large_output_reservation_is_limited_to_a_quarter_window():
    model = {"context_window": 200000, "max_output_tokens": 64000}
    policy = output_policy.context_budget(model)
    assert policy["reserved_output_tokens"] == 50000
    assert policy["effective_max_context_bytes"] == (200000 - 50000 - 512) * 3
    messages = [{"role": "user", "content": "a" * 420000}]
    assert output_policy.admit_request_context(model, messages, [])["text_projection_utf8_bytes"] > 420000
    selected = output_policy.select(model, messages, [])
    assert selected["effective_cap"] <= 200000 - selected["estimated_input_tokens"]
    assert selected["configured_cap"] == 64000


@pytest.mark.parametrize("window,output,reserved", [(32768, 8192, 8192), (32000, 4096, 4096), (1000000, 64000, 64000)])
def test_T2_small_windows_and_ample_large_windows_keep_existing_reserve(window, output, reserved):
    policy = output_policy.context_budget({"context_window": window, "max_output_tokens": output})
    assert policy["reserved_output_tokens"] == reserved


def test_T3_low_output_room_triggers_real_compaction_but_ample_room_does_not(tmp_path):
    runtime, model, _, _ = make_runtime(tmp_path, assistant("Goal: inspect records. Findings: records read. Next: finish the comparison."), assistant("Comparison finished."))
    runtime.catalog = WindowCatalog(200000, 8192)
    task = runtime.create({"message": "Inspect all records; change nothing."})
    history = [message for index in range(30) for message in exchange(f"record-{index}", "observed healthy " + "a" * 4000)]
    row = seed(runtime, task["id"], history)
    assert pressure(runtime, row)["compact"] is False
    analysis = output_policy.analyze_request(runtime._context(row), runtime._specs(row))
    runtime.catalog.window_tokens = analysis["estimated_input_tokens"] + 3000
    decision = pressure(runtime, row)
    assert decision["reason"] == "output_room" and decision["near_limit"] and decision["compact"]
    assert decision["output_room_tokens"] < 4096
    advance(runtime)
    assert len(model.calls) == 2 and is_compaction_call(model.calls[0][1], model.calls[0][2]) and model.calls[1][2]
    assert compaction_events(runtime, task["id"])[0]["reason"] == "output_room"
    assert runtime.get(task["id"])["status"] == "completed"


def test_T3_output_room_helper_matches_selection_and_does_not_throw_on_overflow():
    model = {"context_window": 12000, "max_output_tokens": 8192}
    messages = [{"role": "user", "content": "a" * 30000}]
    analysis = output_policy.analyze_request(messages, [])
    room = output_policy.output_room(model, analysis)
    assert 0 < room < 4096
    assert output_policy.select(model, messages, [])["effective_cap"] == room
    overflow = output_policy.analyze_request([{"role": "user", "content": "a" * 50000}], [])
    assert output_policy.output_room(model, overflow) < 1
    with pytest.raises(AgentError, match="agent_context_limit"):
        output_policy.select(model, [{"role": "user", "content": "a" * 50000}], [])


def test_T4_equal_utf8_bytes_chinese_estimate_is_about_one_and_a_half_english():
    english = output_policy.analyze_request([{"role": "user", "content": "a" * 180000}], [])
    chinese = output_policy.analyze_request([{"role": "user", "content": "中" * 60000}], [])
    assert chinese["text_projection_utf8_bytes"] == english["text_projection_utf8_bytes"]
    assert chinese["cjk_share"] > .999 and english["cjk_share"] == 0
    assert chinese["estimated_text_tokens"] / english["estimated_text_tokens"] == pytest.approx(1.5, rel=.01)
    assert chinese["cjk_char_estimate"] == 60000


@pytest.mark.parametrize("known", [True, False])
def test_T5_real_runtime_first_request_capacity_uses_same_prior(known, tmp_path):
    runtime, _, _, _ = make_runtime(tmp_path)
    runtime.catalog.list_models = lambda: [{"id": "local:test", "source": "local_api", "available": True, "agent_supported": True,
        "context_window": 200000, "context_window_known": known, "max_output_tokens": 8192}]
    task = runtime.create({"message": "Compare documents."})
    row = runtime.store.read()["tasks"][task["id"]]
    chinese = output_policy.analyze_request([{"role": "user", "content": "中" * 10000}], [])
    english = output_policy.analyze_request([{"role": "user", "content": "a" * 30000}], [])
    policy = runtime._context_policy(row)
    _, english_capacity = runtime._effective_request_fit(row, english, policy)
    _, chinese_capacity = runtime._effective_request_fit(row, chinese, policy)
    assert english_capacity == policy["effective_max_context_bytes"]
    assert chinese_capacity < english_capacity
    assert context_compaction.applied_density(policy, None, chinese["cjk_share"]) == pytest.approx(3 - chinese["cjk_share"])


def test_T6_settled_main_usage_overrides_cjk_prior_and_peer_admission_stays_one(tmp_path):
    runtime, _, _, _ = make_runtime(tmp_path)
    runtime.catalog = WindowCatalog(200000, 8192)
    task = runtime.create({"message": "Compare documents."})
    def observed(data):
        b = budget.initialize(data["tasks"][task["id"]])
        b["records"].append({"kind": "model", "stage": "model", "status": "settled", "input_tokens": 10000,
            "output_policy": {"projected_context_utf8_bytes": 27000, "estimated_image_tokens": 0, "model_id": "local:test"}})
    runtime.store.transaction(observed)
    row = runtime.store.read()["tasks"][task["id"]]
    policy = runtime._context_policy(row)
    chinese = output_policy.analyze_request([{"role": "user", "content": "中" * 10000}], [])
    _, capacity = runtime._effective_request_fit(row, chinese, policy)
    assert capacity == int((200000 - 8192 - 512) * 2.7)
    assert context_compaction.applied_density(policy, 2.7, chinese["cjk_share"]) == 2.7
    # A sparse calibration also supersedes the prior. S03-I8 owns whether
    # that calibration can enlarge capacity above the original byte ceiling.
    assert context_compaction.applied_density(policy, 4.2, chinese["cjk_share"]) == context_compaction.applied_density(policy, 4.2, 0)
    peer = output_policy.context_budget({"context_window": 200000, "max_output_tokens": 8192, "context_admission_bytes_per_token": 1})
    assert context_compaction.input_capacity(peer["effective_max_context_bytes"], peer, cjk_share=1) == peer["effective_max_context_bytes"]
