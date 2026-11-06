"""自动处理 (automatic) is full access: no dialog for any change, with no tool list.

Owner report 2026-11-06: a task started in 自动处理 still asked for confirmation,
because automatic only ran tools checked in a settings list that starts empty.
"""
import pytest

from rynmesh.agent_runtime.execution_policy import decide
from test_agent_task_forks import env, fork, parent, stored  # noqa: F401  (fixture)


def policy(mode, grants=()):
    return {"permission_mode": mode, "allowed_write_tools": list(grants),
            "desktop_access": False, "local_execution": {"desktop_access": False}}


WRITES = [
    ("ryn_shell_execute", {"command": "python make_summary.py"}, {"effect": "write"}),
    ("ryn_file_write", {"path": "summary.md", "content": "x"}, {"effect": "write"}),
    ("ryn_apply_patch", {"patch": "*** Begin Patch"}, {"effect": "write"}),
    ("mcp__company_workspace__wecom_history", {"chat": "子陵问题群"}, {"effect": "write"}),
    ("ryn_set_model_sharing", {"model_id": "m"}, {"effect": "write"}),
]


@pytest.mark.parametrize("name,arguments,metadata", WRITES)
def test_automatic_runs_every_change_without_a_tool_list(name, arguments, metadata):
    decision = decide({"name": name, "arguments": arguments}, metadata, policy("automatic"), policy("automatic"))
    assert (decision.action, decision.basis) == ("execute", "automatic")


@pytest.mark.parametrize("name,arguments,metadata", WRITES)
@pytest.mark.parametrize("stricter,expected", [("read_only", "deny"), ("confirm_changes", "ask")])
def test_a_stricter_task_or_host_mode_still_wins(name, arguments, metadata, stricter, expected):
    call = {"name": name, "arguments": arguments}
    assert decide(call, metadata, policy("automatic"), policy(stricter)).action == expected
    assert decide(call, metadata, policy(stricter), policy("automatic")).action == expected


def test_workspace_on_either_side_keeps_external_changes_behind_a_dialog():
    call = {"name": "ryn_set_model_sharing", "arguments": {"model_id": "m"}}
    assert decide(call, {"effect": "write"}, policy("automatic"), policy("workspace")).basis == "external_change"
    assert decide(call, {"effect": "write"}, policy("workspace"), policy("automatic")).basis == "external_change"


def test_explicit_approval_tools_still_ask_in_automatic():
    call = {"name": "ryn_craft_install", "arguments": {"app": "wordcraft"}}
    decision = decide(call, {"effect": "write", "requires_explicit_approval": True},
                      policy("automatic"), policy("automatic"))
    assert (decision.action, decision.basis) == ("ask", "explicit_approval")


def test_fork_takes_the_stricter_of_task_and_host_mode(env):  # noqa: F811
    tid = parent(env)
    env.runtime.update_settings({"permission_mode": "automatic"})
    env.runtime.store.transaction(lambda d: d["tasks"][tid].update(permission_mode="workspace"))
    child = fork(env, tid)
    assert stored(env, child["id"])["permission_mode"] == "workspace"
    env.runtime.store.transaction(lambda d: d["tasks"][tid].update(permission_mode="automatic"))
    child = fork(env, tid)
    assert stored(env, child["id"])["permission_mode"] == "automatic"
