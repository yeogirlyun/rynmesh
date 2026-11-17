"""S12-I5: read-only planning guidance lives in host authority, not the base."""
import copy

import pytest
from test_agent_runtime import advance, assistant, make_runtime
from rynmesh.agent_runtime import execution_policy, prompt

LEGACY = "This task is read-only: inspect and explain a proposed plan, never request a mutation tool."


def _row(tmp_path, mode="read_only"):
    runtime, model, tools, _ = make_runtime(tmp_path, assistant("Prepared the plan."))
    runtime.update_settings({"permission_mode": mode})
    task = runtime.create({"message": "Inspect the code and propose a complete implementation plan."})
    return runtime, model, tools, runtime.store.read()["tasks"][task["id"]]


@pytest.mark.parametrize("mode", ["read_only", "workspace", "confirm_changes", "automatic"])
def test_t1_real_provider_authority_contains_plan_guidance_only_for_read_only(tmp_path, mode):
    runtime, model, _, row = _row(tmp_path, mode)
    guidance = prompt.PLAN_MODE_GUIDANCE
    advance(runtime)
    authority = [m for m in model.calls[-1][1] if m["role"] in {"developer", "system"} and guidance in str(m.get("content"))]
    assert len(authority) == (1 if mode == "read_only" else 0)
    if authority:
        assert authority[0]["content"].startswith(guidance)
        assert "Server control state:" in authority[0]["content"]
    assert runtime.get(row["id"])["permission_mode"] == mode


@pytest.mark.parametrize("override", ["model_base", "persisted_base", "session_base"])
def test_t2_explicit_empty_base_preserves_separate_plan_authority(tmp_path, override):
    _, _, _, row = _row(tmp_path)
    layers = prompt.instruction_layers(row, **{override: ""})
    assert layers[0] == {"role": "developer", "content": ""}
    assert layers[1]["role"] == "developer"
    assert layers[1]["content"].startswith(prompt.PLAN_MODE_GUIDANCE)


def test_t2_existing_task_with_empty_pinned_base_keeps_guidance_in_real_context(tmp_path):
    runtime, _, _, row = _row(tmp_path)
    runtime.store.transaction(lambda data: data["tasks"][row["id"]].update(base_instructions=""))
    saved = runtime.store.read()["tasks"][row["id"]]
    context = runtime._context(saved)
    assert context[0]["role"] == "developer" and context[0]["content"] == ""
    assert sum(prompt.PLAN_MODE_GUIDANCE in str(m.get("content")) for m in context) == 1


@pytest.mark.parametrize("meta", [{}, {"role": "explorer"}, {"role": "worker"}])
def test_subagent_metadata_keeps_existing_short_read_only_policy(tmp_path, meta):
    _, _, _, row = _row(tmp_path)
    child = copy.deepcopy(row)
    child["subagent_meta"] = meta
    layers = prompt.instruction_layers(child)
    assert layers[1]["content"].startswith(LEGACY)
    assert not any(prompt.PLAN_MODE_GUIDANCE in str(m.get("content")) for m in layers)


def test_guidance_covers_research_meaningful_choices_complete_plan_and_execution_boundary():
    text = prompt.PLAN_MODE_GUIDANCE.lower()
    for phrase in ("read-only planning task", "read tools", "choices that change the plan", "one complete plan",
                   "without further decisions", "files", "steps", "tests", "risks", "leave read-only mode",
                   "keep the plan ready", "never request a mutation tool"):
        assert phrase in text
    assert 400 <= len(prompt.PLAN_MODE_GUIDANCE.encode("utf-8")) <= 550


@pytest.mark.parametrize("effect", ["write", "external_change", "unknown"])
def test_host_policy_still_denies_non_reads_even_with_approval_and_model_claims(effect):
    call = {"name": "share_service", "arguments": {"effect": "read", "confirmed": True}}
    row = {"permission_mode": "read_only", "approved": True, "allowed_write_tools": ["share_service"]}
    decision = execution_policy.decide(call, {"effect": effect}, row, {"permission_mode": "automatic"})
    assert decision.action == "deny" and decision.basis == "read_only"


def test_host_policy_still_allows_trusted_read_tools():
    decision = execution_policy.decide({"name": "inspect_service", "arguments": {}}, {"effect": "read"},
        {"permission_mode": "read_only"}, {})
    assert decision.action == "execute" and decision.basis == "read"


def test_owner_execution_request_cannot_enable_mutation_in_real_read_only_dispatch(tmp_path):
    runtime, model, tools, row = _row(tmp_path)
    model.responses = [assistant(name="share_service", args={"service_id": "fixture"})]
    runtime.store.transaction(lambda data: data["tasks"][row["id"]]["messages"][0].update(
        content="The plan is ready. Go ahead and execute it."))
    advance(runtime, 2)
    saved = runtime.get(row["id"])
    assert tools.calls == []
    assert saved["events"][-1]["result"]["error"] == "agent_permission_denied"
    assert saved["permission_mode"] == "read_only"
