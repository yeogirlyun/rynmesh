"""S12-I2 API effort: declaration, runtime dispatch and native wire bodies."""
import copy

import pytest

from test_agent_reasoning_effort import effort_runtime
from test_agent_runtime import advance, assistant
from test_agent_model_profiles import API_MODEL, API_SOURCE, World
from test_native_protocol_codecs import MODEL, SCOPE, body, message
from rynmesh.agent_runtime import reasoning
from rynmesh.agent_runtime.store import AgentError
from rynmesh.llm_package import model_settings
from rynmesh.llm_package.api_formats import normalize
from rynmesh.llm_package.protocol_codecs import MESSAGES, RESPONSES, make_request, parse_response
from rynmesh.llm_package.reasoning import metadata, validate_effort


def test_t1_owner_declaration_is_validated_and_exported():
    values = {"supported_reasoning_efforts": ["low", "medium", "high"],
              "request_defaults": {"reasoning_effort": "medium"}}
    checked = model_settings.validate(values)
    record = {"models": ["m"], "model_settings": {"m": {"overrides": checked}}}
    assert model_settings.public(record, "m")["effective"]["supported_reasoning_efforts"] == values["supported_reasoning_efforts"]
    assert "supported_reasoning_efforts" not in model_settings.public({"models": ["m"]}, "m")["effective"]
    for levels in ("high", ["invented"], ["low"] * 9, [1], ["low", "low"]):
        with pytest.raises(ValueError):
            model_settings.validate({"supported_reasoning_efforts": levels})
    assert model_settings.validate({"request_defaults": {"reasoning_effort": "ultra"}})


@pytest.mark.parametrize("saved_effort", ["high", "xhigh"])
def test_t1_api_creation_requires_declaration_and_reaches_real_http(tmp_path, monkeypatch, saved_effort):
    world = World(tmp_path, monkeypatch)
    world.settings(model_id=API_MODEL)
    with pytest.raises(AgentError, match="agent_reasoning_effort_unsupported"):
        world.runtime.create({"message": "Hello", "reasoning_effort": "low"})
    source = copy.deepcopy(API_SOURCE)
    source["model_settings"] = {"claude-fixture": {"overrides": {
        "supported_reasoning_efforts": ["low", "medium", "high"],
        "max_output_tokens": 32768, "request_defaults": {"reasoning_effort": "medium"},
    }}}
    world.source_store.read = lambda: {"src-a": copy.deepcopy(source)}
    manager = world.managers["api-src-a-claude-fixture"]
    manager._source_revision = model_settings.fingerprint(source, "claude-fixture")
    current = world.runtime.catalog.resolve(API_MODEL)
    assert current["supported_reasoning_efforts"] == ["low", "medium", "high"]
    assert current["default_reasoning_effort"] == "medium"
    created = world.runtime.create({"message": "Hello", "reasoning_effort": "high"})
    if saved_effort != "high":
        world.runtime.store.transaction(lambda data: data["tasks"][created["id"]].update(reasoning_effort=saved_effort))
    assert world.run(created["id"])["status"] == "completed"
    assert world.http_payloads[0]["thinking"] == {"type": "enabled", "budget_tokens": 16384}
    assert "temperature" not in world.http_payloads[0]


@pytest.mark.parametrize("protocol", ["chat", RESPONSES, MESSAGES])
def test_t2_three_wire_formats(protocol):
    source = {**body(), "reasoning_effort": "high", "max_tokens": 32768, "temperature": 0.5}
    wire = normalize(source, "chat") if protocol == "chat" else make_request(source, protocol, MODEL, SCOPE)
    if protocol == "chat":
        assert wire["reasoning_effort"] == "high"
    elif protocol == RESPONSES:
        assert wire["reasoning"] == {"effort": "high"}
    else:
        assert wire["thinking"] == {"type": "enabled", "budget_tokens": 16384}
        assert "temperature" not in wire


@pytest.mark.parametrize("options, expected", [
    ({"thinking": {"type": "enabled", "budget_tokens": 4096}}, {"type": "enabled", "budget_tokens": 4096}),
    ({"enable_thinking": True}, {"type": "enabled", "budget_tokens": 8192}),
    ({"reasoning_effort": "none"}, {"type": "disabled"}),
    ({"reasoning_effort": "ultra"}, {"type": "enabled", "budget_tokens": 32000}),
])
def test_t3_explicit_thinking_and_signed_tool_continuation(options, expected):
    native = message()
    parsed = parse_response(native, MESSAGES, MODEL, SCOPE)["message"]
    history = [{"role": "user", "content": "Check"}, parsed,
               {"role": "tool", "tool_call_id": "call_1", "content": "OK"}]
    wire = make_request({**body(history), "max_tokens": 32768, "temperature": 0.8, **options}, MESSAGES, MODEL, SCOPE)
    assert wire["thinking"] == expected
    assert wire["messages"][1]["content"] == native["content"]
    assert wire["messages"][1]["content"][0]["signature"] == "PRIVATE_SIGNATURE"
    assert wire["messages"][1]["content"][1]["data"] == "PRIVATE_REDACTED"
    if expected["type"] == "enabled":
        assert "temperature" not in wire


@pytest.mark.parametrize("maximum, expected", [(8192, 7168), (1024, None), (2048, 1024)])
def test_t2_thinking_budget_stays_below_output(maximum, expected):
    wire = make_request({**body(), "max_tokens": maximum, "reasoning_effort": "high"}, MESSAGES, MODEL, SCOPE)
    assert wire.get("thinking") == (None if expected is None else {"type": "enabled", "budget_tokens": expected})


@pytest.mark.parametrize("thinking", [{"type": "enabled", "budget_tokens": 1023}, {"type": "enabled", "budget_tokens": True},
                                       {"type": "enabled", "budget_tokens": 32768}, {"type": "adaptive"},
                                       {"type": "enabled", "budget_tokens": 4096, "extra": True}])
def test_t3_bad_thinking_shapes_still_rejected(thinking):
    with pytest.raises(ValueError):
        make_request({**body(), "max_tokens": 32768, "thinking": thinking}, MESSAGES, MODEL, SCOPE)


def test_t3_defaults_accept_explicit_budget_and_responses_enable_is_implicit():
    assert model_settings.validate({"max_output_tokens": 8192,
        "request_defaults": {"thinking": {"type": "enabled", "budget_tokens": 4096}}})
    wire = make_request({**body(), "enable_thinking": True, "reasoning_effort": "low"}, RESPONSES, MODEL, SCOPE)
    assert wire["reasoning"] == {"effort": "low"} and "thinking" not in wire


def test_t4_saved_effort_falls_down_without_weakening_explicit_selection(tmp_path):
    runtime, _, calls, rows = effort_runtime(tmp_path)
    rows[0].update(adapter="openai_compatible", source="local_api", supported_reasoning_efforts=["low", "medium", "high", "xhigh"])
    row = runtime.create({"message": "Hello", "reasoning_effort": "xhigh"})
    rows[0]["supported_reasoning_efforts"] = ["low", "medium", "high"]
    advance(runtime)
    assert runtime.get(row["id"])["status"] == "completed"
    assert calls[-1]["reasoning_effort"] == "high"
    assert runtime.get(row["id"])["reasoning_effort"] == "xhigh"
    with pytest.raises(AgentError, match="agent_reasoning_effort_unsupported"):
        runtime.create({"message": "Hello", "reasoning_effort": "xhigh"})


def test_t4_resolve_uses_nearest_lower_or_last_supported():
    model = {"supported_reasoning_efforts": ["low", "medium", "high"]}
    assert reasoning.resolve(model, "ultra") == "high"
    assert reasoning.resolve(model, "none") == "high"
    with pytest.raises(AgentError, match="agent_invalid_reasoning_effort"):
        reasoning.resolve(model, "invented")


def test_t5_nested_summary_keeps_task_effort(tmp_path):
    runtime, _, calls, rows = effort_runtime(tmp_path)
    rows[0].update(adapter="openai_compatible", source="local_api")
    created = runtime.create({"message": "Hello", "reasoning_effort": "high"})
    runtime.store.transaction(lambda data: data["tasks"][created["id"]].update(status="running"))
    row = runtime.store.read()["tasks"][created["id"]]
    runtime._complete(row, [{"role": "user", "content": "Summarize this history"}], [], lambda: False, None,
                      nested=True, stage="compaction")
    assert calls[-1]["reasoning_effort"] == "high"


@pytest.mark.parametrize("effort, summary_effort", [("high", "high"), (None, "low")])
def test_t5_real_compaction_keeps_explicit_task_effort(tmp_path, effort, summary_effort):
    from test_agent_context_compaction_policy import seed, exchange, is_compaction_call
    runtime, model, _, rows = effort_runtime(tmp_path)
    rows[0].update(adapter="openai_compatible", source="local_api")
    captured = []
    def complete(model_id, messages, specs, cancelled, on_event, **options):
        compact = is_compaction_call(messages, specs)
        captured.append((compact, options.get("reasoning_effort")))
        return assistant("Goal: inspect. Next: finish." if compact else "Done.")
    model.complete = complete
    created = runtime.create({"message": "Inspect then finish", **({"reasoning_effort": effort} if effort else {})})
    for index in range(22):
        seed(runtime, created["id"], exchange(f"item_{index}", "effort_data " * 350))
    advance(runtime)
    assert (True, summary_effort) in captured
    assert (False, effort) in captured


def test_t6_long_metadata_truncates_instead_of_disappearing():
    result = metadata(["low"] + ["future"] * 15 + ["high"] * 4)
    assert result["supported_reasoning_efforts"] == ["low"]
    assert "high" not in result["supported_reasoning_efforts"]


def test_t7_unknown_levels_are_bounded_and_never_sendable():
    result = metadata(["low", "future-v2", {"reasoningEffort": "super_effort"}, "BAD", "x" * 33, "bad name"], "future-v2")
    assert result["supported_reasoning_efforts"] == ["low"]
    assert result["ignored_reasoning_efforts"] == ["future-v2", "super_effort"]
    assert "default_reasoning_effort" not in result
    assert metadata(["future-only"])["ignored_reasoning_efforts"] == ["future-only"]
    with pytest.raises(ValueError):
        validate_effort("future-v2")
