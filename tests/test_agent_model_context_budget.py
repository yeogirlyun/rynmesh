"""Model-window context admission through the real encrypted runtime and loop.

All model responses are authored fixtures; no provider or network is invoked.
"""
import copy
from types import SimpleNamespace

import pytest
from test_agent_runtime import advance, assistant, make_runtime
from test_agent_runtime_model_switch import body as switch_body
from test_agent_runtime_model_switch import setup as switch_setup

from rynmesh.agent_runtime import output_policy, state, verification
from rynmesh.agent_runtime.diagnostics import DiagnosticTools
from rynmesh.agent_runtime.runtime import AgentRuntime
from rynmesh.agent_runtime.store import AgentError, AgentStore, encoded


def window_model(window=1000000, *, output=128000, **extra):
    return {"id": "local:test", "name": "Fixture", "source": "local_api", "available": True,
            "agent_supported": True, "context_window": window, "context_window_known": True,
            "context_window_basis": "owner_model_override", "max_output_tokens": output, **extra}


def configure(runtime, metadata):
    runtime.catalog.list_models = lambda: [copy.deepcopy(metadata)]


def stored(runtime, task_id):
    return runtime.store.read()["tasks"][task_id]


def test_new_default_follows_large_model_without_600k_or_2m_byte_ceiling(tmp_path):
    runtime, _, _, _ = make_runtime(tmp_path)
    configure(runtime, window_model())
    task = runtime.create({"message": "Use the declared model window."})
    row = stored(runtime, task["id"])
    policy = runtime.settings()["context_budget_policy"]
    assert row["limits"]["max_context_chars"] is None
    assert policy["mode"] == "auto" and policy["provenance"] == "default_auto"
    assert policy["effective_max_context_bytes"] == (1000000 - 128000 - 512) * 3 > 2000000
    assert runtime._context_limit(row) == policy["effective_max_context_bytes"]
    assert policy["reserved_output_tokens"] == 128000
    assert policy["context_window_known"] is True
    assert policy["context_window_basis"] == "owner_model_override"
    assert policy["token_count_is_estimate"] is True
    assert policy["units"]["max_context_chars"] == "serialized_request_utf8_bytes"


@pytest.mark.parametrize("manual", [4000, 600000, 2000000])
def test_explicit_manual_ceiling_and_return_to_auto_survive_restart(tmp_path, manual):
    runtime, model, tools, key = make_runtime(tmp_path)
    configure(runtime, window_model())
    settings = runtime.update_settings({"max_context_chars": manual})
    assert settings["context_budget_policy"]["provenance"] == "owner_manual"
    first = runtime.create({"message": "Keep my byte ceiling."})
    assert stored(runtime, first["id"])["context_budget_provenance"] == "owner_manual"
    assert runtime._context_limit(stored(runtime, first["id"])) == manual
    restored = AgentRuntime(AgentStore(tmp_path, key), model, tools, runtime.catalog)
    assert restored.settings()["max_context_chars"] == manual
    restored.update_settings({"max_context_chars": None})
    second = restored.create({"message": "Use automatic model capacity."})
    assert stored(restored, second["id"])["context_budget_provenance"] == "owner_auto"
    assert restored._context_limit(stored(restored, second["id"])) > 2000000
    # Changing the new-task default does not rewrite an existing task's limit.
    assert restored._context_limit(stored(restored, first["id"])) == manual


@pytest.mark.parametrize("bad", [True, 0, 3999, 2000001, "auto", 600000.0])
def test_invalid_manual_context_limit_still_rejected(tmp_path, bad):
    runtime, _, _, _ = make_runtime(tmp_path)
    with pytest.raises(AgentError, match="agent_invalid_settings"):
        runtime.update_settings({"max_context_chars": bad})


@pytest.mark.parametrize("manual", [None, 600000])
def test_actual_model_switch_recomputes_small_window_without_changing_saved_limit(tmp_path, manual):
    runtime, _, _, _, task_id, rows = switch_setup(tmp_path)
    for index, model in enumerate(rows):
        model.update(context_window=1000000 if index == 0 else 16000, max_output_tokens=4096,
                     context_window_known=True, context_window_basis="fixture_declared")
    if manual is not None:
        def set_manual(data):
            row = data["tasks"][task_id]
            row["limits"]["max_context_chars"] = manual
            row["context_budget_provenance"] = "owner_manual"
        runtime.store.transaction(set_manual)
    before = stored(runtime, task_id)
    assert runtime._context_limit(before) > 300000
    runtime.switch_model(task_id, switch_body(runtime, task_id))
    after = stored(runtime, task_id)
    assert after["limits"]["max_context_chars"] is manual or after["limits"]["max_context_chars"] == manual
    assert runtime._context_limit(after) == (16000 - 4096 - 512) * 3
    assert after["limits"] == before["limits"]
    assert after["status"] == "paused"


@pytest.mark.parametrize("metadata", [
    {"id": "local:test", "available": True, "agent_supported": True},
    window_model(2000000, output=4096, context_window_known=False, context_window_basis="unknown_fallback"),
    window_model(0, output=4096),
])
def test_unknown_window_uses_labelled_conservative_fallback(tmp_path, metadata):
    runtime, _, _, _ = make_runtime(tmp_path)
    configure(runtime, metadata)
    policy = runtime.settings()["context_budget_policy"]
    assert policy["context_window_tokens"] == 32768
    assert policy["context_window_known"] is False and policy["context_window_basis"] == "unknown_fallback"
    assert runtime._context_limit({"model_id": "local:test", "limits": {"max_context_chars": None}}) == (32768 - 4096 - 512) * 3
    assert policy["effective_max_context_bytes"] < 120000


@pytest.mark.parametrize("marker", [None, 1])
@pytest.mark.parametrize("manual", [120000, 600000])
def test_ambiguous_legacy_numeric_limits_are_never_migrated_by_value(tmp_path, marker, manual):
    runtime, _, _, _ = make_runtime(tmp_path)
    task = runtime.create({"message": "Existing owner requirement"})
    original = stored(runtime, task["id"])
    original.pop("context_budget_provenance", None)
    original["limits"]["max_context_chars"] = manual
    original["scope"] = {"resource_grants": [{"id": "fixture-grant"}], "egress_grants": []}
    original["receipts"] = [{"id": "fixture-confirmed", "status": "confirmed", "effect": "write"}]
    original["approved"] = {"fixture": "existing-exact-approval"}
    data = {"settings": {"max_context_chars": manual}, "tasks": {task["id"]: copy.deepcopy(original)}}
    if marker is not None:
        data["context_budget_version"] = marker
    migrated = state.migrate(data)
    assert migrated["context_budget_version"] == 2
    assert migrated["settings"]["max_context_chars"] == manual
    assert migrated["settings_context_budget_provenance"] == "legacy_ambiguous_manual"
    row = migrated["tasks"][task["id"]]
    assert row["limits"]["max_context_chars"] == manual
    assert row["context_budget_provenance"] == "legacy_ambiguous_manual"
    for field in ("messages", "goal", "scope", "approved", "receipts", "limits"):
        assert row[field] == original[field]


def test_explicit_600k_and_proven_automatic_provenance_are_distinct(tmp_path):
    runtime, _, _, _ = make_runtime(tmp_path)
    configure(runtime, window_model())
    runtime.update_settings({"max_context_chars": 600000})
    assert runtime.store.read()["settings_context_budget_provenance"] == "owner_manual"
    assert runtime.settings()["context_budget_policy"]["effective_max_context_bytes"] == 600000
    data = {"settings": {"max_context_chars": 600000}, "tasks": {}, "context_budget_version": 1,
            "settings_context_budget_provenance": "default_auto"}
    assert state.migrate(data)["settings"]["max_context_chars"] is None


def test_full_request_accounting_includes_roles_history_tools_utf8_and_framing():
    messages = [{"role": "developer", "content": "Host scope"}, {"role": "user", "content": "中文\\\""},
                {"role": "assistant", "content": "History"}]
    specs = [{"type": "function", "function": {"name": "fixture_tool", "parameters": {"type": "object"}}}]
    policy = output_policy.select(window_model(), messages, specs)
    size = len(encoded(messages)) + len(encoded(specs))
    assert policy["estimated_input_utf8_bytes"] == size
    assert policy["estimated_input_tokens"] == (size + 2) // 3 + 512
    assert "heuristic" in policy["estimate_basis"] and policy["token_count_is_estimate"] is True
    assert output_policy.select(window_model(), messages, [])["estimated_input_tokens"] < policy["estimated_input_tokens"]
    assert output_policy.select(window_model(), messages[:1], specs)["estimated_input_tokens"] < policy["estimated_input_tokens"]


def test_real_loop_keeps_over_600k_history_and_reserves_same_request_estimate(tmp_path):
    runtime, model, _, _ = make_runtime(tmp_path, assistant("Finished without summarizing."))
    configure(runtime, window_model(1000000, output=4096))
    task = runtime.create({"message": "Continue from the complete recorded observations."})
    text = "KNOWN_FIXTURE_HISTORY " * 34000  # >600000 bytes, well within the declared window.
    def add_history(data):
        data["tasks"][task["id"]]["messages"].append({"role": "assistant", "content": text})
    runtime.store.transaction(add_history)
    advance(runtime)
    row = stored(runtime, task["id"])
    assert runtime.get(task["id"])["status"] == "completed" and len(model.calls) == 1
    assert not any(event["type"].startswith("context_compaction") for event in row["events"])
    _, messages, specs = model.calls[0]
    assert any(message.get("content") == text for message in messages)
    actual_bytes = len(encoded(messages)) + len(encoded(specs))
    assert 600000 < actual_bytes <= runtime._context_limit(row)
    record = row["budget"]["records"][0]
    assert record["input_reservation"] == (actual_bytes + 2) // 3 + 512
    assert record["output_policy"]["estimated_input_utf8_bytes"] == actual_bytes
    assert record["output_policy"]["token_count_is_estimate"] is True


def test_actual_small_window_request_never_dispatches_oversized_owner_input(tmp_path):
    runtime, model, _, _ = make_runtime(tmp_path, assistant("Must not dispatch."))
    configure(runtime, window_model(8000, output=4096))
    task = runtime.create({"message": "文" * 6000})  # Valid owner-message length, oversized UTF-8 projection.
    advance(runtime)
    row = runtime.get(task["id"])
    # Schema admission counts the immutable owner authority before inference.
    assert row["status"] == "failed" and row["error"] == "agent_tool_schema_limit"
    assert not model.calls


def test_adapter_without_output_cap_still_cannot_reserve_only_part_of_output(tmp_path):
    runtime, model, _, _ = make_runtime(tmp_path, assistant("Must not dispatch."))
    task = runtime.create({"message": "Inspect the fixture."})
    row = stored(runtime, task["id"])
    estimate = output_policy.estimate_input(runtime._context(row), runtime._specs(row))["estimated_input_tokens"]
    def restrict(data):
        data["tasks"][task["id"]]["budget"]["limits"]["max_total_tokens"] = estimate + 100
    runtime.store.transaction(restrict)
    advance(runtime)
    assert not model.calls
    assert runtime.get(task["id"])["error"] == "agent_token_budget_exhausted"


def test_auto_project_guidance_and_actual_independent_checker_complete(tmp_path):
    from test_agent_runtime_verification import assessment, verified_runtime

    project = tmp_path / "project"
    project.mkdir()
    (project / "AGENTS.md").write_text("KNOWN_PROJECT_GUIDANCE: preserve authored fixtures.", encoding="utf-8")
    runtime, model, _, _ = verified_runtime(tmp_path / "private", assistant("Fixture answer."), assessment(refs=[]))
    configure(runtime, window_model(1000000, output=4096, source_id="fixture", destination_revision="fixture-v1"))
    complete = model.complete
    model.supports_destination_binding = True
    model.complete = lambda *args, **kwargs: complete(*args)
    selected = runtime.load_project({"path": str(project)})
    task = runtime.create({"message": "Explain the authored project guidance.", "project_id": selected["id"]})
    advance(runtime, 3)
    row = stored(runtime, task["id"])
    assert runtime.get(task["id"])["status"] == "completed"
    # A text-only semantic checker is not a mechanical completion proof.
    assert row["outcome"]["status"] == "unverified" and len(model.calls) == 2
    assert row["outcome"]["semantic_assessment"]["status"] == "passed"
    assert any("KNOWN_PROJECT_GUIDANCE" in message.get("content", "") for message in model.calls[0][1])
    assert not model.calls[1][2]  # The checker remains independent and has no tools.
    assert verification.checker_messages(row, context_limit=runtime._context_limit(row))
    assert verification.checker_messages(row)  # Compatibility for direct callers.
    app = SimpleNamespace(state=SimpleNamespace(agent_runtime=runtime))
    diagnostic = DiagnosticTools(app, runtime.catalog, None)._configuration()
    assert diagnostic["limits"]["max_context_chars"] is None
    assert diagnostic["context_budget_policy"]["mode"] == "auto"
    assert diagnostic["context_budget_policy"]["context_window_tokens"] == 1000000


def test_zero_input_allowance_when_full_configured_output_consumes_window():
    policy = output_policy.context_budget(window_model(8000, output=8000))
    assert policy["effective_max_context_bytes"] == 0
    assert policy["reserved_output_tokens"] == 8000
