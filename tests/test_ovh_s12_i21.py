"""S12-I21: shipped permission and recovery wording matches the current host."""
import copy

import pytest
from test_agent_business_plugin_handoff import apps as apps, node as node
from test_agent_runtime import advance, assistant, make_runtime
from test_agent_runtime_diagnostics import toolset

from rynmesh.agent_runtime import control, execution_policy, prompt
from rynmesh.agent_runtime.store import encoded
from rynmesh.agent_runtime.tools import _SPECS
from rynmesh.node_apps.plugin_agent import ALL_INFO, ALL_LIST

TOOLS = ("ryn_connect_local_cli_source", "ryn_share_model_with_friend", "ryn_set_default_agent_model",
         "ryn_create_text_file", "ryn_upload_resource_file", "ryn_create_directory",
         "ryn_set_friend_model_access", "ryn_set_model_sharing")


@pytest.mark.parametrize("mode", ["automatic", "workspace", "confirm_changes"])
def test_T1_actual_model_authority_matches_shipped_automatic_and_desktop_policy(tmp_path, mode):
    runtime, model, _, _ = make_runtime(tmp_path, assistant("Ready."))
    runtime.update_settings({"permission_mode": mode, "desktop_access": False})
    task = runtime.create({"message": "Explain available permissions"})
    advance(runtime)
    sent = " ".join(m.get("content", "") for m in model.calls[0][1] if isinstance(m.get("content"), str))
    assert "no approval or desktop-access gate remains" in sent
    assert "owner's desktop-access switch" in sent and "outside automatic mode" in sent
    assert "only explicit-approval tools" not in sent
    # S04-I24 deliberately bypasses the stored desktop switch in automatic mode.
    row = runtime.store.read()["tasks"][task["id"]]
    call = {"name": "computer_act_window", "arguments": {}}
    decision = execution_policy.decide(call, {"effect": "write", "computer_access": True}, row, runtime.settings())
    assert decision.action == ("execute" if mode == "automatic" else "ask")


def test_T1_permission_rules_remain_a_static_suffix_across_permission_modes(tmp_path):
    runtime, _, _, _ = make_runtime(tmp_path)
    task = runtime.create({"message": "Check static authority"})
    original = runtime.store.read()["tasks"][task["id"]]
    suffixes = []
    for mode in ("automatic", "workspace", "confirm_changes"):
        row = copy.deepcopy(original)
        row["permission_mode"] = mode
        suffixes.append(prompt.authority(row).split("Owner corrections steer the task;", 1)[1])
    assert suffixes[0] == suffixes[1] == suffixes[2]


@pytest.mark.parametrize("error,limit", [("agent_step_limit", "step"), ("agent_time_limit", "time"), ("agent_model_busy", None)])
def test_T2_real_failed_task_diagnostics_describe_budget_recovery_or_exhausted_retries(tmp_path, error, limit):
    runtime, _, _, _ = make_runtime(tmp_path)
    task = runtime.create({"message": "A private request"})
    runtime._fail(task["id"], error)
    tools, _, _ = toolset(runtime)
    result = tools.execute("ryn_diagnose_current_task", {}, context={"task_id": task["id"]}, cancelled=lambda: False)
    instruction = result["suggestions"][0]["instruction"]
    if limit:
        assert "budget" in instruction and limit in instruction and "if available" in instruction
        assert "continue" in instruction and "new task only if" in instruction
        row = runtime.store.read()["tasks"][task["id"]]
        assert control.allowed_actions(row, runtime.settings())["budget"]["enabled"]
    else:
        assert "automatic retries were used up" in instruction and "Retry" in instruction
    assert result["suggestions"][0]["executed"] is False
    assert "private request" not in str(result)


@pytest.mark.parametrize("name", TOOLS)
def test_T3_tool_descriptions_state_automatic_exception_without_unconditional_approval(name):
    description = _SPECS[name][0]
    assert "automatic mode" in description
    assert "Requires approval" not in description
    assert "Requires exact setup preview approval" not in description
    assert "ONE approved transaction" not in description


def test_T3_real_plugin_discovery_guidance_is_conditional_and_nas_scope_is_preserved(apps):
    apps.manager.install("desk")
    apps.manager.install("nas")
    specs = apps.tools.craft_plugins.specs()
    description = next(s["function"]["description"] for s in specs if s["function"]["name"] == ALL_LIST)
    assert "Outside automatic mode" in description and "in automatic mode it runs directly" in description
    assert "keep their approval boundary" not in description
    assert encoded(specs) == encoded(apps.tools.craft_plugins.specs())
    business = apps.tools.execute(ALL_INFO, {"app": "desk"}, operation_id="info-business", cancelled=lambda: False)
    guidance = business["operation_guidance"]
    assert "Outside automatic mode" in guidance and "in automatic mode it runs directly" in guidance
    assert "including private reads and model data release" in guidance
    nas = apps.tools.execute(ALL_INFO, {"app": "nas"}, operation_id="info-nas", cancelled=lambda: False)
    assert nas["resource_permissions_required"] is True
    assert "ask the owner to select NAS files or directories in the Agent resource scope" in nas["scope_guidance"]
