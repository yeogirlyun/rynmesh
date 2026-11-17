"""S07-I3: validate spawn selections before a child is persisted or dispatched."""
import copy
import json

import pytest

from test_agent_runtime import assistant
from test_agent_subagents import _build_env
from rynmesh.agent_runtime import subagents
from rynmesh.agent_runtime.store import AgentError, AgentToolRejected, DEFAULT_SETTINGS


@pytest.fixture
def env(tmp_path):
    _, runtime, model, _ = _build_env(tmp_path)
    root_model = {"id": "local/gpt-4o", "name": "Parent", "source": "local_api", "available": True,
                  "agent_supported": True, "destination_revision": "rev-parent", "supported_reasoning_efforts": ["low", "high"], "default_reasoning_effort": "low"}
    models = [root_model, {"id": "api/plain", "name": "Plain API", "source": "local_api", "available": True,
                          "agent_supported": True},
              {**root_model, "id": "cli/limited", "name": "Limited", "supported_reasoning_efforts": ["low", "medium"], "default_reasoning_effort": "medium"},
              *[{"id": f"api/option-{i}", "name": f"Option {i}", "source": "local_api", "available": True, "agent_supported": True} for i in reversed(range(7))],
              {"id": "api/offline", "name": "Offline", "source": "local_api", "available": False, "agent_supported": True},
              {"id": "api/no-agent", "name": "Unsupported", "source": "local_api", "available": True, "agent_supported": False}]
    runtime.catalog.list_models = lambda: copy.deepcopy(models)
    model.supports_reasoning_effort = True
    calls = []
    original = model.complete
    def complete(model_id, *args, **kwargs):
        calls.append((model_id, copy.deepcopy(kwargs)))
        return original(model_id, *args, **kwargs)
    model.complete = complete
    parent = runtime.create({"message": "delegate", "model_id": "local/gpt-4o", "reasoning_effort": "high"})["id"]
    return runtime, model, parent, models, calls


def spawn(env, **args):
    runtime, model, parent, _, _ = env
    model.parent_responses.append(assistant(name="ryn_spawn_agent", args={"task_name": "worker", "prompt": "inspect", "role": "explorer", **args}, call_id="spawn_call"))
    runtime.run_once()
    runtime.run_once()
    data = runtime.store.read()
    row = data["tasks"][parent]
    replies = [json.loads(message["content"]) for message in row["messages"] if message.get("tool_call_id") == "spawn_call"]
    return replies[-1], [child for child in data["tasks"].values() if child.get("parent_task_id") == parent]


@pytest.mark.parametrize("model_id", ["wrong/model", "api/offline", "api/no-agent"])
def test_t1_model_rejected_before_creation_with_at_most_five_sorted_suggestions(env, model_id):
    reply, children = spawn(env, model_id=model_id)
    assert not children
    assert reply["error"] == "agent_subagent_model_unknown"
    choices = [entry["id"] for entry in sorted(env[3], key=lambda entry: entry["id"]) if entry["available"] and entry["agent_supported"] is True][:5]
    assert all(choice in reply["correction"] for choice in choices)
    assert all(entry["id"] not in reply["correction"] for entry in env[3]
               if entry["id"] not in choices and entry["id"] != model_id)


def test_t2_inherited_effort_dropped_for_plain_api_and_first_request_succeeds(env):
    reply, children = spawn(env, model_id="api/plain")
    assert reply.get("ok") and len(children) == 1
    child = children[0]
    assert "reasoning_effort" not in child
    assert child["subagent_meta"]["display_effort"] is None
    # Pause the parent to dispatch the queued child's first real model request.
    env[0].store.transaction(lambda data: data["tasks"][env[2]].update(status="paused"))
    env[0].run_once()
    row = env[0].store.read()["tasks"][child["id"]]
    assert row["error"] is None and row["status"] == "completed"
    assert any(model_id == "api/plain" and "reasoning_effort" not in options for model_id, options in env[4])
    assert subagents.project_subagent_summary(row)["reasoning_effort"] == ""


def test_t3_explicit_xhigh_rejected_for_model_capability_not_old_three_level_enum(env):
    reply, children = spawn(env, model_id="cli/limited", reasoning_effort="xhigh")
    assert not children
    assert reply["error"] == "agent_reasoning_effort_unsupported"
    assert "low" in reply["correction"] and "medium" in reply["correction"]


def test_t4_default_subagent_model_and_effort_used_without_explicit_model(env):
    runtime = env[0]
    saved = runtime.update_settings({"default_subagent_model_id": "cli/limited", "default_subagent_reasoning_effort": "medium"})
    assert saved["default_subagent_model_id"] == "cli/limited"
    assert saved["default_subagent_reasoning_effort"] == "medium"
    _, children = spawn(env)
    assert children[0]["model_id"] == "cli/limited"
    assert children[0]["reasoning_effort"] == children[0]["subagent_meta"]["display_effort"] == "medium"


def test_t5_fork_all_ignores_explicit_and_configured_models_and_efforts(env):
    env[0].update_settings({"default_subagent_model_id": "api/plain", "default_subagent_reasoning_effort": "low"})
    _, children = spawn(env, model_id="wrong/model", reasoning_effort="xhigh", fork_turns="all")
    assert children[0]["model_id"] == "local/gpt-4o"
    assert children[0]["reasoning_effort"] == children[0]["subagent_meta"]["display_effort"] == "high"


def test_t6_changed_model_binds_its_own_destination_without_parent_confirmation(env):
    env[0].store.transaction(lambda data: data["tasks"][env[2]].update(model_destination_confirmed=True))
    _, children = spawn(env, model_id="api/plain")
    assert children[0]["model_destination"]["catalog_model_id"] == "api/plain"
    assert not children[0].get("model_destination_confirmed")


def test_explicit_model_beats_settings_and_unsupported_setting_effort_is_omitted(env):
    env[0].update_settings({"default_subagent_model_id": "cli/limited", "default_subagent_reasoning_effort": "xhigh"})
    _, children = spawn(env, model_id="api/plain")
    assert children[0]["model_id"] == "api/plain"
    assert "reasoning_effort" not in children[0]


def test_unsupported_inherited_effort_displays_actual_model_default(env):
    _, children = spawn(env, model_id="cli/limited")
    assert "reasoning_effort" not in children[0]
    assert children[0]["subagent_meta"]["display_effort"] == "medium"


def test_valid_full_effort_vocabulary_reaches_real_child(env):
    env[3][2].update(supported_reasoning_efforts=["low", "xhigh", "max", "ultra"], default_reasoning_effort="low")
    _, children = spawn(env, model_id="cli/limited", reasoning_effort="ultra")
    assert children[0]["reasoning_effort"] == children[0]["subagent_meta"]["display_effort"] == "ultra"
    args = subagents.validate_subagent_tool("ryn_spawn_agent", {"task_name": "x", "prompt": "x", "reasoning_effort": "max"})
    assert args["reasoning_effort"] == "max"
    with pytest.raises(AgentToolRejected):
        subagents.validate_subagent_tool("ryn_spawn_agent", {"task_name": "x", "prompt": "x", "reasoning_effort": "invented"})


@pytest.mark.parametrize("settings", [{"default_subagent_model_id": "wrong/model"}, {"default_subagent_model_id": "api/offline"},
                                      {"default_subagent_model_id": "api/no-agent"}, {"default_subagent_model_id": None},
                                      {"default_subagent_reasoning_effort": "invented"}])
def test_settings_reject_invalid_model_or_effort_without_partial_update(env, settings):
    before = env[0].settings()
    with pytest.raises(AgentError):
        env[0].update_settings(settings)
    assert env[0].settings() == before


def test_empty_subagent_defaults_can_be_reset_and_are_returned_for_legacy_store(env):
    assert DEFAULT_SETTINGS["default_subagent_model_id"] == ""
    assert DEFAULT_SETTINGS["default_subagent_reasoning_effort"] is None
    env[0].update_settings({"default_subagent_model_id": "api/plain", "default_subagent_reasoning_effort": "none"})
    settings = env[0].update_settings({"default_subagent_model_id": "", "default_subagent_reasoning_effort": None})
    assert settings["default_subagent_model_id"] == "" and settings["default_subagent_reasoning_effort"] is None
