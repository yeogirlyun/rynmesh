"""Model-window context admission through the real encrypted runtime and loop.

All model responses are authored fixtures; no provider or network is invoked.
"""
import base64
import copy
import io
from types import SimpleNamespace

import pytest
from PIL import Image
from test_agent_runtime import advance, assistant, make_runtime
from test_agent_runtime_model_switch import body as switch_body
from test_agent_runtime_model_switch import setup as switch_setup

from rynmesh.agent_runtime import budget, context_compaction, output_policy, state, verification
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
    reserve = 4096 if "max_output_tokens" in metadata else 8192
    assert runtime._context_limit({"model_id": "local:test", "limits": {"max_context_chars": None}}) == (32768 - reserve - 512) * 3
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
        # An owner budget: a default allowance would grow instead of refusing.
        budget.set_owner_limits(data["tasks"][task["id"]], {"max_total_tokens": estimate + 100})
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


def _png_attachment(name: str, width: int = 1280, height: int = 1280, color: tuple[int, int, int] = (24, 60, 120)) -> dict:
    buf = io.BytesIO()
    Image.new("RGB", (width, height), color).save(buf, format="PNG")
    return {
        "name": name,
        "media_type": "image/png",
        "data_base64": base64.b64encode(buf.getvalue()).decode("ascii"),
    }


def test_s03_i10_t1_image_token_estimates_across_dimensions_and_formula_version():
    assert output_policy.IMAGE_ANALYSIS_VERSION == "canonical-image-heuristic-v2"
    assert output_policy.IMAGE_ESTIMATE_BASIS == (
        "text_utf8_div_3_ceil_plus_512_and_image_area_div_750_capped_10000_plus_85_heuristic"
    )
    assert output_policy.IMAGE_TOKEN_FORMULA_BASIS == (
        "heuristic_min_10000_ceil_w_times_h_div_750_plus_85_not_provider_tokenizer"
    )

    assert output_policy.estimate_image_tokens(1, 1) == 86
    assert output_policy.estimate_image_tokens(64, 48) == 90
    assert output_policy.estimate_image_tokens(512, 512) == 435
    assert output_policy.estimate_image_tokens(1280, 900) == 1621
    assert output_policy.estimate_image_tokens(1280, 1280) == 2270
    assert output_policy.estimate_image_tokens(2738, 2738) == 10081
    assert output_policy.estimate_image_tokens(4000, 4000) == 10085

    for bad_w, bad_h in [(0, 100), (100, -1), (True, 128), (128, 128.0)]:
        with pytest.raises(ValueError, match="invalid_image_dimensions"):
            output_policy.estimate_image_tokens(bad_w, bad_h)

    buf = io.BytesIO()
    Image.new("RGB", (1280, 1280), (12, 34, 56)).save(buf, format="PNG")
    url = "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode("ascii")
    analysis = output_policy.analyze_request(
        [{"role": "user", "content": [{"type": "text", "text": "Inspect"}, {"type": "image_url", "image_url": {"url": url}}]}],
        [],
    )
    assert analysis["formula_version"] == "canonical-image-heuristic-v2"
    assert analysis["estimate_basis"] == output_policy.IMAGE_ESTIMATE_BASIS
    assert analysis["image_estimate_basis"] == output_policy.IMAGE_TOKEN_FORMULA_BASIS
    assert analysis["estimated_image_tokens"] == 2270

    # Requests carrying image token estimates remain excluded from text bytes/token calibration.
    settled_with_image = {
        "model_id": "local:test",
        "budget": {
            "records": [
                {
                    "kind": "model",
                    "stage": "model",
                    "status": "settled",
                    "input_tokens": 5000,
                    "output_policy": {
                        "model_id": "local:test",
                        "projected_context_utf8_bytes": 24000,
                        "estimated_image_tokens": analysis["estimated_image_tokens"],
                    },
                }
            ]
        },
    }
    assert context_compaction.observed_bytes_per_token(settled_with_image) is None


def test_s03_i10_t2_128k_window_with_four_1280_images_does_not_trigger_compaction(tmp_path):
    runtime, model, _, _ = make_runtime(tmp_path, assistant("Reviewed all four 1280x1280 images directly."))
    model.supports_image_input = True
    configure(runtime, window_model(128000, output=4096, adapter="openai_compatible", supports_image_input=True))
    attachments = [
        _png_attachment(f"screen-{idx}.png", 1280, 1280, (20 * idx, 50, 100))
        for idx in range(1, 5)
    ]
    task = runtime.create({"message": "Inspect all four 1280x1280 screenshots.", "attachments": attachments})
    prior_step = "RECORDED_OBSERVATION_STEP " * 400  # ~10.4 KB per exchange, 12 exchanges = ~125 KB (~41k tokens in 128k window)

    def seed_prior_history(data):
        msgs = data["tasks"][task["id"]]["messages"]
        prior = []
        for idx in range(12):
            prior.append({"role": "user", "content": f"Prior turn {idx}", "owner": True})
            prior.append({"role": "assistant", "content": f"[{idx}] {prior_step}"})
        msgs[:0] = prior

    runtime.store.transaction(seed_prior_history)
    advance(runtime, 3)
    row = stored(runtime, task["id"])
    assert runtime.get(task["id"])["status"] == "completed"
    assert len(model.calls) == 1
    assert not any(ev["type"].startswith("context_compaction") for ev in row["events"])
    record = row["budget"]["records"][0]
    assert record["output_policy"]["image_count"] == 4
    assert record["output_policy"]["estimated_image_tokens"] == 4 * 2270

